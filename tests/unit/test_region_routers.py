"""One router per region: the registry, the generated configs, and region resolution.

The load-bearing test here is `test_the_committed_configs_are_what_the_generator_produces`.
`config-phoenix-lts.yml` was a hand-copy of the Bay Area's with two lines changed, and it
carried the Bay Area's extract command and bbox in its header for two milestones — wrong from
the moment it was written and invisible because nobody re-reads a config they did not change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longrun.core.models.geometry import LatLon
from longrun.regions.routers import (
    DEFAULT_URL,
    REGISTRY_PATH,
    RouterEntry,
    load_registry,
    region_for,
    render_config,
    router_url,
)

pytestmark = pytest.mark.skipif(
    not REGISTRY_PATH.exists(), reason="no deploy tree in this checkout"
)


@pytest.fixture(scope="module")
def registry() -> dict[str, RouterEntry]:
    return load_registry()


class TestRegistry:
    def test_every_built_region_has_an_entry(self, registry: dict[str, RouterEntry]) -> None:
        """A region with no entry cannot be served, and nothing else would say so."""
        specs = {p.stem for p in Path("deploy/regions").glob("*.yaml")} - {REGISTRY_PATH.stem}
        assert specs, "no region specs found"
        assert specs <= set(registry), f"no router entry for {specs - set(registry)}"

    def test_no_two_regions_share_a_port(self, registry: dict[str, RouterEntry]) -> None:
        """Including admin ports, which is why the gap between regions is two and not one.

        GraphHopper's admin connector is always `port + 1`. With consecutive ports the second
        region's HTTP port is the first region's admin port, and the collision surfaces at bind
        time as a message naming the wrong region.
        """
        used: dict[int, str] = {}
        for entry in registry.values():
            for port, role in ((entry.port, "http"), (entry.admin_port, "admin")):
                assert port not in used, (
                    f"{entry.region} {role} port {port} collides with {used[port]}"
                )
                used[port] = f"{entry.region} {role}"

    def test_the_bay_area_keeps_the_historical_default(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        """`DEFAULT_URL` and `.env.example` both say 8989, so moving it breaks every old setup."""
        assert registry["bayarea"].url == DEFAULT_URL

    def test_a_missing_registry_is_not_an_error(self, tmp_path: Path) -> None:
        """A checkout with no deploy tree still plans against LONGRUN_GRAPHHOPPER_URL."""
        assert load_registry(tmp_path / "absent.yaml") == {}


class TestGeneratedConfigs:
    def test_the_committed_configs_are_what_the_generator_produces(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        """`longrun region-config --check`, as a test. A hand-edit is reverted, not kept."""
        stale = []
        for name, entry in sorted(registry.items()):
            spec = Path(f"deploy/regions/{name}.yaml")
            if not spec.exists() or not entry.config_path.exists():
                continue
            current = entry.config_path.read_text(encoding="utf-8")
            if current != render_config(entry, spec):
                stale.append(entry.config_path.as_posix())
        assert not stale, f"run `uv run longrun region-config`: {', '.join(stale)} differ"

    def test_each_config_names_its_own_region_everywhere(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        """The copy-paste failure, caught directly: Phoenix's header described the Bay Area."""
        for name, entry in sorted(registry.items()):
            if not entry.config_path.exists():
                continue
            text = entry.config_path.read_text(encoding="utf-8")
            others = [r for r in registry if r != name]
            assert not [r for r in others if r in text], (
                f"{entry.config_path} mentions another region"
            )

    def test_a_config_carries_the_lts_encoded_value(self, registry: dict[str, RouterEntry]) -> None:
        """Without it the import fails with `Unknown encoded value: lts` (ADR 0001)."""
        rendered = render_config(registry["ozarks"])
        assert "\n    lts\n" in rendered

    def test_ch_stays_off_so_the_custom_model_is_a_query_parameter(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        """Scope 4.3. A contraction hierarchy bakes the custom model into the preparation."""
        rendered = render_config(registry["ozarks"])
        assert "profiles_ch: []" in rendered
        assert "profiles_lm:" in rendered

    def test_the_ports_come_from_the_registry(self, registry: dict[str, RouterEntry]) -> None:
        rendered = render_config(registry["boston"])
        assert f"port: {registry['boston'].port}" in rendered
        assert f"port: {registry['boston'].admin_port}" in rendered


class TestRouterUrl:
    """Most specific answer first, and the last two rungs keep every old setup working."""

    def test_an_explicit_url_wins(self, registry: dict[str, RouterEntry]) -> None:
        assert (
            router_url("ozarks", explicit="http://elsewhere:1/", env={}, registry=registry)
            == "http://elsewhere:1"
        )

    def test_a_per_region_variable_beats_the_registry(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        env = {"LONGRUN_GRAPHHOPPER_URL_OZARKS": "http://box:9000"}
        assert router_url("ozarks", env=env, registry=registry) == "http://box:9000"

    def test_the_registry_answers_for_a_known_region(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        assert router_url("ozarks", env={}, registry=registry).endswith(":8997")

    def test_the_single_region_variable_still_works(self, registry: dict[str, RouterEntry]) -> None:
        """The setup that predates this file: one region, one env var, no region named."""
        env = {"LONGRUN_GRAPHHOPPER_URL": "http://legacy:8989"}
        assert router_url(None, env=env, registry=registry) == "http://legacy:8989"

    def test_an_unknown_region_falls_back_rather_than_raising(
        self, registry: dict[str, RouterEntry]
    ) -> None:
        env = {"LONGRUN_GRAPHHOPPER_URL": "http://legacy:8989"}
        assert router_url("atlantis", env=env, registry=registry) == "http://legacy:8989"

    def test_with_nothing_set_it_is_the_default(self, registry: dict[str, RouterEntry]) -> None:
        assert router_url(None, env={}, registry=registry) == DEFAULT_URL


class _Box:
    """A region spec's worth of shape(), over a rectangle whose answer is known by eye."""

    def __init__(self, west: float, south: float, east: float, north: float) -> None:
        self._bounds = (west, south, east, north)

    def shape(self) -> object:
        from shapely.geometry import box

        return box(*self._bounds)


class TestRegionResolution:
    """A route drawn on the wrong region's graph is plausible and wrong, so it refuses."""

    SPECS = {
        "west": _Box(-123.0, 37.0, -122.0, 38.0),
        "east": _Box(-92.0, 37.0, -91.0, 38.0),
        "overlapping": _Box(-123.5, 36.5, -121.5, 38.5),
    }

    def test_a_route_inside_one_region_resolves(self) -> None:
        specs = {k: v for k, v in self.SPECS.items() if k != "overlapping"}
        match = region_for([LatLon(lat=37.5, lon=-122.5), LatLon(lat=37.6, lon=-122.4)], specs)
        assert match.region == "west"

    def test_a_route_straddling_two_regions_refuses_and_names_them(self) -> None:
        """Picking the start's region would give a NoRouteError naming the wrong cause."""
        specs = {k: v for k, v in self.SPECS.items() if k != "overlapping"}
        match = region_for([LatLon(lat=37.5, lon=-122.5), LatLon(lat=37.5, lon=-91.5)], specs)
        assert match.region is None
        assert match.candidates == ["east", "west"]
        assert "straddle" in match.reason

    def test_a_route_in_no_region_says_so(self) -> None:
        specs = {k: v for k, v in self.SPECS.items() if k != "overlapping"}
        match = region_for([LatLon(lat=10.0, lon=10.0)], specs)
        assert match.region is None
        assert match.candidates == []
        assert "no built region" in match.reason

    def test_two_regions_containing_the_route_is_ambiguous_not_arbitrary(self) -> None:
        match = region_for([LatLon(lat=37.5, lon=-122.5)], self.SPECS)
        assert match.region is None
        assert match.ambiguous
        assert set(match.candidates) == {"west", "overlapping"}
        assert "--region" in match.reason

    def test_no_waypoints_is_answered_rather_than_crashing(self) -> None:
        assert region_for([], self.SPECS).region is None
