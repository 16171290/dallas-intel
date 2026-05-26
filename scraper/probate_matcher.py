"""Match re:SearchTX probate decedent names to DCAD property records.

Why a separate module from ``name_matcher.py``:
  ``name_matcher`` is designed for bankruptcy RSS data which arrives as
  "First Middle Last" mixed-case and needs token rotation. Tyler probate
  data arrives as "LAST, FIRST MIDDLE" (with the rare no-comma outlier)
  and needs a DIFFERENT preprocessing path. Sharing one matcher caused
  silent surname-dropped false positives during recon — Tyler input
  ``"CANOVALI, DONALD W."`` got double-rotated, the no_middle tier then
  produced ``"DONALD W"``, and that matched a random Donald-W. owner
  with the Canovali surname completely thrown away. Keeping the two
  matchers in separate modules makes the contract per source clear.

Tier ladder (each tier strictly more permissive than the previous):

  exact:          LAST FIRST MIDDLE
  no_middle:      LAST FIRST                    (3+ tokens)
  initial_form:   LAST FIRST INITIAL_OF_MIDDLE  (3+ tokens)
  double_initial: LAST FIRST INIT1 INIT2        (4+ tokens, catches the
                  "ROLLINGS RAYMOND L M" abbreviation pattern in DCAD)
  surname_only:   LAST                          (uniqueness-gated: only
                  fires when the surname maps to <= SURNAME_ONLY_MAX_ACCOUNTS
                  accounts, so it catches family-trust collapses like
                  "JORDAN LIVING TRUST" -> normalized "JORDAN" -> 1 account
                  without producing the WILSON ROBERT catastrophe)

ALL tiers preserve the surname (first token of the lookup key). Tiers
2-5 only fire when the caller can guarantee the first token IS the
surname — that's true for Tyler's "LAST, FIRST MIDDLE" form (asis) and
for the rotated form we generate for the rare no-comma outlier. For
no-comma names in asis form ("JASON ANTHONY LOWE"), first token is the
FIRST NAME and only tier 1 is tried — anything else would be a
guaranteed FP.

Validated via ``probe_probate_dcad_hitrate.py`` on 45 live decedent
names (2026-05-26 cron snapshot): 37/45 = 82% raw hit rate, of which
~2 are common-name pollution warnings (>3 accounts per match) and
~35 are high-confidence matches. The 8 misses are all genuine
"no Dallas property under that name" cases (cross-county, no real
estate, etc.) and not recoverable by name matching alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .dcad_owner_index import normalize_dcad_owner_name


# Surname-only fallback is risky for common surnames. Cap how many
# accounts the surname can map to before we accept the match. 2 is the
# tested threshold — catches family-trust collapses without polluting
# results with unrelated people sharing a common surname.
SURNAME_ONLY_MAX_ACCOUNTS = 2

# Matches that map to more than this many accounts are flagged with a
# warning so operators know the match might be common-name pollution
# (e.g. WILSON ROBERT — 8 different Robert Wilsons in Dallas).
COMMON_NAME_ACCOUNT_WARNING = 3


@dataclass
class ProbateMatchResult:
    """One successful match of a decedent to DCAD property records."""
    matched_name:  str            # the DCAD index key that hit
    accounts:      list[str]      # DCAD account numbers under that key
    tier:          str            # which tier produced the match
    key_source:    str            # "asis" / "rotated"
    warning:       Optional[str]  # "common_name_pollution" if len(accounts) > COMMON_NAME_ACCOUNT_WARNING


def match_decedent_to_dcad(
    decedent_name: str,
    owner_index: Mapping[str, list[str]],
) -> Optional[ProbateMatchResult]:
    """Look up a Tyler probate decedent name against the DCAD owner index.

    Args:
        decedent_name: name from re:SearchTX, typically "LAST, FIRST MIDDLE"
            but occasionally "FIRST MIDDLE LAST" (no-comma outlier).
        owner_index: ``{normalized_owner_name: [account_nums]}`` from
            ``dcad_owner_index.build_owner_index``.

    Returns:
        ``ProbateMatchResult`` on a hit, ``None`` on miss. Callers
        should treat ``None`` as "decedent doesn't own Dallas property
        under this name" and continue with the case anyway (the
        applicant is still a usable lead even without a property
        address).
    """
    if not decedent_name or not owner_index:
        return None

    for key, source, surname_first in _candidate_keys(decedent_name):
        result = _lookup_with_tiers(key, owner_index, surname_is_first_token=surname_first)
        if result is None:
            continue
        matched_name, tier, accounts = result
        warning = (
            "common_name_pollution"
            if len(accounts) > COMMON_NAME_ACCOUNT_WARNING
            else None
        )
        return ProbateMatchResult(
            matched_name=matched_name,
            accounts=list(accounts),
            tier=tier,
            key_source=source,
            warning=warning,
        )
    return None


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _candidate_keys(name: str) -> list[tuple[str, str, bool]]:
    """Return [(lookup_key, source, surname_is_first_token), ...].

    surname_is_first_token tells ``_lookup_with_tiers`` whether multi-token
    tiers are safe. True only when the first token of the lookup key is
    GUARANTEED to be the surname:
      - Tyler comma-form ("LAST, FIRST MIDDLE") in asis after normalize: True
      - Any rotated key (we explicitly moved last token to front):    True
      - Tyler no-comma form ("FIRST MIDDLE LAST") in asis:            False
    """
    has_comma = "," in name
    norm = normalize_dcad_owner_name(name)
    out: list[tuple[str, str, bool]] = []
    if norm:
        out.append((norm, "asis", has_comma))
    if not has_comma and norm:
        tokens = norm.split()
        if len(tokens) >= 2:
            rotated = " ".join([tokens[-1]] + tokens[:-1])
            if rotated != norm:
                out.append((rotated, "rotated", True))
    return out


def _lookup_with_tiers(
    key: str,
    index: Mapping[str, list[str]],
    *,
    surname_is_first_token: bool,
) -> Optional[tuple[str, str, list[str]]]:
    """Tier ladder. Returns (matched_name, tier, accounts) or None.

    See module docstring for the full ladder spec.
    """
    # Tier 1: exact — always safe regardless of token order
    if key in index:
        return key, "exact", list(index[key])

    if not surname_is_first_token:
        return None

    tokens = key.split()

    # Tier 2: no_middle (LAST FIRST)
    if len(tokens) >= 3:
        candidate = f"{tokens[0]} {tokens[1]}"
        if candidate in index:
            return candidate, "no_middle", list(index[candidate])

    # Tier 3: initial_form (LAST FIRST INITIAL_OF_MIDDLE)
    if len(tokens) >= 3:
        mi = tokens[2][:1]
        if mi.isalpha():
            candidate = f"{tokens[0]} {tokens[1]} {mi}"
            if candidate in index:
                return candidate, "initial_form", list(index[candidate])

    # Tier 3b: double_initial (LAST FIRST INIT1 INIT2)
    if len(tokens) >= 4:
        m1 = tokens[2][:1]
        m2 = tokens[3][:1]
        if m1.isalpha() and m2.isalpha():
            candidate = f"{tokens[0]} {tokens[1]} {m1} {m2}"
            if candidate in index:
                return candidate, "double_initial", list(index[candidate])

    # Tier 4: surname_only (uniqueness-gated)
    if tokens:
        surname = tokens[0]
        if surname in index:
            accts = index[surname]
            if len(accts) <= SURNAME_ONLY_MAX_ACCOUNTS:
                return surname, "surname_only", list(accts)

    return None
