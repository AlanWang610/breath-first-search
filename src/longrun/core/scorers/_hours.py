"""Opening hours, read as the small language they are (OSM `opening_hours`; M18).

Two scorers ask "is this open when the runner gets there": `resupply_schedule` of a café or
a fountain, and `access_hours` of a park gate. Both ask in OSM's `opening_hours` -
`Mo-Fr 07:00-19:00; Sa 08:00-17:00; PH off`, `sunrise-sunset`, `May-Oct: 06:00-22:00` -
and until M18 the parser lived inside `resupply_schedule` and read one sentence of it: an
optional day list and a single time range.

**Three answers, and how they combine.** `True` open, `False` closed, `None` not
understood. A rule nobody could read is not evidence the door is locked, so it may never be
the reason an answer is `False`:

* the rules that *were* read are evaluated as OSM specifies them - a later rule that
  selects the day replaces the earlier ones for that day, a rule joined by `,` adds to the
  one before it, and a day no rule selects is closed - with one exception for a common
  slip, `10:00-14:30; 17:30-20:30`, which `_same_days` explains;
* if that says open, the answer is open;
* if it says closed and some rule could not be read, the answer is `None`, because the
  rule nobody could read may be the one that opens it.

That replaces "ignore what you cannot read", which made a restaurant posting
`We, Th 11:00-22:00; Su 11:00-20:00` *closed* at Wednesday lunchtime because only the
Sunday rule parsed.

**What is read:** weekday lists and ranges (`Mo-Fr`, `We, Th`, `Sa-Mo`); month ranges and
dates (`Apr-Oct`, `May-Sep:`, `Dec 24`, `Dec 24-Jan 02`); several time ranges in one rule
(`11:00-14:00,17:00-21:00`); times past midnight (`22:00-02:00`, `10:00-25:00`), which
spill into the next morning; `dawn`, `sunrise`, `sunset` and `dusk`, with bracketed offsets
(`(sunrise-00:30)`); the modifiers `off`, `closed`, `open` and `unknown`; `24/7`; and quoted
comments, which say nothing about the hours and are dropped.

**What is not, and is unknown:** nth weekdays (`Th[4]`), week numbers, years, Easter,
open-ended times (`18:00+`) and fallback rules (`||`). `PH` and `SH` are read and **never
match**: nothing here knows a holiday calendar, and assuming today is not a holiday keeps
every ordinary day's answer right where any other assumption would trade it for the
holiday's.

**A sun-relative time needs a sun.** The caller passes `sun`, a function from a local date
to that day's `SunEvents` at the place (`sun_at` builds one). Without it a rule that needs
the sun cannot be evaluated, and is not understood for that instant.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from longrun.core.geo.solar import SunEvents, sun_events

#: What `is_open` needs to read `sunrise`: the day's events at the place, by local date.
SunOn = Callable[[date], SunEvents | None]

DAY_MIN = 24 * 60

#: The latest end a rule may state. OSM allows times past midnight (`25:00` is 01:00 the
#: next morning); anything beyond a second day is a typo rather than a schedule.
_MAX_MIN = 48 * 60

_WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_HOLIDAYS = ("ph", "sh")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_EVENTS = ("dawn", "sunrise", "sunset", "dusk")
_STATES: dict[str, bool | None] = {"open": True, "off": False, "closed": False, "unknown": None}

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<comment>\"[^\"]*\")"
    r"|(?P<always>24/7)"
    r"|(?P<time>\d{1,2}:\d{2})"
    r"|(?P<number>\d+)"
    r"|(?P<word>[A-Za-z]+)"
    r"|(?P<punct>[-,:()+])"
    r")"
)

_Token = tuple[str, str]


class _Unread(ValueError):
    """A rule in a part of the language this module does not speak."""


@dataclass(frozen=True)
class _Time:
    """A clock time in minutes from local midnight, or an offset from a sun event."""

    minutes: float
    event: str | None = None

    def resolve(self, sun: SunOn | None, day: date) -> float | None:
        if self.event is None:
            return self.minutes
        events = sun(day) if sun is not None else None
        at = getattr(events, self.event) if events is not None else None
        if at is None:
            return None
        return at.hour * 60 + at.minute + at.second / 60 + self.minutes


@dataclass(frozen=True)
class _Rule:
    """One rule: which days it selects, which times it covers, and what it says of them."""

    #: Month-day ranges as `month * 100 + day`, inclusive; wrapping across New Year when the
    #: first is later than the second. `None` selects every date.
    dates: tuple[tuple[int, int], ...] | None
    #: Monday is 0. `None` selects every weekday; an empty set (`PH off`) selects none.
    weekdays: frozenset[int] | None
    #: Time ranges; empty means the whole day.
    spans: tuple[tuple[_Time, _Time], ...]
    state: bool | None
    #: Joined to the rule before it by `,`, so it adds rather than replaces.
    additional: bool

    def selects(self, day: date) -> bool:
        if self.weekdays is not None and day.weekday() not in self.weekdays:
            return False
        if self.dates is None:
            return True
        key = day.month * 100 + day.day
        return any(lo <= key <= hi if lo <= hi else key >= lo or key <= hi for lo, hi in self.dates)

    def covers(self, minute: float, day: date, sun: SunOn | None) -> bool | None:
        """Whether `minute` past `day`'s midnight falls inside this rule's times.

        Inclusive at both ends, as the parser this replaces was: "07:00-19:00" is open at
        19:00, and changing that would move a waypoint for nothing anyone can verify.
        """
        if not self.spans:
            return 0 <= minute < DAY_MIN
        unknown = False
        for start_at, end_at in self.spans:
            start, end = start_at.resolve(sun, day), end_at.resolve(sun, day)
            if start is None or end is None:
                unknown = True
                continue
            if end < start:
                end += DAY_MIN  # 22:00-02:00, or sunset-sunrise: past midnight
            if start <= minute <= end:
                return True
        return None if unknown else False


class _Parser:
    """Recursive descent over one `;`-separated part, which may hold `,`-joined rules."""

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self, ahead: int = 0) -> _Token | None:
        j = self.i + ahead
        return self.tokens[j] if j < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise _Unread("the rule ends early")
        self.i += 1
        return token

    def accept(self, kind: str, text: str | None = None) -> bool:
        token = self.peek()
        if token is not None and token[0] == kind and (text is None or token[1] == text):
            self.i += 1
            return True
        return False

    def rules(self) -> list[_Rule]:
        found = [self.rule(additional=False)]
        while self.accept("punct", ","):
            found.append(self.rule(additional=True))
        if self.peek() is not None:
            raise _Unread(f"unread from {self.peek()!r}")
        return found

    def rule(self, *, additional: bool) -> _Rule:
        start = self.i
        dates: tuple[tuple[int, int], ...] | None
        weekdays: frozenset[int] | None
        spans: tuple[tuple[_Time, _Time], ...]
        if self.accept("always"):
            dates, weekdays, spans = None, None, ()
        else:
            dates, weekdays, spans = self.dates(), self.weekdays(), self.spans()
        state: bool | None = True
        token = self.peek()
        if token is not None and token[0] == "word" and token[1].lower() in _STATES:
            self.take()
            state = _STATES[token[1].lower()]
        if self.i == start:
            raise _Unread("a rule that selects nothing and says nothing")
        while self.accept("comment"):
            pass
        return _Rule(dates, weekdays, spans, state, additional)

    # --- dates --------------------------------------------------------------

    @staticmethod
    def _month(token: _Token | None) -> int | None:
        if token is not None and token[0] == "word" and token[1].lower() in _MONTHS:
            return _MONTHS.index(token[1].lower()) + 1
        return None

    def _day(self) -> int | None:
        token = self.peek()
        if token is not None and token[0] == "number" and 1 <= int(token[1]) <= 31:
            self.take()
            return int(token[1])
        return None

    def dates(self) -> tuple[tuple[int, int], ...] | None:
        ranges: list[tuple[int, int]] = []
        while (first := self._month(self.peek())) is not None:
            self.take()
            first_day = self._day()
            last, last_day = first, first_day
            if self.accept("punct", "-"):
                month = self._month(self.peek())
                if month is not None:
                    self.take()
                    last, last_day = month, self._day()
                elif first_day is not None and (day := self._day()) is not None:
                    last_day = day  # Dec 24-26
                else:
                    raise _Unread("a date range with no end")
            ranges.append((first * 100 + (first_day or 1), last * 100 + (last_day or 31)))
            if self.peek() == ("punct", ",") and self._month(self.peek(1)) is not None:
                self.take()
                continue
            break
        if not ranges:
            return None
        self.accept("punct", ":")
        return tuple(ranges)

    # --- weekdays -----------------------------------------------------------

    @staticmethod
    def _weekday(token: _Token | None) -> str | None:
        if token is not None and token[0] == "word":
            text = token[1].lower()
            if text in _WEEKDAYS or text in _HOLIDAYS:
                return text
        return None

    def weekdays(self) -> frozenset[int] | None:
        days: set[int] = set()
        found = False
        while (text := self._weekday(self.peek())) is not None:
            self.take()
            found = True
            if text in _HOLIDAYS:
                pass  # read, and never matching - see the module docstring
            elif self.accept("punct", "-"):
                end = self._weekday(self.peek())
                if end is None or end in _HOLIDAYS:
                    raise _Unread("a weekday range with no end")
                self.take()
                lo, hi = _WEEKDAYS.index(text), _WEEKDAYS.index(end)
                days.update(range(lo, hi + 1) if lo <= hi else [*range(lo, 7), *range(0, hi + 1)])
            else:
                days.add(_WEEKDAYS.index(text))
            if self.peek() == ("punct", ",") and self._weekday(self.peek(1)) is not None:
                self.take()
                continue
            break
        return frozenset(days) if found else None

    # --- times --------------------------------------------------------------

    @staticmethod
    def _starts_time(token: _Token | None) -> bool:
        if token is None:
            return False
        kind, text = token
        return (
            kind == "time"
            or token == ("punct", "(")
            or (kind == "word" and text.lower() in _EVENTS)
        )

    def spans(self) -> tuple[tuple[_Time, _Time], ...]:
        found: list[tuple[_Time, _Time]] = []
        while self._starts_time(self.peek()):
            start = self.time()
            if not self.accept("punct", "-"):
                raise _Unread("a time with no range")
            end = self.time()
            if self.peek() == ("punct", "+"):
                raise _Unread("an open-ended time")
            found.append((start, end))
            if self.peek() == ("punct", ",") and self._starts_time(self.peek(1)):
                self.take()
                continue
            break
        return tuple(found)

    def time(self) -> _Time:
        kind, text = self.take()
        if kind == "time":
            return _Time(_clock(text))
        if kind == "word" and text.lower() in _EVENTS:
            return _Time(0.0, text.lower())
        if (kind, text) == ("punct", "("):
            event = self.take()
            sign = self.take()
            offset = self.take()
            if (
                event[0] != "word"
                or event[1].lower() not in _EVENTS
                or sign not in (("punct", "+"), ("punct", "-"))
                or offset[0] != "time"
                or not self.accept("punct", ")")
            ):
                raise _Unread("a sun offset this module does not read")
            minutes = _clock(offset[1])
            return _Time(minutes if sign[1] == "+" else -minutes, event[1].lower())
        raise _Unread(f"not a time: {text!r}")


def _clock(text: str) -> float:
    hour, _, minute = text.partition(":")
    value = int(hour) * 60 + int(minute)
    if int(minute) >= 60 or value > _MAX_MIN:
        raise _Unread(f"not a clock time: {text!r}")
    return float(value)


def _tokens(text: str) -> list[_Token]:
    found: list[_Token] = []
    at = 0
    while at < len(text):
        if text[at:].strip() == "":
            break
        match = _TOKEN.match(text, at)
        if match is None or match.end() == at or match.lastgroup is None:
            raise _Unread(f"unread from {text[at:]!r}")
        found.append((match.lastgroup, match.group(match.lastgroup)))
        at = match.end()
    return found


def _parts(spec: str) -> list[str]:
    """Split on `;` outside quoted comments."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in spec:
        if char == '"':
            quoted = not quoted
        if char == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


