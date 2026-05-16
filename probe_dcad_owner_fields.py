"""
Probate integration probe - DCAD owner field-name sanity check.

Phase 8 PR 1. Mitigates risk R2.4 (silent total matching failure if the
owner-name field in dallas-intel's live DCAD bulk output doesn't match
the candidates dcad_owner_index expects).

This probe:
  1. Loads the live DCAD bulk ZIP through the existing dcad_bulk pipeline
     (cache hit on .dcad_cache if present; otherwise downloads).
  2. Extracts the ACCOUNT_INFO table.
  3. Adapts it from pandas DataFrame to list[dict] (the shape
     dcad_owner_index expects per its docstring).
  4. Runs inspect_account_info_fields to see which candidate field name
     hits OWNER_NAME and ACCOUNT_NUM.
  5. As a smoke test, tries to build the owner_index and reports its size.
  6. Writes a JSON summary to probe_dcad_owner_fields_output/.

If matched_owner_field is None, the candidate list needs the actual
field name added before any probate integration proceeds.

If owner_index is dramatically smaller than DCAD has rows, something
else is wrong (normalization too aggressive? joint-owner expansion
swallowing entries? sample below to investigate).

Usage:
    python probe_dcad_owner_fields.py

Output:
    probe_dcad_owner_fields_output/
        summary.json          structured findings
        sample_rows.txt       first 10 rows for human eyeballing
        index_sample.txt      first 20 owner_index entries for sanity
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sure we can import the scraper package even when running from repo root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scraper import dcad_bulk
from scraper.dcad_owner_index import (
    inspect_account_info_fields,
    build_owner_index,
    _OWNER_NAME_CANDIDATES,
    _ACCOUNT_NUM_CANDIDATES,
)


OUTPUT_DIR = ROOT / "probe_dcad_owner_fields_output"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("DCAD owner field-name sanity probe (Phase 8 PR 1)")
    print("=" * 70)

    # 1. Fetch the DCAD ZIP (will use .dcad_cache if present)
    print("\n[1/5] Fetching DCAD ZIP via existing dcad_bulk pipeline...")
    try:
        zip_path = dcad_bulk.fetch_dcad_zip()
    except Exception as exc:
        print(f"  FAILED: {exc}")
        print("  This usually means the DCAD source is unreachable, the cache")
        print("  is missing, or DCAD_ZIP_URL needs an override. Re-run a normal")
        print("  scraper/main.py first to populate the cache, then re-run this.")
        return 1
    print(f"  ZIP path: {zip_path}")
    print(f"  ZIP size: {zip_path.stat().st_size / 1_000_000:.1f} MB")

    # 2. Parse the tables. parse_dcad_tables returns dict[str, pd.DataFrame].
    print("\n[2/5] Parsing DCAD tables via dcad_bulk.parse_dcad_tables ...")
    try:
        tables_df = dcad_bulk.parse_dcad_tables(zip_path)
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1
    print(f"  Tables found: {sorted(tables_df.keys())}")

    if "ACCOUNT_INFO" not in tables_df:
        print("\n  CRITICAL: ACCOUNT_INFO table not present in DCAD ZIP.")
        print("  Available tables: " + ", ".join(sorted(tables_df.keys())))
        print("  This is a different problem than candidate-list mismatch -")
        print("  the source ZIP doesn't have the table we need at all.")
        return 1

    account_info_df = tables_df["ACCOUNT_INFO"]
    print(f"  ACCOUNT_INFO rows:    {len(account_info_df):,}")
    print(f"  ACCOUNT_INFO columns: {list(account_info_df.columns)}")

    # 3. Adapt DataFrame -> list[dict] so dcad_owner_index can consume it.
    #    This adapter is what we'll bake into the production canonicalization
    #    path during PR 5. Validating it works here.
    print("\n[3/5] Adapting DataFrame -> list[dict] for dcad_owner_index...")
    rows = account_info_df.to_dict(orient="records")
    tables_as_rows = {"ACCOUNT_INFO": rows}
    print(f"  Adapted rows: {len(rows):,}")
    if rows:
        print(f"  Sample row keys: {sorted(rows[0].keys())}")

    # 4. Run the inspector. THE critical test.
    print("\n[4/5] Running inspect_account_info_fields ...")
    inspection = inspect_account_info_fields(tables_as_rows, sample_size=5)

    matched_owner = inspection.get("matched_owner_field")
    matched_account = inspection.get("matched_account_field")

    print(f"  matched_owner_field:   {matched_owner!r}")
    print(f"  matched_account_field: {matched_account!r}")
    print(f"  keys_observed:         {inspection.get('keys_observed', [])}")

    candidates_used = {
        "OWNER_NAME_CANDIDATES": list(_OWNER_NAME_CANDIDATES),
        "ACCOUNT_NUM_CANDIDATES": list(_ACCOUNT_NUM_CANDIDATES),
    }

    # 5. Smoke test: build the index. Report its size.
    print("\n[5/5] Building owner_index (smoke test)...")
    try:
        owner_index = build_owner_index(tables_as_rows)
    except Exception as exc:
        print(f"  build_owner_index raised: {exc}")
        owner_index = {}

    print(f"  owner_index entries: {len(owner_index):,}")
    if owner_index:
        # Distribution of accounts-per-key (most owners have 1 property,
        # some have many)
        from collections import Counter
        bucket_sizes = Counter(len(v) for v in owner_index.values())
        print("  accounts-per-owner-key distribution:")
        for size in sorted(bucket_sizes.keys())[:6]:
            print(f"    {size} account(s): {bucket_sizes[size]:,} keys")

        most_owned = sorted(owner_index.items(), key=lambda kv: -len(kv[1]))[:5]
        print("  top 5 most-owning keys:")
        for name, accts in most_owned:
            print(f"    {len(accts):>3} props  ->  {name}")

    # Status determination
    print("\n" + "=" * 70)
    if matched_owner and matched_account and len(owner_index) > 100_000:
        verdict = "PASS"
        reason = "All field-name candidates match; owner_index built cleanly."
    elif not matched_owner:
        verdict = "FAIL_MISSING_OWNER_FIELD"
        reason = (
            "The OWNER_NAME field in this DCAD ZIP doesn't match any of "
            "dcad_owner_index._OWNER_NAME_CANDIDATES. Add the actual field "
            "name to the candidates tuple before any probate integration."
        )
    elif not matched_account:
        verdict = "FAIL_MISSING_ACCOUNT_FIELD"
        reason = (
            "The ACCOUNT_NUM field doesn't match any candidates. Same "
            "remediation as the owner field case."
        )
    elif len(owner_index) <= 100_000:
        verdict = "WARN_LOW_INDEX_SIZE"
        reason = (
            f"owner_index has only {len(owner_index)} entries. DCAD typically "
            f"has hundreds of thousands of owners. Inspect normalization "
            f"behavior - role-suffix regex may be too greedy, or joint-owner "
            f"expansion is collapsing entries."
        )
    else:
        verdict = "UNKNOWN"
        reason = "Unexpected state; review sample output."

    print(f"VERDICT: {verdict}")
    print(f"REASON:  {reason}")
    print("=" * 70)

    # Persist findings to disk
    summary = {
        "verdict": verdict,
        "reason": reason,
        "zip_path": str(zip_path),
        "zip_size_bytes": zip_path.stat().st_size,
        "tables_found": sorted(tables_df.keys()),
        "account_info_row_count": len(account_info_df),
        "account_info_columns": list(account_info_df.columns),
        "matched_owner_field": matched_owner,
        "matched_account_field": matched_account,
        "keys_observed_sample": inspection.get("keys_observed", []),
        "sample_rows": inspection.get("sample_rows", []),
        "candidates_used": candidates_used,
        "owner_index_size": len(owner_index),
        "owner_index_top_5": [
            {"name": name, "account_count": len(accts), "sample_accounts": accts[:3]}
            for name, accts in
            sorted(owner_index.items(), key=lambda kv: -len(kv[1]))[:5]
        ] if owner_index else [],
    }

    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nSummary written to {summary_path}")

    # Sample rows for eyeballing
    samples_path = OUTPUT_DIR / "sample_rows.txt"
    with samples_path.open("w", encoding="utf-8") as f:
        for i, row in enumerate(rows[:10]):
            f.write(f"=== Row {i} ===\n")
            for k in sorted(row.keys()):
                v = row[k]
                f.write(f"  {k}: {v!r}\n")
            f.write("\n")
    print(f"Sample rows written to {samples_path}")

    # Index sample for eyeballing
    if owner_index:
        idx_sample_path = OUTPUT_DIR / "index_sample.txt"
        with idx_sample_path.open("w", encoding="utf-8") as f:
            # Mix of first 20 alphabetic + top 10 most-owned
            f.write("=== Top 10 most-owning index keys ===\n")
            most_owned = sorted(owner_index.items(), key=lambda kv: -len(kv[1]))[:10]
            for name, accts in most_owned:
                f.write(f"  ({len(accts):>3} props) {name}\n")
                f.write(f"           sample accounts: {accts[:3]}\n")
            f.write("\n=== First 20 alphabetic index keys ===\n")
            alpha = sorted(owner_index.keys())[:20]
            for name in alpha:
                accts = owner_index[name]
                f.write(f"  {name}  ({len(accts)} accounts)\n")
        print(f"Index sample written to {idx_sample_path}")

    # Exit code reflects verdict
    if verdict == "PASS":
        return 0
    elif verdict.startswith("FAIL"):
        return 2
    else:  # WARN or UNKNOWN
        return 1


if __name__ == "__main__":
    sys.exit(main())
