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


# ============================================================================
# Fix 1: Mixed-case + OCR-garbage-tolerant unlabeled address patterns
# (Added per full-population probe 2026-05-26)
# ============================================================================

def test_address_mixed_case_one_line():
    """Format with no label, mixed case, single line:
       '2639 Lenway Street, Dallas, TX 75215'"""
    text = (
        "C&M No. 44-26-02161\n"
        "2639 Lenway Street, Dallas, TX 75215\n"
        "NOTICE OF [SUBSTITUTE] TRUSTEE'S SALE\n"
    )
    r = extract_fields_from_text(text)
    assert "Lenway" in (r.property_address or "")
    assert "75215" in (r.property_address or "")
    assert r.address_pattern == "mixed-case-one-line"


def test_address_mixed_case_two_line_with_barcode_garbage():
    """OCR-injected barcode digits between street and city:
       '5605 SADDLEBACK ROAD 000000 10820736 / GARLAND, TX 75043'"""
    text = (
        "Notice of Foreclosure\n"
        "5605 SADDLEBACK ROAD 000000 10820736\n"
        "GARLAND, TX 75043\n"
        "Grantor: ..."
    )
    r = extract_fields_from_text(text)
    assert "SADDLEBACK" in (r.property_address or "")
    assert "75043" in (r.property_address or "")
    assert r.address_pattern == "mixed-case-two-line"


def test_address_mixed_case_dallas_parkway():
    """Mixed-case street name 'Dallas Parkway' rejected by all-caps patterns:
       '14160 Dallas Parkway\nDallas, TX 75254'"""
    text = (
        "NOTICE OF FORECLOSURE\n"
        "14160 Dallas Parkway\n"
        "Dallas, TX 75254\n"
    )
    r = extract_fields_from_text(text)
    assert "Dallas Parkway" in (r.property_address or "")
    assert "75254" in (r.property_address or "")


def test_address_no_comma_four_word():
    """No-comma form: '611 Matthew Place Richardson TX 75081'"""
    text = (
        "Property to be sold:\n"
        "611 Matthew Place Richardson TX 75081\n"
        "More details below."
    )
    r = extract_fields_from_text(text)
    assert "Matthew Place" in (r.property_address or "")
    assert "75081" in (r.property_address or "")
    assert r.address_pattern == "no-comma-street-city-tx-zip"


def test_address_labeled_still_wins_over_unlabeled():
    """Labeled 'Property Address:' must still take priority over the new
    unlabeled mixed-case patterns (priority via list order)."""
    text = (
        "Top-of-doc: 2639 Lenway Street, Dallas, TX 75215\n"
        "Property Address: 2016 Lone Oak Trail Mesquite, TX 75181\n"
    )
    r = extract_fields_from_text(text)
    assert "Lone Oak Trail" in (r.property_address or "")  # labeled wins
    assert r.address_pattern == "property-address-one-line"


# ============================================================================
# Fix 2: OCR -> publicsearch-snippet bridge for legal_resolver
# ============================================================================

def test_legal_desc_to_snippet_being_lot():
    from scraper.foreclosure_ocr import _legal_desc_to_snippet
    desc = ("BEING LOT 16, IN BLOCK E/8443, OF WALNUT CREEK ESTATES, SECTION "
            "ONE, AN ADDITION TO THE CITY OF DALLAS, DALLAS COUNTY, TEXAS")
    snippet = _legal_desc_to_snippet(desc)
    assert snippet is not None
    assert "Subdivision - Name: WALNUT CREEK ESTATES" in snippet
    assert "Lot: 16" in snippet
    assert "Block: E/8443" in snippet


def test_legal_desc_to_snippet_bare_lot_no_being_prefix():
    from scraper.foreclosure_ocr import _legal_desc_to_snippet
    desc = "LOT 12, BLOCK B, OF BEAR CREEK RANCH-PHASE 4, AN ADDITION TO DALLAS COUNTY"
    snippet = _legal_desc_to_snippet(desc)
    assert snippet is not None
    assert "Subdivision - Name: BEAR CREEK RANCH-PHASE 4" in snippet
    assert "Lot: 12" in snippet
    assert "Block: B" in snippet


def test_legal_desc_to_snippet_unparsable_returns_none():
    from scraper.foreclosure_ocr import _legal_desc_to_snippet
    # Missing block
    assert _legal_desc_to_snippet("LOT 16, OF WALNUT CREEK ESTATES") is None
    # Empty
    assert _legal_desc_to_snippet("") is None
    # Garbage
    assert _legal_desc_to_snippet("asdfqwerty") is None


