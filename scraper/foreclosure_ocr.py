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
import multiprocessing
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

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

# USPS street suffixes consolidated. Used in both the all-caps top-of-doc
# pattern and the new mixed-case unlabeled patterns added per the 41-record
# NOF probe (2026-05-26) that showed 22/28 unmatched records had a street
# address visible but in mixed-case / garbage-laden format the current
# all-uppercase patterns rejected.
_STREET_SUFFIX_GROUP = (
    r"(?:Street|St|Drive|Dr|Avenue|Ave|Road|Rd|Lane|Ln|Boulevard|Blvd|"
    r"Court|Ct|Circle|Cir|Place|Pl|Way|Trail|Trl|Parkway|Pkwy|"
    r"Highway|Hwy|Terrace|Ter)"
)

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
    # ServiceLink format (added per probe 2026-05-26):
    #   "WHEREAS, on April 16, 2010, LAPRENSA GRANT AND REGIONALD GRANT,
    #    WIFE AND HUSBAND, ... as Grantor/Borrower"
    # Anchor before the marital-status keyword so the captured name stops
    # at the natural boundary.
    (re.compile(
        r"WHEREAS,?\s+on\s+\w+\s+\d{1,2},?\s+\d{4},?\s+"
        r"([A-Z][A-Z\s,.&'-]{3,150}?),?\s+"
        r"(?:WIFE\s+AND\s+HUSBAND|HUSBAND\s+AND\s+WIFE|A\s+SINGLE|MARRIED|WITH\s+HIM|WITH\s+HER)",
        re.I), "whereas-on-date-name-marital"),
    # Fallback: pre-"as Grantor/Borrower" name capture (no marital anchor).
    # Lower priority than the marital-anchored form because it can over-capture.
    (re.compile(
        r"(?:^|\n|\.\s+)([A-Z][A-Z\s,.&'-]{5,80})\s+as\s+Grantor\s*/\s*Borrower",
        re.I), "name-as-grantor-borrower"),
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
    # OCR-garble tolerant: "HEREBY" frequently OCRs to "MIEREBY", "WIEREBY"
    # etc; "NOTICE IS" also seen as "NOW THEREFORE-N@". Allow up to 30
    # chars of OCR garbage between the preamble and "GIVEN that on DATE"
    # (probe 2026-05-26).
    (re.compile(
        r"(?:NOTICE\s+IS|NOW\s+THEREFORE)[\s\S]{0,30}?GIVEN\s+that\s+on\s+"
        r"(?:Tuesday|Monday|Wednesday|Thursday|Friday|Saturday|Sunday|\w+)?,?\s*"
        r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
        re.I), "narrative-given-fallback"),
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
    # ------------------------------------------------------------------
    # Mixed-case / OCR-garbage-tolerant unlabeled patterns added per the
    # full-population probe (2026-05-26). The all-caps patterns above
    # reject "Lenway Street", "Dallas Parkway" etc. Sized to recover
    # ~22 of 28 records that had a street address but no label.
    # ------------------------------------------------------------------
    # Mixed-case single-line: "2639 Lenway Street, Dallas, TX 75215"
    (re.compile(
        r"\b(\d{2,6}\s+[A-Z][A-Za-z\.\-\' ]{2,40}?\s+" + _STREET_SUFFIX_GROUP + r"),?\s+"
        r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s*,\s*(?:TX|Texas)\s+(\d{5})",
        re.I), "mixed-case-one-line"),
    # Mixed-case two-line with OCR barcode garbage between street and city:
    #   "5605 SADDLEBACK ROAD 000000 10820736\nGARLAND, TX 75043"
    # Allow arbitrary trailing chars on the street line; allow up to ~40
    # garbage chars on the city line prefix.
    (re.compile(
        r"(?:^|\n)\s*(\d{2,6}\s+[A-Z][A-Za-z\.\-\' ]{2,40}?\s+" + _STREET_SUFFIX_GROUP + r")[^\n]*\n"
        r"[^\n]{0,40}?([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s*,?\s*(?:TX|Texas)\s+(\d{5})",
        re.I), "mixed-case-two-line"),
    # No-comma 4+ word form: "611 Matthew Place Richardson TX 75081"
    (re.compile(
        r"\b(\d{2,6}\s+[A-Z][A-Za-z]+\s+" + _STREET_SUFFIX_GROUP + r")\s+"
        r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\s+(?:TX|Texas)\s+(\d{5})",
        re.I), "no-comma-street-city-tx-zip"),
]

# Loan amount.
LOAN_AMOUNT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # OCR-garble tolerant: Tesseract sometimes inserts a space inside a
    # numeric value ("$11 5,862.00" -> "11" by the strict patterns below).
    # Tried FIRST so it wins when a cents-form amount is OCR-fragmented;
    # the parser strips internal whitespace post-capture.
    (re.compile(
        r"original\s+(?:principal\s+)?amount\s+of\s+\$\s*([\d,\s]{4,15}\.\d{2})",
        re.I), "original-amount-space-tolerant"),
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
        r"Legal\s+Description\s*[:\-]?\s*([^\n]+(?:\s+[^\n]+){0,4}?(?:COUNTY,?\s+TEXAS\.?|TEXAS\.?))",
        re.I), "labeled-legal-description"),
    # NOTE per probe 2026-05-26: `\n[^\n]+` rejected OCR's blank lines
    # between rows. Switched to `\s+[^\n]+` so the line-continuation
    # tolerates `\n\n` page-spaced layouts (e.g. EXHIBIT A pages with
    # double-spaced lines under Walnut Creek Estates / LAPRENSA GRANT).
    (re.compile(
        r"EXHIBIT\s*[\"']?\s*A\s*[\"']?[^L]*?(BEING\s+LOT\s+\d+[^\n]+(?:\s+[^\n]+){0,4}?COUNTY,?\s+TEXAS\.?)",
        re.I), "exhibit-A-being-lot"),
    (re.compile(
        r"(BEING\s+LOT\s+\d+[^\n]+(?:\s+[^\n]+){0,4}?COUNTY,?\s+TEXAS\.?)", re.I),
     "being-lot"),
    (re.compile(
        r"(LOT\s+\d+[,.]?\s+(?:IN\s+)?BLOCK\s+[\d/]+[^\n]+(?:\s+[^\n]+){0,4}?COUNTY,?\s+TEXAS\.?)",
        re.I), "lot-block"),
]

