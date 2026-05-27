"""Unit tests for scraper.legal_resolver."""
from __future__ import annotations

import pandas as pd
import pytest

from scraper import legal_resolver
from scraper.legal_resolver import (
    APNResolverStats,
    FuzzySubdivisionMatch,
    LegalResolverStats,
    ParsedLegal,
    build_legal_index,
    fuzzy_subdivision_match,
    normalize_subdivision,
    parse_legal_from_snippet,
    resolve_apn_to_address,
    resolve_legal_descriptions,
    _generate_subdivision_variants,
    _parse_dcad_legal2,
    _build_account_to_address,
)
from scraper.resolution import (
    PATH_C_APN,
    PATH_C_LEGAL_RESOLVER,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    STATUS_NO_MATCH,
    STATUS_SKIPPED,
    TIER_EXACT,
    TIER_FUZZY_SUBDIVISION,
    WARN_FUZZY_SUBDIVISION_MATCH,
    get_history,
    get_warnings,
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

    def test_skips_records_with_existing_dcad_account(self):
        # PR 4: skip predicate is now dcad_account, not address_normalized.
        # An upstream-resolved record (dcad_account set) is left alone.
        records = [self._make_canonical_record(
            address_normalized="9999 EXISTING ADDR",
            dcad_account="00000000000000999",
        )]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.skipped_already_resolved == 1
        assert records[0]["address_normalized"] == "9999 EXISTING ADDR"
        assert records[0]["dcad_account"] == "00000000000000999"

    def test_overwrites_useless_address_when_no_dcad_account(self):
        # PR 4 bug fix: records with city-only / OCR-garbled address but no
        # dcad_account should be resolved (and address overwritten) by the
        # legal-description match.
        records = [self._make_canonical_record(
            address_normalized="DALLAS",  # city-only, useless
            dcad_account=None,
        )]
        stats = resolve_legal_descriptions(records, self._make_test_dcad())
        assert stats.matched_exact == 1
        assert records[0]["dcad_account"] == "00000169927000000"
        assert "MALCOLM" in records[0]["address_normalized"]
        assert records[0]["address_normalized"] != "DALLAS"

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
        # PR 4: `resolved` is now a computed property = matched_exact + matched_fuzzy
        s = LegalResolverStats(total=0, matched_exact=10)  # nonsensical but defensive
        assert s.resolution_rate == 0.0

    def test_resolution_rate_partial(self):
        s = LegalResolverStats(total=4, matched_exact=3)
        assert s.resolved == 3
        assert s.resolution_rate == 0.75

    def test_resolved_sums_exact_and_fuzzy(self):
        s = LegalResolverStats(total=10, matched_exact=4, matched_fuzzy=2)
        assert s.resolved == 6
        assert s.resolution_rate == 0.6


# ═══════════════════════════════════════════════════════════════════════════
# resolve_apn_to_address
# ═══════════════════════════════════════════════════════════════════════════

class TestResolveAPN:
    def _make_dcad(self) -> dict[str, pd.DataFrame]:
        return _make_dcad_tables([
            {
                "ACCOUNT_NUM": "00000811302800000",
                "STREET_NUM": "4314",
                "FULL_STREET_NAME": "HAMILTON",
                "LEGAL1": "X", "LEGAL2": "Y",
            },
            {
                "ACCOUNT_NUM": "180052700",
                "STREET_NUM": "120",
                "FULL_STREET_NAME": "EDGEFIELD AVE",
                "LEGAL1": "X", "LEGAL2": "Y",
            },
        ])

    def test_apn_zero_padded_match(self):
        """OCR extracts '811302800000' (zeros stripped). DCAD stores
        '00000811302800000'. We try the zero-padded form."""
        records = [{
            "record_id": "r1",
            "signal_metadata": {"ocr": {"apn": "811302800000"}},
        }]
        stats = resolve_apn_to_address(records, self._make_dcad())
        assert stats.resolved == 1
        assert "4314" in records[0]["address_normalized"]
        assert "HAMILTON" in records[0]["address_normalized"]

    def test_apn_short_form_match(self):
        records = [{
            "record_id": "r1",
            "signal_metadata": {"ocr": {"apn": "180052700"}},
        }]
        stats = resolve_apn_to_address(records, self._make_dcad())
        assert stats.resolved == 1
        assert "120" in records[0]["address_normalized"]
        assert "EDGEFIELD" in records[0]["address_normalized"]

    def test_apn_no_match(self):
        records = [{
            "record_id": "r1",
            "signal_metadata": {"ocr": {"apn": "999999999"}},
        }]
        stats = resolve_apn_to_address(records, self._make_dcad())
        assert stats.resolved == 0
        assert stats.no_match == 1
        assert records[0].get("address_normalized") is None

    def test_no_apn_in_record(self):
        records = [{"record_id": "r1"}]
        stats = resolve_apn_to_address(records, self._make_dcad())
        assert stats.resolved == 0
        assert stats.no_apn == 1

    def test_skips_records_with_existing_dcad_account(self):
        """PR 4: skip predicate is now dcad_account. Records with an
        upstream-stamped account are left alone."""
        records = [{
            "record_id": "r1",
            "address_normalized": "PRE-EXISTING ADDR",
            "dcad_account": "00000000000000999",
            "signal_metadata": {"ocr": {"apn": "180052700"}},
        }]
        stats = resolve_apn_to_address(records, self._make_dcad())
        assert stats.resolved == 0
        assert stats.skipped_already_resolved == 1
        assert records[0]["address_normalized"] == "PRE-EXISTING ADDR"
        assert records[0]["dcad_account"] == "00000000000000999"

    def test_overwrites_useless_address_when_no_dcad_account(self):
        """PR 4 bug fix: records with city-only address but no dcad_account
        should be resolved by APN match (and address overwritten)."""
        records = [{
            "record_id": "r1",
            "address_normalized": "DALLAS",  # useless
            "dcad_account": None,
            "signal_metadata": {"ocr": {"apn": "180052700"}},
        }]
        stats = resolve_apn_to_address(records, self._make_dcad())
        assert stats.resolved == 1
        assert records[0]["dcad_account"]
        assert records[0]["address_normalized"] != "DALLAS"

    def test_empty_dcad_tables_does_not_crash(self):
        records = [{"record_id": "r1",
                    "signal_metadata": {"ocr": {"apn": "180052700"}}}]
        stats = resolve_apn_to_address(records, {})
        assert stats.resolved == 0

    def test_apn_resolver_stats_defaults(self):
        s = APNResolverStats()
        assert s.total == 0
        assert s.no_apn == 0
        assert s.no_match == 0
        assert s.resolved == 0
        assert s.resolution_rate == 0.0

    def test_apn_resolver_stats_rate(self):
        s = APNResolverStats(total=10, resolved=3)
        assert s.resolution_rate == 0.3


# ═══════════════════════════════════════════════════════════════════════════
# PR 4 — fuzzy_subdivision_match
# ═══════════════════════════════════════════════════════════════════════════


class TestFuzzySubdivisionMatch:
    """Fuzzy subdivision matcher: catches OCR garbles like BEAR TREK -> BEAR CREEK.

    Conservative single-candidate strategy — returns None on ambiguity.
    """

    def _index(self, entries):
        """Build legal_index dict from a list of (sub, lot, block, [accounts]) tuples."""
        idx = {}
        for sub, lot, block, accts in entries:
            idx[(normalize_subdivision(sub), lot, block)] = list(accts)
        return idx

    def test_typo_one_char_resolves(self):
        idx = self._index([
            ("BEAUMONT ADDN", "5", "B", ["acct_1"]),
        ])
        m = fuzzy_subdivision_match("BEUMONT ADDN", idx, "5", "B")
        assert m is not None
        assert m.account == "acct_1"
        assert m.matched_sub == "BEUMONT ADDN".replace("BEUMONT", "BEAUMONT")
        assert m.ratio >= 0.85

    def test_bear_trek_to_bear_creek(self):
        idx = self._index([
            ("BEAR CREEK RANCH PH 4", "12", "B", ["acct_creek"]),
        ])
        m = fuzzy_subdivision_match("BEAR TREK RANCH PH 4", idx, "12", "B")
        assert m is not None
        assert m.account == "acct_creek"

    def test_sandalwogr_to_sandalwood(self):
        idx = self._index([
            ("SANDALWOOD ADDN", "3", "C", ["acct_sand"]),
        ])
        m = fuzzy_subdivision_match("SANDALWOGR ADDN", idx, "3", "C")
        assert m is not None
        assert m.account == "acct_sand"

    def test_returns_none_when_no_candidates_at_lot_block(self):
        idx = self._index([
            ("BEAR CREEK", "99", "Z", ["acct_z"]),
        ])
        m = fuzzy_subdivision_match("BEAR TREK", idx, "12", "B")
        assert m is None

    def test_returns_none_when_below_threshold(self):
        idx = self._index([
            ("COMPLETELY DIFFERENT NAME", "5", "B", ["acct_x"]),
        ])
        m = fuzzy_subdivision_match("BEUMONT ADDN", idx, "5", "B")
        assert m is None

    def test_returns_none_on_ambiguous_candidates(self):
        # Two subdivisions both above threshold for the same lot/block.
        # Conservative policy: bail rather than guess.
        idx = self._index([
            ("BEAUMONT ADDN", "5", "B", ["acct_1"]),
            ("BEAUMOUNT ADDN", "5", "B", ["acct_2"]),
        ])
        m = fuzzy_subdivision_match("BEUMONT ADDN", idx, "5", "B")
        assert m is None

    def test_skips_multi_account_candidate_subs(self):
        # If a candidate sub has >1 account at (lot, block), it's excluded
        # from the fuzzy pool — fuzzy can't disambiguate, only exact lookup
        # could (which would have already failed).
        idx = self._index([
            ("BEAUMONT ADDN", "5", "B", ["acct_1", "acct_2"]),
        ])
        m = fuzzy_subdivision_match("BEUMONT ADDN", idx, "5", "B")
        assert m is None

    def test_empty_target_returns_none(self):
        idx = self._index([("X", "5", "B", ["a"])])
        assert fuzzy_subdivision_match("", idx, "5", "B") is None
        assert fuzzy_subdivision_match(None, idx, "5", "B") is None

    def test_no_lot_or_block_returns_none(self):
        # Lot+block are required as the constraint; without them no fuzzy.
        idx = self._index([("BEAR CREEK", "12", "B", ["a"])])
        assert fuzzy_subdivision_match("BEAR TREK", idx, "", "B") is None
        assert fuzzy_subdivision_match("BEAR TREK", idx, "12", "") is None

    def test_threshold_is_configurable(self):
        idx = self._index([("ABCDEFG", "5", "B", ["acct_close"])])
        # Default threshold 0.85: this wouldn't quite match
        # Lower threshold 0.5: would
        m_strict = fuzzy_subdivision_match("ABCDXYZ", idx, "5", "B",
                                          similarity_threshold=0.85)
        m_loose  = fuzzy_subdivision_match("ABCDXYZ", idx, "5", "B",
                                          similarity_threshold=0.50)
        assert m_strict is None
        assert m_loose is not None


# ═══════════════════════════════════════════════════════════════════════════
# PR 4 — fuzzy fallback inside resolve_legal_descriptions
# ═══════════════════════════════════════════════════════════════════════════


def _make_test_dcad_with_subs(rows):
    """Helper: build dcad_tables with custom LEGAL1/LEGAL2 rows."""
    return _make_dcad_tables(rows)


class TestResolveLegalDescriptionsFuzzyFallback:
    """Verify fuzzy fallback fires when exact lookup misses but a
    near-match candidate exists at the same (lot, block)."""

    def test_fuzzy_resolves_when_exact_misses(self):
        # DCAD has the correct subdivision; record snippet has OCR garble.
        dcad = _make_test_dcad_with_subs([
            {
                "ACCOUNT_NUM": "00000111000000000",
                "LEGAL1": "BEAUMONT ADDN",
                "LEGAL2": "BLK B LT 5",
                "STREET_NUM": "100",
                "FULL_STREET_NAME": "OAK ST",
            },
        ])
        records = [{
            "record_id": "fuzzy-1",
            "dallas_code": "LP",
            "raw_excerpt": "DALLAS | Subdivision - Name: BEUMONT ADDN Lot: 5 Block: B Township: DALLAS",
        }]
        stats = resolve_legal_descriptions(records, dcad)
        assert stats.matched_fuzzy == 1
        assert stats.matched_exact == 0
        assert records[0]["dcad_account"] == "00000111000000000"
        assert "OAK" in records[0]["address_normalized"]
        # confidence_warning recorded
        assert WARN_FUZZY_SUBDIVISION_MATCH in get_warnings(records[0])
        # history entry tagged with fuzzy tier
        history = get_history(records[0])
        assert any(
            h["tier"] == TIER_FUZZY_SUBDIVISION
            and h["status"] == STATUS_MATCHED
            for h in history
        )

    def test_exact_match_does_not_trigger_fuzzy(self):
        dcad = _make_test_dcad_with_subs([
            {
                "ACCOUNT_NUM": "00000222000000000",
                "LEGAL1": "BEAUMONT ADDN",
                "LEGAL2": "BLK B LT 5",
                "STREET_NUM": "100",
                "FULL_STREET_NAME": "OAK ST",
            },
        ])
        records = [{
            "record_id": "exact-1",
            "dallas_code": "LP",
            "raw_excerpt": "DALLAS | Subdivision - Name: BEAUMONT ADDITION Lot: 5 Block: B Township: DALLAS",
        }]
        stats = resolve_legal_descriptions(records, dcad)
        assert stats.matched_exact == 1
        assert stats.matched_fuzzy == 0
        assert WARN_FUZZY_SUBDIVISION_MATCH not in get_warnings(records[0])

    def test_fuzzy_disabled_flag(self):
        dcad = _make_test_dcad_with_subs([
            {
                "ACCOUNT_NUM": "00000333000000000",
                "LEGAL1": "BEAUMONT ADDN",
                "LEGAL2": "BLK B LT 5",
                "STREET_NUM": "100",
                "FULL_STREET_NAME": "OAK ST",
            },
        ])
        records = [{
            "record_id": "no-fuzzy",
            "dallas_code": "LP",
            "raw_excerpt": "DALLAS | Subdivision - Name: BEUMONT ADDN Lot: 5 Block: B Township: DALLAS",
        }]
        stats = resolve_legal_descriptions(records, dcad, enable_fuzzy=False)
        assert stats.matched_exact == 0
        assert stats.matched_fuzzy == 0
        assert stats.no_match == 1
        assert records[0].get("dcad_account") is None

    def test_completely_garbled_returns_no_match(self):
        dcad = _make_test_dcad_with_subs([
            {
                "ACCOUNT_NUM": "00000444000000000",
                "LEGAL1": "BEAUMONT ADDN",
                "LEGAL2": "BLK B LT 5",
                "STREET_NUM": "100",
                "FULL_STREET_NAME": "OAK ST",
            },
        ])
        records = [{
            "record_id": "garbled",
            "dallas_code": "LP",
            "raw_excerpt": "DALLAS | Subdivision - Name: XYZQRS Lot: 5 Block: B Township: DALLAS",
        }]
        stats = resolve_legal_descriptions(records, dcad)
        assert stats.matched_exact == 0
        assert stats.matched_fuzzy == 0
        assert stats.no_match == 1


# ═══════════════════════════════════════════════════════════════════════════
# PR 4 — direct dcad_account stamping
# ═══════════════════════════════════════════════════════════════════════════


class TestPathCStampsDcadAccount:
    """PR 4: Path C stamps dcad_account directly, not only address_normalized.

    Brings Path C in line with Path A so the downstream skipped_already_resolved
    semantics are uniform across paths.
    """

    def _dcad(self):
        return _make_dcad_tables([
            {
                "ACCOUNT_NUM": "00000169927000000",
                "LEGAL1": "SOUTH SIDE",
                "LEGAL2": "BLK A/1694 PT LOT 20",
                "STREET_NUM": "3411",
                "FULL_STREET_NAME": "S MALCOLM X BLVD",
            },
        ])

    def test_legal_match_stamps_dcad_account(self):
        records = [{
            "record_id": "ld-1",
            "dallas_code": "LP",
            "raw_excerpt": "DALLAS | Subdivision - Name: SOUTH SIDE Lot: 20 Block: A Township: DALLAS",
        }]
        resolve_legal_descriptions(records, self._dcad())
        assert records[0]["dcad_account"] == "00000169927000000"
        assert "MALCOLM" in records[0]["address_normalized"]

    def test_apn_match_stamps_dcad_account(self):
        # APN value "811302800000" zero-pads to 17-digit account form
        # "00000811302800000" — same pattern verified by test_apn_zero_padded_match.
        dcad = _make_dcad_tables([
            {
                "ACCOUNT_NUM": "00000811302800000",
                "LEGAL1": "SOMETHING",
                "LEGAL2": "LT 1",
                "STREET_NUM": "4314",
                "FULL_STREET_NAME": "HAMILTON AVE",
            },
        ])
        records = [{
            "record_id": "apn-1",
            "signal_metadata": {"ocr": {"apn": "811302800000"}},
        }]
        resolve_apn_to_address(records, dcad)
        assert records[0]["dcad_account"] == "00000811302800000"
        assert "HAMILTON" in records[0]["address_normalized"]


# ═══════════════════════════════════════════════════════════════════════════
# PR 4 — resolution_history emission
# ═══════════════════════════════════════════════════════════════════════════


class TestPathCHistoryEmission:
    """Path C writes resolution_history entries for matched / no_match /
    multi_match / skipped outcomes so Stage 6.6 can audit cross-path agreement."""

    def _dcad(self):
        # Two rows: one with a legal-description match path (SOUTH SIDE),
        # one with an APN-match path (811302800000 zero-pads to the 17-digit
        # account number — same pattern as test_apn_zero_padded_match).
        return _make_dcad_tables([
            {
                "ACCOUNT_NUM": "00000169927000000",
                "LEGAL1": "SOUTH SIDE",
                "LEGAL2": "BLK A/1694 PT LOT 20",
                "STREET_NUM": "3411",
                "FULL_STREET_NAME": "S MALCOLM X BLVD",
            },
            {
                "ACCOUNT_NUM": "00000811302800000",
                "LEGAL1": "OTHER",
                "LEGAL2": "LT 1",
                "STREET_NUM": "4314",
                "FULL_STREET_NAME": "HAMILTON",
            },
        ])

    def test_matched_writes_history(self):
        records = [{
            "record_id": "h-match",
            "raw_excerpt": "DALLAS | Subdivision - Name: SOUTH SIDE Lot: 20 Block: A Township: DALLAS",
        }]
        resolve_legal_descriptions(records, self._dcad())
        history = get_history(records[0])
        assert len(history) == 1
        h = history[0]
        assert h["path"] == PATH_C_LEGAL_RESOLVER
        assert h["status"] == STATUS_MATCHED
        assert h["tier"] == TIER_EXACT
        assert h["dcad_account"] == "00000169927000000"

    def test_no_match_writes_history(self):
        records = [{
            "record_id": "h-nomatch",
            "raw_excerpt": "DALLAS | Subdivision - Name: NOPE Lot: 1 Block: Z Township: DALLAS",
        }]
        resolve_legal_descriptions(records, self._dcad())
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_NO_MATCH
        assert history[0]["path"] == PATH_C_LEGAL_RESOLVER

    def test_multi_match_writes_history_with_alternates(self):
        dcad = _make_dcad_tables([
            {"ACCOUNT_NUM": "111", "LEGAL1": "CONDO X", "LEGAL2": "LT 5",
             "STREET_NUM": "100", "FULL_STREET_NAME": "MAIN ST"},
            {"ACCOUNT_NUM": "222", "LEGAL1": "CONDO X", "LEGAL2": "LT 5",
             "STREET_NUM": "200", "FULL_STREET_NAME": "MAIN ST"},
        ])
        records = [{
            "record_id": "h-multi",
            "raw_excerpt": "DALLAS | Subdivision - Name: CONDO X Lot: 5 Township: DALLAS",
        }]
        resolve_legal_descriptions(records, dcad)
        history = get_history(records[0])
        assert len(history) == 1
        h = history[0]
        assert h["status"] == STATUS_MULTI_MATCH
        assert set(h["alternates"]) == {"111", "222"}

    def test_no_snippet_writes_skipped(self):
        records = [{"record_id": "h-skip", "raw_excerpt": None}]
        resolve_legal_descriptions(records, self._dcad())
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["status"] == STATUS_SKIPPED
        assert history[0]["skip_reason"] == "no_snippet"

    def test_apn_no_match_writes_history(self):
        records = [{
            "record_id": "h-apn-miss",
            "signal_metadata": {"ocr": {"apn": "999999999"}},
        }]
        resolve_apn_to_address(records, self._dcad())
        history = get_history(records[0])
        assert len(history) == 1
        assert history[0]["path"] == PATH_C_APN
        assert history[0]["status"] == STATUS_NO_MATCH

    def test_apn_match_writes_history(self):
        records = [{
            "record_id": "h-apn-hit",
            "signal_metadata": {"ocr": {"apn": "811302800000"}},
        }]
        resolve_apn_to_address(records, self._dcad())
        history = get_history(records[0])
        assert len(history) == 1
        h = history[0]
        assert h["path"] == PATH_C_APN
        assert h["status"] == STATUS_MATCHED
        assert h["tier"] == TIER_EXACT
        assert h["dcad_account"] == "00000811302800000"
