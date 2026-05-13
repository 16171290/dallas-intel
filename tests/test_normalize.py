"""
Regression tests for scraper.normalize.

Includes the guard test for §3.3.1 — the HOA-plaintiff bug carried over
from Harris-Intel. If is_hoa_entity() ever stops recognizing the
representative HOA names, this suite fails loudly.
"""

import pytest

from scraper import normalize


# ═══════════════════════════════════════════════════════════════════════════
# Address normalization
# ═══════════════════════════════════════════════════════════════════════════

class TestAddressNormalization:
    def test_empty_input(self):
        assert normalize.normalize_address("") == ""
        assert normalize.normalize_address(None) == ""

    def test_basic_uppercase_and_trim(self):
        assert normalize.normalize_address("  123 main st  ") == "123 MAIN ST"

    def test_punctuation_removal(self):
        assert normalize.normalize_address("123 Main St, Dallas, TX") == \
            "123 MAIN ST DALLAS TX"

    def test_directional_expansion(self):
        assert normalize.normalize_address("123 North Main St") == "123 N MAIN ST"
        assert normalize.normalize_address("456 Northeast Highway") == "456 NE HWY"
        assert normalize.normalize_address("789 South West Main") == "789 S W MAIN"

    def test_suffix_expansion(self):
        cases = [
            ("123 Main Street",     "123 MAIN ST"),
            ("123 Main Avenue",     "123 MAIN AVE"),
            ("123 Main Boulevard",  "123 MAIN BLVD"),
            ("123 Main Drive",      "123 MAIN DR"),
            ("123 Main Road",       "123 MAIN RD"),
            ("123 Main Lane",       "123 MAIN LN"),
            ("123 Main Court",      "123 MAIN CT"),
            ("123 Main Place",      "123 MAIN PL"),
            ("123 Main Parkway",    "123 MAIN PKWY"),
            ("123 Main Highway",    "123 MAIN HWY"),
            ("123 Main Trail",      "123 MAIN TRL"),
            ("123 Main Circle",     "123 MAIN CIR"),
            ("123 Main Terrace",    "123 MAIN TER"),
        ]
        for raw, expected in cases:
            assert normalize.normalize_address(raw) == expected, f"Failed: {raw!r}"

    def test_whitespace_collapse(self):
        assert normalize.normalize_address("123    Main     St") == "123 MAIN ST"

    def test_unit_stripped_from_primary(self):
        assert normalize.normalize_address("123 Main St Apt 5")     == "123 MAIN ST"
        assert normalize.normalize_address("123 Main St Suite 100") == "123 MAIN ST"
        assert normalize.normalize_address("123 Main St #5")        == "123 MAIN ST"
        assert normalize.normalize_address("123 Main St Unit B")    == "123 MAIN ST"

    def test_extract_unit(self):
        assert normalize.extract_unit("123 Main St Apt 5")        == "APT 5"
        assert normalize.extract_unit("123 Main St Ste 100")      == "STE 100"
        assert normalize.extract_unit("123 Main St #5")           == "UNIT 5"
        assert normalize.extract_unit("123 Main St APARTMENT 7B") == "APT 7B"
        assert normalize.extract_unit("123 Main St")              is None
        assert normalize.extract_unit("")                          is None
        assert normalize.extract_unit(None)                        is None

    def test_real_world_dallas_addresses(self):
        # Sample from an actual Dallas foreclosure-PDF.
        assert normalize.normalize_address("1004 Seago Drive, Seagoville, TX 75159") == \
            "1004 SEAGO DR SEAGOVILLE TX 75159"
        # With unit:
        assert normalize.normalize_address("456 Live Oak Ln Apt 12, Dallas, TX") == \
            "456 LIVE OAK LN DALLAS TX"


# ═══════════════════════════════════════════════════════════════════════════
# HOA detection — §3.3.1 regression guard
# ═══════════════════════════════════════════════════════════════════════════

class TestHOADetection:
    def test_empty_or_none(self):
        assert not normalize.is_hoa_entity("")
        assert not normalize.is_hoa_entity(None)
        assert not normalize.is_hoa_entity("   ")

    def test_normal_individual_names(self):
        for name in [
            "John Smith",
            "MARY JONES",
            "Robert and Susan Williams",
            "Maria Garcia-Hernandez",
            "James O'Brien",
        ]:
            assert not normalize.is_hoa_entity(name), f"False positive: {name!r}"

    def test_obvious_hoa_indicators(self):
        # These MUST flag as HOA. If any of these stops flagging, the
        # §3.3.1 fix has regressed.
        for name in [
            "Richland Trace Owners Association, Inc.",
            "Highland Park HOA",
            "Lakewood Homeowners Association",
            "Westgate Property Owners Association",
            "The Vineyards Condominium Association",
            "Stonebriar Townhomes Association",
            "Bent Tree Neighborhood Association",
            "The Master Association of Las Colinas",
            "Las Colinas Community Association",
            "The Cedars Condo Association",
        ]:
            assert normalize.is_hoa_entity(name), f"Should be HOA: {name!r}"

    def test_corporate_suffix_alone_not_sufficient(self):
        """Mere corporate suffix shouldn't auto-flag — only HOA-specific
        patterns flag, to avoid false positives on banks, churches, LLCs.
        """
        for name in [
            "Acme Properties, Inc.",
            "Bank of America",
            "Fellowship Holiness Church, Inc",
            "Wells Fargo Bank N.A.",
            "Quicken Loans LLC",
            "Smith & Sons Construction",
        ]:
            assert not normalize.is_hoa_entity(name), f"False positive: {name!r}"

    def test_case_insensitive(self):
        assert normalize.is_hoa_entity("highland park hoa")
        assert normalize.is_hoa_entity("HIGHLAND PARK HOA")
        assert normalize.is_hoa_entity("Highland Park Hoa")


