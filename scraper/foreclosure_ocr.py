"""OCR-based enrichment for publicsearch.us foreclosure-notice records.

Each foreclosures_ps record carries only what the SPA's list view exposes:
record_id, recorded_date, sale_date, doc_number, sometimes a city-only
property_address. The actual NOTICE documents — PNG scans hosted at
``/files/documents/{record_id}/images/{IMG_ID}_{N}.png?exp=...&sig=...`` —
contain the operationally important fields:

  - Grantor / homeowner name (the motivated seller)
  - Full property street address
  - Loan amount, current mortgagee, deed-of-trust date
  - Legal description (subdivision + lot + block, feeds DCAD legal-resolver)

This module captures those PNGs, OCRs them with Tesseract, and runs a
universal-pattern extractor that handles all 12+ notice templates we
observed in a 58-sample audit. We deliberately do NOT try to classify
template-by-template — 45% of real notices didn't match any template
signature in the audit, while the universal patterns extracted fields
from those records anyway. A pattern-soup approach has been validated
to outperform format detection on this corpus.

Validated patterns by field (with cross-format hit rates from audit):

  Grantor (try in priority order):
    Grantor(s)/Mortgagor(s):              Format I (McCarthy)
    Grantor(s):                           Format A, C, F, others
    Trustor(s):                           Format D (Prestige)
    Mortgagor(s):                         variant
    ORIGINAL MORTGAGOR:                   Format M (Matter No. style)
    with NAME, grantor(s) and             Format B, H inline
    executed by NAME, A SINGLE|MARRIED    Format D, F mid-narrative
    Deed of Trust executed by NAME secures Format K
    NAME ("Borrower")                     Format J private trustee

  Sale date: word + numeric + section-anchored + narrative ("NOTICE IS HEREBY GIVEN that on")
  Property address: Property Address: / Commonly known as: / top-of-doc unlabeled
                    Falls back to DCAD legal-resolver via legal description.
  Loan amount: original principal amount of / Original Principal: / Amount: / original amount of
  Legal description: Legal Description: / EXHIBIT "A" / inline BEING LOT N, BLOCK M

HOA-lien sales (NOTICE OF ASSESSMENT LIEN SALE) are suppressed
explicitly — those records are filed under the same NOF doctype on
publicsearch.us but are condo-association liens, not mortgage
foreclosures (not motivated-seller leads).

Cost on the pipeline:
  ~6-10 seconds per record (Playwright capture + Tesseract OCR).
  For 60-80 records per weekly run, total added time is ~10-15 min.
  Gated by FORECLOSURE_OCR_ENABLED env var; default off so operators
  can keep fast iteration on other parts of the pipeline.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config
from .publicsearch import browser_context

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Tesseract binary detection
# ═══════════════════════════════════════════════════════════════════════════

def _find_tesseract() -> Optional[str]:
    """Locate the Tesseract binary across platforms.

    Detection order: TESSERACT_CMD env var, $PATH, Windows default install,
    standard Unix paths.
    """
    if cmd := os.environ.get("TESSERACT_CMD"):
        if Path(cmd).exists():
            return cmd
    if which := shutil.which("tesseract"):
        return which
    for candidate in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Universal extraction patterns
# ═══════════════════════════════════════════════════════════════════════════

# Grantor / homeowner — try in priority order, first match wins.
# OCR-noise notes:
#   - Tesseract sometimes renders "(" as curly "‘" or "'"; we allow both
#     in label punctuation via [‘'(]?.
#   - The Format-B inline "with NAME, grantor(s) and" pattern drops the
#     trailing "and" when OCR garbles it (e.g. "grantor(s) ay") — we make
#     the "and" optional.
GRANTOR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Grantor[\(‘'\s]?s?[\)’'\s]?\s*/\s*Mortgagor[\(‘'\s]?s?[\)’'\s]?\s*:\s*([^\n]+)", re.I),
     "labeled-grantor-mortgagor"),
    (re.compile(r"Grantor[\(‘'\s]?s?[\)’'\s]?\s*:\s*([^\n]+)", re.I),
     "labeled-grantor"),
    (re.compile(r"Trustor[\(‘'\s]?s?[\)’'\s]?\s*:\s*([A-Z][^\n]+)", re.I),
     "labeled-trustor"),
    (re.compile(r"ORIGINAL\s+MORTGAGOR\s*:\s*([A-Z][^\n]+)", re.I),
     "labeled-original-mortgagor"),
    (re.compile(r"Mortgagor[\(‘'\s]?s?[\)’'\s]?\s*:\s*([A-Z][^\n]+)", re.I),
     "labeled-mortgagor"),
    # Format B / H inline form. The trailing "and" is what comes after
    # "grantor(s)" in clean text, but OCR sometimes turns it into "ay" or
    # other garbage, so accept any non-comma trailing word.
    (re.compile(r"with\s+([A-Z][A-Z\s,.&'-]+?),?\s+grantor[\(‘'\s]?s?[\)’'\s]?\s+\w+", re.I),
     "inline-with-grantor"),
    (re.compile(r"executed\s+by\s+([A-Z][^,\n]+?),?\s+(?:A\s+SINGLE|MARRIED|wife|husband)", re.I),
     "executed-by-status"),
    (re.compile(r"Deed\s+of\s+Trust\s+executed\s+by\s+([A-Z][^,\n]+?)\s+secures", re.I),
     "deed-executed-by-secures"),
    (re.compile(r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)\s*\(\s*[\"']Borrower[\"']\s*\)"),
     "name-quoted-borrower"),
]

# Sale date — both word and numeric formats; section-anchored when possible.
SALE_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Date\s+of\s+Sale\s*[:\-]?\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", re.I),
     "word-date-of-sale"),
    (re.compile(r"Date\s+of\s+Sale\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I),
     "numeric-date-of-sale"),
    (re.compile(
        r"Date,?\s+Time,?\s+and\s+Place\s+of\s+Sale[^A-Za-z\d]*?\bDate\s*:\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
        re.I | re.DOTALL), "word-section1-date"),
    (re.compile(
        r"Date,?\s+Time,?\s+and\s+Place\s+of\s+Sale[^A-Za-z\d]*?\bDate\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        re.I | re.DOTALL), "numeric-section1-date"),
    (re.compile(
        r"NOTICE\s+IS\s+HEREBY\s+GIVEN\s+that\s+on\s+(?:Tuesday|Monday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
        re.I), "narrative-is-hereby"),
    (re.compile(r"(?:^|\n)\s*Date\s*:\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", re.I | re.M),
     "bare-date-word"),
    (re.compile(r"(?:^|\n)\s*Date\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I | re.M),
     "bare-date-numeric"),
]

# Property address — labeled forms first, top-of-doc unlabeled as fallback.
ADDRESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Property\s+Address\s*[:\-]?\s*([0-9][^\n]{8,}\s+(?:TX|TEXAS)\s+\d{5})", re.I),
     "property-address-one-line"),
    (re.compile(r"Commonly\s+known\s+as\s*[:\-]?\s*([0-9][^\n]{8,}\s+(?:TX|TEXAS)\s+\d{5})", re.I),
     "commonly-known-as-one-line"),
    # Format C-variant: "Reported Address: <full>"
    (re.compile(r"Reported\s+Address\s*[:\-]?\s*([0-9][^\n]{8,}\s+(?:TX|TEXAS)\s+\d{5})", re.I),
     "reported-address"),
    (re.compile(
        r"Property\s+Address\s*[:\-]?\s*([0-9][^\n]+)\n\s*([^\n]+,?\s*(?:TX|TEXAS)\s+\d{5})", re.I),
        "property-address-two-line"),
    # Top of doc: number + street name on one line, city + state + zip
    # within the next ~3 lines (allows OCR garbage / barcode digits to
    # intervene -- file_011 had "2808 QUAIL RUN DR / 000000 10790376 /
    # MESQUITE, TX 75149").
    (re.compile(
        r"(?:^|\n)\s*([0-9]+\s+[A-Z][A-Z\s\.'-]+(?:DR|DRIVE|ST|STREET|AVE|AVENUE|RD|ROAD|LN|LANE|BLVD|BOULEVARD|CT|COURT|CIR|CIRCLE|PL|PLACE|WAY|TRL|TRAIL|PKWY|HWY|TER|TERRACE))\s*\n(?:[^\n]*\n){0,2}\s*([A-Z][A-Z\s]+,\s*(?:TX|TEXAS)\s+\d{5})",
        re.I), "top-of-doc-2line"),
]

# Loan amount.
LOAN_AMOUNT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"original\s+principal\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)", re.I),
     "original-principal-amount-of"),
    (re.compile(r"Original\s+Principal\s*[:\-]?\s*\$\s*([\d,]+(?:\.\d{2})?)", re.I),
     "labeled-original-principal"),
    (re.compile(r"original\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)", re.I),
     "original-amount-of"),
    (re.compile(r"(?:^|\n)\s*Amount\s*:\s*\$\s*([\d,]+(?:\.\d{2})?)", re.I | re.M),
     "labeled-amount"),
    (re.compile(r"principal\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)", re.I),
     "principal-amount-of"),
]

# Legal description (feeds DCAD legal-resolver for properties without
# OCR-extracted street address).
LEGAL_DESC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(
        r"Legal\s+Description\s*[:\-]?\s*([^\n]+(?:\n[^\n]+){0,4}?(?:COUNTY,?\s+TEXAS\.?|TEXAS\.?))",
        re.I), "labeled-legal-description"),
    (re.compile(
        r"EXHIBIT\s*[\"']?\s*A\s*[\"']?[^L]*?(BEING\s+LOT\s+\d+[^\n]+(?:\n[^\n]+){0,4}?COUNTY,?\s+TEXAS\.?)",
        re.I), "exhibit-A-being-lot"),
    (re.compile(
        r"(BEING\s+LOT\s+\d+[^\n]+(?:\n[^\n]+){0,4}?COUNTY,?\s+TEXAS\.?)", re.I),
     "being-lot"),
    (re.compile(
        r"(LOT\s+\d+[,.]?\s+(?:IN\s+)?BLOCK\s+[\d/]+[^\n]+(?:\n[^\n]+){0,4}?COUNTY,?\s+TEXAS\.?)",
        re.I), "lot-block"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Pure-function extractor (testable without Playwright or Tesseract)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExtractedFields:
    """Result of universal-pattern extraction over OCR text."""
    grantor:          Optional[str] = None
    grantor_pattern:  Optional[str] = None
    sale_date_raw:    Optional[str] = None
    sale_date_iso:    Optional[str] = None
    sale_date_pattern: Optional[str] = None
    property_address: Optional[str] = None
    address_pattern:  Optional[str] = None
    loan_amount:      Optional[str] = None
    loan_amount_pattern: Optional[str] = None
    legal_description: Optional[str] = None
    legal_desc_pattern: Optional[str] = None
    is_hoa_lien:      bool = False
    warnings:         list[str] = field(default_factory=list)


HOA_LIEN_RE = re.compile(r"NOTICE\s+OF\s+ASSESSMENT\s+LIEN\s+SALE", re.I)


def is_hoa_lien(ocr_text: str) -> bool:
    """True if the OCR text indicates an HOA/condo assessment-lien sale
    rather than a mortgage foreclosure. Those records should be suppressed."""
    return bool(HOA_LIEN_RE.search(ocr_text))


def _try_patterns(text: str, patterns: list[tuple[re.Pattern, str]]) -> tuple[Optional[str], Optional[str]]:
    """Try each (regex, label) pair in order; return first match + label.

    When a pattern has multiple capture groups (e.g. the 2-line address
    matcher: street on group 1, city/state/zip on group 2), join them
    with a comma so the caller gets a single-string address.
    """
    for pat, label in patterns:
        m = pat.search(text)
        if m:
            if m.lastindex and m.lastindex > 1:
                # Multi-group: join all non-empty groups with ", "
                parts = [g.strip() for g in m.groups() if g and g.strip()]
                captured = ", ".join(parts)
            else:
                captured = m.group(1).strip()
            # Collapse whitespace and strip trailing punctuation OCR leaves.
            captured = re.sub(r"\s+", " ", captured).rstrip(".,;: ")
            if captured:
                return (captured, label)
    return (None, None)


_MONTH_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _to_iso_date(raw: str) -> Optional[str]:
    """Normalize 'July 7, 2026' or '7/7/2026' or '07/07/26' -> '2026-07-07'."""
    if not raw:
        return None
    raw = raw.strip().rstrip(".,")
    # Month-name form
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", raw)
    if m:
        month_num = _MONTH_TO_NUM.get(m.group(1).lower())
        if month_num:
            return f"{int(m.group(3)):04d}-{month_num:02d}-{int(m.group(2)):02d}"
    # Numeric form
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:  # 2-digit year
            yr = 2000 + yr if yr < 50 else 1900 + yr
        return f"{yr:04d}-{mo:02d}-{da:02d}"
    return None


def extract_fields_from_text(ocr_text: str) -> ExtractedFields:
    """Pure function: run universal patterns over OCR text and return
    extracted fields. No side effects, no I/O. Unit-testable."""
    out = ExtractedFields()

    if not ocr_text or not ocr_text.strip():
        out.warnings.append("empty_ocr_text")
        return out

    # HOA-lien suppression — non-foreclosure noise that publicsearch.us
    # files under the same NOF doctype.
    if is_hoa_lien(ocr_text):
        out.is_hoa_lien = True
        out.warnings.append("hoa_assessment_lien_sale")
        return out

    out.grantor, out.grantor_pattern = _try_patterns(ocr_text, GRANTOR_PATTERNS)
    out.sale_date_raw, out.sale_date_pattern = _try_patterns(ocr_text, SALE_DATE_PATTERNS)
    out.sale_date_iso = _to_iso_date(out.sale_date_raw) if out.sale_date_raw else None
    out.property_address, out.address_pattern = _try_patterns(ocr_text, ADDRESS_PATTERNS)
    out.loan_amount, out.loan_amount_pattern = _try_patterns(ocr_text, LOAN_AMOUNT_PATTERNS)
    out.legal_description, out.legal_desc_pattern = _try_patterns(ocr_text, LEGAL_DESC_PATTERNS)

    if not out.grantor:
        out.warnings.append("no_grantor_extracted")
    if not out.sale_date_iso:
        out.warnings.append("no_sale_date_extracted")
    if not out.property_address and not out.legal_description:
        out.warnings.append("no_address_or_legal_description")

    return out


# ═══════════════════════════════════════════════════════════════════════════
# Capture (Playwright)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CaptureResult:
    record_id: str
    pages: dict[int, bytes] = field(default_factory=dict)
    status: str = "ok"            # ok | no_images | error
    warnings: list[str] = field(default_factory=list)


def _capture_one(page, record_id: str, base_url: str,
                 passive_wait_s: float = 4.0,
                 per_page_wait_s: float = 1.0) -> CaptureResult:
    """Capture all PNG pages from /doc/{record_id} using a single Playwright
    page. Reads response.body() inline while the page is alive — no
    requests-with-headers race.

    Strategy:
      1. Register response handler to collect PNGs by page number.
      2. goto(/doc/{id}); passive wait for SPA hydration + page-1 fetch.
      3. Detect total page count from viewer DOM ("of N" text).
      4. For each remaining page: click 'Go To Next Page' button and
         use expect_response to actively wait for the matching image
         response.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    result = CaptureResult(record_id=record_id)

    def on_response(response):
        url = response.url
        m = re.search(r"_(\d+)\.png", url)
        if not (m and "/files/documents/" in url and ".png" in url):
            return
        pn = int(m.group(1))
        if pn in result.pages:
            return
        try:
            body = response.body()
            if body and len(body) > 100:
                result.pages[pn] = body
        except Exception:
            pass

    page.on("response", on_response)

    try:
        page.goto(f"{base_url}/doc/{record_id}",
                  wait_until="networkidle", timeout=30_000)
    except Exception as e:
        page.remove_listener("response", on_response)
        result.status = "error"
        result.warnings.append(f"goto_failed: {e}")
        return result

    time.sleep(passive_wait_s)

    # Detect total page count from the viewer's "X of N" text.
    total_pages: Optional[int] = None
    try:
        body_text = page.locator("body").inner_text()
        m = re.search(r"\bof\s+(\d+)\b", body_text)
        if m:
            total_pages = int(m.group(1))
    except Exception:
        pass

    if total_pages and len(result.pages) < total_pages:
        for target_page in range(2, total_pages + 1):
            if target_page in result.pages:
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
                    result.pages[target_page] = body
                time.sleep(per_page_wait_s)
            except PlaywrightTimeoutError:
                result.warnings.append(f"page_{target_page}_timeout")
            except Exception as e:
                result.warnings.append(f"page_{target_page}_error: {e}")

    page.remove_listener("response", on_response)

    if not result.pages:
        result.status = "no_images"

    return result


