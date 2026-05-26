"""
DCAD owner-name index.

Builds a reverse-lookup index ``{normalized_owner_name: [account_nums]}``
from DCAD's ACCOUNT_INFO table. Used by ``name_matcher`` to resolve
bankruptcy-filing debtor names to DCAD parcels.

Why a separate module:
    The existing ``dcad_bulk`` builds an ADDRESS-keyed index for the
    foreclosure-PDF pipeline. For bankruptcy lead matching we need an
    OWNER-keyed index. Same source table, different access pattern.

Design choices:
  - Joint ownership is expanded so each named owner is its own index
    entry pointing back to the shared account. "SMITH JOHN A & MARY S"
    -> {"SMITH JOHN A", "SMITH MARY S"} both -> [account_num].
  - Role suffixes (TRUSTEE, ETUX, ETAL, LIVING TRUST, etc.) are stripped
    before indexing so a person's name matches whether or not they hold
    title in some fiduciary capacity.
  - Field names are looked up defensively against multiple common
    candidates because DCAD's published schema has varied over time.

Public API:
  build_owner_index(dcad_tables) -> dict[str, list[str]]
  normalize_dcad_owner_name(raw) -> str | None
  expand_joint_owners(raw) -> list[str]

PR 2 fix (Phase 4 finding): the original ``_ROLE_SUFFIX_RE`` included
a standalone ``LE`` token intended to match the "Life Estate"
abbreviation. When applied to a name starting with the Vietnamese
surname "Le" (a real common Dallas surname), the regex consumed the
entire name and the entry normalized to None - silently excluding
500+ owners from the index. The fix splits the LE handling: trailing
LE is stripped separately, and the surname-LE collision is avoided.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable, Mapping


# ----------------------------------------------------------------------------
# Field-name defenses
# ----------------------------------------------------------------------------

# DCAD's ACCOUNT_INFO field names have varied across releases. Try in
# order; first hit wins.
_OWNER_NAME_CANDIDATES = (
    "OWNER_NAME",
    "OWNER_NAME1",
    "OWNERS_NAME",
    "OWNERNAME",
    "OWNER1",
    "NAME",
)
_ACCOUNT_NUM_CANDIDATES = (
    "ACCOUNT_NUM",
    "ACCOUNT_NUMBER",
    "ACCT_NUM",
    "ACCT_NO",
    "ACCOUNT_ID",
)


def _first_field(row: Mapping, candidates: Iterable[str]) -> str:
    """Return the first non-empty value among candidate field names."""
    for key in candidates:
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


# ----------------------------------------------------------------------------
# Normalization regexes
# ----------------------------------------------------------------------------

# Trailing role / fiduciary suffixes. Anything after these tokens is
# stripped so "SMITH JOHN A TRUSTEE" -> "SMITH JOHN A".
#
# PR 2 fix: removed standalone "LE" from this alternation. It was meant
# to catch the "Life Estate" abbreviation but matched Vietnamese
# surname "Le" at the start of a name. Trailing LE is handled by
# _TRAILING_LE_RE below instead.
_ROLE_SUFFIX_RE = re.compile(
    r"\b(?:"
    r"TRUSTEE|TRUSTEES|"
    r"ETUX|ET\s+UX|ETAL|ET\s+AL|"
    r"FAMILY\s+TRUST|LIVING\s+TRUST|REVOCABLE\s+TRUST|TRUST|"
    r"LIFE\s+ESTATE|"
    r"JR|SR|II|III|IV|"
    r"EXECUTOR|ADMINISTRATOR|HEIRS|HEIR\s+OF|"
    r"DECEASED|DEC\b"
    r")\b.*$",
    re.IGNORECASE,
)

# PR 2 fix: strip a trailing standalone LE token (Life Estate
# abbreviation) only when it follows other content. Requires whitespace
# before LE and end-of-string after, so a name that *starts* with LE
# (Vietnamese surname) is preserved intact.
_TRAILING_LE_RE = re.compile(r"(?<=\S)\s+LE\s*$", re.IGNORECASE)

# Joint-owner separator. DCAD uses "&" with surrounding whitespace.
_JOINT_SEP_RE = re.compile(r"\s*&\s*")

# Whitespace + punctuation cleanup.
_PUNCT_RE = re.compile(r"[,.;]")
_WS_RE = re.compile(r"\s+")


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def build_owner_index(
    dcad_tables: Mapping,
    *,
    table_name: str = "ACCOUNT_INFO",
) -> dict[str, list[str]]:
    """Build ``{normalized_owner_name: [account_nums]}`` from DCAD tables.

    Joint owners each get their own index entry pointing to the same
    account. Identical normalized names across multiple accounts (a
    homeowner who owns 2+ properties) collect into one list per name.

    Args:
        dcad_tables: dict of {table_name: rows}. ``rows`` may be either
            a list[dict] OR a pandas DataFrame — ``dcad_bulk.parse_dcad_tables``
            returns DataFrames, but the function originally took list[dict]
            from a removed alternate loader. Both shapes are accepted so
            production (DataFrame) and tests (list[dict]) work.
        table_name: which DCAD table holds owner data. Default
            ``"ACCOUNT_INFO"``.

    Returns:
        ``{owner_name: [account_num, ...]}``. Empty dict if the
        target table is missing or all rows lack owner fields.
    """
    table = dcad_tables.get(table_name)
    rows = _coerce_to_row_dicts(table)
    index: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        raw_owner = _first_field(row, _OWNER_NAME_CANDIDATES)
        account = _first_field(row, _ACCOUNT_NUM_CANDIDATES)
        if not raw_owner or not account:
            continue
        for individual in expand_joint_owners(raw_owner):
            normalized = normalize_dcad_owner_name(individual)
            if normalized:
                index[normalized].append(account)

    return dict(index)


def _coerce_to_row_dicts(table) -> list[dict]:
    """Accept either a list[dict] or a pandas DataFrame; return list[dict]."""
    if table is None:
        return []
    # DataFrame has a .to_dict method that takes orient="records"
    if hasattr(table, "to_dict") and hasattr(table, "columns"):
        if table.empty:
            return []
        return table.to_dict("records")
    # Already a list/iterable of dicts
    return list(table)


def normalize_dcad_owner_name(raw: str | None) -> str | None:
    """Normalize a single DCAD owner-name string for hash matching.

    Steps:
      1. Uppercase, strip whitespace.
      2. Remove punctuation (commas, periods, semicolons).
      3. Strip trailing "LE" only if preceded by other content
         (Life Estate abbreviation; PR 2 fix preserves leading LE).
      4. Strip trailing role suffix (TRUSTEE, JR, etc.) and everything
         after it.
      5. Collapse whitespace.

    Returns None if the result is empty.
    """
    if not raw:
        return None
    s = str(raw).upper().strip()
    s = _PUNCT_RE.sub(" ", s)
    # PR 2 fix: strip trailing LE before the general role-suffix sweep
    s = _TRAILING_LE_RE.sub("", s)
    s = _ROLE_SUFFIX_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def expand_joint_owners(raw: str) -> list[str]:
    """Split a DCAD joint-owner string into individual owners.

    Heuristics:
      - Split on ``&``.
      - The first segment is the primary owner; we keep it as-is.
      - For each subsequent segment, if it has <= 2 tokens (typically
        FIRST or FIRST MIDDLE) we assume it shares the primary's
        surname and prepend it. With 3+ tokens we assume the surname
        is included.

    Examples:
        "SMITH JOHN A & MARY S"          -> ["SMITH JOHN A", "SMITH MARY S"]
        "SMITH JOHN A & MARY"            -> ["SMITH JOHN A", "SMITH MARY"]
        "SMITH JOHN A & JONES MARY S"    -> ["SMITH JOHN A", "JONES MARY S"]
        "SMITH JOHN A"                   -> ["SMITH JOHN A"]

    Empty input returns ``[]``.
    """
    if not raw or not raw.strip():
        return []

    segments = _JOINT_SEP_RE.split(raw.strip())
    if not segments:
        return []

    out: list[str] = [segments[0].strip()]

    primary_tokens = segments[0].strip().split()
    primary_surname = primary_tokens[0] if primary_tokens else ""

    for seg in segments[1:]:
        seg = seg.strip()
        if not seg:
            continue
        tokens = seg.split()
        if len(tokens) <= 2 and primary_surname:
            out.append(f"{primary_surname} {seg}")
        else:
            out.append(seg)

    return out


# ----------------------------------------------------------------------------
# Diagnostic helpers (used by the smoke test, not by production code)
# ----------------------------------------------------------------------------


def inspect_account_info_fields(
    dcad_tables: Mapping,
    *,
    table_name: str = "ACCOUNT_INFO",
    sample_size: int = 3,
) -> dict:
    """Return diagnostic info about the ACCOUNT_INFO schema.

    Useful when the owner-index build returns 0 entries and you need to
    figure out which field names actually carry the data. Inspects the
    first few rows and reports:
      - the keys present
      - which candidates matched OWNER_NAME / ACCOUNT_NUM
      - sample values
    """
    rows = dcad_tables.get(table_name) or []
    result = {
        "table_name":     table_name,
        "row_count":      len(rows),
        "sample_rows":    [],
        "keys_observed":  [],
        "matched_owner_field":   None,
        "matched_account_field": None,
    }
    if not rows:
        return result

    first = rows[0]
    result["keys_observed"] = sorted(first.keys()) if hasattr(first, "keys") else []

    for cand in _OWNER_NAME_CANDIDATES:
        if first.get(cand):
            result["matched_owner_field"] = cand
            break
    for cand in _ACCOUNT_NUM_CANDIDATES:
        if first.get(cand):
            result["matched_account_field"] = cand
            break

    for row in rows[:sample_size]:
        result["sample_rows"].append({
            "owner_field": _first_field(row, _OWNER_NAME_CANDIDATES),
            "account":     _first_field(row, _ACCOUNT_NUM_CANDIDATES),
            "all_keys":    sorted(row.keys()) if hasattr(row, "keys") else [],
        })

    return result
