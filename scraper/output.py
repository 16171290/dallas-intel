"""
Output writers - records.json archive and daily GHL-format CSV.

records.json is the source of truth (committed daily by the bot per Sec C.3).
The CSV is a flattened export for downstream import into a CRM (GHL push
is deferred per Sec 3.4.2; the CSV format is the manual-import format
inherited from Harris-Intel).

CSV-filter semantics (Phase 0.A):
  - Records with `active=False` are suppressed (existing behavior).
  - Records with `in_buy_box=False` are also suppressed from the CSV
    but remain in records.json for audit.
  - Records lacking the in_buy_box flag (legacy / pre-Phase-0.A) default
    to True so the CSV is backward-compatible.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CanonicalRecord = dict[str, Any]


# ============================================================================
# JSON archive
# ============================================================================

def write_records_json(records: list[CanonicalRecord], path: Path) -> None:
    """Atomic write of the full records archive to ``path``.

    Atomicity: write to a sibling ``.tmp`` and rename. Prevents readers
    seeing a half-written file if the process dies mid-write.

    Records are sorted by score (descending) then by filing_date (descending)
    so the dashboard's default view is meaningful without client-side sort.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    sorted_records = sorted(
        records,
        key=lambda r: (-int(r.get("score") or 0), str(r.get("filing_date") or "")),
        reverse=False,
    )

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": date.today().isoformat(),
                "record_count": len(sorted_records),
                "records":      sorted_records,
            },
            f,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    tmp.replace(path)
    logger.info("Wrote %d records to %s", len(sorted_records), path)


