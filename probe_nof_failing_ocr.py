"""Probe failing NOF OCR records (one-off recon, not part of pipeline).

For a list of publicsearch.us record IDs (the digits at the end of
/doc/{id}), this:

  1. Captures every PNG page via existing capture_with_retry() pipeline.
  2. OCRs each page separately (so we see per-page text, not joined).
  3. Runs the current extract_fields_from_text() to see what the
     existing ADDRESS_PATTERNS / GRANTOR_PATTERNS / etc. match.
  4. Runs a handful of side diagnostics (APN-present?, EXHIBIT A
     present?, BEING UNIT/LOT present?, HOA_LIEN_RE match?).
  5. Writes:
       data/probes/nof_failing_ocr.json   (small, per-page text + diag)
       data/probes/nof_failing_pngs.zip   (bigger, raw PNGs)

Must be run locally — publicsearch.us blocks the cloud container's IP
(curl returns 403 from the sandbox).

Usage::

    python probe_nof_failing_ocr.py 315524267 315562554 315562567

    python probe_nof_failing_ocr.py --from-records-json --limit 8
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import zipfile
from pathlib import Path

# Project imports — assumes cwd is repo root
REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from scraper import config  # noqa: E402
from scraper.foreclosure_ocr import (  # noqa: E402
    HOA_LIEN_RE,
    _find_tesseract,
    capture_with_retry,
    extract_fields_from_text,
)
from scraper.output import read_records_json  # noqa: E402
from scraper.publicsearch import _open_home, browser_context  # noqa: E402

BASE_URL = "https://dallas.tx.publicsearch.us"

# Side-diagnostic regexes. Looser than the production patterns; here we
# just want to know "is there an APN/EXHIBIT/UNIT marker anywhere in the
# combined text?" so we can prove or kill the hypotheses about why the
# current ADDRESS_PATTERNS missed the address.
APN_RE      = re.compile(r"\bAPN\s*[#:]?\s*(\d{8,20})", re.I)
EXHIBIT_RE  = re.compile(r"EXHIBIT\s*[\"']?\s*A\b", re.I)
BEING_UNIT  = re.compile(r"BEING\s+UNIT\s+([A-Z0-9\-]+)", re.I)
BEING_LOT   = re.compile(r"BEING\s+LOT\s+(\d+)", re.I)
ASSESS_LIEN = re.compile(r"ASSESSMENT\s+LIEN", re.I)  # weaker than HOA_LIEN_RE


def candidates_from_records_json(records_path: Path, limit: int) -> list[str]:
    """Auto-detect failing NOF record IDs from local records.json.

    A record is "failing" iff its category is foreclosure_nof AND any of:
      - dcad_account is null/missing (DCAD match failed), OR
      - OCR ran (signal_metadata.ocr.pages_captured >= 1) but
        signal_metadata.ocr.address_pattern is None, OR
      - address_normalized has no digit in its first comma-segment
        (city-only, no street number).

    limit=0 means no cap.
    """
    if not records_path.exists():
        print(f"records.json not found at {records_path}", file=sys.stderr)
        return []
    data = read_records_json(records_path)
    out: list[str] = []
    for rec in data:
        if rec.get("category") != "foreclosure_nof":
            continue
        sm = rec.get("signal_metadata") or {}
        ocr = sm.get("ocr") or {}
        addr = (rec.get("address_normalized") or "").strip()
        first_segment = addr.split(",")[0].strip() if addr else ""
        is_city_only = bool(first_segment) and not any(c.isdigit() for c in first_segment)
        ocr_missed   = ocr.get("pages_captured", 0) >= 1 and not ocr.get("address_pattern")
        no_dcad      = not rec.get("dcad_account")
        if is_city_only or ocr_missed or no_dcad:
            rid = str(rec.get("source_id") or rec.get("record_id") or "").strip()
            if rid:
                out.append(rid)
        if limit > 0 and len(out) >= limit:
            break
    return out


def all_nof_ids_from_records_json(records_path: Path) -> list[str]:
    """Return every foreclosure_nof record's publicsearch id. No filtering."""
    if not records_path.exists():
        print(f"records.json not found at {records_path}", file=sys.stderr)
        return []
    data = read_records_json(records_path)
    out: list[str] = []
    for rec in data:
        if rec.get("category") != "foreclosure_nof":
            continue
        rid = str(rec.get("source_id") or rec.get("record_id") or "").strip()
        if rid:
            out.append(rid)
    return out


