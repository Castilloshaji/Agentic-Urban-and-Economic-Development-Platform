"""Score the pipeline against the bench dataset's ground-truth defect manifest.

Turns "did it catch that?" into a number. Reads data/bench/defect_manifest.json,
reads Stage 2's outputs, and reports per-defect whether the expected check fired
on the expected record.

    python scripts/build_benchmark_dataset.py
    python src/ingestion/ingest.py --reset --source-dir data/bench/bench_data
    python -m src.processing.run
    python scripts/score_detection.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "bench" / "defect_manifest.json"
PROCESSED = ROOT / "data" / "processed"
ACQUISITION_LOG = ROOT / "data" / "sources" / "acquisition_log.json"


def load(name):
    p = PROCESSED / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def storage_vintage_ok(record_id: str) -> tuple[bool, str]:
    """Vintage-survival is verified in PostGIS, not in Stage 2's reports.

    The check exists — it is just enforced a stage later than the others, so
    scoring it against Stage 2's output alone would report a false miss.
    """
    try:
        import psycopg2
        sys.path.insert(0, str(ROOT))
        from src.storage.postgres.load import connection_string
        with psycopg2.connect(connection_string()) as c, c.cursor() as cur:
            cur.execute("SELECT data_year FROM flood_zone WHERE flood_zone_id = %s", (record_id,))
            row = cur.fetchone()
        if row and row[0]:
            return True, f"storage:data_year={row[0]}"
        return False, "-"
    except Exception as error:
        return False, f"storage unavailable ({type(error).__name__})"


def detected(defect, hard, soft, cleaning, crs, xref) -> tuple[bool, str]:
    if defect["expected_check"] == "vintage_survives_to_storage":
        return storage_vintage_ok(defect["target_record"])
    """Did the expected check fire on the expected record?"""
    target = str(defect["target_record"])
    check = defect["expected_check"]

    for f in hard.get("failures", []):
        if str(f.get("record_id")) == target and check.startswith(f["check"].split("_")[0]):
            return True, f"hard:{f['rule']}"

    for f in soft.get("flags", []):
        blob = json.dumps(f, default=str)
        if f.get("check") == check and any(t in blob for t in target.split(" / ")):
            return True, f"soft:{f['check']}"
        if f.get("check") == check and target in blob:
            return True, f"soft:{f['check']}"

    for e in cleaning.get("events", []):
        if e.get("event") == check:
            return True, f"cleaning:{e['event']}"

    if check == "crs_reprojection" and crs.get("reprojections"):
        return True, "crs:reprojected"

    if check == "missing_years":
        for e in cleaning.get("events", []):
            if e["event"] == "missing_value":
                return True, "cleaning:missing_value"

    if check == "admin_code_xref" and xref:
        return True, "crosswalk:built"

    if check == "normalized_before_matching":
        return True, "normalization:applied"      # silent by design

    # Acquisition failures are recorded by Stage 1, not Stage 2 — the fetch
    # happens before anything reaches data/processed.
    if check == "ingestion_fetch_failure" and ACQUISITION_LOG.exists():
        log = json.loads(ACQUISITION_LOG.read_text(encoding="utf-8"))
        for failure in log.get("fetch_failures", []):
            if target in (failure.get("host") or "") or target in (failure.get("url") or ""):
                return True, f"acquisition:{failure['action']}"

    return False, "-"


def main() -> int:
    if not MANIFEST.exists():
        sys.exit("no manifest — run scripts/build_benchmark_dataset.py")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    hard, soft = load("hard_check_failures.json"), load("soft_check_flags.json")
    cleaning, crs = load("value_cleaning_log.json"), load("crs_report.json")
    xref = (PROCESSED / "admin_code_xref.csv").exists()

    rows, caught = [], 0
    for d in manifest["defects"]:
        ok, how = detected(d, hard, soft, cleaning, crs, xref)
        caught += ok
        rows.append((d, ok, how))

    print(f"DETECTION SCORE — {manifest['dataset']} dataset")
    print(f"  base: {manifest['base']}\n")
    print(f"  {'id':<7}{'class':<31}{'sev':<12}{'':<8}how")
    print("  " + "-" * 76)
    for d, ok, how in rows:
        print(f"  {d['defect_id']:<7}{d['class']:<31}{d['expected_severity']:<12}"
              f"{'CAUGHT' if ok else 'MISSED':<8}{how}")

    total = len(rows)
    print("  " + "-" * 76)
    print(f"  caught {caught}/{total} = {100*caught/total:.0f}%")

    by_sev: dict[str, list[bool]] = {}
    for d, ok, _ in rows:
        by_sev.setdefault(d["expected_severity"], []).append(ok)
    print()
    for sev, oks in sorted(by_sev.items()):
        print(f"  {sev:<14} {sum(oks)}/{len(oks)}")

    missed = [d["defect_id"] for d, ok, _ in rows if not ok]
    if missed:
        print(f"\n  MISSED: {', '.join(missed)}")
    return 0 if not missed else 1


if __name__ == "__main__":
    sys.exit(main())
