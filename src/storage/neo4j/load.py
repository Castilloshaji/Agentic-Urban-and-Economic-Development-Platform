"""Stage 3 — load the knowledge graph into Neo4j.

The node set is exactly the record set that reached PostGIS, using the same
exclusion helpers, so verify.py can prove the two stores agree.

OVERLAPS is computed with Shapely against geometry read back out of PostGIS:
PostGIS stays the spatial source of truth and Neo4j holds the derived fact.

    python -m src.storage.neo4j.load [--apply-schema] [--reset]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform

from ..postgres.load import (
    PROCESSED_DIR,
    PROJECT_ROOT,
    as_int,
    blank_to_none,
    connection_string,
    load_hard_exclusions,
    load_soft_index,
    read_csv_rows,
    read_features,
    sidecar_for,
)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.cypher"

# Equal-area-ish metric CRS for Ernakulam; used only to turn a degree-space
# intersection into a square-metre figure.
AREA_CRS = "EPSG:32643"
_TO_METRIC = Transformer.from_crs("EPSG:4326", AREA_CRS, always_xy=True).transform

# Kerala LSGs are not only panchayats: Ernakulam has 13 municipalities and the
# Kochi corporation, which are peers of panchayats, not subdivisions of them.
LEVEL_LABELS = {"district": "District", "taluk": "Taluk", "panchayat": "Panchayat",
                "ward": "Ward", "municipality": "Municipality",
                "corporation": "Corporation"}
ADMIN_LABELS = tuple(LEVEL_LABELS.values())

# label -> (identity property, PostGIS table it must agree with)
LABEL_KEYS = {
    "District": ("admin_id", "admin_boundary"),
    "Taluk": ("admin_id", "admin_boundary"),
    "Panchayat": ("admin_id", "admin_boundary"),
    "Ward": ("admin_id", "admin_boundary"),
    "Municipality": ("admin_id", "admin_boundary"),
    "Corporation": ("admin_id", "admin_boundary"),
    "Road": ("road_id", "road"),
    "WaterBody": ("waterbody_id", "water_body"),
    "FloodZone": ("flood_zone_id", "flood_zone"),
    "MetroStation": ("station_id", "metro_station"),
    "BusStop": ("stop_id", "bus_stop"),
}


def split_cypher(text: str) -> list[str]:
    """Split a .cypher file into executable statements.

    Comments are stripped *before* splitting on ";". Doing it the other way round
    breaks on a semicolon inside a // comment: the tail of the comment becomes its
    own statement and Neo4j tries to execute prose.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        lines.append(line.split("//", 1)[0] if "//" in line else line)
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def metric_area(geometry) -> float:
    """Area in square metres of a WGS84 shapely geometry."""
    return shapely_transform(_TO_METRIC, geometry).area


# node collection — mirrors src/storage/postgres/load.py exactly

def _vintage_props(properties: dict, source_file: str, record_id, soft_index, *extra_keys) -> dict:
    edition, file_name, confidence, flags = sidecar_for(record_id, source_file, soft_index, *extra_keys)
    return {
        "source": properties.get("source"),
        "source_date": blank_to_none(properties.get("source_date")),
        "data_year": as_int(properties.get("data_year")),
        "revision_status": blank_to_none(properties.get("revision_status")),
        "dataset_edition": edition,
        "source_file": file_name,
        "match_confidence": confidence,
        # Neo4j properties are scalars or arrays of scalars, never nested maps,
        # so the flag list travels as a JSON string.
        "soft_check_flags": flags,
    }


