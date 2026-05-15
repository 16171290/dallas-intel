"""Tests for ``scraper.name_matcher``."""

from __future__ import annotations

import pytest

from scraper import name_matcher as nm


# ============================================================================
# convert_bankruptcy_to_dcad_format
# ============================================================================


class TestConvertFormat:
    def test_basic_three_token(self):
        assert nm.convert_bankruptcy_to_dcad_format(
            "Eddie C Watkins"
        ) == "WATKINS EDDIE C"

    def test_period_in_middle_initial(self):
        assert nm.convert_bankruptcy_to_dcad_format(
            "Eddie C. Watkins"
        ) == "WATKINS EDDIE C"

    def test_two_token(self):
        assert nm.convert_bankruptcy_to_dcad_format(
            "Mary Smith"
        ) == "SMITH MARY"

    def test_compound_surname_preserves_order(self):
        # We don't know which token is "the surname" so we use the
        # convention: last token is surname.
        result = nm.convert_bankruptcy_to_dcad_format(
            "Jose Angel Rivera Hernandez"
        )
        # Hernandez becomes the surname; everything else stays in order.
        assert result == "HERNANDEZ JOSE ANGEL RIVERA"

    def test_already_uppercase(self):
        assert nm.convert_bankruptcy_to_dcad_format(
            "JOHN SMITH"
        ) == "SMITH JOHN"

    def test_single_token_returns_none(self):
        # Can't reorder a single token.
        assert nm.convert_bankruptcy_to_dcad_format("Madonna") is None

    def test_empty_returns_none(self):
        assert nm.convert_bankruptcy_to_dcad_format("") is None
        assert nm.convert_bankruptcy_to_dcad_format(None) is None

    def test_extra_whitespace_collapsed(self):
        assert nm.convert_bankruptcy_to_dcad_format(
            "  Eddie  C.   Watkins  "
        ) == "WATKINS EDDIE C"


# ============================================================================
# match_debtor_to_dcad
# ============================================================================


class TestMatchDebtor:
    def setup_method(self):
        self.index = {
            "WATKINS EDDIE C":      ["A001"],
            "SMITH JOHN":           ["A002"],
            "RIVERA MARIA":         ["A003"],
            "HERNANDEZ JOSE":       ["A004"],
            # Test fixture: input "Multi Owner" converts to "OWNER MULTI"
            # (Owner = surname). The test verifies one matched name can
            # surface multiple property accounts.
            "OWNER MULTI":          ["A005", "A006"],
        }

    def test_exact_match(self):
        result = nm.match_debtor_to_dcad("Eddie C. Watkins", self.index)
        assert result is not None
        assert result.matched_name == "WATKINS EDDIE C"
        assert result.accounts == ["A001"]
        assert result.match_strategy == "exact"

    def test_no_middle_match(self):
        # Index has "SMITH JOHN" with no middle. Debtor has middle "Q".
        # Strategy 2 strips the middle.
        result = nm.match_debtor_to_dcad("John Q Smith", self.index)
        assert result is not None
        assert result.matched_name == "SMITH JOHN"
        assert result.match_strategy == "no_middle"

    def test_initial_match(self):
        # Debtor has full middle name "Charles"; index has "WATKINS EDDIE C"
        # (just the initial). Strategy 3 truncates the middle.
        result = nm.match_debtor_to_dcad("Eddie Charles Watkins", self.index)
        assert result is not None
        assert result.matched_name == "WATKINS EDDIE C"
        assert result.match_strategy == "last_first_initial"

    def test_multi_property_owner(self):
        # Same owner, multiple parcels.
        result = nm.match_debtor_to_dcad("Multi Owner", self.index)
        assert result is not None
        assert sorted(result.accounts) == ["A005", "A006"]

    def test_no_match_returns_none(self):
        assert nm.match_debtor_to_dcad("Nobody Inthelist", self.index) is None

    def test_single_token_returns_none(self):
        assert nm.match_debtor_to_dcad("Madonna", self.index) is None

    def test_empty_inputs(self):
        assert nm.match_debtor_to_dcad("", self.index) is None
        assert nm.match_debtor_to_dcad("Eddie C. Watkins", {}) is None

    def test_case_insensitive_input(self):
        # Lower-case input should still match.
        result = nm.match_debtor_to_dcad("eddie c. watkins", self.index)
        assert result is not None
        assert result.matched_name == "WATKINS EDDIE C"


# ============================================================================
# match_debtor_names (joint filers)
# ============================================================================


class TestMatchDebtorNames:
    def test_joint_filers_independent_match(self):
        index = {
            "WATTS MICHAEL EARL": ["A100"],
            # Jan Watts is NOT in the index - she'll get None.
        }
        results = nm.match_debtor_names(
            ["Michael Earl Watts", "Jan M Watts"],
            index,
        )
        assert len(results) == 2
        assert results[0][0] == "Michael Earl Watts"
        assert results[0][1] is not None
        assert results[0][1].matched_name == "WATTS MICHAEL EARL"
        assert results[1][0] == "Jan M Watts"
        assert results[1][1] is None

    def test_empty_list(self):
        assert nm.match_debtor_names([], {"FOO": ["A001"]}) == []

    def test_no_matches(self):
        results = nm.match_debtor_names(
            ["Alice Anderson", "Bob Brown"],
            {"SOMEONE ELSE": ["A001"]},
        )
        assert all(r[1] is None for r in results)
