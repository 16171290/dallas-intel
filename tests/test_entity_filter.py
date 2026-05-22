"""Unit tests for scraper.entity_filter."""
from __future__ import annotations

import pytest

from scraper import entity_filter
from scraper.entity_filter import (
    filter_entity_records,
    has_commercial_address_suffix,
    is_corporate_entity,
    is_numeric_garbage,
)


# ═══════════════════════════════════════════════════════════════════════════
# is_corporate_entity - positive cases (real entities)
# ═══════════════════════════════════════════════════════════════════════════

class TestIsCorporateEntityPositive:
    def test_llc_suffix(self):
        assert is_corporate_entity("NIMAR PROPERTIES LLC")
        assert is_corporate_entity("ACME LLC")

    def test_llc_dotted(self):
        assert is_corporate_entity("ACME L.L.C.")

    def test_inc_suffix(self):
        assert is_corporate_entity("ACME ROOFING INC")
        assert is_corporate_entity("ACME INCORPORATED")

    def test_corp_suffix(self):
        assert is_corporate_entity("ACME CORP")
        assert is_corporate_entity("ACME CORPORATION")

    def test_ltd_suffix(self):
        assert is_corporate_entity("ACME LTD")
        assert is_corporate_entity("ACME LIMITED")

    def test_company_suffix(self):
        assert is_corporate_entity("ACME COMPANY")

    def test_holdings(self):
        assert is_corporate_entity("DALLAS HOLDINGS GROUP")

    def test_properties(self):
        assert is_corporate_entity("WINDING CREEK PROPERTIES")

    def test_investments(self):
        assert is_corporate_entity("KANDI AMERICA INVESTMENT LLC")

    def test_partners(self):
        assert is_corporate_entity("DALFEN INDUSTRIAL PARTNERS")

    def test_pipeline(self):
        assert is_corporate_entity("ENTERPRISE PRODUCTS PIPELINE")

    def test_synergy(self):
        assert is_corporate_entity("SYNERGY HOMEBUILDERS")

    def test_multifamily(self):
        assert is_corporate_entity("DALLAS MULTIFAMILY VENTURES")


class TestIsCorporateEntityFinancial:
    def test_bank(self):
        assert is_corporate_entity("CARRINGTON MORTGAGE BANK")
        assert is_corporate_entity("U S BANK TRUST")

    def test_mortgage(self):
        assert is_corporate_entity("CARRINGTON MORTGAGE LLC")

    def test_capital(self):
        assert is_corporate_entity("REGIONS CAPITAL ADVISORS")

    def test_lending(self):
        assert is_corporate_entity("PROSPER LENDING SOLUTIONS")

    def test_freddie_mac(self):
        assert is_corporate_entity("FREDDIE MAC")

    def test_fannie_mae(self):
        assert is_corporate_entity("FANNIE MAE")

    def test_credit_union(self):
        assert is_corporate_entity("AMERICA FIRST CREDIT UNION")

    def test_trust_co(self):
        assert is_corporate_entity("U S BANK TRUST COMPANY NATIONAL ASSOCIATION TR")

    def test_national_association(self):
        assert is_corporate_entity("WELLS FARGO NATIONAL ASSOCIATION")

    def test_n_a_abbreviation(self):
        assert is_corporate_entity("WELLS FARGO N.A.")


class TestIsCorporateEntityContractors:
    def test_construction(self):
        assert is_corporate_entity("REGENT CONSTRUCTION")

    def test_roofing(self):
        assert is_corporate_entity("DFW ROOFING SERVICES")

    def test_plumbing(self):
        assert is_corporate_entity("METRO PLUMBING")

    def test_electric(self):
        assert is_corporate_entity("DALLAS ELECTRIC CO")

    def test_contractors(self):
        assert is_corporate_entity("BIG D CONTRACTORS")

    def test_builders(self):
        assert is_corporate_entity("MERITAGE HOMES BUILDERS")

    def test_services_plural(self):
        assert is_corporate_entity("HVAC SERVICES INC")

    def test_realty(self):
        assert is_corporate_entity("KELLER WILLIAMS REALTY")

    def test_painting(self):
        assert is_corporate_entity("PRO PAINTING LLC")


