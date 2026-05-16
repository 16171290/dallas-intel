"""
PR 2 regression tests for the two matcher defects found in Phase 4.

These supplement the existing test suites for name_matcher and
dcad_owner_index. They lock in the fixed behavior so future refactors
can't silently reintroduce the bugs.

Defect 1 (name_matcher): asymmetric suffix normalization. The matcher
converts "Benjamin Franklin Iii Byrd" to "BYRD BENJAMIN FRANKLIN III"
for lookup, but the index normalizes "BYRD BENJAMIN FRANKLIN III" to
"BYRD BENJAMIN FRANKLIN" when building keys. Pre-fix the query missed.

Defect 2 (dcad_owner_index): the LE token in _ROLE_SUFFIX_RE was
intended to catch the "Life Estate" abbreviation at the end of an
owner name. Applied to "LE NHUNG THI" (Vietnamese surname Le), it
consumed the whole string and returned None - excluding 500+ owners.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the scraper package importable when pytest is run from repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.dcad_owner_index import (
    build_owner_index,
    normalize_dcad_owner_name,
)
from scraper.name_matcher import (
    match_debtor_to_dcad,
    convert_bankruptcy_to_dcad_format,
)


# ============================================================================
# Defect 1 - suffix-asymmetry in name_matcher
# ============================================================================


def _idx(*owner_names: str) -> dict[str, list[str]]:
    """Build a minimal owner-index from a few raw DCAD owner names."""
    rows = [
        {"OWNER_NAME": name, "ACCOUNT_NUM": f"acct_{i:04d}"}
        for i, name in enumerate(owner_names)
    ]
    return build_owner_index({"ACCOUNT_INFO": rows})


def test_pr2_suffix_iii_matches_index_after_normalization():
    """Probate-style 'Benjamin Franklin Iii Byrd' must match DCAD's
    'BYRD BENJAMIN FRANKLIN III' (which the index normalizes to
    'BYRD BENJAMIN FRANKLIN' on build)."""
    idx = _idx("BYRD BENJAMIN FRANKLIN III")
    result = match_debtor_to_dcad("Benjamin Franklin Iii Byrd", idx)
    assert result is not None, "PR 2 defect 1 regression: III-suffix query failed"
    assert result.match_strategy == "exact"
    assert result.matched_name == "BYRD BENJAMIN FRANKLIN"


def test_pr2_suffix_jr_matches_index_after_normalization():
    """JR suffix on the query side must not block the match."""
    idx = _idx("PONCE FRANCISCO ISMAEL JR")
    result = match_debtor_to_dcad("Francisco Ismael Jr Ponce", idx)
    assert result is not None, "PR 2 defect 1 regression: JR-suffix query failed"
    assert result.matched_name == "PONCE FRANCISCO ISMAEL"


def test_pr2_suffix_sr_matches_index_after_normalization():
    """SR suffix should behave identically to JR."""
    idx = _idx("GARCIA JOSE LUIS SR")
    result = match_debtor_to_dcad("Jose Luis Sr Garcia", idx)
    assert result is not None
    assert result.matched_name == "GARCIA JOSE LUIS"


def test_pr2_no_suffix_still_works_after_normalization():
    """The fix must not regress the no-suffix case."""
    idx = _idx("WATKINS EDDIE C")
    result = match_debtor_to_dcad("Eddie C Watkins", idx)
    assert result is not None
    assert result.match_strategy == "exact"
    assert result.matched_name == "WATKINS EDDIE C"


def test_pr2_no_match_still_returns_none():
    """A name not in the index must still return None - no false positives."""
    idx = _idx("WATKINS EDDIE C")
    result = match_debtor_to_dcad("Jane Doe", idx)
    assert result is None


# ============================================================================
# Defect 2 - Vietnamese surname LE in dcad_owner_index
# ============================================================================


def test_pr2_vietnamese_le_surname_preserved():
    """A name starting with surname 'LE' must normalize intact, not to None."""
    assert normalize_dcad_owner_name("LE NHUNG THI") == "LE NHUNG THI"
    assert normalize_dcad_owner_name("LE HOANG") == "LE HOANG"
    assert normalize_dcad_owner_name("LE VAN") == "LE VAN"


def test_pr2_le_followed_by_life_estate_phrase_still_strips():
    """The full 'LIFE ESTATE' phrase must still strip - the regex
    is gone but the multi-word form remains."""
    assert normalize_dcad_owner_name("SMITH JOHN A LIFE ESTATE") == "SMITH JOHN A"


def test_pr2_trailing_le_abbreviation_still_strips():
    """A trailing standalone 'LE' (Life Estate abbreviation, the
    legitimate use case) must still be stripped when at the end."""
    assert normalize_dcad_owner_name("SMITH JOHN A LE") == "SMITH JOHN A"
    assert normalize_dcad_owner_name("JONES MARY LE") == "JONES MARY"


def test_pr2_le_only_returns_le():
    """Edge case: a single-token 'LE' input doesn't trigger the
    trailing-LE rule (no preceding content) so it survives."""
    assert normalize_dcad_owner_name("LE") == "LE"


def test_pr2_le_compound_name_owner_index_membership():
    """End-to-end: build an index from a few LE-surname owners and
    confirm they are findable."""
    idx = _idx(
        "LE NHUNG THI",
        "LE HOANG",
        "LE VAN",
        "SMITH JOHN A LE",  # Life Estate use case
    )
    assert "LE NHUNG THI" in idx
    assert "LE HOANG" in idx
    assert "LE VAN" in idx
    assert "SMITH JOHN A" in idx  # Life Estate stripped
    assert "SMITH JOHN A LE" not in idx  # the un-stripped form should NOT be a key


def test_pr2_le_surname_matcher_round_trip():
    """Probate-style query for a Vietnamese decedent should resolve
    to the LE-surname index entry."""
    idx = _idx("LE NHUNG THI")
    # In probate parlance the name comes as "First Middle Last"
    result = match_debtor_to_dcad("Nhung Thi Le", idx)
    assert result is not None, "PR 2 defect 2 regression: LE-surname query failed"
    assert result.matched_name == "LE NHUNG THI"
