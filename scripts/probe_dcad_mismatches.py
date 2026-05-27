"""Forensic probe — quantify DCAD-lookup-mismatch failure classes.

For each record where Stage 7 / Path B already failed to find a DCAD
match despite having a clean-looking address, check whether DCAD has
the property under a REASONABLE VARIANT of the address:

  Class 26  — compound-word tokenization split (LAKEVIEW <-> LAKE VIEW)
  Class 26b — missing street suffix in source  (HAMILTON -> HAMILTON ST/AVE/...)
  Class 26c — cross-county properties (Grand Prairie / Carrollton etc.)
  General   — fuzzy-match (Levenshtein ratio against DCAD candidates)

This is RECON only. It doesn't change any code or records — just
reports what's potentially recoverable so we can scope a future fix.

Usage:
    python scripts/probe_dcad_mismatches.py
"""

from __future__ import annotations

import difflib
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def load_inputs():
    """Load records.json + cached DCAD address_index."""
    from scraper.output import read_records_json

    records_path = REPO_ROOT / "data" / "records.json"
    if not records_path.exists():
        sys.exit(f"ERROR: records.json not found at {records_path}")
    records = read_records_json(records_path)

    # Load the cached index built by validate_resolution.py --build-cache
    cache_path = REPO_ROOT / "data" / "cache" / "dcad_address_index.pkl.gz"
    if not cache_path.exists():
        sys.exit(
            "ERROR: DCAD address_index cache not found. "
            "Run `python scripts/validate_resolution.py --build-cache` first."
        )
    import gzip, pickle
    with gzip.open(cache_path, "rb") as f:
        address_index = pickle.load(f)
    return records, address_index


def find_clean_unresolved(records):
    """Records with no dcad_account but with what looks like a real street
    address (has a digit and length > 5). Path B / Stage 7 already failed
    on these — they're our recovery candidates."""
    out = []
    for r in records:
        if r.get("dcad_account"):
            continue
        addr = r.get("address_normalized") or ""
        if not addr or len(addr) < 6:
            continue
        first_seg = addr.split(",")[0].strip()
        if not any(c.isdigit() for c in first_seg):
            continue
        # Skip obvious garbled cases — already covered by Path B
        if any(c in addr for c in "{}@#%&"):
            continue
        # Skip venue/trustee
        from scraper.resolution import VENUE_TRUSTEE_SIGS
        if any(s in addr.upper() for s in VENUE_TRUSTEE_SIGS):
            continue
        out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────
# Class 26: compound-word split / join
# ─────────────────────────────────────────────────────────────────────

def split_variants(addr: str) -> list[str]:
    """Generate candidates by splitting any 8+char single token at each
    interior position. Returns variants where the split could plausibly
    represent two words (both halves are at least 3 chars)."""
    variants = []
    tokens = addr.split()
    for i, tok in enumerate(tokens):
        if len(tok) < 8:  # only try long tokens
            continue
        for split_at in range(3, len(tok) - 2):  # both halves >= 3 chars
            new_tok = tok[:split_at] + " " + tok[split_at:]
            new_addr = " ".join(tokens[:i] + [new_tok] + tokens[i+1:])
            variants.append(new_addr)
    return variants


# Class 26 reverse: words DCAD splits but our source joins.
# Common compound-fragment prefixes in TX subdivisions.
_KNOWN_COMPOUND_PREFIXES = [
    "LAKE", "HILL", "WOOD", "GREEN", "WEST", "EAST", "NORTH", "SOUTH",
    "RIDGE", "OAK", "MAPLE", "PINE", "RIVER", "CREEK", "PARK", "STONE",
    "STAR", "SUN", "BRIAR", "BROOK", "CEDAR",
]

def known_compound_splits(addr: str) -> list[str]:
    """Try splitting any token that starts with a known compound prefix
    where the remaining suffix is also 3+ chars."""
    variants = []
    tokens = addr.split()
    for i, tok in enumerate(tokens):
        for prefix in _KNOWN_COMPOUND_PREFIXES:
            if (tok.startswith(prefix) and len(tok) >= len(prefix) + 3
                    and tok != prefix):
                new_tok = prefix + " " + tok[len(prefix):]
                new_addr = " ".join(tokens[:i] + [new_tok] + tokens[i+1:])
                if new_addr not in variants:
                    variants.append(new_addr)
    return variants


# ─────────────────────────────────────────────────────────────────────
# Class 26b: missing-suffix variants
# ─────────────────────────────────────────────────────────────────────

_USPS_SUFFIXES = ["ST", "AVE", "RD", "DR", "LN", "CT", "BLVD",
                  "PL", "WAY", "TER", "CIR", "PKWY", "HWY", "TRL"]

