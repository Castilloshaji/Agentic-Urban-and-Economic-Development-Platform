"""Stage 2 step 3 — parse the value formats government exports actually use.

Thousands separators, units fused into the value cell, and mixed date formats.
A blank cell stays NULL and is logged; it never becomes 0, because a literacy
rate of 0 and an unrecorded literacy rate are different claims.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from .paths import PROCESSED_DIR, RAW_DIR, write_json

CLEANING_LOG_PATH = PROCESSED_DIR / "value_cleaning_log.json"
TABULAR_CATEGORIES = ("population", "economy", "transportation", "environment", "gis", "documents")

# Date formats seen in Kerala government exports, tried in this order.
# DD-MM-YYYY is tried before MM-DD-YYYY because Indian sources use it.
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%d-%b-%Y", "%d %B %Y")

# Unit suffixes that turn up glued to a value. Longest first so "crore" wins over "cr".
UNIT_PATTERN = re.compile(
    r"^\s*(?P<number>[-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?)\s*(?P<unit>[A-Za-z₹%/.\s]*?)\s*$"
)
NUMERIC_CHARS = re.compile(r"^[-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?$")

MISSING_TOKENS = {"", "-", "--", "n/a", "na", "nil", "null", "none", "not available", "."}


def is_missing(raw: str | None) -> bool:
    return raw is None or str(raw).strip().casefold() in MISSING_TOKENS


def parse_number(raw: str | None) -> tuple[float | int | None, str | None, bool]:
    """Parse a possibly comma-formatted, possibly unit-carrying numeric cell.

    Returns (value, unit, parsed_ok). A missing cell returns (None, None, True):
    it parsed fine, there was simply nothing there. An unparseable cell returns
    (None, None, False) so the caller can log it as a real problem.
    """
    if is_missing(raw):
        return None, None, True

    text = str(raw).strip()
    match = UNIT_PATTERN.match(text)
    if not match:
        return None, None, False

    number_text = match.group("number").replace(",", "")
    unit = (match.group("unit") or "").strip(" .") or None
    try:
        value = float(number_text)
    except ValueError:
        return None, None, False
    # Keep whole numbers as ints so population counts don't render as 42000.0.
    if value.is_integer() and "." not in number_text and "e" not in number_text.lower():
        value = int(value)
    return value, unit, True


def parse_date(raw: str | None) -> tuple[str | None, str | None, bool]:
    """Parse a date cell to an ISO string. Returns (iso, format_used, parsed_ok)."""
    if is_missing(raw):
        return None, None, True
    text = str(raw).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat(), fmt, True
        except ValueError:
            continue
    return None, None, False


def _classify_columns(rows: list[dict], fieldnames: list[str]) -> dict[str, str]:
    """Decide per column whether it holds dates, numbers, or text.

    Decided from the data rather than from column names, because real sources
    do not name things predictably. A column counts as date/numeric only if
    every non-missing value in it parses that way — one stray word means the
    column is text and is passed through untouched.
    """
    kinds: dict[str, str] = {}
    for column in fieldnames:
        values = [r.get(column) for r in rows if not is_missing(r.get(column))]
        if not values:
            kinds[column] = "text"
            continue
        if all(parse_date(v)[2] and not NUMERIC_CHARS.match(str(v).strip()) for v in values):
            kinds[column] = "date"
        elif all(parse_number(v)[2] for v in values):
            kinds[column] = "numeric"
        else:
            kinds[column] = "text"
    return kinds


def clean_csv(path: Path, relative: Path) -> tuple[list[dict], list[str], list[dict]]:
    """Clean one CSV. Returns (cleaned rows, output fieldnames, log events)."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    kinds = _classify_columns(rows, fieldnames)
    events: list[dict] = []
    cleaned: list[dict] = []
    unit_columns: dict[str, str] = {}

    for index, row in enumerate(rows, start=2):  # start=2: row 1 is the header
        out: dict = {}
        for column in fieldnames:
            raw = row.get(column)
            kind = kinds[column]

            if kind == "text":
                out[column] = None if is_missing(raw) else str(raw).strip()
                if is_missing(raw):
                    events.append({"file": str(relative), "row": index, "column": column,
                                   "event": "missing_value", "raw": raw, "action": "kept_as_null"})
                continue

            if kind == "date":
                iso, fmt, ok = parse_date(raw)
                out[column] = iso
                if not ok:
                    events.append({"file": str(relative), "row": index, "column": column,
                                   "event": "unparseable_date", "raw": raw, "action": "kept_as_null"})
                elif iso is None:
                    events.append({"file": str(relative), "row": index, "column": column,
                                   "event": "missing_value", "raw": raw, "action": "kept_as_null"})
                elif fmt != "%Y-%m-%d":
                    events.append({"file": str(relative), "row": index, "column": column,
                                   "event": "date_reformatted", "raw": raw, "parsed": iso,
                                   "detected_format": fmt, "action": "normalized_to_iso"})
                continue

            value, unit, ok = parse_number(raw)
            out[column] = value
            if not ok:
                events.append({"file": str(relative), "row": index, "column": column,
                               "event": "unparseable_number", "raw": raw, "action": "kept_as_null"})
            elif value is None:
                events.append({"file": str(relative), "row": index, "column": column,
                               "event": "missing_value", "raw": raw, "action": "kept_as_null"})
            else:
                if "," in str(raw):
                    events.append({"file": str(relative), "row": index, "column": column,
                                   "event": "thousands_separator_stripped", "raw": raw,
                                   "parsed": value, "action": "parsed"})
                if unit:
                    # Split the pair rather than dropping the unit: the value is
                    # meaningless without it, and a declared unit column may disagree.
                    unit_column = f"{column}_unit"
                    unit_columns[column] = unit_column
                    out[unit_column] = unit
                    event = {"file": str(relative), "row": index, "column": column,
                             "event": "embedded_unit_extracted", "raw": raw,
                             "parsed": value, "extracted_unit": unit, "action": "split_value_and_unit"}
                    declared = row.get("unit")
                    if declared:
                        event["declared_unit_column"] = declared
                    events.append(event)

        cleaned.append(out)

    out_fieldnames = list(fieldnames)
    for column, unit_column in unit_columns.items():
        out_fieldnames.insert(out_fieldnames.index(column) + 1, unit_column)
    for row in cleaned:
        for unit_column in unit_columns.values():
            row.setdefault(unit_column, None)

    return cleaned, out_fieldnames, events


def clean_values() -> dict:
    """Clean every raw CSV into data/processed/. Returns a summary dict."""
    all_events: list[dict] = []
    files: list[dict] = []

    for category in TABULAR_CATEGORIES:
        source_dir = RAW_DIR / category
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.rglob("*.csv")):
            relative = path.relative_to(RAW_DIR)
            cleaned, fieldnames, events = clean_csv(path, relative)

            destination = PROCESSED_DIR / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
                writer.writeheader()
                # None -> empty cell, which reads back as an explicit null.
                writer.writerows([{k: ("" if v is None else v) for k, v in row.items()} for row in cleaned])

            counts: dict[str, int] = {}
            for event in events:
                counts[event["event"]] = counts.get(event["event"], 0) + 1
            files.append({"file": str(relative), "rows": len(cleaned), "events": counts})
            all_events.extend(events)
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no issues"
            print(f"  {relative}: {len(cleaned)} rows — {summary}")

    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "files": files,
        "events": all_events,
    }
    write_json(CLEANING_LOG_PATH, report)
    return report
