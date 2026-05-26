"""Measure DCAD hit rate for re:SearchTX probate decedents (v3).

Adds to v2:
  - Tier 3: initial-form (LAST FIRST INITIAL_OF_MIDDLE), surname-preserving
  - Tier 4: surname-only fallback, gated on uniqueness (<=2 accounts)
            so we catch family-trust collapses like "KLAUSING FAMILY TRUST"
            -> normalized to just "KLAUSING" -> 1 account in DCAD
            WITHOUT producing the Wilson disaster (lots of unrelated
            Robert Wilsons under just "WILSON ROBERT").
  - Per-miss diagnostic dump: lists every primary_index entry that
    starts with the decedent's surname (up to 20). Lets us eyeball
    whether the miss is "no Dallas property" vs "name variant we
    don't catch."
  - MULTI_OWNER raw-sample dump: prints 10 raw rows so we can see why
    secondary index gave 0 hits (suspicious for 100K entries).

Hit reporting now breaks down by tier so we can see which strategies
add real signal vs marginal junk.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional


# Surname-only fallback is risky for common surnames. Cap the number
# of accounts the surname can map to before we accept it. 2 is a
# reasonable guess for "Dallas-uncommon enough that this is probably
# our decedent's family trust."
SURNAME_ONLY_MAX_ACCOUNTS = 2


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

    # MULTI_OWNER raw sample BEFORE building indexes — sanity check the data
    multi_df = tables.get("MULTI_OWNER")
    if multi_df is not None and not multi_df.empty:
        print(f"\nMULTI_OWNER columns: {list(multi_df.columns)}")
        print(f"MULTI_OWNER row count: {len(multi_df):,}")
        print("MULTI_OWNER raw sample (first 10 rows with non-empty OWNER_NAME):")
        sub = multi_df[multi_df["OWNER_NAME"].astype(str).str.strip() != ""].head(10)
        for row in sub.itertuples(index=False):
            print(f"  acct={getattr(row, 'ACCOUNT_NUM', '?')!r}  "
                  f"seq={getattr(row, 'OWNER_SEQ_NUM', '?')!r}  "
                  f"name={getattr(row, 'OWNER_NAME', '?')!r}  "
                  f"pct={getattr(row, 'OWNERSHIP_PCT', '?')!r}")

    # Build indexes
    primary_index, primary_full_name = _build_index_from_df(
        tables.get("ACCOUNT_INFO"),
        owner_col_candidates=("OWNER_NAME1", "OWNER_NAME", "OWNER1_NAME"),
        account_col_candidates=("ACCOUNT_NUM", "ACCT_NUM"),
        label="primary",
    )

    secondary_index, secondary_full_name = _build_index_from_df(
        multi_df,
        owner_col_candidates=("OWNER_NAME", "OWNER_NAME1", "NAME"),
        account_col_candidates=("ACCOUNT_NUM",),
        label="secondary",
    )

    account_index = build_account_index(tables)
    print(f"\nprimary_index   entries: {len(primary_index):,}")
    print(f"secondary_index entries: {len(secondary_index):,}")
    print(f"account_index   entries: {len(account_index):,}")

    # Build {surname: [keys]} map once for fast per-miss diagnostic dump
    surname_to_keys: dict[str, list[str]] = defaultdict(list)
    for key in primary_index:
        first_tok = key.split(maxsplit=1)[0] if key else None
        if first_tok:
            surname_to_keys[first_tok].append(key)

    # Run matches — try every candidate key against the full tier ladder
    hits_primary: list[dict] = []
    hits_secondary_only: list[dict] = []
    misses: list[dict] = []
    tier_hits: Counter = Counter()
    surname_only_skipped_common: list[tuple] = []  # for diagnostic

    for instrument_num, name in decedents:
        keys = _candidate_keys(name)
        result = None

        for key, key_source in keys:
            m = _lookup_with_tiers(key, primary_index, primary_full_name, surname_to_keys)
            if m:
                result = {
                    "case_number": instrument_num, "decedent": name,
                    "lookup_key": key, "key_source": key_source,
                    "tier": m["tier"], "matched_name": m["matched_name"],
                    "matched_dcad_owner_raw": m["matched_full"],
                    "accounts": m["accounts"], "found_in": "primary",
                }
                break
            if m is False:
                # Sentinel: surname-only would have hit, but uniqueness gate rejected.
                # Track for diagnostic.
                surname_only_skipped_common.append((name, key, len(surname_to_keys.get(key.split()[0], []))))

        if result is None:
            for key, key_source in keys:
                m = _lookup_with_tiers(key, secondary_index, secondary_full_name, None)
                if m:
                    result = {
                        "case_number": instrument_num, "decedent": name,
                        "lookup_key": key, "key_source": key_source,
                        "tier": m["tier"], "matched_name": m["matched_name"],
                        "matched_dcad_owner_raw": m["matched_full"],
                        "accounts": m["accounts"], "found_in": "secondary",
                    }
                    break

        if result is None:
            misses.append({
                "case_number": instrument_num, "decedent": name,
                "tried_keys": keys,
            })
        elif result["found_in"] == "primary":
            hits_primary.append(result)
            tier_hits[result["tier"]] += 1
        else:
            hits_secondary_only.append(result)
            tier_hits[f"secondary/{result['tier']}"] += 1

    total = len(decedents)
    n_primary = len(hits_primary)
    n_secondary = len(hits_secondary_only)
    n_either = n_primary + n_secondary

    print()
    print("=" * 70)
    print("HIT-RATE SUMMARY (v3: + initial_form, + surname_only with uniqueness)")
    print("=" * 70)
    print(f"  ACCOUNT_INFO (primary) hits:        {n_primary:3d}/{total} ({100*n_primary/total:.0f}%)")
    print(f"  MULTI_OWNER-only (secondary) hits:  {n_secondary:3d}/{total} ({100*n_secondary/total:.0f}%)")
    print(f"  Combined hit rate:                  {n_either:3d}/{total} ({100*n_either/total:.0f}%)")
    print(f"  Misses:                             {len(misses):3d}")

    print("\nTier contribution:")
    for tier, n in sorted(tier_hits.items(), key=lambda kv: -kv[1]):
        print(f"    {tier:>30s}: {n:3d}")

    if surname_only_skipped_common:
        print(f"\nSurname-only rejected (too common, >{SURNAME_ONLY_MAX_ACCOUNTS} accounts):")
        for name, key, n_accts in surname_only_skipped_common[:10]:
            print(f"    {name!r}  (surname maps to {n_accts} accounts)")

    # Account count histogram
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
        print(f"\n  WARNING: {len(flagged_multi)} match(es) have >3 properties — common-name pollution")
        for h in flagged_multi:
            print(f"    {h['decedent']!r} -> matched {h['matched_dcad_owner_raw']!r}  ({len(h['accounts'])} accts) tier={h['tier']}")

    # Sample hits
    print()
    print("=" * 70)
    print("SAMPLE HITS WITH PROPERTY ADDRESSES")
    print("=" * 70)
    for h in (hits_primary + hits_secondary_only)[:18]:
        print(f"\n  case={h['case_number']}  ({h['found_in']})")
        print(f"  decedent:           {h['decedent']!r}")
        print(f"  lookup key:         {h['lookup_key']!r}  ({h['key_source']}, tier={h['tier']})")
        print(f"  matched DCAD owner: {h['matched_dcad_owner_raw']!r}")
        for acc in h["accounts"][:3]:
            ai = account_index.get(acc, {})
            print(f"    {acc}  {ai.get('address_normalized', '?')!r}  "
                  f"city={ai.get('address_city')}  zip={ai.get('address_zip')}")

    # Misses with surname diagnostic dump
    print()
    print("=" * 70)
    print(f"MISSES WITH DCAD-SURNAME DIAGNOSTIC ({len(misses)} total)")
    print("=" * 70)
    print("For each miss, shows up to 20 primary_index entries starting with the")
    print("decedent's surname. Use to eyeball whether DCAD has them under a")
    print("variant name (married name, nickname, etc.) or truly nothing.")
    for m in misses:
        print(f"\n  case={m['case_number']}  decedent={m['decedent']!r}")
        for k, src in m["tried_keys"]:
            print(f"    tried {src:8s}: {k!r}")
        # Diagnostic: show what's in the DCAD index for each candidate surname
        surnames_to_check = set()
        for k, _src in m["tried_keys"]:
            if k:
                first_tok = k.split()[0]
                if first_tok and len(first_tok) >= 3:
                    surnames_to_check.add(first_tok)
        for surname in sorted(surnames_to_check):
            entries = surname_to_keys.get(surname, [])
            if not entries:
                print(f"    DCAD entries starting with {surname!r}: NONE")
            else:
                print(f"    DCAD entries starting with {surname!r} ({len(entries)} total, showing first 20):")
                for e in sorted(entries)[:20]:
                    n_accts = len(primary_index.get(e, []))
                    print(f"        {e!r}  ({n_accts} acct{'s' if n_accts != 1 else ''})")

    return 0


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _build_index_from_df(df, *, owner_col_candidates, account_col_candidates, label: str):
    if df is None or df.empty:
        return {}, {}
    from scraper.dcad_owner_index import normalize_dcad_owner_name, expand_joint_owners

    owner_col = next((c for c in owner_col_candidates if c in df.columns), None)
    account_col = next((c for c in account_col_candidates if c in df.columns), None)
    if not owner_col or not account_col:
        print(f"WARNING: {label} index — could not find owner/account columns.")
        return {}, {}
    print(f"{label} index: using owner_col={owner_col!r} account_col={account_col!r}")

    index: dict[str, list[str]] = defaultdict(list)
    full_name: dict[str, str] = {}

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
    from scraper.dcad_owner_index import normalize_dcad_owner_name
    has_comma = "," in (name or "")
    norm = normalize_dcad_owner_name(name)
    out: list[tuple[str, str]] = []
    if norm:
        out.append((norm, "asis"))
    if not has_comma and norm:
        tokens = norm.split()
        if len(tokens) >= 2:
            rotated = " ".join([tokens[-1]] + tokens[:-1])
            if rotated != norm:
                out.append((rotated, "rotated"))
    return out


def _lookup_with_tiers(
    key: str,
    index: dict[str, list[str]],
    full_name: dict[str, str],
    surname_to_keys: Optional[dict[str, list[str]]],
) -> Optional[dict]:
    """Tier ladder. ALL tiers preserve the surname.

    Tier 1: exact match.
    Tier 2: no_middle    -> LAST FIRST
    Tier 3: initial_form -> LAST FIRST INITIAL_OF_MIDDLE
    Tier 4: surname_only -> LAST  (only if surname maps to <= N accounts)

    Returns dict on hit, None on miss, False if surname-only would have
    matched but was rejected for being too common (signal for diagnostic).
    """
    # Tier 1: exact
    if key in index:
        return {
            "tier": "exact",
            "matched_name": key,
            "matched_full": full_name.get(key, key),
            "accounts": list(index[key]),
        }

    tokens = key.split()

    # Tier 2: no_middle  (LAST FIRST)
    if len(tokens) >= 3:
        no_middle = f"{tokens[0]} {tokens[1]}"
        if no_middle in index:
            return {
                "tier": "no_middle",
                "matched_name": no_middle,
                "matched_full": full_name.get(no_middle, no_middle),
                "accounts": list(index[no_middle]),
            }

    # Tier 3: initial_form  (LAST FIRST INITIAL_OF_MIDDLE)
    if len(tokens) >= 3:
        middle_initial = tokens[2][:1]
        if middle_initial.isalpha():
            initial_form = f"{tokens[0]} {tokens[1]} {middle_initial}"
            if initial_form in index:
                return {
                    "tier": "initial_form",
                    "matched_name": initial_form,
                    "matched_full": full_name.get(initial_form, initial_form),
                    "accounts": list(index[initial_form]),
                }

    # Tier 4: surname_only (uniqueness-gated). Only for primary index.
    if surname_to_keys is not None and tokens:
        surname = tokens[0]
        if surname in index:
            accts = index[surname]
            if len(accts) <= SURNAME_ONLY_MAX_ACCOUNTS:
                return {
                    "tier": "surname_only",
                    "matched_name": surname,
                    "matched_full": full_name.get(surname, surname),
                    "accounts": list(accts),
                }
            else:
                # Surname-only would match but is too common — record for diagnostic
                return False  # sentinel

    return None


if __name__ == "__main__":
    sys.exit(main())
