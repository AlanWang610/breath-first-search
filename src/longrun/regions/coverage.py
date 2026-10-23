"""National adapter coverage, measured rather than asserted (scope 7.10, 13; M17).

"Nationwide" is a claim about the country, and routes are the wrong instrument for it: a
plan only resolves jurisdictions inside a frozen fixture, so a Georgia adapter could be
written, tested and registered and never once be consulted by a real plan. This module asks
the question directly - for every state, county and incorporated place in the United
States, which adapter claims it, at what tier, and can it actually be asked - against a
committed table of every such jurisdiction.

**The table is Census Bureau data, built once and committed.** `build_table` reads the
Population Estimates Program's sub-county and county files (public domain, as works of the
US government) and writes `data/us_jurisdictions.csv.gz`. Two choices in it are worth
stating:

* *Incorporated places only.* A census-designated place has no government and publishes
  nothing, so no adapter could ever claim one; counting CDPs would make every coverage
  percentage a statement about statistical areas. The estimates file covers exactly the
  places that govern themselves.
* *A place's counties come from the estimates' "place part within county" rows*, not from
  geometry. `core.data.jurisdictions` decides the same containment by area share for a
  route; the two agree except for a place with an unpopulated sliver in a second county,
  where the table omits the county a plan would include. That errs toward reporting less
  coverage than a plan would find, which is the safe direction for a coverage claim.

**County subdivisions are counted and never claimed.** Sixteen thousand townships and New
England towns are functioning governments, and the jurisdiction id grammar has no level for
them (`adapters.base._ID_PATTERN`). They are reported per state as governments no adapter
can currently be registered against, rather than silently left out of the denominator.

**Park agencies are not in the table.** PAD-US is loaded per region, not nationally, and an
agency is a class of manager rather than a place with a population. For the two park kinds
the report lists which agencies are claimed instead.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from longrun.core.data.jurisdictions import from_tiger_row

if TYPE_CHECKING:  # pragma: no cover
    from longrun.adapters.base import Adapter
    from longrun.core.models.features import FeatureKind
    from longrun.core.models.jurisdiction import Jurisdiction

#: The committed table. Packaged beside this module so the report works from a wheel.
TABLE = Path(__file__).with_name("data") / "us_jurisdictions.csv.gz"

COLUMNS = ("id", "level", "name", "statefp", "counties", "population")

#: Estimates-file summary levels read, and what each becomes.
_STATE, _COUNTY, _PLACE_PART, _PLACE, _CONSOLIDATED_PART, _COUSUB = (
    "040",
    "050",
    "157",
    "162",
    "172",
    "061",
)

#: How many of the largest places `--largest` reports by default: the "top 50" M21 targets.
LARGEST_DEFAULT = 50


@dataclass(frozen=True)
class NationalJurisdiction:
    """One row of the table, as the `Jurisdiction` a plan would mint for it."""

    jurisdiction: Jurisdiction | None
    level: str
    id: str
    name: str
    statefp: str
    population: int | None


@dataclass(frozen=True)
class CoverageRow:
    """Who claims one jurisdiction for one kind, at what tier, and whether they can answer."""

    kind: str
    id: str
    level: str
    name: str
    statefp: str
    population: int | None
    #: The best tier any claimant holds, or `None` if nobody claims it.
    tier: int | None
    adapters: tuple[str, ...]
    #: Every claimant at the best tier needs a key that is not set, so nothing there can
    #: be asked. Distinct from `tier is None`: somebody has written the adapter.
    key_missing: bool


# --- building the table -----------------------------------------------------


def build_table(sub_est: Path, co_est: Path) -> list[dict[str, str]]:
    """Rows for every state, county, incorporated place and functioning county subdivision.

    `sub_est` is the PEP sub-county totals file (`sub-est2023.csv`), `co_est` the county
    totals (`co-est2023-alldata.csv`). Both are Latin-1, which is what the Bureau publishes.
    """
    sub_rows = list(csv.DictReader(io.StringIO(sub_est.read_text(encoding="latin-1"))))
    co_rows = list(csv.DictReader(io.StringIO(co_est.read_text(encoding="latin-1"))))
    year = _latest_year(sub_rows[0] if sub_rows else {})

    parts: dict[str, set[str]] = {}
    for row in sub_rows:
        if row["SUMLEV"] == _PLACE_PART:
            geoid = row["STATE"] + row["PLACE"]
            parts.setdefault(geoid, set()).add(row["STATE"] + row["COUNTY"])

    out: dict[str, dict[str, str]] = {}

    def add(record: dict[str, str]) -> None:
        # A consolidated city's parts are listed twice - once as a place (162) and once as
        # a part of the consolidation (172). The plain place row is the one TIGER draws.
        out.setdefault(record["id"], record)

    for row in sub_rows:
        level = row["SUMLEV"]
        population = row.get(f"POPESTIMATE{year}", "")
        if level == _STATE:
            add(
                _row(
                    f"tiger:state:{row['STATE']}",
                    "state",
                    row["NAME"],
                    row["STATE"],
                    (),
                    population,
                )
            )
        elif level in (_PLACE, _CONSOLIDATED_PART):
            geoid = row["STATE"] + row["PLACE"]
            add(
                _row(
                    f"tiger:place:{geoid}",
                    "place",
                    row["NAME"],
                    row["STATE"],
                    tuple(sorted(parts.get(geoid, ()))),
                    population,
                )
            )
        elif level == _COUSUB and row["FUNCSTAT"] == "A":
            geoid = row["STATE"] + row["COUNTY"] + row["COUSUB"]
            add(_row(f"cousub:{geoid}", "cousub", row["NAME"], row["STATE"], (), population))

    for row in co_rows:
        if row["SUMLEV"] != _COUNTY:
            continue
        geoid = row["STATE"] + row["COUNTY"]
        add(
            _row(
                f"tiger:county:{geoid}",
                "county",
                row["CTYNAME"],
                row["STATE"],
                (),
                row.get(f"POPESTIMATE{year}", ""),
            )
        )

    order = {"state": 0, "county": 1, "place": 2, "cousub": 3}
    return sorted(out.values(), key=lambda r: (r["statefp"], order[r["level"]], r["id"]))


def _latest_year(row: dict[str, str]) -> str:
    """The most recent `POPESTIMATEyyyy` column, so the builder survives a new vintage."""
    years = sorted(k.removeprefix("POPESTIMATE") for k in row if k.startswith("POPESTIMATE"))
    years = [y for y in years if y.isdigit()]
    return years[-1] if years else ""


def _row(
    jid: str, level: str, name: str, statefp: str, counties: tuple[str, ...], population: str
) -> dict[str, str]:
    return {
        "id": jid,
        "level": level,
        "name": name,
        "statefp": statefp,
        "counties": " ".join(counties),
        "population": population.strip(),
    }


def write_table(rows: Sequence[dict[str, str]], path: Path = TABLE) -> None:
    """Gzipped CSV with no mtime and no filename in the header, so rebuilding identical data
    is an identical file wherever it is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as handle,
    ):
        handle.write(text.getvalue().encode("utf-8"))