# ═══════════════════════════════════════════════════════════════════════════
# Grantor / grantee extraction (§3.3.1)
# ═══════════════════════════════════════════════════════════════════════════

class TestGrantorGrantee:
    def test_explicit_keys(self):
        rec = {"grantor": "ABC HOA", "grantee": "John Smith"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == "ABC HOA"
        assert gee == "John Smith"

    def test_filed_by_filed_against(self):
        rec = {"filed_by": "Bank of America", "filed_against": "Jane Doe"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == "Bank of America"
        assert gee == "Jane Doe"

    def test_plaintiff_defendant(self):
        rec = {"plaintiff": "Lakewood HOA", "defendant": "Bob Roberts"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == "Lakewood HOA"
        assert gee == "Bob Roberts"

    def test_trustee_debtor_synonyms(self):
        """Foreclosure-PDF format uses trustee/debtor instead of grantor/grantee."""
        rec = {"trustee": "Substitute Trustee Corp", "debtor": "Homeowner Name"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == "Substitute Trustee Corp"
        assert gee == "Homeowner Name"

    def test_owner_fallback(self):
        rec = {"grantor": "Lender Trust", "owner": "Borrower Name"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == "Lender Trust"
        assert gee == "Borrower Name"

    def test_missing_grantor(self):
        rec = {"grantee": "John Smith"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == ""
        assert gee == "John Smith"

    def test_missing_both(self):
        rec = {"instrument_num": "12345"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == ""
        assert gee == ""

    def test_whitespace_stripped(self):
        rec = {"grantor": "  ABC HOA  ", "grantee": "\tJohn Smith\n"}
        g, gee = normalize.extract_grantor_grantee(rec)
        assert g   == "ABC HOA"
        assert gee == "John Smith"


# ═══════════════════════════════════════════════════════════════════════════
# Instrument code mapping (§A.3)
# ═══════════════════════════════════════════════════════════════════════════

class TestInstrumentCodes:
    def test_known_codes(self):
        assert normalize.dallas_code_to_category("LP")  == "L/P"
        assert normalize.dallas_code_to_category("NOF") == "NOTICE"
        assert normalize.dallas_code_to_category("TD")  == "TRSALE"
        assert normalize.dallas_code_to_category("LN")  == "LIEN"
        assert normalize.dallas_code_to_category("JUD") == "JUDGE"
        assert normalize.dallas_code_to_category("BR")  == "BNKRCY"
        assert normalize.dallas_code_to_category("REL") == "REL"
        assert normalize.dallas_code_to_category("PB")  == "PROB"

    def test_unknown_code(self):
        assert normalize.dallas_code_to_category("XYZ") is None
        assert normalize.dallas_code_to_category("")    is None
        assert normalize.dallas_code_to_category(None)  is None

    def test_case_insensitive(self):
        assert normalize.dallas_code_to_category("lp")  == "L/P"
        assert normalize.dallas_code_to_category("Nof") == "NOTICE"
        assert normalize.dallas_code_to_category(" td ") == "TRSALE"

    def test_codes_in_multiple_categories(self):
        # TXL appears in both T/L and LEVY per §A.3
        cats = normalize.harris_categories_for_dallas_code("TXL")
        assert "T/L"  in cats
        assert "LEVY" in cats
        assert len(cats) == 2

    def test_single_category_codes_return_one(self):
        assert normalize.harris_categories_for_dallas_code("LP") == ["L/P"]
        assert normalize.harris_categories_for_dallas_code("BR") == ["BNKRCY"]

    def test_unknown_code_returns_empty_list(self):
        assert normalize.harris_categories_for_dallas_code("XYZ") == []
        assert normalize.harris_categories_for_dallas_code("")    == []

    def test_suppression_codes(self):
        assert     normalize.is_suppression_code("REL")
        assert     normalize.is_suppression_code("RLP")
        assert     normalize.is_suppression_code("rel")  # case-insensitive
        assert not normalize.is_suppression_code("LP")
        assert not normalize.is_suppression_code("NOF")
        assert not normalize.is_suppression_code("")
        assert not normalize.is_suppression_code(None)