def probe(record_ids: list[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tess_cmd = _find_tesseract()
    if not tess_cmd:
        print("ERROR: tesseract binary not found on PATH", file=sys.stderr)
        sys.exit(1)
    import pytesseract  # noqa: WPS433
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

    print(f"Probing {len(record_ids)} record(s): {record_ids}")
    results: dict[str, dict] = {}
    zip_path  = out_dir / "nof_failing_pngs.zip"
    json_path = out_dir / "nof_failing_ocr.json"

    with browser_context() as (_b, _ctx, page):
        _open_home(page)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, rid in enumerate(record_ids, 1):
                rid = rid.strip()
                if not rid:
                    continue
                print(f"\n=== [{i}/{len(record_ids)}] record_id={rid} ===")
                t0 = time.perf_counter()
                cap = capture_with_retry(page, rid, BASE_URL)
                t_cap = time.perf_counter() - t0

                rec: dict = {
                    "record_id":       rid,
                    "capture_status":  cap.status,
                    "capture_seconds": round(t_cap, 1),
                    "n_pages":         len(cap.pages),
                    "attempts":        getattr(cap, "attempts", None),
                    "page_text":       {},
                    "page_dimensions": {},
                    "full_text_len":   0,
                    "diagnostics":     {},
                    "extracted_fields": None,
                }
                print(f"  capture: status={cap.status} n_pages={len(cap.pages)} t={t_cap:.1f}s")

                if cap.status != "ok" or not cap.pages:
                    results[rid] = rec
                    continue

                # OCR every page individually so we can see where the
                # APN / Exhibit-A / address actually lives. PSM=6 OEM=1
                # matches what the production pipeline uses.
                from PIL import Image  # noqa: WPS433
                from io import BytesIO  # noqa: WPS433
                full_parts: list[str] = []
                for pn in sorted(cap.pages.keys()):
                    png = cap.pages[pn]
                    img = Image.open(BytesIO(png))
                    text = pytesseract.image_to_string(img, config="--psm 6 --oem 1")
                    rec["page_text"][str(pn)] = text
                    rec["page_dimensions"][str(pn)] = f"{img.size[0]}x{img.size[1]} {img.mode}"
                    full_parts.append(text)
                    zf.writestr(f"{rid}/p{pn}.png", png)
                    print(f"  page {pn}: {len(text)} chars  ({img.size[0]}x{img.size[1]}, {len(png)} bytes)")
                full_text = "\n".join(full_parts)
                rec["full_text_len"] = len(full_text)

                # Side diagnostics — does the combined text have any of
                # the markers we hypothesized from the screenshots?
                apn_match     = APN_RE.search(full_text)
                exhibit_match = EXHIBIT_RE.search(full_text)
                unit_match    = BEING_UNIT.search(full_text)
                lot_match     = BEING_LOT.search(full_text)
                hoa_strict    = HOA_LIEN_RE.search(full_text)
                hoa_loose     = ASSESS_LIEN.search(full_text)
                rec["diagnostics"] = {
                    "apn_found":             apn_match.group(1) if apn_match else None,
                    "exhibit_a_marker":      bool(exhibit_match),
                    "being_unit":            unit_match.group(1) if unit_match else None,
                    "being_lot":             lot_match.group(1) if lot_match else None,
                    "hoa_lien_strict_match": bool(hoa_strict),
                    "hoa_lien_loose_match":  bool(hoa_loose),
                }

                # What does the existing production extractor do?
                fields = extract_fields_from_text(full_text)
                rec["extracted_fields"] = {
                    "grantor":            fields.grantor,
                    "grantor_pattern":    fields.grantor_pattern,
                    "sale_date_iso":      fields.sale_date_iso,
                    "property_address":   fields.property_address,
                    "address_pattern":    fields.address_pattern,
                    "loan_amount":        fields.loan_amount,
                    "legal_description":  fields.legal_description,
                    "legal_desc_pattern": fields.legal_desc_pattern,
                    "is_hoa_lien":        fields.is_hoa_lien,
                    "warnings":           list(fields.warnings),
                }
                print(
                    f"  diag: apn={rec['diagnostics']['apn_found']!r} "
                    f"exhibit_a={rec['diagnostics']['exhibit_a_marker']} "
                    f"unit={rec['diagnostics']['being_unit']!r} "
                    f"lot={rec['diagnostics']['being_lot']!r} "
                    f"hoa_strict={rec['diagnostics']['hoa_lien_strict_match']}"
                )
                print(
                    f"  extracted: addr={fields.property_address!r} "
                    f"pattern={fields.address_pattern!r} "
                    f"is_hoa_lien={fields.is_hoa_lien}"
                )
                results[rid] = rec

    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print()
    print(f"Wrote {json_path}  ({json_path.stat().st_size:,} bytes)")
    print(f"Wrote {zip_path}   ({zip_path.stat().st_size:,} bytes)")
    print()
    print("Upload nof_failing_ocr.json to me — that's the small one I need.")
    print("Only send the .zip if I ask for it after reading the JSON.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record_ids", nargs="*", help="publicsearch.us /doc/{id} digits")
    ap.add_argument("--from-records-json", action="store_true",
                    help="Auto-detect FAILING NOFs from data/records.json "
                         "(no dcad_account, or city-only address, or OCR-missed)")
    ap.add_argument("--all-nofs", action="store_true",
                    help="Probe EVERY foreclosure_nof record in data/records.json "
                         "(not just failing). Useful for measuring APN/Exhibit-A "
                         "frequency across the full population.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max records to probe (0 = no cap, the default).")
    args = ap.parse_args()

    records_path = REPO_ROOT / "data" / "records.json"

    if args.all_nofs:
        ids = all_nof_ids_from_records_json(records_path)
        if not ids:
            print("No foreclosure_nof records found in records.json", file=sys.stderr)
            sys.exit(1)
        if args.limit > 0:
            ids = ids[:args.limit]
        print(f"Probing ALL {len(ids)} foreclosure_nof records from records.json")
    elif args.from_records_json:
        ids = candidates_from_records_json(records_path, args.limit)
        if not ids:
            print("No failing NOF candidates detected in records.json", file=sys.stderr)
            sys.exit(1)
        print(f"Auto-detected {len(ids)} FAILING NOF candidate IDs from records.json")
    else:
        ids = args.record_ids

    if not ids:
        ap.print_help()
        sys.exit(1)

    out_dir = REPO_ROOT / "data" / "probes"
    probe(ids, out_dir)


if __name__ == "__main__":
    main()
