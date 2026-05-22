"""Corporate-entity grantee suppression.

A motivated-seller workflow looks for records where the lead target is
an individual property owner. Records where the target field (typically
grantee) is a corporate or institutional entity are noise:

  - "ACME PROPERTIES LLC" buying foreclosure properties: investor, not
    a distressed seller.
  - "U S BANK TRUST NATIONAL ASSOCIATION TR" on a TD: the bank now
    owns the property; the original homeowner is gone.
  - "CHRIST FOR ALL NATIONS CHURCH" on a lien: institutional entity,
    not a household.

This module is the corporate-entity analog of governmental_grantor.py.
Where governmental_grantor checks the grantor for government entities,
entity_filter checks the GRANTEE for corporate/institutional entities
(with one special case: LC city-lien records swap to grantor when
grantee is governmental, since the property owner is on the grantor
side for those).

Coverage scope (intentional):
  - Corporate suffixes (LLC, INC, CORP, LTD)
  - Business descriptors (HOLDINGS, PROPERTIES, GROUP, etc.)
  - Financial institutions (BANK, MORTGAGE, etc.)
  - Contractors and trades (CONSTRUCTION, ROOFING, etc.)
  - Religious and medical institutions (CHURCH, HOSPITAL)
  - Role suffixes (TRUSTEE, TR, AGENT) - person acting in fiduciary
    capacity is not reachable as the underlying owner

Out of scope (handled elsewhere):
  - Governmental entities -> governmental_grantor.py
  - HOA / Homeowners Association -> normalize.is_hoa_entity (used by
    scorer.filter_hoa)

Family-trust escape: names matching FAMILY TRUST, LIVING TRUST, etc.
are NOT treated as corporate entities. They represent real
estate-liquidation lead candidates (death of grantor, estate planning).

Public API
----------
is_corporate_entity(name) -> bool
    True if name matches a corporate-entity pattern (and isn't a
    family trust).

is_numeric_garbage(name) -> bool
    True if name is just digits (e.g. "9999999999" placeholder values).

has_commercial_address_suffix(name) -> bool
    True if name contains an address-style unit suffix (STE, APT,
    UNIT, etc.) - indicates the field is mis-populated with an
    address rather than a person name.

filter_entity_records(records) -> (kept, removed)
    Partition records by whether their effective target is an
    individual or a corporate entity / garbage / address-suffix.
    Includes LC city-lien special case: when LC grantee is
    governmental, the target swaps to grantor.
"""

from __future__ import annotations

import re
from typing import Iterable

from . import governmental_grantor


# ─────────────────────────────────────────────────────────────────────────────
# Corporate / institutional entity patterns
# ─────────────────────────────────────────────────────────────────────────────

ENTITY_PATTERNS: list[str] = [
    # Corporate suffixes
    r"\bLLC\b",
    r"\bL\.L\.C\.",
    r"\bLLP\b",
    r"\bL\.?P\.?\b",
    r"\bINC\.?\b",
    r"\bINCORPORATED\b",
    r"\bCORP\.?\b",
    r"\bCORPORATION\b",
    r"\bLTD\.?\b",
    r"\bLIMITED\b",
    r"\bCOMPANY\b",
    r"\bCO\.?$",
    # Business descriptors
    r"\bENTERPRISES\b",
    r"\bHOLDINGS\b",
    r"\bPROPERTIES\b",
    r"\bINVESTMENTS\b",
    r"\bPARTNERS\b",
    r"\bGROUP\b",
    r"\bPIPELINE\b",
    r"\bSYNERGY\b",
    r"\bCONSTRUCTORS\b",
    r"\bSOLUTIONS\b",
    r"\bMULTIFAMILY\b",
    # Financial institutions
    r"\bBANK\b",
    r"\bMORTGAGE\b",
    r"\bCAPITAL\b",
    r"\bLENDING\b",
    r"\bLOAN\b",
    r"\bFINANCIAL\b",
    r"\bFINANCE\b",
    r"\bFUNDING\b",
    r"\bTRUST\s+CO\b",
    r"\bNATIONAL\s+ASSOCIATION\b",
    r"\bN\.A\.",
    r"\bFSB\b",
    r"\bFREDDIE\s+MAC\b",
    r"\bFANNIE\s+MAE\b",
    r"\bCREDIT\s+UNION\b",
    # Contractors / trades
    r"\bCONSTRUCTION\b",
    r"\bDEVELOPMENT\b",
    r"\bROOFING\b",
    r"\bPLUMBING\b",
    r"\bELECTRIC\b",
    r"\bCONTRACTORS?\b",
    r"\bBUILDERS?\b",
    r"\bSERVICES?\b",
    r"\bREAL\s+ESTATE\b",
    r"\bREALTY\b",
    r"\bPAINTING\b",
    r"\bREMODELING\b",
    r"\bHOMES\b",
    r"\bOFFICE\b",
    # Religious and medical institutions
    r"\bCHURCH\b",
    r"\bBIBLE\b",
    r"\bHOSPITAL\b",
    r"\bHEALTH\s+SYSTEM\b",
    r"\bAUXILIARY\b",
    r"\bCOPTIC\b",
    # Role suffixes (acting-as, not reachable as underlying owner)
    r"\bTRUSTEE$",
    r"\bTR$",
    r"\bAGENT$",
]


# ─────────────────────────────────────────────────────────────────────────────
# Family-trust escape patterns (these are NOT treated as entities)
# ─────────────────────────────────────────────────────────────────────────────

