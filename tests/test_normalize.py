"""
Regression tests for scraper.normalize.

Includes the guard test for Â§3.3.1 â€” the HOA-plaintiff bug carried over
from Harris-Intel. If is_hoa_entity() ever stops recognizing the
representative HOA names, this suite fails loudly.
"""

import pytest

from scraper import normalize


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Address normalization
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestAddressNormalization:
    def test_empty_input(self):
        assert normalize.normalize_address("") == ""
        assert normalize.normalize_address(None) == ""

    def test_basic_uppercase_and_trim(self):
        assert normalize.normalize_address("  123 main st  ") == "123 MAIN ST"

    def test_punctuation_removal(self):
        assert normalize.normalize_address("123 Main St, Dallas, TX") == \
            "123 MAIN ST"

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
            "1004 SEAGO DR"
        # With unit:
        assert normalize.normalize_address("456 Live Oak Ln Apt 12, Dallas, TX") == \
            "456 LIVE OAK LN"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HOA detection â€” Â§3.3.1 regression guard
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
        # Â§3.3.1 fix has regressed.
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
        """Mere corporate suffix shouldn't auto-flag â€” only HOA-specific
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Grantor / grantee extraction (Â§3.3.1)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Instrument code mapping (Â§A.3)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
        # Post-verification (2026-05-13): each code lives in exactly one
        # category. TXL is in T/L only; LEVY is SZS only. The invariant
        # below holds for every code in INSTRUMENT_CODES.
        from scraper import config
        for codes in config.INSTRUMENT_CODES.values():
            for code in codes:
                cats = normalize.harris_categories_for_dallas_code(code)
                assert len(cats) == 1, (
                    f"Code {code!r} appears in multiple categories: {cats}"
                )

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


# ============================================================================
# format_owner_name (2026-05-26)
# ============================================================================

class TestFormatOwnerName:
    """The owner/grantor/grantee column formatter. Covers all the dirt
    categories observed in the live records.json corpus."""

    # --- Empty / degenerate input ---
    def test_none_returns_empty(self):
        assert normalize.format_owner_name(None) == ""

    def test_empty_string_returns_empty(self):
        assert normalize.format_owner_name("") == ""
        assert normalize.format_owner_name("   ") == ""

    # --- Comma form (LAST, FIRST MIDDLE) ---
    def test_comma_form_simple(self):
        assert normalize.format_owner_name("BERGER, ANN M.") == "Berger, Ann M."

    def test_comma_form_internal_whitespace(self):
        # publicsearch list-view sometimes has stray internal spaces
        assert normalize.format_owner_name("BARBA , JUDITH") == "Barba, Judith"
        assert normalize.format_owner_name("CHAVEZ , CATHERINE  FRANCES") == "Chavez, Catherine Frances"

    def test_comma_form_with_suffix(self):
        assert normalize.format_owner_name("ARTHUR LEE JONES, Jr") == "Jones, Arthur Lee Jr"
        assert normalize.format_owner_name("MOORE, ALBERT, Jr") == "Moore, Albert Jr"
        assert normalize.format_owner_name("TART, JAMES  STUART, Sr") == "Tart, James Stuart Sr"

    def test_comma_form_multi_word_last_name(self):
        assert normalize.format_owner_name("BROWN JOHNS, CHERYL LEE") == "Brown Johns, Cheryl Lee"

    def test_comma_form_hyphenated(self):
        assert normalize.format_owner_name("CHAVEZ-AGUIRRE, FELICIA") == "Chavez-Aguirre, Felicia"

    # --- No-comma form, FIRST-LAST default ---
    def test_no_comma_first_last(self):
        assert normalize.format_owner_name("Bobbie Meyers") == "Meyers, Bobbie"
        assert normalize.format_owner_name("CYNTHIA HAWKINS") == "Hawkins, Cynthia"
        assert normalize.format_owner_name("JASON ANTHONY LOWE") == "Lowe, Jason Anthony"

    def test_no_comma_first_initial_last(self):
        assert normalize.format_owner_name("JESUS J. BETANCOURT") == "Betancourt, Jesus J."

    # --- No-comma form, LAST-FIRST (DCAD style) ---
    def test_dcad_format_last_first(self):
        assert normalize.format_owner_name("BERGER ANN M", source_format="last_first") == "Berger, Ann M"
        assert normalize.format_owner_name("BRYAN MICHAEL SANFORD", source_format="last_first") == "Bryan, Michael Sanford"

    def test_dcad_format_joint_owners_share_surname(self):
        """DCAD's 'LAST FIRST & SECOND_FIRST' format -> both share surname."""
        result = normalize.format_owner_name("MEEKING STEVEN & GINA", source_format="last_first")
        assert result == "Meeking, Steven & Meeking, Gina"

    def test_dcad_format_joint_partner_with_middle(self):
        """'MOORE ALBERT & SUSIE MAE' -> SUSIE MAE inherits MOORE."""
        result = normalize.format_owner_name("MOORE ALBERT & SUSIE MAE", source_format="last_first")
        assert result == "Moore, Albert & Moore, Susie Mae"

    def test_dcad_format_joint_first_initial(self):
        """'FLORES JAIME O & MARIE G' -> both Flores."""
        result = normalize.format_owner_name("FLORES JAIME O & MARIE G", source_format="last_first")
        assert result == "Flores, Jaime O & Flores, Marie G"

    # --- Marital-status stripping ---
    def test_strips_single_man(self):
        assert normalize.format_owner_name("James N. Kun, a Single Man") == "Kun, James N."

    def test_strips_single_woman(self):
        assert normalize.format_owner_name("COLE A. MONTGOMERY, SINGLE WOMAN") == "Montgomery, Cole A."

    def test_strips_unmarried_woman(self):
        assert normalize.format_owner_name("JACQUELYN J. TAYLOR, UNMARRIED WOMAN") == "Taylor, Jacquelyn J."

    def test_strips_truncated_unmarried_woma(self):
        """OCR sometimes truncates 'WOMAN' to 'WOMA'."""
        assert normalize.format_owner_name("SKYLER OUSLEY, AN UNMARRIED WOMA") == "Ousley, Skyler"

    def test_strips_husband_and_wife(self):
        text = "JAIME O. FLORES AND MARIE G. FLORES, HUSBAND AND WIFE AS"
        assert normalize.format_owner_name(text) == "Flores, Jaime O. & Flores, Marie G."

    def test_strips_trailing_husband(self):
        text = "BREANNA MARIE BATTIE and WALLACE BRYAN CLARK II, HUSBAND"
        assert normalize.format_owner_name(text) == "Battie, Breanna Marie & Clark, Wallace Bryan II"

    def test_strips_as_her_sole(self):
        text = "_ | ADRIENNE PATRICE BROWN, A SINGLE WOMAN AS HER SOLE AND"
        assert normalize.format_owner_name(text) == "Brown, Adrienne Patrice"

    # --- OCR garbage prefixes ---
    def test_strips_leading_pipe(self):
        assert normalize.format_owner_name("| LUIS CARLOS ACUNA, A SINGLE MAN") == "Acuna, Luis Carlos"

    def test_strips_leading_underscore(self):
        text = "_ | ADRIENNE PATRICE BROWN, A SINGLE WOMAN AS HER SOLE AND"
        assert normalize.format_owner_name(text) == "Brown, Adrienne Patrice"

    # --- DECD / ESTATE markers ---
    def test_strips_decd_marker(self):
        # Auto-detects LAST FIRST via the DECD marker.
        assert normalize.format_owner_name("FLYNN LINDA BARFIELD DECD") == "Flynn, Linda Barfield"
        assert normalize.format_owner_name("NIXON JONATHAN BARRY DECD") == "Nixon, Jonathan Barry"

    def test_strips_trailing_estate(self):
        assert normalize.format_owner_name("MURDOCK DAVID H ESTATE") == "Murdock, David H"

    def test_strips_aka(self):
        assert normalize.format_owner_name("GORDON VERTIS ONETA DECD AKA") == "Gordon, Vertis Oneta"

    def test_preserves_life_estate_as_entity(self):
        """'LIFE ESTATE' is an entity marker — don't reformat."""
        assert normalize.format_owner_name(
            "STINSON BELINDA LIFE ESTATE",
            source_format="last_first",
        ) == "Stinson Belinda Life Estate"

    # --- Joint owners with own surnames (FIRST-LAST format) ---
    def test_joint_owners_both_surnames(self):
        result = normalize.format_owner_name("DAVID WALKER and LINDA E. WALKER")
        assert result == "Walker, David & Walker, Linda E."

    def test_joint_owners_via_spouse_clause(self):
        """X AND HIS/HER WIFE/HUSBAND, Y -> two owners."""
        text = "LUIS VELASQUEZ AND HIS WIFE, ZULMA ARRIAZA"
        result = normalize.format_owner_name(text)
        assert "Velasquez, Luis" in result
        assert "Arriaza" in result and "Zulma" in result
        assert " & " in result

    # --- Entity short-circuit ---
    def test_llc_preserved_uppercase(self):
        assert normalize.format_owner_name("GULF COAST WESTERN LLC") == "Gulf Coast Western LLC"

    def test_trust_not_reformatted(self):
        # Trusts shouldn't get their first token re-ordered as a surname.
        assert normalize.format_owner_name(
            "ARTHUR REVOCABLE TRUST THE", source_format="last_first"
        ) == "Arthur Revocable Trust The"
        assert normalize.format_owner_name(
            "JENSEN FAMILY TRUST", source_format="last_first"
        ) == "Jensen Family Trust"

    # --- Title-case edge cases ---
    def test_mc_prefix(self):
        assert normalize.format_owner_name("EVELYN MCNEIL") == "McNeil, Evelyn"
        assert normalize.format_owner_name("MCALLISTER MICHAEL LEN", source_format="last_first") == "McAllister, Michael Len"

    def test_mac_left_alone_for_short_name(self):
        """'MACK' is a regular surname — title-cased as 'Mack', not 'MacK'."""
        result = normalize.format_owner_name(
            "MCGUIRE MACK & TROSHANE", source_format="last_first"
        )
        assert result == "McGuire, Mack & McGuire, Troshane"

    def test_roman_numeral_suffix(self):
        text = "WALLACE BRYAN CLARK II"
        assert normalize.format_owner_name(text) == "Clark, Wallace Bryan II"

    # --- Truncation tails (OCR noise after the name) ---
    def test_strips_truncated_will_text(self):
        text = "LUCY J. LANGWELL provides thpt if"
        assert normalize.format_owner_name(text) == "Langwell, Lucy J."

    def test_strips_truncated_grantor_borrower_tail(self):
        text = "Larry J Woods Jr., a single man as"
        assert normalize.format_owner_name(text) == "Woods, Larry J Jr"

    # --- Invalid source_format ---
    def test_invalid_source_format_raises(self):
        with pytest.raises(ValueError):
            normalize.format_owner_name("Smith John", source_format="bogus")


