"""Stage 3 — load data/processed/ into PostGIS.

Hard-check failures never load, scoped to (source_file, record_id) so a broken
record in one edition does not suppress a good copy of the same entity in
another. Soft-flagged records load unchanged, with their confidence and flags in
sidecar columns.

    python -m src.storage.postgres.load [--dry-run] [--apply-schema]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

HARD_REPORT = PROCESSED_DIR / "hard_check_failures.json"
SOFT_REPORT = PROCESSED_DIR / "soft_check_flags.json"

# Worst confidence wins when several soft flags hit one row.
CONFIDENCE_RANK = {"exact": 0, "crosswalked": 1, "unmatched-estimate": 2}

SIDECAR_COLUMNS = ("dataset_edition", "source_file", "match_confidence", "soft_check_flags")

# The columns each table's UNIQUE constraint covers. Rows colliding on these are
# discarded by ON CONFLICT DO NOTHING, so they are detected and reported before
# the insert — a source file with two records claiming the same identity is a
# data defect, not something to resolve by silently keeping whichever came first.
NATURAL_KEYS = {
    "admin_boundary": ("admin_id", "boundary_vintage", "dataset_edition"),
    "economic_indicator": ("admin_id", "indicator", "year", "revision_status", "dataset_edition"),
    "road": ("road_id", "dataset_edition"),
    "water_body": ("waterbody_id", "dataset_edition"),
    "flood_zone": ("flood_zone_id", "dataset_edition"),
    "population_taluk": ("admin_id", "data_year", "revision_status", "boundary_vintage", "dataset_edition"),
    "population_panchayat": ("admin_id", "data_year", "revision_status", "boundary_vintage", "dataset_edition"),
    "population_legacy_ward": ("legacy_ward_id", "data_year", "revision_status", "dataset_edition"),
    "metro_station": ("station_id", "dataset_edition"),
    "bus_stop": ("stop_id", "dataset_edition"),
}


def dataset_edition(filename: str) -> str:
    """Trailing filename token identifying which edition of a dataset a file is.

    Mirrors src/processing/consistency_checks.dataset_variant so that a row's
    edition in the database matches the edition its soft flags were computed in.
    """
    stem = Path(filename).stem
    tail = stem.rsplit("_", 1)[-1] if "_" in stem else ""
    return tail if tail.isalpha() else ""


# Stage 2 findings

def load_hard_exclusions() -> set[tuple[str, str]]:
    """(source_file, record_id) pairs that must not be inserted."""
    if not HARD_REPORT.exists():
        print(f"  warning: {HARD_REPORT.name} not found — nothing will be excluded", file=sys.stderr)
        return set()
    report = json.loads(HARD_REPORT.read_text(encoding="utf-8"))
    return {
        (failure.get("source_file") or "", failure.get("record_id") or "")
        for failure in report.get("failures", [])
        if failure.get("record_id")
    }


def load_soft_index() -> dict[tuple[str, str], list[dict]]:
    """Index soft flags by (dataset_edition, record_id).

    Different checks name their subject differently — a list of ids for a
    near-duplicate pair, a composite key for a revision conflict, a parent_id for
    a population sum — so each is unpacked into the ids it actually implicates.
    """
    if not SOFT_REPORT.exists():
        print(f"  warning: {SOFT_REPORT.name} not found — no confidence will be recorded", file=sys.stderr)
        return {}

    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for flag in json.loads(SOFT_REPORT.read_text(encoding="utf-8")).get("flags", []):
        edition = flag.get("dataset") or dataset_edition(flag.get("source_file") or flag.get("entity_type") or "")

        record_ids: list[str] = []
        if flag.get("ids"):
            record_ids += [i for i in flag["ids"] if i]
        if flag.get("record_id"):
            record_ids.append(flag["record_id"])
        if flag.get("parent_id"):
            record_ids.append(flag["parent_id"])
        if flag.get("child_ids"):
            record_ids += [i for i in flag["child_ids"] if i]
        if isinstance(flag.get("key"), dict):
            # A revision conflict is scoped to a composite key (admin_id +
            # indicator + year, say). Indexing it under the bare admin_id would
            # smear the flag across every unrelated row for that entity.
            record_ids.append(composite_key(flag["key"].values()))

        summary = {
            "check": flag.get("check"),
            "match_confidence": flag.get("match_confidence"),
            "detail": flag.get("detail"),
        }
        for record_id in dict.fromkeys(record_ids):
            index[(edition, record_id)].append(summary)
    return index


def composite_key(values) -> str:
    """Stable string form of a multi-column key, for soft-flag lookup."""
    return "|".join("" if v is None else str(v).strip() for v in values)


def sidecar_for(record_id: str | None, source_file: str, soft_index, *extra_keys) -> tuple[str, str, str, str]:
    """Build the four sidecar column values for one row.

    extra_keys lets a table look itself up under a composite key as well as its
    bare id, so a flag scoped to (admin_id, indicator, year) lands only on the
    rows it actually describes.
    """
    edition = dataset_edition(source_file)
    flags: list[dict] = []
    for lookup in (record_id or "", *extra_keys):
        for flag in soft_index.get((edition, lookup), []):
            if flag not in flags:
                flags.append(flag)
    confidence = "exact"
    for flag in flags:
        level = flag.get("match_confidence") or "exact"
        if CONFIDENCE_RANK.get(level, 0) > CONFIDENCE_RANK[confidence]:
            confidence = level
    return edition, source_file, confidence, json.dumps(flags)


# readers

# Every processed file a builder opens is recorded here, so anything left over
# can be reported instead of silently ignored.
CONSUMED_FILES: set[Path] = set()


def read_csv_rows(path: Path) -> list[dict]:
    CONSUMED_FILES.add(path.resolve())
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_features(path: Path) -> list[dict]:
    CONSUMED_FILES.add(path.resolve())
    return json.loads(path.read_text(encoding="utf-8")).get("features", [])


def blank_to_none(value):
    """Empty cell -> SQL NULL. Never 0, never ''."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_int(value):
    text = blank_to_none(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def as_num(value):
    text = blank_to_none(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# table builders — each returns (rows, columns, value_sql)

GEOM_MULTIPOLY = "ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
GEOM_MULTILINE = "ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
GEOM_POINT = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"


def build_admin_boundary(excluded, soft_index):
    columns = ("admin_id", "level", "parent_id", "name", "geom", "source", "source_date",
               "data_year", "revision_status", "boundary_vintage", *SIDECAR_COLUMNS)
    placeholders = f"%s, %s, %s, %s, {GEOM_MULTIPOLY}, %s, %s, %s, %s, %s, %s, %s, %s, %s"
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / "gis").glob("admin_boundaries*.geojson")):
        for feature in read_features(path):
            properties = feature.get("properties") or {}
            record_id = properties.get("admin_id")
            if (path.name, record_id) in excluded:
                skipped.append(record_id)
                continue
            if not feature.get("geometry"):
                skipped.append(record_id)  # defensive: hard check should have caught it
                continue
            rows.append((
                record_id, properties.get("level"), blank_to_none(properties.get("parent_id")),
                blank_to_none(properties.get("name")), json.dumps(feature["geometry"]),
                properties.get("source"), blank_to_none(properties.get("source_date")),
                as_int(properties.get("data_year")), properties.get("revision_status"),
                properties.get("boundary_vintage"),
                *sidecar_for(record_id, path.name, soft_index),
            ))
    return rows, columns, placeholders, skipped


