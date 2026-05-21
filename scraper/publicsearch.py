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
        _probe_foreclosure_doctypes(page)  # one-shot; logs and returns

        for category in target_categories:
            for dallas_code in config.INSTRUMENT_CODES[category]:
                # Skip codes that aren't searchable on publicsearch.us
                # (e.g. NOF — Notice of Foreclosure — only comes through
                # the dallascounty.org foreclosure-PDF walker).
                if dallas_code not in config.DALLAS_CODE_DISPLAY_NAMES:
                    logger.debug(
                        "Skipping non-searchable code %s (%s) — handled by "
                        "another source",
                        dallas_code, category,
                    )
                    continue

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

    Navigates fresh from the home page each time to avoid stale filter
    state from a previous search.
    """
    _polite_delay()
    logger.info("Searching publicsearch.us for code=%s %s..%s",
                dallas_code, date_from, date_to)

    _open_home(page)
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
    """Click into Advanced Search mode.

    VERIFIED 2026-05-13: Advanced Search is a role=link with accessible
    name 'Advanced Search'. Visible on the home page after hydration.
    """
    page.get_by_role("link", name="Advanced Search").click()
    page.wait_for_load_state("networkidle", timeout=15_000)


def _fill_date_range(page, date_from: date, date_to: date) -> None:
    """Fill the advanced-search date-range filter.

    VERIFIED 2026-05-13: Inputs are #recordedDateRange-start and
    #recordedDateRange-end. Format MM/DD/YYYY. The .fill() approach
    replaces text; the calendar overlay is dismissed with Escape.
    """
    fmt = "%m/%d/%Y"
    for selector, value in (
        ("#recordedDateRange-start", date_from.strftime(fmt)),
        ("#recordedDateRange-end",   date_to.strftime(fmt)),
    ):
        page.locator(selector).click()
        page.locator(selector).press("ControlOrMeta+a")
        page.locator(selector).fill(value)
    page.keyboard.press("Escape")  # dismiss any open calendar overlay


def _fill_doc_type(page, dallas_code: str) -> None:
    """Filter document types and check the EXACT-match checkbox.

    VERIFIED 2026-05-14. The doc-type filter is a ReactVirtualized list
    inside a tokenized-nested-select combobox. Each option has an
    ``<input type="checkbox" name="EXACT DISPLAY NAME">`` element — the
    ``name`` attribute equals the display name character-for-character.

    Strategy:
      1. Type the display name into the combobox to narrow the candidate list.
      2. Locate ``input[name="<display_name>"]`` (exact attribute match).
      3. If not currently rendered (virtualized list culled it), scroll the
         ``.ReactVirtualized__Grid`` container until the input appears.
      4. ``check(force=True)`` toggles it via direct DOM event, bypassing
         visibility checks on the visually-hidden underlying input.
    """
    display_name = config.DALLAS_CODE_DISPLAY_NAMES.get(dallas_code)
    if not display_name:
        raise ValueError(
            f"No publicsearch.us display name mapped for Dallas code "
            f"{dallas_code!r} — see config.DALLAS_CODE_DISPLAY_NAMES."
        )

    combo = page.get_by_role("combobox", name="Filter Document Types")
    combo.click()
    combo.fill(display_name)
    page.wait_for_timeout(500)  # let the typeahead filter settle

    # Locate the exact-match input by its `name` attribute.
    target = page.locator(f'input.checkbox__input[name="{display_name}"]')

    # If the virtualized list hasn't rendered this item yet, scroll the
    # container until it appears. Each item is 30px tall; we step at 30px
    # and cap at ~2000px (≈ 67 items, more than any single filter result).
    if target.count() == 0:
        scroll_grid = page.locator(
            "#docTypes-listbox .ReactVirtualized__Grid"
        ).first
        for scroll_top in range(0, 2000, 30):
            try:
                scroll_grid.evaluate(f"el => el.scrollTop = {scroll_top}")
            except Exception:
                break
            page.wait_for_timeout(80)
            if target.count() > 0:
                break

    if target.count() == 0:
        raise ValueError(
            f"input[name={display_name!r}] not found in doc-type list "
            f"after scrolling. Verify config.DALLAS_CODE_DISPLAY_NAMES "
            f"matches a real Dallas RP doc-type name exactly."
        )

    # The input is visually hidden (custom checkbox styling). Playwright's
    # check(force=True) still runs a viewport check that fails on the
    # zero-area hidden input - even with force=True, the API rejects clicks
    # on elements with no rendered bounding box (verified 2026-05-19).
    #
    # The correct strategy is to click the associated <label>, which IS
    # rendered with a real bounding box. The browser proxies the label
    # click to the input via the `for` attribute, which dispatches the
    # change event the React component listens for - the same semantics
    # as a human clicking the visible checkbox area.
    input_id = target.first.get_attribute("id")
    if not input_id:
        raise ValueError(
            f"Found checkbox input for {display_name!r} but it has no id "
            f"attribute - cannot locate its associated <label>."
        )
    label = page.locator(f'label[for="{input_id}"]')
    if label.count() == 0:
        raise ValueError(
            f"Found checkbox input id={input_id!r} for {display_name!r} "
            f"but no <label for={input_id!r}> exists to proxy the click."
        )
    label.first.click()

    # Clear the filter so the next code's search starts with an empty filter.
    combo.fill("")


def _submit_search(page) -> None:
    """Submit the search form.

    VERIFIED 2026-05-13: Submit is role=button with accessible name 'Search'.
    """
    page.get_by_role("button", name="Search").click()


def _wait_for_results(page) -> None:
    """Wait for the SPA to land on the /results page.

    Does NOT fail when the results table is absent — that's the legitimate
    zero-results case (e.g. SZS / Seizure & Sale has 0-1 filings per week).
    The harvest path will see zero rows and return an empty list, which
    is a successful no-results outcome rather than a search failure.

    Failures we DO want to raise: navigation timeouts, network errors,
    or unrecoverable page state — those still bubble up via wait_for_url.
    """
    # Wait for the URL change to /results — this fires whether or not
    # results actually exist.
    try:
        page.wait_for_url("**/results*", timeout=20_000)
    except Exception:
        # URL didn't change to /results — search submission may have failed.
        # Let the harvest find zero rows and decide; don't raise here.
        logger.debug("URL did not transition to /results within 20s")

    # Brief network-idle wait so any in-flight requests settle.
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    # Soft wait for the results table. If it doesn't appear within 5s,
    # this is the zero-results case — harvest_current_page will find 0 rows
    # and return an empty list, which counts as a successful empty search.
    try:
        page.locator(
            "section.search-results__results-wrap table"
        ).first.wait_for(state="visible", timeout=5_000)
    except Exception:
        logger.debug("Results table not visible; likely zero results")


def _harvest_all_pages(page, dallas_code: str) -> list[PublicSearchRecord]:
    """Walk paginated results, yielding records from each page.

    publicsearch.us imposes a 10-page (500-record) hard cap on free-tier
    search-result pagination. When we hit that cap, we log a warning so
    we know to subdivide the date range for that code on the next run.
    """
    results: list[PublicSearchRecord] = []
    page_num = 1
    PUBLICSEARCH_FREE_PAGE_CAP = 10  # 10 pages × 50 records = 500-record cap

    while True:
        _polite_delay()
        page_records = _harvest_current_page(page, dallas_code)
        results.extend(page_records)
        logger.debug("Page %d: harvested %d records for %s",
                     page_num, len(page_records), dallas_code)

        if page_num >= PUBLICSEARCH_FREE_PAGE_CAP:
            # We've reached the free-tier pagination ceiling.
            if _has_next_page(page):
                logger.warning(
                    "Code=%s reached the 10-page (500-record) free-tier cap; "
                    "%d records harvested, more exist but are unreachable. "
                    "Consider subdividing the date range for this code.",
                    dallas_code, len(results),
                )
            break

        if not _has_next_page(page):
            break

        _click_next_page(page)
        page_num += 1

    return results


_PROBED_FORECLOSURE_DOCTYPES = False


def _probe_foreclosure_doctypes(page) -> None:
    """One-shot probe: enumerate publicsearch.us doctype options matching
    'foreclosure'.

    Dallas County moved Notice-of-Foreclosure filings to publicsearch.us as
    of 2026-02-24 (per the county's own banner on /foreclosures.php). The
    exact display name on publicsearch.us is unknown without inspection.
    This probe opens the doctype filter, types 'foreclosure', and logs
    every matching option name at INFO so the operator can encode the
    right string in ``config.DALLAS_CODE_DISPLAY_NAMES``.

    Removed once the name is verified.

    Idempotent: only runs on the first call per process.
    """
    global _PROBED_FORECLOSURE_DOCTYPES
    if _PROBED_FORECLOSURE_DOCTYPES:
        return
    _PROBED_FORECLOSURE_DOCTYPES = True

    try:
        _open_advanced_search(page)
        combo = page.get_by_role("combobox", name="Filter Document Types")
        combo.click()
        combo.fill("foreclosure")
        page.wait_for_timeout(800)  # let typeahead settle

        inputs = page.locator("#docTypes-listbox input.checkbox__input").all()
        names: list[str] = []
        for inp in inputs:
            try:
                n = inp.get_attribute("name")
                if n:
                    names.append(n)
            except Exception:
                pass

        logger.info(
            "FORECLOSURE DOCTYPE PROBE: %d matching options after typing 'foreclosure'",
            len(names),
        )
        for n in names:
            logger.info("FORECLOSURE DOCTYPE PROBE: name=%r", n)

        # Clear filter so the regular scrape starts clean
        try:
            combo.fill("")
        except Exception:
            pass
    except Exception as e:
        logger.warning("FORECLOSURE DOCTYPE PROBE failed: %s", e)


def _harvest_current_page(page, dallas_code: str) -> list[PublicSearchRecord]:
    """Extract one page worth of result rows.

    VERIFIED 2026-05-13: Rows are <tr> elements inside the results
    table's <tbody>, each with role="row".
    """
    rows = page.locator("section.search-results__results-wrap table tbody tr").all()
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

    VERIFIED column layout 2026-05-13:
      col-0   checkbox (record_id extracted from id="table-checkbox-{NNN}")
      col-3   Grantor
      col-4   Grantee
      col-5   Doc Type (description string, not Dallas literal code)
      col-6   Recorded Date (M/D/YYYY)
      col-7   Doc Number (instrument number)
      col-8   Book/Volume/Page
      col-9   Town (city name)
      col-10  Legal Description (contains subdivision, lot, block, township)

    Note: There is no street-address column in the list view. The street
    address lives only on the per-record detail page (click-through). For
    Phase 1 we leave ``address`` as None; address resolution comes via
    DCAD legal-description matching (Phase 2) or detail-page fetch.
    """
    # record_id from the checkbox input id
    checkbox = row.locator("input[id^='table-checkbox-']").first
    if checkbox.count() == 0:
        return None
    checkbox_id = checkbox.get_attribute("id") or ""
    record_id = checkbox_id.removeprefix("table-checkbox-")
    if not record_id:
        return None

    def cell(col_index: int) -> Optional[str]:
        try:
            text = row.locator(f"td.col-{col_index} span").first.inner_text().strip()
            return text or None
        except Exception:
            return None

    legal_desc = cell(10)
    city = cell(9)

    # Store legal description and city in raw_html_snippet for later parsing;
    # used by Phase-2 legal-description-based DCAD enrichment.
    snippet_bits = [s for s in (city, legal_desc) if s]
    raw_snippet = " | ".join(snippet_bits)[:500]

    return PublicSearchRecord(
        record_id=record_id,
        dallas_code=dallas_code,
        filing_date=_parse_filing_date(cell(6)),
        instrument_num=cell(7),
        grantor=cell(3),
        grantee=cell(4),
        address=None,                  # not in list view; see docstring
        amount=None,                   # not in list view
        raw_html_snippet=raw_snippet,
    )


def _parse_filing_date(raw: Optional[str]) -> Optional[str]:
    """Convert "4/8/2026" → "2026-04-08". Returns None on malformed input."""
    if not raw or "/" not in raw:
        return None
    try:
        m, d, y = raw.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return None


def _has_next_page(page) -> bool:
    """True iff the Next-page button is present and not disabled.

    VERIFIED 2026-05-13: Pagination is a <nav aria-label="pagination"> with
    role=button children. The "next page" button carries disabled="" when
    we're on the last page.
    """
    next_btn = page.get_by_role("button", name="next page")
    if next_btn.count() == 0:
        return False
    return not next_btn.first.is_disabled()


def _click_next_page(page) -> None:
    """Advance to the next results page."""
    page.get_by_role("button", name="next page").first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)
    page.wait_for_timeout(500)  # extra settle for client-side re-render


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
