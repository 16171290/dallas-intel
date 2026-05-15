"""
US Bankruptcy Court RSS scraper - N.D. Texas (covers Dallas, Fort Worth,
Amarillo, Lubbock divisions).

RSS feed:   https://ecf.txnb.uscourts.gov/cgi-bin/rss_outside.pl
Auth:       None required
Cost:       Free
Refresh:    ~20 min upstream
Lookback:   24 hours

Phase 3.B scope: fetch + parse + extract voluntary petitions.
NOT included here (deferred to 3.C+):
  - DCAD owner-name matching (next module)
  - Canonicalization to the pipeline schema
  - Scoring category integration
  - Pipeline wiring in main.py

The voluntary-petition filter is intentional. Other RSS items (motions,
orders, discharge notices, etc.) are real docket activity but not new
filings; for motivated-seller leads we want filings that just happened.

Pure-function helpers (no I/O) are extracted so the parsing logic can be
exhaustively unit-tested without network access.

Public API
----------
``fetch_voluntary_petitions(feed_url=...)``
    Top-level convenience: fetch + parse + filter + return records.
``fetch_feed(url)``
    HTTP GET. Returns raw bytes. Raises BankruptcyFeedError on failure.
``parse_feed(xml_bytes)``
    XML parse. Returns list of item dicts. Raises BankruptcyParseError.
``extract_voluntary_petitions(items)``
    Filter + extract. Returns list of BankruptcyRecord.

Data model
----------
``BankruptcyRecord`` carries one new-filing record:
    case_number       "26-32140"            (bare, no suffix)
    case_number_raw   "26-32140-7"          (full, with judge/chapter suffix)
    chapter           "7" / "11" / "13"     (None if not classifiable)
    office            "3" (str digit)       (court division code)
    debtor_names      ["Eddie C. Watkins"]  (joint filings -> 2+ entries)
    is_business       False                 (True for LLC/INC/etc.)
    trustee           "Powers, Thomas"      (None for Ch.7)
    pub_date          raw RSS pubDate string
    link              docket URL (gated by PACER fees if fetched)
    raw_description   original <description> content
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Optional


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

DEFAULT_FEED_URL = "https://ecf.txnb.uscourts.gov/cgi-bin/rss_outside.pl"

# Polite, fingerprint-stable Chrome UA. The PACER RSS endpoint is public
# and unauthenticated, but bot-flagging UAs (curl/python-urllib literal
# defaults) sometimes get rate-limited or 403'd.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30  # seconds


# ----------------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------------


class BankruptcyError(Exception):
    """Base for module-specific exceptions."""


class BankruptcyFeedError(BankruptcyError):
    """Raised when the RSS feed cannot be retrieved."""


class BankruptcyParseError(BankruptcyError):
    """Raised when the RSS body cannot be parsed as XML."""


# ----------------------------------------------------------------------------
# Regexes (compiled once at module load)
# ----------------------------------------------------------------------------

# Case-number-with-suffix at the start of a title.
# Examples:
#   "26-32140-7 Eddie C. Watkins"          -> bare="26-32140", suffix="7"
#   "26-32138-mvl13 Jose A. Rivera"        -> bare="26-32138", suffix="mvl13"
#   "26-20164-bwo7 Battery Joe-John LLC"   -> bare="26-20164", suffix="bwo7"
_TITLE_RE = re.compile(
    r"^\s*(?P<case>\d{2}-\d{4,6})-(?P<suffix>[a-z]{0,3}\d{1,2})\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

# Description field fragments. The court emits a freeform string that
# contains "Type:", "Office:", "Chapter:", optionally "Trustee:", then
# the bracketed event type.
_OFFICE_RE   = re.compile(r"Office:\s*(\d+)", re.IGNORECASE)
_CHAPTER_RE  = re.compile(r"Chapter:\s*(\d{1,2})", re.IGNORECASE)
_TRUSTEE_RE  = re.compile(
    r"Trustee:\s*(?P<name>[^\[]+?)(?:\s*\[|$)",
    re.IGNORECASE,
)

# Voluntary-petition detector. Anchored on the opening bracket so we
# never match the phrase appearing in some other narrative context.
_VOLUNTARY_PETITION_RE = re.compile(
    r"\[\s*Voluntary\s+Petition",
    re.IGNORECASE,
)

# Joint-filer splitter. " and " between names indicates two debtors on
# one petition. Spaces required on both sides so we don't split words
# that contain "and" as a substring.
_JOINT_SEP_RE = re.compile(r"\s+and\s+", re.IGNORECASE)

# Business-name suffix detection. A name ending in any of these tokens
# is treated as a business entity (not a homeowner lead candidate).
_BUSINESS_SUFFIX_RE = re.compile(
    r"\b(?:"
    r"LLC|L\.L\.C\.?"
    r"|INC|INCORPORATED"
    r"|CORP|CORPORATION"
    r"|LP|L\.P\.?"
    r"|LTD|LIMITED"
    r"|CO\.?|COMPANY"
    r"|P\.A\.?|PA"
    r"|P\.C\.?|PC"
    r"|PLLC|P\.L\.L\.C\.?"
    r"|LLP|L\.L\.P\.?"
    r")\.?\s*$",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------


@dataclass
class BankruptcyRecord:
    """One voluntary-petition record parsed from the RSS feed."""

    case_number: str                          # "26-32140"
    case_number_raw: str                      # "26-32140-7"
    chapter: Optional[str]                    # "7" / "11" / "13" / None
    office: Optional[str]                     # "3" (Dallas division), etc.
    debtor_names: list[str] = field(default_factory=list)
    is_business: bool = False
    trustee: Optional[str] = None
    pub_date: str = ""
    link: str = ""
    raw_description: str = ""

    def to_dict(self) -> dict:
        """Plain-dict representation for JSON / canonical-record building."""
        return asdict(self)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def fetch_voluntary_petitions(
    feed_url: str = DEFAULT_FEED_URL,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[BankruptcyRecord]:
    """Top-level entry point: fetch the RSS feed and return voluntary
    petitions only.

    Raises:
        BankruptcyFeedError: HTTP failure
        BankruptcyParseError: XML parse failure
    """
    raw = fetch_feed(feed_url, timeout=timeout, user_agent=user_agent)
    items = parse_feed(raw)
    return extract_voluntary_petitions(items)


def fetch_feed(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """HTTP GET the feed. Raises ``BankruptcyFeedError`` on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read()[:200].decode("utf-8", errors="replace")
        except Exception:
            pass
        raise BankruptcyFeedError(
            f"HTTP {e.code} fetching {url}: {e.reason}; body: {body_preview!r}"
        ) from e
    except urllib.error.URLError as e:
        raise BankruptcyFeedError(
            f"Network error fetching {url}: {e.reason}"
        ) from e
    except Exception as e:
        raise BankruptcyFeedError(
            f"Unexpected error fetching {url}: {type(e).__name__}: {e}"
        ) from e


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Parse RSS body into list of item dicts. Raises ``BankruptcyParseError``."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise BankruptcyParseError(f"XML parse failed: {e}") from e

    # Standard RSS layout is rss/channel/item; some feeds skip the
    # channel wrapper. Try both.
    channel = root.find("channel")
    items_root = channel if channel is not None else root

    items: list[dict] = []
    for item in items_root.findall("item"):
        items.append({
            "title":       (item.findtext("title") or "").strip(),
            "link":        (item.findtext("link") or "").strip(),
            "description": (item.findtext("description") or "").strip(),
            "pubDate":     (item.findtext("pubDate") or "").strip(),
            "guid":        (item.findtext("guid") or "").strip(),
        })
    return items