def collect_nodes(excluded, soft_index) -> dict[str, list[dict]]:
    nodes: dict[str, list[dict]] = {label: [] for label in LABEL_KEYS}

    for path in sorted((PROCESSED_DIR / "gis").glob("admin_boundaries*.geojson")):
        for feature in read_features(path):
            properties = feature.get("properties") or {}
            record_id = properties.get("admin_id")
            if (path.name, record_id) in excluded or not feature.get("geometry"):
                continue
            label = LEVEL_LABELS.get((properties.get("level") or "").lower())
            if not label:
                print(f"  warning: unknown level {properties.get('level')!r} for {record_id}", file=sys.stderr)
                continue
            nodes[label].append({
                "admin_id": record_id,
                "name": blank_to_none(properties.get("name")),
                "level": properties.get("level"),
                "parent_id": blank_to_none(properties.get("parent_id")),
                "boundary_vintage": properties.get("boundary_vintage"),
                **_vintage_props(properties, path.name, record_id, soft_index),
            })

    def geo_nodes(subdir, pattern, label, id_field, extra):
        for path in sorted((PROCESSED_DIR / subdir).glob(pattern)):
            for feature in read_features(path):
                properties = feature.get("properties") or {}
                record_id = properties.get(id_field)
                if (path.name, record_id) in excluded or not feature.get("geometry"):
                    continue
                nodes[label].append({
                    id_field: record_id,
                    **{out: blank_to_none(properties.get(src)) for out, src in extra.items()},
                    **_vintage_props(properties, path.name, record_id, soft_index),
                })

    geo_nodes("gis", "roads*.geojson", "Road", "road_id", {"name": "name", "road_type": "road_type"})
    geo_nodes("gis", "water_bodies*.geojson", "WaterBody", "waterbody_id",
              {"name": "name", "water_body_type": "type"})
    geo_nodes("environment", "flood_hazard*.geojson", "FloodZone", "flood_zone_id",
              {"risk_level": "risk_level"})

    def point_nodes(pattern, label, id_field, extra):
        for path in sorted((PROCESSED_DIR / "transportation").glob(pattern)):
            for row in read_csv_rows(path):
                record_id = row.get(id_field)
                if (path.name, record_id) in excluded:
                    continue
                if blank_to_none(row.get("lat")) is None or blank_to_none(row.get("lon")) is None:
                    continue
                nodes[label].append({
                    id_field: record_id,
                    **{out: blank_to_none(row.get(src)) for out, src in extra.items()},
                    "lat": float(row["lat"]), "lon": float(row["lon"]),
                    **_vintage_props(row, path.name, record_id, soft_index),
                })

    point_nodes("metro_stations*.csv", "MetroStation", "station_id",
                {"name": "name", "line": "line", "admin_id": "admin_id"})
    point_nodes("bus_stops*.csv", "BusStop", "stop_id",
                {"name": "name", "route_id": "route_id", "admin_id": "admin_id",
                 "feed_start_date": "feed_start_date", "feed_end_date": "feed_end_date"})
    return nodes


# relationships

