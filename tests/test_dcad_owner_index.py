"""Tests for ``scraper.dcad_owner_index``."""

from __future__ import annotations

import pytest

from scraper import dcad_owner_index as oi


# ============================================================================
# normalize_dcad_owner_name
# ============================================================================


class TestNormalizeOwnerName:
    def test_basic_uppercase(self):
        assert oi.normalize_dcad_owner_name("smith john a") == "SMITH JOHN A"

    def test_already_uppercase(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A") == "SMITH JOHN A"

    def test_strips_periods(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A.") == "SMITH JOHN A"

    def test_strips_commas(self):
        assert oi.normalize_dcad_owner_name("SMITH, JOHN A") == "SMITH JOHN A"

    def test_collapses_whitespace(self):
        assert oi.normalize_dcad_owner_name("SMITH   JOHN  A") == "SMITH JOHN A"

    def test_strips_trustee(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A TRUSTEE") == "SMITH JOHN A"

    def test_strips_etux(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A ETUX") == "SMITH JOHN A"
        assert oi.normalize_dcad_owner_name("SMITH JOHN A ET UX") == "SMITH JOHN A"

    def test_strips_etal(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A ETAL") == "SMITH JOHN A"

    def test_strips_family_trust(self):
        assert (
            oi.normalize_dcad_owner_name("SMITH JOHN A FAMILY TRUST")
            == "SMITH JOHN A"
        )

    def test_strips_living_trust(self):
        assert (
            oi.normalize_dcad_owner_name("SMITH JOHN A LIVING TRUST")
            == "SMITH JOHN A"
        )

    def test_strips_revocable_trust(self):
        assert (
            oi.normalize_dcad_owner_name("SMITH JOHN A REVOCABLE TRUST")
            == "SMITH JOHN A"
        )

    def test_strips_jr_sr(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A JR") == "SMITH JOHN A"
        assert oi.normalize_dcad_owner_name("SMITH JOHN A SR") == "SMITH JOHN A"

    def test_strips_roman_numerals(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN A III") == "SMITH JOHN A"
        assert oi.normalize_dcad_owner_name("SMITH JOHN A IV") == "SMITH JOHN A"

    def test_empty_returns_none(self):
        assert oi.normalize_dcad_owner_name("") is None
        assert oi.normalize_dcad_owner_name(None) is None
        assert oi.normalize_dcad_owner_name("   ") is None


# ============================================================================
# expand_joint_owners
# ============================================================================


class TestExpandJointOwners:
    def test_single_owner(self):
        assert oi.expand_joint_owners("SMITH JOHN A") == ["SMITH JOHN A"]

    def test_two_owners_shared_surname(self):
        # "MARY S" has 2 tokens, no surname -> inherit "SMITH"
        result = oi.expand_joint_owners("SMITH JOHN A & MARY S")
        assert result == ["SMITH JOHN A", "SMITH MARY S"]

    def test_two_owners_one_token_second(self):
        # "MARY" has 1 token, no surname -> inherit
        result = oi.expand_joint_owners("SMITH JOHN A & MARY")
        assert result == ["SMITH JOHN A", "SMITH MARY"]

    def test_two_owners_explicit_surname(self):
        # "JONES MARY S" has 3 tokens; assume own surname.
        result = oi.expand_joint_owners("SMITH JOHN A & JONES MARY S")
        assert result == ["SMITH JOHN A", "JONES MARY S"]

    def test_three_owners(self):
        result = oi.expand_joint_owners("SMITH JOHN & MARY & SUSAN")
        # JOHN has 1 token (so it's interpreted as first name only) but
        # it's the primary - kept as-is. MARY and SUSAN inherit SMITH.
        assert result == ["SMITH JOHN", "SMITH MARY", "SMITH SUSAN"]

    def test_empty(self):
        assert oi.expand_joint_owners("") == []
        assert oi.expand_joint_owners("   ") == []


# ============================================================================
# build_owner_index
# ============================================================================


class TestBuildOwnerIndex:
    def test_basic_index(self):
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "001", "OWNER_NAME": "SMITH JOHN A"},
                {"ACCOUNT_NUM": "002", "OWNER_NAME": "JONES MARY"},
            ]
        }
        index = oi.build_owner_index(tables)
        assert index == {
            "SMITH JOHN A": ["001"],
            "JONES MARY":   ["002"],
        }

    def test_joint_owners_split(self):
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "001", "OWNER_NAME": "SMITH JOHN A & MARY S"},
            ]
        }
        index = oi.build_owner_index(tables)
        # Both owners point at the same account.
        assert index["SMITH JOHN A"] == ["001"]
        assert index["SMITH MARY S"] == ["001"]

    def test_role_suffix_stripped(self):
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "001", "OWNER_NAME": "SMITH JOHN A TRUSTEE"},
            ]
        }
        index = oi.build_owner_index(tables)
        assert "SMITH JOHN A" in index
        # Original form with TRUSTEE should NOT be a key.
        assert "SMITH JOHN A TRUSTEE" not in index

    def test_same_owner_multiple_properties(self):
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "001", "OWNER_NAME": "SMITH JOHN A"},
                {"ACCOUNT_NUM": "002", "OWNER_NAME": "SMITH JOHN A"},
            ]
        }
        index = oi.build_owner_index(tables)
        assert sorted(index["SMITH JOHN A"]) == ["001", "002"]

    def test_alternative_field_names(self):
        # Schema with "OWNER_NAME1" instead of "OWNER_NAME".
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUMBER": "001", "OWNER_NAME1": "SMITH JOHN A"},
            ]
        }
        index = oi.build_owner_index(tables)
        assert index["SMITH JOHN A"] == ["001"]

    def test_rows_with_missing_data_skipped(self):
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "001", "OWNER_NAME": "SMITH JOHN A"},
                {"ACCOUNT_NUM": "002", "OWNER_NAME": ""},          # skip
                {"ACCOUNT_NUM": "",    "OWNER_NAME": "JONES MARY"},# skip
                {"ACCOUNT_NUM": "003", "OWNER_NAME": "JONES MARY"},
            ]
        }
        index = oi.build_owner_index(tables)
        assert index == {
            "SMITH JOHN A": ["001"],
            "JONES MARY":   ["003"],
        }

    def test_missing_table_returns_empty(self):
        tables = {}  # no ACCOUNT_INFO
        assert oi.build_owner_index(tables) == {}

    def test_empty_table_returns_empty(self):
        tables = {"ACCOUNT_INFO": []}
        assert oi.build_owner_index(tables) == {}


