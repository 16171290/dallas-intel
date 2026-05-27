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

CACHE_DIR         = REPO_ROOT / "data" / "cache"
CACHE_FILE        = CACHE_DIR / "dcad_address_index.pkl.gz"
PATH_A_CACHE_FILE = CACHE_DIR / "dcad_path_a_indexes.pkl.gz"
PATH_C_CACHE_FILE = CACHE_DIR / "dcad_path_c_indexes.pkl.gz"


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


def build_path_a_indexes(zip_path: Path) -> dict:
    """Build the three indexes Path A needs from a DCAD ZIP.

    Returns ``{owner_index, account_owner_lookup, account_address_index}``.
    """
    from scraper import dcad_bulk, dcad_owner_index, legal_resolver

    logger.info("Parsing DCAD tables for Path A indexes...")
    t0 = time.perf_counter()
    tables = dcad_bulk.parse_dcad_tables(zip_path)

    owner_index           = dcad_owner_index.build_owner_index(tables)
    account_owner_lookup  = dcad_owner_index.build_account_to_owner_name(tables)
    account_address_index = legal_resolver._build_account_to_address(tables)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Built Path A indexes: owner_index=%d, account_owner_lookup=%d, "
        "account_address_index=%d in %.1fs",
        len(owner_index), len(account_owner_lookup),
        len(account_address_index), elapsed,
    )
    return {
        "owner_index":           owner_index,
        "account_owner_lookup":  account_owner_lookup,
        "account_address_index": account_address_index,
    }


def build_path_c_indexes(zip_path: Path) -> dict:
    """Build the indexes Path C needs: legal_index + acct_to_addr."""
    from scraper import dcad_bulk, legal_resolver

    logger.info("Parsing DCAD tables for Path C indexes...")
    t0 = time.perf_counter()
    tables       = dcad_bulk.parse_dcad_tables(zip_path)
    legal_index  = legal_resolver.build_legal_index(tables)
    acct_to_addr = legal_resolver._build_account_to_address(tables)
    elapsed = time.perf_counter() - t0
    logger.info(
        "Built Path C indexes: legal_index=%d keys, acct_to_addr=%d in %.1fs",
        len(legal_index), len(acct_to_addr), elapsed,
    )
    return {"legal_index": legal_index, "acct_to_addr": acct_to_addr}


