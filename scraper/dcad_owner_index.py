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
_ROLE_SUFFIX_RE = re.compile(
    r"\b(?:"
    r"TRUSTEE|TRUSTEES|"
    r"ETUX|ET\s+UX|ETAL|ET\s+AL|"
    r"FAMILY\s+TRUST|LIVING\s+TRUST|REVOCABLE\s+TRUST|TRUST|"
    r"LIFE\s+ESTATE|LE|"
    r"JR|SR|II|III|IV|"
    r"EXECUTOR|ADMINISTRATOR|HEIRS|HEIR\s+OF|"
    r"DECEASED|DEC\b"
    r")\b.*$",
    re.IGNORECASE,
)

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
        dcad_tables: dict of {table_name: list[row]} as returned by
            ``dcad_bulk.parse_dcad_tables``.
        table_name: which DCAD table holds owner data. Default
            ``"ACCOUNT_INFO"``.

    Returns:
        ``{owner_name: [account_num, ...]}``. Empty dict if the
        target table is missing or all rows lack owner fields.
    """
    rows = dcad_tables.get(table_name) or []
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


def normalize_dcad_owner_name(raw: str | None) -> str | None:
    """Normalize a single DCAD owner-name string for hash matching.

    Steps:
      1. Uppercase, strip whitespace.
      2. Remove punctuation (commas, periods, semicolons).
      3. Strip trailing role suffix (TRUSTEE, ETUX, JR, etc.) and
         everything after it.
      4. Collapse whitespace.

    Returns None if the result is empty.
    """
    if not raw:
        return None
    s = str(raw).upper().strip()
    s = _PUNCT_RE.sub(" ", s)
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

    # The primary owner is always emitted as-is (post-normalization
    # happens later in normalize_dcad_owner_name).
    out: list[str] = [segments[0].strip()]

    primary_tokens = segments[0].strip().split()
    primary_surname = primary_tokens[0] if primary_tokens else ""

    for seg in segments[1:]:
        seg = seg.strip()
        if not seg:
            continue
        tokens = seg.split()
        if len(tokens) <= 2 and primary_surname:
            # Probably "MARY S" -> prepend "SMITH"
            out.append(f"{primary_surname} {seg}")
        else:
            # Has its own surname (e.g. "JONES MARY S") or is malformed
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