class TestIsCorporateEntityInstitutional:
    def test_church(self):
        assert is_corporate_entity("ST MARY COPTIC CHURCH")

    def test_bible(self):
        assert is_corporate_entity("DALLAS BIBLE INSTITUTE")

    def test_hospital(self):
        assert is_corporate_entity("PARKLAND HOSPITAL")

    def test_health_system(self):
        assert is_corporate_entity("TEXAS HEALTH SYSTEM")


class TestIsCorporateEntityRoles:
    def test_trustee_suffix(self):
        assert is_corporate_entity("SOUTHEAST TRUSTEE SERVICES TRUSTEE")
        assert is_corporate_entity("DOE JOHN TRUSTEE")

    def test_tr_suffix(self):
        assert is_corporate_entity("DOE JANE TR")

    def test_agent_suffix(self):
        assert is_corporate_entity("BIG TITLE COMPANY AGENT")


# ═══════════════════════════════════════════════════════════════════════════
# is_corporate_entity - negative cases (real individuals)
# ═══════════════════════════════════════════════════════════════════════════

class TestIsCorporateEntityNegative:
    def test_typical_individual(self):
        assert not is_corporate_entity("HERNANDEZ LESLIE")

    def test_individual_with_middle_initial(self):
        assert not is_corporate_entity("SMITH JOHN A")

    def test_individual_with_suffix_jr(self):
        # JR alone is not in our entity patterns (that's name_matcher territory)
        assert not is_corporate_entity("SMITH JOHN A JR")

    def test_two_individuals_with_and(self):
        assert not is_corporate_entity("SMITH JOHN & MARY")

    def test_ethnic_name_with_le(self):
        # Vietnamese surname LE - should NOT match anything
        assert not is_corporate_entity("LE NGUYEN")

    def test_individual_with_co_lowercase_word_inside(self):
        # "COLE" contains "CO" but should not match \bCO\.?$ at end
        assert not is_corporate_entity("COLE PAMELA")

    def test_individual_with_homes_lastname(self):
        # Could false-positive on \bHOMES\b - we accepted this risk per audit
        # Test that the failure mode is what we expect (this WILL match)
        assert is_corporate_entity("HOMES MARY")
        # Mitigation: most real homeowners don't have HOMES as part of their name


class TestIsCorporateEntityEmpty:
    def test_none_returns_false(self):
        assert not is_corporate_entity(None)

    def test_empty_returns_false(self):
        assert not is_corporate_entity("")

    def test_whitespace_returns_false(self):
        assert not is_corporate_entity("   ")


# ═══════════════════════════════════════════════════════════════════════════
# Family-trust escape - these should NOT be classified as entities
# ═══════════════════════════════════════════════════════════════════════════

class TestFamilyTrustEscape:
    def test_family_trust(self):
        # Real motivated-seller candidate, not an entity
        assert not is_corporate_entity("SMITH FAMILY TRUST")

    def test_living_trust(self):
        assert not is_corporate_entity("JONES LIVING TRUST")

    def test_revocable_trust(self):
        assert not is_corporate_entity("MILLER REVOCABLE TRUST")

    def test_irrevocable_trust(self):
        assert not is_corporate_entity("ANDERSON IRREVOCABLE TRUST")

    def test_testamentary_trust(self):
        assert not is_corporate_entity("WILSON TESTAMENTARY TRUST")

    def test_family_trust_with_co_suffix_escapes_first(self):
        # Even with "TRUST CO" pattern present, family-trust escape wins
        assert not is_corporate_entity("SMITH FAMILY TRUST CO")


# ═══════════════════════════════════════════════════════════════════════════
# is_numeric_garbage
# ═══════════════════════════════════════════════════════════════════════════

