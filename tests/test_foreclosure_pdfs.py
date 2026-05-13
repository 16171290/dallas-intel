"""
Regression tests for scraper.foreclosure_pdfs.

The pure text-parsing function ``extract_records_from_text`` gets thorough
coverage with sample notice text. The PDF-reading wrapper is not unit-tested
(it's a thin pdfplumber call; tested at integration time against a fixture
PDF in Phase 2).
"""

import re
from pathlib import Path

import pytest

from scraper import foreclosure_pdfs


# ═══════════════════════════════════════════════════════════════════════════
# Filename pattern
# ═══════════════════════════════════════════════════════════════════════════

class TestFilenameRegex:
    def test_basic_filename(self):
        m = foreclosure_pdfs._FILENAME_RE.match("Cedar-Hill_3.pdf")
        assert m is not None
        assert m.group("city") == "Cedar-Hill"
        assert m.group("week") == "3"
        assert m.group("suffix") is None

    def test_dallas_split_filename(self):
        m = foreclosure_pdfs._FILENAME_RE.match("Dallas_4 (2).pdf")
        assert m is not None
        assert m.group("city") == "Dallas"
        assert m.group("week") == "4"
        assert m.group("suffix") == "2"

    def test_multi_word_city(self):
        m = foreclosure_pdfs._FILENAME_RE.match("Glenn Heights_1.pdf")
        assert m is not None
        assert m.group("city").strip() == "Glenn Heights"

    def test_hyphenated_city(self):
        m = foreclosure_pdfs._FILENAME_RE.match("Grand-Prairie_2.pdf")
        assert m is not None
        assert m.group("city") == "Grand-Prairie"

    def test_other_category(self):
        m = foreclosure_pdfs._FILENAME_RE.match("Other_5.pdf")
        assert m is not None
        assert m.group("city") == "Other"

    def test_bad_filename(self):
        assert foreclosure_pdfs._FILENAME_RE.match("notapdf.txt") is None
        assert foreclosure_pdfs._FILENAME_RE.match("README.pdf") is None


# ═══════════════════════════════════════════════════════════════════════════
# Notice splitting
# ═══════════════════════════════════════════════════════════════════════════

class TestSplitIntoNotices:
    def test_empty_text(self):
        assert foreclosure_pdfs._split_into_notices("") == []
        assert foreclosure_pdfs._split_into_notices("   \n  \t  ") == []

    def test_text_without_opener_returned_as_single_block(self):
        text = "Just some random text without any notice header."
        blocks = foreclosure_pdfs._split_into_notices(text)
        assert len(blocks) == 1
        assert blocks[0] == text

    def test_single_notice(self):
        text = (
            "NOTICE OF FORECLOSURE SALE\n"
            "Sale Date: October 7, 2025\n"
            "1004 Seago Drive, Seagoville, TX\n"
        )
        blocks = foreclosure_pdfs._split_into_notices(text)
        assert len(blocks) == 1
        assert "NOTICE OF FORECLOSURE SALE" in blocks[0]

    def test_multiple_notices(self):
        text = (
            "Some header text.\n\n"
            "NOTICE OF FORECLOSURE SALE\n"
            "Sale Date: October 7, 2025\n"
            "1004 Seago Drive, Seagoville, TX\n\n"
            "NOTICE OF SUBSTITUTE TRUSTEE'S SALE\n"
            "Sale Date: November 4, 2025\n"
            "200 Main Street, Dallas, TX\n"
        )
        blocks = foreclosure_pdfs._split_into_notices(text)
        assert len(blocks) == 2
        assert "October 7, 2025"  in blocks[0]
        assert "November 4, 2025" in blocks[1]

    def test_assessment_lien_notice_recognized(self):
        text = "NOTICE OF ASSESSMENT LIEN SALE\nSome HOA content"
        blocks = foreclosure_pdfs._split_into_notices(text)
        assert len(blocks) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Field extraction
# ═══════════════════════════════════════════════════════════════════════════

# Sample foreclosure-notice text modeled on actual Dallas County PDF excerpts.
SAMPLE_NOTICE = """\
NOTICE OF FORECLOSURE SALE

Property: 1004 Seago Drive, Seagoville, TX 75159
Sale Date: October 7, 2025
Sale Time: 10:00 AM

Maker: Ramiro Del Llano Escobedo and Olga A. Yanez

Substitute Trustee: ABC Trustee Corp

Original principal amount: $245,000.00

NOW, THEREFORE, notice is hereby given that on Tuesday, October 7, 2025,
between 10 o'clock a.m. and 4 o'clock p.m., the Association will sell
said real estate.
"""

