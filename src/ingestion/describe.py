"""Regenerate the file inventory in data_dictionary.md from the ingestion manifest.

    python -m src.ingestion.describe [--write]
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "data" / "raw" / "ingestion_manifest.csv"
DICTIONARY = PROJECT_ROOT / "data_dictionary.md"

BEGIN = "<!-- INVENTORY:BEGIN -->"
END = "<!-- INVENTORY:END -->"


def render() -> str:
    if not MANIFEST.exists():
        return "_No ingestion manifest yet — run `python src/ingestion/ingest.py --source-dir ...`._"

    with MANIFEST.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return "_Manifest is empty._"

    # Later ingests supersede earlier ones for the same path; keep the newest.
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["file_path"]] = row

    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in latest.values():
        by_category[row["category"]].append(row)

    editions = sorted({Path(r["source_dir"]).name for r in latest.values()})
    lines = [
        f"**{len(latest)} file(s)** currently in `data/raw/`, "
        f"from: {', '.join(f'`{e}`' for e in editions)}.",
        "",
        "| Category | File | Ingested (UTC) | sha256 (first 12) |",
        "| --- | --- | --- | --- |",
    ]
    for category in sorted(by_category):
        for row in sorted(by_category[category], key=lambda r: r["file_path"]):
            lines.append(
                f"| {category} | `{Path(row['file_path']).name}` | "
                f"{row['copied_at'][:19]} | `{row['sha256_hash'][:12]}` |"
            )
    lines += [
        "",
        "Full hashes and source directories are in "
        "`data/raw/ingestion_manifest.csv`. The manifest is append-only, so it "
        "also records what was ingested previously and when.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="describe", description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="Update the inventory section of data_dictionary.md in place.")
    args = parser.parse_args(argv)

    inventory = render()
    if not args.write:
        print(inventory)
        return 0

    text = DICTIONARY.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"markers {BEGIN}/{END} not found in {DICTIONARY.name}", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    DICTIONARY.write_text(f"{head}{BEGIN}\n{inventory}\n{END}{tail}", encoding="utf-8")
    print(f"updated {DICTIONARY.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