def build_simple_geojson(subdir, pattern, id_field, extra_fields, geom_sql, columns_head, excluded, soft_index):
    """Shared builder for road / water_body / flood_zone."""
    columns = (*columns_head, "geom", "source", "source_date", "data_year", "revision_status", *SIDECAR_COLUMNS)
    head_placeholders = ", ".join(["%s"] * len(columns_head))
    placeholders = f"{head_placeholders}, {geom_sql}, %s, %s, %s, %s, %s, %s, %s, %s"
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / subdir).glob(pattern)):
        for feature in read_features(path):
            properties = feature.get("properties") or {}
            record_id = properties.get(id_field)
            if (path.name, record_id) in excluded or not feature.get("geometry"):
                skipped.append(record_id)
                continue
            rows.append((
                record_id,
                *[blank_to_none(properties.get(field)) for field in extra_fields],
                json.dumps(feature["geometry"]),
                properties.get("source"), blank_to_none(properties.get("source_date")),
                as_int(properties.get("data_year")), properties.get("revision_status"),
                *sidecar_for(record_id, path.name, soft_index),
            ))
    return rows, columns, placeholders, skipped


def build_economic_indicator(excluded, soft_index):
    columns = ("admin_id", "indicator", "value", "value_unit", "unit", "year",
               "source", "source_date", "revision_status", *SIDECAR_COLUMNS)
    placeholders = ", ".join(["%s"] * len(columns))
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / "economy").glob("*.csv")):
        for row in read_csv_rows(path):
            record_id = row.get("admin_id")
            if (path.name, record_id) in excluded:
                skipped.append(record_id)
                continue
            rows.append((
                record_id, row.get("indicator"), as_num(row.get("value")),
                blank_to_none(row.get("value_unit")), blank_to_none(row.get("unit")),
                as_int(row.get("year")), row.get("source"), blank_to_none(row.get("source_date")),
                row.get("revision_status"),
                *sidecar_for(record_id, path.name, soft_index,
                             composite_key([record_id, row.get("indicator"), row.get("year")])),
            ))
    return rows, columns, placeholders, skipped


