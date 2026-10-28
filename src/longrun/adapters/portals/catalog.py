"""Tier 3 as data: open-data portal datasets, described in `catalog.yaml` (M21, ADR 0060).

A city publishing its street closures on Socrata or ArcGIS is not a new kind of source each
time - it is the same two platforms, the same query, and a different table with different
column names. Writing a Python module per dataset would make fifty modules that differ only
in strings, so the strings live in `catalog.yaml` and this module turns each row into an
adapter. One entry point (`portal-catalog`) registers all of them through `registry.expand`,
which is why a bad row is one named `LoadFailure` and never a catalog that vanishes.

**The mapping language stops at five kinds** - `field`, `const`, `map`, `template` and
`point` - and that cap is the design, not a limit reached. A row that needs a sixth (a
lookup in another table, arithmetic between columns, a regular expression) is a dataset
whose meaning is not in its columns, and the honest home for it is a Python adapter whose
code a reviewer can read. Units and timestamp formats are attributes of `field`, not kinds:
they say what a column holds rather than computing something new from it.

**A decision for every place, not only the adoptions.** The file carries `refused:` rows
too, each with a reason from a closed set and what was checked. The fifty largest places
must each have one or the other for closures and for speed surveys, which a test enforces -
so "nobody looked" can never be mistaken for "nothing there".

**Nothing here raises.** A row that does not validate becomes a `LoadFailure` naming it; a
record that does not map is dropped and counted; a dataset whose every record failed is a
reason, never an empty answer.
"""

from __future__ import annotations

import math
import re
import string
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from longrun.adapters.base import LoadFailure, local_to_utc, valid_jurisdiction_id
from longrun.core.models.features import FeatureKind

if TYPE_CHECKING:  # pragma: no cover
    from longrun.adapters.base import AdapterContext, AdapterResult
    from longrun.core.models.features import Feature

#: The packaged catalog. Beside this module so a wheel carries it.
CATALOG = Path(__file__).with_name("catalog.yaml")

#: The confidence a tier-3 record claims, by kind. A portal row is a jurisdiction's own
#: record of what it permitted - better than a model's reading of a page, worse than a feed
#: built to say what is closed - so a closure sits at the NPS adapter's 0.7, below
#: `closures.HARD_FLAG_CONFIDENCE`. A speed survey is different in kind: its number is a
#: measurement the agency took with a radar gun, and ADR 0053 merges nothing under 0.8, so a
#: survey at 0.7 could never reach the graph it was adopted for.
DEFAULT_CONFIDENCE: dict[str, float] = {
    "closures": 0.7,
    "trail_status": 0.7,
    "access_hours": 0.7,
    "speed_survey": 0.85,
}

#: Why a place was not adopted for a kind. Closed, so the refusals can be counted and a
#: reason cannot quietly become "didn't get to it".
RefusalReason = Literal[
    # The jurisdiction's portal(s) were searched and publish nothing of this kind.
    "no_dataset",
    # The jurisdiction has no open-data portal on a platform anyone publishes through.
    "no_portal",
    # Published, but as addresses or prose: nothing a corridor can be tested against.
    "no_geometry",
    # Closures published without the dates that say when they apply.
    "no_window",
    # Published, and not updated within the freshness a closure source needs.
    "stale",
    # Speeds measured at count stations - points, which ADR 0053 cannot match to a way.
    "points_only",
    # Speeds that are not an 85th-percentile survey: probe averages, camera tickets.
    "not_a_survey",
    # Published on a platform this package does not read (CKAN, a bespoke API).
    "platform_unbuilt",
    # A better tier already answers for this place from the same publisher.
    "covered",
]

Platform = Literal["socrata", "arcgis"]

#: Timestamp formats a `field` mapping may declare for `start`, `end` and `observed_on`.
#: `date`: a calendar date, the whole local day; an instant with an offset is first moved to
#: the feed's zone. `local`: a wall-clock time with no offset, in the feed's zone. `utc`: an
#: instant (an offset is honoured; none means UTC). `epoch_ms`: ArcGIS's milliseconds since
#: 1970 UTC. `epoch_ms_date`: an ArcGIS date-only field, the whole day.
TimeFormat = Literal["date", "local", "utc", "epoch_ms", "epoch_ms_date"]