def test_legal_desc_to_snippet_no_of_before_subdivision():
    """NOF audit (record 316730731): the notice reads 'LOT 3, BLOCK 22,
    PICKETTS SUBDIVISION, AN ADDITION...' — comma, not 'OF', before the
    subdivision name. The mandatory 'OF' made this unparsable, so path C
    reported no_subdivision_parsed and the record fell back to a city-only
    address ('GARLAND'). 'OF' is now optional."""
    from scraper.foreclosure_ocr import _legal_desc_to_snippet
    desc = ("LOT 3, BLOCK 22, PICKETTS SUBDIVISION, AN ADDITION TO THE "
            "CITY OF GARLAND, DALLAS COUNTY, TEXAS")
    snippet = _legal_desc_to_snippet(desc)
    assert snippet is not None
    assert "Subdivision - Name: PICKETTS SUBDIVISION" in snippet
    assert "Lot: 3" in snippet
    assert "Block: 22" in snippet


def test_is_valid_address_rejects_courthouse_venue():
    """NOF audit (records 316730723/316730734/316730722): when a notice
    gives the property only by legal description, the address extractor
    otherwise grabs the auction venue — 'George Allen Courts Building,
    600 Commerce Street, Dallas, TX 75202' — as the property, and two
    records resolved to the courthouse's DCAD account. The venue must be
    rejected so the record routes to its legal description."""
    from scraper.foreclosure_ocr import _is_valid_address
    assert _is_valid_address("600 Commerce Street, Dallas, 75202") is False
    assert _is_valid_address(
        "George Allen Courts Building, 600 Commerce Street, Dallas, TX 75202"
    ) is False
    assert _is_valid_address("Place of Sale of Property: Commerce Street") is False
    # Real properties are still accepted.
    assert _is_valid_address("2639 Lenway Street, Dallas, 75215") is True
    assert _is_valid_address("5314 SADDLEBACK RD, GARLAND, TX 75043") is True


# ============================================================================
# Fix 3a: legal-desc patterns tolerate OCR double-newlines
# ============================================================================

def test_legal_desc_being_lot_double_newline_layout():
    """LAPRENSA GRANT case — page 4 EXHIBIT A layout where every line has
    a blank line below it. The old `\\n[^\\n]+` rejected blank lines and
    returned None; the new `\\s+[^\\n]+` skips them."""
    text = (
        "EXHIBIT \"A\"\n"
        "\n"
        "BEING LOT 16, IN BLOCK E/8443, OF WALNUT CREEK ESTATES, SECTION ONE, AN ADDITION To\n"
        "\n"
        "THE CITY OF DALLAS, DALLAS COUNTY, TEXAS, ACCORDING TO THE MAP THEREOF RECORDED\n"
        "\n"
        "IN VOLUME 77035, PAGE 1010, OF THE MAP RECORDS OF DALLAS COUNTY, TEXAS.\n"
    )
    r = extract_fields_from_text(text)
    assert r.legal_description is not None
    assert "WALNUT CREEK ESTATES" in r.legal_description


# ============================================================================
# Fix 3b: Loan amount tolerates OCR-inserted spaces in numbers
# ============================================================================

def test_loan_amount_ocr_inserted_space():
    """OCR splits '$115,862.00' into '$11 5,862.00'. The space-tolerant
    pattern captures it; the extractor strips the space."""
    text = "in the original amount of $11 5,862.00, payable to the order of Selene Finance"
    r = extract_fields_from_text(text)
    assert r.loan_amount == "115,862.00"


def test_loan_amount_no_space_still_works():
    """Existing patterns must keep working unchanged."""
    text = "in the original principal amount of $341,078.00, payable to..."
    r = extract_fields_from_text(text)
    assert r.loan_amount == "341,078.00"


# ============================================================================
# Fix 3c: Grantor patterns for "WHEREAS ... NAME ... as Grantor/Borrower"
# ============================================================================

def test_grantor_whereas_on_date_marital_status():
    """ServiceLink format anchored by marital status."""
    text = (
        "WHEREAS, on April 16, 2010, LAPRENSA GRANT AND REGIONALD GRANT, "
        "WIFE AND HUSBAND, WITH HIM JOINING HEREIN TO PERFECT THE SECURITY "
        "INTEREST BUT NOT TO OTHERWISE BE LIABLE as Grantor/Borrower, "
        "executed and delivered that certain Deed of Trust..."
    )
    r = extract_fields_from_text(text)
    assert r.grantor is not None
    assert "LAPRENSA GRANT" in r.grantor
    assert r.grantor_pattern == "whereas-on-date-name-marital"


def test_grantor_name_as_grantor_borrower_fallback():
    """When no marital status follows the name, use the 'as Grantor/Borrower'
    fallback (lower-priority pattern)."""
    text = (
        "Some preamble.\n"
        "JOHN Q PROPERTYHOLDER as Grantor/Borrower, executed and delivered..."
    )
    r = extract_fields_from_text(text)
    assert r.grantor is not None
    assert "JOHN Q PROPERTYHOLDER" in r.grantor


