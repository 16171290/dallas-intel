"""Unit tests for scraper.legal_resolver."""
from __future__ import annotations

import pandas as pd
import pytest

from scraper import legal_resolver
from scraper.legal_resolver import (
    LegalResolverStats,
    ParsedLegal,
    build_legal_index,
    normalize_subdivision,
    parse_legal_from_snippet,
    resolve_legal_descriptions,
    _generate_subdivision_variants,
    _parse_dcad_legal2,
    _build_account_to_address,
)


# ═══════════════════════════════════════════════════════════════════════════
# normalize_subdivision
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalizeSubdivision:
    def test_empty_returns_empty(self):
        assert normalize_subdivision("") == ""
        assert normalize_subdivision(None) == ""

    def test_uppercase(self):
        assert normalize_subdivision("south side") == "SOUTH SIDE"

    def test_strips_whitespace(self):
        assert normalize_subdivision("  WINDING CREEK  ") == "WINDING CREEK"

    def test_collapses_internal_whitespace(self):
        assert normalize_subdivision("PLEASANT  VALLEY   ESTATES") == "PLEASANT VALLEY ESTATES"

    def test_ordinal_word_to_number_first(self):
        assert normalize_subdivision("FIRST INSTALLMENT") == "1ST INST"

    def test_ordinal_word_to_number_second(self):
        assert normalize_subdivision("SECOND PHASE") == "2ND PH"

    def test_ordinal_word_to_number_through_tenth(self):
        assert normalize_subdivision("THIRD ADDITION") == "3RD ADDN"
        assert normalize_subdivision("FOURTH SECTION") == "4TH SEC"
        assert normalize_subdivision("FIFTH") == "5TH"
        assert normalize_subdivision("SIXTH") == "6TH"
        assert normalize_subdivision("SEVENTH") == "7TH"
        assert normalize_subdivision("EIGHTH") == "8TH"
        assert normalize_subdivision("NINTH") == "9TH"
        assert normalize_subdivision("TENTH") == "10TH"

    def test_addition_abbreviation(self):
        assert normalize_subdivision("OAK PARK ADDITION") == "OAK PARK ADDN"

    def test_installment_abbreviation(self):
        assert normalize_subdivision("FIRST INSTALLMENT") == "1ST INST"

    def test_phase_abbreviation(self):
        assert normalize_subdivision("RIVER OAKS PHASE 2") == "RIVER OAKS PH 2"

    def test_section_abbreviation(self):
        assert normalize_subdivision("LAKE RIDGE SECTION 3") == "LAKE RIDGE SEC 3"

    def test_number_abbreviation(self):
        assert normalize_subdivision("THE PENINSULA NUMBER 6") == "THE PENINSULA NO 6"

    def test_strips_punctuation(self):
        assert normalize_subdivision("OAK PARK, ADDITION.") == "OAK PARK ADDN"


# ═══════════════════════════════════════════════════════════════════════════
# _generate_subdivision_variants
# ═══════════════════════════════════════════════════════════════════════════

class TestSubdivisionVariants:
    def test_base_always_included(self):
        variants = _generate_subdivision_variants("SOUTH SIDE")
        assert "SOUTH SIDE" in variants

    def test_x_of_y_reordering(self):
        # "1ST INSTALLMENT OF EAST KESSLER PARK" should also try
        # "EAST KESSLER PARK 1ST INST"
        variants = _generate_subdivision_variants("FIRST INSTALLMENT OF EAST KESSLER PARK")
        assert "1ST INST OF EAST KESSLER PARK" in variants
        assert "EAST KESSLER PARK 1ST INST" in variants

    def test_generic_of_reordering(self):
        variants = _generate_subdivision_variants("HEIGHTS OF DALLAS")
        assert "HEIGHTS OF DALLAS" in variants
        assert "DALLAS HEIGHTS" in variants

    def test_no_of_pattern_returns_single_variant(self):
        variants = _generate_subdivision_variants("SOUTH SIDE")
        assert variants == {"SOUTH SIDE"}


# ═══════════════════════════════════════════════════════════════════════════
# _parse_dcad_legal2
# ═══════════════════════════════════════════════════════════════════════════

