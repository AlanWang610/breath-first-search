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
    # Scope 14's table has no Open-Meteo row, although scope 7.4 names it as the
    # microclimate fallback and a cloud-cover source. The omission is the scope's, not a
    # statement that nothing is owed: Open-Meteo is CC-BY 4.0 and attribution is required.
    "open_meteo": SourceLicence(
        source="open_meteo",
        licence="CC-BY 4.0",
        attribution="Weather and air quality: Open-Meteo.com",
    ),
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
    # Not a data source: solar position and clear-sky irradiance are computed, not
    # fetched. It appears in coverage because a plan must be able to say the UTC
    # offset was guessed, and every coverage source needs a licence line.
    "pvlib": SourceLicence(source="pvlib", licence="BSD-3 (computation, not data)"),
}


def _layer_sources() -> dict[str, str]:
    """Layer name -> source, derived from the store's own table map rather than restated.

    A scorer records the *layer* it read — `ways`, `nodes`, `parks` — because that is what
    it asked the store for. Attribution is owed by *source*, and the mapping between them
    is already written down exactly once, as the schema half of `DEFAULT_LAYER_TABLES`:
    `ways` lives in `osm.ways`, `parks` in `padus.units`. Deriving it here means a layer
    added to the store cannot arrive unattributed because someone forgot a second table.

    It also means **the schema name has to be the source name**, which is a constraint on
    `DEFAULT_LAYER_TABLES` rather than an accident of it: `fcc.cell_coverage` would
    resolve to a source called `fcc` that no licence table knows, so the schema is
    `fcc_bdc`, matching the row scope 14 actually has.

    This became load-bearing the moment the OSM loader landed. Before it, no real plan had
    an OSM layer in its coverage, so every sheet's attribution block was accidentally
    complete; the first corridor with ways in it printed three `LICENCE NOT RECORDED`
    lines for data that is ODbL and share-alike.
    """
    from longrun.core.data.postgis import DEFAULT_LAYER_TABLES

    return {layer: table.partition(".")[0] for layer, table in DEFAULT_LAYER_TABLES.items()}


#: Coverage sources that are neither a layer nor a source: derivations that read one.
#: `way_matching` snaps route points onto OSM ways, so what it reports is OSM's.
DERIVED_SOURCES: dict[str, str] = {"way_matching": "osm"}


def licence_for(source: str) -> SourceLicence | None:
    """Look up a source's licence, resolving layer names and `osm_ways`-style prefixes."""
    key = source.lower()
    if key in LICENCES:
        return LICENCES[key]
    resolved = DERIVED_SOURCES.get(key) or _layer_sources().get(key)
    if resolved is not None and resolved in LICENCES:
        return LICENCES[resolved]
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