def load_table(path: Path = TABLE) -> list[NationalJurisdiction]:
    """The committed table, each row built with the constructor a plan uses."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    out: list[NationalJurisdiction] = []
    for row in rows:
        level = row["level"]
        geoid = row["id"].rsplit(":", 1)[-1]
        counties = tuple(row["counties"].split()) if row["counties"] else ()
        jurisdiction = (
            from_tiger_row(level, geoid, row["name"], row["statefp"], counties)
            if level in ("state", "county", "place")
            else None
        )
        population = int(row["population"]) if row["population"].isdigit() else None
        out.append(
            NationalJurisdiction(
                jurisdiction=jurisdiction,
                level=level,
                id=row["id"],
                name=row["name"],
                statefp=row["statefp"],
                population=population,
            )
        )
    return out


# --- measuring it -----------------------------------------------------------


def coverage_table(
    adapters: Sequence[Adapter],
    jurisdictions: Sequence[NationalJurisdiction],
    kinds: Sequence[FeatureKind],
) -> list[CoverageRow]:
    """Every census jurisdiction, for every kind, with its best claimant.

    Matched with `registry.matches` - the function a plan's registry uses - so the report
    counts exactly what a plan would find, and park kinds are skipped because no census
    jurisdiction is a park agency.
    """
    from longrun.adapters.base import key_of
    from longrun.adapters.registry import matches

    rows: list[CoverageRow] = []
    for kind in kinds:
        if kind in ("trail_status", "access_hours"):
            continue
        for entry in jurisdictions:
            if entry.jurisdiction is None:
                continue
            claimants = [a for a in adapters if matches(a, kind, entry.jurisdiction)]
            tier = min((int(a.tier) for a in claimants), default=None)
            best = [a for a in claimants if int(a.tier) == tier]
            rows.append(
                CoverageRow(
                    kind=kind,
                    id=entry.id,
                    level=entry.level,
                    name=entry.name,
                    statefp=entry.statefp,
                    population=entry.population,
                    tier=tier,
                    adapters=tuple(sorted(a.name for a in claimants)),
                    key_missing=bool(best)
                    and all((key := key_of(a)) is not None and key.value() is None for a in best),
                )
            )
    return rows


def claimed_agencies(adapters: Sequence[Adapter], kind: FeatureKind) -> list[dict[str, Any]]:
    """For a park kind, which PAD-US agencies are claimed, by whom, and whether keyed."""
    from longrun.adapters.base import key_of

    out: list[dict[str, Any]] = []
    for adapter in sorted(adapters, key=lambda a: (a.tier, a.name)):
        if adapter.kind != kind:
            continue
        key = key_of(adapter)
        out.extend(
            {
                "agency": claimed,
                "adapter": adapter.name,
                "tier": int(adapter.tier),
                "key_missing": key is not None and key.value() is None,
            }
            for claimed in adapter.jurisdictions
            if claimed.startswith("padus:")
        )
    return out


def summarise(
    rows: Sequence[CoverageRow], jurisdictions: Sequence[NationalJurisdiction]
) -> dict[str, Any]:
    """Per kind: states answered by tier, population share by county, places and townships.

    **Population share is measured on counties**, because counties partition the country
    and places do not: summing covered place populations would count a city twice where it
    spans two counties and miss everyone outside a city. A county counts at the best tier
    that claims it, and a key that is not set counts as not covered - the adapter exists and
    cannot be asked, which is what `key_missing` is for.
    """
    total = sum(e.population or 0 for e in jurisdictions if e.level == "county")
    cousubs: dict[str, int] = {}
    for entry in jurisdictions:
        if entry.level == "cousub":
            cousubs[entry.statefp] = cousubs.get(entry.statefp, 0) + 1

    out: dict[str, Any] = {"population_total": total, "cousubs_uncoverable": cousubs}
    for kind in sorted({r.kind for r in rows}):
        of_kind = [r for r in rows if r.kind == kind]
        usable = [r for r in of_kind if r.tier is not None and not r.key_missing]
        states = {r.statefp: r for r in of_kind if r.level == "state"}
        counties = [r for r in usable if r.level == "county"]
        by_tier: dict[str, int] = {}
        for r in counties:
            by_tier[str(r.tier)] = by_tier.get(str(r.tier), 0) + (r.population or 0)
        out[kind] = {
            "states_by_tier": {
                str(t): sorted(s for s, r in states.items() if r.tier == t and not r.key_missing)
                for t in (1, 2, 3, 4)
            },
            "states_key_missing": sorted(s for s, r in states.items() if r.key_missing),
            "states_uncovered": sorted(s for s, r in states.items() if r.tier is None),
            "county_population_by_tier": dict(sorted(by_tier.items())),
            "county_population_share": round(sum(by_tier.values()) / total, 4) if total else 0.0,
            "places_covered": sum(1 for r in usable if r.level == "place"),
            "places_total": sum(1 for r in of_kind if r.level == "place"),
        }
    return out


def largest(
    rows: Sequence[CoverageRow], kind: FeatureKind, n: int = LARGEST_DEFAULT
) -> list[CoverageRow]:
    """The `n` most populous places and how each is covered for one kind."""
    places = [r for r in rows if r.kind == kind and r.level == "place"]
    return sorted(places, key=lambda r: -(r.population or 0))[:n]


__all__ = [
    "COLUMNS",
    "LARGEST_DEFAULT",
    "TABLE",
    "CoverageRow",
    "NationalJurisdiction",
    "build_table",
    "claimed_agencies",
    "coverage_table",
    "largest",
    "load_table",
    "summarise",
    "write_table",
]
