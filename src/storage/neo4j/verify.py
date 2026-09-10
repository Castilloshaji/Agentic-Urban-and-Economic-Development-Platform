"""Hard check — PostGIS and Neo4j must hold the same entities.

Exits non-zero on any drift. Counts are compared per (label, dataset_edition),
not in total, so two errors cancelling across editions cannot pass.

    python -m src.storage.neo4j.verify [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime

from ..postgres.load import PROCESSED_DIR, connection_string
from .load import LABEL_KEYS, LEVEL_LABELS, build_overlaps, fetch_postgis_geometries, neo4j_settings

REPORT_PATH = PROCESSED_DIR / "entity_count_agreement.json"

# admin_boundary maps onto four labels, so it is compared by level, not by table.
ADMIN_TABLE = "admin_boundary"


def postgis_counts(dsn) -> dict[tuple[str, str], int]:
    """Expected node counts keyed by (label, dataset_edition)."""
    import psycopg2

    counts: dict[tuple[str, str], int] = defaultdict(int)
    with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT level, dataset_edition, count(*) FROM {ADMIN_TABLE} GROUP BY 1, 2")
        for level, edition, count in cursor.fetchall():
            label = LEVEL_LABELS.get((level or "").lower())
            if label:
                counts[(label, edition)] += count
            else:
                # A level with no node label is invisible to a per-label
                # comparison, so it would otherwise pass by not being looked at.
                # Count it under a sentinel so the drift is impossible to miss.
                counts[(f"<unmapped level: {level}>", edition)] += count

        for label, (_key, table) in LABEL_KEYS.items():
            if table == ADMIN_TABLE:
                continue
            cursor.execute(f"SELECT dataset_edition, count(*) FROM {table} GROUP BY 1")
            for edition, count in cursor.fetchall():
                counts[(label, edition)] += count
    return dict(counts)


def neo4j_counts(driver) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    with driver.session() as session:
        for label in LABEL_KEYS:
            query = (f"MATCH (n:{label}) RETURN coalesce(n.dataset_edition, '') AS edition, "
                     f"count(n) AS n")
            for record in session.run(query):
                counts[(label, record["edition"])] = record["n"]
    return counts


def neo4j_relationship_counts(driver) -> dict[str, int]:
    with driver.session() as session:
        return {
            "LOCATED_IN": session.run("MATCH ()-[r:LOCATED_IN]->() RETURN count(r) AS n").single()["n"],
            "OVERLAPS": session.run("MATCH ()-[r:OVERLAPS]->() RETURN count(r) AS n").single()["n"],
        }


def expected_located_in(dsn) -> int:
    """Parent references that resolve inside one dataset_edition, per PostGIS."""
    import psycopg2

    with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM admin_boundary child "
            "JOIN admin_boundary parent "
            "  ON parent.admin_id = child.parent_id "
            " AND parent.dataset_edition = child.dataset_edition "
            "WHERE child.parent_id IS NOT NULL"
        )
        return cursor.fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify", description="Hard check: PostGIS and Neo4j must hold the same entities.")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    args = parser.parse_args(argv)

    from neo4j import GraphDatabase

    dsn = connection_string()
    uri, user, password = neo4j_settings()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        pg = postgis_counts(dsn)
        neo = neo4j_counts(driver)
        relationships = neo4j_relationship_counts(driver)
    finally:
        driver.close()

    rows = []
    for key in sorted(set(pg) | set(neo)):
        label, edition = key
        pg_count, neo_count = pg.get(key, 0), neo.get(key, 0)
        rows.append({
            "label": label, "dataset_edition": edition,
            "postgis": pg_count, "neo4j": neo_count, "drift": neo_count - pg_count,
        })

    expected_edges = expected_located_in(dsn)
    geometries = fetch_postgis_geometries(dsn)
    expected_overlaps = len(build_overlaps(geometries))
    edge_rows = [
        {"relationship": "LOCATED_IN", "expected_from_postgis": expected_edges,
         "neo4j": relationships["LOCATED_IN"], "drift": relationships["LOCATED_IN"] - expected_edges},
        {"relationship": "OVERLAPS", "expected_from_postgis": expected_overlaps,
         "neo4j": relationships["OVERLAPS"], "drift": relationships["OVERLAPS"] - expected_overlaps},
    ]

    diverged = [r for r in rows if r["drift"] != 0] + [r for r in edge_rows if r["drift"] != 0]
    report = {
        "check": "entity_count_agreement",
        "rule": "postgis_and_neo4j_hold_the_same_entity_counts",
        "severity": "hard",
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": "fail" if diverged else "pass",
        "entities": rows,
        "relationships": edge_rows,
        "divergences": diverged,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{'label':<16} {'edition':<8} {'postgis':>8} {'neo4j':>7} {'drift':>6}")
        print("-" * 48)
        for row in rows:
            marker = "" if row["drift"] == 0 else "  <-- DRIFT"
            print(f"{row['label']:<16} {row['dataset_edition']:<8} {row['postgis']:>8} "
                  f"{row['neo4j']:>7} {row['drift']:>6}{marker}")
        print("-" * 48)
        for row in edge_rows:
            marker = "" if row["drift"] == 0 else "  <-- DRIFT"
            print(f"{row['relationship']:<16} {'':<8} {row['expected_from_postgis']:>8} "
                  f"{row['neo4j']:>7} {row['drift']:>6}{marker}")
        print(f"\nReport: {REPORT_PATH.relative_to(PROCESSED_DIR.parent.parent)}")

    if diverged:
        print(f"\nHARD CHECK FAILED: {len(diverged)} divergence(s) between PostGIS and Neo4j.", file=sys.stderr)
        print("The two stores disagree about which entities exist. Reload before "
              "building the digital twin layer on top of them.", file=sys.stderr)
        return 1

    print("\nHARD CHECK PASSED: zero drift between PostGIS and Neo4j.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