def read_records_json(path: Path) -> list[CanonicalRecord]:
    """Read an existing records.json archive. Returns [] if missing/invalid."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read %s: %s - starting fresh", path, e)
        return []
    return data.get("records", []) if isinstance(data, dict) else []


# ============================================================================
# Daily CSV (GHL-style)
# ============================================================================

# CSV column order modeled on Harris-Intel's GHL export. Skip-trace fields
# (Email, Phone) ship empty for now since Sec 3.4.1 is deferred.
GHL_CSV_COLUMNS = [
    "First Name",
    "Last Name",
    "Email",
    "Phone",
    "Address",
    "City",
    "State",
    "Zip",
    "Tags",
    "Notes",
    "Source",
    "Score",
    "DCAD Account",
    "Filing Date",
    "Instrument Number",
]


def write_daily_csv(
    records: list[CanonicalRecord],
    target_date: date,
    exports_dir: Path,
) -> Path:
    """Write a single-day GHL-style CSV.

    Filename: ``ghl_export_YYYYMMDD.csv``. Returns the path.

    Filters applied (records suppressed from CSV but kept in records.json):
      - ``active=False``      (existing suppression - REL/RLP releases etc.)
      - ``in_buy_box=False``  (Phase 0.A - outside operator's buy-box)

    Records missing either flag default to True for backward compatibility
    with pre-Phase-0.A records.json archives.
    """
    exports_dir.mkdir(parents=True, exist_ok=True)
    filename = f"ghl_export_{target_date.strftime('%Y%m%d')}.csv"
    out_path = exports_dir / filename

    written = 0
    skipped_active = 0
    skipped_buy_box = 0

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            if not rec.get("active", True):
                skipped_active += 1
                continue
            if not rec.get("in_buy_box", True):
                skipped_buy_box += 1
                continue
            writer.writerow(_record_to_csv_row(rec))
            written += 1
    tmp.replace(out_path)

    logger.info(
        "Wrote daily CSV: %s (%d rows; skipped %d inactive, %d outside buy-box)",
        out_path, written, skipped_active, skipped_buy_box,
    )
    return out_path


def _record_to_csv_row(rec: CanonicalRecord) -> dict[str, str]:
    """Project a canonical record into a flat GHL CSV row."""
    first, last = _split_name(rec.get("grantee") or rec.get("dcad_owner") or "")
    addr_parts = _split_address(rec.get("address") or "")

    category = rec.get("category", "")
    instrument = rec.get("instrument_num", "")
    tags = ";".join(filter(None, [
        f"category:{category}" if category else "",
        f"dallas_code:{rec.get('dallas_code', '')}",
        f"source:{rec.get('source', '')}",
        "homestead" if rec.get("dcad_homestead") else "",
    ]))

    notes_bits = []
    if mv := rec.get("dcad_market_value"):
        notes_bits.append(f"Market value: ${int(mv):,}")
    if grantor := rec.get("grantor"):
        notes_bits.append(f"Grantor: {grantor}")
    if sale_date := rec.get("sale_date"):
        notes_bits.append(f"Sale date: {sale_date}")
    if amount := rec.get("amount"):
        notes_bits.append(f"Amount: ${amount}")

    return {
        "First Name":        first,
        "Last Name":         last,
        "Email":             "",
        "Phone":             "",
        "Address":           addr_parts["street"],
        "City":              addr_parts["city"],
        "State":             addr_parts["state"],
        "Zip":               addr_parts["zip"],
        "Tags":              tags,
        "Notes":             " | ".join(notes_bits),
        "Source":            rec.get("source", ""),
        "Score":             str(rec.get("score", 0)),
        "DCAD Account":      rec.get("dcad_account") or "",
        "Filing Date":       rec.get("filing_date") or "",
        "Instrument Number": instrument or "",
    }


def _split_name(full: str) -> tuple[str, str]:
    """Naive first/last split. Returns ('', '') if unparseable."""
    parts = full.strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return ("", parts[0])
    # Assume last token is surname; rest is first/middle.
    return (" ".join(parts[:-1]), parts[-1])


def _split_address(addr: str) -> dict[str, str]:
    """Best-effort split into street / city / state / zip.

    Examples accepted (Dallas County PDF patterns):
      "1004 Seago Drive, Seagoville, TX 75159"
      "200 N Main St Dallas TX 75201"

    Returns dict with empty strings for parts that couldn't be resolved.
    """
    import re

    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not addr:
        return out

    s = addr.strip().rstrip(".")
    # Try comma-delimited form first.
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 3:
        out["street"] = parts[0]
        out["city"]   = parts[1]
        # Last chunk like "TX 75159" or "TX75159"
        last = parts[-1]
        m = re.match(r"^([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?$", last)
        if m:
            out["state"] = m.group(1)
            out["zip"]   = m.group(2) or ""
        else:
            out["state"] = last
    else:
        # Fall back to whole-string treatment.
        out["street"] = s
        # Try to pull a trailing zip.
        m = re.search(r"\b(\d{5}(?:-\d{4})?)\b\s*$", s)
        if m:
            out["zip"] = m.group(1)
            out["street"] = s[: m.start()].strip().rstrip(",")
        # Try to pull a two-letter state before the zip.
        m = re.search(r"\b([A-Z]{2})\b\s+\d{5}", s)
        if m:
            out["state"] = m.group(1)
    return out


# ============================================================================
# Run summary
# ============================================================================

def summarize_run(
    records: list[CanonicalRecord],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a run-summary dict for the Discord notification.

    Includes counts by category, score band, and overall address-resolution
    rate. Caller can pass ``extra`` to merge in pipeline-level metrics
    (e.g. EnrichmentStats fields, gov_filtered_count, buy_box summary).
    """
    by_category = Counter(r.get("category", "UNKNOWN") for r in records)
    score_bands = Counter(_score_band(r.get("score", 0)) for r in records)
    active_count = sum(1 for r in records if r.get("active", True))
    matched_addr = sum(1 for r in records if r.get("dcad_account"))
    in_buy_box_count = sum(1 for r in records if r.get("in_buy_box", True))

    summary: dict[str, Any] = {
        "total":                       len(records),
        "active":                      active_count,
        "in_buy_box_count":            in_buy_box_count,
        "by_category":                 dict(by_category),
        "by_score_band":               dict(score_bands),
        "address_resolution_count":    matched_addr,
        "address_resolution_rate":     matched_addr / len(records) if records else 0.0,
    }
    if extra:
        summary.update(extra)
    return summary


def _score_band(score: int) -> str:
    """Bucket score into named bands for the summary."""
    if score >= 90:
        return "EXTREME"
    if score >= 75:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    if score >= 40:
        return "LOW"
    return "NONE"