def load_or_build_path_c_indexes(force_rebuild: bool = False) -> dict:
    """Load Path C's indexes from cache; build from DCAD ZIP otherwise."""
    if not force_rebuild and PATH_C_CACHE_FILE.exists():
        logger.info("Loading cached Path C indexes from %s", PATH_C_CACHE_FILE)
        t0 = time.perf_counter()
        with gzip.open(PATH_C_CACHE_FILE, "rb") as f:
            indexes = pickle.load(f)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Loaded Path C indexes: legal_index=%d, acct_to_addr=%d in %.1fs",
            len(indexes["legal_index"]),
            len(indexes["acct_to_addr"]),
            elapsed,
        )
        return indexes

    zip_path = fetch_dcad_zip_or_die()
    indexes  = build_path_c_indexes(zip_path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Writing Path C cache to %s", PATH_C_CACHE_FILE)
    with gzip.open(PATH_C_CACHE_FILE, "wb") as f:
        pickle.dump(indexes, f, protocol=pickle.HIGHEST_PROTOCOL)
    return indexes


def load_or_build_path_a_indexes(force_rebuild: bool = False) -> dict:
    """Load Path A's indexes from cache; build from DCAD ZIP if missing
    or force_rebuild=True. Cache lives alongside the address_index cache."""
    if not force_rebuild and PATH_A_CACHE_FILE.exists():
        logger.info("Loading cached Path A indexes from %s", PATH_A_CACHE_FILE)
        t0 = time.perf_counter()
        with gzip.open(PATH_A_CACHE_FILE, "rb") as f:
            indexes = pickle.load(f)
        elapsed = time.perf_counter() - t0
        logger.info(
            "Loaded Path A indexes: owner_index=%d, account_owner_lookup=%d, "
            "account_address_index=%d in %.1fs",
            len(indexes["owner_index"]),
            len(indexes["account_owner_lookup"]),
            len(indexes["account_address_index"]),
            elapsed,
        )
        return indexes

    zip_path = fetch_dcad_zip_or_die()
    indexes  = build_path_a_indexes(zip_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Writing Path A cache to %s", PATH_A_CACHE_FILE)
    with gzip.open(PATH_A_CACHE_FILE, "wb") as f:
        pickle.dump(indexes, f, protocol=pickle.HIGHEST_PROTOCOL)
    return indexes


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
               verbose: bool = False, *,
               always_run: bool = False) -> dict:
    """Run Stage 6.3 — Path B and return diff stats + per-record changes."""
    from scraper import resolution_paths
    from scraper.resolution import get_history, get_warnings

    before = _snapshot_before(records)
    stats = resolution_paths.run_path_b(records, address_index, always_run=always_run)
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


def run_path_a(records: list[dict], path_a_indexes: dict,
               verbose: bool = False, *,
               always_run: bool = False) -> dict:
    """Run Stage 6.45 — Path A (NOF grantor → DCAD owner_index)."""
    from scraper import resolution_paths
    from scraper.resolution import get_warnings

    before = _snapshot_before(records)
    stats = resolution_paths.run_path_a(
        records,
        path_a_indexes["owner_index"],
        path_a_indexes["account_address_index"],
        path_a_indexes["account_owner_lookup"],
        always_run=always_run,
    )
    newly_matched, overwrote_address = _diff_records(records, before, get_warnings)

    return {
        "stats": {
            "total_records":            stats.total_records,
            "skipped_not_nof":          stats.skipped_not_nof,
            "skipped_already_resolved": stats.skipped_already_resolved,
            "skipped_no_grantor":       stats.skipped_no_grantor,
            "skipped_boilerplate":      stats.skipped_boilerplate,
            "candidates":               stats.candidates,
            "matched":                  stats.matched,
            "multi_account_matched":    stats.multi_account_matched,
            "guarded":                  stats.guarded,
            "no_match":                 stats.no_match,
        },
        "newly_matched":     newly_matched,
        "overwrote_address": overwrote_address,
    }


def run_path_c(records: list[dict], path_c_indexes: dict,
               verbose: bool = False, *,
               always_run: bool = False) -> dict:
    """Run Stage 6.4 APN resolver + Stage 6.5 legal resolver as Path C.

    Both share the acct_to_addr index; legal resolver also needs legal_index.
    Returns a merged diff: stats from both stages plus per-record changes.
    """
    from scraper import legal_resolver
    from scraper.resolution import get_warnings

    before = _snapshot_before(records)
    apn_stats = legal_resolver.resolve_apn_to_address_with_indexes(
        records, path_c_indexes["acct_to_addr"], always_run=always_run,
    )
    legal_stats = legal_resolver.resolve_legal_descriptions_with_indexes(
        records,
        path_c_indexes["legal_index"],
        path_c_indexes["acct_to_addr"],
        always_run=always_run,
    )
    newly_matched, overwrote_address = _diff_records(records, before, get_warnings)

    return {
        "stats": {
            "apn": {
                "total":            apn_stats.total,
                "resolved":         apn_stats.resolved,
                "no_apn":           apn_stats.no_apn,
                "no_match":         apn_stats.no_match,
                "skipped_already_resolved": apn_stats.skipped_already_resolved,
            },
            "legal": {
                "total":            legal_stats.total,
                "matched_exact":    legal_stats.matched_exact,
                "matched_fuzzy":    legal_stats.matched_fuzzy,
                "resolved":         legal_stats.resolved,
                "no_parse":         legal_stats.no_parse,
                "no_match":         legal_stats.no_match,
                "multi_match":      legal_stats.multi_match,
                "no_snippet":       legal_stats.no_snippet,
                "skipped_already_resolved": legal_stats.skipped_already_resolved,
            },
        },
        "newly_matched":     newly_matched,
        "overwrote_address": overwrote_address,
    }


def report_path_c(diff: dict, verbose: bool) -> None:
    """Human-readable summary of Stage 6.4 + 6.5 Path C diff."""
    apn = diff["stats"]["apn"]
    leg = diff["stats"]["legal"]
    print()
    print("=" * 72)
    print("PATH C — APN resolver (6.4) + Legal resolver (6.5)")
    print("=" * 72)
    print(f"  Stage 6.4 (APN):")
    print(f"    Total considered           : {apn['total']}")
    print(f"    Skipped: dcad_account set  : {apn['skipped_already_resolved']}")
    print(f"    Resolved (APN -> account)  : {apn['resolved']}")
    print(f"    No APN in OCR              : {apn['no_apn']}")
    print(f"    APN no match               : {apn['no_match']}")
    print(f"  Stage 6.5 (Legal resolver):")
    print(f"    Total considered           : {leg['total']}")
    print(f"    Skipped: dcad_account set  : {leg['skipped_already_resolved']}")
    print(f"    Matched (exact)            : {leg['matched_exact']}")
    print(f"    Matched (fuzzy subdivision): {leg['matched_fuzzy']}")
    print(f"    Resolved total             : {leg['resolved']}")
    print(f"    No subdivision parsed      : {leg['no_parse']}")
    print(f"    No match                   : {leg['no_match']}")
    print(f"    Multi-account skipped      : {leg['multi_match']}")
    print(f"    No raw_excerpt             : {leg['no_snippet']}")
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


# ═══════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════

def run_variant_lookup(records: list[dict], address_index: dict[str, str],
                       verbose: bool = False, *,
                       always_run: bool = False) -> dict:
    """Run Stage 6.35 — Variant lookup (Class 26 family)."""
    from scraper import address_variants, resolution_paths
    from scraper.resolution import get_warnings

    before = _snapshot_before(records)
    fuzzy_index = address_variants.build_fuzzy_index(address_index)
    stats = resolution_paths.run_variant_lookup(
        records, address_index, fuzzy_index, always_run=always_run,
    )
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


def report_path_a(diff: dict, verbose: bool) -> None:
    """Human-readable summary of Stage 6.45 Path A diff."""
    s = diff["stats"]
    print()
    print("=" * 72)
    print("STAGE 6.45 — Path A (NOF grantor -> DCAD owner_index)")
    print("=" * 72)
    print(f"  Total records considered     : {s['total_records']}")
    print(f"  Skipped: not NOF (PB/AJ)     : {s['skipped_not_nof']}")
    print(f"  Skipped: dcad_account set    : {s['skipped_already_resolved']}")
    print(f"  Skipped: no grantor          : {s['skipped_no_grantor']}")
    print(f"  Skipped: boilerplate grantor : {s['skipped_boilerplate']}")
    print(f"  Path A candidates            : {s['candidates']}")
    print(f"    Matched (single account)   : {s['matched']}")
    print(f"    Multi-account (alternates) : {s['multi_account_matched']}")
    print(f"    Guarded (Class 19a)        : {s['guarded']}")
    print(f"    No match                   : {s['no_match']}")
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


def report_stage_6_6(stats, records: list[dict], verbose: bool) -> None:
    """Human-readable summary of Stage 6.6 (cross-path agreement) diff."""
    from scraper.resolution import (
        get_history,
        get_warnings,
        PATH_STAGE_6_6,
        WARN_PATH_AGREEMENT,
        WARN_PATH_DISAGREEMENT,
        WARN_TIEBROKEN_BY_PAGE,
        WARN_PAGE_TIEBREAK_INCONCLUSIVE,
    )
    print()
    print("=" * 72)
    print("STAGE 6.6 — Cross-path agreement + page tiebreaker")
    print("=" * 72)
    print(f"  Total records considered     : {stats.total_records}")
    print(f"  No paths matched             : {stats.no_match_records}")
    print(f"  Single path matched          : {stats.single_path_records}")
    print(f"  Multi-path AGREEMENT         : {stats.agreement_records}")
    print(f"  Multi-path DISAGREEMENT      : {stats.disagreement_records}")
    print(f"    Page fetches attempted     : {stats.pages_fetched}")
    print(f"    Tiebroken by page          : {stats.tiebroken}")
    print(f"    Inconclusive (no signal)   : {stats.inconclusive}")
    print()

    # Surface disagreements + agreements for inspection
    agreements   = [r for r in records if WARN_PATH_AGREEMENT in get_warnings(r)]
    disagreements = [r for r in records if WARN_PATH_DISAGREEMENT in get_warnings(r)]

    if agreements:
        print(f"AGREEMENTS — {len(agreements)} records (high confidence):")
        for r in agreements[:10]:
            paths = sorted({h["path"] for h in get_history(r)
                            if h.get("status") in ("matched", "multi_match")
                            and h.get("dcad_account")})
            print(f"  {r.get('record_id')} grantor={r.get('grantor')!r} "
                  f"acct={r.get('dcad_account')} paths={paths}")
        if len(agreements) > 10:
            print(f"  ... and {len(agreements) - 10} more")
        print()

    if disagreements:
        print(f"DISAGREEMENTS — {len(disagreements)} records "
              f"(operator should verify):")
        for r in disagreements:
            history = get_history(r)
            picks: dict[str, list[str]] = {}
            for h in history:
                if h.get("status") in ("matched", "multi_match") and h.get("dcad_account"):
                    picks.setdefault(h["dcad_account"], []).append(h["path"])
            tiebroken = WARN_TIEBROKEN_BY_PAGE in get_warnings(r)
            inconclusive = WARN_PAGE_TIEBREAK_INCONCLUSIVE in get_warnings(r)
            status = ("TIEBROKEN" if tiebroken
                     else "INCONCLUSIVE" if inconclusive
                     else "UNRESOLVED")
            print(f"  {r.get('record_id')} grantor={r.get('grantor')!r}")
            print(f"    current acct: {r.get('dcad_account')} "
                  f"addr: {r.get('address_normalized')!r}")
            for acct, paths in picks.items():
                print(f"    pick: {acct}  by {sorted(paths)}")
            print(f"    status: {status}")
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
        "--path", choices=["A", "B", "C", "variant", "all"], default="all",
        help="Which resolution path(s) to validate. "
             "B = Path B raw_excerpt fallback (Stage 6.3). "
             "variant = Class 26 family fuzzy lookup (Stage 6.35). "
             "A = Path A NOF grantor -> owner_index (Stage 6.45). "
             "C = Path C APN + legal-description resolver (Stages 6.4 + 6.5). "
             "all = run B, variant, A, then C in pipeline order.",
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
    ap.add_argument(
        "--stage_6_6", action="store_true",
        help="After all paths run, run Stage 6.6 (cross-path agreement + "
             "page tiebreaker). Implies --path all and switches every path "
             "into always_run mode so disagreements surface in history.",
    )
    ap.add_argument(
        "--fetch-pages", action="store_true",
        help="Stage 6.6: actually fetch publicsearch.us /doc/ pages via "
             "Playwright. Default is dry-run (logs disagreements only).",
    )
    args = ap.parse_args()

    # Stage 6.6 needs full pipeline + always_run mode to see anything.
    if args.stage_6_6:
        if args.path != "all":
            logger.info("Stage 6.6 requires full pipeline; forcing --path all")
        args.path = "all"

    if args.build_cache:
        load_or_build_address_index(force_rebuild=True)
        load_or_build_path_a_indexes(force_rebuild=True)
        load_or_build_path_c_indexes(force_rebuild=True)
        print("DCAD address_index + Path A + Path C indexes caches rebuilt.")
        return 0

    # Load records
    records = load_records(args.records)
    report_baseline(records)

    # Load DCAD address_index (cached). Only paths that need it touch the
    # disk; Paths A/C need different indexes.
    address_index = None
    if args.path in ("B", "variant", "all"):
        address_index = load_or_build_address_index()

    # When Stage 6.6 is requested, all paths must run in diagnostic mode
    # so disagreements appear in resolution_history (else nothing for
    # Stage 6.6 to audit).
    always_run = args.stage_6_6

    # Run requested path(s) — when --path all, pipeline order is
    # B -> variant -> A -> C (mirrors main.py stages
    # 6.3 -> 6.35 -> 6.45 -> 6.4 + 6.5).
    if args.path in ("B", "all"):
        diff = run_path_b(records, address_index,
                          verbose=args.verbose, always_run=always_run)
        report_path_b(diff, verbose=args.verbose)

    if args.path in ("variant", "all"):
        diff = run_variant_lookup(records, address_index,
                                  verbose=args.verbose, always_run=always_run)
        report_variant_lookup(diff, verbose=args.verbose)

    if args.path in ("A", "all"):
        path_a_indexes = load_or_build_path_a_indexes()
        diff = run_path_a(records, path_a_indexes,
                          verbose=args.verbose, always_run=always_run)
        report_path_a(diff, verbose=args.verbose)

    if args.path in ("C", "all"):
        path_c_indexes = load_or_build_path_c_indexes()
        diff = run_path_c(records, path_c_indexes,
                          verbose=args.verbose, always_run=always_run)
        report_path_c(diff, verbose=args.verbose)

    if args.path == "all" and args.stage_6_6:
        # Stage 6.6 only makes sense after all paths have populated history.
        # We need the acct -> address map; Path C cache already has it.
        from scraper import stage_6_6_agreement, page_fetcher as pf
        path_c_indexes = load_or_build_path_c_indexes()
        acct_to_addr = path_c_indexes["acct_to_addr"]

        if args.fetch_pages:
            logger.info("Stage 6.6: real Playwright page fetches enabled")
            with pf.PageFetcher() as fetcher:
                stats = stage_6_6_agreement.run_stage_6_6(
                    records, acct_to_addr, page_fetcher=fetcher,
                )
        else:
            logger.info("Stage 6.6: dry-run (no page fetches; "
                       "disagreements logged but not auto-resolved)")
            stats = stage_6_6_agreement.run_stage_6_6(
                records, acct_to_addr, page_fetcher=None,
            )
        report_stage_6_6(stats, records, verbose=args.verbose)

    # Write enriched records if requested
    if args.write:
        write_records_with_envelope(records, args.write)
        print(f"\nWrote enriched records to {args.write}")
        print("Compare with: diff <(jq -S . data/records.json) "
              f"<(jq -S . {args.write})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