_SLUG = re.compile(r"^portal\.(socrata|arcgis)\.[a-z0-9][a-z0-9-]*$")
_SOURCE = re.compile(r"^portal_[a-z0-9_]+$")
_SOCRATA_ID = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")
_ARCGIS_LAYER = re.compile(r"^/.+/(FeatureServer|MapServer)/\d+$")

_MPH_TO_KPH = 1.609344


class _Mapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldMapping(_Mapping):
    """A column's value, as it is - optionally in a stated unit or timestamp format."""

    kind: Literal["field"]
    field: str
    format: TimeFormat | None = None
    unit: Literal["kph", "mph"] | None = None


class ConstMapping(_Mapping):
    """The same value for every record - a dataset that is all of one category."""

    kind: Literal["const"]
    value: str


class MapMapping(_Mapping):
    """A column's value looked up in a table. An unlisted value takes `default`, and with
    no default the record is dropped: a category nobody mapped is not a guess to make."""

    kind: Literal["map"]
    field: str
    values: dict[str, str]
    default: str | None = None


class TemplateMapping(_Mapping):
    """Several columns joined into one string: `{onstreetname} from {fromstreetname}`."""

    kind: Literal["template"]
    template: str


class PointMapping(_Mapping):
    """A point built from two numeric columns."""

    kind: Literal["point"]
    lat: str
    lon: str


Mapping = Annotated[
    FieldMapping | ConstMapping | MapMapping | TemplateMapping | PointMapping,
    Field(discriminator="kind"),
]

#: Which mapping kinds each slot accepts.
_SLOTS: dict[str, tuple[str, ...]] = {
    "geometry": ("field", "point"),
    "start": ("field",),
    "end": ("field",),
    "category": ("const", "map", "field"),
    "detail": ("template", "field"),
    "ref": ("field", "template"),
    "label": ("field", "template"),
    "hours": ("field", "const", "template"),
    "observed_on": ("field",),
}


class Licence(BaseModel):
    """What the dataset's own metadata declares, verbatim, and where it was read."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    #: The platform's licence id exactly as published (Socrata `licenseId`), or `None` when
    #: the dataset declares none - which is a statement too, and the commonest one.
    id: str | None
    read_on: date
    from_: str = Field(alias="from")

    @field_validator("from_")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("a licence is read from an https URL")
        return value


class Survey(BaseModel):
    """What the adopting survey measured, so a later one can say what changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    on: date
    rows: int = Field(ge=0)
    updated: date
    #: How old the dataset may be before an answer from it is refused. A closure source
    #: that stopped publishing answers "nothing closed" for every day after, which is the
    #: false negative `massdot.py` names; a speed survey ages in years, not days.
    max_staleness_days: int = Field(gt=0)


