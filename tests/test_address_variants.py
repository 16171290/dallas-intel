"""Tests for scraper.address_variants — fuzzy DCAD address lookup.

Covers the Class 26 family recovery cases empirically confirmed by
forensic investigation:

  Class 26   — compound-word split   ('LAKEVIEW' vs 'LAKE VIEW')
  Class 26b  — missing street suffix ('HAMILTON' vs 'HAMILTON AVE')
  Class 26d  — directional drop      ('S LAKEVIEW' vs 'LAKEVIEW')

Plus negative tests (no FP on unrelated same-number streets).
"""

from __future__ import annotations

import pytest

from scraper import address_variants
from scraper.address_variants import (
    FUZZY_THRESHOLD,
    build_fuzzy_index,
    fuzzy_lookup,
    _best_match_ratio,
)


# ═══════════════════════════════════════════════════════════════════════════
# build_fuzzy_index
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildFuzzyIndex:
    def test_empty_input(self):
        assert build_fuzzy_index({}) == {}

    def test_single_address(self):
        idx = build_fuzzy_index({"4314 HAMILTON AVE": "acct_X"})
        assert idx == {"4314": [("4314 HAMILTON AVE", "acct_X")]}

    def test_groups_by_street_number(self):
        idx = build_fuzzy_index({
            "4314 HAMILTON AVE": "acct_1",
            "4314 SOUTH ST":     "acct_2",
            "2828 LAKE VIEW DR": "acct_3",
        })
        assert sorted(idx.keys()) == ["2828", "4314"]
        assert len(idx["4314"]) == 2
        assert len(idx["2828"]) == 1

    def test_skips_non_numeric_lead(self):
        """Defensive: malformed index entries that don't start with a
        street number are silently skipped, not crashed on."""
        idx = build_fuzzy_index({
            "4314 HAMILTON AVE": "acct_1",
            "NOT A REAL ADDRESS": "acct_garbage",
        })
        assert "4314" in idx
        assert len(idx) == 1


# ═══════════════════════════════════════════════════════════════════════════
# fuzzy_lookup — Class 26 family recovery
# ═══════════════════════════════════════════════════════════════════════════