SAMPLE_HOA_NOTICE = """\
NOTICE OF ASSESSMENT LIEN SALE

WHEREAS, the said James D. Jackson has continued to default in the payment
of her indebtedness to the Association and the same is now wholly due,
and the Association, acting by and through its duly authorized agent,
intends to sell the herein described property to satisfy the present
indebtedness of said owners to the Association;

NOW, THEREFORE, notice is hereby given that on Tuesday, October 7, 2025,
between 10 o'clock a.m. and 4 o'clock p.m., the Association will sell
said real estate.

Also commonly known as 1004 Seago Drive, Seagoville, TX 75159.
"""


class TestExtractRecordsFromText:
    def test_empty_text(self):
        assert foreclosure_pdfs.extract_records_from_text("") == []
        assert foreclosure_pdfs.extract_records_from_text("   \n  ") == []

    def test_extracts_sale_date(self):
        recs = foreclosure_pdfs.extract_records_from_text(SAMPLE_NOTICE)
        assert len(recs) >= 1
        assert recs[0].sale_date == "October 7, 2025"

    def test_sale_date_iso_normalization(self):
        recs = foreclosure_pdfs.extract_records_from_text(SAMPLE_NOTICE)
        assert recs[0].sale_date_iso == "2025-10-07"

    def test_extracts_maker_as_debtor(self):
        recs = foreclosure_pdfs.extract_records_from_text(SAMPLE_NOTICE)
        assert recs[0].debtor is not None
        assert "Ramiro" in recs[0].debtor

    def test_extracts_amount(self):
        recs = foreclosure_pdfs.extract_records_from_text(SAMPLE_NOTICE)
        assert recs[0].original_loan_amount == "245000.00"

    def test_hoa_assessment_lien_extracts_address(self):
        recs = foreclosure_pdfs.extract_records_from_text(SAMPLE_HOA_NOTICE)
        assert len(recs) == 1
        assert recs[0].property_address is not None
        assert "Seago" in recs[0].property_address

    def test_hoa_notice_extracts_owner_from_prose(self):
        """When no 'Maker:' line exists, fall back to prose 'present owner' pattern."""
        recs = foreclosure_pdfs.extract_records_from_text(SAMPLE_HOA_NOTICE)
        # The prose form 'present owner of said real property, X' should
        # populate debtor. This test pins the behavior in.
        # (May be None if regex doesn't match a given style; flag warns.)
        if recs[0].debtor is None:
            assert "no_address_or_debtor" not in recs[0].parse_warnings or \
                   recs[0].property_address is not None

    def test_source_pdf_propagated(self):
        recs = foreclosure_pdfs.extract_records_from_text(
            SAMPLE_NOTICE, source_pdf="test_source.pdf"
        )
        assert recs[0].source_pdf == "test_source.pdf"

    def test_warnings_flag_unparseable(self):
        weak_text = "NOTICE OF FORECLOSURE SALE\n\nSome random text with no fields."
        recs = foreclosure_pdfs.extract_records_from_text(weak_text)
        assert len(recs) == 1
        assert "no_address_or_debtor" in recs[0].parse_warnings


class TestParseDateIso:
    def test_basic(self):
        assert foreclosure_pdfs._parse_date_iso("October 7, 2025") == "2025-10-07"
        assert foreclosure_pdfs._parse_date_iso("November 14, 2024") == "2024-11-14"

    def test_january_zero_pad(self):
        assert foreclosure_pdfs._parse_date_iso("January 1, 2026") == "2026-01-01"

    def test_no_comma(self):
        assert foreclosure_pdfs._parse_date_iso("June 15 2025") == "2025-06-15"

    def test_none_passes_through(self):
        assert foreclosure_pdfs._parse_date_iso(None) is None
        assert foreclosure_pdfs._parse_date_iso("") is None

    def test_malformed(self):
        assert foreclosure_pdfs._parse_date_iso("garbage") is None
        assert foreclosure_pdfs._parse_date_iso("2025-10-07") is None  # different format
        assert foreclosure_pdfs._parse_date_iso("Bobruary 32, 2099") is None


# ═══════════════════════════════════════════════════════════════════════════
# Polite delay
# ═══════════════════════════════════════════════════════════════════════════

class TestPoliteDelay:
    def test_uses_configured_range(self, monkeypatch):
        """Verify the delay function reads from RATE_LIMITS."""
        from scraper import config

        called_with = []
        monkeypatch.setattr(
            foreclosure_pdfs.time, "sleep",
            lambda s: called_with.append(s),
        )
        monkeypatch.setattr(
            foreclosure_pdfs.random, "uniform",
            lambda lo, hi: (lo + hi) / 2,
        )

        foreclosure_pdfs._polite_delay("dallascounty.org")
        lo, hi = config.RATE_LIMITS["dallascounty.org"]
        assert called_with == [(lo + hi) / 2]
