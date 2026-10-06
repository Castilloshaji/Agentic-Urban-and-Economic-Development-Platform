"""Stage 4 — unified query layer over PostGIS + Neo4j.

Every value comes back tagged with the vintage it was published under. A
population of 38,500 is not a fact; "38,500, census 2011, final, counted on the
pre-2025 ward split" is.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..storage.neo4j.load import neo4j_settings
from ..storage.postgres.load import connection_string

STALE_FEED_MONTHS = 12

# Ordered coarse-to-fine, for walking up the hierarchy in search of a figure.
LEVEL_ORDER = ("district", "taluk", "panchayat", "ward")


# connections

@contextmanager
def _postgres():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    connection = psycopg2.connect(connection_string())
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
    finally:
        connection.close()


@contextmanager
def _neo4j():
    from neo4j import GraphDatabase

    uri, user, password = neo4j_settings()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            yield session
    finally:
        driver.close()


# the tagged-value contract

def tagged(value: Any, row: dict | None = None, **overrides) -> dict:
    """Wrap a value with the vintage metadata it was published under.

    Never returns a bare scalar. A missing value is still tagged — the caller
    needs to know that a null came from a source that had nothing to say, and
    which vintage that silence belongs to.
    """
    row = row or {}
    tag = {
        "value": value,
        "data_year": row.get("data_year"),
        "revision_status": row.get("revision_status"),
        "boundary_vintage": row.get("boundary_vintage"),
        "source": row.get("source"),
        "source_date": row.get("source_date"),
        "match_confidence": row.get("match_confidence", "exact"),
        "dataset_edition": row.get("dataset_edition"),
        # Review 2: the source hierarchy level (1 government .. 4 secondary).
        # Carried here so a consumer can tell an official figure from a
        # community-maintained one without going back to the register.
        "source_level": row.get("source_level"),
    }
    tag.update(overrides)
    # Some sources (the metro station CSV, for one) publish no vintage columns at
    # all. Saying so beats returning a row of silent nulls that reads like a
    # lookup failure — the value is usable, its provenance simply was not stated.
    if all(tag.get(field) is None for field in
           ("data_year", "revision_status", "boundary_vintage", "source", "source_date")):
        tag["vintage"] = "unpublished_by_source"
    return tag


def _soft_flags(row: dict) -> list[dict]:
    raw = row.get("soft_check_flags")
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else raw


def _months_before(reference: date | None, today: date) -> int | None:
    if not reference:
        return None
    return (today.year - reference.year) * 12 + (today.month - reference.month)


# boundary lookup

def _fetch_boundary(cursor, admin_id: str, edition: str | None) -> dict | None:
    cursor.execute(
        """
        SELECT admin_id, name, level, parent_id, source, source_date, data_year,
               revision_status, boundary_vintage, dataset_edition, match_confidence,
               soft_check_flags::text AS soft_check_flags,
               ST_Area(geom::geography) AS area_m2,
               ST_AsGeoJSON(ST_Centroid(geom)) AS centroid
          FROM admin_boundary
         WHERE admin_id = %s AND (%s IS NULL OR dataset_edition = %s)
         ORDER BY dataset_edition
         LIMIT 1
        """,
        (admin_id, edition, edition),
    )
    return cursor.fetchone()


def _boundary_block(row: dict) -> dict:
    centroid = json.loads(row["centroid"]) if row.get("centroid") else None
    return {
        "admin_id": row["admin_id"],
        "name": tagged(row.get("name"), row),
        "level": tagged(row.get("level"), row),
        "parent_id": tagged(row.get("parent_id"), row),
        "area_m2": tagged(round(row["area_m2"], 2) if row.get("area_m2") is not None else None, row),
        "centroid": tagged(centroid["coordinates"] if centroid else None, row),
        "soft_check_flags": _soft_flags(row),
    }


def _not_found(admin_id: str, edition: str | None) -> dict:
    return {
        "admin_id": admin_id,
        "found": False,
        "dataset_edition": edition,
        "note": (
            f"No boundary for {admin_id!r} in PostGIS"
            + (f" for edition {edition!r}" if edition else "")
            + ". It may have been withheld by a Stage 2 hard check — see "
              "data/processed/hard_check_failures.json."
        ),
    }


# public API

def get_flood_risk(admin_id: str, edition: str | None = None) -> dict:
    """Flood exposure for one admin unit, from the Neo4j OVERLAPS edges.

    The flood layer is 2010 KSDMA/NCESS fieldwork. Its data_year rides on every
    value here precisely so that no agent can present it as current conditions.
    """
    with _postgres() as cursor:
        boundary = _fetch_boundary(cursor, admin_id, edition)
        if not boundary:
            return _not_found(admin_id, edition)
        boundary_edition = boundary["dataset_edition"]

    with _neo4j() as session:
        records = session.run(
            """
            MATCH (f:FloodZone)-[r:OVERLAPS]->(a {admin_id: $admin_id})
            WHERE a.dataset_edition = $edition
            RETURN f.flood_zone_id AS flood_zone_id, f.risk_level AS risk_level,
                   f.data_year AS data_year, f.revision_status AS revision_status,
                   f.source AS source, f.dataset_edition AS dataset_edition,
                   r.overlap_area_m2 AS overlap_area_m2,
                   r.overlap_ratio_of_admin AS overlap_ratio
            ORDER BY r.overlap_area_m2 DESC
            """,
            admin_id=admin_id, edition=boundary_edition,
        ).data()

    zones = [
        {
            "flood_zone_id": record["flood_zone_id"],
            "risk_level": tagged(record["risk_level"], record),
            "overlap_area_m2": tagged(record["overlap_area_m2"], record),
            "overlap_ratio_of_admin": tagged(record["overlap_ratio"], record),
        }
        for record in records
    ]
    exposed_ratio = max((r["overlap_ratio"] or 0) for r in records) if records else 0.0
    return {
        "admin_id": admin_id,
        "found": True,
        "dataset_edition": boundary_edition,
        "flood_zones": zones,
        "zone_count": len(zones),
        "max_overlap_ratio": tagged(
            round(exposed_ratio, 6),
            records[0] if records else {},
            note="share of this admin unit inside its most overlapping flood zone",
        ),
        "caveat": (
            "Flood zones are a static historical hazard classification, not a "
            "real-time or forecast product. Read data_year before acting on this."
        ),
    }


def get_transit_access(admin_id: str, edition: str | None = None) -> dict:
    """Metro and bus access for one admin unit, with feed currency.

    A stop whose GTFS feed expired years ago is still a stop, so it is returned —
    but with feed_is_stale set, because "there is a bus stop here" and "buses
    currently serve this stop" are different claims.

    Stations and stops are matched by geometry, not by their `admin_id` column.
    The real KMRL and GTFS sources tag every point with the district
    (`EKM-D`), so an attribute join finds nothing below district level — it
    silently returned zero stations for every panchayat. The coordinates are
    real, so containment is the reliable test and it works whatever a source
    chose to put in that column.
    """
    today = date.today()
    with _postgres() as cursor:
        boundary = _fetch_boundary(cursor, admin_id, edition)
        if not boundary:
            return _not_found(admin_id, edition)
        boundary_edition = boundary["dataset_edition"]
        centroid = json.loads(boundary["centroid"])["coordinates"] if boundary.get("centroid") else None

        cursor.execute(
            """
            SELECT m.station_id, m.name, m.line, m.source, m.source_date, m.data_year,
                   m.revision_status, m.dataset_edition, m.match_confidence,
                   m.soft_check_flags::text AS soft_check_flags,
                   ST_X(m.geom) AS lon, ST_Y(m.geom) AS lat
              FROM metro_station m
              JOIN admin_boundary a
                ON a.admin_id = %s AND a.dataset_edition = m.dataset_edition
               AND ST_Contains(a.geom, m.geom)
             WHERE m.dataset_edition = %s
             ORDER BY m.station_id
            """,
            (admin_id, boundary_edition),
        )
        metro_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT b.stop_id, b.name, b.route_id, b.feed_start_date, b.feed_end_date,
                   b.source, b.source_date, b.data_year, b.revision_status,
                   b.dataset_edition, b.match_confidence,
                   b.soft_check_flags::text AS soft_check_flags,
                   ST_X(b.geom) AS lon, ST_Y(b.geom) AS lat
              FROM bus_stop b
              JOIN admin_boundary a
                ON a.admin_id = %s AND a.dataset_edition = b.dataset_edition
               AND ST_Contains(a.geom, b.geom)
             WHERE b.dataset_edition = %s
             ORDER BY b.stop_id
            """,
            (admin_id, boundary_edition),
        )
        bus_rows = cursor.fetchall()

    metro_stations = [
        {
            "station_id": row["station_id"],
            "name": tagged(row["name"], row),
            "line": tagged(row["line"], row),
            "location": tagged([row["lon"], row["lat"]], row),
            "soft_check_flags": _soft_flags(row),
        }
        for row in metro_rows
    ]

    bus_stops = []
    stale_count = 0
    for row in bus_rows:
        months = _months_before(row.get("feed_end_date"), today)
        is_stale = months is not None and months > STALE_FEED_MONTHS
        stale_count += int(is_stale)
        bus_stops.append({
            "stop_id": row["stop_id"],
            "name": tagged(row["name"], row),
            "route_id": tagged(row["route_id"], row),
            "location": tagged([row["lon"], row["lat"]], row),
            "feed_end_date": tagged(row.get("feed_end_date"), row),
            "feed_is_stale": tagged(is_stale, row, months_since_feed_end=months,
                                    threshold_months=STALE_FEED_MONTHS),
            "soft_check_flags": _soft_flags(row),
        })

    return {
        "admin_id": admin_id,
        "found": True,
        "dataset_edition": boundary_edition,
        "centroid": centroid,
        "metro_stations": metro_stations,
        "bus_stops": bus_stops,
        "metro_station_count": tagged(len(metro_stations), boundary),
        "bus_stop_count": tagged(len(bus_stops), boundary),
        "stale_feed_count": tagged(stale_count, boundary,
                                   note="bus stops whose GTFS feed ended over a year ago"),
    }


