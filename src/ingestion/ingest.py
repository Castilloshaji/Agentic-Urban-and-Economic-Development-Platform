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
import json
import shutil
import sys
import time
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

MANIFEST_FIELDS = ("file_path", "category", "sha256_hash", "copied_at", "source_dir",
                   "source_url", "source_level", "publisher")

# Fetched upstream files land in a persistent cache, NOT in data/raw. data/raw is
# the working tree for one dataset edition and gets wiped by --reset; an acquired
# government archive must outlive that. Dataset builders read from the cache.
SOURCES_CACHE = PROJECT_ROOT / "data" / "sources"
ACQUISITION_LOG = SOURCES_CACHE / "acquisition_log.json"

# Kerala government hosts are slow: ecostat, kerala.data.gov.in and udiseplus all
# exceeded a 12 s budget during source verification and only answered at ~35 s.
# These are generous on purpose — a timeout here is indistinguishable from a dead
# source, and wrongly recording a source as dead is worse than waiting.
CONNECT_TIMEOUT = 35
READ_TIMEOUT = 300
RETRIES = 3
BACKOFF = 4
LARGE_FETCH_MB = 100

# Which response content-types are acceptable for a given file extension.
# Indian government portals routinely answer a missing path with HTTP 200 and an
# HTML error page — a "soft 404". Without this check that lands on disk as a
# perfectly valid-looking .csv and poisons everything downstream.
EXPECTED_CONTENT = {
    ".csv": ("text/csv", "application/csv", "text/plain", "application/octet-stream"),
    ".json": ("application/json", "text/json", "text/plain", "application/octet-stream"),
    ".geojson": ("application/json", "application/geo+json", "text/plain",
                 "application/octet-stream"),
    ".zip": ("application/zip", "application/x-zip-compressed", "application/octet-stream"),
    ".pdf": ("application/pdf", "application/octet-stream"),
    ".pbf": ("application/octet-stream", "application/x-protobuf"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              "application/octet-stream"),
}


def content_type_mismatch(url: str, content_type: str) -> str | None:
    """Return a reason string when the body is not the type the URL promised."""
    suffix = Path(url.split("?")[0]).suffix.lower()
    allowed = EXPECTED_CONTENT.get(suffix)
    if not allowed:
        return None
    actual = (content_type or "").split(";")[0].strip().lower()
    if not actual or actual in allowed:
        return None
    return (f"soft failure: URL promised {suffix} but server returned "
            f"{actual!r} — almost certainly an error page, not the file")

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

    # An older manifest has fewer columns. Rewrite it under the current header
    # rather than appending rows that would silently misalign.
    if not is_new:
        with MANIFEST_PATH.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if list(reader.fieldnames or []) != list(MANIFEST_FIELDS):
                previous = list(reader)
                with MANIFEST_PATH.open("w", newline="", encoding="utf-8") as out:
                    writer = csv.DictWriter(out, fieldnames=MANIFEST_FIELDS, restval="")
                    writer.writeheader()
                    writer.writerows(previous)
    with MANIFEST_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, restval="")
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


def load_sources() -> dict:
    import yaml

    if not SOURCES_PATH.exists():
        raise FileNotFoundError(f"source register missing: {SOURCES_PATH}")
    return yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}


def _filename_for(entry: dict, url: str) -> str:
    """Name the local file so loaders can dispatch on its extension.

    Some URLs carry no extension at all (GitHub codeload serves a zip from a
    bare path), so the register may state `filename:` explicitly. Without that an
    archive lands as .bin and every extension-based loader skips it.
    """
    if entry.get("filename"):
        return entry["filename"]
    tail = Path(url.split("?")[0].rsplit("/", 1)[-1])
    # Only the final suffix. Upstream names like
    # "1.-Flood-Return-Probability-Historical-10-25-50-years.zip" are full of
    # dots, so treating the last two as one compound suffix mangles the name.
    suffix = tail.suffix
    if tail.suffixes[-2:-1] == [".tar"]:
        suffix = ".tar" + suffix
    return f"{entry['name']}{suffix}" if suffix else f"{entry['name']}.bin"


def fetch_one(entry: dict, category: str, include_large: bool) -> dict:
    """Download one source. Returns a result dict; never raises on a bad source.

    A source that cannot be fetched is a fact to record, not a reason to abandon
    the other nineteen. Everything here funnels into one `status` so the caller
    treats a 404, a timeout and a JS-gated portal the same way: log it, carry on.
    """
    import httpx

    name, url, mode = entry["name"], (entry.get("url") or ""), entry.get("fetch", "auto")
    base = {"name": name, "category": category, "url": url,
            "source_level": entry.get("level"), "publisher": entry.get("publisher"),
            "fetch_mode": mode, "attempted_at": datetime.now(timezone.utc).isoformat()}

    if mode == "manual" or not url:
        return {**base, "status": "skipped_manual",
                "reason": entry.get("notes", "requires manual download")}
    if mode == "api_key":
        return {**base, "status": "skipped_api_key",
                "reason": "needs a registered API key; see .env"}
    if mode == "large" and not include_large:
        return {**base, "status": "skipped_large",
                "reason": "large source; re-run with --include-large"}

    destination = SOURCES_CACHE / category / _filename_for(entry, url)
    destination.parent.mkdir(parents=True, exist_ok=True)

    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT,
                                    write=READ_TIMEOUT, pool=CONNECT_TIMEOUT)
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    mismatch = content_type_mismatch(url, response.headers.get("content-type", ""))
                    if mismatch:
                        raise ValueError(mismatch)
                    # Stream to a temp file so a failure midway cannot leave a
                    # truncated file looking like a complete download.
                    partial = destination.with_suffix(destination.suffix + ".part")
                    written = 0
                    with partial.open("wb") as handle:
                        for chunk in response.iter_bytes(CHUNK_SIZE):
                            handle.write(chunk)
                            written += len(chunk)
                    if written == 0:
                        partial.unlink(missing_ok=True)
                        raise ValueError("empty response body")
                    partial.replace(destination)

            digest = sha256_file(destination)
            return {**base, "status": "ok", "attempts": attempt,
                    "bytes": written, "sha256": digest,
                    "file_path": str(destination.relative_to(PROJECT_ROOT))}

        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
            if attempt < RETRIES:
                time.sleep(BACKOFF * attempt)

    return {**base, "status": "failed", "attempts": RETRIES, "reason": last_error}


