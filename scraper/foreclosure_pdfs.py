"""
Foreclosure-PDF walker and extractor for www.dallascounty.org.

Per Sec 3.7.5 = (b) and Sec 3.7.8: dallascounty.org PDFs cover ~3 months
forward (current + next ~2). Historical notices live on publicsearch.us only.
robots.txt is permissive (Sec B.2.2); the polite-crawler conduct profile
in Sec 3.7.7 applies (1.5-2 s inter-request gap).

Three layers in this module:

  1. ``walk_foreclosure_index()`` - fetches the landing page, parses the
     per-month listing tables, returns a list of PDF metadata dicts
     including the Last-Modified column.
  2. ``download_pdf()`` - conditional GET; only downloads if Last-Modified
     is newer than the local copy. Returns True if a fresh download
     occurred.
  3. ``extract_pdf_records()`` - uses pdfplumber to get text, then calls
     the pure-function ``extract_records_from_text()`` which is the only
     part that's unit-tested.

Parser strategy (2026-05-14 rewrite):
   The original opener-anchored splitter (splitting text on every
   "NOTICE OF TRUSTEE'S SALE" match) produced ~9 fragments per PDF,
   mostly noise. The new strategy is ADDRESS-ANCHORED: every property
   address detected in the text becomes one record. The surrounding
   text within a bounded context window (clipped by neighboring
   addresses) is searched for the other fields. This produces clean
   1-2 records per typical Dallas County foreclosure PDF.

   Records without extractable addresses fall through to a legacy
   opener-anchored path so we don't completely drop a PDF whose
   address parsing failed.

Notice-date extraction (2026-05-15):
   Added a separate ``notice_date`` field distinct from ``sale_date``.
   Sale date is the auction date (e.g. first Tuesday of next month).
   Notice date is when the trustee filed the foreclosure notice with
   the County Clerk - the operationally important "freshness" signal.

   Four positive patterns (in confidence order):
     P1_DECLARANT  -  "on MM/DD/YY I filed at the office of the DALLAS
                        County Clerk"  (most authoritative)
     P3_WITNESS    -  "WITNESS MY HAND this Xth day of MONTH, YYYY"
     P2_DATED      -  "Dated: <date>" near trustee signature
     P4_DATE_LABEL -  "Date: <Month> <day>, <year>" near trustee context

   Rejection rules block dates appearing in Deed-of-Trust contexts,
   sale-date contexts, notary commission lines, etc. Rejection
   matching is whitespace-normalized to catch pdfplumber extraction
   artifacts like ``deed·of trust`` (middle-dot instead of space).
"""

import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, unquote
import time

import requests
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger(__name__)


# ============================================================================
# Polite-delay helper
# ============================================================================

def _polite_delay(host_key: str = "dallascounty.org") -> None:
    """Sleep a random (min, max) interval before next request (Sec 3.7.7)."""
    lo, hi = config.RATE_LIMITS.get(host_key, (1.5, 2.0))
    time.sleep(random.uniform(lo, hi))


# ============================================================================
# Index walker
# ============================================================================

@dataclass(frozen=True)
class ForeclosurePDFRef:
    """Metadata for one entry in the foreclosure-PDF index."""
    month: str          # e.g. "March"
    city: str           # normalized city name (e.g. "Glenn-Heights")
    week_num: int       # 1..4
    suffix_num: Optional[int]  # 1, 2, 3 for split files like "Dallas_4 (1).pdf"; None otherwise
    pdf_url: str
    last_modified: str  # ISO-ish timestamp from the index, e.g. "2026-02-11 15:36:44"
    size_kb: int


# Filenames look like "Cedar-Hill_3.pdf" or "Dallas_4 (1).pdf".
_FILENAME_RE = re.compile(
    r"^(?P<city>[A-Za-z\- ]+?)_(?P<week>\d+)(?:\s*\((?P<suffix>\d+)\))?\.pdf$",
    re.IGNORECASE,
)


