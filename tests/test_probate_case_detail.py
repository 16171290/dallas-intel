"""Unit tests for scraper.probate_case_detail — PR 8 Phase 2."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scraper.probate_case_detail import (
    CaseDetailResult,
    case_has_inventory_filing,
    fetch_case_detail,
    parse_property_addresses_from_text,
)


# ═══════════════════════════════════════════════════════════════════════════
# Address extraction
# ═══════════════════════════════════════════════════════════════════════════


class TestParsePropertyAddresses:
    def test_extracts_basic_dallas_address(self):
        text = "Property listed: 1234 Main Street, Dallas, TX 75201"
        result = parse_property_addresses_from_text(text)
        assert len(result) == 1
        assert "1234 Main Street" in result[0]
        assert "Dallas" in result[0]
        assert "75201" in result[0]

    def test_extracts_multiple_addresses(self):
        text = """
        Inventory includes:
        - 4501 Oak Lawn Avenue, Dallas, TX 75219
        - 9001 Forest Lane, Dallas, Texas 75231
        """
        result = parse_property_addresses_from_text(text)
        assert len(result) == 2

    def test_deduplicates(self):
        text = """
        First mention: 100 Main St, Dallas, TX 75201
        Second mention: 100 Main St, Dallas, TX 75201
        """
        result = parse_property_addresses_from_text(text)
        assert len(result) == 1

    def test_rejects_houston_address(self):
        """Houston is 77XXX — not Dallas/Tarrant County."""
        text = "Property: 1500 McKinney Street, Houston, TX 77010"
        result = parse_property_addresses_from_text(text)
        assert result == []

    def test_rejects_el_paso(self):
        text = "Trustee mailing: 7730 Market Center Ave, El Paso, TX 79912"
        result = parse_property_addresses_from_text(text)
        assert result == []

    def test_accepts_tarrant_county(self):
        """Fort Worth = 76XXX, valid for this filter."""
        text = "Property: 1234 Camp Bowie Boulevard, Fort Worth, TX 76107"
        result = parse_property_addresses_from_text(text)
        assert len(result) == 1
        assert "76107" in result[0]

    def test_rejects_venue_trustee_addresses(self):
        """The courthouse / sale-venue address must be filtered out."""
        text = "Sale will occur at 600 Commerce Street, Dallas, TX 75202"
        result = parse_property_addresses_from_text(text)
        assert result == []

    def test_empty_input(self):
        assert parse_property_addresses_from_text("") == []
        assert parse_property_addresses_from_text(None) == []

    def test_no_address_in_text(self):
        text = "This is just some text without any addresses in it at all."
        assert parse_property_addresses_from_text(text) == []

    def test_case_insensitive_state_marker(self):
        text = "Property: 100 Oak St, Dallas, texas 75201"
        result = parse_property_addresses_from_text(text)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Inventory detection
# ═══════════════════════════════════════════════════════════════════════════


class TestInventoryDetection:
    def test_detects_inventory_and_appraisement(self):
        text = "Filing: INVENTORY, APPRAISEMENT, AND LIST OF CLAIMS"
        assert case_has_inventory_filing(text) is True

    def test_detects_inventory_and_appraisement_short(self):
        text = "Inventory and Appraisement filed on 2026-03-15"
        assert case_has_inventory_filing(text) is True

    def test_does_not_detect_unrelated_filing(self):
        text = "Application for Probate of Will and Issuance of Letters Testamentary"
        assert case_has_inventory_filing(text) is False

    def test_empty_input(self):
        assert case_has_inventory_filing("") is False
        assert case_has_inventory_filing(None) is False


# ═══════════════════════════════════════════════════════════════════════════
# fetch_case_detail (mocked Playwright)
# ═══════════════════════════════════════════════════════════════════════════


class TestFetchCaseDetail:
    def _make_mock_page(self, text: str, raise_goto: bool = False,
                        raise_text: bool = False):
        page = MagicMock()
        context = MagicMock()
        page.context = context
        if raise_goto:
            page.goto = MagicMock(side_effect=RuntimeError("nav failed"))
        else:
            page.goto = MagicMock(return_value=None)
        page.wait_for_timeout = MagicMock(return_value=None)
        if raise_text:
            page.inner_text = MagicMock(side_effect=RuntimeError("no body"))
        else:
            page.inner_text = MagicMock(return_value=text)
        page.content = MagicMock(return_value=f"<html>{text}</html>")
        return page

    def test_successful_fetch_with_address(self):
        page_text = """
        Case Details: PR-26-12345
        Filings:
        - 2026-03-15  Inventory and Appraisement
        Property: 1234 Main Street, Dallas, TX 75201
        """
        page = self._make_mock_page(page_text)
        result = fetch_case_detail("uuid-abc123", {"cookie1": "value1"},
                                   page=page)
        assert result.status == "ok"
        assert result.has_inventory is True
        assert len(result.addresses_found) == 1
        assert "1234 Main Street" in result.addresses_found[0]
        # Navigation hit the right URL
        assert page.goto.called
        url = page.goto.call_args[0][0]
        assert "uuid-abc123/details" in url

    def test_cookie_injection(self):
        page = self._make_mock_page("Sample case page text")
        fetch_case_detail("case-1", {"sess_id": "abc", "csrf_token": "xyz"},
                          page=page)
        page.context.add_cookies.assert_called_once()
        cookie_list = page.context.add_cookies.call_args[0][0]
        names = {c["name"] for c in cookie_list}
        assert names == {"sess_id", "csrf_token"}
        # Domain set so cookies actually attach
        assert all(c["domain"] == ".txcourts.gov" for c in cookie_list)

    def test_goto_failure_returns_error_status(self):
        page = self._make_mock_page("", raise_goto=True)
        result = fetch_case_detail("case-fail", {}, page=page)
        assert result.status == "error"
        assert "nav failed" in result.error

    def test_text_extraction_failure_returns_error_status(self):
        page = self._make_mock_page("", raise_text=True)
        result = fetch_case_detail("case-no-body", {}, page=page)
        assert result.status == "error"
        assert "no body" in result.error

    def test_no_filings_when_page_empty(self):
        """Page rendered but no filing entries, no addresses → no_filings."""
        page = self._make_mock_page(
            "This case has been administratively closed pending review."
        )
        result = fetch_case_detail("c1", {}, page=page)
        assert result.status == "no_filings"
        assert result.addresses_found == []

    def test_inventory_filing_without_inline_address(self):
        """Common case: I&A filing visible in list, but property text
        lives in the PDF not on the page. We return inventory=True
        with empty addresses → operator should review the PDF."""
        page_text = """
        Case: PR-26-12345
        Filings:
        - 2026-03-15  Inventory, Appraisement, and List of Claims (filed)
        """
        page = self._make_mock_page(page_text)
        result = fetch_case_detail("c-inv", {}, page=page)
        assert result.status == "ok"
        assert result.has_inventory is True
        assert result.addresses_found == []

    def test_writes_dump_when_dir_provided(self, tmp_path):
        page = self._make_mock_page("dummy page text")
        fetch_case_detail("dump-case", {}, page=page, dump_dir=tmp_path)
        # Text file written
        assert (tmp_path / "dump-case.txt").exists()
        # HTML file written
        assert (tmp_path / "dump-case.html").exists()
