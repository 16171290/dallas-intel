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
            else:
                results[form][code][f"matched:{m.tier}"] += 1
                if form == "corrected" and len(samples[code]) < args.samples:
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

    print("Interpretation:")
    print("  - PRODUCTION rate = what un-gating Path A as-is would yield")
    print("    (publicsearch grantor parser mis-orders names → likely low).")
    print("  - CORRECTED rate = ceiling once name ordering is also fixed.")
    print("  - Spot-check samples: does the DCAD owner/address look like the")
    print("    decedent's actual property? surname_only + multi-acct = lower")
    print("    confidence; exact/no_middle = high confidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