def get_economic_profile(admin_id: str, edition: str | None = None) -> dict:
    """Economic indicator series for one admin unit.

    Two behaviours matter here. Indicators are published at district level, so a
    panchayat query walks up the LOCATED_IN hierarchy and says so — the figure is
    returned as inherited, at unmatched-estimate confidence, never presented as
    the panchayat's own. And a series with a missing year keeps the gap: the year
    is listed in missing_years rather than interpolated or dropped, because a
    genuine hole in a published series is information.
    """
    with _postgres() as cursor:
        boundary = _fetch_boundary(cursor, admin_id, edition)
        if not boundary:
            return _not_found(admin_id, edition)
        boundary_edition = boundary["dataset_edition"]

        def fetch_for(target_id):
            cursor.execute(
                """
                SELECT admin_id, indicator, value, value_unit, unit, year, source, source_date,
                       revision_status, dataset_edition, match_confidence,
                       soft_check_flags::text AS soft_check_flags
                  FROM economic_indicator
                 WHERE admin_id = %s AND dataset_edition = %s
                 ORDER BY indicator, year, revision_status
                """,
                (target_id, boundary_edition),
            )
            return cursor.fetchall()

        rows = fetch_for(admin_id)
        inherited_from = None

        if not rows:
            # Walk up the hierarchy for the nearest ancestor that publishes figures.
            with _neo4j() as session:
                ancestors = session.run(
                    """
                    MATCH (a {admin_id: $admin_id})-[:LOCATED_IN*1..3]->(ancestor)
                    WHERE a.dataset_edition = $edition
                    RETURN ancestor.admin_id AS admin_id, head(labels(ancestor)) AS label
                    """,
                    admin_id=admin_id, edition=boundary_edition,
                ).data()
            for ancestor in ancestors:
                rows = fetch_for(ancestor["admin_id"])
                if rows:
                    inherited_from = ancestor
                    break

    indicators: dict[str, dict] = {}
    for row in rows:
        series = indicators.setdefault(row["indicator"], {"observations": [], "unit": row.get("unit")})
        confidence = row.get("match_confidence", "exact")
        if inherited_from:
            # A district figure is not this panchayat's figure, whatever the
            # source's own confidence was.
            confidence = "unmatched-estimate"
        series["observations"].append({
            "year": row["year"],
            **tagged(float(row["value"]) if row["value"] is not None else None, row,
                     match_confidence=confidence),
            "value_unit": row.get("value_unit"),
            "soft_check_flags": _soft_flags(row),
        })

    for indicator, series in indicators.items():
        years = sorted({o["year"] for o in series["observations"] if o["year"] is not None})
        series["years_covered"] = years
        series["missing_years"] = (
            [y for y in range(years[0], years[-1] + 1) if y not in years] if len(years) > 1 else []
        )
        finals = [o for o in series["observations"] if o.get("revision_status") == "final"]
        series["latest_final"] = max(finals, key=lambda o: o["year"]) if finals else None
        series["has_revision_conflict"] = any(
            flag.get("check") == "revision_conflict"
            for o in series["observations"] for flag in o["soft_check_flags"]
        )

    return {
        "admin_id": admin_id,
        "found": True,
        "dataset_edition": boundary_edition,
        "indicators": indicators,
        "inherited_from": inherited_from,
        "note": (
            f"No indicators published at {admin_id}; figures inherited from "
            f"{inherited_from['admin_id']} ({inherited_from['label']}) and returned at "
            "unmatched-estimate confidence."
            if inherited_from else None
        ),
    }


