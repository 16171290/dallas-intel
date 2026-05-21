"""
publicsearch.us Foreclosures-department scraper.

Replaces the deprecated dallascounty.org PDF feed. As of 2026-02-24, Dallas
County redirected foreclosure-notice filings to publicsearch.us under the
Foreclosures department (department=FC). Each notice points to an upcoming
trustee's sale — the people behind those filings are the most motivated
sellers in the market: a substitute-trustee sale is typically 3-8 weeks
out, and the homeowner can avoid the auction (and the destruction of
their credit) with a quick private sale.

Architecturally a sibling of publicsearch.py:
  - Same SPA / same vendor (publicsearch.us)
  - Different department (FC vs RP)
  - Single doctype (NOTICE OF FORECLOSURE)
  - Different result-row column layout
  - Direct query-string URL (no Advanced Search button flow needed)

URL convention (verified 2026-05-21 against the live Advanced Search
results page):

    https://dallas.tx.publicsearch.us/results
      ?department=FC
      &recordedDateRange=YYYYMMDD,YYYYMMDD   (when filed)
      &instrumentDateRange=YYYYMMDD,YYYYMMDD (sale-date range; mislabeled param)
      &searchType=advancedSearch
      &limit=50
      &offset=N

NOTE: publicsearch.us's URL parameter naming is inconsistent. The Advanced
Search form labels the second field "Sale Date Range" but the URL param is
named `instrumentDateRange`. We treat it semantically as the sale-date
range — that's what the form actually maps to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

from . import config
from .publicsearch import (
    CircuitBreaker,
    CircuitBreakerTripped,
    browser_context,
    _polite_delay,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Result record
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ForeclosureNoticePSRecord:
    """One Notice-of-Foreclosure row scraped from publicsearch.us."""
    record_id: str                              # vendor's document id
    recorded_date: Optional[str] = None         # ISO YYYY-MM-DD (when filed)
    sale_date: Optional[str] = None             # ISO YYYY-MM-DD (auction date)
    doc_number: Optional[str] = None            # instrument number
    property_address: Optional[str] = None      # raw, full or city-only
    raw_html_snippet: str = ""
    parse_warnings: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def scrape_foreclosures(
    recorded_lookback_days: int = 7,
    sale_lookahead_days: int = 180,
    today: Optional[date] = None,
) -> list[ForeclosureNoticePSRecord]:
    """Scrape Notice-of-Foreclosure records from publicsearch.us Foreclosures.

    Args:
        recorded_lookback_days: pull notices recorded within the last N days.
            Default 7 — matches the daily-window cadence elsewhere in the
            pipeline.
        sale_lookahead_days: pull notices whose auction date falls within
            the next N days. Default 180 — captures ~6 first-Tuesday auction
            batches, which covers the operator's likely outreach window.
        today: explicit reference date (for deterministic tests). Defaults
            to ``date.today()``.

    Returns:
        List of records, deduplicated by record_id. Empty list on
        zero-results or all retries failing.
    """
    ref_today = today or date.today()
    recorded_from = ref_today - timedelta(days=recorded_lookback_days)
    recorded_to   = ref_today
    sale_from     = ref_today
    sale_to       = ref_today + timedelta(days=sale_lookahead_days)

    logger.info(
        "publicsearch.us foreclosures: recorded %s..%s, sale %s..%s",
        recorded_from, recorded_to, sale_from, sale_to,
    )

    breaker = CircuitBreaker()
    seen_ids: set[str] = set()
    out: list[ForeclosureNoticePSRecord] = []

    with browser_context() as (_browser, _context, page):
        offset = 0
        while True:
            url = _build_results_url(
                recorded_from=recorded_from,
                recorded_to=recorded_to,
                sale_from=sale_from,
                sale_to=sale_to,
                offset=offset,
                limit=50,
            )

            _polite_delay("publicsearch.us")

            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
                page_records = _harvest_current_page(page)
                breaker.record_success()
            except CircuitBreakerTripped:
                raise
            except Exception as e:
                logger.warning(
                    "publicsearch foreclosures: page fetch failed at offset=%d: %s",
                    offset, e,
                )
                breaker.record_failure()
                break

            if not page_records:
                logger.debug("publicsearch foreclosures: empty page at offset=%d; stopping", offset)
                break

            new_count = 0
            for rec in page_records:
                if rec.record_id in seen_ids:
                    continue
                seen_ids.add(rec.record_id)
                out.append(rec)
                new_count += 1

            logger.debug(
                "publicsearch foreclosures: offset=%d, harvested=%d (new=%d, total=%d)",
                offset, len(page_records), new_count, len(out),
            )

            # Stop conditions: page returned fewer than full (limit) results,
            # OR no new records on this page (defensive against repeated pages).
            if len(page_records) < 50 or new_count == 0:
                break

            offset += 50

    logger.info(
        "publicsearch foreclosures: scrape complete, %d unique notices",
        len(out),
    )
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Internals
# ═══════════════════════════════════════════════════════════════════════════

def _build_results_url(
    *,
    recorded_from: date,
    recorded_to: date,
    sale_from: date,
    sale_to: date,
    offset: int,
    limit: int = 50,
) -> str:
    """Build the publicsearch.us Foreclosures Advanced Search results URL.

    Date format is YYYYMMDD (no separators), comma-joined for ranges.
    urlencode handles the comma encoding as %2C.
    """
    def yyyymmdd(d: date) -> str:
        return d.strftime("%Y%m%d")

    params = {
        "department":          "FC",
        "recordedDateRange":   f"{yyyymmdd(recorded_from)},{yyyymmdd(recorded_to)}",
        "instrumentDateRange": f"{yyyymmdd(sale_from)},{yyyymmdd(sale_to)}",
        "searchType":          "advancedSearch",
        "limit":               str(limit),
        "offset":              str(offset),
        "sort":                "desc",
        "sortBy":              "recordedDate",
    }
    return f"{config.PUBLICSEARCH_BASE}/results?{urlencode(params)}"


# One-shot probe: logs the first row's HTML on the first call so we can
# verify the col-N mapping matches reality. Removed once the layout is
# confirmed stable across runs.
_PROBED_ROW_LAYOUT = False


def _harvest_current_page(page) -> list[ForeclosureNoticePSRecord]:
    """Extract one page of Notice-of-Foreclosure rows.

    Result-table structure (per 2026-05-21 inspection of the live page):
      col-0   checkbox (record_id extracted from id="table-checkbox-{NNN}")
      col-1   menu/action button (skip)
      col-2   menu/action button (skip)
      col-3   DOC TYPE (always "NOTICE OF FORECLOSURE" given our filter)
      col-4   RECORDED DATE (M/D/YYYY)
      col-5   SALE DATE (M/D/YYYY)
      col-6   DOC NUMBER (instrument number)
      col-7   PROPERTY ADDRESS (full street when known; falls back to city)
    """
    global _PROBED_ROW_LAYOUT

    rows = page.locator("section.search-results__results-wrap table tbody tr").all()
    records: list[ForeclosureNoticePSRecord] = []

    if rows and not _PROBED_ROW_LAYOUT:
        _probe_row_layout(rows[0])
        _PROBED_ROW_LAYOUT = True

    for row in rows:
        try:
            rec = _parse_result_row(row)
            if rec:
                records.append(rec)
        except Exception as e:
            logger.debug("publicsearch foreclosures row parse failed: %s", e)

    return records


def _probe_row_layout(row) -> None:
    """One-shot defensive probe: log first row's HTML so col-N mapping
    can be verified post-hoc. Cheap insurance against silent breakage if
    publicsearch.us renumbers their columns."""
    try:
        html = row.evaluate("el => el.outerHTML") or ""
        logger.info(
            "FORECLOSURES ROW PROBE: first-row html (first 2000 chars): %s",
            html[:2000],
        )
    except Exception as e:
        logger.info("FORECLOSURES ROW PROBE failed: %s", e)


def _parse_result_row(row) -> Optional[ForeclosureNoticePSRecord]:
    """Extract fields from a single Foreclosures result-table row."""
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

    recorded_raw = cell(4)
    sale_raw     = cell(5)
    doc_number   = cell(6)
    address      = cell(7)

    snippet_bits = [s for s in (cell(3), address) if s]  # doctype + address
    raw_snippet = " | ".join(snippet_bits)[:500]

    return ForeclosureNoticePSRecord(
        record_id=record_id,
        recorded_date=_parse_mdY_to_iso(recorded_raw),
        sale_date=_parse_mdY_to_iso(sale_raw),
        doc_number=doc_number,
        property_address=address,
        raw_html_snippet=raw_snippet,
    )


def _parse_mdY_to_iso(raw: Optional[str]) -> Optional[str]:
    """Convert "5/7/2026" -> "2026-05-07". Returns None on malformed input."""
    if not raw or "/" not in raw:
        return None
    try:
        m, d, y = raw.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return None