# ============================================================================
# inspect_account_info_fields
# ============================================================================


class TestInspectFields:
    def test_diagnostic_output(self):
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "001", "OWNER_NAME": "SMITH JOHN A",
                 "STREET": "100 MAIN"},
                {"ACCOUNT_NUM": "002", "OWNER_NAME": "JONES MARY",
                 "STREET": "200 OAK"},
            ]
        }
        result = oi.inspect_account_info_fields(tables)
        assert result["row_count"] == 2
        assert result["matched_owner_field"] == "OWNER_NAME"
        assert result["matched_account_field"] == "ACCOUNT_NUM"
        assert "OWNER_NAME" in result["keys_observed"]
        assert "STREET" in result["keys_observed"]

    def test_empty_table(self):
        tables = {"ACCOUNT_INFO": []}
        result = oi.inspect_account_info_fields(tables)
        assert result["row_count"] == 0
        assert result["sample_rows"] == []
        assert result["matched_owner_field"] is None


# ═══════════════════════════════════════════════════════════════════════════
# PR 8 Phase 1 — OWNER_NAME2, MULTI_OWNER, ESTATE OF
# ═══════════════════════════════════════════════════════════════════════════
#
# PR 8 audit on 2026-05-27 GHA run found 21/50 PB records unresolved.
# Several (Steinhart-class) had the decedent as OWNER_NAME2 — we missed
# them because the index only read OWNER_NAME1. ESTATE OF patterns
# similarly disappeared in normalization.


