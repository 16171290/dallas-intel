"""High-throughput batch foreclosure-OCR sampler.

Reuses one Playwright browser across all records (saves ~3-5s/record of
launch overhead) and parallelizes OCR via multiprocessing.Pool (4-8x
speedup vs serial Tesseract).

Excludes already-probed records via existing data/probes/ocr_<id>/
directories. Safe to re-run; new sample each invocation.

Usage:
    python probe_sample_batch.py --n 25                       # 25 new records
    python probe_sample_batch.py --n 50 --ocr-workers 8       # 50 records, 8 OCR workers
    python probe_sample_batch.py --n 100 --skip-ocr           # capture only; OCR later

Estimated runtime (sequential capture + parallel OCR, 4 cores):
    25 records   → ~6-8 minutes
    50 records   → ~12-15 minutes
    100 records  → ~25-30 minutes
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

PROBE_DIR = ROOT / "data" / "probes"


# ---------------------------------------------------------------------------
# Worker: OCR one PNG (runs in subprocess via multiprocessing.Pool)
# ---------------------------------------------------------------------------

def _ocr_one(args: tuple[str, str]) -> tuple[str, str]:
    """OCR a single PNG file. Run in subprocess. Returns (path, text)."""
    png_path, tess_cmd = args
    try:
        import pytesseract
        from PIL import Image
        pytesseract.pytesseract.tesseract_cmd = tess_cmd
        img = Image.open(png_path)
        return (png_path, pytesseract.image_to_string(img))
    except Exception as e:
        return (png_path, f"<OCR ERROR: {e}>")


# ---------------------------------------------------------------------------
# Capture: open /doc/{record_id}, save all page PNGs to disk
# ---------------------------------------------------------------------------

def _capture_one(page, record_id: str, base_url: str, out_dir: Path) -> dict:
    """Capture all PNGs for one record. Saves PNGs to disk.

    Returns: {"record_id", "status", "pages_captured", "out_dir"}
    Status: "ok" | "no_images" | "error"
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    rec_dir = out_dir / f"ocr_{record_id}"
    rec_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[int, bytes] = {}

    def on_response_initial(response):
        url = response.url
        m = re.search(r"_(\d+)\.png", url)
        if not (m and "/files/documents/" in url and ".png" in url):
            return
        pn = int(m.group(1))
        if pn in captured:
            return
        try:
            body = response.body()
            if body and len(body) > 100:
                captured[pn] = body
        except Exception:
            pass

    page.on("response", on_response_initial)

    try:
        page.goto(f"{base_url}/doc/{record_id}",
                  wait_until="networkidle", timeout=30_000)
    except Exception as e:
        logger.warning(f"  {record_id}: goto failed: {e}")
        page.remove_listener("response", on_response_initial)
        return {"record_id": record_id, "status": "error", "pages_captured": 0, "out_dir": rec_dir}

    # Passive wait for SPA hydration + page 1 fetch
    time.sleep(4)  # bumped from 3 -> 4 to reduce the 15% transient miss rate

    # Detect total page count
    total_pages: Optional[int] = None
    try:
        body_text = page.locator("body").inner_text()
        m = re.search(r"\bof\s+(\d+)\b", body_text)
        if m:
            total_pages = int(m.group(1))
    except Exception:
        pass

    # Click through remaining pages
    if total_pages and len(captured) < total_pages:
        for target_page in range(2, total_pages + 1):
            if target_page in captured:
                continue
            url_pat = re.compile(rf"/files/documents/\d+/images/\d+_{target_page}\.png")
            try:
                with page.expect_response(
                    lambda r, pat=url_pat: bool(pat.search(r.url)),
                    timeout=15_000,
                ) as response_info:
                    page.locator("button[aria-label='Go To Next Page']").click(timeout=3000)
                response = response_info.value
                body = response.body()
                if body and len(body) > 100:
                    captured[target_page] = body
            except PlaywrightTimeoutError:
                logger.warning(f"  {record_id}: timeout on page {target_page}")
            except Exception as e:
                logger.warning(f"  {record_id}: page {target_page} error: {e}")

    page.remove_listener("response", on_response_initial)

    if not captured:
        return {"record_id": record_id, "status": "no_images", "pages_captured": 0, "out_dir": rec_dir}

    # Save PNGs
    for pn in sorted(captured.keys()):
        (rec_dir / f"page_{pn}.png").write_bytes(captured[pn])

    return {"record_id": record_id, "status": "ok", "pages_captured": len(captured), "out_dir": rec_dir}