class TestFuzzyLookupExactMatch:
    """fuzzy_lookup should still return exact matches when they exist
    (defensive — caller may have skipped or the address_index may have
    changed since the caller checked)."""

    def test_exact_match(self):
        idx = {"4314 HAMILTON AVE": "acct_X"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("4314 HAMILTON AVE", idx, fi)
        assert match == "4314 HAMILTON AVE"
        assert acct == "acct_X"


class TestClass26b_MissingSuffix:
    """Source address lacks the USPS suffix that DCAD has on file."""

    def test_meyers_hamilton(self):
        """4314 HAMILTON (source) -> 4314 HAMILTON AVE (DCAD)."""
        idx = {"4314 HAMILTON AVE": "acct_hamilton"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("4314 HAMILTON", idx, fi)
        assert match == "4314 HAMILTON AVE"
        assert acct == "acct_hamilton"

    def test_acuna_billy_wickliffe(self):
        """309 BILLY WICKLIFFE (source) -> 309 BILLY WICKLIFFE DR (DCAD)."""
        idx = {"309 BILLY WICKLIFFE DR": "acct_wickliffe"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("309 BILLY WICKLIFFE", idx, fi)
        assert match == "309 BILLY WICKLIFFE DR"
        assert acct == "acct_wickliffe"

    def test_multiple_suffix_candidates_picks_highest_ratio(self):
        """When two DCAD entries share the source's prefix, pick the
        higher-similarity one."""
        idx = {
            "1234 OAK ST":      "acct_oak_st",
            "1234 OAKVIEW AVE": "acct_oakview",  # less similar to 'OAK'
        }
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("1234 OAK", idx, fi)
        # 'OAK' is closer to 'OAK ST' than to 'OAKVIEW AVE'
        assert match == "1234 OAK ST"
        assert acct == "acct_oak_st"


class TestClass26_CompoundSplit:
    """Source joined compound, DCAD has it split (or vice versa)."""

    def test_lakeview_joined_vs_split(self):
        """LAKEVIEW (source) <-> LAKE VIEW (DCAD)."""
        idx = {"2828 LAKE VIEW DR": "acct_lakeview"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("2828 LAKEVIEW DR", idx, fi)
        assert match == "2828 LAKE VIEW DR"
        assert acct == "acct_lakeview"

    def test_split_to_joined(self):
        """Source split, DCAD joined."""
        idx = {"2828 LAKEVIEW DR": "acct_lakeview"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("2828 LAKE VIEW DR", idx, fi)
        assert match == "2828 LAKEVIEW DR"
        assert acct == "acct_lakeview"


class TestClass26d_DirectionalDrop:
    """Source has a directional that DCAD doesn't (or vice versa)."""

    def test_source_has_directional_dcad_doesnt(self):
        """2828 S LAKEVIEW DR (source) -> 2828 LAKEVIEW DR (DCAD)."""
        idx = {"2828 LAKEVIEW DR": "acct_lakeview"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("2828 S LAKEVIEW DR", idx, fi)
        assert match == "2828 LAKEVIEW DR"
        assert acct == "acct_lakeview"

    def test_dcad_has_directional_source_doesnt(self):
        idx = {"2828 S LAKEVIEW DR": "acct_lakeview"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("2828 LAKEVIEW DR", idx, fi)
        assert match == "2828 S LAKEVIEW DR"
        assert acct == "acct_lakeview"


class TestAdamaCompoundCombined:
    """The Adama empirical case: directional drop + compound split combined.

      source: '2828 S LAKEVIEW DR'
      DCAD:   '2828 LAKE VIEW DR'
    """

    def test_adama_directional_plus_compound(self):
        idx = {"2828 LAKE VIEW DR": "acct_adama"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("2828 S LAKEVIEW DR", idx, fi)
        assert match == "2828 LAKE VIEW DR"
        assert acct == "acct_adama"


# ═══════════════════════════════════════════════════════════════════════════
# Negative tests — must NOT false-positive
# ═══════════════════════════════════════════════════════════════════════════

class TestNoFalsePositives:
    def test_no_match_when_street_number_different(self):
        """1234 OAK ST in DCAD shouldn't match 4314 OAK ST."""
        idx = {"1234 OAK ST": "acct_x"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("4314 OAK ST", idx, fi)
        assert match is None
        assert acct is None

    def test_no_match_when_street_name_unrelated(self):
        """Same street number but unrelated street name → no match."""
        idx = {"1234 BURTON RD": "acct_burton"}
        fi = build_fuzzy_index(idx)
        # 'HAMILTON' vs 'BURTON RD' similarity is well below threshold
        match, acct = fuzzy_lookup("1234 HAMILTON", idx, fi)
        assert match is None
        assert acct is None

    def test_no_match_empty_index(self):
        match, acct = fuzzy_lookup("4314 HAMILTON", {}, {})
        assert match is None and acct is None

    def test_no_match_empty_address(self):
        idx = {"4314 HAMILTON AVE": "acct_x"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("", idx, fi)
        assert match is None
        match, acct = fuzzy_lookup(None, idx, fi)  # type: ignore
        assert match is None

    def test_no_match_when_no_leading_number(self):
        idx = {"4314 HAMILTON AVE": "acct_x"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("HAMILTON AVE", idx, fi)
        assert match is None

    def test_threshold_rejects_low_similarity(self):
        """Force the test: 'X' vs 'HAMILTON AVE' is very low similarity.
        Should be rejected by the 0.85 threshold."""
        idx = {"4314 HAMILTON AVE": "acct_hamilton"}
        fi = build_fuzzy_index(idx)
        match, acct = fuzzy_lookup("4314 X", idx, fi)
        # 'X' vs 'HAMILTON AVE' ratio < 0.85
        assert match is None


# ═══════════════════════════════════════════════════════════════════════════
# _best_match_ratio internal scoring
# ═══════════════════════════════════════════════════════════════════════════

class TestBestMatchRatio:
    def test_identical_strings(self):
        assert _best_match_ratio("HAMILTON AVE", "HAMILTON AVE") == 1.0

    def test_directional_drop_lifts_ratio(self):
        """'S LAKEVIEW DR' vs 'LAKEVIEW DR' — raw ratio low, but stripping
        directional brings it to 1.0."""
        ratio = _best_match_ratio("S LAKEVIEW DR", "LAKEVIEW DR")
        assert ratio >= 0.99

    def test_compound_join_lifts_ratio(self):
        """'LAKEVIEW DR' vs 'LAKE VIEW DR' — raw ratio moderate, joined
        comparison brings it to ~1.0."""
        ratio = _best_match_ratio("LAKEVIEW DR", "LAKE VIEW DR")
        assert ratio >= 0.95

    def test_unrelated_streets_stay_low(self):
        ratio = _best_match_ratio("HAMILTON", "BURTON RD")
        assert ratio < FUZZY_THRESHOLD
