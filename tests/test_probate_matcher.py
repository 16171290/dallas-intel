"""Tests for scraper.probate_matcher.match_decedent_to_dcad.

Each test isolates one tier of the ladder by constructing a synthetic
owner_index with exactly the keys needed to exercise that tier.
"""

from scraper.probate_matcher import (
    match_decedent_to_dcad,
    ProbateMatchResult,
    SURNAME_ONLY_MAX_ACCOUNTS,
    COMMON_NAME_ACCOUNT_WARNING,
)


# ----------------------------------------------------------------------------
# Tier 1: exact match
# ----------------------------------------------------------------------------


class TestTierExact:
    def test_comma_form_exact(self):
        index = {"WALLACE VON ELLEN": ["acct_1"]}
        r = match_decedent_to_dcad("WALLACE, VON ELLEN", index)
        assert r is not None
        assert r.tier == "exact"
        assert r.matched_name == "WALLACE VON ELLEN"
        assert r.accounts == ["acct_1"]
        assert r.key_source == "asis"
        assert r.warning is None

    def test_no_comma_form_via_rotation_exact(self):
        # Tyler's rare outlier: "FIRST MIDDLE LAST" without comma.
        # Matches only via the rotated key.
        index = {"HAWKINS CYNTHIA": ["acct_h"]}
        r = match_decedent_to_dcad("CYNTHIA HAWKINS", index)
        assert r is not None
        assert r.tier == "exact"
        assert r.matched_name == "HAWKINS CYNTHIA"
        assert r.key_source == "rotated"


# ----------------------------------------------------------------------------
# Tier 2: no_middle
# ----------------------------------------------------------------------------


class TestTierNoMiddle:
    def test_strips_middle_preserves_surname(self):
        index = {"WATSON ROBERT": ["acct_w"]}
        r = match_decedent_to_dcad("WATSON, ROBERT ALLEN", index)
        assert r is not None
        assert r.tier == "no_middle"
        assert r.matched_name == "WATSON ROBERT"


# ----------------------------------------------------------------------------
# Tier 3: initial_form
# ----------------------------------------------------------------------------


class TestTierInitialForm:
    def test_middle_initial_match(self):
        index = {"KLAUSING DAVID L": ["acct_k"]}
        r = match_decedent_to_dcad("KLAUSING, DAVID LUCIEN", index)
        assert r is not None
        assert r.tier == "initial_form"
        assert r.matched_name == "KLAUSING DAVID L"


# ----------------------------------------------------------------------------
# Tier 3b: double_initial
# ----------------------------------------------------------------------------


class TestTierDoubleInitial:
    def test_four_token_name_with_two_middle_initials(self):
        index = {"ROLLINGS RAYMOND L M": ["acct_r"]}
        r = match_decedent_to_dcad("ROLLINGS, RAYMOND LEITH MARTIN", index)
        assert r is not None
        assert r.tier == "double_initial"
        assert r.matched_name == "ROLLINGS RAYMOND L M"


# ----------------------------------------------------------------------------
# Tier 4: surname_only (uniqueness-gated)
# ----------------------------------------------------------------------------


class TestTierSurnameOnly:
    def test_unique_surname_family_trust(self):
        # DCAD normalized "JORDAN LIVING TRUST" -> just "JORDAN".
        # Surname-only fires because it maps to <= SURNAME_ONLY_MAX_ACCOUNTS.
        index = {"JORDAN": ["acct_j"]}
        r = match_decedent_to_dcad("JORDAN, BRENDA G.", index)
        assert r is not None
        assert r.tier == "surname_only"
        assert r.matched_name == "JORDAN"

    def test_common_surname_rejected(self):
        # Surname is too common — surname-only must NOT fire (no FP).
        too_common = ["x"] * (SURNAME_ONLY_MAX_ACCOUNTS + 1)
        index = {"JOHNSON": too_common}
        r = match_decedent_to_dcad("JOHNSON, JEREMY", index)
        assert r is None


# ----------------------------------------------------------------------------
# FP protection: no-comma asis must NOT trigger multi-token tiers
# ----------------------------------------------------------------------------


class TestNoCommaAsisDoesNotFanOut:
    def test_no_match_when_first_token_is_first_name(self):
        # asis key "SARAH DUNN" has first token = first name (SARAH).
        # surname_only on SARAH would match unrelated stuff. Must not fire.
        # Rotated key "DUNN SARAH" has DUNN as first token but DUNN
        # doesn't exist in this index, so no rotated hit either.
        index = {"SARAH LIVING TRUST": ["bogus"]}  # would FP under buggy logic
        r = match_decedent_to_dcad("SARAH DUNN", index)
        assert r is None

    def test_no_comma_asis_exact_still_works(self):
        # If DCAD happens to have the FIRST LAST form exactly (rare but
        # possible), the asis exact path should still hit. Tier 1 is
        # safe regardless of token order.
        index = {"JASON ANTHONY LOWE": ["acct_j"]}
        r = match_decedent_to_dcad("JASON ANTHONY LOWE", index)
        assert r is not None
        assert r.tier == "exact"
        assert r.key_source == "asis"


# ----------------------------------------------------------------------------
# Common-name pollution warning
# ----------------------------------------------------------------------------


class TestCommonNamePollutionWarning:
    def test_more_than_three_accounts_flagged(self):
        # WILSON ROBERT case: exact match but 8 different Robert Wilsons.
        accounts = [f"acct_{i}" for i in range(8)]
        index = {"WILSON ROBERT": accounts}
        r = match_decedent_to_dcad("WILSON, ROBERT", index)
        assert r is not None
        assert r.tier == "exact"
        assert r.warning == "common_name_pollution"
        assert len(r.accounts) == 8

    def test_three_or_fewer_accounts_no_warning(self):
        # Edge of the warning threshold — 3 accounts should NOT warn,
        # 4+ should. Matches COMMON_NAME_ACCOUNT_WARNING = 3.
        index = {"WALLACE ALLEN": ["a", "b", "c"]}
        r = match_decedent_to_dcad("WALLACE, ALLEN", index)
        assert r is not None
        assert r.warning is None


# ----------------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_name(self):
        assert match_decedent_to_dcad("", {"FOO BAR": ["x"]}) is None

    def test_empty_index(self):
        assert match_decedent_to_dcad("WALLACE, VON", {}) is None

    def test_punctuation_only_name(self):
        # normalize_dcad_owner_name will strip to empty string.
        assert match_decedent_to_dcad(",,,", {"FOO": ["x"]}) is None

    def test_jr_suffix_stripped(self):
        # Tyler input "WARREN, ALLEN, Jr" — normalize strips Jr suffix,
        # leaving "WARREN ALLEN" which should match.
        index = {"WARREN ALLEN": ["acct_w"]}
        r = match_decedent_to_dcad("WARREN, ALLEN, Jr", index)
        assert r is not None
        assert r.tier == "exact"
        assert r.matched_name == "WARREN ALLEN"