def get_context(admin_id: str, edition: str | None = None) -> dict:
    """Everything the twin knows about one admin unit.

    PostGIS supplies the boundary, its population and its indicators; Neo4j
    supplies what it is connected to. This is the entry point a domain agent
    calls before it reasons about anything.
    """
    with _postgres() as cursor:
        boundary = _fetch_boundary(cursor, admin_id, edition)
        if not boundary:
            return _not_found(admin_id, edition)
        boundary_edition = boundary["dataset_edition"]

        cursor.execute(
            """
            SELECT admin_id, population, male_population, female_population, literacy_rate,
                   households, source, source_date, data_year, revision_status,
                   boundary_vintage, dataset_edition, match_confidence,
                   soft_check_flags::text AS soft_check_flags
              FROM population_panchayat
             WHERE admin_id = %s AND dataset_edition = %s
             ORDER BY (revision_status = 'final') DESC, data_year DESC
            """,
            (admin_id, boundary_edition),
        )
        population_rows = cursor.fetchall()

    population = None
    if population_rows:
        row = population_rows[0]
        population = {
            "population": tagged(row["population"], row),
            "male_population": tagged(row["male_population"], row),
            "female_population": tagged(row["female_population"], row),
            # Explicitly null, not zero — Stage 2 kept the gap and so does this.
            "literacy_rate": tagged(
                float(row["literacy_rate"]) if row["literacy_rate"] is not None else None, row),
            "households": tagged(row["households"], row),
            "revisions_available": len(population_rows),
            "soft_check_flags": _soft_flags(row),
        }

    with _neo4j() as session:
        infrastructure = session.run(
            """
            MATCH (a {admin_id: $admin_id}) WHERE a.dataset_edition = $edition
            OPTIONAL MATCH (m:MetroStation {admin_id: $admin_id, dataset_edition: $edition})
            OPTIONAL MATCH (b:BusStop {admin_id: $admin_id, dataset_edition: $edition})
            OPTIONAL MATCH (f:FloodZone)-[:OVERLAPS]->(a)
            OPTIONAL MATCH (a)-[:LOCATED_IN]->(parent)
            OPTIONAL MATCH (child)-[:LOCATED_IN]->(a)
            RETURN
              collect(DISTINCT {id: m.station_id, name: m.name, line: m.line,
                                data_year: m.data_year, revision_status: m.revision_status,
                                source: m.source}) AS metro,
              collect(DISTINCT {id: b.stop_id, name: b.name, route_id: b.route_id,
                                feed_end_date: b.feed_end_date, data_year: b.data_year,
                                revision_status: b.revision_status, source: b.source}) AS bus,
              collect(DISTINCT {id: f.flood_zone_id, risk_level: f.risk_level,
                                data_year: f.data_year, revision_status: f.revision_status,
                                source: f.source}) AS flood,
              collect(DISTINCT {id: parent.admin_id, label: head(labels(parent)),
                                boundary_vintage: parent.boundary_vintage}) AS parents,
              collect(DISTINCT {id: child.admin_id, label: head(labels(child)),
                                boundary_vintage: child.boundary_vintage}) AS children
            """,
            admin_id=admin_id, edition=boundary_edition,
        ).single()

    def clean(items):
        # OPTIONAL MATCH yields a {id: null} placeholder when nothing matched.
        return [item for item in (items or []) if item.get("id")]

    connected = {
        "metro_stations": [
            {"station_id": item["id"], "name": tagged(item["name"], item),
             "line": tagged(item["line"], item)} for item in clean(infrastructure["metro"])
        ],
        "bus_stops": [
            {"stop_id": item["id"], "name": tagged(item["name"], item),
             "route_id": tagged(item["route_id"], item),
             "feed_end_date": tagged(item["feed_end_date"], item)}
            for item in clean(infrastructure["bus"])
        ],
        "flood_zones": [
            {"flood_zone_id": item["id"], "risk_level": tagged(item["risk_level"], item)}
            for item in clean(infrastructure["flood"])
        ],
        "parents": clean(infrastructure["parents"]),
        "children": clean(infrastructure["children"]),
    }

    return {
        "admin_id": admin_id,
        "found": True,
        "dataset_edition": boundary_edition,
        "retrieved_at": datetime.now().astimezone().isoformat(),
        "boundary": _boundary_block(boundary),
        "population": population,
        "economic_profile": get_economic_profile(admin_id, boundary_edition),
        "flood_risk": get_flood_risk(admin_id, boundary_edition),
        "connected_infrastructure": connected,
        "vintage_warning": (
            "Values carry their own data_year / revision_status / boundary_vintage. "
            "Population is counted on the pre-2025 ward split while boundaries are the "
            "2025 delimitation — do not sum across the two without saying so."
        ),
    }


