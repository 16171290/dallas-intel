"""Local probe for PR 8 Phase 2 — Tyler case-detail scrape.

Iterate on the case-detail parser WITHOUT running main.py end-to-end
(which takes 20+ minutes per cycle). The probe:

  1. Loads the current data/records.json
  2. Filters to Tyler-source PB records that have no dcad_account
     (the targets PR 8 Phase 2 is supposed to recover)
  3. Acquires Tyler session cookies via probate_auth (one Playwright
     login — same flow main.py uses)
  4. For each target record, fetches the case-detail page and dumps:
       - Raw page text → data/probes/tyler_case_detail/{id}.txt
       - Raw HTML       → data/probes/tyler_case_detail/{id}.html
       - Parser result  → printed inline + saved to extraction.json
  5. Reports a summary: how many cases parsed cleanly, how many had
     inventory filings visible, how many surfaced addresses

Output the operator can review BEFORE flipping
TYLER_CASE_DETAIL_ENABLED=true in main.py:

  - If addresses appear inline in Tyler's HTML → enable the gate
  - If addresses are only in filing PDFs → defer Phase 2 v1; need
    Inventory PDF OCR follow-up
  - If Tyler HTML structure differs from our parser's assumptions →
    inspect the dumped text and tune the regex

Usage:
    # Probe all unresolved Tyler PBs in current records.json
    python scripts/probe_tyler_case_detail.py

    # Probe a specific case (good for individual debugging)
    python scripts/probe_tyler_case_detail.py --case-data-id <UUID>

    # Probe N records max (cap for cost control during initial debug)
    python scripts/probe_tyler_case_detail.py --limit 3

Requires PROBATE_ENABLED + RESEARCH_TX_EMAIL + RESEARCH_TX_PASSWORD set
in the environment.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("probe_tyler_case_detail")


DEFAULT_RECORDS = REPO_ROOT / "data" / "records.json"
DEFAULT_DUMP_DIR = REPO_ROOT / "data" / "probes" / "tyler_case_detail"


def load_unresolved_tyler_pbs(records_json: Path) -> list[dict]:
    """Read records.json and return Tyler-source PB records without a
    dcad_account. Returns empty list when the file is missing or has
    no qualifying records."""
    if not records_json.exists():
        logger.warning("No records.json at %s; nothing to probe", records_json)
        return []
    with records_json.open("r", encoding="utf-8") as f:
        envelope = json.load(f)
    records = envelope.get("records", envelope) if isinstance(envelope, dict) else envelope
    targets = [
        r for r in records
        if r.get("dallas_code") == "PB"
        and r.get("source") == "probate.txcourts.gov"
        and not r.get("dcad_account")
    ]
    logger.info(
        "Loaded %d records; %d are Tyler PBs without dcad_account",
        len(records), len(targets),
    )
    return targets


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--records-json", type=Path, default=DEFAULT_RECORDS,
        help="records.json to read target Tyler PBs from "
             "(default: data/records.json)",
    )
    ap.add_argument(
        "--case-data-id", action="append", default=None,
        help="Probe a specific Tyler caseDataID. Repeatable. "
             "Overrides records.json filter.",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Cap on number of cases probed in this run. "
             "Useful for first-run debugging.",
    )
    ap.add_argument(
        "--dump-dir", type=Path, default=DEFAULT_DUMP_DIR,
        help="Where to write per-case text + HTML dumps "
             "(default: data/probes/tyler_case_detail/)",
    )
    ap.add_argument(
        "--no-dump", action="store_true",
        help="Don't write per-case dump files (text + html). "
             "Use when iterating only on summary output.",
    )
    args = ap.parse_args()

    # Resolve targets
    targets: list[tuple[str, dict | None]] = []
    if args.case_data_id:
        for cid in args.case_data_id:
            targets.append((cid, None))
    else:
        records = load_unresolved_tyler_pbs(args.records_json)
        for rec in records:
            rid = rec.get("record_id") or ""
            if not rid.startswith("pro-"):
                continue
            targets.append((rid.removeprefix("pro-"), rec))

    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("No targets to probe. Either records.json has no unresolved "
              "Tyler PBs, or --case-data-id list was empty.")
        return 0

    logger.info("Probing %d case detail page(s)", len(targets))

    # Acquire Tyler session
    from scraper import probate_auth
    from scraper.probate_case_detail import CaseDetailFetcher

    t0 = time.perf_counter()
    cookies = probate_auth.acquire_session()
    if cookies is None:
        logger.error(
            "Failed to acquire Tyler session. Set RESEARCH_TX_EMAIL + "
            "RESEARCH_TX_PASSWORD env vars and try again."
        )
        return 1
    logger.info(
        "Tyler session acquired in %.1fs (%d cookies)",
        time.perf_counter() - t0, len(cookies),
    )

    dump_dir = None if args.no_dump else args.dump_dir
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Page dumps → %s", dump_dir)

    results: list[dict] = []
    with CaseDetailFetcher(cookies, dump_dir=dump_dir) as fetcher:
        for idx, (case_data_id, rec) in enumerate(targets, start=1):
            print()
            print("=" * 72)
            print(f"[{idx}/{len(targets)}] case_data_id={case_data_id}")
            if rec:
                print(f"  decedent (grantor):  {rec.get('grantor')!r}")
                print(f"  applicant (grantee): {rec.get('grantee')!r}")
                print(f"  case_type:           "
                      f"{(rec.get('signal_metadata') or {}).get('case_type')!r}")
                print(f"  record_id:           {rec.get('record_id')!r}")
            print("=" * 72)

            t_start = time.perf_counter()
            result = fetcher(case_data_id)
            elapsed = time.perf_counter() - t_start

            print(f"  status:           {result.status}")
            print(f"  elapsed:          {elapsed:.1f}s")
            print(f"  page_text_chars:  {len(result.page_text)}")
            print(f"  has_inventory:    {result.has_inventory}")
            print(f"  filings_found:    {len(result.filings_text)}")
            print(f"  addresses_found:  {result.addresses_found}")
            if result.error:
                print(f"  error:            {result.error}")
            if result.filings_text:
                print(f"  first 5 filings lines:")
                for line in result.filings_text[:5]:
                    print(f"    - {line[:150]}")
            print()

            results.append({
                "case_data_id":    case_data_id,
                "decedent":        rec.get("grantor") if rec else None,
                "status":          result.status,
                "elapsed_s":       round(elapsed, 1),
                "has_inventory":   result.has_inventory,
                "filings_count":   len(result.filings_text),
                "addresses_found": result.addresses_found,
                "error":           result.error,
            })

    # Summary
    print()
    print("=" * 72)
    print("PROBE SUMMARY")
    print("=" * 72)
    statuses = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    print(f"  Total probed:      {len(results)}")
    for s, c in statuses.items():
        print(f"    status={s}:      {c}")
    n_inventory = sum(1 for r in results if r["has_inventory"])
    n_addresses = sum(1 for r in results if r["addresses_found"])
    print(f"  With I&A filing:   {n_inventory}")
    print(f"  With inline addr:  {n_addresses}")
    if dump_dir:
        print(f"  Dumps written to:  {dump_dir}")
        print(f"  Inspect any *.txt to see what Tyler actually rendered.")

    # Save summary JSON
    if dump_dir:
        summary_path = dump_dir / "_summary.json"
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"  Summary JSON:      {summary_path}")

    # Exit code: non-zero if NOTHING worked (helps CI / scripting)
    if statuses.get("ok", 0) == 0 and len(results) > 0:
        print()
        print("⚠ No cases returned status=ok. Either auth is broken, "
              "Tyler's HTML differs from the parser's assumptions, or "
              "the case_data_ids are stale. Inspect a dumped *.txt "
              "to diagnose.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