class CatalogEntry(BaseModel):
    """One adopted dataset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: `portal.<platform>.<slug>`: the adapter name and cache scope, so stable forever.
    name: str
    jurisdiction: str
    kind: FeatureKind
    platform: Platform
    #: The portal's host.
    domain: str
    #: Socrata: the four-by-four id. ArcGIS: the layer's path on `domain`.
    dataset: str
    #: The attribution key: `attribution.LICENCES[source]` names the publisher on a sheet.
    source: str
    #: The IANA zone the dataset's local times are in.
    zone: str
    geometry: Mapping
    category: Mapping
    start: FieldMapping | None = None
    end: FieldMapping | None = None
    detail: Mapping | None = None
    ref: Mapping | None = None
    #: What the record calls its street or gate (`Feature.name`).
    label: Mapping | None = None
    hours: Mapping | None = None
    observed_on: FieldMapping | None = None
    measures: dict[str, FieldMapping] = Field(default_factory=dict)
    #: A platform-native filter ANDed into every query (SoQL or ArcGIS SQL): which rows of
    #: a permits table are closures at all.
    where: str | None = None
    #: Whether the window is filtered on the server. Off for a dataset whose dates are text.
    window_filter: bool = True
    #: Socrata only: a legacy `location` column takes `within_box`, not `intersects`.
    spatial: Literal["intersects", "within_box"] = "intersects"
    #: `feed` reads the whole dataset once a day (a survey); `polygon` asks per corridor.
    scope: Literal["feed", "polygon"] = "polygon"
    complete: bool = True
    facet: str | None = None
    confidence: float | None = Field(default=None, gt=0.5, le=1.0)
    licence: Licence
    survey: Survey
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> CatalogEntry:
        from zoneinfo import ZoneInfo

        from longrun.core.models.features import SPEED_85

        if not _SLUG.match(self.name):
            raise ValueError(f"name {self.name!r} is not portal.<platform>.<slug>")
        if self.name.split(".")[1] != self.platform:
            raise ValueError(f"name {self.name!r} does not name its platform {self.platform}")
        if not valid_jurisdiction_id(self.jurisdiction):
            raise ValueError(f"malformed jurisdiction id {self.jurisdiction!r}")
        if not _SOURCE.match(self.source):
            raise ValueError(f"source {self.source!r} is not portal_<publisher>")
        if self.platform == "socrata" and not _SOCRATA_ID.match(self.dataset):
            raise ValueError(f"{self.dataset!r} is not a Socrata dataset id")
        if self.platform == "arcgis" and not _ARCGIS_LAYER.match(self.dataset):
            raise ValueError(f"{self.dataset!r} is not an ArcGIS layer path")
        try:
            ZoneInfo(self.zone)
        except Exception as exc:  # noqa: BLE001 - any failure is the same complaint
            raise ValueError(f"unknown zone {self.zone!r}") from exc
        for slot, allowed in _SLOTS.items():
            mapping = getattr(self, slot)
            if mapping is not None and mapping.kind not in allowed:
                raise ValueError(f"{slot} takes {'/'.join(allowed)}, not {mapping.kind}")
        for slot in ("start", "end", "observed_on"):
            mapping = getattr(self, slot)
            if mapping is not None and mapping.format is None:
                raise ValueError(f"{slot} must state its timestamp format")
        if self.kind == "speed_survey":
            speed = self.measures.get(SPEED_85)
            if speed is None:
                raise ValueError(f"a speed survey maps measures[{SPEED_85!r}]")
            if any(m.unit is None for m in self.measures.values()):
                raise ValueError("every measure states its unit")
        elif self.measures:
            raise ValueError(f"only a speed survey maps measures, not {self.kind}")
        if (self.kind == "access_hours") != (self.hours is not None):
            raise ValueError("an access_hours dataset, and only one, maps hours")
        return self

    @property
    def url(self) -> str:
        """Where the records are queried."""
        if self.platform == "socrata":
            return f"https://{self.domain}/resource/{self.dataset}.json"
        return f"https://{self.domain}{self.dataset}/query"

    @property
    def page(self) -> str:
        """Where a reader can see the dataset - the `source_url` on every record."""
        if self.platform == "socrata":
            return f"https://{self.domain}/d/{self.dataset}"
        return f"https://{self.domain}{self.dataset}"

    def effective_confidence(self) -> float:
        return self.confidence if self.confidence is not None else DEFAULT_CONFIDENCE[self.kind]


class Refusal(BaseModel):
    """A place and kind looked at and not adopted, with why and what was checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jurisdiction: str
    kind: FeatureKind
    reason: RefusalReason
    #: What was searched or read: queries, dataset ids, URLs. At least one, because a
    #: refusal nobody can retrace is indistinguishable from not having looked.
    checked: list[str] = Field(min_length=1)
    on: date
    note: str

    @model_validator(mode="after")
    def _check(self) -> Refusal:
        if not valid_jurisdiction_id(self.jurisdiction):
            raise ValueError(f"malformed jurisdiction id {self.jurisdiction!r}")
        return self


# --- loading ----------------------------------------------------------------


@dataclass(frozen=True)
class Catalog:
    """Everything `catalog.yaml` says, and every row it could not say."""

    adopted: list[CatalogEntry] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    failures: list[LoadFailure] = field(default_factory=list)


def read(path: Path = CATALOG) -> Catalog:
    """The catalog, row by row. Never raises: a row that does not validate is a failure
    naming it, and a file that does not parse is one failure naming the file."""
    import yaml

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - a broken catalog is not a broken plan
        return Catalog(failures=[LoadFailure(name=path.name, reason=f"unreadable: {exc}")])
    if not isinstance(document, dict):
        return Catalog(failures=[LoadFailure(name=path.name, reason="is not a mapping")])

    catalog = Catalog()
    for index, row in enumerate(document.get("adopted") or []):
        label = str(row.get("name") if isinstance(row, dict) else index)
        try:
            catalog.adopted.append(CatalogEntry.model_validate(row))
        except ValidationError as exc:
            catalog.failures.append(LoadFailure(name=f"catalog[{label}]", reason=_first(exc)))
    for index, row in enumerate(document.get("refused") or []):
        label = (
            f"{row.get('jurisdiction')}/{row.get('kind')}" if isinstance(row, dict) else str(index)
        )
        try:
            catalog.refused.append(Refusal.model_validate(row))
        except ValidationError as exc:
            catalog.failures.append(
                LoadFailure(name=f"catalog.refused[{label}]", reason=_first(exc))
            )
    names = [e.name for e in catalog.adopted]
    for name in sorted({n for n in names if names.count(n) > 1}):
        catalog.failures.append(LoadFailure(name=f"catalog[{name}]", reason="adopted twice"))
    return catalog


