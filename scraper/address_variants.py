"""DCAD address-index fuzzy lookup — Class 26 / 26b / 26d recovery.

The bulk DCAD address_index is strict string equality after
``normalize_address`` runs. DCAD's web search supports forgiving
partial / fuzzy matching that catches:

  Class 26   — compound-word split   ("LAKEVIEW" vs "LAKE VIEW")
  Class 26b  — missing street suffix ("HAMILTON" vs "HAMILTON AVE")
  Class 26d  — directional drop      ("S LAKEVIEW" vs "LAKEVIEW")
  Class 26e  — partial street name   ("BILLY" vs "BILLY WICKLIFFE")

Empirically validated forensic cases (per docs/FORENSIC_AUDIT):

  source addr                  DCAD has                   transformation
  ─────────────────────────────────────────────────────────────────────
  4314 HAMILTON                4314 HAMILTON AVE          + suffix
  309 BILLY WICKLIFFE          309 BILLY WICKLIFFE DR     + suffix
  2828 S LAKEVIEW DR           2828 LAKE VIEW DR          - directional + split

This module provides:
  - ``build_fuzzy_index(address_index)`` — secondary index keyed by
    street number for tractable iteration
  - ``fuzzy_lookup(addr, address_index, fuzzy_index)`` — finds best
    DCAD match for an address that exact-lookup missed

The fuzzy lookup is conservative: requires SequenceMatcher ratio >=
``FUZZY_THRESHOLD`` (0.85) on the best of four comparisons (raw, source
no-directional, candidate no-directional, both-joined). False positives
on this threshold are rare because addresses sharing a street number are
usually the same parcel.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


# Conservative similarity threshold. Per design §13 Q1 — 0.85 catches
# obvious variants (HAMILTON ↔ HAMILTON AVE, LAKEVIEW ↔ LAKE VIEW) while
# keeping FP surface small. Recalibratable after first production run.
FUZZY_THRESHOLD: float = 0.85

# USPS directional tokens we strip-and-retry against. Matches the abbreviated
# forms that ``normalize_address`` produces (full-word forms like SOUTH/NORTH
# are already abbreviated by the time we see them).
_DIRECTIONAL_PREFIX_RE = re.compile(r"^(?:N|S|E|W|NE|NW|SE|SW)\s+")

# Capture the street number off the front of a normalized address so we can
# index by it. Address index keys are post-``normalize_address`` so they
# always start with the bare street number.
_STREET_NUMBER_RE = re.compile(r"^(\d+)\s+(.+)$")


def build_fuzzy_index(
    address_index: dict[str, str],
) -> dict[str, list[tuple[str, str]]]:
    """Build a secondary index keyed by street number for fuzzy lookup.

    Returns ``{street_number: [(full_normalized_address, account_num), ...]}``.

    The size is ~700K entries split across tens of thousands of street
    numbers. Average bucket size is small (<10), so per-lookup iteration
    is fast.

    Idempotent on dict-shape input. Skips entries that don't start with a
    leading number (defensive against malformed index entries).
    """
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for addr, acct in address_index.items():
        m = _STREET_NUMBER_RE.match(addr)
        if not m:
            continue
        num = m.group(1)
        out[num].append((addr, acct))
    return dict(out)


def _is_prefix_word_match(source_rest: str, cand_rest: str) -> bool:
    """True if ``source_rest`` is a complete word-boundary prefix of
    ``cand_rest`` (or identical).

    This catches the Class 26b missing-suffix case directly:
      'HAMILTON'         is a prefix of 'HAMILTON AVE'        ✓
      'BILLY WICKLIFFE'  is a prefix of 'BILLY WICKLIFFE DR'  ✓
      'HAMIL'            is NOT a word-boundary prefix of 'HAMILTON AVE'
      'HAMILTON'         is NOT a prefix of 'HAMILTONS PL'    (no boundary)

    SequenceMatcher gives a low score (~0.80) for these because the
    length difference dominates; the prefix check gives us the correct
    high-confidence signal.
    """
    if source_rest == cand_rest:
        return True
    return cand_rest.startswith(source_rest + " ")


def _best_match_ratio(source_rest: str, cand_rest: str) -> float:
    """Return the BEST similarity score between the post-number portions
    of source and candidate addresses.

    Strategies (highest wins):
      0. Word-boundary prefix match -> 1.0 (Class 26b: missing suffix)
      1. Raw SequenceMatcher ratio
      2. Source with leading directional dropped vs candidate
      3. Source vs candidate with leading directional dropped (apply
         prefix check to no-dir form too — catches 'S LAKEVIEW' vs
         'LAKEVIEW DR' style cases)
      4. Both with all whitespace removed (Class 26: compound-word
         equivalence — 'LAKEVIEW' vs 'LAKE VIEW')
    """
    s = source_rest
    c = cand_rest

    # 0. Word-boundary prefix match — handles missing-suffix cleanly
    if _is_prefix_word_match(s, c) or _is_prefix_word_match(c, s):
        return 1.0

    # 1. Raw
    best = difflib.SequenceMatcher(None, s, c).ratio()

    # 2. Source minus directional
    s_no_dir = _DIRECTIONAL_PREFIX_RE.sub("", s, count=1)
    if s_no_dir != s:
        if _is_prefix_word_match(s_no_dir, c) or _is_prefix_word_match(c, s_no_dir):
            return 1.0
        best = max(best, difflib.SequenceMatcher(None, s_no_dir, c).ratio())

    # 3. Candidate minus directional
    c_no_dir = _DIRECTIONAL_PREFIX_RE.sub("", c, count=1)
    if c_no_dir != c:
        if _is_prefix_word_match(s, c_no_dir) or _is_prefix_word_match(c_no_dir, s):
            return 1.0
        best = max(best, difflib.SequenceMatcher(None, s, c_no_dir).ratio())

    # 4. Whitespace-collapsed: catches LAKEVIEW <-> LAKE VIEW.
    # Apply on the directional-stripped forms so 'S LAKEVIEW' (joined)
    # collapses to 'LAKEVIEW' which equals 'LAKE VIEW' (split) after
    # collapse.
    s_joined = (s_no_dir or s).replace(" ", "")
    c_joined = (c_no_dir or c).replace(" ", "")
    if s_joined and c_joined:
        best = max(best, difflib.SequenceMatcher(None, s_joined, c_joined).ratio())

    return best


def fuzzy_lookup(
    addr_normalized: str,
    address_index: dict[str, str],
    fuzzy_index: dict[str, list[tuple[str, str]]],
    *,
    threshold: float = FUZZY_THRESHOLD,
) -> tuple[Optional[str], Optional[str]]:
    """Find the best DCAD match for an address that exact-lookup missed.

    Strategy:
      1. Try exact lookup against ``address_index`` (defensive — caller
         should have already tried, but check again for safety).
      2. Pull all DCAD entries sharing the same leading street number
         from ``fuzzy_index``.
      3. Score each candidate via ``_best_match_ratio`` (best of raw +
         directional-dropped + whitespace-collapsed comparisons).
      4. Return the highest-scoring candidate above ``threshold``.
         Returns ``(None, None)`` if no candidate clears the threshold
         or if there are zero same-number candidates.

    The returned tuple is ``(matched_full_address, account_num)`` so
    callers can stamp both ``address_normalized`` and ``dcad_account``.
    """
    if not addr_normalized:
        return None, None

    # Defensive exact-match check
    exact = address_index.get(addr_normalized)
    if exact:
        return addr_normalized, exact

    m = _STREET_NUMBER_RE.match(addr_normalized)
    if not m:
        return None, None
    num, source_rest = m.group(1), m.group(2)

    candidates = fuzzy_index.get(num)
    if not candidates:
        return None, None

    best_score = 0.0
    best_match: Optional[tuple[str, str]] = None
    for full_addr, acct in candidates:
        cm = _STREET_NUMBER_RE.match(full_addr)
        if not cm:
            continue
        cand_rest = cm.group(2)
        ratio = _best_match_ratio(source_rest, cand_rest)
        if ratio > best_score:
            best_score = ratio
            best_match = (full_addr, acct)

    if best_score >= threshold and best_match:
        logger.debug(
            "fuzzy_lookup: %r -> %r (ratio=%.3f acct=%s)",
            addr_normalized, best_match[0], best_score, best_match[1],
        )
        return best_match

    return None, None