def build_population_panchayat(excluded, soft_index):
    columns = ("admin_id", "population", "male_population", "female_population", "literacy_rate",
               "households", "source", "source_date", "data_year", "revision_status",
               "boundary_vintage", *SIDECAR_COLUMNS)
    placeholders = ", ".join(["%s"] * len(columns))
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / "population").glob("population_panchayat*.csv")):
        for row in read_csv_rows(path):
            record_id = row.get("admin_id")
            if (path.name, record_id) in excluded:
                skipped.append(record_id)
                continue
            rows.append((
                record_id, as_int(row.get("population")), as_int(row.get("male_population")),
                as_int(row.get("female_population")), as_num(row.get("literacy_rate")),
                as_int(row.get("households")), row.get("source"), blank_to_none(row.get("source_date")),
                as_int(row.get("data_year")), row.get("revision_status"), row.get("boundary_vintage"),
                *sidecar_for(record_id, path.name, soft_index,
                             composite_key([record_id, row.get("data_year")])),
            ))
    return rows, columns, placeholders, skipped


def build_admin_code_xref(excluded, soft_index):
    """Load Stage 2's crosswalk, matched and unmatched rows alike."""
    columns = ("normalized_name", "match_status", "match_confidence", "match_method",
               "canonical_admin_id", "canonical_name", "canonical_level",
               "alt_id", "alt_name_as_published", "alt_level", "alt_source_file",
               "dataset_edition")
    placeholders = ", ".join(["%s"] * len(columns))
    rows, skipped = [], []
    path = PROCESSED_DIR / "admin_code_xref.csv"
    if path.exists():
        for row in read_csv_rows(path):
            rows.append((
                row.get("normalized_name"), row.get("match_status"),
                row.get("match_confidence") or "exact", row.get("match_method"),
                blank_to_none(row.get("canonical_admin_id")),
                blank_to_none(row.get("canonical_name")),
                blank_to_none(row.get("canonical_level")),
                blank_to_none(row.get("alt_id")),
                blank_to_none(row.get("alt_name_as_published")),
                blank_to_none(row.get("alt_level")),
                blank_to_none(row.get("alt_source_file")),
                dataset_edition(row.get("alt_source_file") or ""),
            ))
    return rows, columns, placeholders, skipped