def capture_with_retry(page, record_id: str, base_url: str,
                       max_attempts: int = 2) -> CaptureResult:
    """Capture with one retry on no_images. Mitigates the ~15% transient
    miss rate we observed where the SPA hadn't finished hydrating before
    our passive wait expired."""
    for attempt in range(1, max_attempts + 1):
        wait_s = 4.0 if attempt == 1 else 8.0
        result = _capture_one(page, record_id, base_url, passive_wait_s=wait_s)
        if result.status == "ok":
            if attempt > 1:
                result.warnings.append(f"recovered_on_attempt_{attempt}")
            return result
        if attempt < max_attempts:
            logger.warning(
                "OCR capture got 0 images for %s (attempt %d); retrying with %.1fs wait",
                record_id, attempt, 8.0,
            )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# OCR (Tesseract)
# ═══════════════════════════════════════════════════════════════════════════

def _ocr_pages(pages: dict[int, bytes], tess_cmd: str) -> str:
    """Run Tesseract on each captured PNG and concatenate the text in page
    order. Returns combined OCR text."""
    import pytesseract
    from PIL import Image
    from io import BytesIO

    pytesseract.pytesseract.tesseract_cmd = tess_cmd
    out_parts: list[str] = []
    for pn in sorted(pages.keys()):
        try:
            img = Image.open(BytesIO(pages[pn]))
            text = pytesseract.image_to_string(img)
            out_parts.append(text)
        except Exception as e:
            logger.warning("OCR failed on page %d: %s", pn, e)
    return "\n\n".join(out_parts)