def _first(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(p) for p in error.get("loc", ()))
    return f"{where}: {error.get('msg')}" if where else str(error.get("msg"))


def load(path: Path = CATALOG) -> list[CatalogAdapter | LoadFailure]:
    """The entry point's target: an adapter per adopted row, a failure per broken one."""
    catalog = read(path)
    duplicated = {f.name for f in catalog.failures if f.reason == "adopted twice"}
    adapters: list[CatalogAdapter | LoadFailure] = [
        CatalogAdapter(entry)
        for entry in catalog.adopted
        if f"catalog[{entry.name}]" not in duplicated
    ]
    return [*adapters, *(f for f in catalog.failures if not f.name.startswith("catalog.refused"))]


# --- the adapter ------------------------------------------------------------


class CatalogAdapter:
    """One catalog row as an adapter. Tier 3, keyless; a token, if set, only raises a
    rate limit and is never something whose absence skips a rung."""

    tier = 3

    def __init__(self, entry: CatalogEntry) -> None:
        self.entry = entry
        self.name = entry.name
        self.kind = entry.kind
        self.jurisdictions = (entry.jurisdiction,)
        self.source = entry.source
        self.scope = entry.scope
        self.complete = entry.complete
        self.facet = entry.facet
        self.vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        from longrun.adapters.portals.client import fetch_portal

        return fetch_portal(self.entry, polygon, day, ctx)

    def __repr__(self) -> str:
        return f"CatalogAdapter({self.name!r})"


# --- records to features ----------------------------------------------------


@dataclass
class Mapped:
    """What a page of records became."""

    features: list[Feature] = field(default_factory=list)
    dropped: int = 0
    #: Why the first dropped record was dropped - enough to diagnose a whole dataset that
    #: maps to nothing, which is one wrong column name far more often than many problems.
    first_problem: str | None = None


def to_features(entry: CatalogEntry, records: list[dict[str, Any]]) -> Mapped:
    """Records, flattened to one dict each, as `Feature`s. Never raises."""
    from longrun.core.models.features import Feature

    out = Mapped()
    for record in records:
        try:
            values = _values(entry, record)
            out.features.append(Feature(**values))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            out.dropped += 1
            if out.first_problem is None:
                message = _first(exc) if isinstance(exc, ValidationError) else str(exc)
                out.first_problem = message.splitlines()[0][:200]
    return out


def _values(entry: CatalogEntry, record: dict[str, Any]) -> dict[str, Any]:
    geometry = _geometry(entry.geometry, record)
    if geometry is None:
        raise ValueError("no geometry")
    category = _text(entry.category, record)
    if not category:
        raise ValueError("no category")
    values: dict[str, Any] = {
        "kind": entry.kind,
        "category": category,
        "geometry": geometry,
        "tier": 3,
        "confidence": entry.effective_confidence(),
        "jurisdiction": entry.jurisdiction,
        "source_url": entry.page,
    }
    if entry.start is not None:
        values["start"] = _instant(entry.start, record, entry.zone, end=False)
    if entry.end is not None:
        values["end"] = _instant(entry.end, record, entry.zone, end=True)
    for slot, target in (("detail", "detail"), ("ref", "ref"), ("label", "name")):
        mapping = getattr(entry, slot)
        if mapping is not None:
            values[target] = _text(mapping, record)
    if entry.hours is not None:
        values["hours"] = _text(entry.hours, record)
    if entry.observed_on is not None:
        observed = _instant(entry.observed_on, record, entry.zone, end=False, as_date=True)
        values["observed_on"] = observed
    if entry.measures:
        values["measures"] = {name: _measure(m, record) for name, m in entry.measures.items()}
    return values