class TestParseDcadLegal2:
    def test_empty_returns_empty(self):
        assert _parse_dcad_legal2("") == {"lot": "", "block": ""}
        assert _parse_dcad_legal2(None) == {"lot": "", "block": ""}

    def test_lt_only(self):
        # "LT 0033" - lot only, leading zeros stripped
        assert _parse_dcad_legal2("LT 0033") == {"lot": "33", "block": ""}

    def test_lt_zero(self):
        # "LT 0" - lot 0 preserves as "0", not empty
        assert _parse_dcad_legal2("LT 0000") == {"lot": "0", "block": ""}

    def test_blk_lt(self):
        # "BLK H LT 38"
        assert _parse_dcad_legal2("BLK H LT 38") == {"lot": "38", "block": "H"}

    def test_numeric_block(self):
        # "BLK 1 LOT 1"
        assert _parse_dcad_legal2("BLK 1 LOT 1") == {"lot": "1", "block": "1"}

    def test_block_with_slash_keeps_first_segment(self):
        # "BLK A/1694 PT LOT 20" - block "A", lot "20"
        assert _parse_dcad_legal2("BLK A/1694 PT LOT 20") == {"lot": "20", "block": "A"}

    def test_block_with_slash_numeric_first(self):
        # "BLK 2/8291 LT 2 LESS ROW"
        assert _parse_dcad_legal2("BLK 2/8291 LT 2 LESS ROW") == {"lot": "2", "block": "2"}

    def test_tract_treated_as_lot(self):
        # "TR 24" - tract treated as lot, no block
        assert _parse_dcad_legal2("TR 24") == {"lot": "24", "block": ""}

    def test_tract_full_word(self):
        # "TRACT 24"
        assert _parse_dcad_legal2("TRACT 24") == {"lot": "24", "block": ""}

    def test_block_word_full(self):
        # BLOCK (full word) variant
        assert _parse_dcad_legal2("BLOCK A LT 5") == {"lot": "5", "block": "A"}

    def test_no_match_returns_empty(self):
        # Unstructured text - parser returns empty values
        assert _parse_dcad_legal2("RANDOM TEXT WITH NO PATTERN") == {"lot": "", "block": ""}


# ═══════════════════════════════════════════════════════════════════════════
# parse_legal_from_snippet
# ═══════════════════════════════════════════════════════════════════════════

class TestParseLegalFromSnippet:
    def test_none_returns_none(self):
        assert parse_legal_from_snippet(None) is None

    def test_empty_returns_none(self):
        assert parse_legal_from_snippet("") is None
        assert parse_legal_from_snippet("   ") is None

    def test_na_pipe_na_returns_empty_parsed(self):
        # AJ records have "N/A | N/A" - parseable but no content
        result = parse_legal_from_snippet("N/A | N/A")
        assert isinstance(result, ParsedLegal)
        assert result.subdivision == ""
        assert result.lot == ""
        assert result.block == ""

    def test_full_snippet(self):
        snippet = "DALLAS | Subdivision - Name: SOUTH SIDE Lot: 20 Block: A Township: DALLAS"
        result = parse_legal_from_snippet(snippet)
        assert result is not None
        assert result.subdivision == "SOUTH SIDE"
        assert result.lot == "20"
        assert result.block == "A"
        assert result.township == "DALLAS"

    def test_no_block_in_snippet(self):
        snippet = "DALLAS | Subdivision - Name: SOMEWHERE Lot: 5 Township: DALLAS"
        result = parse_legal_from_snippet(snippet)
        assert result is not None
        assert result.subdivision == "SOMEWHERE"
        assert result.lot == "5"
        assert result.block == ""

    def test_with_reference(self):
        # Reference suffix should not be captured into subdivision name
        snippet = "DALLAS | Subdivision - Name: WINDING CREEK Lot: 1 Block: A Township: GRAND PRAIRIE Reference - 201800111371/"
        result = parse_legal_from_snippet(snippet)
        assert result is not None
        assert result.subdivision == "WINDING CREEK"
        assert "Reference" not in result.subdivision

    def test_township_only_no_subdivision(self):
        # Records that only have a township (no subdivision/lot/block)
        snippet = "DALLAS | Township: DALLAS Reference - 2005159/9904"
        result = parse_legal_from_snippet(snippet)
        assert result is not None
        assert result.subdivision == ""

    def test_alphanumeric_lot(self):
        snippet = "DALLAS | Subdivision - Name: ROYAL TECH Lot: 1RA Block: C Township: DALLAS"
        result = parse_legal_from_snippet(snippet)
        assert result is not None
        assert result.lot == "1RA"


# ═══════════════════════════════════════════════════════════════════════════
# build_legal_index
# ═══════════════════════════════════════════════════════════════════════════

def _make_dcad_tables(account_info_rows: list[dict]) -> dict[str, pd.DataFrame]:
    """Helper: build a minimal DCAD-tables fixture for testing."""
    return {"ACCOUNT_INFO": pd.DataFrame(account_info_rows)}