# ═══════════════════════════════════════════════════════════════════════════
# Public API: per-record enrichment + batch
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OCREnrichmentStats:
    total:              int = 0
    captured_ok:        int = 0
    no_images:          int = 0
    capture_errors:     int = 0
    hoa_lien_suppressed: int = 0
    grantor_extracted:  int = 0
    sale_date_extracted: int = 0
    address_extracted:  int = 0
    loan_amount_extracted: int = 0
    legal_desc_extracted: int = 0


def enrich_foreclosure_records(
    records: list[dict[str, Any]],
    base_url: Optional[str] = None,
) -> tuple[list[dict[str, Any]], OCREnrichmentStats]:
    """Enrich a batch of foreclosures_ps canonical records with OCR-extracted
    fields. Reuses one Playwright browser across all records.

    Mutates each record in place:
      - grantor:               filled from OCR if list-view had None
      - address:               filled from OCR if a labeled form was found
      - amount:                set to loan amount if extracted
      - signal_metadata:       dict with OCR provenance (which patterns matched)
      - parse_warnings:        appended with any extractor warnings
      - active:                set to False with reason "hoa_lien_suppression"
                                for assessment-lien records

    Returns (records, stats). HOA-lien records are kept in the list but
    marked active=False so they appear in records.json for audit but not
    in the CSV.
    """
    base_url = base_url or config.PUBLICSEARCH_BASE
    stats = OCREnrichmentStats(total=len(records))

    if not records:
        return records, stats

    tess_cmd = _find_tesseract()
    if not tess_cmd:
        logger.warning(
            "OCR enrichment SKIPPED: Tesseract binary not found. "
            "Install Tesseract or set TESSERACT_CMD env var to the binary path."
        )
        for r in records:
            r.setdefault("parse_warnings", []).append("ocr_skipped_no_tesseract")
        return records, stats

    logger.info("OCR enrichment starting on %d foreclosure records (Tesseract: %s)",
                len(records), tess_cmd)

    with browser_context() as (_browser, _context, page):
        for i, rec in enumerate(records, 1):
            rid = rec.get("record_id")
            if not rid:
                continue
            t0 = time.time()
            try:
                cap = capture_with_retry(page, rid, base_url)
            except Exception as e:
                logger.warning("OCR capture uncaught error for %s: %s", rid, e)
                rec.setdefault("parse_warnings", []).append(f"ocr_capture_error:{type(e).__name__}")
                stats.capture_errors += 1
                continue

            if cap.status == "no_images":
                rec.setdefault("parse_warnings", []).append("ocr_no_images")
                stats.no_images += 1
                continue
            elif cap.status == "error":
                rec.setdefault("parse_warnings", []).append("ocr_capture_error")
                stats.capture_errors += 1
                continue

            stats.captured_ok += 1
            text = _ocr_pages(cap.pages, tess_cmd)
            fields = extract_fields_from_text(text)

            if fields.is_hoa_lien:
                rec["active"] = False
                rec.setdefault("buy_box_reasons", []).append("hoa_assessment_lien")
                rec.setdefault("parse_warnings", []).extend(fields.warnings)
                stats.hoa_lien_suppressed += 1
                logger.info("Suppressed HOA-assessment-lien record %s", rid)
                continue

            # Stamp extracted fields (don't overwrite list-view values
            # unless they were missing).
            if fields.grantor and not rec.get("grantor"):
                rec["grantor"] = fields.grantor
                stats.grantor_extracted += 1
            elif fields.grantor:
                stats.grantor_extracted += 1
            if fields.sale_date_iso and not rec.get("sale_date"):
                rec["sale_date"] = fields.sale_date_iso
                stats.sale_date_extracted += 1
            elif fields.sale_date_iso:
                stats.sale_date_extracted += 1
            if fields.property_address:
                # Always prefer OCR-derived full address over list-view's
                # city-only value when we found a labeled "Property Address:".
                rec["address"] = fields.property_address
                # Re-normalize so DCAD enrichment can find it.
                from . import normalize as _norm
                rec["address_normalized"] = _norm.normalize_address(fields.property_address)
                stats.address_extracted += 1
            if fields.loan_amount:
                rec["amount"] = fields.loan_amount
                stats.loan_amount_extracted += 1
            if fields.legal_description:
                # Stash legal description in raw_excerpt for legal_resolver to
                # consume on its next pass. Don't blow away existing snippet.
                existing = rec.get("raw_excerpt") or ""
                if "LOT" not in existing.upper():
                    rec["raw_excerpt"] = (
                        f"{existing} | {fields.legal_description}"
                    ).strip(" |")[:500]
                stats.legal_desc_extracted += 1

            # Provenance + warnings for monitoring/debugging
            meta = rec.get("signal_metadata") or {}
            meta["ocr"] = {
                "grantor_pattern":      fields.grantor_pattern,
                "sale_date_pattern":    fields.sale_date_pattern,
                "address_pattern":      fields.address_pattern,
                "loan_amount_pattern":  fields.loan_amount_pattern,
                "legal_desc_pattern":   fields.legal_desc_pattern,
                "pages_captured":       len(cap.pages),
                "capture_warnings":     cap.warnings,
            }
            rec["signal_metadata"] = meta
            rec.setdefault("parse_warnings", []).extend(fields.warnings)

            logger.debug(
                "[%d/%d] %s: ocr ok in %.1fs (%d pages, grantor=%s, sale=%s, addr=%s)",
                i, len(records), rid, time.time() - t0,
                len(cap.pages),
                "Y" if fields.grantor else "n",
                "Y" if fields.sale_date_iso else "n",
                "Y" if fields.property_address else "n",
            )

    logger.info(
        "OCR enrichment complete: %d/%d captured; extracted "
        "grantor=%d, sale_date=%d, address=%d, amount=%d, legal=%d; "
        "hoa_suppressed=%d, no_images=%d, errors=%d",
        stats.captured_ok, stats.total,
        stats.grantor_extracted, stats.sale_date_extracted,
        stats.address_extracted, stats.loan_amount_extracted,
        stats.legal_desc_extracted,
        stats.hoa_lien_suppressed, stats.no_images, stats.capture_errors,
    )
    return records, stats
