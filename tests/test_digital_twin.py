"""Tests for the Stage 4 digital twin query layer, against the loaded demo dataset.

These assume Steps 1-4 have been run with --source-dir data/demo/demo_data, so
PostGIS and Neo4j hold the demo edition. They are integration tests on purpose:
the thing worth testing about this layer is that it stitches two live stores
together correctly, which a mock would assume rather than prove.
"""

from __future__ import annotations

import pytest

from src.digital_twin.twin import (
    get_context,
    get_economic_profile,
    get_flood_risk,
    get_transit_access,
)

PANCHAYAT = "DEMO-P-02"       # two metro stations, one bus stop, in the flood zone
DRY_PANCHAYAT = "DEMO-P-01"   # no flood overlap
DISTRICT = "DEMO-D-01"        # where the economic indicators are published

VINTAGE_FIELDS = ("data_year", "revision_status", "boundary_vintage", "source", "match_confidence")

# These assert demo-edition facts (three wards under P-02, a current GTFS feed,
# no withheld records). Run against the stress-test edition they would fail on
# differences that are correct — W-02b is withheld by a hard check there, and its
# bus feed really is stale — so the suite skips rather than reporting a false red.
_loaded = get_context("DEMO-P-02")
pytestmark = pytest.mark.skipif(
    _loaded.get("dataset_edition") != "demo",
    reason=(
        f"stores hold the {_loaded.get('dataset_edition')!r} edition; these tests assert "
        "demo-dataset facts. Reload with: python src/ingestion/ingest.py --reset "
        "--source-dir data/demo/demo_data && python -m src.processing.run && ..."
    ),
)


def is_tagged(value) -> bool:
    """A tagged value carries its vintage keys, even when the source left them null."""
    return isinstance(value, dict) and "value" in value and all(f in value for f in VINTAGE_FIELDS)


@pytest.fixture(scope="module")
def context():
    return get_context(PANCHAYAT)


# --- get_context -----------------------------------------------------------

def test_get_context_returns_boundary_population_and_infrastructure(context):
    assert context["found"] is True
    assert context["admin_id"] == PANCHAYAT
    assert context["boundary"]["name"]["value"] == "Demo Panchayat B"
    assert context["population"]["population"]["value"] == 38500
    assert set(context["connected_infrastructure"]) == {
        "metro_stations", "bus_stops", "flood_zones", "parents", "children"
    }


def test_get_context_includes_both_metro_stations(context):
    stations = {s["station_id"] for s in context["connected_infrastructure"]["metro_stations"]}
    assert stations == {"DEMO-MS-01", "DEMO-MS-02"}


def test_get_context_includes_the_flood_zone_with_its_data_year(context):
    zones = context["flood_risk"]["flood_zones"]
    assert [z["flood_zone_id"] for z in zones] == ["DEMO-FZ-01"]
    assert zones[0]["risk_level"]["data_year"] is not None


def test_get_context_hierarchy_resolves_upward(context):
    assert [p["id"] for p in context["connected_infrastructure"]["parents"]] == ["DEMO-T-01"]
    children = {c["id"] for c in context["connected_infrastructure"]["children"]}
    assert children == {"DEMO-W-02a", "DEMO-W-02b"}


def test_every_context_value_is_vintage_tagged(context):
    """The core contract: no bare numbers anywhere an agent can read one."""
    for field in ("name", "level", "area_m2", "centroid"):
        assert is_tagged(context["boundary"][field]), field
    for field in ("population", "male_population", "female_population", "literacy_rate", "households"):
        assert is_tagged(context["population"][field]), field


def test_population_carries_a_different_vintage_than_the_boundary(context):
    """The delimitation mismatch must stay visible, not be flattened away."""
    assert context["boundary"]["name"]["boundary_vintage"] == "2025-delimitation"
    assert context["population"]["population"]["boundary_vintage"] == "pre-2025-delimitation"


def test_unknown_admin_id_reports_not_found_instead_of_raising():
    result = get_context("DEMO-P-99")
    assert result["found"] is False
    assert "hard_check_failures" in result["note"]


# --- get_flood_risk --------------------------------------------------------

def test_get_flood_risk_reports_overlap_ratio_and_caveat():
    risk = get_flood_risk(PANCHAYAT)
    assert risk["zone_count"] == 1
    assert 0 < risk["max_overlap_ratio"]["value"] <= 1
    assert is_tagged(risk["flood_zones"][0]["overlap_area_m2"])
    assert "not a real-time" in risk["caveat"]


def test_get_flood_risk_is_empty_but_valid_where_nothing_overlaps():
    risk = get_flood_risk(DRY_PANCHAYAT)
    assert risk["found"] is True
    assert risk["zone_count"] == 0
    assert risk["max_overlap_ratio"]["value"] == 0.0


# --- get_transit_access ----------------------------------------------------

def test_get_transit_access_counts_metro_and_bus():
    access = get_transit_access(PANCHAYAT)
    assert access["metro_station_count"]["value"] == 2
    assert access["bus_stop_count"]["value"] == 1
    assert {s["station_id"] for s in access["metro_stations"]} == {"DEMO-MS-01", "DEMO-MS-02"}


def test_get_transit_access_flags_feed_staleness():
    access = get_transit_access(PANCHAYAT)
    stop = access["bus_stops"][0]
    assert is_tagged(stop["feed_is_stale"])
    # The demo feed runs through 2026, so it is current.
    assert stop["feed_is_stale"]["value"] is False
    assert access["stale_feed_count"]["value"] == 0


# --- get_economic_profile --------------------------------------------------

def test_get_economic_profile_at_the_publishing_level():
    profile = get_economic_profile(DISTRICT)
    assert profile["inherited_from"] is None
    assert "gddp" in profile["indicators"]
    assert profile["indicators"]["gddp"]["latest_final"]["value"] is not None


def test_get_economic_profile_inherits_upward_and_downgrades_confidence():
    """A district figure is not a panchayat figure, and must not be presented as one."""
    profile = get_economic_profile(PANCHAYAT)
    assert profile["inherited_from"]["admin_id"] == DISTRICT
    for observation in profile["indicators"]["gddp"]["observations"]:
        assert observation["match_confidence"] == "unmatched-estimate"


def test_get_economic_profile_keeps_series_gaps_visible():
    """Manifest #15: a genuine hole in a published series is information, not noise."""
    profile = get_economic_profile(DISTRICT)
    gddp = profile["indicators"]["gddp"]
    assert gddp["years_covered"] == [2011, 2015, 2019, 2021, 2023]
    assert 2012 in gddp["missing_years"]
    assert all(is_tagged(o) for o in gddp["observations"])