def extract_voluntary_petitions(items: list[dict]) -> list[BankruptcyRecord]:
    """Filter to voluntary-petition items and extract records.

    Items that don't match the voluntary-petition pattern OR whose title
    can't be parsed into a case number + debtor name are silently dropped.
    This is deliberate: the goal is high-precision lead records, not
    exhaustive coverage.
    """
    records: list[BankruptcyRecord] = []
    for item in items:
        if not _is_voluntary_petition(item.get("description", "")):
            continue

        parsed_title = _parse_title(item.get("title", ""))
        if parsed_title is None:
            # Couldn't extract a case number - skip rather than emit a
            # garbage record.
            continue

        case_num_bare, case_num_raw, rest = parsed_title

        # Detect business filings BEFORE joint-split so "Watts and Watts LLC"
        # is classified correctly.
        is_business = _is_business_name(rest)
        debtor_names = [rest] if is_business else _split_joint_filers(rest)

        record = BankruptcyRecord(
            case_number=case_num_bare,
            case_number_raw=case_num_raw,
            chapter=_extract_chapter(item.get("description", "")),
            office=_extract_office(item.get("description", "")),
            debtor_names=debtor_names,
            is_business=is_business,
            trustee=_extract_trustee(item.get("description", "")),
            pub_date=item.get("pubDate", ""),
            link=item.get("link", ""),
            raw_description=item.get("description", ""),
        )
        records.append(record)

    return records


