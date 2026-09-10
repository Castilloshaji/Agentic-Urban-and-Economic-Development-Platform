"""Stage 4 — unified query layer over PostGIS + Neo4j.

Every value comes back tagged with the vintage it was published under. A
population of 38,500 is not a fact; "38,500, census 2011, final, counted on the
pre-2025 ward split" is.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
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
            SELECT station_id, name, line, source, source_date, data_year, revision_status,
                   dataset_edition, match_confidence, soft_check_flags::text AS soft_check_flags,
                   ST_X(geom) AS lon, ST_Y(geom) AS lat
              FROM metro_station
             WHERE admin_id = %s AND dataset_edition = %s
             ORDER BY station_id
            """,
            (admin_id, boundary_edition),
        )
        metro_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT stop_id, name, route_id, feed_start_date, feed_end_date, source, source_date,
                   data_year, revision_status, dataset_edition, match_confidence,
                   soft_check_flags::text AS soft_check_flags,
                   ST_X(geom) AS lon, ST_Y(geom) AS lat
              FROM bus_stop
             WHERE admin_id = %s AND dataset_edition = %s
             ORDER BY stop_id
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