@dataclass(frozen=True)
class Hours:
    """One `opening_hours` value, read once."""

    rules: tuple[_Rule, ...]
    #: How many `;`-separated parts could not be read.
    unread: int

    def state(self, when: datetime, sun: SunOn | None = None) -> bool | None:
        """Open, closed, or not understood, at a naive **local** time (ADR 0046 §5)."""
        day = when.date()
        minute = when.hour * 60 + when.minute + when.second / 60
        today = _state_on(self.rules, day, minute, sun)
        # Yesterday's rules, for the part of a range past its midnight (Fr 22:00-02:00 is
        # open at 01:00 on Saturday).
        spill = _state_on(self.rules, day - timedelta(days=1), minute + DAY_MIN, sun)
        if today is True or spill is True:
            return True
        if today is None or spill is None:
            return None
        return None if self.unread else False

    def next_open(
        self, when: datetime, sun: SunOn | None = None, horizon: timedelta = timedelta(days=1)
    ) -> datetime | None:
        """The first moment at or after `when`, within `horizon`, that reads open."""
        if self.state(when, sun) is True:
            return when
        candidates: set[datetime] = set()
        for offset in range(-1, horizon.days + 2):
            day = when.date() + timedelta(days=offset)
            midnight = datetime.combine(day, time())
            for rule in self.rules:
                if rule.state is not True or not rule.selects(day):
                    continue
                if not rule.spans:
                    candidates.add(midnight)
                for start, _ in rule.spans:
                    minutes = start.resolve(sun, day)
                    if minutes is not None:
                        candidates.add(midnight + timedelta(minutes=minutes))
        for candidate in sorted(candidates):
            if when < candidate <= when + horizon and self.state(candidate, sun) is True:
                return candidate
        return None


