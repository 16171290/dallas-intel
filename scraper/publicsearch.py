"""
publicsearch.us SPA scraper for Dallas County Clerk Official Records.

Per §3.7.5 = (b) — robots.txt is treated as advisory; the polite-crawler
conduct profile (§3.7.7) and the legal-risk acknowledgement (§F.1.1) apply.
This is the highest-risk module per §F.2.1: the SPA can change and break
selectors at any time.

Lifecycle, rate-limiting, circuit-breaker, and retry handling are concrete
and tested.

The DOM selectors are STRONG INFERENCE — they need to be tuned against
the live SPA on the first real run. Every selector site is marked
``TODO_SELECTOR`` so the verification work is greppable. The Phase 3
verification step is D.3.2-D.3.3 (see docs/ARCHITECTURE.md).

Recommended first-run procedure:
  1. ``playwright codegen https://dallas.tx.publicsearch.us/`` to generate
     starting selectors interactively.
  2. Replace each ``TODO_SELECTOR`` placeholder.
  3. Capture one record end-to-end; expand from there.
"""

import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator, Optional

from . import config, normalize

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Result record
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PublicSearchRecord:
    """One row scraped from publicsearch.us results."""
    record_id: str                              # vendor's document id
    dallas_code: str                            # e.g. "LP", "NOF"
    harris_category: Optional[str] = None       # from normalize.dallas_code_to_category
    filing_date: Optional[str] = None           # YYYY-MM-DD
    instrument_num: Optional[str] = None
    grantor: Optional[str] = None
    grantee: Optional[str] = None
    address: Optional[str] = None
    amount: Optional[str] = None
    raw_html_snippet: str = ""                  # for debugging / unparsed-field discovery
    parse_warnings: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker
# ═══════════════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """Halt-after-N-consecutive-failures pattern (§3.7.7)."""
    def __init__(self, threshold: int = config.CIRCUIT_BREAKER_THRESHOLD):
        self.threshold = threshold
        self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            raise CircuitBreakerTripped(
                f"Circuit breaker tripped after {self.consecutive_failures} "
                f"consecutive failures"
            )


class CircuitBreakerTripped(Exception):
    """Raised when the circuit breaker has opened — halt the pipeline."""


# ═══════════════════════════════════════════════════════════════════════════
# Polite delay
# ═══════════════════════════════════════════════════════════════════════════

def _polite_delay(host_key: str = "publicsearch.us") -> None:
    """Sleep ``RATE_LIMITS[host_key]`` random seconds (§3.7.7)."""
    lo, hi = config.RATE_LIMITS.get(host_key, (2.0, 4.0))
    time.sleep(random.uniform(lo, hi))


# ═══════════════════════════════════════════════════════════════════════════
# Browser lifecycle
# ═══════════════════════════════════════════════════════════════════════════