class TestIsNumericGarbage:
    def test_pure_digits(self):
        assert is_numeric_garbage("9999999999")
        assert is_numeric_garbage("12345")

    def test_digits_with_whitespace(self):
        assert is_numeric_garbage("  9999999999  ")

    def test_digits_with_hyphens(self):
        assert is_numeric_garbage("123-456-789")

    def test_digits_with_underscores(self):
        assert is_numeric_garbage("123_456")

    def test_mixed_returns_false(self):
        assert not is_numeric_garbage("123 SMITH")
        assert not is_numeric_garbage("SMITH JOHN")

    def test_real_individual_returns_false(self):
        assert not is_numeric_garbage("HERNANDEZ LESLIE")

    def test_empty_returns_false(self):
        assert not is_numeric_garbage("")
        assert not is_numeric_garbage(None)
        assert not is_numeric_garbage("   ")


# ═══════════════════════════════════════════════════════════════════════════
# has_commercial_address_suffix
# ═══════════════════════════════════════════════════════════════════════════

class TestHasCommercialAddressSuffix:
    def test_ste(self):
        assert has_commercial_address_suffix("POLK CHRISTI E STE 131")

    def test_suite(self):
        assert has_commercial_address_suffix("LAW OFFICE SUITE 200")

    def test_unit(self):
        assert has_commercial_address_suffix("SMITH JOHN UNIT B")

    def test_apt(self):
        assert has_commercial_address_suffix("DOE JANE APT 5")

    def test_bldg(self):
        assert has_commercial_address_suffix("WILLIAMS BLDG 3")

    def test_hash_number(self):
        assert has_commercial_address_suffix("JONES MARY #5")

    def test_regular_name_no_suffix(self):
        assert not has_commercial_address_suffix("HERNANDEZ LESLIE")

    def test_empty_returns_false(self):
        assert not has_commercial_address_suffix("")
        assert not has_commercial_address_suffix(None)


# ═══════════════════════════════════════════════════════════════════════════
# filter_entity_records - the main filter function
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterEntityRecords:
    def test_keeps_individual_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X LLC", "grantee": "HERNANDEZ LESLIE"}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 1
        assert len(removed) == 0

    def test_removes_llc_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X", "grantee": "ACME PROPERTIES LLC"}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_removes_bank_grantee(self):
        records = [{"dallas_code": "TD", "grantor": "X", "grantee": "U S BANK TRUST NATIONAL ASSOCIATION TR"}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_removes_empty_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X", "grantee": ""}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_removes_none_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X", "grantee": None}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_removes_numeric_garbage_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X", "grantee": "9999999999"}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_removes_commercial_suffix_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X", "grantee": "POLK CHRISTI E STE 131"}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_keeps_family_trust_grantee(self):
        records = [{"dallas_code": "LP", "grantor": "X", "grantee": "SMITH FAMILY TRUST"}]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 1
        assert len(removed) == 0