def suffix_append_variants(addr: str) -> list[str]:
    """If the address doesn't end in a recognized USPS suffix, try
    appending each common one."""
    tokens = addr.split()
    if not tokens:
        return []
    last = tokens[-1].rstrip(".,")
    if last in _USPS_SUFFIXES:
        return []  # already has a suffix
    return [f"{addr} {sfx}" for sfx in _USPS_SUFFIXES]


# ─────────────────────────────────────────────────────────────────────
# Fuzzy match against DCAD keys with same street number
# ─────────────────────────────────────────────────────────────────────

def fuzzy_match_in_dcad(addr: str, address_index: dict, threshold: float = 0.85):
    """Find keys in address_index with the same leading street number and
    similar street name (Levenshtein ratio >= threshold)."""
    m = re.match(r"^(\d+)\s+(.+)$", addr)
    if not m:
        return []
    streetnum = m.group(1)
    streetname = m.group(2)

    # Limit candidates to same street number for tractability
    candidates = [k for k in address_index if k.startswith(streetnum + " ")]
    matches = []
    for k in candidates:
        k_name = k[len(streetnum) + 1:]
        ratio = difflib.SequenceMatcher(None, streetname, k_name).ratio()
        if ratio >= threshold:
            matches.append((ratio, k, address_index[k]))
    return sorted(matches, reverse=True)


# ─────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────

def probe_record(rec, address_index):
    addr = rec.get("address_normalized") or ""
    result = {
        "record_id":          rec.get("record_id"),
        "grantor":            rec.get("grantor"),
        "address_normalized": addr,
        "raw_excerpt":        (rec.get("raw_excerpt") or "")[:120],
        "split_variants":     [],
        "suffix_variants":    [],
        "fuzzy_candidates":   [],
    }
    # Compound-word splits (Class 26)
    all_splits = set(split_variants(addr)) | set(known_compound_splits(addr))
    for v in all_splits:
        if v in address_index:
            result["split_variants"].append((v, address_index[v]))
    # Missing-suffix variants (Class 26b)
    for v in suffix_append_variants(addr):
        if v in address_index:
            result["suffix_variants"].append((v, address_index[v]))
    # Fuzzy matches (general)
    fuzz = fuzzy_match_in_dcad(addr, address_index)
    result["fuzzy_candidates"] = fuzz[:5]
    return result


def main():
    records, address_index = load_inputs()
    print(f"DCAD address_index: {len(address_index):,} entries")
    print(f"Total records: {len(records)}")

    candidates = find_clean_unresolved(records)
    print(f"Clean-but-unresolved candidates: {len(candidates)}")
    print()
    print("=" * 80)

    by_class = defaultdict(list)
    for r in candidates:
        res = probe_record(r, address_index)
        print()
        print(f"record={res['record_id']}")
        print(f"  grantor:     {res['grantor']!r}")
        print(f"  address:     {res['address_normalized']!r}")
        print(f"  raw_excerpt: {res['raw_excerpt']!r}")
        if res["split_variants"]:
            print(f"  → COMPOUND-SPLIT recovery (Class 26):")
            for variant, acct in res["split_variants"]:
                print(f"      try '{variant}' -> acct={acct}")
            by_class["class_26_split"].append(res)
        if res["suffix_variants"]:
            print(f"  → SUFFIX-APPEND recovery (Class 26b):")
            for variant, acct in res["suffix_variants"]:
                print(f"      try '{variant}' -> acct={acct}")
            by_class["class_26b_suffix"].append(res)
        if res["fuzzy_candidates"]:
            print(f"  → FUZZY candidates (general):")
            for ratio, k, acct in res["fuzzy_candidates"]:
                print(f"      [{ratio:.2f}] '{k}' -> acct={acct}")
            by_class["fuzzy_only"].append(res)
        if not (res["split_variants"] or res["suffix_variants"]
                or res["fuzzy_candidates"]):
            print(f"  → NO RECOVERY found — likely cross-county (Class 26c) or "
                  f"genuinely not in DCAD")
            by_class["unrecoverable"].append(res)

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Total clean-but-unresolved : {len(candidates)}")
    print(f"  Class 26 (compound-split)  : {len(by_class['class_26_split'])}")
    print(f"  Class 26b (missing suffix) : {len(by_class['class_26b_suffix'])}")
    print(f"  Fuzzy-only (other variants): {len(by_class['fuzzy_only'])}")
    print(f"  Unrecoverable (likely 26c) : {len(by_class['unrecoverable'])}")


if __name__ == "__main__":
    main()
