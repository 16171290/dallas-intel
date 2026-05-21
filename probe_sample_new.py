"""Sample N foreclosure records we haven't OCR'd yet, then run the probe on each.

Treats existing data/probes/ocr_<id>/ directories as "already seen" markers,
so we never re-sample the same record. Calls probe_foreclosure_ocr.py in
sequence on each newly sampled ID.

Usage:
    python probe_sample_new.py            # sample 5 new records
    python probe_sample_new.py --n 10     # sample 10 new records
"""

from __future__ import annotations

import argparse
import logging
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

PROBE_DIR = ROOT / "data" / "probes"
PROBE_SCRIPT = ROOT / "probe_foreclosure_ocr.py"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=5, help="Number of new records to sample (default 5)")
    args = ap.parse_args()

    # Already-probed = anything under data/probes/ocr_<id>/
    seen: set[str] = set()
    if PROBE_DIR.exists():
        for d in PROBE_DIR.iterdir():
            if d.is_dir() and d.name.startswith("ocr_"):
                seen.add(d.name[len("ocr_"):])
    print(f"Already probed: {len(seen)} record(s)")
    for s in sorted(seen):
        print(f"  - {s}")

    # Pull fresh foreclosure records
    print("\nFetching current foreclosure records from publicsearch.us ...")
    logging.basicConfig(level=logging.WARNING)
    from scraper.foreclosures_ps import scrape_foreclosures
    recs = scrape_foreclosures()
    print(f"Found {len(recs)} foreclosure records")

    candidates = [r.record_id for r in recs if r.record_id not in seen]
    print(f"Of those, {len(candidates)} are not yet probed")

    if not candidates:
        print("Nothing new to sample. Try a different recorded-date window or wait for new filings.")
        return 0

    n = min(args.n, len(candidates))
    ids = random.sample(candidates, n)

    print(f"\nSampling {n} new record(s):")
    for i in ids:
        print(f"  - {i}")

    print(f"\nRunning probe on each (estimated {n*30}s total)...")
    for i, rid in enumerate(ids, 1):
        print(f"\n{'='*70}")
        print(f"  [{i}/{n}] Probing {rid}")
        print('='*70)
        subprocess.run([sys.executable, str(PROBE_SCRIPT), rid])

    print(f"\nDone. New OCR files at:")
    for rid in ids:
        print(f"  data/probes/ocr_{rid}/ocr_text.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
