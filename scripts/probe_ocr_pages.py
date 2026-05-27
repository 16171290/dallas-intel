"""Forensic OCR probe — capture, OCR, and compare per page.

Purpose: before changing OCR extractors in PR 7, we need to actually SEE
what the scanner reads vs what the document contains. This script:

  1. Reads data/records.json
  2. Filters to a set of failure-case record_ids (the records where the
     production pipeline produced bad grantor / address values: Liable,
     Montgomery, Cox, Battie, Betancourt, Adama, Velasquez, Flores)
  3. For each, downloads the PNG pages from publicsearch.us via the
     existing capture_with_retry machinery
  4. Saves the PNGs to docs/ocr_forensic/<record_id>/page_<n>.png so
     you can visually inspect what the OCR is seeing
  5. Runs Tesseract on each page; saves raw OCR text to
     docs/ocr_forensic/<record_id>/page_<n>.txt
  6. Runs the current extract_fields_from_text against the OCR; saves
     the extraction result to docs/ocr_forensic/<record_id>/extraction.json
  7. Writes a top-level docs/ocr_forensic/SUMMARY.md aggregating all
     records with their CURRENT field values from records.json next to
     the OCR raw + extraction result
  8. Bundles everything into ocr_forensic_bundle.zip at the repo root —
     upload that zip to the chat and Claude will extract + view PNGs
     directly to diagnose.

Usage:
    python scripts/probe_ocr_pages.py
    python scripts/probe_ocr_pages.py --records 315562554 315561589
    python scripts/probe_ocr_pages.py --all-unresolved  # all records without dcad_account

After running, upload ocr_forensic_bundle.zip to the chat. Claude will
extract the PNGs and visually inspect them along with the OCR text and
extraction results, so we can diagnose each failure case at three layers:
document truth ↔ Tesseract output ↔ regex extraction.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("probe_ocr_pages")


# Known failure-case record_ids from our investigation so far.
# Default set the probe runs against if you don't pass --records.
DEFAULT_FAILURE_CASES = [
    "315562554",  # Liable — boilerplate-as-grantor (Class 2)
    "315561589",  # Montgomery, Cole A. — should be Nicole A. (Class 27 OCR truncation)
    "315561585",  # Cox, Laura Annette — address "3031 CREST RDG D &" (garble)
    "315561578",  # Battie, Breanna Marie — address "1442 MA 5: {E)" (garble)
    "315561574",  # Betancourt, Jesus J. — address "43 VANGUARD OWE AY TALLY NTY" (garble)
    "315561580",  # Adama — address "7730 MARKET CENTER AVE" (wrong, should be 2828 LAKE VW DR)
    "315561570",  # Velasquez, Luis & Arriaza — grantor has ".noh Os" garble suffix
    "315562562",  # Flores — page-tiebreaker case
]


OUT_DIR = REPO_ROOT / "docs" / "ocr_forensic"


def _ensure_outdir(record_id: str) -> Path:
    p = OUT_DIR / record_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_records_json_state(record_id: str, all_records: list[dict]) -> dict:
    """Find the production record + return its currently-stamped fields."""
    for r in all_records:
        if r.get("record_id") == record_id:
            return {
                "record_id":          r.get("record_id"),
                "dallas_code":        r.get("dallas_code"),
                "grantor":            r.get("grantor"),
                "grantee":            r.get("grantee"),
                "address":            r.get("address"),
                "address_normalized": r.get("address_normalized"),
                "dcad_account":       r.get("dcad_account"),
                "dcad_owner":         r.get("dcad_owner"),
                "raw_excerpt":        r.get("raw_excerpt"),
                "source_url":         r.get("source_url"),
            }
    return {"record_id": record_id, "_not_found_in_records_json": True}


def probe_one(record_id: str, page, tess_cmd: str, base_url: str,
              production_state: dict) -> dict:
    """Capture + OCR + extract for a single record. Saves all artifacts
    to docs/ocr_forensic/<record_id>/."""
    from scraper.foreclosure_ocr import (
        capture_with_retry, extract_fields_from_text, _ocr_one_image_worker,
    )

    out = _ensure_outdir(record_id)
    logger.info("=== Probing %s ===", record_id)

    # 1. Capture PNGs
    cap_t0 = time.perf_counter()
    cap = capture_with_retry(page, record_id, base_url=base_url)
    cap_elapsed = time.perf_counter() - cap_t0
    logger.info(
        "  Capture status=%s pages=%d attempts=%d elapsed=%.1fs",
        cap.status, len(cap.pages), cap.attempts, cap_elapsed,
    )

    page_findings = []
    combined_text_parts = []

    for pn in sorted(cap.pages.keys()):
        png_bytes = cap.pages[pn]
        png_path = out / f"page_{pn:02d}.png"
        png_path.write_bytes(png_bytes)

        # 2. Run Tesseract on this page
        ocr_t0 = time.perf_counter()
        try:
            text, elapsed, size, width, height, mode, dpi = _ocr_one_image_worker(
                (png_bytes, tess_cmd),
            )
        except Exception as e:
            logger.warning("  OCR failed for page %d: %s", pn, e)
            text = ""
            elapsed = 0
            width = height = 0

        ocr_path = out / f"page_{pn:02d}.txt"
        ocr_path.write_text(text, encoding="utf-8")

        combined_text_parts.append(text)
        page_findings.append({
            "page":          pn,
            "png":           png_path.name,
            "txt":           ocr_path.name,
            "ocr_elapsed_s": round(elapsed, 2),
            "ocr_chars":     len(text),
            "image_size":    f"{width}x{height}",
        })

    combined_text = "\n".join(combined_text_parts)

    # 3. Run current extractors against the combined OCR text
    fields = extract_fields_from_text(combined_text)

    extraction = {
        "production_state": production_state,
        "pages":            page_findings,
        "combined_ocr_chars": len(combined_text),
        "extracted_fields": {
            "grantor":           fields.grantor,
            "grantor_pattern":   fields.grantor_pattern,
            "property_address":  fields.property_address,
            "address_pattern":   fields.address_pattern,
            "sale_date_raw":     fields.sale_date_raw,
            "sale_date_iso":     fields.sale_date_iso,
            "sale_date_pattern": fields.sale_date_pattern,
            "loan_amount":       fields.loan_amount,
            "loan_amount_pattern": fields.loan_amount_pattern,
            "legal_description": fields.legal_description,
            "legal_desc_pattern": fields.legal_desc_pattern,
            "apn":               fields.apn,
            "apn_pattern":       fields.apn_pattern,
            "is_hoa_lien":       fields.is_hoa_lien,
            "warnings":          fields.warnings,
        },
    }

    (out / "extraction.json").write_text(
        json.dumps(extraction, indent=2, default=str), encoding="utf-8",
    )

    # 4. Per-record markdown summary
    md_lines = [
        f"# OCR forensic: record_id={record_id}",
        "",
        "## Production state (from data/records.json)",
        "",
        "```json",
        json.dumps(production_state, indent=2, default=str),
        "```",
        "",
        "## Current OCR extraction",
        "",
        "```json",
        json.dumps(extraction["extracted_fields"], indent=2, default=str),
        "```",
        "",
        "## Pages captured",
        "",
    ]
    for pf in page_findings:
        md_lines.extend([
            f"- **page_{pf['page']:02d}** "
            f"({pf['image_size']}, {pf['ocr_chars']} OCR chars, "
            f"{pf['ocr_elapsed_s']}s) → see `{pf['png']}` + `{pf['txt']}`",
        ])
    md_lines.extend([
        "",
        "## Diagnostic questions",
        "",
        "- Does the PNG show a legible scan, or is the underlying image poor quality?",
        "- Is the field present in the document? (i.e. did the writer include a "
        "labeled Property Address, or only \"Commonly known as\", or only inline?)",
        "- Did our regex hit the right region but mis-parse, OR miss the region entirely?",
        "- Did Tesseract mangle the text *within* the field region?",
        "",
    ])
    (out / "REPORT.md").write_text("\n".join(md_lines), encoding="utf-8")

    return extraction


def write_summary(per_record_results: dict[str, dict]) -> None:
    """Aggregate markdown summary across all probed records."""
    lines = [
        "# OCR Forensic Summary",
        "",
        "Generated by `scripts/probe_ocr_pages.py`. Each record's full",
        "OCR text + PNG + extraction lives in `docs/ocr_forensic/<record_id>/`.",
        "",
        "## Failure-case overview",
        "",
        "| record_id | grantor (production) | address_normalized (production) | "
        "grantor (OCR extracted) | grantor_pattern | address (OCR extracted) | address_pattern |",
        "|---|---|---|---|---|---|---|",
    ]
    for rid, result in per_record_results.items():
        ps = result["production_state"]
        ef = result["extracted_fields"]
        lines.append(
            f"| `{rid}` | "
            f"{ps.get('grantor')!r} | "
            f"{ps.get('address_normalized')!r} | "
            f"{ef.get('grantor')!r} | "
            f"`{ef.get('grantor_pattern')}` | "
            f"{ef.get('property_address')!r} | "
            f"`{ef.get('address_pattern')}` |"
        )
    lines.extend([
        "",
        "## Per-record reports",
        "",
    ])
    for rid in per_record_results:
        lines.append(f"- [{rid}](./{rid}/REPORT.md)")
    lines.extend([
        "",
        "## What to look for, per record",
        "",
        "Open each `<record_id>/page_01.png` and inspect:",
        "",
        "1. **Is the document legible?** If Tesseract produced 0-100 chars",
        "   for a multi-page notice, the scan itself is bad — no extractor",
        "   can recover.",
        "2. **Where is the grantor labeled?** Common forms:",
        "   - `Grantor(s): NAME`",
        "   - `Original Mortgagor: NAME`",
        "   - Inline `with NAME, Grantor(s) and...`",
        "   - ServiceLink `WHEREAS, on <DATE>, NAME, WIFE AND HUSBAND, ...`",
        "   - `NAME (\"Borrower\")`",
        "3. **Where is the property address labeled?** Common forms:",
        "   - `Property Address: <addr>`",
        "   - `Commonly known as: <addr>`",
        "   - `Reported Address: <addr>`",
        "   - Top-of-doc unlabeled",
        "4. **Did our pattern hit?** Check `grantor_pattern` / `address_pattern`",
        "   in the extraction JSON. `None` = no pattern hit. Otherwise it",
        "   tells you which regex fired — which lets us decide whether to",
        "   tighten THAT specific pattern or add a new one.",
        "",
    ])
    SUMMARY = OUT_DIR / "SUMMARY.md"
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote summary: %s", SUMMARY)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records", nargs="*", default=None,
                    help="Record IDs to probe. Default = known failure cases.")
    ap.add_argument("--all-unresolved", action="store_true",
                    help="Probe every record without dcad_account.")
    ap.add_argument("--records-json", type=Path,
                    default=REPO_ROOT / "data" / "records.json",
                    help="records.json to read production state from.")
    args = ap.parse_args()

    # Load records.json
    with args.records_json.open("r", encoding="utf-8") as f:
        envelope = json.load(f)
    all_records = envelope.get("records", envelope) if isinstance(envelope, dict) else envelope

    if args.all_unresolved:
        target_ids = [r["record_id"] for r in all_records
                      if r.get("dallas_code") == "NOF"
                      and not r.get("dcad_account")]
    elif args.records:
        target_ids = args.records
    else:
        target_ids = DEFAULT_FAILURE_CASES

    logger.info("Probing %d records: %s", len(target_ids), target_ids)

    # Set up Playwright + Tesseract (reuse foreclosure_ocr's helpers)
    from scraper import config
    from scraper.foreclosure_ocr import _find_tesseract

    tess_cmd = _find_tesseract()
    if not tess_cmd:
        sys.exit("ERROR: Tesseract not found. Install per "
                "scraper/foreclosure_ocr.py docstring.")
    logger.info("Tesseract: %s", tess_cmd)

    base_url = config.PUBLICSEARCH_BASE

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    per_record_results: dict[str, dict] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for rid in target_ids:
                production_state = _save_records_json_state(rid, all_records)
                try:
                    result = probe_one(rid, page, tess_cmd, base_url,
                                       production_state)
                    per_record_results[rid] = result
                except Exception as e:
                    logger.error("Probe failed for %s: %s", rid, e)
                    per_record_results[rid] = {
                        "production_state": production_state,
                        "_error": f"{type(e).__name__}: {e}",
                        "extracted_fields": {},
                    }
        finally:
            browser.close()

    write_summary(per_record_results)

    # Bundle everything into a zip for easy sharing with the reviewer.
    zip_path = REPO_ROOT / "ocr_forensic_bundle.zip"
    bundle_zip(zip_path)

    print()
    print("=" * 72)
    print(f"Done. Probed {len(per_record_results)} records.")
    print(f"Open {OUT_DIR}/SUMMARY.md for the aggregate table.")
    print(f"Each record's PNG + raw OCR + extraction is under "
          f"{OUT_DIR}/<record_id>/")
    print()
    print(f"📦 BUNDLED ZIP for sharing: {zip_path}")
    print(f"   Size: {zip_path.stat().st_size / 1024:.1f} KB")
    print("   Upload this zip to the chat and Claude will extract + view the PNGs.")
    print("=" * 72)
    return 0


def bundle_zip(zip_path: Path) -> None:
    """Bundle the whole docs/ocr_forensic/ tree into a single zip."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(OUT_DIR.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(OUT_DIR.parent)
                zf.write(path, arcname)
    logger.info("Wrote zip bundle: %s (%.1f KB)",
                zip_path, zip_path.stat().st_size / 1024)


if __name__ == "__main__":
    sys.exit(main())
