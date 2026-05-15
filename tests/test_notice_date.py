"""Tests for notice-date extraction in scraper.foreclosure_pdfs.

These tests use VERBATIM text strings observed in the 98-PDF validator
survey (2026-05-15). Each test that exercises a positive pattern uses
a string copied directly from a real PDF.

The single confirmed false positive from validation (``Dallas_4 (2).pdf``,
where ``deed.of trust:`` with a middle-dot bypassed the rejection rule)
is exercised explicitly so we never silently regress.
"""

from __future__ import annotations

import pytest

from scraper import foreclosure_pdfs as fp


# ============================================================================
# _normalize_for_match
# ============================================================================


class TestNormalizeForMatch:
    def test_collapses_normal_whitespace(self):
        assert fp._normalize_for_match("deed of trust") == "deed of trust"

    def test_collapses_double_spaces(self):
        assert fp._normalize_for_match("deed  of   trust") == "deed of trust"

    def test_collapses_newlines(self):
        assert fp._normalize_for_match("deed\nof\ntrust") == "deed of trust"

    def test_collapses_middle_dot(self):
        # The exact pdfplumber artifact that caused the false positive.
        assert fp._normalize_for_match("deed\u00b7of trust") == "deed of trust"

    def test_collapses_mixed_punctuation(self):
        # PDF text extraction noise can produce all sorts of cruft.
        assert fp._normalize_for_match("deed-of/trust") == "deed of trust"

    def test_lowercases(self):
        assert fp._normalize_for_match("DEED OF TRUST") == "deed of trust"

    def test_empty(self):
        assert fp._normalize_for_match("") == ""
        assert fp._normalize_for_match(None) == ""


# ============================================================================
# _notice_date_to_iso
# ============================================================================


class TestNoticeDateToIso:
    def test_slash_format_4_digit_year(self):
        assert fp._notice_date_to_iso("1/12/2026") == "2026-01-12"
        assert fp._notice_date_to_iso("01/12/2026") == "2026-01-12"

    def test_slash_format_2_digit_year(self):
        # Two-digit year < 50 -> 2000s
        assert fp._notice_date_to_iso("02/19/26") == "2026-02-19"
        assert fp._notice_date_to_iso("12/30/25") == "2025-12-30"

    def test_dash_format(self):
        assert fp._notice_date_to_iso("12-30-25") == "2025-12-30"
        assert fp._notice_date_to_iso("1-12-2026") == "2026-01-12"

    def test_month_name_format(self):
        assert fp._notice_date_to_iso("January 28, 2026") == "2026-01-28"
        assert fp._notice_date_to_iso("February 9, 2026") == "2026-02-09"

    def test_xth_day_of_month(self):
        assert fp._notice_date_to_iso("6th day of February, 2026") == "2026-02-06"
        assert fp._notice_date_to_iso("6th day of FEBRUARY 2026") == "2026-02-06"

    def test_invalid_returns_none(self):
        assert fp._notice_date_to_iso("not a date") is None
        assert fp._notice_date_to_iso("Febrnary 1, 2022") is None  # OCR typo
        assert fp._notice_date_to_iso("") is None
        assert fp._notice_date_to_iso(None) is None

    def test_impossible_dates_return_none(self):
        # February 30 doesn't exist.
        assert fp._notice_date_to_iso("02/30/2026") is None
        assert fp._notice_date_to_iso("February 30, 2026") is None


# ============================================================================
# _has_notice_date_reject_context
# ============================================================================