def ingest_url(category: str | None = None, include_large: bool = False) -> list[dict]:
    """Fetch sources from the register. Failures are logged, never fatal."""
    register = load_sources()
    categories = [category] if category else sorted(register)
    unknown = [c for c in categories if c not in register]
    if unknown:
        raise KeyError(f"no such category in the register: {unknown}")

    results, rows = [], []
    for cat in categories:
        print(f"\n[{cat}]")
        for entry in register.get(cat) or []:
            result = fetch_one(entry, cat, include_large)
            results.append(result)
            marker = {"ok": "  ok      ", "failed": "  FAILED  "}.get(
                result["status"], "  skipped ")
            detail = (f"{result['bytes']:,} bytes" if result["status"] == "ok"
                      else result.get("reason", "")[:88])
            print(f"{marker}L{result['source_level']} {result['name']:<46} {detail}")

            if result["status"] == "ok":
                rows.append({
                    "file_path": result["file_path"], "category": cat,
                    "sha256_hash": result["sha256"],
                    "copied_at": datetime.now(timezone.utc).isoformat(),
                    "source_dir": result["url"], "source_url": result["url"],
                    "source_level": result["source_level"],
                    "publisher": result["publisher"],
                })

    write_acquisition_log(results)
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"].startswith("skipped")]

    print(f"\n  fetched {ok}, failed {len(failed)}, skipped {len(skipped)}")
    if failed:
        print(f"  {len(failed)} source(s) unreachable — logged, run continued:")
        for r in failed:
            print(f"    - {r['name']}: {r.get('reason','')[:80]}")
    if skipped:
        by = {}
        for r in skipped:
            by.setdefault(r["status"], []).append(r["name"])
        for status, names in sorted(by.items()):
            print(f"  {status}: {len(names)} ({', '.join(names[:3])}"
                  f"{'...' if len(names) > 3 else ''})")
    print(f"  acquisition log: {ACQUISITION_LOG.relative_to(PROJECT_ROOT)}")
    return rows


def write_acquisition_log(results: list[dict]) -> None:
    """Record every attempt, including the ones that did not produce a file.

    This is the artefact defect D20 is scored against: an unreachable source has
    to leave evidence behind rather than vanishing.
    """
    ACQUISITION_LOG.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if ACQUISITION_LOG.exists():
        try:
            existing = json.loads(ACQUISITION_LOG.read_text(encoding="utf-8")).get("attempts", [])
        except (json.JSONDecodeError, OSError):
            existing = []
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeouts": {"connect_s": CONNECT_TIMEOUT, "read_s": READ_TIMEOUT,
                     "retries": RETRIES},
        "summary": {
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "skipped": sum(1 for r in results if r["status"].startswith("skipped")),
        },
        "fetch_failures": [
            {"check": "ingestion_fetch_failure", "source": r["name"],
             "url": r["url"], "host": r["url"].split("/")[2] if "://" in r["url"] else None,
             "source_level": r["source_level"], "attempts": r.get("attempts"),
             "reason": r.get("reason"), "action": "logged_and_continued"}
            for r in results if r["status"] == "failed"
        ],
        "unavailable_by_design": [
            {"check": "source_requires_manual_acquisition", "source": r["name"],
             "status": r["status"], "reason": r.get("reason")}
            for r in results if r["status"].startswith("skipped")
        ],
        "attempts": existing + results,
    }
    ACQUISITION_LOG.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


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
        nargs="?",
        const="__all__",
        metavar="CATEGORY",
        help="Fetch sources from src/ingestion/sources.yaml. Give a category "
             f"({', '.join(CATEGORIES)}) or omit it to fetch every category.",
    )
    parser.add_argument(
        "--include-large",
        action="store_true",
        help="Also fetch sources marked fetch: large (e.g. the 532 MB OSM extract).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.reset:
        removed = 0
        preserve = {".gitkeep"}
        for path in sorted(RAW_DIR.rglob("*"), reverse=True):
            if path.is_file() and path.name not in preserve:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        print(f"--reset: removed {removed} file(s) from {RAW_DIR.relative_to(PROJECT_ROOT)}\n")

    if args.url:
        category = None if args.url == "__all__" else args.url
        rows = ingest_url(category, include_large=args.include_large)
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
