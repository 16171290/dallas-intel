"""Unit tests for scraper.page_fetcher.

These tests use a mock Playwright Page to verify the fetcher logic
without spinning up a real browser. Live publicsearch.us behavior is
out of scope for unit testing (the SPA changes; that's what the
validation harness is for).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scraper.page_fetcher import (
    PROPERTY_ADDRESS_LABEL_RE,
    _clean_address_text,
    fetch_property_address,
)


class TestPropertyAddressLabelRegex:
    def test_matches_exact(self):
        assert PROPERTY_ADDRESS_LABEL_RE.match("Property Address")

    def test_matches_case_insensitive(self):
        assert PROPERTY_ADDRESS_LABEL_RE.match("PROPERTY ADDRESS")
        assert PROPERTY_ADDRESS_LABEL_RE.match("property address")

    def test_matches_extra_whitespace(self):
        assert PROPERTY_ADDRESS_LABEL_RE.match("  Property   Address  ")

    def test_rejects_substring(self):
        assert not PROPERTY_ADDRESS_LABEL_RE.match("My Property Address")


class TestCleanAddressText:
    def test_trims_whitespace(self):
        assert _clean_address_text("  3031 CREST RIDGE DR  ") == "3031 CREST RIDGE DR"

    def test_collapses_internal_whitespace(self):
        assert _clean_address_text("3031\n\n  CREST\t RIDGE   DR") == "3031 CREST RIDGE DR"

    def test_strips_bullet_markers(self):
        assert _clean_address_text("• 3031 CREST RIDGE DR") == "3031 CREST RIDGE DR"
        assert _clean_address_text("▸  100 OAK ST") == "100 OAK ST"

    def test_empty_returns_none(self):
        assert _clean_address_text("") is None
        assert _clean_address_text(None) is None
        assert _clean_address_text("   ") is None


class TestFetchPropertyAddress:
    def _mock_page_with_label(self, label_text="Property Address",
                              address_text="3031 CREST RIDGE DRIVE DALLAS TX 75228",
                              full_text=None):
        """Build a MagicMock Playwright Page whose label-relative lookup
        returns ``address_text``."""
        page = MagicMock()
        page.goto = MagicMock(return_value=None)

        label_locator = MagicMock()
        address_dd = MagicMock()
        address_dd.inner_text = MagicMock(return_value=address_text)
        label_locator.locator = MagicMock(return_value=MagicMock(first=address_dd))

        get_by_text_result = MagicMock()
        get_by_text_result.first = label_locator
        page.get_by_text = MagicMock(return_value=get_by_text_result)

        if full_text is not None:
            page.inner_text = MagicMock(return_value=full_text)

        return page

    def test_label_relative_match_returns_address(self):
        page = self._mock_page_with_label(
            address_text="3031 CREST RIDGE DRIVE DALLAS TX 75228",
        )
        result = fetch_property_address("315561585", page, base_url="https://example.com")
        assert result == "3031 CREST RIDGE DRIVE DALLAS TX 75228"

    def test_address_text_is_cleaned(self):
        page = self._mock_page_with_label(
            address_text="  3031 CREST RIDGE DRIVE \n\n   DALLAS TX 75228  ",
        )
        result = fetch_property_address("r1", page, base_url="https://example.com")
        assert result == "3031 CREST RIDGE DRIVE DALLAS TX 75228"

    def test_goto_failure_returns_none(self):
        page = MagicMock()
        page.goto = MagicMock(side_effect=RuntimeError("network error"))
        result = fetch_property_address("r1", page, base_url="https://example.com")
        assert result is None

    def test_label_lookup_failure_falls_back_to_full_text(self):
        page = MagicMock()
        page.goto = MagicMock(return_value=None)
        # Make get_by_text raise so we fall through to inner_text
        page.get_by_text = MagicMock(side_effect=Exception("not found"))
        page.inner_text = MagicMock(return_value=(
            "Document Number: 202600001062\n"
            "Sale Date: 07/07/2026\n"
            "\n"
            "Property Address\n"
            "3031 CREST RIDGE DRIVE DALLAS TX 75228\n"
            "\n"
            "Some other content."
        ))
        result = fetch_property_address("r1", page, base_url="https://example.com")
        assert result is not None
        assert "CREST RIDGE" in result

    def test_both_strategies_fail_returns_none(self):
        page = MagicMock()
        page.goto = MagicMock(return_value=None)
        page.get_by_text = MagicMock(side_effect=Exception("nope"))
        page.inner_text = MagicMock(return_value="No address here at all.")
        result = fetch_property_address("r1", page, base_url="https://example.com")
        assert result is None