class TestBuildLegalIndex:
    def test_empty_tables(self):
        assert build_legal_index({}) == {}

    def test_missing_account_info(self):
        # No ACCOUNT_INFO table
        tables = {"OTHER_TABLE": pd.DataFrame([{"X": "1"}])}
        assert build_legal_index(tables) == {}

    def test_missing_columns(self):
        # ACCOUNT_INFO present but missing required columns
        tables = {"ACCOUNT_INFO": pd.DataFrame([{"ACCOUNT_NUM": "123"}])}
        assert build_legal_index(tables) == {}

    def test_single_record(self):
        tables = _make_dcad_tables([{
            "ACCOUNT_NUM": "00000169927000000",
            "LEGAL1": "SOUTH SIDE",
            "LEGAL2": "BLK A/1694 PT LOT 20",
            "STREET_NUM": "3411",
            "FULL_STREET_NAME": "S MALCOLM X BLVD",
        }])
        index = build_legal_index(tables)
        assert ("SOUTH SIDE", "20", "A") in index
        assert index[("SOUTH SIDE", "20", "A")] == ["00000169927000000"]

    def test_multiple_records_same_key(self):
        # Two accounts share the same legal description (condos, etc.)
        tables = _make_dcad_tables([
            {"ACCOUNT_NUM": "111", "LEGAL1": "CONDO X", "LEGAL2": "LT 5"},
            {"ACCOUNT_NUM": "222", "LEGAL1": "CONDO X", "LEGAL2": "LT 5"},
        ])
        index = build_legal_index(tables)
        key = ("CONDO X", "5", "")
        assert key in index
        assert sorted(index[key]) == ["111", "222"]

    def test_skips_rows_without_subdivision(self):
        # LEGAL1 empty -> skipped
        tables = _make_dcad_tables([
            {"ACCOUNT_NUM": "111", "LEGAL1": "", "LEGAL2": "LT 5"},
            {"ACCOUNT_NUM": "222", "LEGAL1": "REAL SUB", "LEGAL2": "LT 1"},
        ])
        index = build_legal_index(tables)
        # Only the second row should appear
        assert len(index) == 1
        assert ("REAL SUB", "1", "") in index

    def test_skips_rows_without_account_num(self):
        tables = _make_dcad_tables([
            {"ACCOUNT_NUM": "", "LEGAL1": "SUB", "LEGAL2": "LT 1"},
        ])
        index = build_legal_index(tables)
        assert index == {}

    def test_normalization_applied_to_index_keys(self):
        # DCAD LEGAL1 uses "ADDITION" - index should key on "ADDN"
        tables = _make_dcad_tables([{
            "ACCOUNT_NUM": "111",
            "LEGAL1": "OAK PARK ADDITION",
            "LEGAL2": "LT 1",
        }])
        index = build_legal_index(tables)
        # Should be normalized
        assert ("OAK PARK ADDN", "1", "") in index
        assert ("OAK PARK ADDITION", "1", "") not in index


# ═══════════════════════════════════════════════════════════════════════════
# _build_account_to_address
# ═══════════════════════════════════════════════════════════════════════════

class TestAccountToAddress:
    def test_empty_tables(self):
        assert _build_account_to_address({}) == {}

    def test_missing_columns(self):
        tables = {"ACCOUNT_INFO": pd.DataFrame([{"ACCOUNT_NUM": "111"}])}
        assert _build_account_to_address(tables) == {}

    def test_single_account(self):
        tables = _make_dcad_tables([{
            "ACCOUNT_NUM": "111",
            "STREET_NUM": "3411",
            "FULL_STREET_NAME": "S MALCOLM X BLVD",
            "LEGAL1": "X", "LEGAL2": "Y",
        }])
        result = _build_account_to_address(tables)
        assert "111" in result
        # normalize_address output - exact form depends on normalize.py
        assert "3411" in result["111"]
        assert "MALCOLM" in result["111"]


# ═══════════════════════════════════════════════════════════════════════════
# resolve_legal_descriptions - integration with mock DCAD
# ═══════════════════════════════════════════════════════════════════════════

