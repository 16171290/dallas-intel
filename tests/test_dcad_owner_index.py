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
