"""
Debtor-name to DCAD-owner matching.

The bankruptcy RSS emits debtor names in "First Middle Last" mixed case
(``Eddie C. Watkins``). DCAD owner records are in "LAST FIRST MIDDLE"
uppercase (``WATKINS EDDIE C``). This module converts between the two
formats and attempts to resolve a debtor to a DCAD account using
progressively looser matching strategies.

Public API:
    match_debtor_to_dcad(debtor_name, owner_index) -> MatchResult | None
    convert_bankruptcy_to_dcad_format(name) -> str | None

The match result, when non-None, includes:
    matched_name:     the DCAD index key that matched
    accounts:         list of DCAD account numbers
    match_strategy:   "exact" / "no_middle" / "last_first_initial"
                      (so callers can audit match quality)

Strategy ladder (each tier strictly looser than the previous):
    Tier 1 (exact):       "WATKINS EDDIE C"      direct lookup
    Tier 2 (no middle):   "WATKINS EDDIE"        try without middle
    Tier 3 (initial fix): "WATKINS EDDIE C"      try first-name first-initial

Anything looser than this would create false positives. Last-name-only
matching is intentionally not implemented; common surnames would over-match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------


@dataclass
class MatchResult:
    """One successful match against the DCAD owner index."""
    matched_name:    str            # the DCAD key that hit (e.g. "WATKINS EDDIE C")
    accounts:        list[str]      # one or more DCAD account numbers
    match_strategy:  str            # how we found it (audit / debugging)


# ----------------------------------------------------------------------------
# Format conversion
# ----------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[.,;]")
_WS_RE    = re.compile(r"\s+")


def convert_bankruptcy_to_dcad_format(name: str | None) -> str | None:
    """Reformat ``"First Middle Last"`` to ``"LAST FIRST MIDDLE"`` uppercase.

    Examples:
        "Eddie C. Watkins"           -> "WATKINS EDDIE C"
        "Jose Angel Rivera Hernandez"-> "HERNANDEZ JOSE ANGEL RIVERA"
        "Mary Smith"                 -> "SMITH MARY"
        "Madonna"                    -> None  (single token, can't reorder)

    Returns None if the name has fewer than 2 tokens after cleanup.
    """
    if not name:
        return None
    s = name.upper().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    tokens = s.split()
    if len(tokens) < 2:
        return None
    # Last token becomes the surname; the rest stays in order.
    surname = tokens[-1]
    rest = " ".join(tokens[:-1])
    return f"{surname} {rest}"


def _make_no_middle_form(dcad_format: str) -> str | None:
    """From "WATKINS EDDIE C" -> "WATKINS EDDIE". Returns None if already 2 tokens."""
    tokens = dcad_format.split()
    if len(tokens) < 3:
        return None
    # Keep surname + first name only
    return f"{tokens[0]} {tokens[1]}"


def _make_initial_form(dcad_format: str) -> str | None:
    """From "WATKINS EDDIE CHARLES" -> "WATKINS EDDIE C". Returns None if N/A."""
    tokens = dcad_format.split()
    if len(tokens) < 3:
        return None
    # Trim middle name to first initial.
    middle_initial = tokens[2][:1]
    if not middle_initial.isalpha():
        return None
    return f"{tokens[0]} {tokens[1]} {middle_initial}"


# ----------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------


def match_debtor_to_dcad(
    debtor_name: str,
    owner_index: Mapping[str, list[str]],
) -> Optional[MatchResult]:
    """Try to match a bankruptcy debtor against the DCAD owner index.

    Strategy ladder - the first successful tier wins:
      1. Exact: the full reformatted name as-is.
      2. No-middle: drop the middle name.
      3. First-initial: shorten the middle to a single letter.

    Returns None if no tier matches. Callers can interpret None as
    "this debtor doesn't own Dallas County property under this name."

    Common reasons for no-match (informational - not bugs):
      - Debtor rents
      - Debtor owns property outside Dallas County
      - Name in DCAD has a suffix we haven't normalized (Jr./Sr.,
        deceased, etc.)
      - Name spelling differs (Hispanic compound surnames, hyphens,
        marriage name changes)
    """
    if not debtor_name or not owner_index:
        return None

    dcad_form = convert_bankruptcy_to_dcad_format(debtor_name)
    if not dcad_form:
        return None

    # Tier 1: exact.
    if dcad_form in owner_index:
        return MatchResult(
            matched_name=dcad_form,
            accounts=list(owner_index[dcad_form]),
            match_strategy="exact",
        )

    # Tier 2: no middle name.
    no_middle = _make_no_middle_form(dcad_form)
    if no_middle and no_middle in owner_index:
        return MatchResult(
            matched_name=no_middle,
            accounts=list(owner_index[no_middle]),
            match_strategy="no_middle",
        )

    # Tier 3: first initial of middle name.
    initial_form = _make_initial_form(dcad_form)
    if initial_form and initial_form in owner_index:
        return MatchResult(
            matched_name=initial_form,
            accounts=list(owner_index[initial_form]),
            match_strategy="last_first_initial",
        )

    return None


def match_debtor_names(
    debtor_names: list[str],
    owner_index: Mapping[str, list[str]],
) -> list[tuple[str, Optional[MatchResult]]]:
    """Match each name in a list (e.g. joint filers) independently.

    Returns ``[(debtor_name, match_result_or_None), ...]`` in the same
    order. Useful when a single bankruptcy record has multiple debtors
    and any one of them might own property.
    """
    return [(name, match_debtor_to_dcad(name, owner_index))
            for name in debtor_names]