# ----------------------------------------------------------------------------
# Private parsing helpers
# ----------------------------------------------------------------------------


def _parse_title(title: str) -> Optional[tuple[str, str, str]]:
    """Split title into (case_number, case_number_raw, debtor-name-portion).

    Returns None if the title doesn't match the case-number pattern.
    """
    if not title:
        return None
    m = _TITLE_RE.match(title)
    if not m:
        return None
    case_bare = m.group("case")
    suffix    = m.group("suffix")
    rest      = m.group("rest").strip()
    if not rest:
        return None
    case_raw = f"{case_bare}-{suffix}"
    return (case_bare, case_raw, rest)


def _split_joint_filers(name_block: str) -> list[str]:
    """Split a joint-filing name block on ' and '. Returns 1+ names.

    Examples:
        "Michael Earl Watts and Jan M Watts" -> ["Michael Earl Watts",
                                                  "Jan M Watts"]
        "Eddie C. Watkins"                   -> ["Eddie C. Watkins"]
    """
    if not name_block or not name_block.strip():
        return []
    parts = _JOINT_SEP_RE.split(name_block)
    cleaned = [p.strip() for p in parts if p.strip()]
    return cleaned or [name_block.strip()]


def _is_business_name(name: str) -> bool:
    """True if ``name`` ends with a corporate-entity suffix.

    Used to skip business filings from the homeowner-lead pipeline.
    """
    if not name:
        return False
    return bool(_BUSINESS_SUFFIX_RE.search(name.strip()))


def _is_voluntary_petition(description: str) -> bool:
    """True if the description marks a new bankruptcy filing.

    Anchored on the opening bracket of the event type so we don't match
    incidental mentions of the phrase.
    """
    if not description:
        return False
    return bool(_VOLUNTARY_PETITION_RE.search(description))


def _extract_chapter(description: str) -> Optional[str]:
    """Pull the chapter digits out of ``Chapter: 7`` / ``Chapter: 13`` etc."""
    if not description:
        return None
    m = _CHAPTER_RE.search(description)
    return m.group(1) if m else None


def _extract_office(description: str) -> Optional[str]:
    """Pull the office (court division) digit out of ``Office: 3`` etc.

    Office codes for N.D. Texas (observed):
        1 = Lubbock
        2 = Amarillo
        3 = Dallas
        4 = Fort Worth
        5 = Wichita Falls / shared
    These may vary; treat as opaque identifiers.
    """
    if not description:
        return None
    m = _OFFICE_RE.search(description)
    return m.group(1) if m else None


def _extract_trustee(description: str) -> Optional[str]:
    """Pull the trustee name out of ``Trustee: Lopez, Aldo  [...]`` etc.

    Returns None if no Trustee field is present (typical for Ch.7
    voluntary petitions where the trustee hasn't been assigned yet).
    """
    if not description:
        return None
    m = _TRUSTEE_RE.search(description)
    if not m:
        return None
    name = m.group("name").strip().rstrip(",").strip()
    return name or None
