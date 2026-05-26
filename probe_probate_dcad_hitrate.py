"""Measure DCAD hit rate for re:SearchTX probate decedents (v2).

Standalone recon — does NOT modify the pipeline. Bypasses the
production ``match_debtor_to_dcad`` (which has an internal rotation
designed for bankruptcy RSS format and produces surname-dropped
false positives on Tyler's "LAST, FIRST MIDDLE" inputs).

What this probe does:

  1. Reads data/records.json, filters to source=probate.txcourts.gov
  2. Loads DCAD bulk data from ~/.dcad_cache
  3. Builds two indexes:
       - primary_index   from ACCOUNT_INFO (one owner per row, with
         joint-owner & splits)
       - secondary_index from MULTI_OWNER  (extra co-owners per account)
  4. For each decedent name:
       - Normalizes it (strip punct, suffixes, etc.)
       - Detects whether Tyler emitted it WITH a comma ("LAST, FIRST")
         or WITHOUT ("FIRST MIDDLE LAST" — the rare outlier)
       - For comma-case: try the normalized key directly
       - For no-comma case: try BOTH the normalized key AND a rotated
         form (move last token to front, mimicking the bankruptcy
         converter — but applied at the caller, not silently inside)
       - Uses a SURNAME-PRESERVING tier ladder:
            Tier 1: exact   (LAST FIRST MIDDLE)
            Tier 2: drop middle (LAST FIRST)
         NO Tier 3. Dropping anything past the first name produces too
         many surname-less false positives (e.g. "DONALD W" matches
         random people).
  5. Reports separate hit counts for primary, secondary, combined.
  6. Sample hits show the FULL matched DCAD owner name (not just the
     lookup key) so visual sanity check is easy. Multi-account matches
     (>3 accounts) are flagged as likely common-name pollution.

Usage:
    python probe_probate_dcad_hitrate.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


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
    decedents = [
        (r.get("instrument_num"), r.get("grantor"))
        for r in rsxtx if r.get("grantor")
    ]
    print(f"re:SearchTX records:        {len(rsxtx)}")
    print(f"with decedent name (grantor): {len(decedents)}")
    if not decedents:
        return 2

    print("\nLoading DCAD bulk data...")
    try:
        from scraper.dcad_bulk import fetch_dcad_zip, parse_dcad_tables, build_account_index
        from scraper.dcad_owner_index import normalize_dcad_owner_name, expand_joint_owners
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
    print(f"DCAD tables loaded: {sorted(tables.keys())}")

    # Build PRIMARY index from ACCOUNT_INFO (one owner per row)
    primary_index, primary_full_name = _build_index_from_df(
        tables.get("ACCOUNT_INFO"),
        owner_col_candidates=("OWNER_NAME1", "OWNER_NAME", "OWNER1_NAME"),
        account_col_candidates=("ACCOUNT_NUM", "ACCT_NUM"),
        label="primary",
    )

    # Build SECONDARY index from MULTI_OWNER
    multi_df = tables.get("MULTI_OWNER")
    if multi_df is None or multi_df.empty:
        print("WARNING: MULTI_OWNER table missing or empty")
        secondary_index: dict[str, list[str]] = {}
        secondary_full_name: dict[str, str] = {}
    else:
        # Inspect columns so we pick the right ones for MULTI_OWNER
        print(f"MULTI_OWNER columns: {list(multi_df.columns)}")
        secondary_index, secondary_full_name = _build_index_from_df(
            multi_df,
            owner_col_candidates=("OWNER_NAME", "OWNER_NAME1", "NAME", "MULTI_OWNER_NAME"),
            account_col_candidates=("ACCOUNT_NUM", "ACCT_NUM"),
            label="secondary",
        )

    account_index = build_account_index(tables)
    print(f"primary_index   entries: {len(primary_index):,}")
    print(f"secondary_index entries: {len(secondary_index):,}")
    print(f"account_index   entries: {len(account_index):,}")

    # Run matches
    hits_primary: list[dict] = []
    hits_secondary_only: list[dict] = []
    misses: list[dict] = []

    for rid, name in decedents:
        keys = _candidate_keys(name)  # list of (key, source: 'asis'|'rotated')
        result = None

        for key, key_source in keys:
            m = _lookup_with_tiers(key, primary_index, primary_full_name)
            if m:
                result = {
                    "case_number": rid, "decedent": name,
                    "lookup_key": key, "key_source": key_source,
                    "tier": m["tier"], "matched_name": m["matched_name"],
                    "matched_dcad_owner_raw": m["matched_full"],
                    "accounts": m["accounts"], "found_in": "primary",
                }
                break
        if result is None:
            for key, key_source in keys:
                m = _lookup_with_tiers(key, secondary_index, secondary_full_name)
                if m:
                    result = {
                        "case_number": rid, "decedent": name,
                        "lookup_key": key, "key_source": key_source,
                        "tier": m["tier"], "matched_name": m["matched_name"],
                        "matched_dcad_owner_raw": m["matched_full"],
                        "accounts": m["accounts"], "found_in": "secondary",
                    }
                    break

        if result is None:
            misses.append({
                "case_number": rid, "decedent": name,
                "tried_keys": keys,
            })
        elif result["found_in"] == "primary":
            hits_primary.append(result)
        else:
            hits_secondary_only.append(result)

    total = len(decedents)
    n_primary = len(hits_primary)
    n_secondary = len(hits_secondary_only)
    n_either = n_primary + n_secondary

    print()
    print("=" * 70)
    print("HIT-RATE SUMMARY (surname-preserving tiers, no rotation FP)")
    print("=" * 70)
    print(f"  ACCOUNT_INFO (primary) hits:        {n_primary:3d}/{total} ({100*n_primary/total:.0f}%)")
    print(f"  MULTI_OWNER-only (secondary) hits:  {n_secondary:3d}/{total} ({100*n_secondary/total:.0f}%)")
    print(f"  Combined hit rate:                  {n_either:3d}/{total} ({100*n_either/total:.0f}%)")
    print(f"  Misses:                             {len(misses):3d}")

    # Account count histogram (1=single property, 2-3=normal multi, >3=suspect)
    print()
    print("Properties per matched decedent:")
    cnt = Counter()
    flagged_multi = []
    for h in hits_primary + hits_secondary_only:
        n = len(h["accounts"])
        cnt[min(n, 10)] += 1
        if n > 3:
            flagged_multi.append(h)
    for n in sorted(cnt):
        label = f">{n-1}" if n == 10 else str(n)
        print(f"    {label:>3} property: {cnt[n]:3d}")
    if flagged_multi:
        print(f"\n  WARNING: {len(flagged_multi)} match(es) have >3 properties — likely common-name pollution")
        for h in flagged_multi:
            print(f"    {h['decedent']!r} -> matched {h['matched_dcad_owner_raw']!r}  ({len(h['accounts'])} accts) tier={h['tier']}")

    # Sample hits with full property addresses
    print()
    print("=" * 70)
    print("SAMPLE HITS WITH PROPERTY ADDRESSES")
    print("=" * 70)
    for h in (hits_primary + hits_secondary_only)[:15]:
        print(f"\n  case={h['case_number']}  ({h['found_in']})")
        print(f"  decedent:           {h['decedent']!r}")
        print(f"  lookup key:         {h['lookup_key']!r}  ({h['key_source']}, tier={h['tier']})")
        print(f"  matched DCAD owner: {h['matched_dcad_owner_raw']!r}")
        for acc in h["accounts"][:3]:
            ai = account_index.get(acc, {})
            print(f"    {acc}  {ai.get('address_normalized', '?')!r}  "
                  f"city={ai.get('address_city')}  zip={ai.get('address_zip')}")

    # Sample misses
    print()
    print("=" * 70)
    print(f"SAMPLE MISSES (first 12 of {len(misses)})")
    print("=" * 70)
    for m in misses[:12]:
        print(f"  case={m['case_number']}  decedent={m['decedent']!r}")
        for k, src in m["tried_keys"]:
            print(f"    tried {src:8s}: {k!r}")

    return 0


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _build_index_from_df(df, *, owner_col_candidates, account_col_candidates, label: str):
    """Build {normalized_owner: [account_nums]} and {normalized_owner: full_raw_owner_string} from a DataFrame."""
    if df is None or df.empty:
        return {}, {}
    from scraper.dcad_owner_index import normalize_dcad_owner_name, expand_joint_owners

    owner_col = next((c for c in owner_col_candidates if c in df.columns), None)
    account_col = next((c for c in account_col_candidates if c in df.columns), None)
    if not owner_col or not account_col:
        print(f"WARNING: {label} index — could not find owner/account columns. "
              f"have={list(df.columns)} owner_candidates={owner_col_candidates} "
              f"account_candidates={account_col_candidates}")
        return {}, {}
    print(f"{label} index: using owner_col={owner_col!r} account_col={account_col!r}")

    index: dict[str, list[str]] = defaultdict(list)
    full_name: dict[str, str] = {}  # normalized -> first raw owner string seen (for display)

    for raw_owner, account in zip(df[owner_col], df[account_col]):
        if not raw_owner or not account:
            continue
        raw_owner = str(raw_owner)
        account = str(account)
        for individual in expand_joint_owners(raw_owner):
            normalized = normalize_dcad_owner_name(individual)
            if not normalized:
                continue
            index[normalized].append(account)
            full_name.setdefault(normalized, individual)

    return dict(index), full_name


def _candidate_keys(name: str) -> list[tuple[str, str]]:
    """Return list of (key, source) tuples to try for a Tyler decedent name.

    Tyler convention: "LAST, FIRST MIDDLE" (with comma).
    Outlier:           "FIRST MIDDLE LAST" (no comma; rare).

    For the comma form, the normalized key is already in DCAD format
    (LAST FIRST MIDDLE) — try it as-is.
    For the no-comma form, we don't know which is the surname, so try
    BOTH: as-is (assume already DCAD format, rarely true) AND rotated
    (assume FIRST MIDDLE LAST, surname is last token, rotate to front).
    """
    from scraper.dcad_owner_index import normalize_dcad_owner_name
    has_comma = "," in (name or "")
    norm = normalize_dcad_owner_name(name)
    out: list[tuple[str, str]] = []
    if norm:
        out.append((norm, "asis"))
    if not has_comma and norm:
        # Try rotated (move last token to front) — Tyler's no-comma outlier
        tokens = norm.split()
        if len(tokens) >= 2:
            rotated = " ".join([tokens[-1]] + tokens[:-1])
            if rotated != norm:
                out.append((rotated, "rotated"))
    return out


def _lookup_with_tiers(key: str, index: dict[str, list[str]], full_name: dict[str, str]) -> Optional[dict]:
    """Surname-preserving tier ladder. Never drops the surname.

    Tier 1: exact (full key)
    Tier 2: drop middle name (first two tokens only — surname + first name)
            Only fires when key has 3+ tokens.
    """
    # Tier 1: exact
    if key in index:
        return {
            "tier": "exact",
            "matched_name": key,
            "matched_full": full_name.get(key, key),
            "accounts": list(index[key]),
        }
    # Tier 2: no_middle
    tokens = key.split()
    if len(tokens) >= 3:
        no_middle = f"{tokens[0]} {tokens[1]}"
        if no_middle in index:
            return {
                "tier": "no_middle",
                "matched_name": no_middle,
                "matched_full": full_name.get(no_middle, no_middle),
                "accounts": list(index[no_middle]),
            }
    return None


if __name__ == "__main__":
    sys.exit(main())
