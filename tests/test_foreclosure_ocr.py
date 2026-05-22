"""Tests for the foreclosure OCR universal-pattern extractor.

Browser/Playwright capture is covered by manual probe runs; this file
covers the pure-function extractor (extract_fields_from_text) against
synthetic samples that mirror each of the 12+ real notice templates we
catalogued in the 58-sample audit.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.foreclosure_ocr import (
    extract_fields_from_text,
    is_hoa_lien,
    _to_iso_date,
)


# ============================================================================
# Date normalization helper
# ============================================================================

def test_to_iso_word_format():
    assert _to_iso_date("July 7, 2026") == "2026-07-07"
    assert _to_iso_date("August 04, 2026") == "2026-08-04"
    assert _to_iso_date("November 25, 2002") == "2002-11-25"


def test_to_iso_numeric_format():
    assert _to_iso_date("7/7/2026") == "2026-07-07"
    assert _to_iso_date("08/04/2026") == "2026-08-04"
    assert _to_iso_date("5/12/26") == "2026-05-12"


def test_to_iso_returns_none_on_garbage():
    assert _to_iso_date("") is None
    assert _to_iso_date("no-date-here") is None
    assert _to_iso_date(None) is None


# ============================================================================
# HOA-lien suppression
# ============================================================================

def test_hoa_lien_detection():
    text = "NOTICE OF ASSESSMENT LIEN SALE\nfor unpaid HOA dues..."
    assert is_hoa_lien(text) is True
    result = extract_fields_from_text(text)
    assert result.is_hoa_lien is True
    assert "hoa_assessment_lien_sale" in result.warnings


def test_normal_foreclosure_not_hoa():
    text = "NOTICE OF SUBSTITUTE TRUSTEE'S SALE\nGrantor(s): John Doe"
    assert is_hoa_lien(text) is False


# ============================================================================
# Grantor extraction across 9 template variants
# ============================================================================

def test_format_A_labeled_grantor():
    """Format A — Servicing-form."""
    text = "DALLAS County\nGrantor(s): DAVID WALKER and LINDA E. WALKER\nDate of Sale: July 7, 2026"
    r = extract_fields_from_text(text)
    assert r.grantor == "DAVID WALKER and LINDA E. WALKER"
    assert r.grantor_pattern == "labeled-grantor"


def test_format_I_grantor_mortgagor_slash():
    """Format I — McCarthy & Holthus with Grantor(s)/Mortgagor(s): combined label."""
    text = "Grantor(s)/Mortgagor(s):\nCHRISTINA BETZABE SALGUERO-AVENDANO AND JOSE DE JESUS HUERTA"
    r = extract_fields_from_text(text)
    # The "labeled-grantor-mortgagor" pattern catches values after the slash variant
    assert r.grantor_pattern == "labeled-grantor-mortgagor"


def test_format_D_trustor():
    """Format D — Prestige Default Services uses 'Trustor(s):' not 'Grantor'."""
    text = "Trustor(s):           HARRISON J TASSOPOULOS\nOriginal Beneficiary:"
    r = extract_fields_from_text(text)
    assert "HARRISON J TASSOPOULOS" in (r.grantor or "")
    assert r.grantor_pattern == "labeled-trustor"


def test_format_M_original_mortgagor():
    """Newer template seen in audit (file_009) uses 'ORIGINAL MORTGAGOR:'."""
    text = "Matter No.: 144736-TX\nORIGINAL MORTGAGOR: SAMUEL T. TYSON, A MARRIED MAN"
    r = extract_fields_from_text(text)
    assert "SAMUEL T. TYSON" in (r.grantor or "")
    assert r.grantor_pattern == "labeled-original-mortgagor"


def test_format_B_inline_with_grantor():
    """Format B — FCTX_NTSS inline 'with NAME, grantor(s) and' pattern."""
    text = (
        "3. Instrument to be Foreclosed. The Instrument to be foreclosed is the Deed of Trust "
        "dated November 25, 2002 and recorded in Document VOLUME 2002235, "
        "with EDMUND BARRON AND WIFE, JUANITA TREVINO, grantor(s) and MORTGAGE ELECTRONIC..."
    )
    r = extract_fields_from_text(text)
    assert "EDMUND BARRON" in (r.grantor or "")
    assert r.grantor_pattern == "inline-with-grantor"


def test_format_D_F_executed_by_status():
    """Format D / F — 'executed by NAME, A SINGLE MAN' inside narrative."""
    text = (
        "Obligations Secured. The deed of trust provides that it secures the payment of the "
        "indebtedness in the original principal amount of $136,000.00, executed by "
        "HARRISON J TASSOPOULOS, A SINGLE MAN, and payable to..."
    )
    r = extract_fields_from_text(text)
    # Trustor label may not be present in this snippet, so executed-by takes over
    assert "HARRISON J TASSOPOULOS" in (r.grantor or "")


def test_format_K_dot_executed_by_secures():
    """Format K — Hive Point: 'The Deed of Trust executed by NAME secures...'"""
    text = "The Deed of Trust executed by JESUS J. BETANCOURT secures the payment of the indebtedness"
    r = extract_fields_from_text(text)
    assert "JESUS J. BETANCOURT" in (r.grantor or "")
    assert r.grantor_pattern == "deed-executed-by-secures"


def test_format_J_borrower_quoted():
    """Format J — Private trustee uses 'NAME ("Borrower")' parenthetical."""
    text = (
        "THAT, WHEREAS, on or about November 28, 2022 "
        "Yutaka Meyers and Bobbie Meyers (\"Borrower\"), executed and delivered to..."
    )
    r = extract_fields_from_text(text)
    assert "Meyers" in (r.grantor or "")
    assert r.grantor_pattern == "name-quoted-borrower"


# ============================================================================
# Sale-date extraction across format variants
# ============================================================================

def test_sale_date_word_labeled():
    text = "Date of Sale: July 7, 2026 between the hours of 10:00 AM and 1:00 PM."
    r = extract_fields_from_text(text)
    assert r.sale_date_raw == "July 7, 2026"
    assert r.sale_date_iso == "2026-07-07"


def test_sale_date_numeric_labeled():
    text = "Date of Sale: 7/7/2026"
    r = extract_fields_from_text(text)
    assert r.sale_date_iso == "2026-07-07"


def test_sale_date_section_1_word():
    """Format B — sale date inside numbered section '1. Date, Time, and Place of Sale.'"""
    text = (
        "1. Date, Time, and Place of Sale.\n"
        "Date: August 04, 2026\n"
        "Time: The sale will begin at 10:00 AM\n"
        "Place: THE AREA OUTSIDE..."
    )
    r = extract_fields_from_text(text)
    assert r.sale_date_iso == "2026-08-04"


def test_sale_date_section_1_numeric():
    """Format D / H — numeric date in section 1."""
    text = (
        "Date, Time, and Place of Sale - The sale is scheduled to be held...\n"
        "Date:    7/7/2026\n"
        "Time:   10:00 AM..."
    )
    r = extract_fields_from_text(text)
    assert r.sale_date_iso == "2026-07-07"


def test_sale_date_narrative_format_E():
    """Format E — ServiceLink prose narrative."""
    text = (
        "NOTICE IS HEREBY GIVEN that on Tuesday, August 4, 2026 at 01:00 PM, "
        "no later than three hours thereafter, the Substitute Trustee will sell..."
    )
    r = extract_fields_from_text(text)
    assert r.sale_date_iso == "2026-08-04"


# ============================================================================
# Property-address extraction
# ============================================================================

def test_address_labeled_one_line():
    text = "Property Address: 2016 Lone Oak Trail Mesquite, TX 75181"
    r = extract_fields_from_text(text)
    assert "Lone Oak Trail" in (r.property_address or "")
    assert "75181" in r.property_address


def test_address_commonly_known_as():
    """Format F variant uses 'Commonly known as:' for the property address."""
    text = "2. Property To Be Sold. LOT 18, BLOCK A...\nCommonly known as: 1937 P STER, TX 75146"
    r = extract_fields_from_text(text)
    assert "STER" in (r.property_address or "")
    assert r.address_pattern == "commonly-known-as-one-line"


def test_address_no_match():
    """When no labeled address is present, leave it None (DCAD legal-resolver
    is the downstream fallback)."""
    text = "Grantor(s): John Doe\nDate of Sale: 7/7/2026"
    r = extract_fields_from_text(text)
    assert r.property_address is None


# ============================================================================
# Loan-amount extraction
# ============================================================================

def test_loan_amount_original_principal_amount_of():
    text = "The promissory note in the original principal amount of $341,078.00, payable to..."
    r = extract_fields_from_text(text)
    assert r.loan_amount == "341,078.00"


def test_loan_amount_labeled_original_principal():
    """Format C — PLG labeled tabular row."""
    text = "Original Principal:    $341,078.00"
    r = extract_fields_from_text(text)
    assert r.loan_amount == "341,078.00"


def test_loan_amount_simple_amount_label():
    text = "DALLAS County\nDeed of Trust Dated: August 23, 2005\nAmount: $100,000.00\nGrantor(s):"
    r = extract_fields_from_text(text)
    assert r.loan_amount == "100,000.00"


# ============================================================================
# Legal description extraction
# ============================================================================

def test_legal_desc_labeled():
    text = (
        "Legal Description: LOT 5 IN BLOCK 9 OF THE FIFTH INCREMENT OF PLYMOUTH PARK NORTH, "
        "AN ADDITION TO THE CITY OF IRVING, DALLAS COUNTY, TEXAS.\n\n"
        "Whereas, an Order to Proceed..."
    )
    r = extract_fields_from_text(text)
    assert "PLYMOUTH PARK NORTH" in (r.legal_description or "")


def test_legal_desc_being_lot_unlabeled():
    """Format B / G — legal description starts with 'BEING LOT ...' on its own."""
    text = (
        "EXHIBIT \"A\"\n"
        "BEING LOT 16, IN BLOCK E/8443, OF WALNUT CREEK ESTATES, SECTION ONE, "
        "AN ADDITION TO THE CITY OF DALLAS, DALLAS COUNTY, TEXAS."
    )
    r = extract_fields_from_text(text)
    assert "WALNUT CREEK ESTATES" in (r.legal_description or "")


# ============================================================================
# Empty / degenerate inputs
# ============================================================================

def test_empty_text():
    r = extract_fields_from_text("")
    assert r.grantor is None
    assert r.sale_date_iso is None
    assert "empty_ocr_text" in r.warnings


def test_no_extractable_fields():
    """When OCR text is garbage, all fields stay None but no crash."""
    text = "asdfqwerty zxcv asdf qwer"
    r = extract_fields_from_text(text)
    assert r.grantor is None
    assert r.sale_date_iso is None
    assert r.property_address is None
    assert "no_grantor_extracted" in r.warnings


# ============================================================================
# Cross-field — a realistic Format C (PLG) example end-to-end
# ============================================================================

def test_ocr_pages_multi_page_uses_pool():
    """2+ pages should be dispatched through the multiprocessing.Pool when
    one is provided. We use a FakePool with a recording .map() to verify
    the parallel path is taken without depending on Tesseract being
    installed in the test environment."""
    from scraper.foreclosure_ocr import _ocr_pages
    class FakePool:
        def __init__(self): self.received_args = None
        def map(self, fn, args):
            self.received_args = list(args)
            # Worker returns (text, elapsed_s, size_bytes, width, height, mode, dpi) — PR 12.19.
            return [
                ("TEXT-P1", 0.1, 11, 1200, 1600, "RGB", "300x300"),
                ("TEXT-P2", 0.1, 11, 1200, 1600, "RGB", "300x300"),
            ]
    fake = FakePool()
    pages = {1: b"png-bytes-1", 2: b"png-bytes-2"}
    result = _ocr_pages(pages, "/fake/tesseract", pool=fake)
    assert fake.received_args is not None, "pool.map should be called for 2+ pages"
    assert len(fake.received_args) == 2
    # Each worker arg is (bytes, tess_cmd)
    assert fake.received_args[0] == (b"png-bytes-1", "/fake/tesseract")
    assert result == "TEXT-P1\n\nTEXT-P2"


def test_ocr_pages_empty_returns_empty():
    from scraper.foreclosure_ocr import _ocr_pages
    assert _ocr_pages({}, "/fake/tesseract") == ""


def test_format_C_end_to_end_PLG():
    """Format C — Padgett Law Group: structured tabular block with everything."""
    text = """NOTICE OF ACCELERATION AND NOTICE OF TRUSTEE'S SALE

DEED OF TRUST INFORMATION:
Date: August 11, 2023
Grantor(s): James N. Kun, a Single Man
Original Mortgagee: Mortgage Electronic Registration Systems, Inc.
Original Principal: $341,078.00
Property: Lot 4, Block Y, Solterra Phase 1A, an Addition to the City of Mesquite, Texas
Property Address: 2016 Lone Oak Trail Mesquite, TX 75181

SALE INFORMATION:
Date of Sale: July 7, 2026
PLG File Number: 25-007799-2
"""
    r = extract_fields_from_text(text)
    assert "James N. Kun" in (r.grantor or "")
    assert r.sale_date_iso == "2026-07-07"
    assert "Lone Oak Trail" in (r.property_address or "")
    assert r.loan_amount == "341,078.00"
    assert r.is_hoa_lien is False