# ---------------------------------------------------------------------------
# Review 2 extensions
#
# These read the real hazard and transit features from PostGIS rather than the
# CSVs, so the twin stays the single query layer and the new data sits under the
# same cross-store verification as everything else.
# ---------------------------------------------------------------------------

# Which derived columns are measurements and which are allocations. The split is
# hard-coded rather than inferred because getting it wrong would present an
# estimate as a measurement, which is the one mistake this layer exists to
# prevent.
DERIVED_MEASURED = ("road_density_km_per_km2", "max_road_class", "row_narrow_share",
                    "row_arterial_km_per_km2", "row_lane_tag_coverage",
                    "water_area_share", "water_distance_m", "nearest_metro_m",
                    "nearest_metro_station", "metro_stations_in_unit",
                    "health_count", "health_nearest_m", "health_per_km2",
                    "education_count", "education_nearest_m", "education_per_km2")
DERIVED_ESTIMATED = ("msme_estimated_count", "msme_per_1000_people",
                     "msme_density_per_km2", "income_per_capita_estimate",
                     "gddp_share_estimate_crore", "employment_capacity_index",
                     "health_capacity", "health_per_1000",
                     "education_capacity", "education_per_1000")


def get_derived_features(admin_id: str, edition: str | None = None) -> dict:
    """The derived feature layers for one unit: OSM, KMRL, and the estimates.

    These are the quantities the Phase-1 tables never carried — distance to the
    nearest metro station, how much of the unit is water, how much of its road
    network is narrow-class, and the economic figures allocated down from
    published district totals.

    Measurements and estimates are returned in separate blocks, each value
    vintage-tagged, and every estimate carries `is_estimate: True`. An agent
    reading this cannot confuse "687 m from water" with "~2,223 enterprises"
    unless it ignores the label.
    """
    with _postgres() as cursor:
        boundary = _fetch_boundary(cursor, admin_id, edition)
        if not boundary:
            return _not_found(admin_id, edition)
        boundary_edition = boundary["dataset_edition"]
        cursor.execute(
            """
            SELECT * FROM admin_derived_feature
             WHERE admin_id = %s AND dataset_edition = %s
            """,
            (admin_id, boundary_edition),
        )
        row = cursor.fetchone()

    if not row:
        return {
            "admin_id": admin_id, "found": False,
            "dataset_edition": boundary_edition,
            "note": ("No derived features for this unit. Build them with "
                     "python3 -m src.features.build_all, then load with "
                     "python3 -m src.storage.postgres.load_features."),
        }

    def block(columns, is_estimate):
        out = {}
        for column in columns:
            value = row.get(column)
            out[column] = tagged(
                float(value) if isinstance(value, Decimal) else value, row,
                is_estimate=is_estimate)
        return out

    return {
        "admin_id": admin_id,
        "found": True,
        "dataset_edition": boundary_edition,
        "retrieved_at": datetime.now().astimezone().isoformat(),
        "measured": block(DERIVED_MEASURED, False),
        "estimated": block(DERIVED_ESTIMATED, True),
        "allocation": row.get("econ_allocation"),
        "population_imputed": bool(row.get("econ_population_imputed")),
        "caveat": ("Values under 'estimated' are allocations of published district "
                   "figures, not measurements of this unit. See GET /api/anchors "
                   "for the figures they were allocated from."),
    }


