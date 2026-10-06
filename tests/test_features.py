"""Tests for the Review-2 real-data feature pipeline.

These assert the properties that make the hazard and transit features
trustworthy: the geography is right, the climate scenario behaves physically,
provenance survives into the database, and a stale feed stays visibly stale.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURES = PROJECT_ROOT / "data" / "features"
CENSUS_POPULATION = (PROJECT_ROOT / "data" / "ernakulam" / "ernakulam_data" /
                     "population" / "population_panchayat_ernakulam.csv")

pytestmark = pytest.mark.skipif(
    not (FEATURES / "hazard_features.csv").exists(),
    reason="real features not built; run python3 -m src.features.hazard and .transit")


@pytest.fixture(scope="module")
def hazard():
    return pd.read_csv(FEATURES / "hazard_features.csv")


@pytest.fixture(scope="module")
def transit():
    return pd.read_csv(FEATURES / "transit_features.csv")


# --- coverage --------------------------------------------------------------

def test_every_ernakulam_local_body_has_hazard_features(hazard):
    assert len(hazard) == 97
    assert hazard.admin_id.is_unique


def test_transit_covers_the_same_units(hazard, transit):
    assert set(transit.admin_id) == set(hazard.admin_id)


# --- physical coherence ----------------------------------------------------

def test_flood_share_is_a_proportion(hazard):
    for column in [c for c in hazard.columns if c.startswith("flood_share_")]:
        values = hazard[column].dropna()
        assert (values >= 0).all() and (values <= 1).all(), column


def test_longer_return_periods_are_not_less_flood_prone(hazard):
    """A 500-year event cannot inundate less area than a 10-year one."""
    bad = hazard[hazard.flood_share_historical_500yr + 1e-9
                 < hazard.flood_share_historical_10yr]
    assert bad.empty, f"{len(bad)} units violate return-period monotonicity"


def test_rcp85_is_at_least_as_severe_as_historical(hazard):
    """The climate scenario must not reduce hazard — that would be unphysical."""
    delta = hazard.flood_share_rcp85_50yr - hazard.flood_share_historical_50yr
    assert (delta >= -1e-9).all(), "RCP 8.5 shows less flooding than historical somewhere"
    assert delta.mean() > 0, "RCP 8.5 should increase exposure on average"


def test_flood_exposure_is_concentrated_in_the_river_delta(hazard):
    """Highest exposure belongs to the Periyar delta / backwater belt."""
    top = set(hazard.nlargest(6, "flood_share_historical_50yr").name.str.lower().str.split().str[0])
    delta_belt = {"chittattukara", "alangad", "kunnukara", "kottuvally",
                  "paravur", "vadakkekkara", "varapuzha", "eloor", "kadamakkudy"}
    assert top & delta_belt, f"expected delta-belt units in the top 6, got {top}"


def test_landslide_high_units_are_the_eastern_hills(hazard):
    high = set(hazard[hazard.landslide_susceptibility == "high"].name.str.lower().str.split().str[0])
    hills = {"kuttampuzha", "ayyampuzha", "kavalangad", "keerampaara", "pindimana",
             "edamalakkudi", "pothanikkad", "kuttamangalam", "neriamangalam"}
    assert len(high & hills) >= 3, f"expected hill panchayats to dominate, got {high}"


# --- transit ---------------------------------------------------------------

def test_nearest_stop_distance_is_non_negative(transit):
    assert (transit.nearest_stop_m >= 0).all()


def test_units_without_stops_have_a_positive_distance(transit):
    """Zero stops inside the unit still means a real distance to the nearest one."""
    none_inside = transit[transit.stop_count == 0]
    assert not none_inside.empty
    assert (none_inside.nearest_stop_m > 0).all()


def test_stop_density_agrees_with_count_and_area(transit):
    recomputed = (transit.stop_count / transit.area_km2).round(3)
    assert ((recomputed - transit.stop_density_per_km2).abs() < 0.01).all()


def test_feed_staleness_is_recorded_and_large(transit):
    """The real Kochi feed expired in March 2023; it must not look current."""
    assert transit.transit_feed_months_stale.notna().all()
    assert transit.transit_feed_months_stale.max() > 12


# --- provenance ------------------------------------------------------------

def test_hazard_declares_a_level_one_source(hazard):
    assert (hazard.hazard_source_level == 1).all()
    assert hazard.hazard_source.notna().all()


def test_transit_declares_a_level_three_source(transit):
    """Community GTFS must never be labelled as government data."""
    assert (transit.transit_source_level == 3).all()


# --- database round trip ---------------------------------------------------

def test_features_reach_postgis_with_provenance():
    psycopg2 = pytest.importorskip("psycopg2")
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.storage.postgres.load import connection_string
    try:
        with psycopg2.connect(connection_string()) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*), min(source_level), max(source_level) "
                        "FROM admin_hazard_feature")
            count, lo, hi = cur.fetchone()
            cur.execute("SELECT count(*) FROM transit_feature WHERE feed_months_stale > 12")
            stale = cur.fetchone()[0]
    except Exception as error:
        pytest.skip(f"PostGIS unavailable: {error}")
    assert count == 97
    assert lo == hi == 1, "hazard rows must all be level 1"
    assert stale == 97, "every transit row should carry the stale-feed flag"


# --- anchored economic estimates --------------------------------------------
# The guarantee that makes these estimates arguable rather than invented is that
# each one adds back up to a published district figure. That is cheap to assert
# and expensive to notice if it silently breaks, so it is asserted here.

def test_allocated_enterprise_counts_reproduce_the_district_total():
    import pandas as pd

    from src.features.anchors import MSME_REGISTRATIONS

    path = FEATURES / "econ_features.csv"
    if not path.exists():
        pytest.skip("econ_features.csv not built")
    frame = pd.read_csv(path)
    total = frame.msme_estimated_count.sum()
    # Rounding each unit to a whole enterprise is the only permitted drift.
    assert abs(total - MSME_REGISTRATIONS.value) <= len(frame)


def test_income_estimates_average_back_to_the_district_figure():
    import pandas as pd

    from src.features.anchors import PER_CAPITA_INCOME

    econ_path, census = FEATURES / "econ_features.csv", CENSUS_POPULATION
    if not econ_path.exists() or not census.exists():
        pytest.skip("econ_features.csv or the census table is not present")
    frame = pd.read_csv(econ_path).merge(
        pd.read_csv(census)[["admin_id", "population"]], on="admin_id", how="left")
    population = pd.to_numeric(frame.population, errors="coerce")
    population = population.fillna(population.median())
    weighted = ((frame.income_per_capita_estimate * population).sum()
                / population.sum())
    assert weighted == pytest.approx(PER_CAPITA_INCOME.value, rel=0.001)


def test_every_anchor_names_its_publisher_and_url():
    from src.features.anchors import table

    rows = table()
    assert rows
    for row in rows:
        assert row["publisher"], row["anchor"]
        assert row["url"].startswith("https://"), row["anchor"]
        assert row["source_level"] in (1, 2, 3, 4), row["anchor"]
        assert row["as_on"], row["anchor"]


def test_imputed_population_is_flagged_not_hidden():
    """An estimate resting on an imputed input must say so on the row."""
    import pandas as pd

    path = FEATURES / "econ_features.csv"
    if not path.exists():
        pytest.skip("econ_features.csv not built")
    frame = pd.read_csv(path)
    assert "econ_population_imputed" in frame.columns
    assert frame.econ_population_imputed.isin((0, 1)).all()
    # Where the flag is set there is still an estimate — the point is disclosure,
    # not omission.
    flagged = frame[frame.econ_population_imputed == 1]
    assert flagged.msme_estimated_count.notna().all()


def test_metro_and_water_layers_are_measurements_not_estimates():
    import pandas as pd

    for name, column, expect_level in (("metro_features.csv", "nearest_metro_m", 2),
                                       ("water_features.csv", "water_distance_m", 3)):
        path = FEATURES / name
        if not path.exists():
            pytest.skip(f"{name} not built")
        frame = pd.read_csv(path)
        assert frame[column].notna().all(), name
        assert (frame[column] >= 0).all(), name
        level_column = next(c for c in frame.columns if c.endswith("source_level"))
        assert set(frame[level_column].unique()) == {expect_level}, name


def test_income_estimates_stay_inside_the_stated_spread():
    """The ±30% bound is a documented claim, so it is enforced rather than hoped."""
    import pandas as pd

    from src.features.anchors import PER_CAPITA_INCOME
    from src.features.econ import INCOME_SPREAD

    path = FEATURES / "econ_features.csv"
    if not path.exists():
        pytest.skip("econ_features.csv not built")
    estimates = pd.read_csv(path).income_per_capita_estimate
    floor = PER_CAPITA_INCOME.value * (1 - INCOME_SPREAD)
    ceiling = PER_CAPITA_INCOME.value * (1 + INCOME_SPREAD)
    assert estimates.min() >= floor - 1
    assert estimates.max() <= ceiling + 1
