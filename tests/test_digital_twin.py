"""Tests for the Stage 4 digital twin query layer, against real Ernakulam data.

These assume Steps 1-4 have been run with
--source-dir data/ernakulam/ernakulam_data, so PostGIS and Neo4j hold the
`ernakulam` edition. They are integration tests on purpose: the thing worth
testing about this layer is that it stitches two live stores together
correctly, which a mock would assume rather than prove.

The fixtures are chosen from the real district, not invented:

  G07027  Choornikkara   4 metro stations, 7 bus stops, 1 flood zone
  G07017  Malayattoor    no metro, no bus, no flood zone, GSI high landslide
  EKM-D   Ernakulam      the level the economic indicators are published at
"""

from __future__ import annotations

import pytest

from src.digital_twin.twin import (
    get_context,
    get_derived_features,
    get_economic_profile,
    get_flood_risk,
    get_transit_access,
)

PANCHAYAT = "G07027"       # Choornikkara: 4 metro stations, 7 bus stops, 1 flood zone
DRY_PANCHAYAT = "G07017"   # Malayattoor-Neeleswaram: no flood overlap
DISTRICT = "EKM-D"         # Ernakulam, where the economic indicators are published

VINTAGE_FIELDS = ("data_year", "revision_status", "boundary_vintage", "source", "match_confidence")

# Run against the demo or stress edition these would fail on differences that
# are correct, so the suite skips rather than reporting a false red.
_loaded = get_context(PANCHAYAT)
pytestmark = pytest.mark.skipif(
    _loaded.get("dataset_edition") != "ernakulam",
    reason=(
        f"stores hold the {_loaded.get('dataset_edition')!r} edition; these assert real "
        "Ernakulam facts. Reload with: bash scripts/run_phase1.sh real"
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
    assert context["boundary"]["name"]["value"] == "Choornikkara Gramapanchayath"
    assert context["population"]["population"]["value"] == 43207
    assert set(context["connected_infrastructure"]) == {
        "metro_stations", "bus_stops", "flood_zones", "parents", "children"
    }


def test_get_context_includes_the_metro_stations_inside_the_unit(context):
    """Four KMRL stations sit in Choornikkara, on the Aluva-bound stretch.

    The real station list tags every station with the district (`EKM-D`), so
    this only works because the Neo4j loader re-attributes each point to the
    local body that geometrically contains it.
    """
    stations = {s["station_id"] for s in context["connected_infrastructure"]["metro_stations"]}
    assert len(stations) == 4
    # name is vintage-tagged like every other value, so read through .value.
    names = {s["name"]["value"] for s in context["connected_infrastructure"]["metro_stations"]}
    assert "Companypady" in names


def test_get_context_includes_the_flood_zone_with_its_data_year(context):
    zones = context["flood_risk"]["flood_zones"]
    assert [z["flood_zone_id"] for z in zones] == ["EKM-FZ-002"]
    assert zones[0]["risk_level"]["data_year"] is not None


def test_get_context_hierarchy_resolves_upward(context):
    """Choornikkara sits in Aluva taluk. The real data has no ward layer, so a
    local body has no children — which must come back as an empty list, not as
    a missing key."""
    assert [p["id"] for p in context["connected_infrastructure"]["parents"]] == ["EKM-T-ALU"]
    assert context["connected_infrastructure"]["children"] == []


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
    result = get_context("G99999")
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
    assert access["metro_station_count"]["value"] == 4
    assert access["bus_stop_count"]["value"] == 7


def test_transit_access_matches_geometry_not_the_admin_id_column():
    """Regression: the real sources tag every point `EKM-D`.

    An attribute join therefore returned zero stations for all 97 local bodies
    while 25 real stations sat in the table. Containment is the reliable test.
    """
    assert get_transit_access("C07003")["metro_station_count"]["value"] == 13
    assert get_transit_access(DRY_PANCHAYAT)["metro_station_count"]["value"] == 0


def test_get_transit_access_flags_feed_staleness():
    """Some real stops carry an expired GTFS feed; the count must surface it."""
    access = get_transit_access(PANCHAYAT)
    stop = access["bus_stops"][0]
    assert is_tagged(stop["feed_is_stale"])
    assert isinstance(stop["feed_is_stale"]["value"], bool)
    assert access["stale_feed_count"]["value"] == 3


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
    assert gddp["years_covered"] == [2015, 2016, 2024]
    # Ecostat published 2015, 2016 and 2024; the years between are a real hole.
    assert 2017 in gddp["missing_years"]
    assert all(is_tagged(o) for o in gddp["observations"])


# --- get_derived_features --------------------------------------------------
# The layers the Phase-1 tables never carried: OSM water and right-of-way, the
# KMRL station distance, and the economic figures allocated from district
# totals.

def test_derived_features_split_measurements_from_estimates():
    derived = get_derived_features(PANCHAYAT)
    assert derived["found"] is True
    assert derived["measured"] and derived["estimated"]
    assert all(v["is_estimate"] is False for v in derived["measured"].values())
    assert all(v["is_estimate"] is True for v in derived["estimated"].values())
    assert "not measurements of this unit" in derived["caveat"]


def test_derived_measurements_match_the_feature_layer():
    """The twin must not round, rescale or reinterpret what the layer measured."""
    import pandas as pd

    frame = pd.read_csv("data/features/metro_features.csv")
    row = frame[frame.admin_id == PANCHAYAT].iloc[0]
    derived = get_derived_features(PANCHAYAT)
    assert derived["measured"]["nearest_metro_m"]["value"] == pytest.approx(
        row.nearest_metro_m)
    assert derived["measured"]["nearest_metro_station"]["value"] == row.nearest_metro_station


def test_derived_features_report_not_found_for_an_unknown_unit():
    assert get_derived_features("G99999")["found"] is False
