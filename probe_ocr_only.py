"""Focused probe: run ONLY the production foreclosures_ps scrape + OCR
enrichment. Skips DCAD bulk fetch, publicsearch Real-Property scrape,
probate, merge, scoring, output — everything except the bit we're
actively debugging.

Tests the EXACT production code path (scraper/foreclosure_ocr.py:
enrich_foreclosure_records), so any hang or bug reproduces here.

Usage:
    python probe_ocr_only.py                     # all 50-80 fresh records
    python probe_ocr_only.py --limit 5           # just first 5 (for fastest iteration)
    FORECLOSURE_OCR_PARALLEL=false python probe_ocr_only.py    # serial OCR

Total runtime:
    --limit 5:        ~1-2 minutes (vs ~20 min for full main.py)
    full pool (~54):  ~5-10 minutes
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0,
                    help="If >0, only OCR the first N records (default: all)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 70)
    print("Focused OCR probe — production code path, no full pipeline")
    print("=" * 70)

    # ── Step 1: scrape foreclosure records (~20-30s) ─────────────────────
    print("\n[1/3] Scraping foreclosure records from publicsearch.us ...")
    from scraper.foreclosures_ps import scrape_foreclosures
    records = scrape_foreclosures()
    print(f"  Got {len(records)} records")

    if args.limit and len(records) > args.limit:
        records = records[:args.limit]
        print(f"  Limited to first {args.limit}")

    if not records:
        print("\nNo records to OCR. Exiting.")
        return 0

    # ── Step 2: canonicalize to dict shape (instant, in-memory) ──────────
    print(f"\n[2/3] Canonicalizing {len(records)} records ...")
    from scraper.enrichment import canonicalize_foreclosure_notice_ps
    canonical = [canonicalize_foreclosure_notice_ps(r) for r in records]
    print(f"  Canonical dicts ready")

    # ── Step 3: OCR enrichment (the bit we're debugging) ─────────────────
    print(f"\n[3/3] Running production OCR enrichment ...")
    print(f"  (This calls scraper.foreclosure_ocr.enrich_foreclosure_records,")
    print(f"  the exact code path that runs in main.py Stage 3.6.)")
    print()

    from scraper.foreclosure_ocr import enrich_foreclosure_records
    enriched, stats = enrich_foreclosure_records(canonical)

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Total records:           {stats.total}")
    print(f"  Captured OK:             {stats.captured_ok}  ({100*stats.captured_ok/max(stats.total,1):.0f}%)")
    print(f"  No images (after retry): {stats.no_images}")
    print(f"  Capture errors:          {stats.capture_errors}")
    print(f"  HOA-lien suppressed:     {stats.hoa_lien_suppressed}")
    print(f"  Past-sale skipped:       {stats.skipped_past_sale_date}")
    print()
    print(f"  Field extraction (% of captured-ok):")
    n_ok = max(stats.captured_ok, 1)
    print(f"    Grantor (owner):       {stats.grantor_extracted}  ({100*stats.grantor_extracted/n_ok:.0f}%)")
    print(f"    Sale date:             {stats.sale_date_extracted}  ({100*stats.sale_date_extracted/n_ok:.0f}%)")
    print(f"    Property address:      {stats.address_extracted}  ({100*stats.address_extracted/n_ok:.0f}%)")
    print(f"    Loan amount:           {stats.loan_amount_extracted}  ({100*stats.loan_amount_extracted/n_ok:.0f}%)")
    print(f"    Legal description:     {stats.legal_desc_extracted}  ({100*stats.legal_desc_extracted/n_ok:.0f}%)")

    # First 3 enriched records as a sanity check
    if enriched:
        print(f"\n  First 3 enriched records (sample fields):")
        for rec in enriched[:3]:
            print(f"    record_id={rec.get('record_id')}")
            print(f"      grantor:  {rec.get('grantor')!r}")
            print(f"      address:  {rec.get('address')!r}")
            print(f"      sale_date: {rec.get('sale_date')!r}")
            print(f"      warnings: {rec.get('parse_warnings', [])[:3]}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