def build_population_taluk(excluded, soft_index):
    columns = ("admin_id", "population", "area_km2", "density", "sex_ratio", "households",
               "literacy_rate", "source", "source_date", "data_year", "revision_status",
               "boundary_vintage", *SIDECAR_COLUMNS)
    placeholders = ", ".join(["%s"] * len(columns))
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / "population").glob("population_taluk*.csv")):
        for row in read_csv_rows(path):
            record_id = row.get("admin_id")
            if (path.name, record_id) in excluded:
                skipped.append(record_id)
                continue
            rows.append((
                record_id, as_int(row.get("population")), as_num(row.get("area_km2")),
                as_int(row.get("density")), as_int(row.get("sex_ratio")),
                as_int(row.get("households")), as_num(row.get("literacy_rate")),
                row.get("source"), blank_to_none(row.get("source_date")),
                as_int(row.get("data_year")), row.get("revision_status"),
                row.get("boundary_vintage"),
                *sidecar_for(record_id, path.name, soft_index,
                             composite_key([record_id, row.get("data_year")])),
            ))
    return rows, columns, placeholders, skipped


def build_population_legacy_ward(excluded, soft_index):
    columns = ("legacy_ward_id", "panchayat_id", "population", "note", "source", "source_date",
               "data_year", "revision_status", "boundary_vintage", *SIDECAR_COLUMNS)
    placeholders = ", ".join(["%s"] * len(columns))
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / "population").glob("population_ward_legacy*.csv")):
        for row in read_csv_rows(path):
            record_id = row.get("legacy_ward_id")
            if (path.name, record_id) in excluded:
                skipped.append(record_id)
                continue
            rows.append((
                record_id, blank_to_none(row.get("panchayat_id")), as_int(row.get("population")),
                blank_to_none(row.get("note")), row.get("source"), blank_to_none(row.get("source_date")),
                as_int(row.get("data_year")), row.get("revision_status"), row.get("boundary_vintage"),
                *sidecar_for(record_id, path.name, soft_index),
            ))
    return rows, columns, placeholders, skipped


def build_point_table(pattern, id_field, extra_fields, columns_head, excluded, soft_index, feed_dates=False):
    """Shared builder for metro_station / bus_stop."""
    tail = ("feed_start_date", "feed_end_date") if feed_dates else ()
    columns = (*columns_head, "geom", *tail, "source", "source_date", "data_year",
               "revision_status", *SIDECAR_COLUMNS)
    head_placeholders = ", ".join(["%s"] * len(columns_head))
    tail_placeholders = ", %s, %s" if feed_dates else ""
    placeholders = f"{head_placeholders}, {GEOM_POINT}{tail_placeholders}, %s, %s, %s, %s, %s, %s, %s, %s"
    rows, skipped = [], []
    for path in sorted((PROCESSED_DIR / "transportation").glob(pattern)):
        for row in read_csv_rows(path):
            record_id = row.get(id_field)
            lat, lon = as_num(row.get("lat")), as_num(row.get("lon"))
            if (path.name, record_id) in excluded or lat is None or lon is None:
                skipped.append(record_id)
                continue
            feed = (blank_to_none(row.get("feed_start_date")), blank_to_none(row.get("feed_end_date"))) if feed_dates else ()
            rows.append((
                record_id,
                *[blank_to_none(row.get(field)) for field in extra_fields],
                lon, lat,  # ST_MakePoint takes x then y
                *feed,
                row.get("source"), blank_to_none(row.get("source_date")),
                as_int(row.get("data_year")), row.get("revision_status"),
                *sidecar_for(record_id, path.name, soft_index),
            ))
    return rows, columns, placeholders, skipped


