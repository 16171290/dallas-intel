"""Local validation harness for resolution paths.

Lets you iterate on Path B / A / C without running the full main.py
pipeline (which requires Playwright + network + probate auth and takes
20+ minutes per run).

What it does:
  1. Loads the current data/records.json
  2. Builds (or loads cached) DCAD address_index from data/dcad-*.zip
  3. Runs the requested resolution path(s) against in-memory records
  4. Prints before/after diff + structured summary
  5. Optionally writes a new records.json so you can compare locally

What it does NOT do:
  - Touch the live data/records.json without --write
  - Run the full pipeline (no scraping, no OCR, no probate)
  - Modify state on the remote / production

Usage:
    # First run — builds DCAD pickle cache (~30s on cold start)
    python scripts/validate_resolution.py --build-cache

    # Validate Path B against current records.json (no writes)
    python scripts/validate_resolution.py --path B

    # Same, write enriched records to data/records.path_b.json for diffing
    python scripts/validate_resolution.py --path B --write data/records.path_b.json

    # Verbose per-record output
    python scripts/validate_resolution.py --path B --verbose

The cached DCAD pickle lives in data/cache/dcad_address_index.pkl.gz.
Delete it to force a rebuild after a DCAD bulk refresh.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import pickle
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

# Allow running from the repo root: python scripts/validate_resolution.py
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Configure logging early so module-level loggers in scraper.* emit
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("validate_resolution")


# ═══════════════════════════════════════════════════════════════════════════
# DCAD index cache
# ═══════════════════════════════════════════════════════════════════════════

CACHE_DIR  = REPO_ROOT / "data" / "cache"
CACHE_FILE = CACHE_DIR / "dcad_address_index.pkl.gz"


def find_dcad_zip() -> Optional[Path]:
    """Locate a DCAD bulk ZIP.

    Resolution order:
      1. ``data/dcad-*.zip``           — committed-with-repo location (rare)
      2. ``$DCAD_CACHE_DIR/dcad-*.zip`` — where the daily scrape actually caches
                                          (~/.dcad_cache by default)

    Returns ``None`` when no ZIP is reachable; caller decides whether to
    fall back to downloading a fresh copy.
    """
    # 1. Repo-local data/ dir
    candidates = sorted(
        (REPO_ROOT / "data").glob("dcad-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]

    # 2. The production cache directory (where scraper.dcad_bulk writes)
    try:
        from scraper import config
        cache_dir = config.DCAD_CACHE_DIR
    except Exception:
        cache_dir = None

    if cache_dir and cache_dir.exists():
        candidates = sorted(
            cache_dir.glob("dcad-*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]

    return None


def fetch_dcad_zip_or_die() -> Path:
    """Locate a DCAD ZIP; if missing, attempt the canonical download via
    scraper.dcad_bulk.fetch_dcad_zip(). Exits with a clear message if
    no ZIP is reachable."""
    zip_path = find_dcad_zip()
    if zip_path:
        logger.info("Found DCAD ZIP: %s", zip_path)
        return zip_path

    logger.info("No cached DCAD ZIP found; attempting download via scraper.dcad_bulk")
    try:
        from scraper import dcad_bulk
        zip_path = dcad_bulk.fetch_dcad_zip()
        logger.info("Downloaded DCAD ZIP to %s", zip_path)
        return zip_path
    except Exception as e:
        sys.exit(
            f"ERROR: could not locate or download DCAD ZIP "
            f"({type(e).__name__}: {e}). "
            f"Run main.py once to populate the cache, or set DCAD_ZIP_URL env var."
        )


def build_address_index(zip_path: Path) -> dict[str, str]:
    """Build the DCAD address_index from the ZIP. Takes ~30s on first run."""
    from scraper import dcad_bulk
    logger.info("Building DCAD address_index from %s...", zip_path)
    t0 = time.perf_counter()
    tables = dcad_bulk.parse_dcad_tables(zip_path)
    index = dcad_bulk.build_address_index(tables)
    elapsed = time.perf_counter() - t0
    logger.info("Built address_index: %d entries in %.1fs", len(index), elapsed)
    return index


def load_or_build_address_index(force_rebuild: bool = False) -> dict[str, str]:
    """Load address_index from cache; build if cache is missing or
    force_rebuild=True."""
    if not force_rebuild and CACHE_FILE.exists():
        logger.info("Loading cached address_index from %s", CACHE_FILE)
        t0 = time.perf_counter()
        with gzip.open(CACHE_FILE, "rb") as f:
            index = pickle.load(f)
        elapsed = time.perf_counter() - t0
        logger.info("Loaded address_index: %d entries in %.1fs", len(index), elapsed)
        return index

    zip_path = fetch_dcad_zip_or_die()
    index = build_address_index(zip_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Writing cache to %s", CACHE_FILE)
    with gzip.open(CACHE_FILE, "wb") as f:
        pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)
    return index


# ═══════════════════════════════════════════════════════════════════════════
# Path runners
# ═══════════════════════════════════════════════════════════════════════════

def _snapshot_before(records):
    return {
        r.get("record_id"): {
            "dcad_account":       r.get("dcad_account"),
            "address_normalized": r.get("address_normalized"),
        }
        for r in records
    }


def _diff_records(records, before, get_warnings_fn):
    newly_matched = []
    overwrote_address = []
    for r in records:
        rid = r.get("record_id")
        b = before.get(rid, {})
        if not b.get("dcad_account") and r.get("dcad_account"):
            newly_matched.append({
                "record_id":      rid,
                "grantor":        r.get("grantor"),
                "before_address": b.get("address_normalized"),
                "after_address":  r.get("address_normalized"),
                "dcad_account":   r.get("dcad_account"),
                "warnings":       get_warnings_fn(r),
            })
        if (b.get("address_normalized")
                and r.get("address_normalized") != b.get("address_normalized")):
            overwrote_address.append({
                "record_id": rid,
                "before":    b.get("address_normalized"),
                "after":     r.get("address_normalized"),
            })
    return newly_matched, overwrote_address


def run_path_b(records: list[dict], address_index: dict[str, str],
               verbose: bool = False) -> dict:
    """Run Stage 6.3 — Path B and return diff stats + per-record changes."""
    from scraper import resolution_paths
    from scraper.resolution import get_history, get_warnings

    before = _snapshot_before(records)
    stats = resolution_paths.run_path_b(records, address_index)
    newly_matched, overwrote_address = _diff_records(records, before, get_warnings)

    return {
        "stats": {
            "total_records":            stats.total_records,
            "skipped_already_resolved": stats.skipped_already_resolved,
            "skipped_clean_address":    stats.skipped_clean_address,
            "candidates":               stats.candidates,
            "skipped_no_raw_excerpt":   stats.skipped_no_raw_excerpt,
            "skipped_no_clean_address": stats.skipped_no_clean_address,
            "matched":                  stats.matched,
            "no_match":                 stats.no_match,
        },
        "newly_matched":      newly_matched,
        "overwrote_address":  overwrote_address,
    }


# Path A and Path C runners will be added by PRs 3 and 4. They follow
# the same signature shape: (records, indexes_needed, verbose) -> diff dict.


# ═══════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════

def run_variant_lookup(records: list[dict], address_index: dict[str, str],
                       verbose: bool = False) -> dict:
    """Run Stage 6.35 — Variant lookup (Class 26 family)."""
    from scraper import address_variants, resolution_paths
    from scraper.resolution import get_warnings

    before = _snapshot_before(records)
    fuzzy_index = address_variants.build_fuzzy_index(address_index)
    stats = resolution_paths.run_variant_lookup(records, address_index, fuzzy_index)
    newly_matched, overwrote_address = _diff_records(records, before, get_warnings)

    return {
        "stats": {
            "total_records":            stats.total_records,
            "skipped_already_resolved": stats.skipped_already_resolved,
            "skipped_suspect_address":  stats.skipped_suspect_address,
            "skipped_no_address":       stats.skipped_no_address,
            "candidates":               stats.candidates,
            "matched":                  stats.matched,
            "no_match":                 stats.no_match,
        },
        "newly_matched":     newly_matched,
        "overwrote_address": overwrote_address,
    }


def report_variant_lookup(diff: dict, verbose: bool) -> None:
    """Human-readable summary of Stage 6.35 variant-lookup diff."""
    s = diff["stats"]
    print()
    print("=" * 72)
    print("STAGE 6.35 — Variant lookup (Class 26 family)")
    print("=" * 72)
    print(f"  Total records considered     : {s['total_records']}")
    print(f"  Skipped: dcad_account set    : {s['skipped_already_resolved']}")
    print(f"  Skipped: suspect address     : {s['skipped_suspect_address']}")
    print(f"  Skipped: no address          : {s['skipped_no_address']}")
    print(f"  Variant-lookup candidates    : {s['candidates']}")
    print(f"    Matched (newly resolved)   : {s['matched']}")
    print(f"    No match (lookup failed)   : {s['no_match']}")
    print()

    if diff["newly_matched"]:
        print(f"NEWLY RESOLVED — {len(diff['newly_matched'])} records:")
        print()
        for m in diff["newly_matched"]:
            print(f"  record_id={m['record_id']}")
            print(f"    grantor:         {m['grantor']!r}")
            print(f"    before:          {m['before_address']!r}")
            print(f"    after:           {m['after_address']!r}")
            print(f"    dcad_account:    {m['dcad_account']}")
            if m['warnings']:
                print(f"    warnings:        {m['warnings']}")
            print()


def report_path_b(diff: dict, verbose: bool) -> None:
    """Human-readable summary of Path B diff."""
    s = diff["stats"]
    print()
    print("=" * 72)
    print("PATH B — raw_excerpt clean-address fallback")
    print("=" * 72)
    print(f"  Total records considered     : {s['total_records']}")
    print(f"  Skipped: dcad_account set    : {s['skipped_already_resolved']}")
    print(f"  Skipped: address was clean   : {s['skipped_clean_address']}")
    print(f"  Path B candidates            : {s['candidates']}")
    print(f"    Skipped: no raw_excerpt    : {s['skipped_no_raw_excerpt']}")
    print(f"    Skipped: no clean addr     : {s['skipped_no_clean_address']}")
    print(f"    Matched (newly resolved)   : {s['matched']}")
    print(f"    No match (lookup failed)   : {s['no_match']}")
    print()

    if diff["newly_matched"]:
        print(f"NEWLY RESOLVED — {len(diff['newly_matched'])} records:")
        print()
        for m in diff["newly_matched"]:
            print(f"  record_id={m['record_id']}")
            print(f"    grantor:         {m['grantor']!r}")
            print(f"    before:          {m['before_address']!r}")
            print(f"    after:           {m['after_address']!r}")
            print(f"    dcad_account:    {m['dcad_account']}")
            if m['warnings']:
                print(f"    warnings:        {m['warnings']}")
            print()

    if diff["overwrote_address"] and verbose:
        print(f"ADDRESS-NORMALIZED OVERWRITES — {len(diff['overwrote_address'])} records:")
        for o in diff["overwrote_address"]:
            print(f"  {o['record_id']}: {o['before']!r}  ->  {o['after']!r}")
        print()


def report_baseline(records: list[dict]) -> None:
    """Show the baseline state of records.json before any new resolution runs."""
    print()
    print("=" * 72)
    print("BASELINE — current records.json state")
    print("=" * 72)
    print(f"  Total records              : {len(records)}")
    print(f"  By dallas_code             : {dict(Counter(r.get('dallas_code') for r in records))}")
    print(f"  Has dcad_account           : {sum(1 for r in records if r.get('dcad_account'))}")
    print(f"  Has address_normalized     : {sum(1 for r in records if r.get('address_normalized'))}")
    print(f"  Has raw_excerpt            : {sum(1 for r in records if r.get('raw_excerpt'))}")
    print()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def load_records(path: Path) -> list[dict]:
    from scraper.output import read_records_json
    if not path.exists():
        sys.exit(f"ERROR: records.json not found at {path}")
    return read_records_json(path)


def write_records_with_envelope(records: list[dict], path: Path) -> None:
    """Write a records.json-shaped file (with the canonical envelope) to path.
    Used when --write target. Does NOT touch the production data/records.json."""
    from datetime import date
    envelope = {
        "generated_at": date.today().isoformat(),
        "record_count": len(records),
        "records":      records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(envelope, f, indent=2, ensure_ascii=False, default=str)
    logger.info("Wrote %d records to %s", len(records), path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--path", choices=["B", "variant", "all"], default="all",
        help="Which resolution path(s) to validate. "
             "B = Path B raw_excerpt fallback (Stage 6.3). "
             "variant = Class 26 family fuzzy lookup (Stage 6.35). "
             "all = run both in pipeline order. "
             "Future PRs add A and C.",
    )
    ap.add_argument(
        "--records", type=Path,
        default=REPO_ROOT / "data" / "records.json",
        help="records.json to load (default: data/records.json)",
    )
    ap.add_argument(
        "--write", type=Path, default=None,
        help="If set, write the post-resolution records to this path. "
             "Production data/records.json is never modified by this tool.",
    )
    ap.add_argument(
        "--build-cache", action="store_true",
        help="Force rebuild of the DCAD address_index cache.",
    )
    ap.add_argument(
        "--verbose", action="store_true",
        help="Show per-record overwrite diffs in addition to summary.",
    )
    args = ap.parse_args()

    if args.build_cache:
        load_or_build_address_index(force_rebuild=True)
        print("DCAD address_index cache rebuilt.")
        return 0

    # Load records
    records = load_records(args.records)
    report_baseline(records)

    # Load DCAD address_index (cached)
    address_index = load_or_build_address_index()

    # Run requested path(s) — note: when --path all, B runs first, then
    # variant lookup runs over remaining unresolved records (pipeline order).
    if args.path in ("B", "all"):
        diff = run_path_b(records, address_index, verbose=args.verbose)
        report_path_b(diff, verbose=args.verbose)

    if args.path in ("variant", "all"):
        diff = run_variant_lookup(records, address_index, verbose=args.verbose)
        report_variant_lookup(diff, verbose=args.verbose)

    # Write enriched records if requested
    if args.write:
        write_records_with_envelope(records, args.write)
        print(f"\nWrote enriched records to {args.write}")
        print("Compare with: diff <(jq -S . data/records.json) "
              f"<(jq -S . {args.write})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
