"""publicsearch.us /doc/{record_id} Summary-panel scraper for Stage 6.6.

Per the user's clarification (2026-05-27): the publicsearch document
page's Summary panel carries a "Property Address" field. Most entries
are partial (street name only, or just a city) but the partial text is
enough to break a tie between known candidates in Stage 6.6.

This module exposes a single function for fetching that text, and a
``PageFetcher`` class that batches Playwright session setup so multiple
record_ids can be processed in one browser context.

Design notes:
  - Reuses the same Playwright pattern as ``scraper.foreclosure_ocr``
    (same SPA, same base URL, same hydration timeouts).
  - The Summary panel is rendered after the SPA hydrates; we wait for
    the "Property Address" label text to appear.
  - Returns ``None`` (not an empty string) when the field is genuinely
    absent — Stage 6.6 then treats the page as inconclusive.
  - The fetcher is fault-tolerant: any Playwright error returns None
    so a single network blip doesn't stop the daily run.

Public API:
  fetch_property_address(record_id, page, base_url=None) -> Optional[str]
  PageFetcher() — context manager that wraps Playwright lifecycle

The lazy-init pattern in PageFetcher means Stage 6.6 only spins up
Playwright when there's actually a disagreement to resolve.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Selectors / locators (verified 2026-05-27 against publicsearch.us SPA)
# ─────────────────────────────────────────────────────────────────────────

# The Summary panel renders a series of <dl> / <dt> / <dd> pairs. The
# Property Address dt has visible text "Property Address"; the
# corresponding dd holds the address text (often a clickable link).
#
# We use Playwright's get_by_text + locator chaining rather than CSS so
# the lookup survives SPA reshuffles of the underlying DOM.
PROPERTY_ADDRESS_LABEL_RE = re.compile(r"^\s*Property\s+Address\s*$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────
# Single-shot fetcher
# ─────────────────────────────────────────────────────────────────────────


def fetch_property_address(
    record_id: str,
    page,
    *,
    base_url: Optional[str] = None,
    timeout_ms: int = 20_000,
) -> Optional[str]:
    """Visit /doc/{record_id}; return the Summary panel's Property Address
    text, or None if not found / fetch failed.

    Args:
        record_id: publicsearch record id.
        page: a live Playwright Page object.
        base_url: defaults to ``config.PUBLICSEARCH_BASE``.
        timeout_ms: per-navigation timeout in milliseconds.

    Returns:
        The address text (typically all-caps, like
        ``"3031 CREST RIDGE DRIVE DALLAS TEXAS 75228"``), or None.
    """
    base_url = base_url or config.PUBLICSEARCH_BASE
    url = f"{base_url}/doc/{record_id}"

    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except Exception as e:
        logger.debug("page_fetcher: goto failed for %s: %s", url, e)
        return None

    # Strategy 1: locate "Property Address" label, then read the next dd.
    # Playwright's get_by_role / get_by_text gives us a label-relative anchor.
    try:
        label = page.get_by_text(PROPERTY_ADDRESS_LABEL_RE).first
        # Read the text content of the next element (the dd / address).
        # XPath ./following-sibling is robust to whitespace nodes.
        address_dd = label.locator("xpath=following-sibling::*[1]").first
        text = address_dd.inner_text(timeout=3_000).strip()
        if text:
            return _clean_address_text(text)
    except Exception as e:
        logger.debug("page_fetcher: label-relative lookup failed: %s", e)

    # Strategy 2: fall back to scanning the Summary section for any
    # element whose preceding sibling text matches "Property Address".
    try:
        all_text = page.inner_text("body", timeout=3_000)
        m = re.search(
            r"Property\s+Address\s*\n+\s*(.+?)(?:\n\n|\Z)",
            all_text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            return _clean_address_text(m.group(1))
    except Exception as e:
        logger.debug("page_fetcher: full-text fallback failed: %s", e)

    return None


def _clean_address_text(raw: str) -> Optional[str]:
    """Trim whitespace; collapse internal whitespace; drop leading
    bullets / link markers. Returns None when nothing meaningful remains."""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"\s+", " ", s)
    # Drop common SPA artifacts ("\n   ", trailing arrows, etc.)
    s = s.lstrip("•·▸>•‣▸").strip()
    return s or None


# ─────────────────────────────────────────────────────────────────────────
# Batched fetcher (lazy Playwright init)
# ─────────────────────────────────────────────────────────────────────────


class PageFetcher:
    """Callable wrapper that lazy-initializes a Playwright browser/page
    and reuses it across multiple record_id lookups.

    Use as a context manager so the browser is properly closed::

        with PageFetcher() as fetcher:
            stage_6_6_agreement.run_stage_6_6(records, acct_to_addr, fetcher)

    The fetcher is a callable ``record_id -> Optional[str]`` so
    ``run_stage_6_6`` can accept it directly.
    """

    def __init__(self, *, base_url: Optional[str] = None, headless: bool = True):
        self._base_url = base_url or config.PUBLICSEARCH_BASE
        self._headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __call__(self, record_id: str) -> Optional[str]:
        page = self._ensure_page()
        if page is None:
            return None
        return fetch_property_address(record_id, page, base_url=self._base_url)

    def _ensure_page(self):
        """Spin up Playwright on first call. Returns None on failure."""
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
            self._page = self._browser.new_page()
            return self._page
        except Exception as e:
            logger.warning("PageFetcher: Playwright init failed: %s", e)
            self._page = None
            return None

    def close(self):
        try:
            if self._page is not None:
                self._page.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._page = self._browser = self._pw = None
