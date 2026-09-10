"""Stage 1 — copy source files into data/raw/<category>/ and log them.

Copies bytes and records what it copied. It never edits, cleans or re-encodes;
that is Stage 2's job.

    python src/ingestion/ingest.py --source-dir DIR [--reset]
    python src/ingestion/ingest.py --url CATEGORY      # not implemented
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# The six raw categories defined by the Phase 1 architecture.
CATEGORIES = (
    "population",
    "economy",
    "gis",
    "transportation",
    "environment",
    "documents",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
MANIFEST_PATH = RAW_DIR / "ingestion_manifest.csv"
SOURCES_PATH = Path(__file__).resolve().parent / "sources.yaml"

MANIFEST_FIELDS = ("file_path", "category", "sha256_hash", "copied_at", "source_dir")

CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so large GIS files stay off the heap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_manifest_rows(rows: list[dict[str, str]]) -> None:
    """Append rows to the manifest, writing the header if the file is new.

    The manifest is append-only: re-ingesting the same source leaves the earlier
    rows in place, so the hashes form a history of what arrived and when.
    """
    if not rows:
        return
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not MANIFEST_PATH.exists() or MANIFEST_PATH.stat().st_size == 0
    with MANIFEST_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def ingest_directory(source_dir: Path) -> list[dict[str, str]]:
    """Copy every file under a category subdirectory of source_dir into data/raw/.

    source_dir is expected to hold one subdirectory per category:

        <source_dir>/gis/roads.geojson  ->  data/raw/gis/roads.geojson
        <source_dir>/gis/osm/roads.pbf  ->  data/raw/gis/osm/roads.pbf

    Paths below the category directory are preserved. Files that sit outside a
    known category (loose READMEs at the top level, unrecognised folders) are
    skipped and reported, not guessed at.
    """
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise NotADirectoryError(f"--source-dir is not a directory: {source_dir}")

    rows: list[dict[str, str]] = []
    skipped: list[Path] = []

    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(source_dir)
        category = relative.parts[0] if len(relative.parts) > 1 else None
        if category not in CATEGORIES:
            skipped.append(relative)
            continue

        destination = RAW_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # copy2 preserves mtime, so the raw copy keeps the source's timestamps.
        shutil.copy2(path, destination)

        rows.append(
            {
                "file_path": str(destination.relative_to(PROJECT_ROOT)),
                "category": category,
                "sha256_hash": sha256_file(destination),
                "copied_at": datetime.now(timezone.utc).isoformat(),
                "source_dir": str(source_dir),
            }
        )
        print(f"  copied {relative} -> {destination.relative_to(PROJECT_ROOT)}")

    if skipped:
        print(f"\nSkipped {len(skipped)} file(s) outside the known categories:")
        for relative in skipped:
            print(f"  - {relative}")
        print(f"  Known categories: {', '.join(CATEGORIES)}")

    return rows


def ingest_url(category: str) -> list[dict[str, str]]:
    """Download a category's sources from sources.yaml. Not implemented yet."""
    raise NotImplementedError(
        f"--url {category!r} is not implemented yet.\n"
        f"Remote downloading is Stage 1 future work: it will read the {category!r} "
        f"entries from {SOURCES_PATH.relative_to(PROJECT_ROOT)}, fetch each url into "
        f"data/raw/{category}/, and write the same manifest rows as --source-dir. "
        f"Most Phase 1 sources also still need their url filled in there.\n"
        "For now, download the files by hand into a staging directory laid out as "
        "<staging>/<category>/... and run: "
        "python -m src.ingestion.ingest --source-dir <staging>"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest",
        description="Copy raw source files into data/raw/ and log them to the ingestion manifest.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--source-dir",
        type=Path,
        metavar="DIR",
        help="Directory holding one subdirectory per category to copy from.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Empty data/raw/ (and the manifest) before copying, so a run against a "
             "different --source-dir starts from a clean tree instead of merging into "
             "whatever the previous run left behind.",
    )
    mode.add_argument(
        "--url",
        choices=CATEGORIES,
        metavar="CATEGORY",
        help=f"Download a category from sources.yaml ({', '.join(CATEGORIES)}). Not implemented yet.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.reset:
        removed = 0
        for path in sorted(RAW_DIR.rglob("*"), reverse=True):
            if path.is_file() and path.name != ".gitkeep":
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        print(f"--reset: removed {removed} file(s) from {RAW_DIR.relative_to(PROJECT_ROOT)}\n")

    if args.url:
        rows = ingest_url(args.url)
    else:
        print(f"Ingesting from {args.source_dir}")
        rows = ingest_directory(args.source_dir)

    append_manifest_rows(rows)
    print(
        f"\nIngested {len(rows)} file(s). "
        f"Manifest: {MANIFEST_PATH.relative_to(PROJECT_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