class TestLCCityLienSwap:
    """LC code city-lien swap: when grantee is governmental, check grantor."""

    def test_lc_with_individual_grantor_governmental_grantee_kept(self):
        # The actual property owner is the grantor; swap should preserve it
        records = [{
            "dallas_code": "LC",
            "grantor": "JOHNSON ROBERT",
            "grantee": "CITY OF DALLAS",
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 1, "LC with individual grantor and gov grantee should be KEPT via swap"

    def test_lc_with_entity_grantor_governmental_grantee_removed(self):
        # Swap finds grantor is also an entity -> remove
        records = [{
            "dallas_code": "LC",
            "grantor": "DEVELOPER HOLDINGS LLC",
            "grantee": "CITY OF DALLAS",
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_lc_with_individual_grantee_kept_normally(self):
        # Normal LC path: grantee is not governmental, use grantee as target
        records = [{
            "dallas_code": "LC",
            "grantor": "ACME LLC",
            "grantee": "SMITH JOHN",
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 1

    def test_lc_with_entity_grantee_removed_normally(self):
        # LC with corporate grantee, non-governmental: standard remove
        records = [{
            "dallas_code": "LC",
            "grantor": "SMITH JOHN",
            "grantee": "ACME LLC",
        }]
        kept, removed = filter_entity_records(records)
        # No swap because grantee isn't governmental
        assert len(kept) == 0
        assert len(removed) == 1

    def test_nof_swap_keeps_individual_grantor(self):
        # NOF (Notice of Foreclosure): grantee is always absent (no buyer yet).
        # The motivated-seller lead is the grantor (homeowner). Without the
        # NOF swap added in PR 12.7, every foreclosure record would be dropped
        # for empty grantee on line ~291 of entity_filter.
        records = [{
            "dallas_code": "NOF",
            "grantor": "DAVID WALKER and LINDA E. WALKER",
            "grantee": None,
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 1
        assert len(removed) == 0

    def test_nof_swap_removes_corporate_grantor(self):
        # Commercial-entity foreclosures (e.g. PARKER & PARKER REAL ESTATE LLC)
        # are NOT motivated-seller leads -- they're business loans defaulting.
        # The NOF swap should still apply the corporate-entity check.
        records = [{
            "dallas_code": "NOF",
            "grantor": "PARKER & PARKER REAL ESTATE, LLC",
            "grantee": None,
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_nof_empty_grantor_still_kept(self):
        # When OCR couldn't extract the owner name (~50% of records), the
        # record still has value: sale_date, source_url, sometimes
        # city-level address. Operator clicks the source URL to find the
        # name manually. Don't drop these.
        records = [{
            "dallas_code": "NOF",
            "grantor": None,
            "grantee": None,
            "record_id": "315566857",
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 1, "NOF with empty grantor must be kept (OCR-miss not record-invalid)"

    def test_non_nof_empty_target_still_removed(self):
        # The empty-target removal is preserved for non-NOF codes. A WD
        # (warranty deed) with no grantee is junk.
        records = [{
            "dallas_code": "WD",
            "grantor": "JONES SAM",
            "grantee": None,
        }]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 0
        assert len(removed) == 1

    def test_non_lc_code_does_not_swap(self):
        # LP with governmental grantee - NO swap, remove
        records = [{
            "dallas_code": "LP",
            "grantor": "JOHNSON ROBERT",
            "grantee": "CITY OF DALLAS",
        }]
        kept, removed = filter_entity_records(records)
        # Governmental grantee triggers no entity match in entity_filter
        # (because gov entities aren't in our pattern list), but governmental
        # grantor filter would handle this earlier. Here we test entity_filter
        # behavior in isolation:
        # The "CITY OF DALLAS" - does our pattern list catch it?
        # Looking at patterns: no city-of pattern in entity_filter.
        # But governmental_grantor.is_governmental_grantor("CITY OF DALLAS") = True
        # And our _resolve_target for non-LC code uses grantee.
        # is_corporate_entity("CITY OF DALLAS") = False (no entity pattern match)
        # So this record passes through entity_filter even with a gov grantee.
        # In production this gets caught by governmental_grantor (which checks grantor).
        # If we wanted to also drop gov grantees we'd need a separate filter.
        # Documenting current behavior:
        assert len(kept) == 1, "Non-LC gov grantee passes entity_filter (governmental_grantor handles this elsewhere)"


class TestFilterEntityRecordsBatch:
    def test_mixed_batch(self):
        records = [
            {"dallas_code": "LP", "grantor": "X", "grantee": "HERNANDEZ LESLIE"},  # keep
            {"dallas_code": "LP", "grantor": "Y", "grantee": "ACME LLC"},  # drop
            {"dallas_code": "AJ", "grantor": "Z", "grantee": "SMITH JOHN"},  # keep
            {"dallas_code": "TD", "grantor": "W", "grantee": "U S BANK TRUST"},  # drop
            {"dallas_code": "LP", "grantor": "V", "grantee": "9999999999"},  # drop
            {"dallas_code": "LP", "grantor": "U", "grantee": "ANDERSON FAMILY TRUST"},  # keep
        ]
        kept, removed = filter_entity_records(records)
        assert len(kept) == 3
        assert len(removed) == 3

    def test_empty_list(self):
        kept, removed = filter_entity_records([])
        assert kept == []
        assert removed == []