def walk_foreclosure_index() -> list[ForeclosurePDFRef]:
    """Fetch and parse the foreclosures.php landing page.

    Returns one :class:`ForeclosurePDFRef` per PDF listed across all
    visible months. The page rolls - only the current and next ~2 months
    are visible at any time.
    """
    logger.info("Fetching foreclosure index: %s", config.DALLASCOUNTY_FORECLOSURES)
    resp = requests.get(
        config.DALLASCOUNTY_FORECLOSURES,
        headers={"User-Agent": config.USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "html.parser")
    refs: list[ForeclosurePDFRef] = []
    current_month: Optional[str] = None

    # The page structure interleaves <strong>Month</strong> markers with
    # <table> elements. We walk siblings in document order, tracking the
    # current month context as we go.
    for element in soup.find_all(["strong", "h2", "h3", "h4", "table", "a"]):
        # Track month context from headers or strong markers.
        if element.name in ("strong", "h2", "h3", "h4"):
            text = element.get_text(strip=True)
            if _is_month_name(text):
                current_month = text
                continue

        # Parse tables under the current month context.
        if element.name == "table" and current_month:
            refs.extend(_parse_index_table(element, current_month))

    logger.info("Foreclosure index: %d PDF refs across visible months", len(refs))
    return refs


_MONTH_NAMES = {
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
}


def _is_month_name(text: str) -> bool:
    return text.strip() in _MONTH_NAMES


def _parse_index_table(table_el, month: str) -> list[ForeclosurePDFRef]:
    """Parse one month's listing table into ForeclosurePDFRef entries."""
    refs: list[ForeclosurePDFRef] = []

    for row in table_el.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue  # header row or malformed

        link = cells[0].find("a")
        if not link or not link.get("href"):
            continue

        href = link["href"]
        filename = unquote(Path(urlparse(href).path).name)
        m = _FILENAME_RE.match(filename)
        if not m:
            logger.debug("Skipping unrecognized PDF filename: %s", filename)
            continue

        city_raw = m.group("city").strip()
        city = config.DALLAS_CITY_ALIASES.get(city_raw, city_raw)
        try:
            week_num = int(m.group("week"))
        except ValueError:
            continue
        suffix = m.group("suffix")
        suffix_num = int(suffix) if suffix else None

        last_modified = cells[1].get_text(strip=True)
        try:
            size_kb = int(cells[2].get_text(strip=True))
        except (ValueError, IndexError):
            size_kb = 0

        pdf_url = href if href.startswith("http") else urljoin(
            config.DALLASCOUNTY_BASE + "/", href
        )

        refs.append(ForeclosurePDFRef(
            month=month,
            city=city,
            week_num=week_num,
            suffix_num=suffix_num,
            pdf_url=pdf_url,
            last_modified=last_modified,
            size_kb=size_kb,
        ))

    return refs


# ============================================================================
# Downloader
# ============================================================================

def download_pdf(
    ref: ForeclosurePDFRef,
    dest_dir: Path,
    polite: bool = True,
) -> tuple[Path, bool]:
    """Conditional-GET download.

    Saves to ``<dest_dir>/<month>/<filename>``. If the local file already
    exists, sends ``If-Modified-Since`` based on its mtime; on a 304 the
    cached file is kept.

    Returns ``(local_path, downloaded)`` where ``downloaded`` is True iff
    a fresh body was written.
    """
    if polite:
        _polite_delay("dallascounty.org")

    filename = unquote(Path(urlparse(ref.pdf_url).path).name)
    month_dir = dest_dir / ref.month
    month_dir.mkdir(parents=True, exist_ok=True)
    local_path = month_dir / filename

    headers = {"User-Agent": config.USER_AGENT}
    if local_path.exists():
        # HTTP date format: "Wed, 21 Oct 2015 07:28:00 GMT"
        import email.utils
        mtime = local_path.stat().st_mtime
        headers["If-Modified-Since"] = email.utils.formatdate(mtime, usegmt=True)

    resp = requests.get(ref.pdf_url, headers=headers, timeout=120, stream=True)
    if resp.status_code == 304:
        logger.debug("304 not-modified: %s", filename)
        return (local_path, False)

    resp.raise_for_status()
    tmp = local_path.with_suffix(local_path.suffix + ".tmp")
    try:
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
        tmp.replace(local_path)
        logger.info("Downloaded %s/%s", ref.month, filename)
        return (local_path, True)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


# ============================================================================
# PDF text-record extraction
# ============================================================================

@dataclass
class ForeclosureRecord:
    """One foreclosure notice parsed from a PDF."""
    source_pdf: str
    source_pdf_url: Optional[str] = None     # dallascounty.org URL of the containing PDF
    sale_date: Optional[str] = None          # "October 7, 2025" or normalized
    sale_date_iso: Optional[str] = None      # "2025-10-07" if parseable
    notice_date: Optional[str] = None        # raw form of when notice was filed
    notice_date_iso: Optional[str] = None    # "2026-01-12" if parseable - the operationally important freshness signal
    notice_date_pattern: Optional[str] = None  # which extractor pattern fired (audit)
    property_address: Optional[str] = None
    debtor: Optional[str] = None             # "Maker:" / "Borrower:" / homeowner
    trustee: Optional[str] = None            # Substitute trustee or beneficiary agent
    original_loan_amount: Optional[str] = None
    raw_excerpt: str = ""                    # first ~500 chars of the notice for review
    parse_warnings: list[str] = field(default_factory=list)


def extract_pdf_records(
    pdf_path: Path,
    pdf_url: Optional[str] = None,
) -> list[ForeclosureRecord]:
    """Extract foreclosure-notice records from a PDF.

    Thin wrapper around :func:`extract_records_from_text` that handles
    the PDF->text conversion via pdfplumber. The text-parsing is in a
    separate pure function so it can be unit-tested without a real PDF.

    ``pdf_url`` (optional) is the dallascounty.org source URL for the PDF.
    When provided it is stamped onto every record's ``source_pdf_url``
    so downstream consumers can link back to the file.
    """
    import pdfplumber

    text_blocks: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                text_blocks.append(t)
    full_text = "\n".join(text_blocks)
    return extract_records_from_text(
        full_text,
        source_pdf=str(pdf_path.name),
        pdf_url=pdf_url,
    )


def extract_records_from_text(
    text: str,
    source_pdf: str = "",
    pdf_url: Optional[str] = None,
) -> list[ForeclosureRecord]:
    """Extract foreclosure-notice records from PDF text.

    Address-anchored strategy (2026-05-14):
        Each property address detected in the text becomes one record.
        The surrounding text within a bounded context window (clipped
        by neighboring addresses) is searched for the other fields.

    Fallback:
        If no addresses are found at all, we fall through to the
        legacy opener-anchored splitter so we don't completely drop
        the PDF. This path produces records with no address that
        will fail DCAD enrichment but may still be visible for review.

    Pure function - no I/O. Unit-testable.
    """
    if not text or not text.strip():
        return []

    address_matches = list(_ADDRESS_RE.finditer(text))

    if not address_matches:
        # Fallback path - no addresses found, use legacy opener splitter.
        # Records produced here will have no address; they'll fail DCAD
        # enrichment but at least won't be silently dropped.
        return _legacy_opener_split(text, source_pdf, pdf_url=pdf_url)

    # NOTICE-opener positions are used as PREFERRED boundaries between
    # adjacent records in multi-notice PDFs. Without them, we'd risk
    # leaking text (e.g. the previous notice's Sale Date) into the next
    # record's context window.
    opener_starts = [m.start() for m in _STRICT_OPENER_RE.finditer(text)]

    records: list[ForeclosureRecord] = []
    for i, addr_match in enumerate(address_matches):
        addr = addr_match.group("addr").strip().rstrip(".")
        if not addr or len(addr) < 6:
            # Too short to be a real address - skip.
            continue

        addr_start = addr_match.start()
        addr_end = addr_match.end()

        # Lower bound: the nearest NOTICE opener at or before this address
        # (so we don't leak fields from the previous notice). Fall back to
        # the previous address's end, or addr_start - 2500.
        preceding_openers = [s for s in opener_starts if s <= addr_start]
        if preceding_openers:
            ctx_start = preceding_openers[-1]
        else:
            ctx_start = max(0, addr_start - 2500)
        if i > 0:
            prev_end = address_matches[i - 1].end()
            ctx_start = max(ctx_start, prev_end)

        # Upper bound: the earliest of (next NOTICE opener after this
        # address) or (next address) or (addr_end + 2500). The next opener
        # is the strongest signal because it marks the start of a new
        # notice; everything before it belongs to this one.
        following_openers = [s for s in opener_starts if s > addr_end]
        ctx_end_candidates = [addr_end + 2500, len(text)]
        if following_openers:
            ctx_end_candidates.append(following_openers[0])
        if i + 1 < len(address_matches):
            ctx_end_candidates.append(address_matches[i + 1].start())
        ctx_end = min(ctx_end_candidates)

        context = text[ctx_start:ctx_end]

        rec = ForeclosureRecord(
            source_pdf=source_pdf,
            source_pdf_url=pdf_url,
            property_address=addr,
            raw_excerpt=context[:500].strip(),
        )
        _parse_fields_into_record(rec, context)
        records.append(rec)

    return records


def _legacy_opener_split(
    text: str,
    source_pdf: str,
    pdf_url: Optional[str] = None,
) -> list[ForeclosureRecord]:
    """Fallback: opener-anchored split for PDFs with no detectable address.

    Less reliable than the address-anchored path because foreclosure
    notices reference "NOTICE OF TRUSTEE'S SALE" multiple times per
    notice (header + body + footer). To reduce over-splitting we
    require the opener to be at the start of a line AND in uppercase
    (real headers are typically ALL CAPS).
    """
    matches = list(_STRICT_OPENER_RE.finditer(text))
    if not matches:
        return []

    blocks: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)

    records: list[ForeclosureRecord] = []
    for block in blocks:
        rec = ForeclosureRecord(
            source_pdf=source_pdf,
            source_pdf_url=pdf_url,
            raw_excerpt=block[:500].strip(),
        )
        _parse_fields_into_record(rec, block)
        rec.parse_warnings.append("no_address_extracted")
        records.append(rec)
    return records