def build_all(excluded, soft_index) -> dict:
    return {
        "admin_boundary": build_admin_boundary(excluded, soft_index),
        "admin_code_xref": build_admin_code_xref(excluded, soft_index),
        "economic_indicator": build_economic_indicator(excluded, soft_index),
        "road": build_simple_geojson("gis", "roads*.geojson", "road_id", ("name", "road_type"),
                                     GEOM_MULTILINE, ("road_id", "name", "road_type"), excluded, soft_index),
        "water_body": build_simple_geojson("gis", "water_bodies*.geojson", "waterbody_id", ("name", "type"),
                                           GEOM_MULTIPOLY, ("waterbody_id", "name", "water_body_type"),
                                           excluded, soft_index),
        "flood_zone": build_simple_geojson("environment", "flood_hazard*.geojson", "flood_zone_id",
                                           ("risk_level",), GEOM_MULTIPOLY,
                                           ("flood_zone_id", "risk_level"), excluded, soft_index),
        "population_taluk": build_population_taluk(excluded, soft_index),
        "population_panchayat": build_population_panchayat(excluded, soft_index),
        "population_legacy_ward": build_population_legacy_ward(excluded, soft_index),
        "metro_station": build_point_table("metro_stations*.csv", "station_id", ("name", "line", "admin_id"),
                                           ("station_id", "name", "line", "admin_id"), excluded, soft_index),
        "bus_stop": build_point_table("bus_stops*.csv", "stop_id", ("name", "route_id", "admin_id"),
                                      ("stop_id", "name", "route_id", "admin_id"), excluded, soft_index,
                                      feed_dates=True),
    }


# driver

def _row_index_map(columns: tuple, row_length: int) -> dict[str, int]:
    """Map column name -> position in the row tuple.

    A geometry column is fed by more placeholders than it has columns
    (ST_MakePoint takes lon and lat), so every column after `geom` sits further
    along the row than its position in `columns` suggests.
    """
    extra = row_length - len(columns)
    geom_position = columns.index("geom") if "geom" in columns else None
    return {
        column: (index + extra if geom_position is not None and index > geom_position else index)
        for index, column in enumerate(columns)
    }


def report_key_collisions(tables: dict) -> dict[str, list[dict]]:
    """Rows that would be silently discarded by the natural-key conflict clause."""
    collisions: dict[str, list[dict]] = {}
    for name, (rows, columns, _placeholders, _skipped) in tables.items():
        key_columns = NATURAL_KEYS.get(name)
        if not key_columns or not rows:
            continue
        index_map = _row_index_map(columns, len(rows[0]))
        if any(c not in index_map for c in key_columns):
            continue
        seen: set[tuple] = set()
        found: list[dict] = []
        for row in rows:
            key = tuple(row[index_map[c]] for c in key_columns)
            if key in seen:
                found.append({"key": dict(zip(key_columns, key))})
            seen.add(key)
        if found:
            collisions[name] = found
    return collisions


def report_unconsumed() -> list[Path]:
    """Processed data files that no table builder read.

    A dataset can arrive with a level the schema does not model — real Census
    taluk figures did exactly that — and without this the file cleans
    successfully, loads nothing, and warns no one. Silence is the failure mode
    worth engineering against.
    """
    # Files that are deliberately not tables: Stage 2 reads them to build
    # something else, and loading the raw form too would duplicate it.
    intentionally_not_loaded = ("admin_boundaries_alt_scheme", "source_manifest")
    candidates = {
        path.resolve()
        for pattern in ("*.csv", "*.geojson")
        for path in PROCESSED_DIR.rglob(pattern)
        # Top-level files are Stage 2's own reports, not source data.
        if path.parent != PROCESSED_DIR
        and not any(marker in path.name for marker in intentionally_not_loaded)
    }
    return sorted(candidates - CONSUMED_FILES)


