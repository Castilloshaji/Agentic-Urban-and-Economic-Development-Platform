"""Parameter engine: measurable features -> normalized, confidence-tagged parameters.

Every parameter carries where it came from, how old it is, how much to trust it,
and — crucially — whether it is real, a proxy, or unavailable. A parameter with
no data path returns status "unavailable" and a null value. It never returns 0,
because 0 is a measurement and "we do not know" is not.

Normalization is percentile rank across all 97 Ernakulam local bodies, which is
scale-free and resistant to the long tails these distributions have (one unit is
52 km from a bus stop; min-max would flatten everything else against it).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FEATURES = PROJECT_ROOT / "data" / "features"

REAL, PROXY, UNAVAILABLE = "real", "proxy", "unavailable"

# Confidence by source level, per the Review-2 source hierarchy.
LEVEL_CONFIDENCE = {1: 1.00, 2: 0.85, 3: 0.70, 4: 0.50}

# A feed this stale cannot be treated as describing current service.
STALE_FEED_MONTHS = 12
STALE_PENALTY = 0.6

# An estimate allocated down from a published district total is not as good as a
# measurement of the unit, however good the anchor is. The anchor's own level
# sets the ceiling and this cuts it: a Level-1 district publication shared out by
# a stated allocator lands at 0.55 — below a current Level-3 measurement (0.70)
# and above a Level-4 secondary source (0.50).
#
# One ordering is deliberately *not* guaranteed: a measurement carrying the
# stale-feed penalty drops to 0.42, beneath these estimates. That is intended. A
# 2022 transit feed describing service in 2026 is a worse guide to today than a
# current district total shared out by a stated rule, and the confidence numbers
# should say so.
ALLOCATION_PENALTY = 0.55


@dataclass
class Parameter:
    name: str
    domain: str
    value: float | None
    normalized: float | None
    confidence: float
    status: str
    source: str | None
    source_level: int | None
    data_year: int | None
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _pct_rank(series: pd.Series, invert: bool = False) -> pd.Series:
    """Percentile rank in [0,1]; invert when a LOW raw value means HIGH score."""
    ranked = series.rank(pct=True, na_option="keep")
    return (1.0 - ranked) if invert else ranked


def load_features() -> pd.DataFrame:
    hazard = pd.read_csv(FEATURES / "hazard_features.csv")
    transit = pd.read_csv(FEATURES / "transit_features.csv")
    frame = hazard.merge(transit, on="admin_id", how="outer")

    # Road features are optional: they need the 532 MB OSM extract, so the engine
    # must work without them and simply report those parameters unavailable.
    roads_path = FEATURES / "road_features.csv"
    if roads_path.exists():
        roads = pd.read_csv(roads_path)[
            ["admin_id", "road_segments", "road_length_m",
             "road_density_km_per_km2", "max_road_class",
             "road_source", "road_source_level", "road_data_year"]]
        frame = frame.merge(roads, on="admin_id", how="left")

    # Layers added after the first pass closed six "unavailable" parameters.
    # Each stays optional for the same reason roads do: the engine must run on a
    # machine where the 532 MB OSM extract was never downloaded, and simply
    # report those parameters unavailable instead of failing.
    for name, columns in (
        ("metro_features.csv", ["nearest_metro_m", "nearest_metro_station",
                                "metro_stations_in_unit", "metro_source",
                                "metro_source_level", "metro_data_year"]),
        ("water_features.csv", ["water_area_share", "water_distance_m",
                                "water_source", "water_source_level",
                                "water_data_year"]),
        ("row_features.csv", ["row_narrow_share", "row_arterial_km_per_km2",
                              "row_lane_tag_coverage", "row_source",
                              "row_source_level", "row_data_year"]),
        ("social_features.csv", ["health_count", "health_capacity", "health_nearest_m",
                                 "health_per_km2", "health_per_1000",
                                 "education_count", "education_capacity",
                                 "education_nearest_m", "education_per_km2",
                                 "education_per_1000", "social_source",
                                 "social_source_level", "social_data_year"]),
        ("econ_features.csv", ["msme_estimated_count", "msme_per_1000_people",
                               "msme_density_per_km2", "income_per_capita_estimate",
                               "gddp_share_estimate_crore",
                               "employment_capacity_index", "econ_source",
                               "econ_source_level", "econ_allocation",
                               "econ_data_year", "econ_population_imputed"]),
    ):
        path = FEATURES / name
        if path.exists():
            frame = frame.merge(pd.read_csv(path)[["admin_id", *columns]],
                                on="admin_id", how="left")

    census = PROJECT_ROOT / "data" / "ernakulam" / "ernakulam_data" / "population" / "population_panchayat_ernakulam.csv"
    if census.exists():
        pop = pd.read_csv(census)[["admin_id", "population", "literacy_rate"]]
        frame = frame.merge(pop, on="admin_id", how="left")
    else:
        frame["population"] = None
        frame["literacy_rate"] = None
    return frame


def _derive(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    f["population_density"] = pd.to_numeric(f.population, errors="coerce") / f.area_km2
    f["flood_hist_50"] = pd.to_numeric(f.get("flood_share_historical_50yr"), errors="coerce")
    f["flood_rcp_50"] = pd.to_numeric(f.get("flood_share_rcp85_50yr"), errors="coerce")
    f["climate_delta"] = f.flood_rcp_50 - f.flood_hist_50
    f["landslide_rank"] = pd.to_numeric(f.get("landslide_rank"), errors="coerce").fillna(0)

    # Normalized columns. invert=True where a low raw number is a good score.
    f["n_flood"] = _pct_rank(f.flood_hist_50)
    f["n_climate"] = _pct_rank(f.climate_delta)
    f["n_landslide"] = _pct_rank(f.landslide_rank)
    f["n_access_deficit"] = _pct_rank(f.nearest_stop_m)            # far = high deficit
    f["n_transit_avail"] = _pct_rank(f.stop_density_per_km2)
    f["n_service_freq"] = _pct_rank(f.trips_per_stop)
    f["n_pop_affected"] = _pct_rank(pd.to_numeric(f.population, errors="coerce"))
    f["n_density"] = _pct_rank(f.population_density)
    f["n_literacy"] = _pct_rank(pd.to_numeric(f.literacy_rate, errors="coerce"))
    f["n_area"] = _pct_rank(f.area_km2)
    if "road_density_km_per_km2" in f.columns:
        f["n_road_density"] = _pct_rank(pd.to_numeric(f.road_density_km_per_km2, errors="coerce"))
        f["n_road_class"] = _pct_rank(pd.to_numeric(f.max_road_class, errors="coerce"))
    if "nearest_metro_m" in f.columns:
        # Close to a station is good access, so invert.
        f["n_metro_access"] = _pct_rank(
            pd.to_numeric(f.nearest_metro_m, errors="coerce"), invert=True)
    if "water_area_share" in f.columns:
        # Near water is high environmental sensitivity, so invert the distance.
        f["n_water_proximity"] = _pct_rank(
            pd.to_numeric(f.water_distance_m, errors="coerce"), invert=True)
    if "row_narrow_share" in f.columns:
        f["n_row_constraint"] = _pct_rank(
            pd.to_numeric(f.row_narrow_share, errors="coerce"))
    # ---- the fiscal entitlement, straight from the published formula ----
    # Kerala's State Finance Commission distributes the basic grant on 80%
    # population, 10% area, 10% inverse of own income. Own income is not
    # published per local body, so estimated per-capita income stands in for it
    # and the parameter says so. Implementing the real rule is what makes a
    # budget share arguable instead of invented.
    population = pd.to_numeric(f.population, errors="coerce")
    own_income = pd.to_numeric(f.get("income_per_capita_estimate"), errors="coerce")
    if own_income is not None and own_income.notna().any():
        share = lambda col: col / col.sum() if col.sum() else col * 0
        inverse_income = (1.0 / own_income).replace([float("inf")], pd.NA)
        f["fiscal_share"] = (
            0.80 * share(population.fillna(population.median()))
            + 0.10 * share(f.area_km2)
            + 0.10 * share(inverse_income.fillna(inverse_income.median()))
        )
        f["n_fiscal"] = _pct_rank(f.fiscal_share)
        f["n_own_income"] = _pct_rank(own_income)
        # What a project here plausibly costs to deliver, relative to the rest
        # of the district: dense, hazard-exposed and poorly connected ground is
        # dearer to build on. High value means dear.
        f["cost_index"] = (
            _pct_rank(f.n_density if "n_density" in f else f.area_km2) * 0.4
            + f.n_flood.fillna(0.5) * 0.35
            + _pct_rank(pd.to_numeric(f.get("row_narrow_share"), errors="coerce")).fillna(0.5) * 0.25)
        f["n_cost"] = _pct_rank(f.cost_index)

    if "health_count" in f.columns:
        # Distance is the robust signal. Counts depend on how well a place is
        # mapped; a centroid's distance to the nearest mapped facility does not
        # move nearly as much with mapping effort.
        f["n_health_access"] = _pct_rank(
            pd.to_numeric(f.health_nearest_m, errors="coerce"), invert=True)
        f["n_health_capacity"] = _pct_rank(
            pd.to_numeric(f.health_per_km2, errors="coerce"))
        f["n_health_per_1000"] = _pct_rank(
            pd.to_numeric(f.get("health_per_1000"), errors="coerce"))
        f["n_edu_access"] = _pct_rank(
            pd.to_numeric(f.education_nearest_m, errors="coerce"), invert=True)
        f["n_edu_capacity"] = _pct_rank(
            pd.to_numeric(f.education_per_km2, errors="coerce"))
        f["n_edu_per_1000"] = _pct_rank(
            pd.to_numeric(f.get("education_per_1000"), errors="coerce"))

    if "msme_estimated_count" in f.columns:
        f["n_msme"] = _pct_rank(pd.to_numeric(f.msme_estimated_count, errors="coerce"))
        f["n_investment"] = _pct_rank(
            pd.to_numeric(f.income_per_capita_estimate, errors="coerce"))
        f["n_employment"] = _pct_rank(
            pd.to_numeric(f.employment_capacity_index, errors="coerce"))
    return f


def parameters_for(admin_id: str, frame: pd.DataFrame | None = None) -> list[Parameter]:
    """Every parameter for one local body, with provenance and status."""
    frame = _derive(load_features() if frame is None else frame)
    match = frame[frame.admin_id == admin_id]
    if match.empty:
        raise KeyError(f"unknown admin_id {admin_id!r}")
    r = match.iloc[0]

    has_roads = pd.notna(r.get("road_density_km_per_km2"))
    has_metro = pd.notna(r.get("nearest_metro_m"))
    has_water = pd.notna(r.get("water_distance_m"))
    has_row = pd.notna(r.get("row_narrow_share"))
    has_econ = pd.notna(r.get("msme_estimated_count"))
    has_social = pd.notna(r.get("health_nearest_m"))
    has_fiscal = pd.notna(r.get("fiscal_share"))
    social_conf = LEVEL_CONFIDENCE[3] if has_social else 0.0
    # The fiscal share implements a published Level-1 formula, but one of its
    # three terms (own income) is substituted, so it cannot claim a clean
    # Level-1 confidence.
    fiscal_conf = LEVEL_CONFIDENCE[1] * 0.75 if has_fiscal else 0.0
    # The anchors are Level-1 district publications; the allocation step is what
    # costs the confidence, not the source.
    econ_conf = LEVEL_CONFIDENCE[1] * ALLOCATION_PENALTY if has_econ else 0.0
    stale = r.get("transit_feed_months_stale")
    transit_conf = LEVEL_CONFIDENCE[3] * (
        STALE_PENALTY if pd.notna(stale) and stale > STALE_FEED_MONTHS else 1.0)
    has_population = pd.notna(r.get("population"))
    has_literacy = pd.notna(r.get("literacy_rate"))
    census_conf = LEVEL_CONFIDENCE[1] if has_population else 0.0
    # 26 of the 97 units carry no Census 2011 count, because their boundaries
    # changed after the census. Saying so is part of the contract: a parameter
    # that reports "unavailable" owes the reader a reason, and these four used
    # to return an empty note.
    NO_CENSUS = ("no Census 2011 count for this unit — its boundary post-dates "
                 "the census, so the figure was never published for it")

    def p(name, domain, value, normalized, confidence, status, source, level, year, note=""):
        return Parameter(name, domain,
                         None if pd.isna(value) else float(value),
                         None if pd.isna(normalized) else round(float(normalized), 4),
                         round(confidence, 3), status, source, level, year, note)

    out: list[Parameter] = [
        # ---- ENVIRONMENT (real: KSDMA + GSI) ----
        p("flood_risk", "environment", r.flood_hist_50, r.n_flood, LEVEL_CONFIDENCE[1], REAL,
          "KSDMA flood return probability 50yr historical", 1, 2026,
          "share of unit area flood-modelled"),
        p("flood_zone_overlap", "environment", r.flood_hist_50, r.n_flood, LEVEL_CONFIDENCE[1], REAL,
          "KSDMA raster zonal statistics", 1, 2026),
        p("climate_vulnerability", "environment", r.climate_delta, r.n_climate,
          LEVEL_CONFIDENCE[1], REAL, "KSDMA RCP8.5 minus historical, 50yr", 1, 2026,
          "increase in flood-modelled share under RCP 8.5"),
        p("hazard_exposure", "environment", r.landslide_rank, r.n_landslide,
          LEVEL_CONFIDENCE[1], REAL, "GSI landslide susceptibility", 1, 2025),
        p("environmental_sensitivity", "environment",
          max(r.n_flood or 0, r.n_landslide or 0), max(r.n_flood or 0, r.n_landslide or 0),
          LEVEL_CONFIDENCE[1], PROXY, "max(flood, landslide) normalized", 1, 2026,
          "composite proxy; no protected-area layer acquired"),
        p("water_body_proximity", "environment",
          r.get("water_distance_m"), r.get("n_water_proximity"),
          LEVEL_CONFIDENCE[3] if has_water else 0.0,
          REAL if has_water else UNAVAILABLE,
          r.get("water_source") if has_water else None,
          r.get("water_source_level") if has_water else None,
          r.get("water_data_year") if has_water else None,
          (f"metres from centroid to nearest mapped water body; "
           f"{float(r.get('water_area_share') or 0):.1%} of the unit is water"
           ) if has_water else "needs OSM water layer (Geofabrik clip pending)"),

        # ---- TRANSPORTATION (real: Kochi GTFS) ----
        p("accessibility_deficit", "transportation", r.nearest_stop_m, r.n_access_deficit,
          transit_conf, REAL, "Kochi GTFS nearest-stop distance from centroid", 3, 2022,
          f"feed {int(stale)} months stale" if pd.notna(stale) else ""),
        p("transit_availability", "transportation", r.stop_density_per_km2, r.n_transit_avail,
          transit_conf, REAL, "Kochi GTFS stop density", 3, 2022),
        p("distance_to_transit", "transportation", r.nearest_stop_m,
          _pct_rank(frame.nearest_stop_m, invert=True).loc[match.index[0]],
          transit_conf, REAL, "Kochi GTFS", 3, 2022),
        p("service_frequency", "transportation", r.trips_per_stop, r.n_service_freq,
          transit_conf, REAL, "Kochi GTFS stop_times trip counts", 3, 2022),
        p("population_affected", "transportation", r.population, r.n_pop_affected,
          census_conf, REAL if has_population else UNAVAILABLE,
          "Census 2011 via censusindia2011", 1, 2011,
          "" if has_population else NO_CENSUS),
        p("congestion_pressure", "transportation", r.population_density, r.n_density,
          census_conf * 0.7, PROXY if has_population else UNAVAILABLE,
          "Census 2011 population density" if has_population else None,
          1 if has_population else None, 2011 if has_population else None,
          "no traffic-count data published for Ernakulam" if has_population
          else NO_CENSUS),
        p("metro_access", "transportation",
          r.get("nearest_metro_m"), r.get("n_metro_access"),
          LEVEL_CONFIDENCE[2] if has_metro else 0.0,
          REAL if has_metro else UNAVAILABLE,
          r.get("metro_source") if has_metro else None,
          r.get("metro_source_level") if has_metro else None,
          r.get("metro_data_year") if has_metro else None,
          (f"{float(r.get('nearest_metro_m')) / 1000:.1f} km to "
           f"{r.get('nearest_metro_station')}; "
           f"{int(r.get('metro_stations_in_unit') or 0)} station(s) inside the unit"
           ) if has_metro else "metro station layer held separately; join pending"),

        # ---- INFRASTRUCTURE ----
        p("population_served", "infrastructure", r.population, r.n_pop_affected,
          census_conf, REAL if has_population else UNAVAILABLE,
          "Census 2011", 1, 2011,
          "" if has_population else NO_CENSUS),
        p("settlement_density", "infrastructure", r.population_density, r.n_density,
          census_conf, REAL if has_population else UNAVAILABLE,
          "Census 2011 / area", 1, 2011,
          "" if has_population else NO_CENSUS),
        p("implementation_area", "infrastructure", r.area_km2, r.n_area,
          LEVEL_CONFIDENCE[3], REAL, "OpenDataKerala LSG boundary area", 3, 2024),
        p("road_connectivity", "infrastructure",
          r.get("road_density_km_per_km2"), r.get("n_road_density"),
          LEVEL_CONFIDENCE[3] if has_roads else 0.0,
          REAL if has_roads else UNAVAILABLE,
          r.get("road_source") if has_roads else None,
          r.get("road_source_level") if has_roads else None,
          r.get("road_data_year") if has_roads else None,
          "road length per km² from the OSM clip" if has_roads
          else "needs OSM road clip (Geofabrik Southern Zone, 532 MB)"),
        p("infrastructure_capacity", "infrastructure",
          r.get("max_road_class"), r.get("n_road_class"),
          LEVEL_CONFIDENCE[3] * 0.7 if has_roads else 0.0,
          PROXY if has_roads else UNAVAILABLE,
          r.get("road_source") if has_roads else None,
          r.get("road_source_level") if has_roads else None,
          r.get("road_data_year") if has_roads else None,
          "highest OSM highway class as a capacity proxy — no measured capacity "
          "dataset exists for Ernakulam" if has_roads else "no capacity dataset published"),
        p("right_of_way_constraint", "infrastructure",
          r.get("row_narrow_share"), r.get("n_row_constraint"),
          LEVEL_CONFIDENCE[3] * 0.8 if has_row else 0.0,
          PROXY if has_row else UNAVAILABLE,
          r.get("row_source") if has_row else None,
          r.get("row_source_level") if has_row else None,
          r.get("row_data_year") if has_row else None,
          (f"share of network that is narrow-class road; OSM lanes tagged on "
           f"only {float(r.get('row_lane_tag_coverage') or 0):.1%} of segments "
           f"here, which is why class stands in for width"
           ) if has_row else "no road network mapped for this unit"),

        # ---- ECONOMIC ----
        p("literacy_capacity", "economic", r.literacy_rate, r.n_literacy,
          LEVEL_CONFIDENCE[1] if has_literacy else 0.0,
          REAL if has_literacy else UNAVAILABLE,
          "Census 2011 literacy", 1, 2011,
          "" if has_literacy else NO_CENSUS),
        p("economic_catchment", "economic", r.population, r.n_pop_affected,
          census_conf * 0.8, PROXY if has_population else UNAVAILABLE,
          "Census 2011 population" if has_population else None,
          1 if has_population else None, 2011 if has_population else None,
          "district GDDP cannot be disaggregated to local-body level"
          if has_population else NO_CENSUS),
        p("urbanisation", "economic",
          1.0 if r.get("local_auth") in ("municipality", "municipal_corporation") else 0.0,
          1.0 if r.get("local_auth") in ("municipality", "municipal_corporation") else 0.0,
          LEVEL_CONFIDENCE[3], PROXY, "OpenDataKerala LSG classification", 3, 2024),
        p("msme_presence", "economic",
          r.get("msme_estimated_count"), r.get("n_msme"), econ_conf,
          PROXY if has_econ else UNAVAILABLE,
          r.get("econ_source") if has_econ else None,
          r.get("econ_source_level") if has_econ else None,
          r.get("econ_data_year") if has_econ else None,
          (f"~{int(r.get('msme_estimated_count')):,} enterprises"
           # Only quote a per-head rate where there is a real head count to
           # divide by; 26 units have no Census population and printing "nan
           # per 1,000 people" would be worse than printing nothing.
           + (f" ({float(r.get('msme_per_1000_people')):.0f} per 1,000 people)"
              if pd.notna(r.get("msme_per_1000_people")) else "")
           + f", estimated by sharing out the district's 166,200 Udyam "
             f"registrations (MSME Ministry, as on 2024-12-15); allocator: "
             f"{r.get('econ_allocation')}"
           + (" — this unit's population was imputed from the district median, "
              "so its share is weaker than the others"
              if r.get("econ_population_imputed") == 1 else "")
           ) if has_econ else
          "Kerala OGD Udyam catalog returns no resources; national API needs a key"),
        p("investment_potential", "economic",
          r.get("income_per_capita_estimate"), r.get("n_investment"), econ_conf,
          PROXY if has_econ else UNAVAILABLE,
          r.get("econ_source") if has_econ else None,
          r.get("econ_source_level") if has_econ else None,
          r.get("econ_data_year") if has_econ else None,
          (f"~INR {int(r.get('income_per_capita_estimate')):,} per capita, "
           f"modulated from the district figure of INR 261,319 (Ecostat 2024) "
           f"by measured activity; the population-weighted mean of all 97 "
           f"estimates returns the district figure exactly"
           ) if has_econ else "not published below district level"),
        # ---- HEALTHCARE (real: OSM facility locations) ----
        p("health_access", "healthcare",
          r.get("health_nearest_m"), r.get("n_health_access"), social_conf,
          REAL if has_social else UNAVAILABLE,
          r.get("social_source") if has_social else None,
          r.get("social_source_level") if has_social else None,
          r.get("social_data_year") if has_social else None,
          (f"{float(r.get('health_nearest_m')) / 1000:.1f} km from the centre to "
           f"the nearest mapped health facility"
           ) if has_social else "needs the OSM extract (Geofabrik clip pending)"),
        p("health_capacity", "healthcare",
          r.get("health_per_km2"), r.get("n_health_capacity"), social_conf,
          PROXY if has_social else UNAVAILABLE,
          r.get("social_source") if has_social else None,
          r.get("social_source_level") if has_social else None,
          r.get("social_data_year") if has_social else None,
          (f"{int(r.get('health_count') or 0)} mapped facilities, capacity-weighted "
           f"to {float(r.get('health_capacity') or 0):.1f} per km2. OSM mixes "
           f"government and private and its coverage is uneven, so this is a "
           f"proxy for service capacity, not a facility census"
           ) if has_social else "needs the OSM extract"),
        p("health_deficit", "healthcare",
          r.get("health_per_1000"), r.get("n_health_per_1000"),
          social_conf * 0.8 if pd.notna(r.get("health_per_1000")) else 0.0,
          PROXY if pd.notna(r.get("health_per_1000")) else UNAVAILABLE,
          r.get("social_source") if has_social else None,
          r.get("social_source_level") if has_social else None,
          r.get("social_data_year") if has_social else None,
          "capacity per 1,000 residents" if pd.notna(r.get("health_per_1000"))
          else NO_CENSUS),

        # ---- EDUCATION (real: OSM facility locations) ----
        p("education_access", "education",
          r.get("education_nearest_m"), r.get("n_edu_access"), social_conf,
          REAL if has_social else UNAVAILABLE,
          r.get("social_source") if has_social else None,
          r.get("social_source_level") if has_social else None,
          r.get("social_data_year") if has_social else None,
          (f"{float(r.get('education_nearest_m')) / 1000:.1f} km from the centre "
           f"to the nearest mapped school, college or library"
           ) if has_social else "needs the OSM extract"),
        p("education_capacity", "education",
          r.get("education_per_km2"), r.get("n_edu_capacity"), social_conf,
          PROXY if has_social else UNAVAILABLE,
          r.get("social_source") if has_social else None,
          r.get("social_source_level") if has_social else None,
          r.get("social_data_year") if has_social else None,
          (f"{int(r.get('education_count') or 0)} mapped institutions, "
           f"capacity-weighted to {float(r.get('education_capacity') or 0):.1f} "
           f"per km2. Weighted by type, not by enrolment, which UDISE publishes "
           f"only at district level"
           ) if has_social else "needs the OSM extract"),
        p("education_deficit", "education",
          r.get("education_per_1000"), r.get("n_edu_per_1000"),
          social_conf * 0.8 if pd.notna(r.get("education_per_1000")) else 0.0,
          PROXY if pd.notna(r.get("education_per_1000")) else UNAVAILABLE,
          r.get("social_source") if has_social else None,
          r.get("social_source_level") if has_social else None,
          r.get("social_data_year") if has_social else None,
          "capacity per 1,000 residents" if pd.notna(r.get("education_per_1000"))
          else NO_CENSUS),

        # ---- BUDGET (the published devolution formula, applied) ----
        p("fiscal_entitlement", "budget",
          r.get("fiscal_share"), r.get("n_fiscal"), fiscal_conf,
          PROXY if has_fiscal else UNAVAILABLE,
          "Kerala State Finance Commission basic-grant formula" if has_fiscal else None,
          1 if has_fiscal else None, 2021 if has_fiscal else None,
          (f"{float(r.get('fiscal_share')) * 100:.2f}% of a district-wide grant "
           f"under the published rule: 80% population, 10% area, 10% inverse own "
           f"income. Own income is not published per local body, so estimated "
           f"per-capita income substitutes for that one term"
           ) if has_fiscal else "needs the economic layer"),
        p("own_income_capacity", "budget",
          r.get("income_per_capita_estimate"), r.get("n_own_income"),
          econ_conf, PROXY if has_fiscal else UNAVAILABLE,
          r.get("econ_source") if has_fiscal else None,
          r.get("econ_source_level") if has_fiscal else None,
          r.get("econ_data_year") if has_fiscal else None,
          "ability to raise and match funds locally, proxied by estimated "
          "per-capita income" if has_fiscal else "needs the economic layer"),
        p("cost_exposure", "budget",
          r.get("cost_index"), r.get("n_cost"),
          fiscal_conf * 0.8, PROXY if has_fiscal else UNAVAILABLE,
          "composite of density, flood exposure and narrow-road share"
          if has_fiscal else None,
          3 if has_fiscal else None, 2026 if has_fiscal else None,
          ("PERCENTILE RANK from 0 to 1 across the 97 local bodies, NOT a "
           "percentage and NOT a price. 0.90 means this ground is dearer to "
           "build on than 90% of the district, not that it costs 90% more. "
           "Composite of density, flood exposure and narrow-road share"
           ) if has_fiscal else "needs the economic layer"),

        p("employment_potential", "economic",
          r.get("employment_capacity_index"), r.get("n_employment"), econ_conf,
          PROXY if has_econ else UNAVAILABLE,
          r.get("econ_source") if has_econ else None,
          r.get("econ_source_level") if has_econ else None,
          r.get("econ_data_year") if has_econ else None,
          ("absorbable-jobs index: half estimated enterprise density, half "
           "population, both percentile-ranked — headroom for new employment "
           "rather than a count of existing jobs"
           ) if has_econ else "not published below district level"),
    ]
    return out


def summary() -> dict:
    frame = _derive(load_features())
    sample = parameters_for(frame.admin_id.iloc[0], frame)
    counts: dict[str, int] = {}
    for prm in sample:
        counts[prm.status] = counts.get(prm.status, 0) + 1
    return {"units": len(frame), "parameters": len(sample), "by_status": counts}


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2))
