"""Standalone probe runner for the foreclosures_ps scraper.

Runs ONLY the publicsearch.us Foreclosures scrape -- skips DCAD bulk
fetch, Real-Property publicsearch, probate, enrichment, scoring, etc.
Goal: fast iteration on the scraper itself without paying for the
full ~10-minute pipeline on every run.

Usage:
    python probe_foreclosures.py
    python probe_foreclosures.py --recorded 14 --sale 180
    python probe_foreclosures.py --recorded 7 --sale 90

Writes probe artifacts to data/probes/foreclosures_<timestamp>.{png,html,txt}
on the first harvest call (see scraper/foreclosures_ps.py _probe_page_state).

After running, open the screenshot to see exactly what publicsearch.us
rendered for our request -- no inference needed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make scraper/ importable when run from repo root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Auto-load .env so any env-var overrides apply
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from scraper import foreclosures_ps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--recorded", type=int, default=7,
        help="Recorded-date lookback in days (default 7)",
    )
    ap.add_argument(
        "--sale", type=int, default=180,
        help="Sale-date lookahead in days (default 180)",
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true",
        help="DEBUG-level logging",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 70)
    print(f"foreclosures_ps probe -- recorded_lookback={args.recorded}d, "
          f"sale_lookahead={args.sale}d")
    print("=" * 70)

    records = foreclosures_ps.scrape_foreclosures(
        recorded_lookback_days=args.recorded,
        sale_lookahead_days=args.sale,
    )

    print()
    print("=" * 70)
    print(f"Result: {len(records)} record(s) scraped")
    print("=" * 70)
    for i, r in enumerate(records[:5]):
        print(f"\n--- record {i} ---")
        print(f"  record_id        = {r.record_id!r}")
        print(f"  recorded_date    = {r.recorded_date!r}")
        print(f"  sale_date        = {r.sale_date!r}")
        print(f"  doc_number       = {r.doc_number!r}")
        print(f"  property_address = {r.property_address!r}")
        if r.parse_warnings:
            print(f"  parse_warnings   = {r.parse_warnings}")

    if len(records) > 5:
        print(f"\n  ...{len(records) - 5} more records (suppressed)")

    print()
    print("=" * 70)
    print("Probe artifacts written to data/probes/foreclosures_<timestamp>.{png,html,txt}")
    print("Open the .png to see what publicsearch.us actually rendered.")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