FAMILY_TRUST_PATTERNS: list[str] = [
    r"\bFAMILY\s+TRUST\b",
    r"\bLIVING\s+TRUST\b",
    r"\bREVOCABLE\s+TRUST\b",
    r"\bIRREVOCABLE\s+TRUST\b",
    r"\bTESTAMENTARY\s+TRUST\b",
]


# ─────────────────────────────────────────────────────────────────────────────
# Commercial address-suffix patterns (target name contains a unit indicator)
# ─────────────────────────────────────────────────────────────────────────────

COMMERCIAL_SUFFIX_PATTERNS: list[str] = [
    r"\bSTE\s+\w+",
    r"\bSUITE\s+\w+",
    r"\bUNIT\s+\w+",
    r"\bAPT\s+\w+",
    r"\bBLDG\s+\w+",
    r"#\d+\b",
]


# Compiled patterns (case-insensitive)
_COMPILED_ENTITY: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in ENTITY_PATTERNS
]
_COMPILED_FAMILY_TRUST: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in FAMILY_TRUST_PATTERNS
]
_COMPILED_COMMERCIAL: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in COMMERCIAL_SUFFIX_PATTERNS
]


# ─────────────────────────────────────────────────────────────────────────────
# Public predicates
# ─────────────────────────────────────────────────────────────────────────────


def is_corporate_entity(name: str | None) -> bool:
    """True if name is a corporate / institutional entity.

    A family-trust name (e.g. "SMITH FAMILY TRUST") returns False even
    if it would otherwise match an entity pattern, because such trusts
    represent legitimate motivated-seller lead candidates.

    Empty / None returns False (no information to decide on).
    """
    if not name or not name.strip():
        return False

    # Family-trust escape: check first, if matched, NOT an entity
    for pattern in _COMPILED_FAMILY_TRUST:
        if pattern.search(name):
            return False

    # Entity pattern check
    for pattern in _COMPILED_ENTITY:
        if pattern.search(name):
            return True

    return False


def is_numeric_garbage(name: str | None) -> bool:
    """True if name is just digits (placeholder values like "9999999999").

    Strips whitespace, hyphens, and underscores before checking.
    Empty / None returns False.
    """
    if not name or not name.strip():
        return False
    cleaned = re.sub(r"[\s\-_]", "", name)
    return cleaned.isdigit() and len(cleaned) > 0


def has_commercial_address_suffix(name: str | None) -> bool:
    """True if name contains an address-style unit suffix.

    A name like "POLK CHRISTI E STE 131" suggests the field is
    mis-populated with an address rather than a person name. Such
    records are unreliable lead targets even if the name otherwise
    looks individual.

    Empty / None returns False.
    """
    if not name or not name.strip():
        return False
    for pattern in _COMPILED_COMMERCIAL:
        if pattern.search(name):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Filter entry point
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_target(rec: dict) -> tuple[str, str]:
    """Resolve effective target field for a record.

    Returns (target_value, target_source) where target_source is
    "grantee" for the normal case or "grantor" for the swap cases.

    Swap cases (return grantor, not grantee):

      1. LC city-lien swap: when an LC record has a governmental grantee
         (City of Dallas, DISD, etc.), the property owner is on the
         grantor side. We swap so the filter checks the actual owner
         candidate.

      2. NOF (Notice of Foreclosure) records: the grantee is always
         absent at notice-filing time (the auction hasn't happened, so
         there's no buyer). The motivated-seller lead IS the grantor
         (the homeowner being foreclosed on). Without this swap every
         NOF record falls through the empty-target check below and gets
         wrongly suppressed.
    """
    code = rec.get("dallas_code", "")
    grantor = (rec.get("grantor") or "").strip()
    grantee = (rec.get("grantee") or "").strip()

    if code == "NOF":
        return (grantor, "grantor")
    if code == "LC" and governmental_grantor.is_governmental_grantor(grantee):
        return (grantor, "grantor")
    return (grantee, "grantee")


def filter_entity_records(
    records: Iterable[dict],
) -> tuple[list[dict], list[dict]]:
    """Partition records into (kept, removed).

    A record is removed if its effective target field is:
      - empty,
      - numeric garbage (e.g. "9999999999"),
      - a corporate / institutional entity (with family-trust escape),
      - or contains a commercial address suffix.

    LC city-lien special case: when LC grantee is governmental, the
    grantor is treated as the effective target.

    The ``removed`` list preserves the records that were suppressed
    so callers can log diagnostics; it is never written to the
    lead-list output.
    """
    kept: list[dict] = []
    removed: list[dict] = []

    for rec in records:
        target, _source = _resolve_target(rec)
        code = rec.get("dallas_code", "")

        # NOF exception: empty grantor means OCR didn't extract the
        # owner name from the notice scan. The record is still a valid
        # motivated-seller lead -- it has a sale_date, source_url, and
        # often a city-level address. The operator can click into the
        # source URL to read the name. Don't drop these for missing
        # grantor. We still apply the corporate-entity check below when
        # grantor IS populated, so commercial foreclosures are filtered.
        if not target and code == "NOF":
            kept.append(rec)
            continue

        if not target:
            removed.append(rec)
            continue
        if is_numeric_garbage(target):
            removed.append(rec)
            continue
        if is_corporate_entity(target):
            removed.append(rec)
            continue
        if has_commercial_address_suffix(target):
            removed.append(rec)
            continue

        kept.append(rec)

    return kept, removed