class TestEstateOfNormalization:
    """PR 8: leading and trailing 'ESTATE OF' / 'EST OF' / 'ESTATE'
    forms collapse to the underlying decedent name."""

    def test_leading_estate_of(self):
        assert oi.normalize_dcad_owner_name("ESTATE OF JOHN SMITH") == "JOHN SMITH"

    def test_leading_estate_of_case_insensitive(self):
        assert oi.normalize_dcad_owner_name("estate of john smith") == "JOHN SMITH"

    def test_trailing_estate_of(self):
        assert oi.normalize_dcad_owner_name("JOHN SMITH ESTATE OF") == "JOHN SMITH"

    def test_trailing_est_of(self):
        """Real data observed: 'BELL ANGELA B EST OF' at 611 MATTHEW PL."""
        assert oi.normalize_dcad_owner_name("BELL ANGELA B EST OF") == "BELL ANGELA B"

    def test_trailing_estate_no_of(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN ESTATE") == "SMITH JOHN"

    def test_trailing_est_no_of(self):
        assert oi.normalize_dcad_owner_name("SMITH JOHN EST") == "SMITH JOHN"

    def test_le_surname_not_consumed_by_estate(self):
        """Vietnamese surname 'LE' must not be eaten by leading-estate
        or any estate-related pattern."""
        assert oi.normalize_dcad_owner_name("LE MINH HUNG") == "LE MINH HUNG"

    def test_existing_role_suffixes_still_work(self):
        """Regression: TRUSTEE etc. still strip correctly."""
        assert oi.normalize_dcad_owner_name("SMITH JOHN A TRUSTEE") == "SMITH JOHN A"


class TestBuildOwnerIndexWithSecondaryOwners:
    """PR 8: index OWNER_NAME2 + MULTI_OWNER so joint-property decedents
    resolve through Path A."""

    def test_owner_name2_indexed_at_same_account(self):
        """Steinhart case: Ronald is NAME1, Phyllis is NAME2. Both
        index under the same account so a decedent search on Phyllis
        finds the property."""
        tables = {
            "ACCOUNT_INFO": [
                {
                    "ACCOUNT_NUM": "acct_robledo",
                    "OWNER_NAME1": "STEINHART RONALD G",
                    "OWNER_NAME2": "STEINHART PHYLLIS Y",
                },
            ],
        }
        idx = oi.build_owner_index(tables)
        assert idx.get("STEINHART RONALD G") == ["acct_robledo"]
        assert idx.get("STEINHART PHYLLIS Y") == ["acct_robledo"]

    def test_owner_name2_empty_does_not_break(self):
        """A row with NAME1 but no NAME2 should still index NAME1."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME1": "DOE JANE", "OWNER_NAME2": ""},
            ],
        }
        idx = oi.build_owner_index(tables)
        assert idx == {"DOE JANE": ["a1"]}

    def test_multi_owner_table_indexed(self):
        """MULTI_OWNER captures 3rd+ joint owners (condos, family
        trust co-owners). Decedents holding 1/4 interest in a family
        property would only appear here."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "acct_family",
                 "OWNER_NAME1": "DOE JANE",
                 "OWNER_NAME2": "DOE JOHN"},
            ],
            "MULTI_OWNER": [
                {"ACCOUNT_NUM": "acct_family", "OWNER_SEQ_NUM": "3",
                 "OWNER_NAME": "DOE ALICE"},
                {"ACCOUNT_NUM": "acct_family", "OWNER_SEQ_NUM": "4",
                 "OWNER_NAME": "DOE BOB"},
            ],
        }
        idx = oi.build_owner_index(tables)
        assert idx["DOE JANE"]  == ["acct_family"]
        assert idx["DOE JOHN"]  == ["acct_family"]
        assert idx["DOE ALICE"] == ["acct_family"]
        assert idx["DOE BOB"]   == ["acct_family"]

    def test_same_owner_in_multiple_columns_deduplicated(self):
        """If a name appears in OWNER_NAME1 AND in MULTI_OWNER for the
        same account, we should record the account once — not twice."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME1": "DOE JANE"},
            ],
            "MULTI_OWNER": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME": "DOE JANE"},
            ],
        }
        idx = oi.build_owner_index(tables)
        assert idx["DOE JANE"] == ["a1"]  # not ["a1", "a1"]

    def test_same_owner_multiple_accounts_preserved(self):
        """An owner with two distinct properties still gets both
        accounts in the list."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME1": "DOE JANE"},
                {"ACCOUNT_NUM": "a2", "OWNER_NAME1": "DOE JANE"},
            ],
        }
        idx = oi.build_owner_index(tables)
        assert set(idx["DOE JANE"]) == {"a1", "a2"}

    def test_estate_of_owner_indexed_under_decedent(self):
        """A property already retitled to 'ESTATE OF JOHN SMITH' in
        DCAD should still match a probate search for 'John Smith'."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "acct_est",
                 "OWNER_NAME1": "ESTATE OF SMITH JOHN"},
            ],
        }
        idx = oi.build_owner_index(tables)
        assert idx.get("SMITH JOHN") == ["acct_est"]

    def test_multi_owner_table_missing_handled(self):
        """Backwards compat: MULTI_OWNER table not present in fixtures
        (and not yet in older DCAD bulk releases) → owner_index still
        builds from ACCOUNT_INFO."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME1": "DOE JANE"},
            ],
        }
        idx = oi.build_owner_index(tables)
        assert idx == {"DOE JANE": ["a1"]}

    def test_explicit_multi_owner_table_none_disables_lookup(self):
        """Callers can pass multi_owner_table=None to opt out (e.g.
        unit tests of the legacy code path)."""
        tables = {
            "ACCOUNT_INFO": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME1": "DOE JANE"},
            ],
            "MULTI_OWNER": [
                {"ACCOUNT_NUM": "a1", "OWNER_NAME": "DOE BOB"},
            ],
        }
        idx = oi.build_owner_index(tables, multi_owner_table=None)
        assert idx == {"DOE JANE": ["a1"]}
        # Bob is in MULTI_OWNER but we opted out → not indexed
        assert "DOE BOB" not in idx
