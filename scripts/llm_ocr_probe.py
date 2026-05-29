#!/usr/bin/env python3
"""Standalone test harness for the LLM-OCR fallback (scraper/llm_ocr.py).

Exercises scraper.llm_ocr.extract() DIRECTLY on page images -- no Playwright,
no Tesseract, no main.py pipeline. Use this to confirm the vision-LLM recovers
watermark-garbled grantor/address fields before wiring it into main.py.

Requires ANTHROPIC_API_KEY in the environment (network access to
api.anthropic.com). The probe force-enables FORECLOSURE_LLM_OCR_ENABLED so you
don't have to.

Usage:
    # One record from explicit page images:
    python scripts/llm_ocr_probe.py page_01.png [page_02.png ...]

    # All documented forensic failure cases, scored against known answers:
    python scripts/llm_ocr_probe.py --forensic /path/to/ocr_forensic

    # Ignore cached results and re-call the API:
    python scripts/llm_ocr_probe.py --fresh --forensic /path/to/ocr_forensic
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force the fallback on for the probe regardless of shell env.
os.environ["FORECLOSURE_LLM_OCR_ENABLED"] = "true"

from scraper import llm_ocr  # noqa: E402

# Ground-truth keywords (uppercase substrings expected somewhere in the
# extracted grantor/address/date) for the documented forensic failure cases.
# Source: docs/OCR_WATERMARK_REMOVAL_INVESTIGATION.md + manual reads.
FORENSIC_TRUTH = {
    "315561589": ("Montgomery", ["NICOLE", "MONTGOMERY", "BEAUMONT"]),
    "315561578": ("Battie",     ["BATTIE", "CLARK", "MARLENE"]),
    "315561574": ("Betancourt", ["BETANCOURT", "VANGUARD"]),
    "315562554": ("Liable",     ["GRANT"]),            # LAPRENA / REGINALD GRANT
    "315561570": ("Velasquez",  ["VELASQUEZ", "GRANADOS", "GRENOBLE"]),
    "315561585": ("Cox",        ["COX", "CREST RIDGE"]),
    "315562562": ("Flores",     ["FLORES", "MATHIS"]),
}


def _preflight() -> bool:
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        print("ERROR: ANTHROPIC_API_KEY is not set. Export it and re-run:\n"
              "         export ANTHROPIC_API_KEY=sk-ant-...\n", file=sys.stderr)
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("ERROR: anthropic SDK not installed. Run: pip install anthropic",
              file=sys.stderr)
        return False
    return True


def _load_pages(paths: list[Path]) -> list[bytes]:
    return [p.read_bytes() for p in paths if p.exists()]


def _run_one(record_id: str, page_paths: list[Path], fresh: bool):
    if fresh:
        cp = llm_ocr._cache_path(record_id)
        if cp.exists():
            cp.unlink()
    pages = _load_pages(page_paths)
    if not pages:
        print(f"  no page images found for {record_id}")
        return None
    return llm_ocr.extract(record_id, pages)


def probe_files(paths: list[str], fresh: bool) -> None:
    page_paths = [Path(p) for p in paths]
    record_id = page_paths[0].stem
    print(f"=== {record_id} ({len(page_paths)} page(s), model={llm_ocr.MODEL}) ===")
    fields = _run_one(record_id, page_paths, fresh)
    if fields is None:
        print("  extract() returned None (disabled / no key / API error -- see logs)")
        return
    print(f"  grantor          : {fields.grantor!r}")
    print(f"  property_address : {fields.property_address!r}")
    print(f"  sale_date        : {fields.sale_date!r}")
    print(f"  confidence       : {fields.confidence}")
    print(f"  from_cache       : {fields.from_cache}")


def probe_forensic(base: str, fresh: bool) -> None:
    base_path = Path(base)
    print(f"Model: {llm_ocr.MODEL}   confidence_floor: {llm_ocr.CONFIDENCE_FLOOR}\n")
    total_kw = hit_kw = 0
    for rid, (label, keywords) in FORENSIC_TRUTH.items():
        rec_dir = base_path / rid
        pages = sorted(rec_dir.glob("page_0*.png"))[: llm_ocr.MAX_PAGES]
        fields = _run_one(rid, pages, fresh)
        if fields is None:
            print(f"{label:12s} ({rid}): extract() returned None")
            continue
        blob = " ".join(filter(None, [
            fields.grantor, fields.property_address, fields.sale_date])).upper()
        found = [k for k in keywords if k in blob]
        missing = [k for k in keywords if k not in blob]
        total_kw += len(keywords)
        hit_kw += len(found)
        status = "OK " if not missing else "MISS"
        print(f"{status} {label:12s} ({rid})  conf={fields.confidence:.2f}"
              f"{' [cache]' if fields.from_cache else ''}")
        print(f"      grantor={fields.grantor!r}")
        print(f"      address={fields.property_address!r}  date={fields.sale_date!r}")
        if missing:
            print(f"      MISSING keywords: {missing}")
        print()
    print(f"Keyword recall: {hit_kw}/{total_kw}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*", help="page image(s) for one record")
    ap.add_argument("--forensic", metavar="DIR",
                    help="run all forensic cases under DIR/<record_id>/page_0*.png")
    ap.add_argument("--fresh", action="store_true",
                    help="delete cached results and re-call the API")
    args = ap.parse_args()

    if not _preflight():
        return 2
    if args.forensic:
        probe_forensic(args.forensic, args.fresh)
    elif args.images:
        probe_files(args.images, args.fresh)
    else:
        ap.error("provide page image(s) or --forensic DIR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
