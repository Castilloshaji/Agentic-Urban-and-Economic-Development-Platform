"""Stage 2 driver: data/raw/ -> data/processed/.

    python -m src.processing.run
"""

from __future__ import annotations

import sys

from .clean_values import clean_values
from .consistency_checks import run_checks
from .crs import standardize_crs
from .paths import PROCESSED_DIR, RAW_DIR
from .reconcile_ids import reconcile_ids


def clear_processed() -> int:
    """Empty data/processed/ before a run.

    Everything under data/processed is derived from data/raw, so carrying any of
    it across a run is never right — and when the two runs are different source
    datasets it is actively wrong: stale boundaries from the previous edition
    stay visible to the consistency checks and get reported as findings about
    data that is no longer there.
    """
    removed = 0
    for path in sorted(PROCESSED_DIR.rglob("*"), reverse=True):
        if path.is_file() and path.name != ".gitkeep":
            path.unlink()
            removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def main(argv: list[str] | None = None) -> int:
    print(f"Stage 2 processing: {RAW_DIR} -> {PROCESSED_DIR}\n")
    removed = clear_processed()
    if removed:
        print(f"Cleared {removed} file(s) from a previous run\n")

    print("[1/4] CRS standardization")
    crs_report = standardize_crs()

    print("\n[2/4] Admin code reconciliation")
    xref_summary = reconcile_ids()

    print("\n[3/4] Value cleaning")
    cleaning_report = clean_values()

    print("\n[4/4] Consistency checks")
    hard_report, soft_report = run_checks()

    print("\n" + "-" * 68)
    print(f"CRS         : {crs_report['files_inspected']} file(s) inspected, "
          f"{crs_report['files_reprojected']} reprojected")
    print(f"Crosswalk   : {xref_summary['matched']} matched, "
          f"{len(xref_summary['unmatched_alt'])} unmatched alt, "
          f"{len(xref_summary['unmatched_canonical'])} unmatched canonical")
    print(f"Cleaning    : {len(cleaning_report['files'])} file(s), "
          f"{len(cleaning_report['events'])} event(s) logged")
    print(f"Hard checks : {hard_report['failure_count']} failure(s), "
          f"{len(hard_report['records_halted'])} record(s) halted")
    print(f"Soft checks : {soft_report['flag_count']} flag(s) {soft_report['by_confidence']}")
    print(f"\nOutputs in {PROCESSED_DIR}")
    # Soft flags and hard failures are findings, not crashes — the run itself
    # succeeded if it produced its reports.
    return 0


if __name__ == "__main__":
    sys.exit(main())