def build_located_in(nodes) -> list[dict]:
    """Ward -> Panchayat -> Taluk -> District, resolved within one edition.

    A parent is only valid inside the same dataset_edition. Crossing editions
    would graft the messy hierarchy onto the demo one and quietly invent a
    topology neither source published.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for label in ADMIN_LABELS:
        for node in nodes[label]:
            by_key[(node["dataset_edition"], node["admin_id"])] = {**node, "label": label}

    edges, unresolved = [], []
    for node in by_key.values():
        parent_id = node.get("parent_id")
        if not parent_id:
            continue
        parent = by_key.get((node["dataset_edition"], parent_id))
        if not parent:
            unresolved.append((node["dataset_edition"], node["admin_id"], parent_id))
            continue
        edges.append({
            "child_label": node["label"], "child_id": node["admin_id"],
            "parent_label": parent["label"], "parent_id": parent["admin_id"],
            "dataset_edition": node["dataset_edition"],
            "boundary_vintage": node.get("boundary_vintage"),
        })

    for edition, child, parent in unresolved:
        print(f"  LOCATED_IN unresolved: {child} -> {parent} (edition {edition!r})", file=sys.stderr)
    return edges


def fetch_postgis_geometries(dsn) -> dict[str, list[dict]]:
    """Read flood zone and admin boundary geometries back out of PostGIS."""
    import psycopg2

    out = {"flood_zone": [], "admin_boundary": []}
    with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT flood_zone_id, dataset_edition, data_year, risk_level, ST_AsGeoJSON(geom) FROM flood_zone"
        )
        for zone_id, edition, year, risk, geojson in cursor.fetchall():
            out["flood_zone"].append({"id": zone_id, "dataset_edition": edition, "data_year": year,
                                      "risk_level": risk, "geom": shape(json.loads(geojson))})
        cursor.execute(
            "SELECT admin_id, level, dataset_edition, boundary_vintage, ST_AsGeoJSON(geom) FROM admin_boundary"
        )
        for admin_id, level, edition, vintage, geojson in cursor.fetchall():
            out["admin_boundary"].append({"id": admin_id, "level": level, "dataset_edition": edition,
                                          "boundary_vintage": vintage, "geom": shape(json.loads(geojson))})
    return out


def build_overlaps(geometries) -> list[dict]:
    """FloodZone -> admin entity, wherever the two geometries actually intersect.

    Both the absolute overlap and the share of the admin unit affected are
    written: "3.2 km2 of this ward floods" and "that is 41% of the ward" are
    different findings, and an agent weighing a route needs the second one.
    """
    edges = []
    for zone in geometries["flood_zone"]:
        for boundary in geometries["admin_boundary"]:
            if zone["dataset_edition"] != boundary["dataset_edition"]:
                continue  # editions are separate assertions; never cross them
            if not zone["geom"].intersects(boundary["geom"]):
                continue
            intersection = zone["geom"].intersection(boundary["geom"])
            if intersection.is_empty:
                continue
            overlap_area = metric_area(intersection)
            if overlap_area <= 0:
                continue  # touching edges only
            boundary_area = metric_area(boundary["geom"])
            edges.append({
                "flood_zone_id": zone["id"],
                "admin_id": boundary["id"],
                "admin_label": LEVEL_LABELS.get((boundary["level"] or "").lower(), "Ward"),
                "dataset_edition": zone["dataset_edition"],
                "overlap_area_m2": round(overlap_area, 2),
                "overlap_ratio_of_admin": round(overlap_area / boundary_area, 6) if boundary_area else None,
                "flood_data_year": zone["data_year"],
                "risk_level": zone["risk_level"],
                "boundary_vintage": boundary["boundary_vintage"],
            })
    return edges


# writing

def write_nodes(session, label, rows) -> int:
    key = LABEL_KEYS[label][0]
    query = (
        f"UNWIND $rows AS row "
        f"MERGE (n:{label} {{{key}: row.{key}, dataset_edition: row.dataset_edition}}) "
        f"SET n += row "
        f"RETURN count(n) AS n"
    )
    return session.run(query, rows=rows).single()["n"]


def write_located_in(session, edges) -> int:
    written = 0
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for edge in edges:
        by_pair.setdefault((edge["child_label"], edge["parent_label"]), []).append(edge)
    for (child_label, parent_label), rows in by_pair.items():
        query = (
            f"UNWIND $rows AS row "
            f"MATCH (c:{child_label} {{admin_id: row.child_id, dataset_edition: row.dataset_edition}}) "
            f"MATCH (p:{parent_label} {{admin_id: row.parent_id, dataset_edition: row.dataset_edition}}) "
            f"MERGE (c)-[r:LOCATED_IN]->(p) "
            f"SET r.boundary_vintage = row.boundary_vintage, r.dataset_edition = row.dataset_edition "
            f"RETURN count(r) AS n"
        )
        written += session.run(query, rows=rows).single()["n"]
    return written


def write_overlaps(session, edges) -> int:
    written = 0
    by_label: dict[str, list[dict]] = {}
    for edge in edges:
        by_label.setdefault(edge["admin_label"], []).append(edge)
    for label, rows in by_label.items():
        query = (
            f"UNWIND $rows AS row "
            f"MATCH (f:FloodZone {{flood_zone_id: row.flood_zone_id, dataset_edition: row.dataset_edition}}) "
            f"MATCH (a:{label} {{admin_id: row.admin_id, dataset_edition: row.dataset_edition}}) "
            f"MERGE (f)-[r:OVERLAPS]->(a) "
            f"SET r.overlap_area_m2 = row.overlap_area_m2, "
            f"    r.overlap_ratio_of_admin = row.overlap_ratio_of_admin, "
            f"    r.flood_data_year = row.flood_data_year, "
            f"    r.risk_level = row.risk_level, "
            f"    r.boundary_vintage = row.boundary_vintage, "
            f"    r.dataset_edition = row.dataset_edition "
            f"RETURN count(r) AS n"
        )
        written += session.run(query, rows=rows).single()["n"]
    return written


def neo4j_settings() -> tuple[str, str, str]:
    load_dotenv(PROJECT_ROOT / ".env")
    return (
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        os.getenv("NEO4J_USER", "neo4j"),
        os.getenv("NEO4J_PASSWORD", "password"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="load", description="Load the Ernakulam knowledge graph into the local Neo4j instance.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report node and relationship counts without writing to Neo4j.")
    parser.add_argument("--apply-schema", action="store_true",
                        help="Run schema.cypher (constraints and indexes) before loading.")
    parser.add_argument("--reset", action="store_true",
                        help="Delete all existing nodes and relationships first.")
    args = parser.parse_args(argv)

    print("Reading Stage 2 findings")
    excluded = load_hard_exclusions()
    soft_index = load_soft_index()
    print(f"  {len(excluded)} record(s) excluded by hard checks\n")

    nodes = collect_nodes(excluded, soft_index)
    located_in = build_located_in(nodes)

    print(f"{'label':<16} {'nodes':>6}")
    print("-" * 24)
    for label, rows in nodes.items():
        print(f"{label:<16} {len(rows):>6}")
    total_nodes = sum(len(rows) for rows in nodes.values())
    print("-" * 24)
    print(f"{'TOTAL':<16} {total_nodes:>6}")

    overlaps: list[dict] = []
    if args.dry_run:
        print(f"\nLOCATED_IN edges: {len(located_in)}")
        print("OVERLAPS edges  : requires PostGIS geometries; skipped under --dry-run")
        print("\n--dry-run: nothing written, no database connection opened.")
        return 0

    print("\nComputing OVERLAPS from PostGIS geometries with Shapely")
    geometries = fetch_postgis_geometries(connection_string())
    overlaps = build_overlaps(geometries)
    print(f"  {len(geometries['flood_zone'])} flood zone(s) x "
          f"{len(geometries['admin_boundary'])} boundary(ies) -> {len(overlaps)} intersection(s)")

    from neo4j import GraphDatabase

    uri, user, password = neo4j_settings()
    print(f"\nConnecting to {uri}")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            if args.reset:
                session.run("MATCH (n) DETACH DELETE n")
                print("  reset: existing graph deleted")
            if args.apply_schema:
                statements = split_cypher(SCHEMA_PATH.read_text(encoding="utf-8"))
                for statement in statements:
                    session.run(statement)
                print(f"  applied {SCHEMA_PATH.name} ({len(statements)} statement(s))")

            for label, rows in nodes.items():
                if rows:
                    print(f"  {label}: {write_nodes(session, label, rows)} node(s)")
            print(f"  LOCATED_IN: {write_located_in(session, located_in)} relationship(s)")
            print(f"  OVERLAPS: {write_overlaps(session, overlaps)} relationship(s)")
    finally:
        driver.close()

    print(f"\nWrote {total_nodes} node(s), {len(located_in) + len(overlaps)} relationship(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