# ---------------------------------------------------------------------------
# Main batch driver
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=25, help="Records to sample (default 25)")
    ap.add_argument("--ocr-workers", type=int, default=multiprocessing.cpu_count(),
                    help="Parallel OCR workers (default: CPU count)")
    ap.add_argument("--skip-ocr", action="store_true",
                    help="Capture PNGs only, skip OCR (run probe_foreclosure_ocr.py on individual IDs later)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    # --- Dependency checks ---
    from probe_foreclosure_ocr import find_tesseract
    tess_path = find_tesseract() if not args.skip_ocr else None
    if not args.skip_ocr and not tess_path:
        print("ERROR: Tesseract not found. Install or use --skip-ocr")
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed")
        return 1

    from scraper import config

    PROBE_DIR.mkdir(parents=True, exist_ok=True)

    # --- Find already-probed records to skip ---
    seen: set[str] = set()
    for d in PROBE_DIR.iterdir():
        if d.is_dir() and d.name.startswith("ocr_"):
            seen.add(d.name[len("ocr_"):])
    print(f"Already probed: {len(seen)} record(s) will be excluded")

    # --- Fetch fresh candidate records ---
    print("\nFetching foreclosure records from publicsearch.us ...")
    from scraper.foreclosures_ps import scrape_foreclosures
    recs = scrape_foreclosures()
    candidates = [r.record_id for r in recs if r.record_id not in seen]
    print(f"Found {len(recs)} total, {len(candidates)} unprobed")

    if not candidates:
        print("Nothing new to sample.")
        return 0

    n = min(args.n, len(candidates))
    sample_ids = random.sample(candidates, n)
    print(f"Sampling {n} record(s)\n")

    # --- Phase 1: Capture (single browser, reused across records) ---
    print("="*70)
    print(f"PHASE 1: CAPTURE — {n} records, single browser")
    print("="*70)

    t0 = time.time()
    results: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )

        for i, rid in enumerate(sample_ids, 1):
            t_rec = time.time()
            page = context.new_page()
            try:
                result = _capture_one(page, rid, config.PUBLICSEARCH_BASE, PROBE_DIR)
            except Exception as e:
                logger.error(f"  {rid}: uncaught error: {e}")
                result = {"record_id": rid, "status": "error", "pages_captured": 0,
                          "out_dir": PROBE_DIR / f"ocr_{rid}"}
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            results.append(result)
            elapsed = time.time() - t_rec
            status_icon = "✓" if result["status"] == "ok" else "✗"
            print(f"  [{i}/{n}] {status_icon} {rid}: {result['status']} "
                  f"({result['pages_captured']} pages, {elapsed:.1f}s)")

        context.close()
        browser.close()

    capture_elapsed = time.time() - t0
    ok_count = sum(1 for r in results if r["status"] == "ok")
    print(f"\nCapture phase: {ok_count}/{n} ok in {capture_elapsed:.1f}s "
          f"({capture_elapsed/n:.1f}s/record)")

    if args.skip_ocr:
        print("\nSkipping OCR phase per --skip-ocr.")
        return 0

    # --- Phase 2: OCR (parallel via multiprocessing.Pool) ---
    print("\n" + "="*70)
    print(f"PHASE 2: OCR — {args.ocr_workers} parallel workers")
    print("="*70)

    # Gather all PNG paths across all successful captures
    ocr_jobs: list[tuple[str, str]] = []
    job_groups: dict[str, list[str]] = {}  # record_id -> list of png paths

    for result in results:
        if result["status"] != "ok":
            continue
        rid = result["record_id"]
        png_paths = sorted(result["out_dir"].glob("page_*.png"),
                          key=lambda p: int(re.search(r"_(\d+)\.png", p.name).group(1)))
        job_groups[rid] = [str(p) for p in png_paths]
        for p in png_paths:
            ocr_jobs.append((str(p), tess_path))

    print(f"Total PNG pages to OCR: {len(ocr_jobs)}")
    if not ocr_jobs:
        print("Nothing to OCR.")
        return 0

    t0 = time.time()
    with multiprocessing.Pool(processes=args.ocr_workers) as pool:
        ocr_results = pool.map(_ocr_one, ocr_jobs)
    ocr_elapsed = time.time() - t0
    print(f"OCR phase: {len(ocr_jobs)} pages in {ocr_elapsed:.1f}s "
          f"({ocr_elapsed/len(ocr_jobs):.2f}s/page)")

    # Reassemble per-record concatenated text
    text_by_path = dict(ocr_results)
    for rid, paths in job_groups.items():
        combined = "\n\n".join(text_by_path[p] for p in paths)
        (PROBE_DIR / f"ocr_{rid}" / "ocr_text.txt").write_text(combined, encoding="utf-8")

    # --- Summary ---
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"  Sampled:        {n}")
    print(f"  Captured ok:    {ok_count}")
    print(f"  No images:      {sum(1 for r in results if r['status'] == 'no_images')}")
    print(f"  Errors:         {sum(1 for r in results if r['status'] == 'error')}")
    print(f"  Capture time:   {capture_elapsed:.1f}s ({capture_elapsed/n:.1f}s/record avg)")
    print(f"  OCR time:       {ocr_elapsed:.1f}s ({len(ocr_jobs)} pages, {ocr_elapsed/len(ocr_jobs):.2f}s/page avg)")
    print(f"  Total:          {capture_elapsed + ocr_elapsed:.1f}s")
    print(f"  Output:         data/probes/ocr_<record_id>/ocr_text.txt")
    print()
    print("To inspect:")
    for r in results:
        if r["status"] == "ok":
            print(f"  data/probes/ocr_{r['record_id']}/ocr_text.txt")

    return 0


if __name__ == "__main__":
    sys.exit(main())