# ============================================================================
# Fix 3d: Sale-date fallback for OCR-garbled "NOTICE IS HEREBY GIVEN"
# ============================================================================

def test_sale_date_ocr_garbled_hereby():
    """LAPRENSA GRANT case — OCR rendered 'HEREBY' as 'MIEREBY' and split
    'NOTICE IS' to 'NOW THEREFORE'. The fallback accepts both preambles
    plus any word in the HEREBY slot."""
    text = (
        "NOW THEREFORE-N@ MIEREBY GIVEN that on Tuesday, August 4, 2026 at "
        "01:00 PM, no later than three hours thereafter..."
    )
    r = extract_fields_from_text(text)
    assert r.sale_date_iso == "2026-08-04"


def test_sale_date_strict_hereby_still_works():
    """The strict 'NOTICE IS HEREBY GIVEN' pattern must still match
    cleanly, so we don't regress the dominant template."""
    text = (
        "NOTICE IS HEREBY GIVEN that on Tuesday, August 4, 2026 at 01:00 PM, "
        "no later than three hours thereafter..."
    )
    r = extract_fields_from_text(text)
    assert r.sale_date_iso == "2026-08-04"


# ============================================================================
# Fix 4: APN extraction
# ============================================================================

def test_apn_extraction_with_leading_zeros():
    """ServiceLink-format NOFs carry 'APN 00000811302800000' on page 1.
    Strip leading zeros so it matches DCAD's account_num space."""
    text = (
        "TS No TX07000360-26-1 APN 00000811302800000 TO No FCL-217556-TX\n"
        "NOTICE OF FORECLOSURE SALE..."
    )
    r = extract_fields_from_text(text)
    assert r.apn == "811302800000"
    assert r.apn_pattern == "apn-labeled"


def test_apn_extraction_short_form():
    """Some notices have non-padded APN (e.g., '180052700')."""
    text = "APN: 180052700\nNotice of foreclosure..."
    r = extract_fields_from_text(text)
    assert r.apn == "180052700"


def test_apn_not_in_warnings_when_apn_extracted_but_no_address():
    """A record with only an APN (no street addr, no legal desc) should
    NOT raise the 'no_address_or_legal_description' warning since the
    APN provides a downstream-resolvable identifier."""
    text = (
        "Notice of Trustee's Sale\n"
        "APN 00000811302800000\n"
        "Grantor: John Doe\n"
        "Date of Sale: 8/4/2026\n"
    )
    r = extract_fields_from_text(text)
    assert r.apn is not None
    assert "no_address_or_legal_description" not in r.warnings


# ═══════════════════════════════════════════════════════════════════════════
# PR 7 — OCR field validators (boilerplate rejection, garble rejection,
# leading-punctuation strip, wrong-county-zip filter).
#
# Each test below pins a production failure case observed via
# docs/ocr_forensic/<record_id>/ output. The text fixtures are excerpts
# of the actual Tesseract output for those records.
# ═══════════════════════════════════════════════════════════════════════════


def test_grantor_boilerplate_liable_rejected():
    """Liable case (record_id=315562554): the name-as-grantor-borrower
    regex captured 'LIABLE' from ServiceLink boilerplate
    "BUT NOT TO OTHERWISE BE LIABLE as Grantor/Borrower".
    With the boilerplate validator, that capture is rejected and the
    next pattern is tried."""
    text = (
        "WHEREAS, on April 16, 2010, LAPRENSA GRANT AND REGIO D GRAMMY YHFE AND HUSBAND,\n"
        "WITH HIM JOINING HEREIN TO PERFECT THE SECURITY INTEREST BUT NOT TO OTHERWISE BE\n"
        "LIABLE as Grantor/Borrower, executed and delivered that certain Deed of Trust"
    )
    r = extract_fields_from_text(text)
    # The OCR-garbled marital anchor (YHFE instead of WIFE) means the
    # whereas-on-date-marital pattern won't match either; result is
    # either None or some other non-boilerplate capture. Critical: NOT 'LIABLE'.
    assert r.grantor != "LIABLE"
    assert (r.grantor or "").upper() != "LIABLE"


def test_grantor_single_word_capture_rejected():
    """Defense in depth: a single-token grantor capture is almost
    always boilerplate noise, even when not in the explicit blocklist.
    Reject and try the next pattern."""
    text = "ONLYTOKEN as Grantor/Borrower\nMore text follows."
    r = extract_fields_from_text(text)
    assert r.grantor != "ONLYTOKEN"