# APN extraction (Fix 4 per probe 2026-05-26). ServiceLink-format NOFs
# carry the DCAD account number padded with leading zeros on page 1.
# Stripped of leading zeros, it maps directly to DCAD's ACCOUNT_NUM,
# bypassing address-text extraction entirely. Hit rate is ~5% of NOFs
# alone, but the path is high-confidence (no false positives) and cheap.
APN_PATTERNS: list[tuple[re.Pattern, str]] = [
    # OCR often concatenates trailing cadastral chars onto the APN
    # ("APN 180052700C0070000"), so we don't anchor on \b at the end —
    # capture the leading digit run after stripping zero-padding.
    (re.compile(r"\bAPN\s*[#:]?\s*0*(\d{6,15})", re.I), "apn-labeled"),
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
    apn:              Optional[str] = None  # DCAD account_num, leading zeros stripped
    apn_pattern:      Optional[str] = None
    is_hoa_lien:      bool = False
    warnings:         list[str] = field(default_factory=list)


HOA_LIEN_RE = re.compile(r"NOTICE\s+OF\s+ASSESSMENT\s+LIEN\s+SALE", re.I)


def is_hoa_lien(ocr_text: str) -> bool:
    """True if the OCR text indicates an HOA/condo assessment-lien sale
    rather than a mortgage foreclosure. Those records should be suppressed."""
    return bool(HOA_LIEN_RE.search(ocr_text))


def _try_patterns(
    text: str,
    patterns: list[tuple[re.Pattern, str]],
    *,
    cleaner: Optional[Callable[[str], str]] = None,
    validator: Optional[Callable[[str], bool]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Try each (regex, label) pair in order; return first match + label.

    When a pattern has multiple capture groups (e.g. the 2-line address
    matcher: street on group 1, city/state/zip on group 2), join them
    with a comma so the caller gets a single-string address.

    PR 7 additions:
        cleaner: optional post-processing function applied to the captured
            string after whitespace/trailing-punctuation normalization.
            Used to strip leading OCR garbage like pipe chars from names.
        validator: optional predicate; when it returns False, the candidate
            is discarded and the next pattern is tried. Used to reject
            boilerplate captures (e.g. "LIABLE" from
            "BUT NOT TO OTHERWISE BE LIABLE as Grantor/Borrower") and
            obviously-garbled or wrong-county addresses.
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
            if cleaner:
                captured = cleaner(captured)
            if not captured:
                continue
            if validator and not validator(captured):
                continue
            return (captured, label)
    return (None, None)


# ─────────────────────────────────────────────────────────────────────────
# Field validators / cleaners (PR 7)
# ─────────────────────────────────────────────────────────────────────────
#
# Built from forensic findings on the 8 production failure cases (Liable,
# Montgomery, Cox, Battie, Betancourt, Adama, Velasquez, Flores). See
# docs/ocr_forensic/SUMMARY.md and the per-record REPORT.md files for the
# evidence each validator addresses.

# Leading-punctuation strip: OCR occasionally captures stray characters
# before a name (a vertical pipe from a table border, a bullet glyph, a
# stray period from a watermark, etc.). Strip them so the grantor field
# starts with a real letter.
_GRANTOR_LEADING_PUNCT_RE = re.compile(r"^[\s|:>•·.\-]+")


def _clean_grantor(captured: str) -> str:
    """Strip leading punctuation/whitespace OCR sometimes leaves on names.

    Fixes the Flores case (record_id=315562562) where OCR captured
    "| JAIME O. FLORES AND MARIE G. FLORES..." (vertical pipe prefix).
    """
    if not captured:
        return captured
    return _GRANTOR_LEADING_PUNCT_RE.sub("", captured).strip()


def _is_valid_grantor(captured: str) -> bool:
    """Reject single-word boilerplate captures like 'LIABLE' / 'MORTGAGOR'.

    Fixes the Liable case (record_id=315562554) where the
    name-as-grantor-borrower regex captured "LIABLE" from
    "BUT NOT TO OTHERWISE BE LIABLE as Grantor/Borrower" (the
    OCR-correct text following ServiceLink boilerplate).

    Reuses the canonical ``GRANTOR_BOILERPLATE_BLOCKLIST`` from
    ``scraper.resolution`` so Path A's filter and the OCR-extractor's
    filter stay in lockstep.
    """
    # Lazy import to avoid a top-of-module cycle with scraper.resolution.
    from .resolution import GRANTOR_BOILERPLATE_BLOCKLIST

    if not captured:
        return False
    upper = captured.upper().strip()
    # Direct hit on the blocklist (any form)
    if upper in GRANTOR_BOILERPLATE_BLOCKLIST:
        return False
    # First-token-only match (handles "LIABLE, ABC" variants)
    main = upper.split(",")[0].strip()
    if main in GRANTOR_BOILERPLATE_BLOCKLIST:
        return False
    first_token = main.split()[0] if main.split() else ""
    if first_token in GRANTOR_BOILERPLATE_BLOCKLIST:
        return False
    # A single-token capture without any whitespace is almost certainly
    # boilerplate garbage even when not in the blocklist.
    if " " not in upper.strip():
        return False
    return True


# Symbols that appear inside captured address strings are a strong garble
# signal — they aren't legitimate parts of US street addresses. Counts of
# 2+ such symbols indicate the watermark or some other artifact mangled
# the text and the regex captured the mangle.
_ADDRESS_GARBLE_SYMBOLS = set(":{}<>[]&")

# Dallas County + immediate Texas-metro zip prefixes. Dallas County core is
# 75xxx; Tarrant County (Fort Worth metro, sometimes appears in shared
# trustee filings) is 76xxx. Anything else (77xxx Houston, 78xxx
# San Antonio, 79xxx El Paso/West Texas) is the substitute trustee's
# office address and not the property.
_LOCAL_ZIP_PREFIXES = {"75", "76"}

# ZIP-of-address detector: look for a 5-digit run that follows "TX" /
# "TEXAS" (with optional comma), OR is at the very end of the string.
# Anchoring this way avoids false positives where the regex would match
# the street number (e.g. "14160 Dallas Parkway, ..." -> 14160 is NOT a
# ZIP code).
_ZIP_AT_END_RE = re.compile(
    r"(?:\bTX|\bTEXAS|,)\s*(\d{5})\b\s*$",
    re.IGNORECASE,
)


def _extract_zip_code(captured: str) -> Optional[str]:
    """Return the 5-digit ZIP of a property-address candidate, or None.

    Anchors on "TX <zip>" / "Texas <zip>" / trailing-comma <zip> at end
    of string so the street number isn't mistaken for a ZIP.
    """
    if not captured:
        return None
    m = _ZIP_AT_END_RE.search(captured)
    if m:
        return m.group(1)
    return None


# The Dallas County foreclosure-auction venue. Every notice names it as the
# "Place of Sale" ("George Allen Courts Building, 600 Commerce Street, Dallas,
# TX 75202"). When a notice gives the property only by legal description, the
# address extractor otherwise grabs this venue as the property address — which
# then resolves to the courthouse's DCAD account (a silent wrong match). Reject
# it so the record falls back to its legal description (path C).
_VENUE_RE = re.compile(
    r"\b600\s+COMMERCE\b|GEORGE\s+ALLEN|COURTS?\s+BUILDING|PLACE\s+OF\s+SALE",
    re.IGNORECASE,
)


def _is_valid_address(captured: str) -> bool:
    """Reject obviously-garbled, wrong-county, or auction-venue address
    candidates.

    Fixes:
        * Adama case (record_id=315561580) — the substitute trustee's
          El Paso office "7730 Market Center Ave, El Paso, 79912" was
          captured instead of the property. ZIP 79912 is in West Texas;
          the property is at 2828 LAKE VIEW DR, Cedar Hill, ZIP 75104.
        * Battie case (record_id=315561578) — OCR captured
          "1442 MA 5: {E)" with three stray symbols (`:`, `{`, `)`)
          from the watermarked "Commonly known as: 1442 MARLENE PLACE"
          line.
        * Courthouse-venue cases (record_ids 316730723/316730734/316730722)
          — "600 Commerce Street, Dallas, 75202" (the George Allen Courts
          Building auction venue) was captured as the property and two
          resolved to the courthouse account. Rejecting it routes the
          record to its legal description instead.

    Heuristics (intentionally conservative — false rejection here just
    falls through to the next pattern, which usually yields ``None``;
    Path A / Path B / Path C / Stage 6.6 can still resolve the record
    via grantor name or raw_excerpt):

      1. The auction-venue address (600 Commerce / George Allen Courts
         Building / Place of Sale) → reject.
      2. 2+ stray symbols (``:`` ``{`` ``}`` ``<`` ``>`` ``[`` ``]`` ``&``)
         inside the captured text → reject as garbled.
      3. ZIP code present (anchored to end of string) but prefix not in
         ``_LOCAL_ZIP_PREFIXES`` → reject as wrong-county (trustee
         mailing address leakage).
    """
    if not captured:
        return False

    if _VENUE_RE.search(captured):
        return False

    stray_count = sum(1 for c in captured if c in _ADDRESS_GARBLE_SYMBOLS)
    if stray_count >= 2:
        return False

    zip_code = _extract_zip_code(captured)
    if zip_code:
        zip_prefix = zip_code[:2]
        if zip_prefix not in _LOCAL_ZIP_PREFIXES:
            return False

    return True


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


# OCR-derived legal-description -> publicsearch-snippet adapter.
# legal_resolver.parse_legal_from_snippet expects the publicsearch list-view
# format ("<TWP> | Subdivision - Name: <NAME> Lot: <N> Block: <B>"). OCR
# extracts text like "BEING LOT 16, IN BLOCK E/8443, OF WALNUT CREEK
# ESTATES, SECTION ONE, AN ADDITION TO..." which the resolver can't parse.
# This helper bridges the two formats so 41-record probe's 14/17 parsable
# legal descriptions can route through the existing legal_resolver pass.
_OCR_LEGAL_RE = re.compile(
    r"(?:BEING\s+)?LOT\s+(\d+[A-Z]?)\s*,?\s*(?:IN\s+)?BLOCK\s+([A-Z0-9/\-]+)\s*,?\s+"
    r"(?:OF\s+)?([A-Z][A-Z0-9\s\.\-/]+?)"
    r"(?:,\s+(?:AN?\s+ADDITION|SECTION|PHASE|INSTALLMENT|NO\.|ACCORDING)|\.|\s+ACCORDING|$)",
    re.I,
)


def _legal_desc_to_snippet(legal_desc: str) -> Optional[str]:
    """Parse an OCR'd legal description into publicsearch-snippet format.

    Returns ``"| Subdivision - Name: <NAME> Lot: <N> Block: <B>"`` on
    success, or ``None`` if subdivision/lot/block can't all three be
    extracted. The leading ``|`` keeps the format compatible with the
    resolver's `<township> | <fields>` shape (township unused for lookup).
    """
    if not legal_desc:
        return None
    m = _OCR_LEGAL_RE.search(legal_desc)
    if not m:
        return None
    lot   = m.group(1).strip()
    block = m.group(2).strip()
    subdiv = re.sub(r"\s+", " ", m.group(3).strip().rstrip(",.")).strip()
    if not (lot and block and subdiv):
        return None
    return f"| Subdivision - Name: {subdiv} Lot: {lot} Block: {block}"


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

    # PR 7: grantor extraction now strips leading OCR punctuation (Flores)
    # and rejects boilerplate single-word captures like "LIABLE" (the
    # Liable case where the regex caught text from ServiceLink boilerplate
    # rather than the actual grantor name).
    out.grantor, out.grantor_pattern = _try_patterns(
        ocr_text, GRANTOR_PATTERNS,
        cleaner=_clean_grantor, validator=_is_valid_grantor,
    )
    out.sale_date_raw, out.sale_date_pattern = _try_patterns(ocr_text, SALE_DATE_PATTERNS)
    out.sale_date_iso = _to_iso_date(out.sale_date_raw) if out.sale_date_raw else None
    # PR 7: property address extraction now rejects candidates with 2+
    # stray symbols (Battie's "1442 MA 5: {E)") and addresses whose ZIP
    # is outside Dallas/Tarrant County (Adama's "7730 Market Center Ave,
    # El Paso, 79912" — the substitute trustee's office).
    out.property_address, out.address_pattern = _try_patterns(
        ocr_text, ADDRESS_PATTERNS,
        validator=_is_valid_address,
    )
    out.loan_amount, out.loan_amount_pattern = _try_patterns(ocr_text, LOAN_AMOUNT_PATTERNS)
    out.legal_description, out.legal_desc_pattern = _try_patterns(ocr_text, LEGAL_DESC_PATTERNS)
    out.apn, out.apn_pattern = _try_patterns(ocr_text, APN_PATTERNS)

    # OCR sometimes inserts a space inside a number ("$11 5,862.00").
    # The space-tolerant loan-amount pattern preserves the space in the
    # capture; strip it here so downstream dollar parsing sees "115,862.00".
    if out.loan_amount and " " in out.loan_amount:
        out.loan_amount = re.sub(r"\s+", "", out.loan_amount)

    if not out.grantor:
        out.warnings.append("no_grantor_extracted")
    if not out.sale_date_iso:
        out.warnings.append("no_sale_date_extracted")
    if not out.property_address and not out.legal_description and not out.apn:
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
    # PR 12.15 instrumentation — all in seconds, never None so callers can
    # log unconditionally. Per-page costs use a list aligned with the order
    # in which pages were attempted (page 1 first, then 2..N).
    t_goto: float = 0.0
    t_passive_wait: float = 0.0
    t_pagecount_detect: float = 0.0
    t_reload: float = 0.0
    t_per_page: list[float] = field(default_factory=list)
    attempts: int = 1
    reload_fired: bool = False
    per_page_timeouts: int = 0
    # PR 12.20 forensic: every publicsearch.us response observed during
    # _capture_one — (url, status, content_type). Lets us see if the SPA
    # is being 429-rate-limited, redirected to an error page, etc.
    responses: list[tuple[str, int, str]] = field(default_factory=list)
    final_url: str = ""
    # Screenshot + HTML captured INSIDE _capture_one while the page is
    # still alive — main loop closes the page before checking status.
    # Populated only when result.pages is empty (capture failed).
    failure_screenshot: Optional[bytes] = None
    failure_html: str = ""
    failure_img_srcs: list[str] = field(default_factory=list)


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
        # PR 12.20: log every publicsearch.us response (URL host + status +
        # content-type) so we can spot 429s, 5xxs, redirects, block pages.
        # Cap at 200 entries per record so a runaway page can't blow logs.
        if "publicsearch.us" in url and len(result.responses) < 200:
            try:
                ctype = response.headers.get("content-type", "") if hasattr(response, "headers") else ""
            except Exception:
                ctype = ""
            result.responses.append((url, response.status, ctype))

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

    _t0 = time.perf_counter()
    try:
        page.goto(f"{base_url}/doc/{record_id}",
                  wait_until="networkidle", timeout=30_000)
        result.t_goto = time.perf_counter() - _t0
    except Exception as e:
        result.t_goto = time.perf_counter() - _t0
        page.remove_listener("response", on_response)
        result.status = "error"
        result.warnings.append(f"goto_failed: {e}")
        return result

    _t1 = time.perf_counter()
    time.sleep(passive_wait_s)
    result.t_passive_wait = time.perf_counter() - _t1

    # Detect total page count from the viewer's "X of N" text.
    _t2 = time.perf_counter()
    total_pages: Optional[int] = None
    try:
        body_text = page.locator("body").inner_text()
        m = re.search(r"\bof\s+(\d+)\b", body_text)
        if m:
            total_pages = int(m.group(1))
    except Exception:
        pass
    result.t_pagecount_detect = time.perf_counter() - _t2

    # Mirror the manual-refresh workaround the operator confirmed works on
    # publicsearch.us: when the first /doc/{id} load hangs (you see a
    # spinning circle, no images), pressing Ctrl+R fixes it. The SPA's
    # signed-URL-minting backend is flaky on first request. If our passive
    # wait completed with no images captured AND the viewer's "of N"
    # pagination text never appeared, do a page.reload() and wait again
    # before declaring failure.
    if not result.pages and total_pages is None:
        result.warnings.append("initial_load_hung_reloading")
        result.reload_fired = True
        _t3 = time.perf_counter()
        try:
            page.reload(wait_until="networkidle", timeout=30_000)
            time.sleep(passive_wait_s)
            # Re-detect total_pages after the reload.
            try:
                body_text = page.locator("body").inner_text()
                m = re.search(r"\bof\s+(\d+)\b", body_text)
                if m:
                    total_pages = int(m.group(1))
            except Exception:
                pass
        except Exception as e:
            result.warnings.append(f"reload_failed: {e}")
        result.t_reload = time.perf_counter() - _t3

    if total_pages and len(result.pages) < total_pages:
        for target_page in range(2, total_pages + 1):
            if target_page in result.pages:
                result.t_per_page.append(0.0)  # already had it; no fetch
                continue
            url_pat = re.compile(rf"/files/documents/\d+/images/\d+_{target_page}\.png")
            _tp = time.perf_counter()
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
                result.per_page_timeouts += 1
            except Exception as e:
                result.warnings.append(f"page_{target_page}_error: {e}")
            result.t_per_page.append(time.perf_counter() - _tp)

    page.remove_listener("response", on_response)

    # PR 12.20: capture final URL so we can detect redirects (block page,
    # CAPTCHA challenge, login wall, etc).
    try:
        result.final_url = page.url
    except Exception:
        pass

    if not result.pages:
        result.status = "no_images"
        # PR 12.20: while the page is still alive, snap a screenshot,
        # dump the HTML, and enumerate every <img src=...> on the page.
        # Stored on the result; the main loop writes to disk once per run.
        try:
            result.failure_screenshot = page.screenshot(full_page=True)
        except Exception as e:
            result.warnings.append(f"failure_screenshot_failed: {e}")
        try:
            result.failure_html = page.content() or ""
        except Exception as e:
            result.warnings.append(f"failure_html_failed: {e}")
        try:
            srcs = page.evaluate(
                "() => Array.from(document.querySelectorAll('img'))"
                "  .map(e => e.getAttribute('src') || e.src || '')"
                "  .filter(Boolean)"
            )
            result.failure_img_srcs = list(srcs)[:50]
        except Exception as e:
            result.warnings.append(f"failure_img_srcs_failed: {e}")

    return result


def capture_with_retry(page, record_id: str, base_url: str,
                       max_attempts: int = 2) -> CaptureResult:
    """Capture with one retry on no_images. Mitigates the ~15% transient
    miss rate we observed where the SPA hadn't finished hydrating before
    our passive wait expired."""
    for attempt in range(1, max_attempts + 1):
        wait_s = 4.0 if attempt == 1 else 8.0
        result = _capture_one(page, record_id, base_url, passive_wait_s=wait_s)
        result.attempts = attempt
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

def _ocr_one_image_worker(args: tuple[bytes, str]) -> tuple:
    """OCR a single PNG in a worker. Returns
        (text, elapsed_s, image_size_bytes, width, height, mode, dpi).

    width/height from PIL.Image.size; mode is "RGB"/"RGBA"/"L"/etc;
    dpi is a "XxY" string or "?" if not in image metadata. All come
    back so the parent can log per_page_pixels / per_page_mode /
    per_page_dpi without per-image guessing.
    """
    img_bytes, tess_cmd = args
    _t0 = time.perf_counter()
    width = height = 0
    mode = "?"
    dpi  = "?"
    try:
        import pytesseract
        from PIL import Image
        from io import BytesIO
        pytesseract.pytesseract.tesseract_cmd = tess_cmd
        img = Image.open(BytesIO(img_bytes))
        width, height = img.size
        mode = img.mode
        d = img.info.get("dpi")
        if d:
            dpi = f"{int(d[0])}x{int(d[1])}"
        text = pytesseract.image_to_string(img, config="--psm 6 --oem 1")
        return (text, time.perf_counter() - _t0, len(img_bytes), width, height, mode, dpi)
    except Exception as e:
        return (f"<OCR_ERROR: {e}>", time.perf_counter() - _t0, len(img_bytes),
                width, height, mode, dpi)


def _ocr_pages(pages: dict[int, bytes], tess_cmd: str,
               pool: Optional["multiprocessing.pool.Pool"] = None) -> str:
    """Run Tesseract on each captured PNG and concatenate the text in
    page order. Returns combined OCR text.

    If a multiprocessing.Pool is provided AND there are 2+ pages, OCR
    runs in parallel (~3-4x speedup on a 4-core machine for typical
    notices). Single-page records skip the pool overhead and run inline.

    PR 12.16 instrumentation: emits a forensic line on every call:
      _ocr_pages: pages=N path=pool|serial wall=Xs sum_worker=Ys
                  per_page_seconds=[...] per_page_kb=[...]
    If path=pool and sum_worker ≈ wall, the pool is not parallelizing.
    """
    if not pages:
        return ""

    sorted_keys = sorted(pages.keys())
    if pool is not None and len(sorted_keys) >= 2:
        # Parallel: dispatch all pages to the pool at once.
        args = [(pages[pn], tess_cmd) for pn in sorted_keys]
        _wall_t = time.perf_counter()
        try:
            results = pool.map(_ocr_one_image_worker, args)
            wall = time.perf_counter() - _wall_t
            texts    = [r[0] for r in results]
            timings  = [r[1] for r in results]
            sizes_kb = [r[2] / 1024 for r in results]
            pixels   = [(r[3], r[4]) for r in results]
            modes    = [r[5] if len(r) > 5 else "?" for r in results]
            dpis     = [r[6] if len(r) > 6 else "?" for r in results]
            logger.info(
                "_ocr_pages: pages=%d path=pool wall=%.1fs sum_worker=%.1fs "
                "per_page_seconds=%s per_page_kb=%s per_page_pixels=%s "
                "per_page_mode=%s per_page_dpi=%s",
                len(sorted_keys), wall, sum(timings),
                [f"{t:.1f}" for t in timings],
                [f"{s:.0f}" for s in sizes_kb],
                [f"{w}x{h}" for w, h in pixels],
                modes, dpis,
            )
            return "\n\n".join(texts)
        except Exception as e:
            logger.warning("Parallel OCR failed, falling back to serial: %s", e)
            # fall through to the serial path below

    # Serial fallback (also used for single-page docs where pool overhead
    # would dominate).
    import pytesseract
    from PIL import Image
    from io import BytesIO

    pytesseract.pytesseract.tesseract_cmd = tess_cmd
    out_parts: list[str] = []
    timings: list[float] = []
    sizes_kb: list[float] = []
    pixels: list[tuple[int, int]] = []
    _wall_t = time.perf_counter()
    for pn in sorted_keys:
        _pt = time.perf_counter()
        wh = (0, 0)
        try:
            img = Image.open(BytesIO(pages[pn]))
            wh = img.size
            out_parts.append(pytesseract.image_to_string(img, config="--psm 6 --oem 1"))
        except Exception as e:
            logger.warning("OCR failed on page %d: %s", pn, e)
        timings.append(time.perf_counter() - _pt)
        sizes_kb.append(len(pages[pn]) / 1024)
        pixels.append(wh)
    wall = time.perf_counter() - _wall_t
    logger.info(
        "_ocr_pages: pages=%d path=serial wall=%.1fs sum_worker=%.1fs "
        "per_page_seconds=%s per_page_kb=%s per_page_pixels=%s",
        len(sorted_keys), wall, sum(timings),
        [f"{t:.1f}" for t in timings],
        [f"{s:.0f}" for s in sizes_kb],
        [f"{w}x{h}" for w, h in pixels],
    )
    return "\n\n".join(out_parts)


# ═══════════════════════════════════════════════════════════════════════════
# Public API: per-record enrichment + batch
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OCREnrichmentStats:
    total:                  int = 0
    captured_ok:            int = 0
    no_images:              int = 0
    capture_errors:         int = 0
    hoa_lien_suppressed:    int = 0
    skipped_past_sale_date: int = 0  # auction already happened — skip OCR
    grantor_extracted:      int = 0
    sale_date_extracted:    int = 0
    address_extracted:      int = 0
    loan_amount_extracted:  int = 0
    legal_desc_extracted:   int = 0


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

    # Python-binding check: the tesseract-ocr APT package and the
    # pytesseract/Pillow Python libs are independent installs. Without the
    # libs, every worker's `import pytesseract` raises and the worker's
    # bare-except returns "<OCR_ERROR: ...>" as the OCR text, leaving all
    # field-extraction counts at 0 with no log line. Fail loud instead.
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        logger.warning(
            "OCR enrichment SKIPPED: Python OCR bindings not installed (%s). "
            "Run: pip install pytesseract Pillow",
            exc,
        )
        for r in records:
            r.setdefault("parse_warnings", []).append("ocr_skipped_no_python_bindings")
        return records, stats

    # Multiprocessing pool DISABLED BY DEFAULT (PR 12.22). Reproduced
    # locally and on GHA: running Tesseract through multiprocessing.Pool
    # with 2+ workers on a 4-vCPU machine causes a ~150x per-worker
    # slowdown even with OMP_NUM_THREADS=1. Single-process PSM=6 = 1.07s,
    # Pool(4) PSM=6 = 159s/page. The pool's apparent "parallel speedup"
    # was masking this — each worker was so slow that 4 in parallel ≈
    # 1 serial in PSM=3 terms. Serial OCR in the main process is the
    # correct choice: ~2s/page × ~3 pages × 56 records ≈ 6 min total.
    #
    # Set FORECLOSURE_OCR_PARALLEL=true to re-enable for testing on
    # high-CPU machines where the contention pattern may differ.
    pool_size = min(multiprocessing.cpu_count(), 8)
    pool: Optional[multiprocessing.pool.Pool] = None
    parallel_enabled = os.environ.get("FORECLOSURE_OCR_PARALLEL", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )

    today = date.today()

    logger.info(
        "OCR enrichment starting on %d foreclosure records "
        "(Tesseract: %s, parallel=%s, max-workers=%d)",
        len(records), tess_cmd,
        "on" if parallel_enabled else "off (FORECLOSURE_OCR_PARALLEL=false)",
        pool_size,
    )

    # PR 12.17 forensic: log environment relevant to OCR performance so a
    # 28x CI-vs-local slowdown can be diagnosed from a single run's logs.
    try:
        import subprocess
        tess_ver = subprocess.run(
            [tess_cmd, "--version"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().split("\n")[0]
    except Exception as e:
        tess_ver = f"<unknown: {e}>"
    logger.info(
        "OCR env: tesseract_version=%r cpu_count=%d "
        "OMP_NUM_THREADS=%r OPENBLAS_NUM_THREADS=%r MKL_NUM_THREADS=%r",
        tess_ver, multiprocessing.cpu_count(),
        os.environ.get("OMP_NUM_THREADS", "<unset>"),
        os.environ.get("OPENBLAS_NUM_THREADS", "<unset>"),
        os.environ.get("MKL_NUM_THREADS", "<unset>"),
    )

    # PR 12.19 forensic: CPU model + RAM totals.
    # Helps distinguish "slow CPU" from "swap thrash" from "fast CPU but
    # something else is the bottleneck".
    try:
        with open("/proc/cpuinfo") as f:
            cpu_model = next(
                (line.split(":", 1)[1].strip()
                 for line in f if line.startswith("model name")),
                "<unknown>",
            )
    except Exception:
        cpu_model = "<not-linux>"
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                meminfo[k.strip()] = rest.strip()
        mem_total = meminfo.get("MemTotal", "?")
        mem_avail = meminfo.get("MemAvailable", "?")
    except Exception:
        mem_total = mem_avail = "<not-linux>"
    logger.info(
        "OCR sysinfo: cpu_model=%r mem_total=%s mem_available=%s",
        cpu_model, mem_total, mem_avail,
    )

    # PR 12.19 forensic: synthetic Tesseract baseline. OCR a known-good
    # 1000x1500 image with embedded text once at startup. If this takes
    # ~1-3s -> Tesseract itself is healthy and the captured PNGs are
    # anomalous. If it takes 60s+ -> CPU or Tesseract is the bottleneck
    # regardless of image content. Pure measurement, ~2-5s once.
    try:
        from PIL import Image, ImageDraw, ImageFont
        from io import BytesIO
        import pytesseract as _pyt
        _pyt.pytesseract.tesseract_cmd = tess_cmd
        synth = Image.new("RGB", (1000, 1500), "white")
        draw = ImageDraw.Draw(synth)
        # Render multiple lines so the OCR has something resembling
        # a document, not just a single line.
        lines = [
            "NOTICE OF FORECLOSURE SALE",
            "Property: 123 Main Street, Dallas, Texas 75201",
            "Grantor: John A. Smith and Jane B. Smith",
            "Sale Date: June 4, 2026   Trustee: Acme Trustee LLC",
            "Loan Amount: $185,000.00",
            "Legal Description: Lot 5, Block 12, Acme Addition",
        ]
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
        y = 100
        for line in lines:
            draw.text((50, y), line, fill="black", font=font)
            y += 80
        _bench_t = time.perf_counter()
        text = _pyt.image_to_string(synth)
        bench_s = time.perf_counter() - _bench_t
        logger.info(
            "OCR baseline (synthetic 1000x1500, 6 text lines): "
            "image_to_string took %.2fs, text_len=%d, sample_text=%r",
            bench_s, len(text), text[:80].replace("\n", " "),
        )
    except Exception as e:
        logger.info("OCR baseline: synthetic benchmark failed: %s", e)

    # Spawn the Pool BEFORE entering browser_context, so worker subprocess
    # spawn doesn't compete with Playwright's running browser for system
    # resources (anecdotally a source of Windows hangs).
    if parallel_enabled and any(r.get("record_id") for r in records):
        try:
            logger.info("OCR enrichment: spawning multiprocessing.Pool(processes=%d)...", pool_size)
            pool = multiprocessing.Pool(processes=pool_size)
            logger.info("OCR enrichment: pool ready (%d workers)", pool_size)
        except Exception as e:
            logger.warning(
                "Could not start OCR multiprocessing pool: %s; "
                "falling back to serial OCR", e,
            )
            pool = None

    logger.info("OCR enrichment: launching Playwright browser context...")
    with browser_context() as (_browser, context, _page):
        logger.info("OCR enrichment: browser context ready, starting per-record loop")
        # Discard the initial page from browser_context; we use a fresh
        # page per record to avoid React/SPA state accumulation that
        # caused intermittent ~10% capture failures when one page was
        # reused across 78 navigations.
        try:
            _page.close()
        except Exception:
            pass

        for i, rec in enumerate(records, 1):
            rid = rec.get("record_id")
            if not rid:
                continue

            # Skip OCR for records where the auction has already happened.
            # Sale-date < today means the foreclosure is no longer a
            # motivated-seller lead -- the property is either sold,
            # withdrawn, or in post-auction processing. Saves ~10s/record
            # for the ~10-15% of the pool that's already past.
            sale_date_str = rec.get("sale_date")
            if sale_date_str:
                try:
                    sd = date.fromisoformat(str(sale_date_str)[:10])
                    if sd < today:
                        rec.setdefault("parse_warnings", []).append("ocr_skipped_past_sale_date")
                        stats.skipped_past_sale_date += 1
                        continue
                except (ValueError, TypeError):
                    pass

            _rec_t0 = time.perf_counter()
            logger.info("OCR [%d/%d] capturing %s ...", i, len(records), rid)
            # Fresh page per record — disposed after each capture so SPA
            # state, JS context, and memory don't accumulate.
            _newpage_t = time.perf_counter()
            page = context.new_page()
            _t_newpage = time.perf_counter() - _newpage_t
            cap = None
            _t_capture_total = 0.0
            try:
                _cap_t = time.perf_counter()
                try:
                    cap = capture_with_retry(page, rid, base_url)
                except Exception as e:
                    _t_capture_total = time.perf_counter() - _cap_t
                    logger.warning("OCR capture uncaught error for %s: %s", rid, e)
                    rec.setdefault("parse_warnings", []).append(f"ocr_capture_error:{type(e).__name__}")
                    stats.capture_errors += 1
                    logger.info(
                        "OCR [%d/%d] %s done (error): total=%.1fs new_page=%.2fs capture=%.1fs",
                        i, len(records), rid,
                        time.perf_counter() - _rec_t0, _t_newpage, _t_capture_total,
                    )
                    continue
                _t_capture_total = time.perf_counter() - _cap_t
            finally:
                _close_t = time.perf_counter()
                try:
                    page.close()
                except Exception:
                    pass
                _t_close = time.perf_counter() - _close_t

            if cap.status == "no_images":
                rec.setdefault("parse_warnings", []).append("ocr_no_images")
                stats.no_images += 1
                logger.info(
                    "OCR [%d/%d] %s done (no_images): total=%.1fs "
                    "capture=%.1fs (goto=%.1fs passive=%.1fs pagecount=%.2fs reload=%.1fs) "
                    "attempts=%d reload_fired=%s final_url=%s warnings=%s",
                    i, len(records), rid,
                    time.perf_counter() - _rec_t0,
                    _t_capture_total,
                    cap.t_goto, cap.t_passive_wait, cap.t_pagecount_detect, cap.t_reload,
                    cap.attempts, cap.reload_fired, cap.final_url, cap.warnings,
                )
                # PR 12.20: dump full diagnostic ONCE per run (first failure).
                # All publicsearch.us responses, screenshot, HTML, img srcs.
                if stats.no_images == 1:
                    # Response summary: total count + first 4xx/5xx samples
                    n_resp = len(cap.responses)
                    errs = [(u, s, c) for u, s, c in cap.responses if s >= 400]
                    logger.info(
                        "OCR capture failure forensic [%s]: %d publicsearch.us responses, %d >=400",
                        rid, n_resp, len(errs),
                    )
                    if errs:
                        for u, s, c in errs[:10]:
                            logger.info("  ERR response: status=%d ctype=%r url=%s", s, c, u)
                    # Full response list (truncated to 50)
                    for u, s, c in cap.responses[:50]:
                        logger.info("  response: status=%d ctype=%r url=%s", s, c, u)
                    if len(cap.responses) > 50:
                        logger.info("  ... (%d more responses truncated)", len(cap.responses) - 50)
                    # img src list — does the page even ask for /files/documents/*.png?
                    logger.info(
                        "  page <img> srcs (first 20): %s",
                        cap.failure_img_srcs[:20],
                    )
                    # Save artifacts to data/probes/
                    try:
                        probe_dir = config.DATA_DIR / "probes"
                        probe_dir.mkdir(parents=True, exist_ok=True)
                        if cap.failure_screenshot:
                            png_path = probe_dir / f"capture_failure_{rid}.png"
                            png_path.write_bytes(cap.failure_screenshot)
                            logger.info("  saved screenshot %s (%d bytes)", png_path, len(cap.failure_screenshot))
                        if cap.failure_html:
                            html_path = probe_dir / f"capture_failure_{rid}.html"
                            html_path.write_text(cap.failure_html, encoding="utf-8")
                            logger.info("  saved html %s (%d chars)", html_path, len(cap.failure_html))
                    except Exception as e:
                        logger.info("  artifact save failed: %s", e)
                continue
            elif cap.status == "error":
                rec.setdefault("parse_warnings", []).append("ocr_capture_error")
                stats.capture_errors += 1
                logger.info(
                    "OCR [%d/%d] %s done (error): total=%.1fs capture=%.1fs warnings=%s",
                    i, len(records), rid,
                    time.perf_counter() - _rec_t0, _t_capture_total, cap.warnings,
                )
                continue

            stats.captured_ok += 1

            # PR 12.18 forensic: save the very first successfully-captured PNG
            # to data/probes/ so we can inspect what Tesseract is actually
            # being asked to read. data/probes/ is committed by the daily
            # cron, so the artifact surfaces on main automatically.
            if stats.captured_ok == 1 and 1 in cap.pages:
                try:
                    probe_dir = config.DATA_DIR / "probes"
                    probe_dir.mkdir(parents=True, exist_ok=True)
                    png_path = probe_dir / f"ocr_sample_{rid}_p1.png"
                    png_path.write_bytes(cap.pages[1])
                    logger.info(
                        "OCR forensic: saved sample %s (%d bytes)",
                        png_path, len(cap.pages[1]),
                    )
                except Exception as e:
                    logger.info("OCR forensic: sample save failed: %s", e)

            _ocr_t = time.perf_counter()
            text = _ocr_pages(cap.pages, tess_cmd, pool=pool)
            _t_ocr = time.perf_counter() - _ocr_t

            # PR 12.19 forensic: PSM comparison on the first record's page 1.
            # The pipeline uses PSM=3 (auto layout detection) by default;
            # PSM=6 treats the image as a single uniform text block and skips
            # the layout-detection pass. If PSM=6 is dramatically faster on
            # the same image, PSM=3 layout detection is the killer; if not,
            # raw Tesseract speed is the bottleneck regardless of layout.
            # Runs once per pipeline invocation.
            if stats.captured_ok == 1 and 1 in cap.pages:
                try:
                    import pytesseract as _pyt
                    from PIL import Image as _PILImage
                    from io import BytesIO as _BIO
                    _pyt.pytesseract.tesseract_cmd = tess_cmd
                    img = _PILImage.open(_BIO(cap.pages[1]))
                    _psm6_t = time.perf_counter()
                    psm6_text = _pyt.image_to_string(img, config="--psm 6 --oem 1")
                    psm6_s = time.perf_counter() - _psm6_t
                    # We can recover the PSM=3 cost for page 1 from the
                    # per_page_seconds list logged by _ocr_pages; here we
                    # just record PSM=6 cost on the same image.
                    logger.info(
                        "OCR PSM compare (first record, page 1): "
                        "psm6_oem1_s=%.1fs psm6_text_len=%d",
                        psm6_s, len(psm6_text),
                    )
                except Exception as e:
                    logger.info("OCR PSM compare failed: %s", e)

            _extract_t = time.perf_counter()
            fields = extract_fields_from_text(text)
            _t_extract = time.perf_counter() - _extract_t

            # Forensic line: per-phase wall time for this record. Single
            # structured log line so a run can be analysed with grep/awk.
            per_page_str = ",".join(f"{t:.1f}" for t in cap.t_per_page) if cap.t_per_page else "-"
            logger.info(
                "OCR [%d/%d] %s done: total=%.1fs capture=%.1fs "
                "(goto=%.1fs passive=%.1fs pagecount=%.2fs reload=%.1fs per_page=[%s]) "
                "ocr=%.1fs extract=%.2fs new_page=%.2fs close=%.2fs "
                "| pages=%d attempts=%d reload_fired=%s timeouts=%d text_len=%d",
                i, len(records), rid,
                time.perf_counter() - _rec_t0,
                _t_capture_total,
                cap.t_goto, cap.t_passive_wait, cap.t_pagecount_detect, cap.t_reload,
                per_page_str,
                _t_ocr, _t_extract, _t_newpage, _t_close,
                len(cap.pages), cap.attempts, cap.reload_fired,
                cap.per_page_timeouts, len(text),
            )

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
                # Stash legal description in raw_excerpt for legal_resolver
                # to consume on its next pass. Two strategies:
                #   1. If the OCR'd legal description parses into a
                #      structured (subdivision, lot, block) and the
                #      existing snippet has no "Subdivision - Name:" tag,
                #      REPLACE raw_excerpt with a publicsearch-format
                #      snippet so legal_resolver can match it.
                #   2. Otherwise fall back to the original append-behind-
                #      separator behavior (keeps backwards compat).
                existing = rec.get("raw_excerpt") or ""
                snippet  = _legal_desc_to_snippet(fields.legal_description)
                if snippet and "Subdivision - Name:" not in existing:
                    # Preserve any leading township-ish prefix from the
                    # original snippet (everything before the first "|").
                    prefix = existing.split("|", 1)[0].strip() if existing else ""
                    rec["raw_excerpt"] = (
                        f"{prefix} {snippet}".strip()
                        if prefix else snippet.lstrip("| ").lstrip()
                    )[:500]
                elif "LOT" not in existing.upper():
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
                "apn_pattern":          fields.apn_pattern,
                "apn":                  fields.apn,
                "pages_captured":       len(cap.pages),
                "capture_warnings":     cap.warnings,
            }
            rec["signal_metadata"] = meta
            rec.setdefault("parse_warnings", []).extend(fields.warnings)

            logger.debug(
                "[%d/%d] %s: ocr ok in %.1fs (%d pages, grantor=%s, sale=%s, addr=%s)",
                i, len(records), rid, time.perf_counter() - _rec_t0,
                len(cap.pages),
                "Y" if fields.grantor else "n",
                "Y" if fields.sale_date_iso else "n",
                "Y" if fields.property_address else "n",
            )

    # Clean up the OCR multiprocessing pool.
    if pool is not None:
        try:
            pool.close()
            pool.join()
        except Exception:
            pass

    logger.info(
        "OCR enrichment complete: %d/%d captured; extracted "
        "grantor=%d, sale_date=%d, address=%d, amount=%d, legal=%d; "
        "hoa_suppressed=%d, skipped_past_sale=%d, no_images=%d, errors=%d",
        stats.captured_ok, stats.total,
        stats.grantor_extracted, stats.sale_date_extracted,
        stats.address_extracted, stats.loan_amount_extracted,
        stats.legal_desc_extracted,
        stats.hoa_lien_suppressed, stats.skipped_past_sale_date,
        stats.no_images, stats.capture_errors,
    )
    return records, stats
