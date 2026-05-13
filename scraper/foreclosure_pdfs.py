"""
Foreclosure-PDF walker and extractor for www.dallascounty.org.

Per §3.7.5 = (b) and §3.7.8: dallascounty.org PDFs cover ~3 months forward
(current + next ~2). Historical notices live on publicsearch.us only.
robots.txt is permissive (§B.2.2); the polite-crawler conduct profile in
§3.7.7 applies (1.5–2 s inter-request gap).

Three layers in this module:

  1. ``walk_foreclosure_index()`` — fetches the landing page, parses the
     per-month listing tables, returns a list of PDF metadata dicts
     including the Last-Modified column.
  2. ``download_pdf()`` — conditional GET; only downloads if Last-Modified
     is newer than the local copy. Returns True if a fresh download
     occurred.
  3. ``extract_pdf_records()`` — uses pdfplumber to get text, then calls
     the pure-function ``extract_records_from_text()`` which is the only
     part that's unit-tested.
"""

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Polite-delay helper
# ═══════════════════════════════════════════════════════════════════════════

def _polite_delay(host_key: str = "dallascounty.org") -> None:
    """Sleep a random (min, max) interval before next request (§3.7.7)."""
    lo, hi = config.RATE_LIMITS.get(host_key, (1.5, 2.0))
    time.sleep(random.uniform(lo, hi))


# ═══════════════════════════════════════════════════════════════════════════
# Index walker
# ═══════════════════════════════════════════════════════════════════════════

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
    visible months. The page rolls — only the current and next ~2 months
    are visible at any time.
    """
    logger.info("Fetching foreclosure index: %s", config.DALLASCOUNTY_FORECLOSURES)
    resp = requests.get(
        config.DALLASCOUNTY_FORECLOSURES,
        headers={"User-Agent": config.USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "lxml")
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


# ═══════════════════════════════════════════════════════════════════════════
# Downloader
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# PDF text-record extraction
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ForeclosureRecord:
    """One foreclosure notice parsed from a PDF."""
    source_pdf: str
    sale_date: Optional[str] = None          # "October 7, 2025" or normalized
    sale_date_iso: Optional[str] = None      # "2025-10-07" if parseable
    property_address: Optional[str] = None
    debtor: Optional[str] = None             # "Maker:" / "Borrower:" / homeowner
    trustee: Optional[str] = None            # Substitute trustee or beneficiary agent
    original_loan_amount: Optional[str] = None
    raw_excerpt: str = ""                    # first ~500 chars of the notice for review
    parse_warnings: list[str] = field(default_factory=list)


def extract_pdf_records(pdf_path: Path) -> list[ForeclosureRecord]:
    """Extract foreclosure-notice records from a PDF.

    Thin wrapper around :func:`extract_records_from_text` that handles
    the PDF→text conversion via pdfplumber. The text-parsing is in a
    separate pure function so it can be unit-tested without a real PDF.
    """
    import pdfplumber

    text_blocks: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                text_blocks.append(t)
    full_text = "\n".join(text_blocks)
    return extract_records_from_text(full_text, source_pdf=str(pdf_path.name))


def extract_records_from_text(
    text: str,
    source_pdf: str = "",
) -> list[ForeclosureRecord]:
    """Split notice-bearing text into per-notice records.

    Heuristic boundary: notices typically open with one of several
    headings. We split on those, then run field regexes on each block.

    Pure function — no I/O. Unit-testable.
    """
    if not text or not text.strip():
        return []

    blocks = _split_into_notices(text)
    records: list[ForeclosureRecord] = []
    for block in blocks:
        rec = _parse_one_notice(block, source_pdf=source_pdf)
        if rec is not None:
            records.append(rec)
    return records


# Notice-opening headings. Order matters — longer/specific patterns first.
_NOTICE_OPENERS = [
    r"NOTICE\s+OF\s+(?:SUBSTITUTE\s+)?TRUSTEE['\u2019]?S?\s+SALE",
    r"NOTICE\s+OF\s+FORECLOSURE\s+SALE",
    r"NOTICE\s+OF\s+ASSESSMENT\s+LIEN\s+SALE",
    r"NOTICE\s+OF\s+SALE",
]
_OPENER_RE = re.compile("|".join(_NOTICE_OPENERS), re.IGNORECASE)


def _split_into_notices(text: str) -> list[str]:
    """Split full text into notice-chunks at each opener heading."""
    matches = list(_OPENER_RE.finditer(text))
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


# Field regexes — conservative. Many notices have slightly different wording.

_SALE_DATE_RE = re.compile(
    r"\b(?:Sale\s+Date|Date\s+of\s+Sale)\s*[:\-]?\s*"
    r"(?P<date>[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
# "on Tuesday, October 7, 2025" — a common embedded form.
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
    r"(?:Maker|Borrower|Mortgagor|Grantor)\s*:?\s*(?P<name>[^\n\r]+?)(?:[\n\r]|$)",
    re.IGNORECASE,
)
_OWNER_RE = re.compile(
    r"(?:present\s+owner(?:s)?\s+of\s+said\s+real\s+property[,;\s]+)"
    r"(?P<name>[^\n\r;]+)",
    re.IGNORECASE,
)
_TRUSTEE_RE = re.compile(
    r"(?:Substitute\s+Trustee|Successor\s+Trustee|Trustee)\s*:?\s*"
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


def _parse_one_notice(block: str, source_pdf: str) -> Optional[ForeclosureRecord]:
    """Regex-parse a single notice block into a record. Returns None if empty."""
    if not block.strip():
        return None

    rec = ForeclosureRecord(
        source_pdf=source_pdf,
        raw_excerpt=block[:500].strip(),
    )

    # Sale date — try explicit "Sale Date:" first, then prose form.
    if m := _SALE_DATE_RE.search(block):
        rec.sale_date = m.group("date").strip()
    elif m := _SALE_DATE_PROSE_RE.search(block):
        rec.sale_date = m.group("date").strip()
    rec.sale_date_iso = _parse_date_iso(rec.sale_date) if rec.sale_date else None

    # Property address.
    if m := _ADDRESS_RE.search(block):
        rec.property_address = m.group("addr").strip().rstrip(".")

    # Debtor / homeowner — Maker line first, fallback to "present owner" prose.
    if m := _MAKER_RE.search(block):
        rec.debtor = m.group("name").strip()
    elif m := _OWNER_RE.search(block):
        rec.debtor = m.group("name").strip()

    # Trustee.
    if m := _TRUSTEE_RE.search(block):
        rec.trustee = m.group("name").strip()

    # Original loan amount.
    if m := _AMOUNT_RE.search(block):
        rec.original_loan_amount = m.group("amount").replace(",", "")

    # Flag empty parses for monitoring.
    if not rec.property_address and not rec.debtor:
        rec.parse_warnings.append("no_address_or_debtor")

    return rec


def _parse_date_iso(date_str: Optional[str]) -> Optional[str]:
    """Convert "October 7, 2025" → "2025-10-07". Returns None on failure."""
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