def get_development_parameters(admin_id: str, scenario_key: str | None = None) -> dict:
    """Every parameter for one local body, optionally narrowed to one scenario."""
    from ..decision.parameters import parameters_for
    from ..decision.scenarios import get as get_scenario

    params = [p.as_dict() for p in parameters_for(admin_id)]
    if scenario_key:
        wanted = {n for names in get_scenario(scenario_key).parameters.values() for n in names}
        params = [p for p in params if p["name"] in wanted]
    counts: dict[str, int] = {}
    for prm in params:
        counts[prm["status"]] = counts.get(prm["status"], 0) + 1
    return {"admin_id": admin_id, "scenario": scenario_key,
            "parameters": params, "by_status": counts}


def get_environmental_risk(admin_id: str) -> dict:
    """Measured hazard for one unit, straight from the loaded KSDMA/GSI tables."""
    with _postgres() as cursor:
        cursor.execute(
            """SELECT name, flood_share_hist_50yr, flood_share_rcp85_50yr,
                      flood_share_hist_100yr, landslide_susceptibility, landslide_rank,
                      source, source_level, data_year
                 FROM admin_hazard_feature WHERE admin_id = %s""", (admin_id,))
        row = cursor.fetchone()
    if not row:
        return _not_found(admin_id, None)
    hist = row["flood_share_hist_50yr"]
    rcp = row["flood_share_rcp85_50yr"]
    meta = {"source": row["source"], "source_level": row["source_level"],
            "data_year": row["data_year"]}
    return {
        "admin_id": admin_id, "name": row["name"], "found": True,
        "flood_share_50yr": tagged(float(hist) if hist is not None else None, meta),
        "flood_share_100yr": tagged(
            float(row["flood_share_hist_100yr"]) if row["flood_share_hist_100yr"] is not None else None, meta),
        "flood_share_rcp85_50yr": tagged(float(rcp) if rcp is not None else None, meta),
        "climate_delta": tagged(
            round(float(rcp) - float(hist), 6) if (hist is not None and rcp is not None) else None,
            meta, note="RCP 8.5 minus historical at the 50-year return period"),
        "landslide_susceptibility": tagged(row["landslide_susceptibility"], meta),
        "caveat": "Flood values are a modelled return-period quantity, not a depth. "
                  "The layer is a hazard classification, not a forecast.",
    }


