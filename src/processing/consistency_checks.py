"""Stage 2 step 4 — hard and soft consistency checks.

A HARD failure halts loading for that record only, never the file or the run.
A SOFT flag never halts anything; it carries a match_confidence of exact /
crosswalked / unmatched-estimate that travels with the row into storage.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from shapely.geometry import shape
from shapely.validation import explain_validity

from .paths import PROCESSED_DIR, write_json
from .reconcile_ids import normalize_name

HARD_PATH = PROCESSED_DIR / "hard_check_failures.json"
SOFT_PATH = PROCESSED_DIR / "soft_check_flags.json"

# Threshold applies to the *core* name — after abbreviation expansion and after
# generic type words ("road", "junction", "bus stop") are stripped. Stripping
# shared filler removes similarity mass that used to prop scores up, so this sits
# lower than a raw whole-string threshold would.
#
# Calibrated on real Ernakulam name pairs: every genuine same-place pair scores
# >= 0.727 while the closest genuinely-different pair scores 0.455, so 0.70 sits
# in a wide empty band rather than balanced on an edge. Directional blocking and
# the <100m distance gate are the two independent guards that make a threshold
# this permissive safe.
NAME_SIMILARITY_THRESHOLD = 0.70

# Written-out forms of abbreviations that appear in one source but not another.
ABBREVIATIONS = {
    "rd": "road", "st": "street", "jn": "junction", "jct": "junction",
    "stn": "station", "bldg": "building", "hosp": "hospital", "mkt": "market",
    "gnd": "ground", "clg": "college", "sch": "school", "brdg": "bridge",
    "nr": "near", "opp": "opposite", "jnc": "junction",
}

# Words that describe what a thing IS rather than which one it is. Two sources
# routinely disagree about whether to include them ("Thoppumpady Road" vs
# "Thoppumpady Junction Road"), so they are removed before comparison.
GENERIC_TOKENS = {
    "road", "street", "junction", "bus", "stop", "stand", "terminal", "station",
    "bridge", "bypass", "highway", "lane", "cross", "main", "nagar", "jetty",
}

# A difference in any of these means two DIFFERENT places, however similar the
# rest of the name. "North Paravur" and "South Paravur" are 0.846 similar and
# are not the same panchayat. This guard is what makes a low threshold safe.
DIRECTIONAL_TOKENS = {
    "north", "south", "east", "west", "upper", "lower", "new", "old",
    "inner", "outer", "central",
}
NEAR_DUPLICATE_METRES = 100.0
STALE_FEED_MONTHS = 12

CONFIDENCE_EXACT = "exact"
CONFIDENCE_CROSSWALKED = "crosswalked"
CONFIDENCE_UNMATCHED = "unmatched-estimate"


# helpers

def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Adequate at district scale."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _name_tokens(name: str | None) -> list[str]:
    return [ABBREVIATIONS.get(t, t) for t in normalize_name(name).split()]


def _core_tokens(name: str | None) -> list[str]:
    """The identifying part of a name, with type words removed."""
    return [t for t in _name_tokens(name) if t not in GENERIC_TOKENS]


def _is_initialism(short: list[str], long_: list[str]) -> bool:
    """True when `short` is the initials of `long_` — "M G" for "Mahatma Gandhi"."""
    if not short or len(short) != len(long_):
        return False
    if not all(len(t) == 1 for t in short):
        return False
    if all(len(t) == 1 for t in long_):
        return False  # two initialisms tell us nothing about each other
    return all(word.startswith(initial) for initial, word in zip(short, long_))


def name_similarity(left: str | None, right: str | None) -> float:
    """How likely two names denote the same real-world thing.

    Plain string similarity fails badly on Kerala source data: an official layer
    writes "Mahatma Gandhi Road" where OSM writes "M G Road" (0.593 raw), and no
    raw threshold separates that from "North Paravur" vs "South Paravur" (0.846
    raw, different places). So this compares identity rather than spelling:
    abbreviations are expanded, type words dropped, initialisms matched
    structurally, and a directional mismatch is an outright veto.
    """
    return name_match(left, right)[0]


def name_match(left: str | None, right: str | None) -> tuple[float, str]:
    """name_similarity, plus which rule decided it — for auditable flags."""
    left_tokens, right_tokens = _name_tokens(left), _name_tokens(right)
    left_dir = {t for t in left_tokens if t in DIRECTIONAL_TOKENS}
    right_dir = {t for t in right_tokens if t in DIRECTIONAL_TOKENS}
    if left_dir != right_dir:
        return 0.0, "blocked_directional_mismatch"

    left_core, right_core = _core_tokens(left), _core_tokens(right)
    if not left_core or not right_core:
        return 0.0, "no_identifying_tokens"
    if left_core == right_core:
        return 1.0, "identical_core"
    if _is_initialism(left_core, right_core) or _is_initialism(right_core, left_core):
        return 0.95, "initialism"
    return SequenceMatcher(None, " ".join(left_core), " ".join(right_core)).ratio(), "core_similarity"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_geojson_features(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("features", [])


def dataset_variant(filename: str) -> str:
    """Group files that belong to the same edition of a dataset.

    data/raw can legitimately hold two editions of the same district — here a
    clean 'demo' set beside a 'messy' stress-test set, in production an official
    layer beside a community one. Cross-level sums must compare like with like,
    so we key on the trailing token of the filename stem (population_ward_legacy
    _messy -> "messy"), which is how these editions are actually distinguished.
    A corpus with only one edition collapses to a single group and behaves as if
    this were not here.
    """
    stem = Path(filename).stem
    tail = stem.rsplit("_", 1)[-1] if "_" in stem else ""
    return tail if tail.isalpha() else ""


def to_float(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def geometry_centroid(feature: dict) -> tuple[float, float] | None:
    geometry = feature.get("geometry")
    if not geometry:
        return None
    try:
        point = shape(geometry).centroid
        return point.y, point.x
    except Exception:
        return None


# HARD checks

def check_geometry_validity(features: list[dict], source_file: str) -> list[dict]:
    """Null geometry and self-intersection. Both reject the record, not the file."""
    failures: list[dict] = []
    for feature in features:
        properties = feature.get("properties") or {}
        record_id = properties.get("admin_id") or properties.get("road_id") or properties.get("flood_zone_id")
        geometry = feature.get("geometry")

        if geometry is None or geometry.get("coordinates") is None:
            failures.append({
                "check": "geometry_validity",
                "rule": "geometry_not_null",
                "source_file": source_file,
                "record_id": record_id,
                "detail": "feature has null geometry",
                "action": "record_not_loaded",
            })
            continue

        try:
            geom = shape(geometry)
        except Exception as error:
            failures.append({
                "check": "geometry_validity", "rule": "geometry_constructible",
                "source_file": source_file, "record_id": record_id,
                "detail": f"geometry could not be constructed: {error}",
                "action": "record_not_loaded",
            })
            continue

        if not geom.is_valid:
            failures.append({
                "check": "geometry_validity",
                "rule": "no_self_intersection",
                "source_file": source_file,
                "record_id": record_id,
                "detail": explain_validity(geom),
                "action": "record_not_loaded_pending_repair",
            })
    return failures


def check_foreign_keys(features: list[dict], source_file: str) -> list[dict]:
    """parent_id must resolve to an admin_id of the same boundary_vintage.

    Scoping the lookup to one vintage is deliberate: a parent that only exists
    in a different delimitation is not a valid parent, it is a stale reference
    that would silently graft two incompatible hierarchies together.
    """
    ids_by_vintage: dict[str, set[str]] = defaultdict(set)
    for feature in features:
        properties = feature.get("properties") or {}
        if properties.get("admin_id"):
            ids_by_vintage[properties.get("boundary_vintage") or ""].add(properties["admin_id"])

    failures: list[dict] = []
    for feature in features:
        properties = feature.get("properties") or {}
        parent_id = properties.get("parent_id")
        if not parent_id:
            continue
        vintage = properties.get("boundary_vintage") or ""
        if parent_id in ids_by_vintage.get(vintage, set()):
            continue
        other_vintages = [v for v, ids in ids_by_vintage.items() if v != vintage and parent_id in ids]
        failures.append({
            "check": "foreign_key_resolution",
            "rule": "parent_id_resolves_within_boundary_vintage",
            "source_file": source_file,
            "record_id": properties.get("admin_id"),
            "detail": (
                f"parent_id {parent_id!r} not found among admin_ids with boundary_vintage {vintage!r}"
                + (f"; it exists under vintage(s) {other_vintages}" if other_vintages else "")
            ),
            "boundary_vintage": vintage,
            "action": "record_not_loaded",
        })
    return failures


def check_entity_counts() -> dict:
    """PostGIS/Neo4j entity-count agreement — stub until the stores exist.

    Deliberately reports not_implemented rather than passing vacuously: a check
    that silently succeeds because it never ran is worse than no check.
    """
    return {
        "check": "entity_count_agreement",
        "rule": "postgis_and_neo4j_hold_the_same_entity_counts",
        "status": "deferred_to_storage_layer",
        "detail": (
            "Implemented in src/storage/neo4j/verify.py, which compares per-(label, "
            "dataset_edition) counts in PostGIS against Neo4j node counts and exits "
            "non-zero on any drift. It cannot run at Stage 2, before either store is "
            "loaded; run it after src/storage/neo4j/load.py."
        ),
        "action": "no_records_halted",
    }


# SOFT checks

def check_population_sums(boundaries: list[dict], panchayat_rows: list[dict], ward_rows: list[dict]) -> list[dict]:
    """Compare child population sums to their parent, flagging vintage mismatches.

    A sum that matches across two different boundary_vintages is still only an
    estimate — the units happen to add up, but they are not the same units.
    """
    flags: list[dict] = []
    vintage_by_admin = {
        (f.get("properties") or {}).get("admin_id"): (f.get("properties") or {}).get("boundary_vintage")
        for f in boundaries
    }

    parent_population: dict[tuple[str, str], dict] = {}
    for row in panchayat_rows:
        admin_id = (row.get("admin_id") or "").strip()
        # Later revisions win for the comparison; the revision conflict itself is
        # flagged separately by check_revision_conflicts.
        if admin_id and to_float(row.get("population")) is not None:
            key = (row.get("__dataset", ""), admin_id)
            existing = parent_population.get(key)
            if existing is None or (row.get("revision_status") == "final" and existing.get("revision_status") != "final"):
                parent_population[key] = row

    # Group by (dataset, parent) so two files describing the same district are
    # compared separately instead of being summed into one impossible total.
    children: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in ward_rows:
        parent = (row.get("panchayat_id") or "").strip()
        if parent:
            children[(row.get("__dataset", ""), parent)].append(row)

    for (dataset, parent_id), rows in sorted(children.items()):
        child_total = sum(to_float(r.get("population")) or 0.0 for r in rows)
        child_vintages = {r.get("boundary_vintage") for r in rows}
        parent_row = parent_population.get((dataset, parent_id))

        if parent_row is None:
            flags.append({
                "check": "population_sum_across_vintages",
                "match_confidence": CONFIDENCE_UNMATCHED,
                "dataset": dataset,
                "parent_id": parent_id,
                "child_count": len(rows),
                "child_population_sum": child_total,
                "parent_population": None,
                "detail": (
                    f"no parent population row for {parent_id!r} in {dataset!r} — likely a malformed id "
                    "(e.g. a dropped leading zero); sum cannot be reconciled"
                ),
                "action": "flagged_do_not_join",
            })
            continue

        parent_total = to_float(parent_row.get("population"))
        parent_vintage = parent_row.get("boundary_vintage")
        boundary_vintage = vintage_by_admin.get(parent_id)
        vintages_agree = len(child_vintages) == 1 and child_vintages == {parent_vintage}
        totals_agree = parent_total is not None and abs(child_total - parent_total) < 1e-6

        if vintages_agree and totals_agree and parent_vintage == boundary_vintage:
            confidence = CONFIDENCE_EXACT
            detail = "child populations sum to the parent within one boundary_vintage"
        elif totals_agree:
            confidence = CONFIDENCE_UNMATCHED
            detail = (
                f"totals agree ({child_total:g}) but the units differ: children are "
                f"{sorted(v for v in child_vintages if v)}, parent row is {parent_vintage!r}, "
                f"current boundary is {boundary_vintage!r} — an arithmetic coincidence "
                "across a delimitation change, not a verified match"
            )
        else:
            confidence = CONFIDENCE_UNMATCHED
            detail = (
                f"child sum {child_total:g} != parent {parent_total} across vintages "
                f"{sorted(v for v in child_vintages if v)} vs {parent_vintage!r}"
            )

        flags.append({
            "check": "population_sum_across_vintages",
            "match_confidence": confidence,
            "dataset": dataset,
            "parent_id": parent_id,
            "child_count": len(rows),
            "child_ids": [r.get("legacy_ward_id") for r in rows],
            "child_population_sum": child_total,
            "parent_population": parent_total,
            "child_boundary_vintages": sorted(v for v in child_vintages if v),
            "parent_boundary_vintage": parent_vintage,
            "current_boundary_vintage": boundary_vintage,
            "detail": detail,
            "action": "flagged_do_not_join" if confidence != CONFIDENCE_EXACT else "ok",
        })
    return flags


def check_revision_conflicts(rows: list[dict], key_fields: tuple[str, ...], source_file: str) -> list[dict]:
    """Same key, different revision_status — a real revision, not a duplicate bug.

    Both rows are kept. 'final' is named as the authoritative one for downstream
    use, but 'provisional' is never deleted: the earlier estimate is itself a
    published fact with its own citations.
    """
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for index, row in enumerate(rows, start=2):
        key = tuple((row.get(field) or "").strip() for field in key_fields)
        if any(key):
            grouped[key].append({"row": index, **row})

    flags: list[dict] = []
    for key, group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        statuses = {(r.get("revision_status") or "").strip() for r in group}
        finals = [r for r in group if (r.get("revision_status") or "").strip() == "final"]
        flags.append({
            "check": "revision_conflict",
            "match_confidence": CONFIDENCE_EXACT if len(statuses) > 1 else CONFIDENCE_UNMATCHED,
            "source_file": source_file,
            "key": dict(zip(key_fields, key)),
            "row_count": len(group),
            "revision_statuses": sorted(statuses),
            "rows": [{"row": r["row"], "revision_status": r.get("revision_status"),
                      "source_date": r.get("source_date"),
                      "value": r.get("value") if "value" in r else r.get("population")} for r in group],
            "detail": (
                "same key published more than once with differing revision_status — "
                "a genuine revision" if len(statuses) > 1 else
                "same key duplicated with identical revision_status — cannot tell which is authoritative"
            ),
            "authoritative_row": finals[-1]["row"] if len(finals) == 1 else None,
            "action": "keep_both_rows",
        })
    return flags


def check_near_duplicates(entities: list[dict], source_label: str) -> list[dict]:
    """Entities from different sources describing one real-world thing.

    Requires BOTH a >90% normalized-name similarity and a centroid within ~100m.
    Either signal alone is too weak: parallel roads share names, and unrelated
    features share corners.
    """
    flags: list[dict] = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            left, right = entities[i], entities[j]
            if left["source"] and left["source"] == right["source"]:
                continue  # a source is allowed to be internally distinct
            if not left["point"] or not right["point"]:
                continue
            similarity, method = name_match(left["name"], right["name"])
            if similarity < NAME_SIMILARITY_THRESHOLD:
                continue
            distance = haversine_metres(*left["point"], *right["point"])
            if distance > NEAR_DUPLICATE_METRES:
                continue
            flags.append({
                "check": "near_duplicate_entity",
                "match_confidence": CONFIDENCE_EXACT if similarity == 1.0 else CONFIDENCE_CROSSWALKED,
                "entity_type": source_label,
                "ids": [left["id"], right["id"]],
                "names": [left["name"], right["name"]],
                "sources": [left["source"], right["source"]],
                "name_similarity": round(similarity, 4),
                "name_match_method": method,
                "distance_m": round(distance, 1),
                "detail": (
                    f"{left['id']!r} and {right['id']!r} are {round(distance, 1)}m apart with "
                    f"{round(similarity * 100, 1)}% name similarity across different sources"
                ),
                "action": "flagged_for_dedup_review",
            })
    return flags


def check_stale_feeds(rows: list[dict], source_file: str, today: date) -> list[dict]:
    """feed_end_date more than 12 months in the past."""
    flags: list[dict] = []
    for index, row in enumerate(rows, start=2):
        raw = (row.get("feed_end_date") or "").strip()
        if not raw:
            continue
        try:
            end_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        months_stale = (today.year - end_date.year) * 12 + (today.month - end_date.month)
        if months_stale <= STALE_FEED_MONTHS:
            continue
        flags.append({
            "check": "stale_feed",
            "match_confidence": CONFIDENCE_UNMATCHED,
            "source_file": source_file,
            "row": index,
            "record_id": row.get("stop_id") or row.get("station_id"),
            "feed_end_date": end_date.isoformat(),
            "months_stale": months_stale,
            "checked_against": today.isoformat(),
            "detail": f"feed ended {months_stale} months ago — coverage is not current",
            "action": "flagged_use_with_caveat",
        })
    return flags


# driver

def run_checks(today: date | None = None) -> tuple[dict, dict]:
    today = today or date.today()
    gis_dir = PROCESSED_DIR / "gis"
    env_dir = PROCESSED_DIR / "environment"

    boundaries: list[dict] = []
    hard_failures: list[dict] = []

    for path in sorted(gis_dir.glob("admin_boundaries*.geojson")):
        features = read_geojson_features(path)
        boundaries.extend(features)
        hard_failures += check_geometry_validity(features, path.name)
        hard_failures += check_foreign_keys(features, path.name)

    for path in sorted(list(gis_dir.glob("*.geojson")) + list(env_dir.glob("*.geojson"))):
        if path.name.startswith("admin_boundaries"):
            continue
        hard_failures += check_geometry_validity(read_geojson_features(path), path.name)

    entity_count_check = check_entity_counts()

    # --- soft ---
    soft_flags: list[dict] = []
    population_dir = PROCESSED_DIR / "population"
    panchayat_rows: list[dict] = []
    ward_rows: list[dict] = []
    for path in sorted(population_dir.glob("*.csv")):
        rows = read_csv(path)
        for row in rows:
            row["__dataset"] = dataset_variant(path.name)
        if rows and "legacy_ward_id" in rows[0]:
            ward_rows.extend(rows)
        elif rows:
            panchayat_rows.extend(rows)
            soft_flags += check_revision_conflicts(rows, ("admin_id", "data_year"), path.name)

    soft_flags += check_population_sums(boundaries, panchayat_rows, ward_rows)

    for path in sorted((PROCESSED_DIR / "economy").glob("*.csv")):
        soft_flags += check_revision_conflicts(read_csv(path), ("admin_id", "indicator", "year"), path.name)

    # near-duplicates: point features from CSV, line/polygon features from GeoJSON
    transport_dir = PROCESSED_DIR / "transportation"
    for path in sorted(transport_dir.glob("*.csv")):
        rows = read_csv(path)
        entities = []
        for row in rows:
            lat, lon = to_float(row.get("lat")), to_float(row.get("lon"))
            entities.append({
                "id": row.get("stop_id") or row.get("station_id"),
                "name": row.get("name"),
                "source": row.get("source"),
                "point": (lat, lon) if lat is not None and lon is not None else None,
            })
        soft_flags += check_near_duplicates(entities, path.stem)
        soft_flags += check_stale_feeds(rows, path.name, today)

    for path in sorted(gis_dir.glob("*.geojson")):
        if path.name.startswith("admin_boundaries"):
            continue
        entities = []
        for feature in read_geojson_features(path):
            properties = feature.get("properties") or {}
            entities.append({
                "id": properties.get("road_id") or properties.get("waterbody_id") or properties.get("admin_id"),
                "name": properties.get("name"),
                "source": properties.get("source"),
                "point": geometry_centroid(feature),
            })
        soft_flags += check_near_duplicates(entities, path.stem)

    halted_ids = sorted({f["record_id"] for f in hard_failures if f.get("record_id")})
    hard_report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "policy": "hard failures halt loading of the affected records only; the rest of the file still loads",
        "failure_count": len(hard_failures),
        "records_halted": halted_ids,
        "stubs": [entity_count_check],
        "failures": hard_failures,
    }
    soft_report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "policy": "soft flags never halt loading; each carries a match_confidence for downstream weighting",
        "flag_count": len(soft_flags),
        "by_confidence": {
            level: sum(1 for f in soft_flags if f["match_confidence"] == level)
            for level in (CONFIDENCE_EXACT, CONFIDENCE_CROSSWALKED, CONFIDENCE_UNMATCHED)
        },
        "flags": soft_flags,
    }

    write_json(HARD_PATH, hard_report)
    write_json(SOFT_PATH, soft_report)

    print(f"  HARD: {len(hard_failures)} failure(s); records halted: {halted_ids or 'none'}")
    for failure in hard_failures:
        print(f"    - [{failure['rule']}] {failure['record_id']}: {failure['detail']}")
    print(f"  SOFT: {len(soft_flags)} flag(s) — {soft_report['by_confidence']}")
    for flag in soft_flags:
        print(f"    - [{flag['check']}/{flag['match_confidence']}] {flag['detail']}")
    print(f"  STUB: {entity_count_check['check']} — {entity_count_check['status']}")

    return hard_report, soft_report
