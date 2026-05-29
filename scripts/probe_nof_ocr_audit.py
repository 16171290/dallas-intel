"""NOF OCR extraction audit — does the foreclosure pipeline extract the
CORRECT name / address from each notice scan?

The OCR'd notice text is not retained in records.json (only the final
extracted fields + pattern labels). So to audit extraction correctness we
re-run the REAL pipeline on each NOF: capture the notice PNG pages, OCR
them, and re-extract fields — then line that up against what the record
actually stored and how it resolved.

For every NOF record this prints:
  - raw OCR text length + a saved per-record dump
  - freshly extracted fields (address / grantor / sale_date / loan / legal)
  - the values the record stored
  - the DCAD match outcome
  - FLAGS: garble (©/§/stray glyphs in street), city_only (no street #),
    reextract_diff (fresh extraction != stored — OCR drift or parser gap),
    hoa_lien, grantor_from_dcad (grantor was reverse-filled, not OCR'd).

This is measurement only — it mutates nothing. It reuses
scraper.foreclosure_ocr's own capture/OCR/extract functions so we audit
the actual production behavior, not a reimplementation.

Usage:
    python scripts/probe_nof_ocr_audit.py
    python scripts/probe_nof_ocr_audit.py --limit 10
    python scripts/probe_nof_ocr_audit.py --record-ids 316708049 315584731
    python scripts/probe_nof_ocr_audit.py --only-unresolved

Requires Tesseract + Playwright + network to publicsearch.us (no login).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("probe_nof_ocr_audit")

DEFAULT_RECORDS = REPO_ROOT / "data" / "records.json"
DEFAULT_DUMP_DIR = REPO_ROOT / "data" / "probes" / "nof_ocr_audit"

# Characters that should never appear in a clean street address — their
# presence is a strong OCR-garble signal.
_GARBLE_RE = re.compile(r"[^A-Za-z0-9 .,#/-]")
# A real street address starts with (or contains early) a house number.
_STREET_NUM_RE = re.compile(r"\b\d{1,6}\b")


def load_records(path: Path) -> list[dict]:
    env = json.load(path.open("r", encoding="utf-8"))
    return env.get("records", env) if isinstance(env, dict) else env


def looks_city_only(addr: str | None) -> bool:
    """True if the address has no street number (just a city/region)."""
    if not addr:
        return True
    return not _STREET_NUM_RE.search(addr)


def garble_chars(s: str | None) -> str:
    """Return the set of garble characters found, as a string (empty if clean)."""
    if not s:
        return ""
    return "".join(sorted(set(_GARBLE_RE.findall(s))))


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records-json", type=Path, default=DEFAULT_RECORDS)
    ap.add_argument("--dump-dir", type=Path, default=DEFAULT_DUMP_DIR)
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of NOF records audited (0 = all)")
    ap.add_argument("--record-ids", nargs="+", default=None,
                    help="Explicit NOF record_ids to audit")
    ap.add_argument("--only-unresolved", action="store_true",
                    help="Audit only NOF records with no dcad_account")
    args = ap.parse_args()

    records = load_records(args.records_json)
    nof = [r for r in records if r.get("dallas_code") == "NOF"]
    if args.record_ids:
        wanted = set(args.record_ids)
        nof = [r for r in nof if r.get("record_id") in wanted]
    if args.only_unresolved:
        nof = [r for r in nof if not r.get("dcad_account")]
    if args.limit:
        nof = nof[:args.limit]
    if not nof:
        print("No NOF records matched the filters.")
        return 1

    args.dump_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Auditing %d NOF records; dumps -> %s", len(nof), args.dump_dir)

    from scraper import config
    from scraper.foreclosure_ocr import (
        _find_tesseract, capture_with_retry, _ocr_pages,
        extract_fields_from_text,
    )
    from scraper.publicsearch import browser_context

    tess = _find_tesseract()
    if not tess:
        logger.error("Tesseract not found — set TESSERACT_CMD or install it.")
        return 2
    base = config.PUBLICSEARCH_BASE
    logger.info("Tesseract: %s | base: %s", tess, base)

    audit: list[dict] = []

    with browser_context() as (_browser, context, page0):
        try:
            page0.close()
        except Exception:
            pass
        for i, rec in enumerate(nof, 1):
            rid = rec.get("record_id")
            logger.info("[%d/%d] capturing + OCR %s", i, len(nof), rid)
            page = context.new_page()
            ocr_text, fields, cap_status, cap_warn = "", None, "?", []
            try:
                cap = capture_with_retry(page, rid, base)
                cap_status = cap.status
                cap_warn = cap.warnings
                if cap.pages:
                    ocr_text = _ocr_pages(cap.pages, tess)
                    fields = extract_fields_from_text(ocr_text)
            except Exception as e:
                cap_warn = [f"probe_exception: {e}"]
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            # Save raw OCR text for eyeballing
            (args.dump_dir / f"{rid}_ocr.txt").write_text(
                ocr_text or "", encoding="utf-8")

            stored_addr = rec.get("address")
            stored_norm = rec.get("address_normalized")
            fresh_addr = fields.property_address if fields else None
            fresh_grantor = fields.grantor if fields else None

            # Flags
            flags = []
            if cap_status != "ok" or not ocr_text:
                flags.append("CAPTURE_FAIL")
            g = garble_chars(fresh_addr or stored_addr or stored_norm)
            if g:
                flags.append(f"GARBLE[{g}]")
            if looks_city_only(fresh_addr or stored_norm):
                flags.append("CITY_ONLY")
            if fields and fields.is_hoa_lien:
                flags.append("HOA_LIEN")
            cw = (rec.get("signal_metadata") or {}).get("confidence_warnings") or []
            if "grantor_from_dcad" in cw:
                flags.append("GRANTOR_FROM_DCAD")
            # fresh-vs-stored consistency (only meaningful if we got OCR)
            if ocr_text and fresh_addr and stored_addr:
                if norm(fresh_addr) != norm(stored_addr):
                    flags.append("REEXTRACT_DIFF")

            audit.append({
                "record_id": rid,
                "resolved": bool(rec.get("dcad_account")),
                "dcad_account": rec.get("dcad_account"),
                "cap_status": cap_status,
                "ocr_len": len(ocr_text or ""),
                "stored_address": stored_addr,
                "stored_norm": stored_norm,
                "fresh_address": fresh_addr,
                "fresh_addr_pattern": fields.address_pattern if fields else None,
                "fresh_grantor": fresh_grantor,
                "fresh_grantor_pattern": fields.grantor_pattern if fields else None,
                "fresh_sale_date": fields.sale_date_iso if fields else None,
                "fresh_loan": fields.loan_amount if fields else None,
                "fresh_legal": fields.legal_description if fields else None,
                "flags": flags,
                "cap_warnings": cap_warn,
            })

    # Save full audit JSON
    (args.dump_dir / "_audit.json").write_text(
        json.dumps(audit, indent=2, default=str), encoding="utf-8")

    # ---- Report ----
    print()
    print("=" * 90)
    print("NOF OCR EXTRACTION AUDIT")
    print("=" * 90)
    print(f"records audited: {len(audit)}   dumps: {args.dump_dir}/{{rid}}_ocr.txt")
    print()

    from collections import Counter
    flag_counts: Counter = Counter()
    for a in audit:
        for f in a["flags"]:
            flag_counts[f.split("[")[0]] += 1
    print("FLAG SUMMARY:")
    for f, n in flag_counts.most_common():
        print(f"  {n:>3}  {f}")
    print()

    for a in audit:
        status = "RESOLVED" if a["resolved"] else "UNRESOLVED"
        print(f"── {a['record_id']}  [{status}]  ocr_len={a['ocr_len']}  "
              f"cap={a['cap_status']}  flags={a['flags']}")
        print(f"     OCR address : {a['fresh_address']!r}  ({a['fresh_addr_pattern']})")
        print(f"     stored addr : {a['stored_address']!r}  -> norm {a['stored_norm']!r}")
        print(f"     OCR grantor : {a['fresh_grantor']!r}  ({a['fresh_grantor_pattern']})")
        if a["fresh_sale_date"] or a["fresh_loan"]:
            print(f"     sale_date={a['fresh_sale_date']}  loan={a['fresh_loan']}")
        if a["cap_warnings"]:
            print(f"     cap_warnings: {a['cap_warnings']}")
        print()

    print("Read the saved _ocr.txt for any flagged record to see exactly what")
    print("Tesseract produced vs what we parsed. GARBLE = OCR misread; CITY_ONLY")
    print("= street line not captured; REEXTRACT_DIFF = parser/OCR drift vs stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
