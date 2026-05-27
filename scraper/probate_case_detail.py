"""PR 8 Phase 2 — Tyler/re:SearchTX case detail page scraper.

For each probate (PB) record that didn't match DCAD's owner_index via
the decedent name, fetch the Tyler case detail page and look for
inline property addresses. Texas Estates Code §309.051 requires most
estates to file an Inventory and Appraisement listing the decedent's
real property — when the filing title or summary surfaces the property
address inline on the case detail page, we can recover the property.

Why this matters: 12/21 unresolved PB records on the 2026-05-27 GHA run
had only the decedent name as signal. The Tyler case detail page is the
nearest external source of property data we haven't yet tapped.

What this DOES:
  - Navigate to /CourtRecordsSearch/ui/cases/{case_data_id}/details
  - Apply pre-acquired session cookies (from probate_auth.acquire_session)
  - Wait for SPA hydration; extract full rendered text
  - Search for Texas-style property addresses
  - Report which filings appear on the page (Inventory & Appraisement
    detection is a separate signal)

What this DOES NOT do (v1):
  - Download individual filing PDFs and OCR them
  - Parse structured Inventory & Appraisement schedules
  - Cross-reference filing PDFs with DCAD

Those would be follow-on work if Phase 2 v1's hit rate is too low.

Design choices:
  - Fail-open: any error (auth issue, slow page, malformed HTML) returns
    a result with status='error' and the pipeline moves on. Never crashes
    the daily run.
  - Forensic-friendly: when ``dump_dir`` is set, the full page text and
    HTML are written to disk so the operator can spot-check what Tyler
    actually returns. Critical for first-run debugging since Tyler's HTML
    structure is observed from production, not pre-known.
  - Address detection reuses the venue/trustee blocklist from
    scraper.resolution so courthouse and attorney addresses get rejected.

Public API:
  fetch_case_detail(case_data_id, cookies, *, page=None, base_url=...) -> CaseDetailResult
  parse_property_addresses_from_text(text) -> list[str]
  CaseDetailFetcher  (context manager wrapping a shared Playwright page)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .resolution import VENUE_TRUSTEE_SIGS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class CaseDetailResult:
    """One Tyler case-detail page fetch outcome.

    ``status`` semantics:
      - ``ok``        — page rendered, text extracted, parsing succeeded
      - ``no_filings``— page rendered but no filings list found (auth ok,
                        case may be sealed or in unusual state)
      - ``error``     — navigation or hydration failed; ``error`` field
                        carries the diagnostic
    """
    case_data_id:     str
    status:           str
    page_text:        str = ""
    filings_text:     list[str] = field(default_factory=list)
    addresses_found:  list[str] = field(default_factory=list)
    has_inventory:    bool = False
    error:            Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────
# Address extraction
# ─────────────────────────────────────────────────────────────────────────

# Texas-style property addresses: <num> <street name> <suffix>, <city>,
# TX <zip>. Permissive about street types and city spacing. Anchored on
# the state + zip combo so random numbers in the page don't false-match.
_ADDRESS_RE = re.compile(
    r"\b(\d{2,6}\s+[A-Z][A-Za-z0-9\.\-\' ]{2,60}?\s+"
    r"(?:Street|St|Drive|Dr|Avenue|Ave|Road|Rd|Lane|Ln|Boulevard|Blvd|"
    r"Court|Ct|Circle|Cir|Place|Pl|Way|Trail|Trl|Parkway|Pkwy|"
    r"Highway|Hwy|Terrace|Ter)\.?)"
    r"\s*,?\s+"
    r"([A-Z][A-Za-z\.\-\' ]{2,30}?)"
    r"\s*,\s*"
    r"(?:TX|Texas)"
    r"\s+(\d{5})(?:-\d{4})?\b",
    re.IGNORECASE,
)

# Inventory filing signal — text patterns that suggest the case has filed
# an I&A. Used to flag has_inventory; doesn't affect address extraction.
_INVENTORY_HINT_RE = re.compile(
    r"\bINVENTORY[,\s]+(?:APPRAISEMENT|AND\s+APPRAISEMENT)\b",
    re.IGNORECASE,
)


def parse_property_addresses_from_text(text: Optional[str]) -> list[str]:
    """Extract Texas-style property addresses from rendered page text.

    Rejects courthouse / trustee-office signatures via the canonical
    ``VENUE_TRUSTEE_SIGS`` from scraper.resolution (same list Path B uses).
    Returns unique addresses in first-seen order.

    Returns ``[]`` on empty input or when no addresses are found.
    """
    if not text:
        return []
    matches: list[str] = []
    seen: set[str] = set()
    for m in _ADDRESS_RE.finditer(text):
        street, city, zip_code = m.group(1), m.group(2), m.group(3)
        full = f"{street.strip()}, {city.strip()}, TX {zip_code}"
        full_upper = full.upper()
        # Venue/trustee blocklist
        if any(sig in full_upper for sig in VENUE_TRUSTEE_SIGS):
            continue
        # Dallas-area zip filter (75XXX, 76XXX); reject Houston/Austin/etc.
        if not (zip_code.startswith("75") or zip_code.startswith("76")):
            continue
        if full_upper in seen:
            continue
        seen.add(full_upper)
        matches.append(full)
    return matches


def case_has_inventory_filing(text: Optional[str]) -> bool:
    """True if the page text mentions an Inventory & Appraisement filing.

    This is a SIGNAL — it indicates the case probably has property in
    the filed inventory PDF, even when no address was extractable from
    the rendered page text. Operator can manually inspect those cases.
    """
    if not text:
        return False
    return bool(_INVENTORY_HINT_RE.search(text))


# ─────────────────────────────────────────────────────────────────────────
# Page fetcher
# ─────────────────────────────────────────────────────────────────────────


def _case_detail_url(case_data_id: str, base_url: str) -> str:
    return (
        f"{base_url.rstrip('/')}/CourtRecordsSearch/ui/cases/"
        f"{case_data_id}/details"
    )


def fetch_case_detail(
    case_data_id: str,
    cookies: dict[str, str],
    *,
    page,
    base_url: str = "https://research.txcourts.gov",
    timeout_ms: int = 30_000,
    settle_wait_s: float = 3.0,
    dump_dir: Optional[Path] = None,
) -> CaseDetailResult:
    """Visit the Tyler case-detail page, render via Playwright, extract
    text + addresses.

    The Tyler page is a single-page-application; we navigate, wait for
    networkidle, then settle briefly for late-rendering content. Total
    cost per fetch is ~3-6 seconds.

    Args:
        case_data_id: Tyler caseDataID (returned by the search API)
        cookies: session cookies from ``probate_auth.acquire_session``
        page: live Playwright Page (caller manages browser lifecycle)
        base_url: Tyler base URL (default research.txcourts.gov)
        timeout_ms: per-navigation timeout
        settle_wait_s: extra wait after networkidle for SPA content
        dump_dir: when set, write raw page text + HTML for debugging

    Returns:
        CaseDetailResult with status field indicating success / failure.
        Never raises — pipeline must continue when an individual case fails.
    """
    # Lazy import to avoid circular dep at module-load time.
    from .probate_auth import USER_AGENT as TYLER_UA

    url = _case_detail_url(case_data_id, base_url)
    result = CaseDetailResult(case_data_id=case_data_id, status="error")

    # PR 8.2 fix for 403: Tyler's ALB enforces browser User-Agent.
    # Set the same UA probate_auth uses (and probate.py search API uses).
    # Also set Referer so the request looks like it came from the SPA
    # search results page — Tyler often rejects naked direct navigations
    # to /ui/cases/.../details with no Referer.
    try:
        page.set_extra_http_headers({
            "User-Agent":      TYLER_UA,
            "Referer":         f"{base_url.rstrip('/')}/CourtRecordsSearch/ui/search?q=",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
    except Exception as e:
        logger.debug("case detail %s: header set failed: %s", case_data_id, e)

    # Apply pre-acquired cookies to the browser context so we navigate
    # as the logged-in user. Tyler's SPA depends on these for any data
    # request behind the auth wall.
    try:
        context = page.context
        # Convert cookies dict → Playwright cookie format
        cookie_list = [
            {"name": k, "value": v,
             "domain": ".txcourts.gov", "path": "/"}
            for k, v in cookies.items()
        ]
        if cookie_list:
            context.add_cookies(cookie_list)
    except Exception as e:
        result.error = f"cookie injection failed: {type(e).__name__}: {e}"
        logger.warning("case detail %s: %s", case_data_id, result.error)
        return result

    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except Exception as e:
        result.error = f"goto failed: {type(e).__name__}: {e}"
        logger.warning("case detail %s: %s", case_data_id, result.error)
        return result

    # Brief settle — Tyler's SPA defers some content
    try:
        page.wait_for_timeout(int(settle_wait_s * 1000))
    except Exception:
        pass

    # Extract full visible page text. Tyler doesn't guarantee a stable
    # CSS selector, so we grab everything and search.
    try:
        text = page.inner_text("body", timeout=5_000)
        result.page_text = text
    except Exception as e:
        result.error = f"text extract failed: {type(e).__name__}: {e}"
        logger.warning("case detail %s: %s", case_data_id, result.error)
        return result

    # Forensic dump for first-run debugging
    if dump_dir:
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / f"{case_data_id}.txt").write_text(text, encoding="utf-8")
            try:
                html = page.content()
                (dump_dir / f"{case_data_id}.html").write_text(html, encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            logger.debug("case detail %s: dump failed: %s", case_data_id, e)

    # PR 8.2: detect HTTP-error pages BEFORE running the filings parser.
    # When Tyler's ALB serves a 403/404/5xx page, the body is a tiny
    # error string ("403 Forbidden"). Reporting that as "no_filings" is
    # misleading — it's actually an auth/blocked-by-WAF situation that
    # needs operator intervention (UA tuning, cookie refresh, etc.).
    text_stripped = text.strip()
    text_short = len(text_stripped) < 200
    text_upper = text_stripped.upper()
    error_markers = ("403 FORBIDDEN", "404 NOT FOUND", "ACCESS DENIED",
                     "500 INTERNAL", "502 BAD GATEWAY", "503 SERVICE")
    detected_marker = next((m for m in error_markers if m in text_upper), None)
    if text_short and detected_marker:
        result.status = "error"
        result.error = (
            f"server returned error page ({detected_marker.lower()}). "
            f"Tyler likely blocked the request — verify User-Agent + "
            f"cookies + Referer. Body: {text_stripped!r}"
        )
        logger.warning(
            "case detail %s: %s",
            case_data_id, result.error,
        )
        # Don't bother parsing an error page
        return result

    # Parse: filings list + addresses + inventory hint
    result.filings_text = _extract_filings_lines(text)
    result.addresses_found = parse_property_addresses_from_text(text)
    result.has_inventory = case_has_inventory_filing(text)

    if not result.filings_text and not result.addresses_found:
        result.status = "no_filings"
    else:
        result.status = "ok"

    logger.info(
        "case detail %s: status=%s filings=%d addresses=%d inventory=%s "
        "page_text_len=%d",
        case_data_id, result.status, len(result.filings_text),
        len(result.addresses_found), result.has_inventory, len(text),
    )

    return result


_FILING_KEYWORDS = (
    "FILING", "PETITION", "APPLICATION", "INVENTORY", "APPRAISEMENT",
    "ORDER", "AFFIDAVIT", "MOTION", "LETTERS",
)


def _extract_filings_lines(text: str) -> list[str]:
    """Heuristic: return text lines that look like filing entries.

    A filing entry typically has a date prefix or a keyword like
    INVENTORY / PETITION / APPLICATION. We collect any line that
    contains one of those keywords — a rough proxy for the filings
    list since Tyler's SPA doesn't expose stable selectors.
    """
    if not text:
        return []
    lines: list[str] = []
    for line in text.split("\n"):
        upper = line.upper()
        if any(kw in upper for kw in _FILING_KEYWORDS):
            stripped = line.strip()
            if stripped and len(stripped) < 300:
                lines.append(stripped)
    return lines


# ─────────────────────────────────────────────────────────────────────────
# Context-manager wrapper (mirrors scraper.page_fetcher.PageFetcher)
# ─────────────────────────────────────────────────────────────────────────


class CaseDetailFetcher:
    """Lazy-init Playwright wrapper for Tyler case-detail lookups.

    Usage::

        with CaseDetailFetcher(cookies) as fetcher:
            result = fetcher(case_data_id)
            if result.addresses_found:
                ...

    The browser is launched on first ``__call__`` so a run with zero
    unresolved PB records pays no Playwright cost.
    """

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        base_url: str = "https://research.txcourts.gov",
        headless: bool = True,
        dump_dir: Optional[Path] = None,
    ):
        self._cookies = dict(cookies)
        self._base_url = base_url
        self._headless = headless
        self._dump_dir = dump_dir
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __call__(self, case_data_id: str) -> CaseDetailResult:
        page = self._ensure_page()
        if page is None:
            return CaseDetailResult(
                case_data_id=case_data_id,
                status="error",
                error="playwright init failed",
            )
        return fetch_case_detail(
            case_data_id, self._cookies,
            page=page, base_url=self._base_url,
            dump_dir=self._dump_dir,
        )

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
            # PR 8.2: launch the browser context with the same User-Agent
            # probate_auth used to acquire the session. Tyler's ALB rejects
            # default Chromium UA — see probate.py line 211.
            from .probate_auth import USER_AGENT as TYLER_UA
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
            context = self._browser.new_context(user_agent=TYLER_UA)
            self._page = context.new_page()
            return self._page
        except Exception as e:
            logger.warning(
                "CaseDetailFetcher: Playwright init failed: %s", e,
            )
            return None

    def close(self):
        for closer in (self._page, self._browser, self._pw):
            if closer is None:
                continue
            try:
                if hasattr(closer, "close"):
                    closer.close()
                elif hasattr(closer, "stop"):
                    closer.stop()
            except Exception:
                pass
        self._page = self._browser = self._pw = None
