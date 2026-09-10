"""Build the `bench` dataset: real Ernakulam data with defects injected on purpose.

WHY THIS EXISTS. The three datasets each fail at something:

  demo       clean but tiny and entirely invented — proves nothing
  stress     covers all 20 defect classes, but on 11 fabricated records, so its
             thresholds are calibrated to fiction (its one road-name pair scored
             0.923; the real ones score 0.52-0.78)
  ernakulam  real geometry and real Census figures, but clean — it never
             exercises a single hard check

This merges them: the ernakulam dataset's real records are the substrate, and
defects are injected INTO real records. That makes every detection test run at
real scale, against real Kerala spellings and a real administrative hierarchy.

The part that matters most is `defect_manifest.json`. Every injection records
what was done, to which record, and which check is expected to catch it — so
detection can be SCORED (precision and recall per defect class) instead of
eyeballed in a log. None of the other three datasets supports that.

Injected records are marked `injected_defect` in their own source field, so a
reader can never mistake a planted defect for real published data.

    python scripts/build_ernakulam_dataset.py      # build the real base first
    python scripts/build_benchmark_dataset.py
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "ernakulam" / "ernakulam_data"
OUT = ROOT / "data" / "bench" / "bench_data"
MANIFEST = OUT.parent / "defect_manifest.json"

defects: list[dict] = []


def record(defect_id, klass, target, file, check, severity, note):
    defects.append({"defect_id": defect_id, "class": klass, "target_record": target,
                    "file": file, "expected_check": check,
                    "expected_severity": severity, "note": note})


def load_json(p): return json.loads(p.read_text(encoding="utf-8"))
def dump_json(p, d): p.write_text(json.dumps(d, indent=1), encoding="utf-8")
def load_csv(p): return list(csv.DictReader(p.open(encoding="utf-8-sig")))


def dump_csv(p, rows, fields=None):
    fields = fields or list(rows[0].keys())
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(rows)


def build():
    if not BASE.exists():
        raise SystemExit("run scripts/build_ernakulam_dataset.py first")
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(BASE, OUT)
    for old in OUT.rglob("*_ernakulam.*"):
        old.rename(old.with_name(old.name.replace("_ernakulam", "_bench")))

    # ---------------- boundaries: geometry + FK defects ----------------
    gp = OUT / "gis" / "admin_boundaries_bench.geojson"
    doc = load_json(gp)
    feats = doc["features"]
    lsgs = [f for f in feats if f["properties"]["level"] in ("panchayat", "municipality")]

    # 1 — self-intersecting polygon, built by crossing a real ring's own points.
    victim = lsgs[3]
    ring = victim["geometry"]["coordinates"][0][0]
    mid = len(ring) // 2
    victim["geometry"] = {"type": "Polygon",
                          "coordinates": [[ring[0], ring[mid], ring[1], ring[mid + 1], ring[0]]]}
    victim["properties"]["source"] = "injected_defect"
    record("D01", "self_intersecting_geometry", victim["properties"]["admin_id"],
           gp.name, "geometry_validity", "hard",
           "real LSG ring re-ordered into a bowtie")

    # 2 — null geometry, as shipped by real government shapefiles.
    victim = lsgs[7]
    victim["geometry"] = None
    victim["properties"]["source"] = "injected_defect"
    record("D02", "null_geometry", victim["properties"]["admin_id"], gp.name,
           "geometry_validity", "hard", "geometry set to null")

    # 3 — parent_id pointing at a taluk that does not exist.
    victim = lsgs[11]
    victim["properties"]["parent_id"] = "EKM-T-ZZZ"
    victim["properties"]["source"] = "injected_defect"
    record("D03", "orphaned_foreign_key", victim["properties"]["admin_id"], gp.name,
           "foreign_key_resolution", "hard", "parent_id -> EKM-T-ZZZ (nonexistent)")

    # 4 — transliteration/casing noise of the kind that separates agencies.
    for offset, mangle in ((15, lambda n: f"  {n.lower()}  "), (19, lambda n: n.upper())):
        victim = lsgs[offset]
        original = victim["properties"]["name"]
        victim["properties"]["name"] = mangle(original)
        record(f"D04{offset}", "name_casing_whitespace", victim["properties"]["admin_id"],
               gp.name, "normalized_before_matching", "silent",
               f"{original!r} -> {victim['properties']['name']!r}")
    dump_json(gp, doc)

    # ---------------- population: value + revision defects ----------------
    pp = OUT / "population" / "population_panchayat_bench.csv"
    rows = load_csv(pp)
    populated = [r for r in rows if r["population"]]

    # 9 — comma-formatted number, as Excel exports it.
    r = populated[0]
    r["population"] = f"{int(r['population']):,}"
    r["source"] = "injected_defect"
    record("D09", "comma_formatted_number", r["admin_id"], pp.name,
           "thousands_separator_stripped", "cleaning", f"population -> {r['population']!r}")

    # 10 — DD-MM-YYYY where the rest of the column is ISO.
    r = populated[1]
    r["source_date"] = "04-09-2026"
    r["source"] = "injected_defect"
    record("D10", "dd_mm_yyyy_date", r["admin_id"], pp.name, "date_reformatted",
           "cleaning", "source_date -> '04-09-2026'")

    # 11 — a blank that must stay null, never become zero.
    r = populated[2]
    r["literacy_rate"] = ""
    r["source"] = "injected_defect"
    record("D11", "missing_value", r["admin_id"], pp.name, "missing_value",
           "cleaning", "literacy_rate blanked; must load as NULL not 0")

    # 12 — a genuine revision: same unit published twice, provisional then final.
    r = populated[3]
    provisional = dict(r)
    provisional["population"] = str(int(r["population"]) - 850)
    provisional["revision_status"] = "provisional"
    provisional["source_date"] = "2011-01-15"
    provisional["source"] = "injected_defect"
    rows.insert(rows.index(r), provisional)
    record("D12", "revision_conflict", r["admin_id"], pp.name, "revision_conflict",
           "soft", "provisional + final rows for one admin_id/year; both must survive")
    dump_csv(pp, rows)

    # ---------------- legacy wards: cross-vintage + malformed id ----------------
    lw = OUT / "population" / "population_ward_legacy_bench.csv"
    legacy = []
    for index, r in enumerate(populated[4:7]):
        total = int(r["population"])
        for w in range(1, 4):
            pid = r["admin_id"]
            # 14 — a dropped leading zero, the commonest manual-entry error.
            if index == 2 and w == 3:
                pid = pid[:-2] + pid[-1]
            legacy.append({"legacy_ward_id": f"{r['admin_id']}-LW{w}", "panchayat_id": pid,
                           "population": total // 3 - (400 if w == 1 else 0),
                           "data_year": 2011, "revision_status": "final",
                           "boundary_vintage": "pre-2025-delimitation",
                           "note": "legacy ward split; 2025 delimitation added 1,712 wards",
                           "source": "injected_defect"})
    record("D13", "cross_vintage_population_sum", populated[4]["admin_id"], lw.name,
           "population_sum_across_vintages", "soft",
           "ward sums do not reconcile to the parent across the delimitation change")
    record("D14", "malformed_admin_id", legacy[-1]["panchayat_id"], lw.name,
           "population_sum_across_vintages", "soft",
           f"panchayat_id {legacy[-1]['panchayat_id']!r} has a dropped leading zero")
    dump_csv(lw, legacy)

    # ---------------- economy: embedded unit ----------------
    ec = OUT / "economy" / "economy_bench.csv"
    rows = load_csv(ec)
    # 16 — value and unit fused into one cell by a copy-paste from a PDF table.
    r = next(x for x in rows if x["indicator"] == "gddp" and x["year"] == "2015")
    r["value"] = f"{r['value']} Cr"
    r["source"] = "injected_defect"
    record("D16", "embedded_unit_in_value", f"{r['admin_id']}/gddp/2015", ec.name,
           "embedded_unit_extracted", "cleaning", "value -> '62965.47 Cr'")
    dump_csv(ec, rows)

    # ---------------- documents: an acquisition failure to log ----------------
    sm = OUT / "documents" / "source_manifest_bench.csv"
    dump_csv(sm, [
        {"dataset": "LSG boundaries", "url": "https://github.com/opendatakerala/lsg-kerala-data",
         "status": "reachable", "checked_on": "2026-09-10"},
        {"dataset": "Census 2011 taluk tables", "url": "https://www.censusindia2011.com/",
         "status": "reachable", "checked_on": "2026-09-10"},
        {"dataset": "Economy (OLD portal - do not use)", "url": "https://kerala.data.gov.in/",
         "status": "unreachable_maintenance", "checked_on": "2026-09-10"},
        {"dataset": "Bus GTFS (community)",
         "url": "https://jungle-bus.github.io/KochiTransport/KochiTransport.zip",
         "status": "reachable_but_stale", "checked_on": "2026-09-10"},
    ])
    record("D20", "unreachable_source_url", "kerala.data.gov.in", sm.name,
           "ingestion_fetch_failure", "acquisition",
           "--url path must log and fall back, not crash (still stubbed)")

    # ---------------- defects the REAL data already contains ----------------
    for did, klass, target, file, check, note in [
        ("R06", "near_duplicate_entity", "EKM-R-001 / EKM-R-001-OSM", "roads_bench.geojson",
         "near_duplicate_entity", "real abbreviation pair: 'Mahatma Gandhi Road' vs 'M G Road'"),
        ("R07", "non_wgs84_crs", "water_bodies_utm_bench.geojson", "water_bodies_utm_bench.geojson",
         "crs_reprojection", "shipped in EPSG:32643 as agency exports are"),
        ("R08", "stale_vintage_layer", "EKM-FZ-001", "flood_hazard_bench.geojson",
         "vintage_survives_to_storage", "KSDMA classification is 2010 NCESS fieldwork"),
        ("R15", "missing_years_in_series", "EKM-D/per_capita_income", "economy_bench.csv",
         "missing_years", "Ecostat publishes no open figure for most years"),
        ("R18", "near_duplicate_entity", "EKM-BS-001 / EKM-BS-001-COMM", "bus_stops_bench.csv",
         "near_duplicate_entity", "official vs community GTFS naming"),
        ("R19", "stale_feed", "EKM-BS-001-COMM", "bus_stops_bench.csv", "stale_feed",
         "community feed ended 2023-03-25"),
        ("R05", "alternate_id_scheme", "admin_boundaries_alt_scheme_bench.csv",
         "admin_boundaries_alt_scheme_bench.csv", "admin_code_xref",
         "real Census names vs real OSM names, no crosswalk published"),
    ]:
        record(did, klass, target, file, check, "soft", "PRE-EXISTING IN REAL DATA: " + note)

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    dump_json(MANIFEST, {
        "dataset": "bench",
        "base": "data/ernakulam/ernakulam_data (real OSM boundaries + Census 2011 + Ecostat)",
        "purpose": "real data at real scale with defects injected, for SCORED detection testing",
        "injected": sum(1 for d in defects if not d["note"].startswith("PRE-EXISTING")),
        "pre_existing_real": sum(1 for d in defects if d["note"].startswith("PRE-EXISTING")),
        "defects": defects,
    })

    files = sum(1 for p in OUT.rglob("*") if p.is_file())
    print(f"built {OUT.relative_to(ROOT)}  ({files} files)")
    print(f"  injected defects     : {sum(1 for d in defects if not d['note'].startswith('PRE-'))}")
    print(f"  real defects kept    : {sum(1 for d in defects if d['note'].startswith('PRE-'))}")
    print(f"  ground truth         : {MANIFEST.relative_to(ROOT)}")
    for d in defects:
        tag = "REAL " if d["note"].startswith("PRE-") else "INJ  "
        print(f"    {tag}{d['defect_id']:<5} {d['class']:<30} {d['expected_severity']:<12} {d['target_record']}")


if __name__ == "__main__":
    build()
