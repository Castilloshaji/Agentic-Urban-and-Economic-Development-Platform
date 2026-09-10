"""Stage 2 step 2 — reconcile LGD/LSG/Census codes onto one canonical admin_id.

No crosswalk ships with the data, so the only bridge is the published name.
Nothing is dropped: an entity that fails to match is written out with
match_status=unmatched_canonical or unmatched_alt.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path

from .paths import PROCESSED_DIR, RAW_DIR, write_json

XREF_PATH = PROCESSED_DIR / "admin_code_xref.csv"
UNMATCHED_LOG_PATH = PROCESSED_DIR / "admin_code_xref_unmatched.json"

XREF_FIELDS = (
    "normalized_name",
    "match_status",
    "match_confidence",
    "match_method",
    "canonical_admin_id",
    "canonical_name",
    "canonical_level",
    "alt_id",
    "alt_name_as_published",
    "alt_level",
    "alt_source_file",
)

# Candidate column names in an alternate-scheme CSV, in priority order.
ALT_ID_COLUMNS = ("alt_id", "lgd_code", "lsg_code", "census_code", "code", "id")
ALT_NAME_COLUMNS = ("name_as_published", "name", "entity_name", "label")


def normalize_name(value: str | None) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace.

    Handles the transliteration drift the raw data is full of: "  demo
    panchayat b  ", "DEMO PANCHAYAT-C" and "Demo panchayat c" all normalize to
    the same key.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^\w\s]+", " ", text)  # hyphens, dots, commas -> space
    return re.sub(r"\s+", " ", text).strip()


def _pick_column(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {name.lower().strip(): name for name in fieldnames}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def load_canonical_entities(processed_gis_dir: Path) -> list[dict]:
    """Read the canonical admin entities from the processed admin_boundaries GeoJSONs.

    More than one boundary file can describe the same district — an official
    layer alongside an OSM-derived one, say. Features sharing an admin_id are
    therefore folded into one canonical entity that carries every name variant
    seen for it, so the same real place is not treated as two candidates (and
    then rejected as ambiguous) purely because two sources spell it differently.
    """
    by_admin_id: dict[str, dict] = {}
    for path in sorted(processed_gis_dir.glob("admin_boundaries*.geojson")):
        for feature in json.loads(path.read_text(encoding="utf-8")).get("features", []):
            properties = feature.get("properties") or {}
            admin_id = properties.get("admin_id")
            if not admin_id:
                continue
            name = properties.get("name")
            entity = by_admin_id.setdefault(
                admin_id,
                {"admin_id": admin_id, "name": name, "level": properties.get("level"),
                 "name_variants": [], "normalized_names": set(), "source_files": []},
            )
            if name is not None and name not in entity["name_variants"]:
                entity["name_variants"].append(name)
            entity["normalized_names"].add(normalize_name(name))
            if path.name not in entity["source_files"]:
                entity["source_files"].append(path.name)

    for entity in by_admin_id.values():
        # Report the tidiest spelling as the canonical one.
        entity["name"] = min(entity["name_variants"], key=lambda n: (len(str(n).strip()) != len(str(n)), str(n)))
        entity["normalized_name"] = normalize_name(entity["name"])
        entity["source_file"] = ", ".join(entity["source_files"])
    return list(by_admin_id.values())


def load_alt_scheme_rows(processed_gis_dir: Path) -> list[dict]:
    """Read every alternate-ID-scheme CSV sitting alongside the boundaries."""
    rows: list[dict] = []
    for path in sorted(processed_gis_dir.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                continue
            id_column = _pick_column(reader.fieldnames, ALT_ID_COLUMNS)
            name_column = _pick_column(reader.fieldnames, ALT_NAME_COLUMNS)
            if not id_column or not name_column:
                print(f"  skipping {path.name}: no recognisable id/name columns")
                continue
            level_column = _pick_column(reader.fieldnames, ("level", "admin_level"))
            for record in reader:
                rows.append(
                    {
                        "alt_id": (record.get(id_column) or "").strip(),
                        "alt_name": record.get(name_column),
                        "alt_level": (record.get(level_column) or "").strip() if level_column else "",
                        "normalized_name": normalize_name(record.get(name_column)),
                        "source_file": path.name,
                    }
                )
    return rows


def reconcile_ids() -> dict:
    """Build the admin code crosswalk. Returns a summary dict."""
    # Boundaries come from the CRS-standardized copies; the alternate-scheme
    # CSVs are read straight from raw, since Stage 2 step 1 only touches GeoJSON
    # and this step normalizes the names itself anyway.
    canonical = load_canonical_entities(PROCESSED_DIR / "gis")
    alternates = load_alt_scheme_rows(RAW_DIR / "gis")

    # Group by normalized name so an ambiguous name (same name at two levels)
    # is visible rather than resolved by whichever row happened to come first.
    canonical_by_name: dict[str, list[dict]] = {}
    for entity in canonical:
        for normalized in entity["normalized_names"]:
            canonical_by_name.setdefault(normalized, []).append(entity)

    xref_rows: list[dict] = []
    matched_canonical_ids: set[str] = set()
    unmatched_alt: list[dict] = []

    for alt in alternates:
        candidates = canonical_by_name.get(alt["normalized_name"], [])
        # When a name appears at more than one level, the level column disambiguates.
        if len(candidates) > 1 and alt["alt_level"]:
            narrowed = [c for c in candidates if (c["level"] or "").lower() == alt["alt_level"].lower()]
            candidates = narrowed or candidates

        if len(candidates) == 1:
            entity = candidates[0]
            matched_canonical_ids.add(entity["admin_id"])
            exact = any(str(variant) == str(alt["alt_name"]) for variant in entity["name_variants"])
            xref_rows.append(
                {
                    "normalized_name": alt["normalized_name"],
                    "match_status": "matched",
                    "match_confidence": "exact" if exact else "crosswalked",
                    "match_method": "identical_name" if exact else "normalized_name",
                    "canonical_admin_id": entity["admin_id"],
                    "canonical_name": entity["name"],
                    "canonical_level": entity["level"],
                    "alt_id": alt["alt_id"],
                    "alt_name_as_published": alt["alt_name"],
                    "alt_level": alt["alt_level"],
                    "alt_source_file": alt["source_file"],
                }
            )
        else:
            reason = "ambiguous_name" if candidates else "no_canonical_match"
            unmatched_alt.append({**alt, "reason": reason, "candidate_count": len(candidates)})
            xref_rows.append(
                {
                    "normalized_name": alt["normalized_name"],
                    "match_status": "unmatched_alt",
                    "match_confidence": "unmatched-estimate",
                    "match_method": reason,
                    "canonical_admin_id": "",
                    "canonical_name": "",
                    "canonical_level": "",
                    "alt_id": alt["alt_id"],
                    "alt_name_as_published": alt["alt_name"],
                    "alt_level": alt["alt_level"],
                    "alt_source_file": alt["source_file"],
                }
            )

    unmatched_canonical = [e for e in canonical if e["admin_id"] not in matched_canonical_ids]
    for entity in unmatched_canonical:
        xref_rows.append(
            {
                "normalized_name": entity["normalized_name"],
                "match_status": "unmatched_canonical",
                "match_confidence": "unmatched-estimate",
                "match_method": "no_alt_scheme_row",
                "canonical_admin_id": entity["admin_id"],
                "canonical_name": entity["name"],
                "canonical_level": entity["level"],
                "alt_id": "",
                "alt_name_as_published": "",
                "alt_level": "",
                "alt_source_file": entity["source_file"],
            }
        )

    XREF_PATH.parent.mkdir(parents=True, exist_ok=True)
    with XREF_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=XREF_FIELDS)
        writer.writeheader()
        writer.writerows(xref_rows)

    summary = {
        "canonical_entities": len(canonical),
        "alt_scheme_rows": len(alternates),
        "matched": sum(1 for r in xref_rows if r["match_status"] == "matched"),
        "unmatched_alt": [
            {"alt_id": u["alt_id"], "alt_name": u["alt_name"], "source_file": u["source_file"], "reason": u["reason"]}
            for u in unmatched_alt
        ],
        "unmatched_canonical": [
            {"admin_id": e["admin_id"], "name": e["name"], "level": e["level"]} for e in unmatched_canonical
        ],
    }
    write_json(UNMATCHED_LOG_PATH, summary)

    print(f"  matched {summary['matched']} of {len(alternates)} alternate rows")
    for item in summary["unmatched_alt"]:
        print(f"  UNMATCHED alt   {item['alt_id']} ({item['alt_name']!r}) — {item['reason']}")
    for item in summary["unmatched_canonical"]:
        print(f"  UNMATCHED canon {item['admin_id']} ({item['name']!r}) — no alt-scheme row")
    return summary
