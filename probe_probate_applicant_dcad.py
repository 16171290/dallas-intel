"""Measure DCAD hit rate for re:SearchTX probate APPLICANTS (heirs).

Companion probe to ``probe_probate_dcad_hitrate.py``. That probe answered
"does the decedent own Dallas property, and where?" — this one answers
"does the applicant (the heir who filed the case) own Dallas property,
and what's their MAILING address?"

Why mailing instead of property:
  The applicant is who you'd contact about the estate. DCAD distinguishes
  the property address (where the parcel is) from the mailing address
  (where the tax bill is sent — could be a different home, P.O. box, or
  out-of-state address). For outreach you want the mailing address.

Uses the existing ``probate_matcher.match_decedent_to_dcad`` — the
function is name-agnostic, it just takes a name and an owner_index.
The "decedent" naming is historical; semantically it's a Tyler-format
name matcher.

Reports:
  - Hit count + percentage (expected lower than decedents — heirs tend
    to be younger and may not own Dallas property)
  - Sample hits showing the applicant's PROPERTY address vs MAILING
    address side-by-side, with a flag when they differ (out-of-area
    signal)
  - Sample misses
  - Distribution of where mailing addresses land (TX vs out-of-state)
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def main() -> int:
    records_path = Path("data/records.json")
    if not records_path.is_file():
        print(f"ERROR: {records_path} not found", file=sys.stderr)
        return 2

    data = json.loads(records_path.read_text(encoding="utf-8"))
    rsxtx = [
        r for r in data.get("records", [])
        if r.get("source") == "probate.txcourts.gov"
    ]
    # Tyler returns the applicant in the canonical record's `grantee` field.
    applicants = [
        (r.get("instrument_num"), r.get("grantee"), r.get("grantor"))
        for r in rsxtx if r.get("grantee")
    ]
    print(f"re:SearchTX probate records:     {len(rsxtx)}")
    print(f"with applicant name (grantee):    {len(applicants)}")
    if not applicants:
        return 2

    print("\nLoading DCAD bulk data...")
    try:
        from scraper.dcad_bulk import fetch_dcad_zip, parse_dcad_tables, build_account_index
        from scraper.dcad_owner_index import build_owner_index
        from scraper.probate_matcher import match_decedent_to_dcad
    except ImportError as e:
        print(f"ERROR: failed to import scraper modules: {e}", file=sys.stderr)
        return 3

    try:
        zip_path = fetch_dcad_zip(year=2025)
    except Exception as e:
        print(f"ERROR: DCAD fetch failed: {e}", file=sys.stderr)
        return 3

    print(f"DCAD ZIP: {zip_path}")
    tables = parse_dcad_tables(zip_path)
    owner_index = build_owner_index(tables)
    account_address_index = build_account_index(tables)
    print(f"owner_index entries: {len(owner_index):,}")

    # Build a mailing-address index: account_num -> {address_line1..4, city, state, zip}
    # using the same OWNER_ADDRESS_LINE1..4 / OWNER_CITY / OWNER_STATE / OWNER_ZIPCODE
    # columns the production enrich_record uses (scraper/enrichment.py:305-314).
    df = tables.get("ACCOUNT_INFO")
    mailing_index: dict[str, dict] = {}
    if df is not None and not df.empty:
        needed = ["ACCOUNT_NUM"]
        have = set(df.columns)
        mailing_cols = [c for c in (
            "OWNER_ADDRESS_LINE1", "OWNER_ADDRESS_LINE2",
            "OWNER_ADDRESS_LINE3", "OWNER_ADDRESS_LINE4",
            "OWNER_CITY", "OWNER_STATE", "OWNER_ZIPCODE",
        ) if c in have]
        if "ACCOUNT_NUM" in have and mailing_cols:
            for row in df[needed + mailing_cols].itertuples(index=False):
                acct = str(getattr(row, "ACCOUNT_NUM", "") or "").strip()
                if not acct:
                    continue
                lines = [
                    str(getattr(row, c, "") or "").strip()
                    for c in mailing_cols if c.startswith("OWNER_ADDRESS_LINE")
                ]
                mailing_index[acct] = {
                    "address": " ".join(l for l in lines if l) or None,
                    "city":    str(getattr(row, "OWNER_CITY",    "") or "").strip() or None,
                    "state":   str(getattr(row, "OWNER_STATE",   "") or "").strip() or None,
                    "zip":     str(getattr(row, "OWNER_ZIPCODE", "") or "").strip() or None,
                }
    print(f"mailing_index entries: {len(mailing_index):,}")

    # Run matches
    hits: list[dict] = []
    misses: list[dict] = []
    tier_counter: Counter = Counter()
    state_counter: Counter = Counter()
    differ_from_property = 0

    for instrument_num, applicant, decedent in applicants:
        match = match_decedent_to_dcad(applicant, owner_index)
        if match is None:
            misses.append({
                "case_number": instrument_num,
                "applicant":   applicant,
                "decedent":    decedent,
            })
            continue
        tier_counter[match.tier] += 1
        # Pull mailing + property address for the FIRST matched account
        primary_acct = match.accounts[0]
        mailing = mailing_index.get(primary_acct, {})
        property_addr = account_address_index.get(primary_acct, {})
        state_counter[mailing.get("state") or "?"] += 1
        prop_norm = property_addr.get("address_normalized")
        mail_addr = mailing.get("address")
        # Crude "different" test: does the mailing address even mention
        # the property's street?
        mail_differs_from_prop = bool(
            prop_norm and mail_addr
            and not _contains_property_street(mail_addr, prop_norm)
        )
        if mail_differs_from_prop:
            differ_from_property += 1
        hits.append({
            "case_number":      instrument_num,
            "applicant":        applicant,
            "decedent":         decedent,
            "tier":             match.tier,
            "matched_name":     match.matched_name,
            "n_accounts":       len(match.accounts),
            "warning":          match.warning,
            "primary_acct":     primary_acct,
            "property_address": prop_norm,
            "property_city":    property_addr.get("address_city"),
            "mailing_address":  mail_addr,
            "mailing_city":     mailing.get("city"),
            "mailing_state":    mailing.get("state"),
            "mailing_zip":      mailing.get("zip"),
            "mail_differs":     mail_differs_from_prop,
        })

    total = len(applicants)
    print()
    print("=" * 70)
    print("APPLICANT->DCAD HIT-RATE SUMMARY")
    print("=" * 70)
    print(f"  Hits:   {len(hits):3d}/{total} ({100*len(hits)/total:.0f}%)")
    print(f"  Misses: {len(misses):3d}/{total}")
    if hits:
        print(f"  Mailing address DIFFERS from property: {differ_from_property}/{len(hits)} "
              f"({100*differ_from_property/len(hits):.0f}%)")
        print()
        print("Tier contribution:")
        for tier, n in sorted(tier_counter.items(), key=lambda kv: -kv[1]):
            print(f"    {tier:>30s}: {n:3d}")
        print()
        print("Mailing state distribution (where DCAD sends tax bills):")
        for st, n in sorted(state_counter.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {st:>6s}: {n:3d}")

    print()
    print("=" * 70)
    print("SAMPLE HITS (applicant -> mailing address; property addr for context)")
    print("=" * 70)
    for h in hits[:15]:
        marker = " (DIFFERS from property)" if h["mail_differs"] else ""
        warning = f"  WARN: {h['warning']}" if h["warning"] else ""
        print(f"\n  case={h['case_number']}")
        print(f"  decedent:    {h['decedent']!r}")
        print(f"  applicant:   {h['applicant']!r}  (match tier={h['tier']})  ({h['n_accounts']} acct){warning}")
        print(f"  matched DCAD owner: {h['matched_name']!r}")
        print(f"  property addr: {h['property_address']!r}  "
              f"({h['property_city']})")
        print(f"  MAILING addr:  {h['mailing_address']!r}{marker}")
        print(f"                 {h['mailing_city']!r}, {h['mailing_state']!r} {h['mailing_zip']!r}")

    print()
    print("=" * 70)
    print(f"SAMPLE MISSES (first 15 of {len(misses)})")
    print("=" * 70)
    for m in misses[:15]:
        print(f"  case={m['case_number']}")
        print(f"    decedent:  {m['decedent']!r}")
        print(f"    applicant: {m['applicant']!r}")

    return 0


def _contains_property_street(mailing: str, property_normalized: str) -> bool:
    """True if the mailing-address string appears to contain the property's
    street number + street name. Crude — looks for the first 2 tokens
    (number + first street word) of property_normalized as a substring.
    """
    if not mailing or not property_normalized:
        return False
    tokens = property_normalized.split()
    if len(tokens) < 2:
        return False
    needle = f"{tokens[0]} {tokens[1]}".upper()
    return needle in mailing.upper()


if __name__ == "__main__":
    sys.exit(main())