def _state_on(rules: tuple[_Rule, ...], day: date, minute: float, sun: SunOn | None) -> bool | None:
    """What the rules that select `day` say about `minute` past its midnight.

    OSM's rule order: a rule selecting the day replaces those before it, a `,`-joined one
    adds to them, and among the survivors the last whose times cover the minute decides -
    which is how `Mo-Fr 08:00-18:00, We 12:00-13:00 off` closes Wednesday lunch.
    """
    effective: list[_Rule] = []
    for rule in rules:
        if rule.selects(day):
            effective = [*effective, rule] if rule.additional else [rule]
    state: bool | None = False
    for rule in effective:
        covered = rule.covers(minute, day, sun)
        if covered is True:
            state = rule.state
        elif covered is None and state is not rule.state:
            state = None
    return state


@lru_cache(maxsize=4096)
def read_hours(spec: str) -> Hours:
    """Read an `opening_hours` value. Never raises: what cannot be read is counted."""
    rules: list[_Rule] = []
    unread = 0
    for part in _parts(spec):
        if "||" in part:
            unread += 1
            continue
        try:
            found = _Parser(_tokens(part)).rules()
        except _Unread:
            unread += 1
            continue
        if rules and _same_days(rules[-1], found[0]) and found[0].spans:
            found[0] = replace(found[0], additional=True)
        rules.extend(found)
    return Hours(tuple(rules), unread)