class TestHasRejectContext:
    def test_deed_of_trust_normal_spaces(self):
        text = "Deed of Trust: Dated: 1/12/2026"
        # Position is the "1" in "1/12/2026"
        pos = text.index("1/12/2026")
        assert fp._has_notice_date_reject_context(text, pos) == "deed of trust"

    def test_deed_of_trust_middle_dot(self):
        # The exact artifact that broke the validator's rejection.
        text = "NOTICE OF FORECLOSURE SALE\nDeed\u00b7of Trust:\nDated: February 5, 2025"
        pos = text.index("February 5, 2025")
        result = fp._has_notice_date_reject_context(text, pos)
        assert result is not None
        assert result == "deed of trust"

    def test_whereas_rejection(self):
        text = "WHEREAS, on May 22, 2006, MARIA MIRELES ... executed a Deed of Trust"
        pos = text.index("May 22, 2006")
        assert fp._has_notice_date_reject_context(text, pos) is not None

    def test_sale_context_rejection(self):
        text = "Date of Sale: March 3, 2026"
        pos = text.index("March 3, 2026")
        assert fp._has_notice_date_reject_context(text, pos) is not None

    def test_notice_is_hereby_given_rejection(self):
        text = "NOTICE IS HEREBY GIVEN that on March 3, 2026"
        pos = text.index("March 3, 2026")
        assert fp._has_notice_date_reject_context(text, pos) is not None

    def test_clean_context_no_rejection(self):
        # Far-away rejection phrases are out of window.
        text = "A" * 500 + "Dated: 1/12/2026"
        # The rejection-context check only looks at the 200 chars before
        # the position. 500 As in front means no reject phrase nearby.
        pos = text.index("1/12/2026")
        assert fp._has_notice_date_reject_context(text, pos) is None


# ============================================================================
# _extract_notice_date - the integration of patterns + rejection
# ============================================================================


