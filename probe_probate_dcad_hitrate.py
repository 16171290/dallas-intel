"""Measure DCAD hit rate for re:SearchTX probate decedents.

Standalone recon — does NOT modify the pipeline. Reads the current
data/records.json (which already contains the re:SearchTX probate
records pulled by the latest cron), loads DCAD bulk data from the
local week-cached ZIP, and for each decedent name attempts a DCAD
owner-index lookup. Reports:

  - Total decedents tested
  - Hits via normalize-only path (the correct path for Tyler "LAST,FIRST" format)
  - Hits via bankruptcy-rotate path (only correct for Tyler's rare
    no-comma "FIRST MIDDLE LAST" outliers)
  - Hits via either path
  - Sample successful matches with property addresses
  - Sample misses (with normalized form, to help diagnose why)
  - DCAD account counts per hit (decedents who own multiple properties)

Usage (PowerShell, Windows)
---------------------------
    cd <repo>
    .\.venv\Scripts\Activate.ps1
    python probe_probate_dcad_hitrate.py

No env vars required. Uses ~/.dcad_cache/dcad-{year}-week{YYYYWW}.zip
that the cron already maintains. If the cache is missing or stale,
the script will attempt a fresh download via the same path the cron
uses (your home IP can reach dallascad.org; cloud sandboxes are
sometimes 403-blocked).

Exit codes:
    0  measurement completed (read the printed report)
    2  records.json missing or has no probate records
    3  DCAD data unavailable
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    records_path = Path("data/records.json")
    if not records_path.is_file():
        print(f"ERROR: {records_path} not found. Run from repo root.", file=sys.stderr)
        return 2

    data = json.loads(records_path.read_text(encoding="utf-8"))
    rsxtx = [
        r for r in data.get("records", [])
        if r.get("source") == "probate.txcourts.gov"
    ]
    decedents = [(r.get("record_id"), r.get("grantor")) for r in rsxtx if r.get("grantor")]
    print(f"re:SearchTX records:        {len(rsxtx)}")
    print(f"with decedent name (grantor): {len(decedents)}")
    if not decedents:
        print("Nothing to measure.", file=sys.stderr)
        return 2

    # Load DCAD via the same code path the cron uses, so we know the
    # measurement reflects what production would see.
    print("\nLoading DCAD bulk data (uses ~/.dcad_cache if warm)...")
    try:
        from scraper.dcad_bulk import fetch_dcad_zip, parse_dcad_tables, build_account_index
        from scraper.dcad_owner_index import build_owner_index, normalize_dcad_owner_name
        from scraper.name_matcher import match_debtor_to_dcad, convert_bankruptcy_to_dcad_format
    except ImportError as e:
        print(f"ERROR: failed to import scraper modules: {e}", file=sys.stderr)
        return 3

    try:
        zip_path = fetch_dcad_zip(year=2025)
    except Exception as e:
        print(f"ERROR: DCAD fetch/cache failed: {e}", file=sys.stderr)
        return 3

    print(f"DCAD ZIP: {zip_path}")
    tables = parse_dcad_tables(zip_path)

    # parse_dcad_tables returns DataFrames; build_owner_index expects
    # list[dict] (it's never called in production — main.py only uses
    # build_address_index). Convert the one table we need.
    account_info_df = tables.get("ACCOUNT_INFO")
    if account_info_df is None or account_info_df.empty:
        print("ERROR: ACCOUNT_INFO table missing from DCAD bundle", file=sys.stderr)
        return 3
    tables_as_dicts = {"ACCOUNT_INFO": account_info_df.to_dict("records")}
    owner_index   = build_owner_index(tables_as_dicts)
    account_index = build_account_index(tables)
    print(f"owner_index entries:   {len(owner_index):,}")
    print(f"account_index entries: {len(account_index):,}")

    # Run both candidate paths on every decedent
    hits_normalize_only: list[tuple] = []
    hits_bankruptcy_only: list[tuple] = []
    hits_both:            list[tuple] = []
    misses:               list[tuple] = []

    for rid, name in decedents:
        # Path A: normalize-only (correct for Tyler's "LAST, FIRST MIDDLE")
        key_a = normalize_dcad_owner_name(name)
        m_a   = match_debtor_to_dcad(key_a, owner_index) if key_a else None

        # Path B: bankruptcy rotate then normalize (correct for the rare
        # Tyler outlier that comes back as "FIRST MIDDLE LAST")
        key_b_pre = convert_bankruptcy_to_dcad_format(name)
        key_b     = normalize_dcad_owner_name(key_b_pre) if key_b_pre else None
        m_b       = match_debtor_to_dcad(key_b, owner_index) if key_b else None

        if m_a and m_b:
            hits_both.append((rid, name, m_a, m_b))
        elif m_a:
            hits_normalize_only.append((rid, name, m_a))
        elif m_b:
            hits_bankruptcy_only.append((rid, name, m_b))
        else:
            misses.append((rid, name, key_a, key_b))

    total = len(decedents)
    n_a    = len(hits_normalize_only) + len(hits_both)
    n_b    = len(hits_bankruptcy_only) + len(hits_both)
    n_any  = n_a + len(hits_bankruptcy_only)
    print()
    print("=" * 70)
    print("HIT-RATE SUMMARY")
    print("=" * 70)
    print(f"  normalize-only path:        {n_a:3d}/{total} ({100*n_a/total:.0f}%)")
    print(f"  bankruptcy-rotate path:     {n_b:3d}/{total} ({100*n_b/total:.0f}%)")
    print(f"  either path (recommended):  {n_any:3d}/{total} ({100*n_any/total:.0f}%)")
    print(f"  both paths agreed:          {len(hits_both):3d}")
    print(f"  misses:                     {len(misses):3d}")

    # Account counts — how many properties does each successful match own?
    all_hits = hits_normalize_only + hits_both + hits_bankruptcy_only
    print()
    print("Properties owned per successful match (decedent->DCAD):")
    from collections import Counter
    cnt = Counter()
    for h in all_hits:
        m = h[2] if len(h) >= 3 else None
        if m:
            cnt[min(len(m.accounts), 5)] += 1  # cap at 5 for the histogram
    for n in sorted(cnt):
        label = f"{n}" if n < 5 else "5+"
        print(f"    {label} account(s): {cnt[n]:3d}")

    # Sample successes with full addresses (this is the operator-visible output)
    print()
    print("=" * 70)
    print("SAMPLE HITS (decedent -> property address)")
    print("=" * 70)
    sample = (hits_both + hits_normalize_only + hits_bankruptcy_only)[:12]
    for h in sample:
        rid, name = h[0], h[1]
        m = h[2]
        print(f"\n  record={rid}")
        print(f"  decedent: {name!r}")
        print(f"  strategy: {m.match_strategy}  matched_dcad_owner={m.matched_name!r}  accounts={len(m.accounts)}")
        for acc in m.accounts[:3]:
            ai = account_index.get(acc, {})
            print(f"    {acc}  {ai.get('address_normalized', '?')!r}  "
                  f"city={ai.get('address_city')}  zip={ai.get('address_zip')}")

    # Sample misses with diagnostic info
    print()
    print("=" * 70)
    print("SAMPLE MISSES (first 10) — keys we tried but didn't match")
    print("=" * 70)
    for rid, name, key_a, key_b in misses[:10]:
        print(f"  decedent={name!r}")
        print(f"    normalize-only key:   {key_a!r}")
        print(f"    bankruptcy-rotate key: {key_b!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