def _same_days(before: _Rule, after: _Rule) -> bool:
    """Whether two rules select exactly the same days, by what they say rather than by
    which dates that happens to be.

    The one place this module departs from OSM. Strictly, `10:00-14:30; 17:30-20:30` is a
    café open only for dinner - the second rule selects the same days and replaces the
    first - and OSM's own validator warns that the mapper almost certainly meant `,`.
    Read strictly, the mapper's slip becomes a place shut at lunch, which is exactly the
    manufactured closure this module exists not to make. So a `;` rule *with times* that
    selects precisely the days the rule before it did adds to it. `Mo-Fr 08:00-18:00;
    We off` is untouched (different days), and so is `Mo-Fr 08:00-18:00; Mo-Fr off` (no
    times: the mapper said the whole day).
    """
    return (before.dates, before.weekdays) == (after.dates, after.weekdays)


def is_open(spec: str | None, when: datetime, sun: SunOn | None = None) -> bool | None:
    """`True` open, `False` closed, `None` not understood, at a naive local time.

    `None` is the load-bearing answer: it means this module does not speak the dialect, not
    that the door is locked. No hours at all is open - a park fountain has none.
    """
    if spec is None or not spec.strip():
        return True
    return read_hours(spec).state(when, sun)


def next_open(
    spec: str | None,
    when: datetime,
    sun: SunOn | None = None,
    horizon: timedelta = timedelta(days=1),
) -> datetime | None:
    """The first moment at or after `when` this reads open, or `None` within `horizon`."""
    if spec is None or not spec.strip():
        return when
    return read_hours(spec).next_open(when, sun, horizon)


def sun_at(lat: float, lon: float, utc_offset_hours: float) -> SunOn:
    """The `sun` argument for a place: its day's events, computed only if a rule asks."""
    return lambda day: sun_events(lat, lon, day, utc_offset_hours)


__all__ = ["DAY_MIN", "Hours", "SunOn", "is_open", "next_open", "read_hours", "sun_at"]
