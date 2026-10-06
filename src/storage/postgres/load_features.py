"""Load the real hazard and transit features into PostGIS, and persist runs.

The parameter engine originally read CSVs under data/processed/features. That
made the dashboard work but left the measured hazard data outside the digital
twin, and therefore outside the cross-store verification. These loaders put it
in the database so it is queryable, joinable and auditable like everything else.

    python3 -m src.storage.postgres.load_features
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .load import connection_string

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FEATURES = PROJECT_ROOT / "data" / "features"
EDITION = "ernakulam"

HAZARD_MAP = {
    "flood_share_historical_10yr": "flood_share_hist_10yr",
    "flood_share_historical_25yr": "flood_share_hist_25yr",
    "flood_share_historical_50yr": "flood_share_hist_50yr",
    "flood_share_historical_100yr": "flood_share_hist_100yr",
    "flood_share_historical_200yr": "flood_share_hist_200yr",
    "flood_share_historical_500yr": "flood_share_hist_500yr",
    "flood_share_rcp85_10yr": "flood_share_rcp85_10yr",
    "flood_share_rcp85_25yr": "flood_share_rcp85_25yr",
    "flood_share_rcp85_50yr": "flood_share_rcp85_50yr",
    "flood_share_rcp85_100yr": "flood_share_rcp85_100yr",
    "flood_share_rcp85_200yr": "flood_share_rcp85_200yr",
    "flood_share_rcp85_500yr": "flood_share_rcp85_500yr",
}


def _none(value):
    return None if value is None or pd.isna(value) else value


def load_hazard(cursor) -> int:
    frame = pd.read_csv(FEATURES / "hazard_features.csv")
    columns = (["admin_id", "name", "local_auth"] + list(HAZARD_MAP.values())
               + ["landslide_rank", "landslide_susceptibility",
                  "source", "source_level", "data_year", "dataset_edition"])
    rows = []
    for _, r in frame.iterrows():
        rows.append(tuple([r.admin_id, _none(r.get("name")), _none(r.get("local_auth"))]
                          + [_none(r.get(src)) for src in HAZARD_MAP]
                          + [_none(r.get("landslide_rank")),
                             _none(r.get("landslide_susceptibility")),
                             _none(r.get("hazard_source")), _none(r.get("hazard_source_level")),
                             _none(r.get("hazard_data_year")), EDITION]))
    cursor.execute("TRUNCATE admin_hazard_feature")
    cursor.executemany(
        f"INSERT INTO admin_hazard_feature ({', '.join(columns)}) "
        f"VALUES ({', '.join(['%s'] * len(columns))})", rows)
    return len(rows)


def load_transit(cursor) -> int:
    frame = pd.read_csv(FEATURES / "transit_features.csv")
    columns = ["admin_id", "area_km2", "stop_count", "trips_total",
               "stop_density_per_km2", "trips_per_stop", "nearest_stop_m",
               "feed_end_date", "feed_months_stale", "source", "source_level",
               "data_year", "dataset_edition"]
    rows = []
    for _, r in frame.iterrows():
        feed = str(int(r.transit_feed_end_date)) if pd.notna(r.get("transit_feed_end_date")) else None
        feed_date = f"{feed[:4]}-{feed[4:6]}-{feed[6:]}" if feed and len(feed) == 8 else None
        rows.append((r.admin_id, _none(r.area_km2), _none(r.stop_count), _none(r.trips_total),
                     _none(r.stop_density_per_km2), _none(r.trips_per_stop),
                     _none(r.nearest_stop_m), feed_date, _none(r.get("transit_feed_months_stale")),
                     _none(r.get("transit_source")), _none(r.get("transit_source_level")),
                     2022, EDITION))
    cursor.execute("TRUNCATE transit_feature")
    cursor.executemany(
        f"INSERT INTO transit_feature ({', '.join(columns)}) "
        f"VALUES ({', '.join(['%s'] * len(columns))})", rows)
    return len(rows)


# (column in admin_derived_feature, source file, column in that file)
DERIVED_SOURCES = (
    ("road_features.csv", {
        "road_density_km_per_km2": "road_density_km_per_km2",
        "max_road_class": "max_road_class"}),
    ("row_features.csv", {
        "row_narrow_share": "row_narrow_share",
        "row_arterial_km_per_km2": "row_arterial_km_per_km2",
        "row_lane_tag_coverage": "row_lane_tag_coverage"}),
    ("water_features.csv", {
        "water_area_share": "water_area_share",
        "water_distance_m": "water_distance_m"}),
    ("metro_features.csv", {
        "nearest_metro_m": "nearest_metro_m",
        "nearest_metro_station": "nearest_metro_station",
        "metro_stations_in_unit": "metro_stations_in_unit"}),
    ("social_features.csv", {
        "health_count": "health_count",
        "health_capacity": "health_capacity",
        "health_nearest_m": "health_nearest_m",
        "health_per_km2": "health_per_km2",
        "health_per_1000": "health_per_1000",
        "education_count": "education_count",
        "education_capacity": "education_capacity",
        "education_nearest_m": "education_nearest_m",
        "education_per_km2": "education_per_km2",
        "education_per_1000": "education_per_1000"}),
    ("econ_features.csv", {
        "msme_estimated_count": "msme_estimated_count",
        "msme_per_1000_people": "msme_per_1000_people",
        "msme_density_per_km2": "msme_density_per_km2",
        "income_per_capita_estimate": "income_per_capita_estimate",
        "gddp_share_estimate_crore": "gddp_share_estimate_crore",
        "employment_capacity_index": "employment_capacity_index",
        "econ_allocation": "econ_allocation",
        "econ_population_imputed": "econ_population_imputed"}),
)


def load_derived(cursor) -> int:
    """Load the derived layers so the twin can serve them.

    Built one row per local body by outer-joining whichever layer files exist.
    A missing file leaves its columns null rather than failing: the OSM-derived
    layers need a 532 MB extract that is not on every machine, and the engine's
    contract is to report those values unavailable, not to refuse to load.
    """
    base = pd.read_csv(FEATURES / "hazard_features.csv")[["admin_id"]]
    present = []
    for filename, mapping in DERIVED_SOURCES:
        path = FEATURES / filename
        if not path.exists():
            print(f"  note: {filename} absent — its columns stay null")
            continue
        frame = pd.read_csv(path)
        keep = ["admin_id"] + [c for c in mapping.values() if c in frame.columns]
        base = base.merge(frame[keep], on="admin_id", how="left")
        present.append(filename)
    if not present:
        print("  no derived layer present — nothing to load")
        return 0

    columns = [c for _, mapping in DERIVED_SOURCES for c in mapping
               if mapping[c] in base.columns]
    full = columns + ["source", "source_level", "data_year", "dataset_edition"]
    rows = [
        tuple([_none(r.get(c)) for c in columns]
              + ["derived feature layers: OSM clip, KMRL station list, "
                 "district anchors allocated", 1, 2026, EDITION])
        for _, r in base.iterrows()
    ]
    # admin_id leads the insert; it is the natural key, not a measured value.
    cursor.execute("TRUNCATE admin_derived_feature")
    cursor.executemany(
        f"INSERT INTO admin_derived_feature (admin_id, {', '.join(full)}) "
        f"VALUES ({', '.join(['%s'] * (len(full) + 1))})",
        [(admin_id, *row) for admin_id, row in zip(base.admin_id, rows)])
    print(f"  from: {', '.join(present)}")
    return len(rows)


def persist_run(result: dict) -> None:
    """Write one scenario run and its full audit trail to the database."""
    import psycopg2

    run_id = result["run_id"]
    priority, constraints, decision = result["priority"], result["constraints"], result["decision"]

    with psycopg2.connect(connection_string()) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM scenario_run WHERE run_id = %s", (run_id,))
        cur.execute(
            """INSERT INTO scenario_run (run_id, admin_id, admin_name, scenario_key,
                 scenario_label, budget_inr_crore, formula_version, llm_involved,
                 suitability_score, stance, leading_domain, constraint_verdict,
                 blocking_count, dataset_edition, generated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, result["admin_id"], result["admin_name"], result["scenario"]["key"],
             result["scenario"]["label"], result.get("budget_inr_crore"),
             priority["formula_version"], result["determinism"]["llm_involved"],
             decision["suitability_score"], decision["stance"], decision["leading_domain"],
             constraints["verdict"], constraints["blocking_count"], EDITION,
             result["generated_at"]))

        cur.executemany(
            """INSERT INTO scenario_parameter (run_id, parameter, domain, raw_value,
                 normalized, confidence, status, source, source_level, data_year, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, parameter) DO NOTHING""",
            [(run_id, p["name"], p["domain"], p["value"], p["normalized"], p["confidence"],
              p["status"], p["source"], p["source_level"], p["data_year"], p["note"])
             for p in priority["parameters"]])

        cur.executemany(
            """INSERT INTO agent_priority (run_id, domain, scenario_relevance, relevance_term,
                 evidence_confidence, parameter_signal, signal_modulator, raw_weight,
                 final_weight, rank, parameters_used, parameters_skipped)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(run_id, d, x["scenario_relevance"], x["relevance_term"], x["evidence_confidence"],
              x["parameter_signal"], x["signal_modulator"], x["raw_weight"], x["final_weight"],
              x["rank"], len(x["parameters_used"]), len(x["parameters_skipped"]))
             for d, x in priority["detail"].items()])

        if constraints["constraints"]:
            cur.executemany(
                """INSERT INTO constraint_result (run_id, code, severity, domain, parameter,
                     measured_value, threshold, message, source, source_level, data_year,
                     overridable_by_model)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, code) DO NOTHING""",
                [(run_id, c["code"], c["severity"], c["domain"], c["parameter"],
                  c["measured_value"], c["threshold"], c["message"], c["source"],
                  c["source_level"], c["data_year"], c["overridable_by_model"])
                 for c in constraints["constraints"]])

        cur.execute(
            """INSERT INTO decision (run_id, stance, headline, conditions, advisories, agent_outputs)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (run_id, decision["stance"], decision["headline"],
             json.dumps(decision["conditions"]), json.dumps(decision["advisories"]),
             json.dumps(result.get("agents")) if result.get("agents") else None))

        cur.executemany(
            "INSERT INTO audit_event (run_id, stage, event, detail) VALUES (%s,%s,%s,%s)",
            [(run_id, "parameters", "computed",
              json.dumps({"count": len(priority["parameters"])})),
             (run_id, "priority", "weights_normalized",
              json.dumps({"weights": priority["weights"], "sum": priority["weight_sum"],
                          "formula": priority["formula_version"]})),
             (run_id, "constraints", constraints["verdict"],
              json.dumps({"blocking": constraints["blocking_count"]})),
             (run_id, "decision", decision["stance"],
              json.dumps({"score": decision["suitability_score"]}))])
        conn.commit()


def main() -> int:
    import psycopg2

    with psycopg2.connect(connection_string()) as conn, conn.cursor() as cur:
        hazard = load_hazard(cur)
        transit = load_transit(cur)
        derived = load_derived(cur)
        conn.commit()
    print(f"  admin_hazard_feature:   {hazard} rows")
    print(f"  transit_feature:        {transit} rows")
    print(f"  admin_derived_feature:  {derived} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