def get_accessibility_score(admin_id: str) -> dict:
    """Transit accessibility from the real GTFS feed, with its staleness."""
    with _postgres() as cursor:
        cursor.execute(
            """SELECT stop_count, stop_density_per_km2, trips_per_stop, nearest_stop_m,
                      feed_end_date, feed_months_stale, source, source_level, data_year
                 FROM transit_feature WHERE admin_id = %s""", (admin_id,))
        row = cursor.fetchone()
    if not row:
        return _not_found(admin_id, None)
    meta = {"source": row["source"], "source_level": row["source_level"],
            "data_year": row["data_year"]}
    stale = row["feed_months_stale"]
    return {
        "admin_id": admin_id, "found": True,
        "stop_count": tagged(row["stop_count"], meta),
        "stop_density_per_km2": tagged(
            float(row["stop_density_per_km2"]) if row["stop_density_per_km2"] is not None else None, meta),
        "trips_per_stop": tagged(
            float(row["trips_per_stop"]) if row["trips_per_stop"] is not None else None, meta),
        "nearest_stop_m": tagged(
            float(row["nearest_stop_m"]) if row["nearest_stop_m"] is not None else None, meta),
        "feed_is_stale": tagged(bool(stale and stale > STALE_FEED_MONTHS), meta,
                                months_since_feed_end=stale,
                                threshold_months=STALE_FEED_MONTHS),
    }