def _raw(record: dict[str, Any], name: str) -> Any:
    value = record.get(name)
    return None if value in ("", None) else value


def _text(mapping: Any, record: dict[str, Any]) -> str | None:
    if mapping.kind == "const":
        return str(mapping.value)
    if mapping.kind == "field":
        value = _raw(record, mapping.field)
        return None if value is None else " ".join(str(value).split())
    if mapping.kind == "map":
        value = _raw(record, mapping.field)
        key = None if value is None else str(value).strip()
        if key is not None and key in mapping.values:
            return str(mapping.values[key])
        if mapping.default is None:
            raise ValueError(f"{mapping.field}={value!r} is not in the category map")
        return str(mapping.default)
    if mapping.kind == "template":
        names = [n for _, n, _, _ in string.Formatter().parse(mapping.template) if n]
        filled = mapping.template.format(
            **{n: "" if _raw(record, n) is None else _raw(record, n) for n in names}
        )
        return " ".join(filled.split()) or None
    raise ValueError(f"a {mapping.kind} mapping is not text")


def _geometry(mapping: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    if mapping.kind == "point":
        lat, lon = _raw(record, mapping.lat), _raw(record, mapping.lon)
        if lat is None or lon is None:
            return None
        return _point(float(lon), float(lat))
    value = _raw(record, mapping.field)
    if isinstance(value, dict):
        if value.get("type") and value.get("coordinates") is not None:
            return {"type": value["type"], "coordinates": value["coordinates"]}
        # Socrata's legacy `location` column: a dict of strings, not GeoJSON.
        if value.get("latitude") is not None and value.get("longitude") is not None:
            return _point(float(value["longitude"]), float(value["latitude"]))
    return None


def _point(lon: float, lat: float) -> dict[str, Any] | None:
    if not (math.isfinite(lon) and math.isfinite(lat)) or (lon == 0 and lat == 0):
        return None
    return {"type": "Point", "coordinates": [lon, lat]}


def _measure(mapping: FieldMapping, record: dict[str, Any]) -> float:
    value = _raw(record, mapping.field)
    if value is None:
        raise ValueError(f"no {mapping.field}")
    number = float(value)
    if mapping.unit == "mph":
        number *= _MPH_TO_KPH
    return round(number, 2)


def _instant(
    mapping: FieldMapping, record: dict[str, Any], zone: str, *, end: bool, as_date: bool = False
) -> Any:
    """A column's timestamp as naive UTC (ADR 0046), or a local date for `observed_on`."""
    from zoneinfo import ZoneInfo

    value = _raw(record, mapping.field)
    if value is None:
        return None
    fmt = mapping.format
    if fmt in ("epoch_ms", "epoch_ms_date"):
        moment = datetime.fromtimestamp(float(value) / 1000.0, UTC)
        if fmt == "epoch_ms" and not as_date:
            return moment.replace(tzinfo=None)
        # A date-only ArcGIS field is midnight of the day, stored either as UTC midnight or
        # as local midnight converted to UTC. Both are read as the day they name.
        local = moment.astimezone(ZoneInfo(zone))
        day = moment.date() if moment.time() == time(0) else local.date()
        return day if as_date else _whole_day(day, zone, end=end)
    moment = datetime.fromisoformat(str(value).strip())
    if fmt == "date":
        if moment.tzinfo is not None:
            moment = moment.astimezone(ZoneInfo(zone))
        return moment.date() if as_date else _whole_day(moment.date(), zone, end=end)
    if as_date:
        return moment.date()
    if fmt == "local":
        if moment.tzinfo is not None:
            return moment.astimezone(UTC).replace(tzinfo=None)
        return local_to_utc(moment, zone)
    # utc
    if moment.tzinfo is not None:
        return moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def _whole_day(day: date, zone: str, *, end: bool) -> datetime:
    """A calendar date as the instant it starts or, for an end, the last second it lasts."""
    clock = time(23, 59, 59) if end else time(0)
    return local_to_utc(datetime.combine(day, clock), zone)


#: The entry point's target (`portal-catalog`), built once at import.
ADAPTERS = load()

__all__ = [
    "ADAPTERS",
    "CATALOG",
    "DEFAULT_CONFIDENCE",
    "Catalog",
    "CatalogAdapter",
    "CatalogEntry",
    "Licence",
    "Mapped",
    "Refusal",
    "RefusalReason",
    "Survey",
    "load",
    "read",
    "to_features",
]