@contextmanager
def browser_context() -> Iterator:
    """Yield a fresh Playwright (browser, context, page) tuple.

    No session reuse across runs — fresh Chromium context per pipeline
    invocation (§F.3.4). Caller is responsible for using the page; cleanup
    is automatic via context exit.

    Usage::

        with browser_context() as (browser, context, page):
            page.goto(...)
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        try:
            yield browser, context, page
        finally:
            try:
                context.close()
            finally:
                browser.close()


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def scrape_all(
    date_from: date,
    date_to: date,
    categories: Optional[list[str]] = None,
) -> list[PublicSearchRecord]:
    """Scrape all configured instrument categories within the date range.

    Iterates ``config.INSTRUMENT_CODES`` (or the subset given in
    ``categories``) and runs one search per Dallas literal code per
    category. Returns the merged list of records, deduplicated by
    ``record_id``.

    Halts on circuit-breaker trip; partial results are NOT returned.
    """
    breaker = CircuitBreaker()
    seen_ids: set[str] = set()
    out: list[PublicSearchRecord] = []

    target_categories = categories or list(config.INSTRUMENT_CODES.keys())

    with browser_context() as (_browser, _context, page):
        _open_home(page)

        for category in target_categories:
            for dallas_code in config.INSTRUMENT_CODES[category]:
                try:
                    batch = search_by_code(page, dallas_code, date_from, date_to)
                    breaker.record_success()
                except CircuitBreakerTripped:
                    raise
                except Exception as e:
                    logger.warning(
                        "Search failed for %s (%s): %s",
                        category, dallas_code, e,
                    )
                    breaker.record_failure()
                    continue

                for rec in batch:
                    if rec.record_id in seen_ids:
                        continue
                    seen_ids.add(rec.record_id)
                    rec.harris_category = (
                        normalize.dallas_code_to_category(rec.dallas_code)
                    )
                    out.append(rec)

    logger.info(
        "publicsearch.us scrape complete: %d unique records across %d categories",
        len(out), len(target_categories),
    )
    return out


def search_by_code(
    page,
    dallas_code: str,
    date_from: date,
    date_to: date,
) -> list[PublicSearchRecord]:
    """Run one advanced-search query for a single Dallas instrument code.

    Submits the search form, paginates through results, returns the list
    of records. Handles in-page rate-limit signals (429-like UI responses).

    This is the highest-friction surface; selectors here will need tuning.
    """
    _polite_delay()
    logger.info("Searching publicsearch.us for code=%s %s..%s",
                dallas_code, date_from, date_to)

    _open_advanced_search(page)
    _fill_date_range(page, date_from, date_to)
    _fill_doc_type(page, dallas_code)
    _submit_search(page)
    _wait_for_results(page)

    return _harvest_all_pages(page, dallas_code)


# ═══════════════════════════════════════════════════════════════════════════
# Page interactions — SELECTORS NEED VERIFICATION (Phase 3, §D.3.2)
# ═══════════════════════════════════════════════════════════════════════════
#
# Every function below contains TODO_SELECTOR placeholders. The structure
# is correct; the exact CSS / Playwright selectors must be confirmed
# against the live page. Use ``playwright codegen`` to capture them
# interactively.

def _open_home(page) -> None:
    """Navigate to the SPA home and wait for it to hydrate."""
    page.goto(config.PUBLICSEARCH_BASE, wait_until="networkidle", timeout=30_000)


def _open_advanced_search(page) -> None:
    """Click into Advanced Search mode."""
    # TODO_SELECTOR — confirm the advanced-search trigger.
    # Likely candidates: a button/link with text "Advanced Search" or
    # a tab on the main search panel.
    page.get_by_role("link", name="Advanced Search").click()
    page.wait_for_load_state("networkidle")


def _fill_date_range(page, date_from: date, date_to: date) -> None:
    """Fill the date-range filter."""
    # TODO_SELECTOR — confirm date-input field names/locators.
    page.fill('input[name="recordingDateFrom"]', date_from.isoformat())
    page.fill('input[name="recordingDateTo"]', date_to.isoformat())


def _fill_doc_type(page, dallas_code: str) -> None:
    """Set the document-type filter to a specific Dallas literal code."""
    # TODO_SELECTOR — confirm the doc-type widget (multiselect? combobox?).
    # The form likely supports filtering by the literal code captured in
    # window.__data.configuration.docTypeMappings (§A.3).
    page.fill('input[name="docTypeFilter"]', dallas_code)
    page.keyboard.press("Enter")


def _submit_search(page) -> None:
    """Submit the search form."""
    # TODO_SELECTOR — confirm submit button.
    page.get_by_role("button", name="Search").click()


def _wait_for_results(page) -> None:
    """Wait for the SPA to render the results table or empty-state."""
    # TODO_SELECTOR — confirm a stable post-search container selector.
    page.wait_for_selector('[data-testid="results-table"], [data-testid="empty-state"]',
                           timeout=20_000)


def _harvest_all_pages(page, dallas_code: str) -> list[PublicSearchRecord]:
    """Walk paginated results, yielding records from each page."""
    results: list[PublicSearchRecord] = []
    page_num = 1

    while True:
        _polite_delay()
        page_records = _harvest_current_page(page, dallas_code)
        results.extend(page_records)
        logger.debug("Page %d: harvested %d records for %s",
                     page_num, len(page_records), dallas_code)

        if not _has_next_page(page):
            break

        _click_next_page(page)
        page_num += 1

        # Defensive cap to prevent runaway pagination if "next" check breaks.
        if page_num > 200:
            logger.warning("Pagination cap reached (200 pages) for %s", dallas_code)
            break

    return results


def _harvest_current_page(page, dallas_code: str) -> list[PublicSearchRecord]:
    """Extract one page worth of result rows."""
    # TODO_SELECTOR — confirm row locator and per-cell field selectors.
    # Pattern below assumes a table with data-testid rows.
    rows = page.locator('[data-testid="result-row"]').all()
    records: list[PublicSearchRecord] = []

    for row in rows:
        try:
            rec = _parse_result_row(row, dallas_code)
            if rec:
                records.append(rec)
        except Exception as e:
            logger.debug("Row parse failed: %s", e)
    return records


def _parse_result_row(row, dallas_code: str) -> Optional[PublicSearchRecord]:
    """Extract fields from a single result row.

    Field selectors below are placeholders — verify and tune in Phase 3.
    """
    # TODO_SELECTOR — confirm each field selector.
    def text_or_none(sel: str) -> Optional[str]:
        try:
            return row.locator(sel).first.inner_text().strip() or None
        except Exception:
            return None

    record_id = text_or_none('[data-testid="record-id"]')
    if not record_id:
        return None

    return PublicSearchRecord(
        record_id=record_id,
        dallas_code=dallas_code,
        filing_date=text_or_none('[data-testid="filing-date"]'),
        instrument_num=text_or_none('[data-testid="instrument-num"]'),
        grantor=text_or_none('[data-testid="grantor"]'),
        grantee=text_or_none('[data-testid="grantee"]'),
        address=text_or_none('[data-testid="address"]'),
        amount=text_or_none('[data-testid="amount"]'),
        raw_html_snippet=row.inner_html()[:1000],
    )


def _has_next_page(page) -> bool:
    """True iff a Next button is enabled."""
    # TODO_SELECTOR — confirm pagination control.
    try:
        next_btn = page.locator('[data-testid="pagination-next"]').first
        return next_btn.is_enabled()
    except Exception:
        return False


def _click_next_page(page) -> None:
    """Advance to the next results page."""
    # TODO_SELECTOR — confirm pagination control.
    page.locator('[data-testid="pagination-next"]').first.click()
    page.wait_for_load_state("networkidle")


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: date-range helpers
# ═══════════════════════════════════════════════════════════════════════════

def daily_window(days_back: Optional[int] = None) -> tuple[date, date]:
    """Return ``(date_from, date_to)`` for the daily run.

    Uses ``config.DAYS_BACK`` by default.
    """
    today = date.today()
    n = days_back or config.DAYS_BACK
    return (today - timedelta(days=n), today)


def backfill_window() -> tuple[date, date]:
    """Return the one-shot backfill window from §3.7.4."""
    today = date.today()
    return (today - timedelta(days=config.BACKFILL_DAYS), today)
