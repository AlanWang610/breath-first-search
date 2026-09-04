"""Per-source attribution, rendered into every plan sheet (scope 14).

The project is open source and some outputs are derivative databases, so attribution is a
licence obligation rather than a courtesy. Building it here — keyed by the same source
names the coverage manifest uses — means a sheet cannot render a source's data without
also rendering its attribution, and adding a new source without a licence entry shows up
immediately as an unattributed line.

ODbL is the one that bites: the offline LTS table and any published graph are derivative
databases of OSM, so distributing them carries share-alike obligations.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SourceLicence(BaseModel):
    """One data source's licence and the attribution it requires."""

    model_config = ConfigDict(frozen=True)

    source: str
    licence: str
    attribution: str | None = None
    share_alike: bool = False

    def line(self) -> str:
        text = self.attribution or self.source
        return f"{text} ({self.licence})"


#: Scope 14's table. Keyed by the source names scorers put in the coverage manifest.
LICENCES: dict[str, SourceLicence] = {
    "osm": SourceLicence(
        source="osm",
        licence="ODbL 1.0",
        attribution="Map data © OpenStreetMap contributors",
        share_alike=True,
    ),
    "overture": SourceLicence(
        source="overture",
        licence="mixed by theme (ODbL / CDLA-Permissive)",
        attribution="Overture Maps Foundation",
        share_alike=True,
    ),
    "hpms": SourceLicence(source="hpms", licence="US public domain"),
    "3dep": SourceLicence(source="3dep", licence="US public domain"),
    "nhd": SourceLicence(source="nhd", licence="US public domain"),
    "padus": SourceLicence(source="padus", licence="US public domain"),
    "tiger": SourceLicence(source="tiger", licence="US public domain"),
    "nws": SourceLicence(source="nws", licence="US public domain"),
    "noaa_coops": SourceLicence(source="noaa_coops", licence="US public domain"),
    "fcc_bdc": SourceLicence(source="fcc_bdc", licence="US public domain"),
    "canopy": SourceLicence(
        source="canopy",
        licence="CC-BY 4.0",
        attribution="Canopy height: Meta / World Resources Institute",
    ),
    "airnow": SourceLicence(
        source="airnow", licence="public, attribution requested", attribution="AirNow (EPA)"
    ),
    "purpleair": SourceLicence(source="purpleair", licence="PurpleAir API terms"),
    "gtfs": SourceLicence(source="gtfs", licence="per agency"),
}


def licence_for(source: str) -> SourceLicence | None:
    """Look up a source's licence, tolerating the `osm_ways`-style qualified names."""
    key = source.lower()
    if key in LICENCES:
        return LICENCES[key]
    return next((v for k, v in LICENCES.items() if key.startswith(k)), None)


def attributions_for(sources: list[str]) -> list[SourceLicence]:
    """Deduplicated licences for the sources a plan actually used."""
    seen: dict[str, SourceLicence] = {}
    for source in sources:
        licence = licence_for(source)
        if licence and licence.source not in seen:
            seen[licence.source] = licence
    return list(seen.values())


def unattributed(sources: list[str]) -> list[str]:
    """Sources with no licence entry.

    Surfaced rather than ignored: an unattributed source is a licence bug, and it should
    be visible on the sheet the moment someone adds a scorer without adding its source.
    """
    return sorted({s for s in sources if licence_for(s) is None})


def render(sources: list[str]) -> str:
    """The attribution block for a plan sheet (scope 14)."""
    lines = [f"- {lic.line()}" for lic in attributions_for(sources)]
    for source in unattributed(sources):
        lines.append(f"- {source} (LICENCE NOT RECORDED)")
    if any(lic.share_alike for lic in attributions_for(sources)):
        lines.append(
            "- Derived databases (offline LTS table, routing graph) are ODbL-encumbered "
            "if distributed."
        )
    return "\n".join(lines) if lines else "- No external sources used."