def connection_string() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "ernakulam")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="load",
        description="Load data/processed/ into PostGIS, honouring Stage 2's hard and soft checks.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report per-table row counts without connecting to or writing to the database.")
    parser.add_argument("--apply-schema", action="store_true",
                        help="Run schema.sql before loading (drops and recreates every table).")
    args = parser.parse_args(argv)

    print("Reading Stage 2 findings")
    excluded = load_hard_exclusions()
    soft_index = load_soft_index()
    print(f"  {len(excluded)} record(s) excluded by hard checks")
    print(f"  {len(soft_index)} record(s) carry soft flags\n")

    tables = build_all(excluded, soft_index)

    total_rows = total_flagged = total_uncertain = total_skipped = 0
    # The sidecar values are always the last four in a row tuple. Indexing by
    # column position would be wrong for the point tables, where one geom column
    # is fed by two placeholders (lon, lat).
    print(f"{'table':<24} {'rows':>6} {'flagged':>8} {'uncertain':>10} {'skipped':>8}")
    print("-" * 62)
    for name, (rows, _columns, _placeholders, skipped) in tables.items():
        flagged = sum(1 for row in rows if row[-1] != "[]")
        uncertain = sum(1 for row in rows if row[-2] != "exact")
        total_rows += len(rows)
        total_flagged += flagged
        total_uncertain += uncertain
        total_skipped += len(skipped)
        print(f"{name:<24} {len(rows):>6} {flagged:>8} {uncertain:>10} {len(skipped):>8}")
    print("-" * 62)
    print(f"{'TOTAL':<24} {total_rows:>6} {total_flagged:>8} {total_uncertain:>10} {total_skipped:>8}")
    print("\n  flagged   = rows carrying at least one soft-check flag")
    print("  uncertain = rows whose match_confidence is worse than 'exact'")

    collisions = report_key_collisions(tables)
    if collisions:
        total = sum(len(v) for v in collisions.values())
        print(f"\nWARNING: {total} row(s) collide on their natural key and will be "
              f"DISCARDED by the conflict clause:", file=sys.stderr)
        for table, items in collisions.items():
            for item in items[:5]:
                print(f"  - {table}: {item['key']}", file=sys.stderr)
        print("  Two source records claim the same identity — fix the source ids.",
              file=sys.stderr)

    unconsumed = report_unconsumed()
    if unconsumed:
        print(f"\nWARNING: {len(unconsumed)} processed file(s) matched no table builder "
              f"and were NOT loaded:", file=sys.stderr)
        for path in unconsumed:
            print(f"  - {path.relative_to(PROJECT_ROOT)}", file=sys.stderr)
        print("  Add a builder for these, or they are silently absent from the twin.",
              file=sys.stderr)

    if excluded:
        print("\nHard-check exclusions (never loaded):")
        for source_file, record_id in sorted(excluded):
            print(f"  {record_id}  ({source_file})")

    if args.dry_run:
        print("\n--dry-run: nothing inserted, no database connection opened.")
        return 0

    try:
        import psycopg2
    except ImportError:
        print("\npsycopg2 is not installed — run: pip install -r requirements.txt", file=sys.stderr)
        return 1

    dsn = connection_string()
    print(f"\nConnecting to {dsn.rsplit('@', 1)[-1]}")
    with psycopg2.connect(dsn) as connection:
        with connection.cursor() as cursor:
            if args.apply_schema:
                print(f"  applying {SCHEMA_PATH.name}")
                cursor.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

            for name, (rows, columns, placeholders, _skipped) in tables.items():
                if name == "admin_code_xref" and rows:
                    cursor.execute("TRUNCATE admin_code_xref")
                if not rows:
                    print(f"  {name}: nothing to load")
                    continue
                conflict = ("" if name == "admin_code_xref"
                            else f" ON CONFLICT ON CONSTRAINT {name}_natural_key DO NOTHING")
                statement = (
                    f"INSERT INTO {name} ({', '.join(columns)}) VALUES ({placeholders}){conflict}"
                )
                cursor.executemany(statement, rows)
                print(f"  {name}: {cursor.rowcount if cursor.rowcount != -1 else len(rows)} row(s) inserted")
        connection.commit()

    print(f"\nLoaded {total_rows} row(s) at {datetime.now().astimezone().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