# ============================================================================
# Field regexes - conservative; many notices have slightly different wording.
# ============================================================================

# Stricter opener: line-anchored AND case-sensitive (real headers are caps).
# Used only in the fallback path now.
_STRICT_OPENER_RE = re.compile(
    r"(?m)^[\s]*(?:"
    r"NOTICE\s+OF\s+(?:SUBSTITUTE\s+)?TRUSTEE['\u2019]?S?\s+SALE"
    r"|NOTICE\s+OF\s+FORECLOSURE\s+SALE"
    r"|NOTICE\s+OF\s+ASSESSMENT\s+LIEN\s+SALE"
    r")"
)

_SALE_DATE_RE = re.compile(
    r"\b(?:Sale\s+Date|Date\s+of\s+Sale)\s*[:\-]?\s*"
    r"(?P<date>[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
# "on Tuesday, October 7, 2025" - a common embedded form.
_SALE_DATE_PROSE_RE = re.compile(
    r"on\s+(?:Tuesday|Monday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*"
    r"(?P<date>[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"(?:commonly\s+known\s+as|property\s+address|premises\s+address)\s*:?\s*"
    r"(?P<addr>[^\n\r]+?)(?:\s*\.\s*$|[\n\r])",
    re.IGNORECASE | re.MULTILINE,
)
_MAKER_RE = re.compile(
    r"(?:Maker|Borrower|Mortgagor|Grantor)\s*\(?s?\)?\s*:\s*"
    r"(?P<name>[^\n\r]+?)(?:[\n\r]|$)",
    re.IGNORECASE,
)
_OWNER_RE = re.compile(
    r"present\s+owner\s*\(?s?\)?\s+of\s+said\s+real\s+property[\s:,;]+"
    r"(?P<name>[^\n\r;]+)",
    re.IGNORECASE,
)
# Trustee regex - REQUIRES literal colon so we don't accidentally match
# the "TRUSTEE'S SALE" header. (2026-05-14 fix.)
_TRUSTEE_RE = re.compile(
    r"(?:Substitute\s+Trustee|Successor\s+Trustee|Trustee)\s*:\s*"
    r"(?P<name>[^\n\r]+?)(?:[\n\r]|$)",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"(?:original\s+(?:principal|loan)\s+amount|principal\s+balance|note\s+amount)"
    r"\s*[:\-]?\s*\$?\s*(?P<amount>[\d,]+(?:\.\d{2})?)",
    re.IGNORECASE,
)
_MONTH_NAME_TO_NUM = {
    "January":  1,  "February": 2,  "March":     3,  "April":   4,
    "May":      5,  "June":     6,  "July":      7,  "August":  8,
    "September":9,  "October":  10, "November":  11, "December":12,
}


# ============================================================================
# Notice-date extraction (2026-05-15)
# ============================================================================
#
# Validated across 98 PDF corpus. 58/98 (~59%) yield a notice date.
# Four patterns in confidence order:
#
#   P1 - Declarant filing clause (most authoritative)
#        "...on 02/19/26 I filed at the office of the DALLAS County Clerk..."
#
#   P2 - "Dated: <date>" near trustee signature
#        "Dated: 1/12/2026"  -> the trustee-signing date, within days of clerk filing
#
#   P3 - "WITNESS MY HAND this Xth day of MONTH, YYYY" - legal formal version of P2
#
#   P4 - "Date: <Month> <day>, <year>" near trustee context
#        "Date: February 9, 2026"  -> trustee signing date in label form
#
# A date candidate is REJECTED if its preceding 200 chars (after whitespace
# normalization) contain a phrase indicating a Deed-of-Trust context, a
# sale-date context, a notary commission line, etc.

# Pattern 1: Declarant-filed (highest confidence)
_NOTICE_DECLARANT_RE = re.compile(
    r"on\s+(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+I\s+filed\s+at\s+the\s+office\s+of\s+(?:the\s+)?DALLAS\s+County",
    re.IGNORECASE,
)

# Pattern 2: Trustee Dated: line
_NOTICE_DATED_RE = re.compile(
    r"(?im)(?:^|\n)\s*Dated:?\s*(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
)

# Pattern 3: WITNESS MY HAND this <date>
_NOTICE_WITNESS_RE = re.compile(
    r"WITNESS\s+(?:MY|my)\s+hand\s+this\s+(?P<date>\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+[A-Z][a-z]+,?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    re.IGNORECASE,
)

# Pattern 4: Date: <Month> <day>, <year>
_NOTICE_DATE_LABEL_RE = re.compile(
    r"(?im)(?:^|\n)\s*Date:\s*(?P<date>[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
)

# Phrases whose presence in the 200 chars BEFORE a date candidate marks
# the date as NOT a notice-filing date (Deed-of-Trust, mortgage origination,
# prior recording, sale date, notary expiry, etc.). Comparison is done
# AFTER whitespace normalization so PDF artifacts like "deed.of trust"
# (middle-dot) still match "deed of trust".
_NOTICE_REJECT_PHRASES = (
    "deed of trust",
    "whereas",
    "recorded",
    "original mortgagee",
    "instrument to be foreclosed",
    "executed",
    "mortgagor",
    "grantor s",         # "Grantor(s):" after punctuation strip
    "deed of trust date",
    "in payment of a debt",
    "note dated",
    "promissory note",
    "notice of sale",
    "notary",
    "commission expires",
    "deed of trust information",
    "date of sale",
    "notice is hereby given that on",
    "foreclosure sale will be conducted",
    "sale is scheduled",
    "first tuesday",
    "time of sale",
    "place of sale",
)


def _normalize_for_match(snippet: str) -> str:
    """Collapse whitespace and non-alphanumeric runs to single spaces.

    Used so the reject-phrase substring search catches pdfplumber
    extraction artifacts like ``deed.of trust`` (middle-dot instead of
    space) or ``deed  of   trust`` (collapsed whitespace).
    """
    if not snippet:
        return ""
    # Replace any sequence of non-alphanumeric chars (including unicode
    # punctuation like U+00B7 middle dot) with a single space.
    return re.sub(r"[^a-z0-9]+", " ", snippet.lower()).strip()


def _has_notice_date_reject_context(
    text: str,
    position: int,
    window: int = 200,
) -> Optional[str]:
    """Check the ``window`` chars before ``position`` for rejection phrases.

    Returns the matching phrase if rejected, None if accepted.
    """
    if position <= 0:
        return None
    start = max(0, position - window)
    snippet = text[start:position]
    normalized = _normalize_for_match(snippet)
    for phrase in _NOTICE_REJECT_PHRASES:
        if phrase in normalized:
            return phrase
    return None


def _notice_date_to_iso(raw: str) -> Optional[str]:
    """Convert a raw date string (any of the four pattern formats) to ISO.

    Returns None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip().replace("\n", " ")
    raw = re.sub(r"\s+", " ", raw)

    # M/D/YYYY or M/D/YY or M-D-YYYY etc.
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", raw)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            # Two-digit year: 00-49 -> 2000s, 50-99 -> 1900s
            y += 2000 if y < 50 else 1900
        try:
            from datetime import date
            return date(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # "Month D, YYYY" or "Month D YYYY"
    m = re.match(r"^([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})$", raw)
    if m:
        mo = _MONTH_NAME_TO_NUM.get(m.group(1))
        if mo:
            try:
                from datetime import date
                return date(int(m.group(3)), mo, int(m.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                return None

    # "Xth day of Month, YYYY"
    m = re.match(
        r"^(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Z][a-z]+),?\s+(\d{4})$",
        raw,
        re.IGNORECASE,
    )
    if m:
        # Title-case the month for lookup since case-insensitive.
        mo_name = m.group(2).capitalize()
        mo = _MONTH_NAME_TO_NUM.get(mo_name)
        if mo:
            try:
                from datetime import date
                return date(int(m.group(3)), mo, int(m.group(1))).strftime("%Y-%m-%d")
            except ValueError:
                return None

    return None


def _extract_notice_date(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Try each pattern in confidence order, apply rejection.

    Returns ``(iso_or_None, raw_or_None, pattern_name_or_None)``. Returns
    ``(None, None, None)`` if no valid candidate survives rejection.
    """
    if not text:
        return (None, None, None)

    candidates = []
    for pattern_name, pattern, confidence in [
        ("P1_DECLARANT",  _NOTICE_DECLARANT_RE,   1),
        ("P3_WITNESS",    _NOTICE_WITNESS_RE,     2),
        ("P2_DATED",      _NOTICE_DATED_RE,       3),
        ("P4_DATE_LABEL", _NOTICE_DATE_LABEL_RE,  4),
    ]:
        for m in pattern.finditer(text):
            raw = m.group("date").strip()
            iso = _notice_date_to_iso(raw)
            if not iso:
                continue  # parse failure - skip
            reject = _has_notice_date_reject_context(text, m.start())
            if reject:
                continue  # rejected - skip
            candidates.append({
                "pattern":    pattern_name,
                "confidence": confidence,
                "raw":        raw,
                "iso":        iso,
                "position":   m.start(),
            })

    if not candidates:
        return (None, None, None)

    # Sort by confidence (lower = higher conf), then position (earlier = better).
    candidates.sort(key=lambda c: (c["confidence"], c["position"]))
    best = candidates[0]
    return (best["iso"], best["raw"], best["pattern"])


# ============================================================================
# Parsing helpers
# ============================================================================

def _parse_fields_into_record(rec: ForeclosureRecord, block: str) -> None:
    """Run field regexes on a context block and populate the record in-place."""
    # Sale date - try explicit "Sale Date:" first, then prose form.
    if m := _SALE_DATE_RE.search(block):
        rec.sale_date = m.group("date").strip()
    elif m := _SALE_DATE_PROSE_RE.search(block):
        rec.sale_date = m.group("date").strip()
    rec.sale_date_iso = _parse_date_iso(rec.sale_date) if rec.sale_date else None

    # Notice date - the operationally important field for "filed in last
    # N days" filtering. Separate from sale_date.
    notice_iso, notice_raw, notice_pattern = _extract_notice_date(block)
    rec.notice_date_iso = notice_iso
    rec.notice_date = notice_raw
    rec.notice_date_pattern = notice_pattern

    # Debtor / homeowner - Maker line first, fallback to "present owner" prose.
    if m := _MAKER_RE.search(block):
        rec.debtor = _clean_name(m.group("name"))
    elif m := _OWNER_RE.search(block):
        rec.debtor = _clean_name(m.group("name"))

    # Trustee.
    if m := _TRUSTEE_RE.search(block):
        rec.trustee = _clean_name(m.group("name"))

    # Original loan amount.
    if m := _AMOUNT_RE.search(block):
        rec.original_loan_amount = m.group("amount").replace(",", "")


# Legacy alias kept for backward compatibility with anything that imported
# the original _parse_one_notice name (e.g. unit tests).
def _parse_one_notice(block: str, source_pdf: str) -> Optional[ForeclosureRecord]:
    """Regex-parse a single notice block into a record. Returns None if empty.

    Preserved for backward compatibility with the original API. The current
    primary entry point is ``extract_records_from_text``, which uses
    address-anchored extraction; this function still works for callers that
    want to parse a single pre-split block.
    """
    if not block.strip():
        return None
    rec = ForeclosureRecord(
        source_pdf=source_pdf,
        raw_excerpt=block[:500].strip(),
    )
    # Address (in single-block mode we also extract here).
    if m := _ADDRESS_RE.search(block):
        rec.property_address = m.group("addr").strip().rstrip(".")

    _parse_fields_into_record(rec, block)

    if not rec.property_address and not rec.debtor:
        rec.parse_warnings.append("no_address_or_debtor")
    return rec


# Legacy alias for tests that import _split_into_notices.
def _split_into_notices(text: str) -> list[str]:
    """Legacy opener-anchored split. Kept for backward-compat with tests.

    Production code uses address-anchored extraction in
    ``extract_records_from_text``; this function is retained only so
    existing tests don't break.
    """
    matches = list(_STRICT_OPENER_RE.finditer(text))
    if not matches:
        return [text] if text.strip() else []
    blocks: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def _clean_name(name: Optional[str]) -> Optional[str]:
    """Strip leading punctuation/digits/whitespace from extracted name strings.

    PDF text often captures the form-label noise: "(s):", "{s):", ":", etc.
    This helper trims those prefixes so the canonical record has clean names.

    Examples:
        "(s): Jerry L Fletcher"   -> "Jerry L Fletcher"
        "{s): Sherena Smith"      -> "Sherena Smith"
        "  :  John Doe"           -> "John Doe"
        ":(s) Jane Doe"           -> "Jane Doe"
    """
    if not name:
        return None
    cleaned = name.strip()
    # Step 1: strip parenthesized-s prefix like "(s):", "{s):", "(S):".
    # This must run BEFORE the general punctuation strip because the
    # 's' letter inside is non-punctuation and would stop a naive strip.
    cleaned = re.sub(
        r"^[\(\{]\s*s\s*[\)\}]\s*[:;,]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Step 2: strip any remaining leading punctuation/digits/whitespace.
    cleaned = re.sub(r"^[\(\)\{\}:;,\-\.\s\d]+", "", cleaned)
    # Step 3: strip trailing punctuation crud.
    cleaned = re.sub(r"[\s\.\;,]+$", "", cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else None


def _parse_date_iso(date_str: Optional[str]) -> Optional[str]:
    """Convert "October 7, 2025" -> "2025-10-07". Returns None on failure."""
    if not date_str:
        return None
    m = re.match(
        r"^(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})$",
        date_str.strip(),
    )
    if not m:
        return None
    month_num = _MONTH_NAME_TO_NUM.get(m.group("month"))
    if not month_num:
        return None
    try:
        return f"{int(m.group('year')):04d}-{month_num:02d}-{int(m.group('day')):02d}"
    except ValueError:
        return None