class TestResolveLegalDescriptions:
    def _make_canonical_record(self, **overrides) -> dict:
        """Build a canonical-shape record with defaults; override as needed."""
        base = {
            "record_id": "test-1",
            "dallas_code": "LP",
            "raw_excerpt": "DALLAS | Subdivision - Name: SOUTH SIDE Lot: 20 Block: A Township: DALLAS",
            "address_normalized": None,
        }
        base.update(overrides)
        return base

    def _make_test_dcad(self) -> dict:
        """Build a DCAD fixture with a known match."""
        return _make_dcad_tables([
            {
                "ACCOUNT_NUM": "00000169927000000",
                "LEGAL1": "SOUTH SIDE",
                "LEGAL2": "BLK A/1694 PT LOT 20",
                "STREET_NUM": "3411",
                "FULL_STREET_NAME": "S MALCOLM X BLVD",
            },
        ])

    def test_stats_returned(self):
        records = [self._make_canonical_record()]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert isinstance(stats, LegalResolverStats)
        assert stats.total == 1

    def test_resolves_known_record(self):
        records = [self._make_canonical_record()]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.resolved == 1
        assert records[0]["address_normalized"] is not None
        assert "3411" in records[0]["address_normalized"]
        assert "MALCOLM" in records[0]["address_normalized"]

    def test_skips_records_with_existing_address(self):
        # Pre-populated address_normalized must NOT be overwritten
        records = [self._make_canonical_record(
            address_normalized="9999 EXISTING ADDR"
        )]
        resolve_legal_descriptions(records, self._make_test_dcad())
        assert records[0]["address_normalized"] == "9999 EXISTING ADDR"

    def test_no_parse_for_empty_snippet(self):
        records = [self._make_canonical_record(raw_excerpt="N/A | N/A")]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.no_parse == 1
        assert stats.resolved == 0
        assert records[0]["address_normalized"] is None

    def test_no_match_when_subdivision_unknown(self):
        records = [self._make_canonical_record(
            raw_excerpt="DALLAS | Subdivision - Name: NONEXISTENT_SUB Lot: 5 Block: Z Township: DALLAS"
        )]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.no_match == 1
        assert stats.resolved == 0

    def test_multi_match_skipped(self):
        # Two DCAD accounts with the same legal -> resolver should skip
        dcad = _make_dcad_tables([
            {"ACCOUNT_NUM": "111", "LEGAL1": "CONDO X", "LEGAL2": "LT 5",
             "STREET_NUM": "100", "FULL_STREET_NAME": "MAIN ST"},
            {"ACCOUNT_NUM": "222", "LEGAL1": "CONDO X", "LEGAL2": "LT 5",
             "STREET_NUM": "200", "FULL_STREET_NAME": "MAIN ST"},
        ])
        records = [self._make_canonical_record(
            raw_excerpt="DALLAS | Subdivision - Name: CONDO X Lot: 5 Township: DALLAS"
        )]
        stats = resolve_legal_descriptions(records, dcad)
        assert stats.multi_match == 1
        assert stats.resolved == 0
        assert records[0]["address_normalized"] is None

    def test_no_snippet(self):
        records = [self._make_canonical_record(raw_excerpt=None)]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.no_snippet == 1
        assert stats.resolved == 0

    def test_empty_string_snippet_treated_as_no_snippet(self):
        # Empty string should also be no_snippet
        records = [self._make_canonical_record(raw_excerpt="")]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.no_snippet == 1

    def test_resolution_rate_calculation(self):
        # 1 resolved out of 2 total = 50% rate
        records = [
            self._make_canonical_record(record_id="match"),
            self._make_canonical_record(
                record_id="nomatch",
                raw_excerpt="DALLAS | Subdivision - Name: NOPE Lot: 1 Township: DALLAS",
            ),
        ]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.total == 2
        assert stats.resolved == 1
        assert stats.resolution_rate == 0.5

    def test_empty_record_list(self):
        stats = resolve_legal_descriptions([], self._make_test_dcad())
        assert stats.total == 0
        assert stats.resolution_rate == 0.0

    def test_ordinal_word_normalization_works_end_to_end(self):
        # Publicsearch has "FIRST INSTALLMENT" - DCAD has "1ST INST"
        dcad = _make_dcad_tables([{
            "ACCOUNT_NUM": "111",
            "LEGAL1": "OAK ESTATES 1ST INST",
            "LEGAL2": "LT 5",
            "STREET_NUM": "200",
            "FULL_STREET_NAME": "OAK DR",
        }])
        records = [self._make_canonical_record(
            raw_excerpt="DALLAS | Subdivision - Name: OAK ESTATES FIRST INSTALLMENT Lot: 5 Township: DALLAS"
        )]
        stats = resolve_legal_descriptions(records, dcad)
        assert stats.resolved == 1
        assert records[0]["address_normalized"] is not None


# ═══════════════════════════════════════════════════════════════════════════
# LegalResolverStats
# ═══════════════════════════════════════════════════════════════════════════

class TestLegalResolverStats:
    def test_default_values(self):
        s = LegalResolverStats()
        assert s.total == 0
        assert s.no_snippet == 0
        assert s.no_parse == 0
        assert s.no_match == 0
        assert s.multi_match == 0
        assert s.resolved == 0
        assert s.resolution_rate == 0.0

    def test_resolution_rate_zero_total(self):
        s = LegalResolverStats(total=0, resolved=10)  # nonsensical but defensive
        assert s.resolution_rate == 0.0

    def test_resolution_rate_partial(self):
        s = LegalResolverStats(total=4, resolved=3)
        assert s.resolution_rate == 0.75