def test_grantor_blocklist_other_tokens():
    """MORTGAGOR / BORROWER / TRUSTEE / etc — same fix as LIABLE."""
    for noise in ("MORTGAGOR", "BORROWER", "TRUSTEE", "DEBTOR"):
        text = f"BLAH BLAH BE {noise} as Grantor/Borrower\nMore text."
        r = extract_fields_from_text(text)
        assert (r.grantor or "").upper() != noise, (
            f"grantor should not be the boilerplate token {noise!r}; got {r.grantor!r}"
        )


def test_grantor_leading_pipe_stripped():
    """Flores case (record_id=315562562): OCR captured
    'Grantor(s): | JAIME O. FLORES AND MARIE G. FLORES, HUSBAND AND WIFE'
    The leading '| ' is OCR garbage from a table border."""
    text = "Grantor(s): | JAIME O. FLORES AND MARIE G. FLORES, HUSBAND AND WIFE"
    r = extract_fields_from_text(text)
    assert r.grantor is not None
    # The pipe and surrounding whitespace must be stripped
    assert not r.grantor.startswith("|"), f"leading pipe not stripped: {r.grantor!r}"
    assert r.grantor.startswith("JAIME"), f"got: {r.grantor!r}"


def test_grantor_legitimate_capture_unaffected():
    """Regression: a real grantor extraction shouldn't trip any validator."""
    text = "Grantor(s): JANE DOE AND JOHN DOE, HUSBAND AND WIFE"
    r = extract_fields_from_text(text)
    assert r.grantor is not None
    assert "JANE DOE" in r.grantor


def test_address_wrong_county_zip_rejected():
    """Adama case (record_id=315561580): the substitute trustee's
    El Paso office was captured because page 1's labeled property
    address had its 'TX 75' OCR-dropped. ZIP 79912 is El Paso —
    must be rejected so a wrong-property address isn't shipped."""
    text = (
        "Substitute Trustees:\n"
        "7730 Market Center Ave, El Paso, TX 79912\n"
    )
    r = extract_fields_from_text(text)
    assert r.property_address != "7730 Market Center Ave, El Paso, 79912"
    # Conservative: validator should reject any address whose anchored ZIP
    # is non-Dallas/Tarrant. Result here = None (no other pattern matches).
    if r.property_address:
        zip_present = any(s.endswith("79912") for s in r.property_address.split())
        assert not zip_present, f"El Paso ZIP slipped through: {r.property_address!r}"


def test_address_dallas_county_zip_accepted():
    """Regression: 75xxx (Dallas County) and 76xxx (Tarrant) must still
    pass the validator. Cedar Hill = 75104; Fort Worth = 76102."""
    for zip_code, prefix_name in [("75104", "Cedar Hill"), ("76102", "Fort Worth")]:
        text = f"Property Address: 1234 Main Street, City, TX {zip_code}\n"
        r = extract_fields_from_text(text)
        assert r.property_address is not None, f"{prefix_name} ZIP {zip_code} rejected!"
        assert zip_code in r.property_address


def test_address_garbled_symbols_rejected():
    """Battie case (record_id=315561578): OCR captured
    'Commonly known as: 1442 MA 5: {E)' from the watermarked address line.
    With three stray symbols (':', '{', ')'), the candidate is garbled
    and must be rejected."""
    text = "Commonly known as: 1442 MA 5: {E) DESOTO, TX 75115\n"
    r = extract_fields_from_text(text)
    # The garbled capture should be rejected
    if r.property_address:
        garble_count = sum(1 for c in r.property_address if c in ":{}<>[]&")
        assert garble_count < 2, (
            f"garbled address slipped through: {r.property_address!r}"
        )


def test_address_single_ampersand_still_accepted():
    """Edge: a single stray symbol shouldn't reject — only 2+ counts as
    garbled. This is the Cox case ('3031 CREST RIDGE D &') where Stage 6.6
    will correct it via the page tiebreaker; OCR layer shouldn't suppress
    it entirely."""
    text = "Property Address: 3031 CREST RIDGE D &, DALLAS, TX 75228\n"
    r = extract_fields_from_text(text)
    # 1 stray symbol ('&'): accepted by the validator (falls through to
    # Stage 6.6 for correction rather than being silently dropped here)
    assert r.property_address is not None
    assert "75228" in r.property_address


def test_address_street_number_not_treated_as_zip():
    """Regression: '14160 Dallas Parkway, Dallas, TX 75254' — the street
    number 14160 is a 5-digit run but it's NOT the ZIP. ZIP is 75254.
    Validator must anchor on the TX/Texas marker, not the first 5-digit run."""
    text = (
        "NOTICE OF FORECLOSURE\n"
        "14160 Dallas Parkway\n"
        "Dallas, TX 75254\n"
    )
    r = extract_fields_from_text(text)
    assert r.property_address is not None
    assert "75254" in r.property_address
    assert "14160" in r.property_address
