"""Tests for the publicsearch.us Foreclosures-department scraper.

Browser-driven scrape paths require Playwright and a live SPA; those are
covered by integration smoke runs, not here. This file covers the pure
deterministic bits: URL construction and date-cell parsing.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.foreclosures_ps import (
    ForeclosureNoticePSRecord,
    _build_results_url,
    _parse_mdY_to_iso,
)
from scraper.enrichment import canonicalize_foreclosure_notice_ps


# ============================================================================
# URL builder
# ============================================================================

def test_url_builder_param_set_matches_advanced_search_form():
    """Verifies the URL builder produces every param the live Advanced Search
    page included on 2026-05-21 + the pagination/sort params we added."""
    url = _build_results_url(
        recorded_from=date(2026, 5, 7),
        recorded_to=date(2026, 5, 21),
        sale_from=date(2026, 4, 7),
        sale_to=date(2026, 10, 6),
        offset=0,
    )
    params = parse_qs(urlparse(url).query)
    assert params["department"] == ["FC"]
    assert params["recordedDateRange"]   == ["20260507,20260521"]
    assert params["instrumentDateRange"] == ["20260407,20261006"]  # sale range, mislabeled by publicsearch
    assert params["searchType"] == ["advancedSearch"]
    assert params["limit"]  == ["50"]
    assert params["offset"] == ["0"]
    assert params["sort"]   == ["desc"]
    assert params["sortBy"] == ["recordedDate"]


def test_url_builder_increments_offset():
    """Offset must round-trip through urlencode and be readable as a string."""
    url = _build_results_url(
        recorded_from=date(2026, 1, 1),
        recorded_to=date(2026, 1, 31),
        sale_from=date(2026, 2, 1),
        sale_to=date(2026, 7, 31),
        offset=150,
    )
    assert "offset=150" in url


def test_url_builder_uses_yyyymmdd_format_no_separators():
    """Date format inside the param values must be YYYYMMDD with no dashes."""
    url = _build_results_url(
        recorded_from=date(2026, 5, 7),
        recorded_to=date(2026, 5, 21),
        sale_from=date(2026, 5, 21),
        sale_to=date(2026, 11, 17),
        offset=0,
    )
    # The comma between dates becomes %2C after urlencoding, but the digits
    # should appear directly in the URL.
    assert "20260507" in url
    assert "20260521" in url
    assert "20261117" in url
    # No spurious separators inside a single date.
    assert "2026-05-07" not in url
    assert "2026%2D" not in url


# ============================================================================
# Date-cell parsing (M/D/YYYY -> ISO)
# ============================================================================

def test_parse_mdY_typical():
    assert _parse_mdY_to_iso("5/7/2026")  == "2026-05-07"
    assert _parse_mdY_to_iso("12/31/2025") == "2025-12-31"


def test_parse_mdY_zero_padded():
    assert _parse_mdY_to_iso("05/07/2026") == "2026-05-07"


def test_parse_mdY_none_on_garbage():
    assert _parse_mdY_to_iso(None) is None
    assert _parse_mdY_to_iso("")   is None
    assert _parse_mdY_to_iso("no-slashes") is None
    assert _parse_mdY_to_iso("bad/input/here") is None


# ============================================================================
# Canonicalization
# ============================================================================

def test_canonicalize_emits_nof_notice_with_source_url():
    rec = ForeclosureNoticePSRecord(
        record_id="315470230",
        recorded_date="2026-05-07",
        sale_date="2026-06-02",
        doc_number="202600000830",
        property_address="3017 GRAYSTONE COURT, DALLAS, TX",
    )
    out = canonicalize_foreclosure_notice_ps(rec)
    assert out["record_id"]      == "315470230"
    assert out["source"]         == "publicsearch.us"
    assert out["dallas_code"]    == "NOF"
    assert out["category"]       == "NOTICE"
    assert out["filing_date"]    == "2026-05-07"
    assert out["sale_date"]      == "2026-06-02"
    assert out["instrument_num"] == "202600000830"
    assert out["address"]        == "3017 GRAYSTONE COURT, DALLAS, TX"
    assert out["source_url"]     == "https://dallas.tx.publicsearch.us/doc/315470230"
    # Address gets normalized
    assert out["address_normalized"] is not None
    # New schema fields default to None until DCAD enrichment runs
    assert out["dcad_mailing_address"] is None
    assert out["dcad_account"]         is None
    # Backward-compat fields present
    assert out["signal_metadata"] is None
    assert out["active"] is True


def test_canonicalize_handles_empty_record_id():
    rec = ForeclosureNoticePSRecord(record_id="")
    out = canonicalize_foreclosure_notice_ps(rec)
    assert out["source_url"] is None
    assert out["record_id"]  == ""
