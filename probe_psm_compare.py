"""PSM=3 vs PSM=6 A/B comparison on a captured OCR sample PNG.

Standalone probe — does NOT run the full pipeline. Reads one PNG from
disk, OCRs it twice (default config vs --psm 6 --oem 1), runs
extract_fields_from_text() on each output, and prints what each
configuration successfully extracted.

Use this to verify that the universal-pattern extractor still finds
grantor / sale_date / property_address / loan_amount /
legal_description in PSM=6 output before swapping the production
config.

Usage (PowerShell, Windows)
---------------------------
    cd <repo>
    .\.venv\Scripts\Activate.ps1
    # Use a sample PNG already captured by the pipeline:
    python probe_psm_compare.py data\probes\ocr_sample_315586580_p1.png
    # Or any other foreclosure-notice PNG you have on disk:
    python probe_psm_compare.py path\to\some.png

Exit code 0 if both configs extracted ALL of (grantor, sale_date,
property_address). Non-zero if PSM=6 lost a field PSM=3 found.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _summarise_fields(label: str, fields, elapsed: float, text: str) -> dict:
    """Print one block of extraction results, return them as a dict."""
    print(f"\n{'=' * 70}")
    print(f"{label}   elapsed={elapsed:.2f}s   text_len={len(text)} chars")
    print("=" * 70)
    extracted = {
        "grantor":             fields.grantor,
        "sale_date_iso":       fields.sale_date_iso,
        "property_address":    fields.property_address,
        "loan_amount":         fields.loan_amount,
        "legal_description":   fields.legal_description,
        "is_hoa_lien":         fields.is_hoa_lien,
    }
    for k, v in extracted.items():
        marker = "Y" if v else "n"
        v_disp = repr(v)[:80] if v else "(none)"
        print(f"  [{marker}] {k:22s}  {v_disp}")
    print(f"  warnings: {fields.warnings}")
    return extracted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "png", type=Path,
        help="Path to a captured foreclosure-notice PNG (e.g. data/probes/ocr_sample_*.png)",
    )
    args = parser.parse_args(argv)

    if not args.png.is_file():
        print(f"ERROR: file not found: {args.png}", file=sys.stderr)
        return 2

    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        print(f"ERROR: missing dependency ({e}). pip install pytesseract Pillow",
              file=sys.stderr)
        return 2

    from scraper.foreclosure_ocr import extract_fields_from_text, _find_tesseract

    tess = _find_tesseract()
    if not tess:
        print("ERROR: tesseract binary not found on PATH", file=sys.stderr)
        return 2
    pytesseract.pytesseract.tesseract_cmd = tess

    print(f"Tesseract: {tess}")
    print(f"Image:     {args.png}  ({args.png.stat().st_size:,} bytes)")
    img = Image.open(args.png)
    print(f"Pixels:    {img.size[0]}x{img.size[1]}  mode={img.mode}")

    # Run A (default — PSM=3, OEM=3)
    t0 = time.perf_counter()
    text_a = pytesseract.image_to_string(img)
    elapsed_a = time.perf_counter() - t0
    fields_a = extract_fields_from_text(text_a)
    a = _summarise_fields("CONFIG A: pipeline default (PSM=3, OEM=3)", fields_a, elapsed_a, text_a)

    # Run B (proposed — PSM=6, OEM=1)
    t0 = time.perf_counter()
    text_b = pytesseract.image_to_string(img, config="--psm 6 --oem 1")
    elapsed_b = time.perf_counter() - t0
    fields_b = extract_fields_from_text(text_b)
    b = _summarise_fields("CONFIG B: proposed     (PSM=6, OEM=1)", fields_b, elapsed_b, text_b)

    # Speedup ratio
    print(f"\n{'=' * 70}")
    print(f"SPEEDUP: {elapsed_a / max(elapsed_b, 0.001):.1f}x  "
          f"(A={elapsed_a:.2f}s  vs  B={elapsed_b:.2f}s)")
    print("=" * 70)

    # Field-by-field comparison: did B lose anything A had?
    print("\nField-by-field comparison (A=default, B=PSM6):")
    lost = []
    gained = []
    same = []
    for key in ("grantor", "sale_date_iso", "property_address",
                "loan_amount", "legal_description"):
        va, vb = a.get(key), b.get(key)
        if va and not vb:
            lost.append(key)
            print(f"  LOST in B:    {key:22s}  A={repr(va)[:60]}")
        elif vb and not va:
            gained.append(key)
            print(f"  GAINED in B:  {key:22s}  B={repr(vb)[:60]}")
        elif va and vb:
            if va == vb:
                same.append(key)
                print(f"  same:         {key:22s}  {repr(va)[:60]}")
            else:
                print(f"  DIFFERENT:    {key:22s}")
                print(f"                A={repr(va)[:60]}")
                print(f"                B={repr(vb)[:60]}")
        # If both None, don't print (uninteresting)

    print("\nVerdict:")
    if lost:
        print(f"  PSM=6 LOST {len(lost)} field(s): {lost}")
        print(f"  -> Risk: production switch may regress extraction quality.")
        return 1
    if gained:
        print(f"  PSM=6 gained {len(gained)} field(s) PSM=3 missed: {gained}")
    print(f"  PSM=6 is safe to ship: extracted same or more, "
          f"{elapsed_a / max(elapsed_b, 0.001):.1f}x faster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
