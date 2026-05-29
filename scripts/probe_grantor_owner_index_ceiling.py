"""Validation probe — what's the CEILING if we run grantor/decedent →
DCAD owner-index matching on publicsearch deed records?

Recon (2026-05-29) found:
  - publicsearch hit rate is 14.1% (129/917).
  - The 482 publicsearch PB records (biggest bucket) only get path_b
    (address) + path_c (subdivision). Most have raw_excerpt "N/A | N/A"
    (no legal description), so both paths are structurally dead.
  - But those records carry clean decedent names ("BRADLEY MICHAEL R
    DECD"). Path A (grantor → DCAD owner_index) is the right tool — but
    resolution_paths.run_path_a is hardcoded to NOF-only
    (resolution_paths.py:820), so it never runs on them.

This probe measures the ACHIEVABLE ceiling BEFORE we touch the production
gate. It does NOT mutate records.json. For every unresolved publicsearch
record (PB/LP/BR by default), it runs the existing
``probate_matcher.match_decedent_to_dcad`` against the real DCAD
owner-index, in two name forms:

  1. PRODUCTION form — ``rec['grantor']`` exactly as Path A would feed it
     today. The publicsearch grantor parser yields "W, Slaughter Demetris"
     (last token mistaken for surname), so this measures the as-is ceiling.
  2. CORRECTED form — rebuilt from ``grantor_raw`` ("LAST FIRST MIDDLE"),
     DECD/ESTATE stripped, comma inserted after the surname so the
     matcher's tier ladder engages. This measures the ceiling once name
     ordering is fixed.

Output: match rate per code per form, tier breakdown, and sample matches
with the resolved DCAD property address so you can eyeball correctness.

Usage:
    python scripts/probe_grantor_owner_index_ceiling.py
    python scripts/probe_grantor_owner_index_ceiling.py --codes PB
    python scripts/probe_grantor_owner_index_ceiling.py --codes PB LP BR --samples 20

Requires the DCAD bulk ZIP (downloaded/cached the same way main.py does).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import json
import logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("probe_grantor_owner_index_ceiling")

DEFAULT_RECORDS = REPO_ROOT / "data" / "records.json"

# Trailing estate/deceased markers to strip before matching. Tyler/DCAD
# normalize() already strips ESTATE/EST OF, but DECD/DECEASED are not in
# that regex, so strip them here.
_DECD_RE = re.compile(r"\b(DECD|DECEASED|DEC'D|ESTATE|EST)\b", re.IGNORECASE)

# Non-person grantors: governments, companies, churches, etc. These match
# DCAD owner keys but are never motivated-seller leads (a city lien or a
# corporate filing is not a deceased homeowner). NOTE: "ESTATE" is NOT here
# — "<NAME> DECD ESTATE" is exactly our target (a deceased person's estate).
_ENTITY_RE = re.compile(
    r"\b(CITY|COUNTY|STATE\s+OF|INC|L\.?L\.?C|LTD|CORP|COMPANY|"
    r"BANK|ASSN|ASSOCIATION|CHURCH|MINISTR|FOUNDATION|PARTNERS|"
    r"HOLDINGS|PROPERTIES|INVESTMENT|HOMEOWNERS|DBA|TRUST|FUND|"
    r"CARE\s+CENTER|HOSPITAL|SCHOOL|DISTRICT|AUTHORITY|DEPT|"
    r"DEPARTMENT|UNITED\s+STATES|USA|TEXAS\b)\b",
    re.IGNORECASE,
)

# High-confidence tiers — the surname is pinned by first+ tokens, not a
# bare-surname guess. surname_only is deliberately excluded.
_HIGH_CONF_TIERS = {"exact", "no_middle", "initial_form", "double_initial"}


def is_entity(name: str | None) -> bool:
    return bool(name and _ENTITY_RE.search(name))


def filing_year(rec: dict) -> str:
    fd = rec.get("filing_date") or ""
    return fd[:4] if len(fd) >= 4 else "?"


def clean_and_commaify(grantor_raw: str | None) -> str | None:
    """Rebuild a publicsearch 'LAST FIRST MIDDLE [SUFFIX] DECD' grantor as
    'LAST, FIRST MIDDLE' so match_decedent_to_dcad's tier ladder engages
    (it only runs multi-token tiers when the surname is the first token,
    which it infers from a comma)."""
    if not grantor_raw:
        return None
    s = _DECD_RE.sub("", grantor_raw.upper())
    s = re.sub(r"\s+", " ", s).strip(" ,")
    if not s:
        return None
    toks = s.split()
    if len(toks) >= 2:
        return f"{toks[0]}, {' '.join(toks[1:])}"
    return s


def load_records(path: Path) -> list[dict]:
    env = json.load(path.open("r", encoding="utf-8"))
    return env.get("records", env) if isinstance(env, dict) else env


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records-json", type=Path, default=DEFAULT_RECORDS)
    ap.add_argument("--codes", nargs="+", default=["PB", "LP", "BR"],
                    help="Dallas codes to test (default: PB LP BR)")
    ap.add_argument("--samples", type=int, default=12,
                    help="How many matched samples to print per code")
    args = ap.parse_args()

    # --- Load DCAD exactly like main.py [1/12] ---
    from scraper import dcad_bulk, dcad_owner_index
    from scraper.probate_matcher import match_decedent_to_dcad

    logger.info("Fetching + parsing DCAD bulk data (cached if available)...")
    zip_path = dcad_bulk.fetch_dcad_zip()
    dcad_tables = dcad_bulk.parse_dcad_tables(zip_path)
    owner_index = dcad_owner_index.build_owner_index(dcad_tables)
    account_index = dcad_bulk.build_account_index(dcad_tables)
    logger.info("DCAD ready: %d owner keys, %d account keys",
                len(owner_index), len(account_index))

    records = load_records(args.records_json)
    targets = [r for r in records
               if r.get("source") == "publicsearch.us"
               and not r.get("dcad_account")
               and r.get("dallas_code") in set(args.codes)]
    logger.info("Unresolved publicsearch targets (%s): %d",
                ",".join(args.codes), len(targets))

    # form -> code -> Counter(outcome/tier)
    results: dict[str, dict[str, Counter]] = {
        "production": defaultdict(Counter),
        "corrected":  defaultdict(Counter),
    }
    # collect matched samples (corrected form) per code
    samples: dict[str, list] = defaultdict(list)

    # VALIDATED-RECOVERABLE classification (corrected form only).
    # quality buckets per code; plus a year-bucketed high-confidence count.
    quality: dict[str, Counter] = defaultdict(Counter)
    high_conf_by_year: dict[str, Counter] = defaultdict(Counter)  # code -> {year: n}

    for rec in targets:
        code = rec.get("dallas_code")

        production_name = rec.get("grantor")  # what Path A feeds today
        corrected_name = clean_and_commaify(rec.get("grantor_raw"))

        for form, name in (("production", production_name),
                           ("corrected", corrected_name)):
            if not name:
                results[form][code]["no_name"] += 1
                continue
            m = match_decedent_to_dcad(name, owner_index)
            if m is None:
                results[form][code]["no_match"] += 1
                continue
            results[form][code][f"matched:{m.tier}"] += 1
            if form != "corrected":
                continue

            # --- classify match quality (corrected form) ---
            n_acct = len(m.accounts)
            if is_entity(corrected_name) or is_entity(rec.get("grantor_raw")):
                quality[code]["entity_excluded"] += 1
            elif m.tier in _HIGH_CONF_TIERS and n_acct == 1:
                quality[code]["HIGH (high-tier, single acct)"] += 1
                high_conf_by_year[code][filing_year(rec)] += 1
            elif m.tier in _HIGH_CONF_TIERS and n_acct <= 3:
                quality[code]["MEDIUM (high-tier, 2-3 acct)"] += 1
            else:
                quality[code]["LOW (surname_only or 4+ acct)"] += 1

            if len(samples[code]) < args.samples:
                acct = m.accounts[0] if m.accounts else None
                addr = account_index.get(acct, {}) if acct else {}
                samples[code].append({
                    "grantor_raw": rec.get("grantor_raw"),
                    "fed": name,
                    "matched_name": m.matched_name,
                    "tier": m.tier,
                    "n_accounts": len(m.accounts),
                    "account": acct,
                    "address": addr.get("address_normalized"),
                    "city": addr.get("address_city"),
                    "filing_date": rec.get("filing_date"),
                    "warning": m.warning,
                })

    # ---- Report ----
    def matched_total(counter: Counter) -> int:
        return sum(n for k, n in counter.items() if k.startswith("matched:"))

    print()
    print("=" * 78)
    print("GRANTOR → OWNER-INDEX CEILING  (no mutation; measurement only)")
    print("=" * 78)
    print(f"records.json: {args.records_json}")
    print(f"codes tested: {', '.join(args.codes)}")
    print(f"unresolved targets: {len(targets)}")
    print()

    for form in ("production", "corrected"):
        print(f"── {form.upper()} name form "
              f"({'rec.grantor as-is' if form=='production' else 'grantor_raw → LAST, FIRST'})")
        grand_match = grand_total = 0
        for code in args.codes:
            c = results[form][code]
            tot = sum(c.values())
            mt = matched_total(c)
            grand_match += mt
            grand_total += tot
            if tot == 0:
                continue
            rate = f"{100.0*mt/tot:.1f}%" if tot else "n/a"
            print(f"   {code}: {mt}/{tot} matched ({rate})")
            for k, n in sorted(c.items(), key=lambda kv: -kv[1]):
                print(f"        {n:>4}  {k}")
        gr = f"{100.0*grand_match/grand_total:.1f}%" if grand_total else "n/a"
        print(f"   TOTAL: {grand_match}/{grand_total} would match ({gr})")
        print()

    # Sample matches for eyeballing correctness
    for code in args.codes:
        if not samples[code]:
            continue
        print(f"── SAMPLE MATCHES [{code}] (corrected form):")
        for s in samples[code]:
            print(f"   {s['grantor_raw']!r}  →  DCAD {s['matched_name']!r} "
                  f"[{s['tier']}{'/'+str(s['n_accounts'])+'acct' if s['n_accounts']>1 else ''}]")
            print(f"        acct={s['account']}  addr={s['address']}, {s['city']}  "
                  f"filed={s['filing_date']}"
                  f"{'  ⚠ '+s['warning'] if s['warning'] else ''}")
        print()

    # ---- VALIDATED RECOVERABLE: the defensible number ----
    print("=" * 78)
    print("VALIDATED RECOVERABLE  (corrected form, quality-classified)")
    print("=" * 78)
    print("HIGH   = high-confidence tier (exact/no_middle/initial) + single DCAD account")
    print("MEDIUM = high-confidence tier but 2-3 accounts (needs a tiebreaker)")
    print("LOW    = surname_only OR 4+ accounts (common-name pollution)")
    print("entity = non-person grantor (city/INC/DBA/church/etc.) — never a lead")
    print()
    grand_high = 0
    for code in args.codes:
        q = quality[code]
        tot = sum(q.values())
        if tot == 0:
            continue
        high = q.get("HIGH (high-tier, single acct)", 0)
        grand_high += high
        print(f"   {code}:  (of {tot} corrected-form matches)")
        for label in ("HIGH (high-tier, single acct)",
                      "MEDIUM (high-tier, 2-3 acct)",
                      "LOW (surname_only or 4+ acct)",
                      "entity_excluded"):
            if q.get(label):
                print(f"        {q[label]:>4}  {label}")
    print()
    print(f"   GRAND TOTAL HIGH-confidence recoverable: {grand_high} "
          f"(of {len(targets)} unresolved targets)")
    print()

    # High-confidence recoveries split by filing year — the flood (2020-22)
    # vs the records that are actually fresh leads (2026).
    print("   HIGH-confidence recoveries by filing year (recent = real leads):")
    all_years = sorted({y for c in args.codes for y in high_conf_by_year[c]})
    for code in args.codes:
        if not high_conf_by_year[code]:
            continue
        spread = ", ".join(f"{y}:{high_conf_by_year[code][y]}"
                           for y in sorted(high_conf_by_year[code]))
        print(f"        {code}: {spread}")
    recent = sum(high_conf_by_year[c].get("2026", 0) for c in args.codes)
    print(f"   HIGH-confidence on 2026 (fresh-lead) records: {recent}")
    print()

    print("Interpretation:")
    print("  - PRODUCTION rate = what un-gating Path A as-is would yield")
    print("    (publicsearch grantor parser mis-orders names → likely low).")
    print("  - CORRECTED rate = raw ceiling once name ordering is fixed, but")
    print("    INFLATED by surname-only / common-name / entity noise.")
    print("  - VALIDATED RECOVERABLE (HIGH) = the defensible win: high-tier,")
    print("    single-account, person grantors. This is what the production")
    print("    fix should stamp as primary; MEDIUM needs a tiebreaker, LOW")
    print("    and entity should be alternates/flagged or dropped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