def get_infrastructure_score(admin_id: str) -> dict:
    """Infrastructure-domain parameters, with the unavailable ones named."""
    bundle = get_development_parameters(admin_id)
    infra = [p for p in bundle["parameters"] if p["domain"] == "infrastructure"]
    available = [p for p in infra if p["normalized"] is not None]
    return {
        "admin_id": admin_id,
        "score": round(sum(p["normalized"] for p in available) / len(available), 4) if available else None,
        "parameters": infra,
        "unavailable": [p["name"] for p in infra if p["status"] == "unavailable"],
    }


def get_economic_potential(admin_id: str) -> dict:
    """Economic-domain parameters. Most are proxies — the dict says which."""
    bundle = get_development_parameters(admin_id)
    econ = [p for p in bundle["parameters"] if p["domain"] == "economic"]
    available = [p for p in econ if p["normalized"] is not None]
    return {
        "admin_id": admin_id,
        "score": round(sum(p["normalized"] for p in available) / len(available), 4) if available else None,
        "parameters": econ,
        "proxies": [p["name"] for p in econ if p["status"] == "proxy"],
        "unavailable": [p["name"] for p in econ if p["status"] == "unavailable"],
        "caveat": "Economic indicators are published at district level only; nothing "
                  "here is a measured local-body economic figure.",
    }


def get_agent_priority_features(admin_id: str, scenario_key: str) -> dict:
    """The full priority computation — the twin's view of the decision engine."""
    from ..decision.priority import compute
    return compute(admin_id, scenario_key)


def get_candidate_area_score(scenario_key: str, limit: int = 15) -> list[dict]:
    """Rank local bodies for one objective."""
    from ..decision.runner import rank_units
    return rank_units(scenario_key, limit)