class TestExtractNoticeDate:
    def test_p1_declarant_simple(self):
        # Verbatim shape from Lancaster_2.pdf survey output.
        text = (
            "Suite 100, Addison, Texas 75001-4320. I declare under penalty "
            "of perjury that on 1/29/26 I filed at the office of the "
            "DALLAS County Clerk and caused to be posted at the courthouse"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso == "2026-01-29"
        assert pattern == "P1_DECLARANT"

    def test_p2_dated_simple(self):
        # Verbatim shape from Other_1.pdf survey output (McCarthy template).
        text = (
            "SENDER OF THIS NOTICE IMMEDIATELY.\nDated: 12/30/2025\n"
            "Thuy Frazier, Attorney Substitute Trustee\n"
            "McCarthy & Holthus, LLP"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso == "2025-12-30"
        assert pattern == "P2_DATED"

    def test_p3_witness_uppercase(self):
        # Verbatim from Richardson_4.pdf.
        text = (
            "military service to the sender of this notice immediately.\n"
            "WITNESS MY HAND this 6th day of FEBRUARY 2026.\n"
            "David Garvin, Jeff Benton, Brandy Bacon"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso == "2026-02-06"
        assert pattern == "P3_WITNESS"

    def test_p3_witness_mixed_case(self):
        text = "WITNESS my hand this 9th day of February, 2026"
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso == "2026-02-09"
        assert pattern == "P3_WITNESS"

    def test_p4_date_label(self):
        # Verbatim from Farmers-Branch_1.pdf.
        text = (
            "AGENT OF THE MORTGAGEE OR MORTGAGE SERVICER.\n"
            "MatterNo.: 141993-TX\n"
            "Date: January 2, 2026\n"
            "County where Real Property is Located: Dallas"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso == "2026-01-02"
        assert pattern == "P4_DATE_LABEL"

    def test_rejects_whereas_clause(self):
        # The MIRELES record's misleading 2006 date.
        text = (
            "WHEREAS, on May 22, 2006, MARIA MIRELES, A SINGLE PERSON AND "
            "ERICA ANTILLON, A SINGLE PERSON, as Grantor(s), executed a "
            "Deed of Trust"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso is None
        assert pattern is None

    def test_rejects_deed_of_trust_dated(self):
        # The Carrollton_4.pdf case: two Dated: lines, only the first should be considered.
        # And even if both were considered, the Deed-of-Trust one should reject.
        text = (
            "Trustee signs the notice.\nDated: 02/09/2026\n"
            "Some narrative paragraph.\n"
            "Deed of Trust dated July 25, 2022\nWhich is irrelevant"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        # First Dated: should win.
        assert iso == "2026-02-09"

    def test_rejects_deed_dot_of_trust_artifact(self):
        # The Dallas_4 (2) false positive case.
        # The validator (with the unnormalized rejection) incorrectly
        # accepted "Dated: February 5, 2025" here. With the fix, it
        # must be rejected.
        text = (
            "Some preamble text.\n"
            "NOTICE OF FORECLOSURE SALE\n"
            "Deed\u00b7of Trust:\n"
            "Dated: February 5, 2025\n"
            "Grantor: F.A.N. l RE HOLDINGS LLC"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso is None, (
            "Should reject - 'Deed.of Trust:' (with middle-dot) "
            "is the rejection context"
        )

    def test_rejects_sale_date_context(self):
        text = (
            "The auction is scheduled.\n"
            "Date of Sale: 3/3/2026\n"
            "Place of Sale: Dallas County Courthouse"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        # Both dates should be rejected (sale context).
        # Note: P4_DATE_LABEL might still try to match "Date: ..." but
        # rejection context "date of sale" catches it.
        assert iso is None

    def test_p1_outranks_p2_when_both_present(self):
        text = (
            "Dated: 1/05/2026 (this would be a trustee signing date)\n"
            "Long body text here...\n"
            "I declare under penalty of perjury that on 1/29/2026 I filed "
            "at the office of the DALLAS County Clerk"
        )
        iso, raw, pattern = fp._extract_notice_date(text)
        # P1 (declarant) is higher confidence than P2 (dated).
        assert iso == "2026-01-29"
        assert pattern == "P1_DECLARANT"

    def test_empty_input(self):
        iso, raw, pattern = fp._extract_notice_date("")
        assert iso is None
        assert raw is None
        assert pattern is None

    def test_no_date_in_text(self):
        text = "This document contains no date-shaped strings at all."
        iso, raw, pattern = fp._extract_notice_date(text)
        assert iso is None


# ============================================================================
# Integration via _parse_fields_into_record
# ============================================================================


class TestParseFieldsIntegration:
    def test_notice_date_populates_on_record(self):
        block = (
            "Trustee signed.\nDated: 1/12/2026\n"
            "Property address: 5605 EVERGLADE ROAD"
        )
        rec = fp.ForeclosureRecord(source_pdf="test.pdf")
        fp._parse_fields_into_record(rec, block)
        assert rec.notice_date_iso == "2026-01-12"
        assert rec.notice_date_pattern == "P2_DATED"

    def test_notice_date_none_when_unextractable(self):
        block = (
            "WHEREAS, on May 22, 2006, debtor signed a Deed of Trust.\n"
            "Property address: 5605 EVERGLADE ROAD"
        )
        rec = fp.ForeclosureRecord(source_pdf="test.pdf")
        fp._parse_fields_into_record(rec, block)
        assert rec.notice_date_iso is None
        assert rec.notice_date_pattern is None

    def test_sale_date_and_notice_date_are_independent(self):
        # Realistic notice structure: "Date of Sale" appears near the top
        # of the notice; "Dated:" appears near the bottom (trustee
        # signature). In real PDFs these are separated by 1000+ chars of
        # legal boilerplate, putting them well outside each other's
        # 200-char rejection window.
        #
        # The boilerplate below avoids any rejection phrase (no "deed of
        # trust", "whereas", "executed", "recorded", "notary", etc.) so
        # we can verify the spacing semantics in isolation.
        filler = (
            "The substitute trustee will conduct a public sale at the "
            "designated location. The property securing the loan shall "
            "be sold to the highest bidder for cash in lawful money of "
            "the United States. Bidders should arrive at the courthouse "
            "with sufficient funds. " * 5
        )
        block = (
            "Date of Sale: October 7, 2025\n"
            "Time of Sale: 10:00 AM\n"
            + filler +
            "\nTrustee signs below.\n"
            "Dated: 1/12/2026"
        )
        rec = fp.ForeclosureRecord(source_pdf="test.pdf")
        fp._parse_fields_into_record(rec, block)
        # sale_date should pick up Oct 7, 2025.
        assert rec.sale_date_iso == "2025-10-07"
        # notice_date should pick up Jan 12, 2026 (Dated: is far enough
        # away from "Date of Sale" that the rejection rule doesn't fire).
        assert rec.notice_date_iso == "2026-01-12"
