"""Stage 2 step 1 — reproject every raw GeoJSON to EPSG:4326.

RFC 7946 says a GeoJSON with no "crs" member IS WGS84. We honour that but record
the assumption in the report rather than leaving it implicit.
"""

from __future__ import annotations

import json
import re

from pyproj import CRS, Transformer

from .paths import PROCESSED_DIR, RAW_DIR, write_json

GEO_CATEGORIES = ("gis", "environment")
TARGET_CRS = "EPSG:4326"
CRS_REPORT_PATH = PROCESSED_DIR / "crs_report.json"


def parse_crs_member(crs_member: dict | None) -> str | None:
    """Return an 'EPSG:xxxx' string from a GeoJSON crs member, or None if absent.

    Handles both the OGC URN form ("urn:ogc:def:crs:EPSG::32643") and the older
    short form ("EPSG:32643"), which is what agency exports actually contain.
    """
    if not crs_member:
        return None
    name = (crs_member.get("properties") or {}).get("name")
    if not name:
        return None
    match = re.search(r"EPSG:{1,2}(\d+)", str(name))
    if match:
        return f"EPSG:{match.group(1)}"
    return str(name)


def _reproject_coords(coords, transformer: Transformer):
    """Recursively rebuild a GeoJSON coordinate structure through a transformer.

    Coordinates nest to an arbitrary depth (Point -> MultiPolygon), so recurse
    until we hit the [x, y] leaf. Any third element (elevation) is carried
    through untouched.
    """
    if coords and isinstance(coords[0], (int, float)):
        x, y = transformer.transform(coords[0], coords[1])
        return [x, y, *coords[2:]]
    return [_reproject_coords(part, transformer) for part in coords]


def reproject_geojson(document: dict, from_crs: str) -> dict:
    """Return a copy of the document with every coordinate moved to EPSG:4326."""
    # always_xy keeps input as (lon/easting, lat/northing) — GeoJSON's own order.
    transformer = Transformer.from_crs(CRS.from_user_input(from_crs), CRS.from_user_input(TARGET_CRS), always_xy=True)
    out = json.loads(json.dumps(document))
    for feature in out.get("features", []):
        geometry = feature.get("geometry")
        if geometry and geometry.get("coordinates") is not None:
            geometry["coordinates"] = _reproject_coords(geometry["coordinates"], transformer)
    # The reprojected file is now WGS84, which RFC 7946 expresses by omission.
    out.pop("crs", None)
    return out


def standardize_crs() -> dict:
    """Inspect and standardize every raw GeoJSON. Returns the report dict."""
    entries: list[dict] = []

    for category in GEO_CATEGORIES:
        source_dir = RAW_DIR / category
        if not source_dir.is_dir():
            continue

        for path in sorted(source_dir.rglob("*.geojson")):
            relative = path.relative_to(RAW_DIR)
            document = json.loads(path.read_text(encoding="utf-8"))
            declared = parse_crs_member(document.get("crs"))

            if declared is None:
                action, from_crs = "assumed_wgs84_no_crs_member", None
                out_doc = document
            elif CRS.from_user_input(declared) == CRS.from_user_input(TARGET_CRS):
                action, from_crs = "already_wgs84", declared
                out_doc = document
            else:
                action, from_crs = "reprojected", declared
                out_doc = reproject_geojson(document, declared)

            destination = PROCESSED_DIR / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(out_doc, indent=2), encoding="utf-8")

            entries.append(
                {
                    "file": str(relative),
                    "from_crs": from_crs,
                    "to_crs": TARGET_CRS,
                    "action": action,
                    "feature_count": len(out_doc.get("features", [])),
                    "output": str(destination.relative_to(PROCESSED_DIR.parent.parent)),
                }
            )
            print(f"  [{action}] {relative}" + (f"  {from_crs} -> {TARGET_CRS}" if from_crs and action == "reprojected" else ""))

    reprojections = [e for e in entries if e["action"] == "reprojected"]
    # "reprojections" is the report proper: empty means nothing needed moving.
    # "inspected" is the audit trail of what was looked at and why it was left
    # alone — a file with no crs member is an assumption worth recording, not a
    # finding worth reporting.
    report = {
        "target_crs": TARGET_CRS,
        "files_inspected": len(entries),
        "files_reprojected": len(reprojections),
        "reprojections": reprojections,
        "inspected": entries,
    }
    write_json(CRS_REPORT_PATH, report)
    return report
