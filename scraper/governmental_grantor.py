"""Governmental-grantor suppression.

Records where the grantor is a governmental entity are noise from a
motivated-seller standpoint:
  - "TRINITY RIVER AUTHORITY" filing a Lis Pendens = water-rights / easement
    dispute, not a foreclosure on a homeowner.
  - "CITY OF DALLAS" filing a TD = the city already foreclosed and is the
    new owner; the original homeowner is no longer the seller.
  - "STATE OF TEXAS" filing TXL = tax suit; relevant lead is the grantee
    (taxpayer), not the grantor.

The fix is operationally simple: any record whose ``grantor`` matches a
governmental entity is suppressed before scoring. This matches the
existing HOA filter pattern (``normalize.is_hoa``) but for the
governmental-entity class instead of homeowner-association class.

Public API
----------
``is_governmental_grantor(name: str) -> bool``
    True if ``name`` matches the denylist or any of the configured patterns.
``filter_governmental_records(records: list[dict]) -> tuple[list[dict], list[dict]]``
    Returns ``(kept, removed)``. The ``removed`` list preserves the records
    that were suppressed so callers can log diagnostics; it is never
    written to the lead-list output.

Pattern philosophy
------------------
Patterns must be specific enough to avoid false positives:
  - ``AUTHORITY`` alone is too broad ("Smith Authority Holdings LLC" is
    not a government entity). We require qualifying co-occurrence like
    ``\\bRIVER\\s+AUTHORITY\\b`` or ``\\bTOLLWAY\\s+AUTHORITY\\b``.
  - ``STATE`` alone is too broad (could be a surname). We require
    ``\\bSTATE\\s+OF\\s+TEXAS\\b``.
  - ``CITY OF X`` is anchored to a list of known Texas cities to avoid
    matching surnames like ``CITY OF GOD INVESTMENTS``.

When in doubt, add an exact-match string to ``GOVERNMENTAL_GRANTOR_DENYLIST``
rather than loosening a pattern.
"""

from __future__ import annotations

import re
from typing import Iterable


# ─────────────────────────────────────────────────────────────────────────────
# Exact-match denylist (case-insensitive comparison against normalized name)
# ─────────────────────────────────────────────────────────────────────────────

GOVERNMENTAL_GRANTOR_DENYLIST: set[str] = {
    # Federal
    "UNITED STATES OF AMERICA",
    "UNITED STATES OF AMERICA ACTING THROUGH THE",
    "UNITED STATES OF AMERICA SECRETARY OF HOUSING",
    "UNITED STATES OF AMERICA ACTING THROUGH SBA",
    "UNITED STATES OF AMERICA INTERNAL REVENUE SERVICE",
    "FEDERAL DEPOSIT INSURANCE CORPORATION",
    "FDIC",
    "FEDERAL NATIONAL MORTGAGE ASSOCIATION",
    "FANNIE MAE",
    "FEDERAL HOME LOAN MORTGAGE CORPORATION",
    "FREDDIE MAC",
    "U S DEPARTMENT OF HOUSING AND URBAN DEVELOPMENT",
    "US DEPARTMENT OF HOUSING AND URBAN DEVELOPMENT",
    "DEPARTMENT OF HOUSING AND URBAN DEVELOPMENT",
    "HUD",
    "SECRETARY OF VETERANS AFFAIRS",
    "DEPARTMENT OF VETERANS AFFAIRS",
    "UNITED STATES SMALL BUSINESS ADMINISTRATION",
    "SMALL BUSINESS ADMINISTRATION",
    "INTERNAL REVENUE SERVICE",
    "IRS",
    # State
    "STATE OF TEXAS",
    "THE STATE OF TEXAS",
    "TEXAS WORKFORCE COMMISSION",
    "TEXAS COMPTROLLER OF PUBLIC ACCOUNTS",
    "COMPTROLLER OF PUBLIC ACCOUNTS",
    "TEXAS DEPARTMENT OF TRANSPORTATION",
    "TXDOT",
    "TEXAS DEPARTMENT OF FAMILY AND PROTECTIVE SERVICES",
    "TEXAS DEPARTMENT OF HUMAN SERVICES",
    "TEXAS DEPARTMENT OF PUBLIC SAFETY",
    "TEXAS DEPARTMENT OF CRIMINAL JUSTICE",
    "TEXAS HEALTH AND HUMAN SERVICES COMMISSION",
    # Dallas County
    "DALLAS COUNTY",
    "COUNTY OF DALLAS",
    "DALLAS COUNTY TEXAS",
    "DALLAS COUNTY COMMUNITY COLLEGE DISTRICT",
    "DALLAS COUNTY HOSPITAL DISTRICT",
    "DALLAS COUNTY UTILITY AND RECLAMATION DISTRICT",
    "DALLAS CENTRAL APPRAISAL DISTRICT",
    "DCAD",
    # Dallas-area ISD acronyms — single-word forms don't match the
    # `\w+ ISD` pattern, so they need explicit listing here.
    "DISD",
    "GISD",   # Garland / Grand Prairie (collision is fine — both are govt)
    "RISD",   # Richardson
    "MISD",   # Mesquite
    "IISD",   # Irving
    "PISD",   # Plano
    "FISD",   # Frisco
    "DESOTO ISD",
    "LANCASTER ISD",
    "CEDAR HILL ISD",
    "DUNCANVILLE ISD",
    "CARROLLTON FARMERS BRANCH ISD",
    "COPPELL ISD",
    "HIGHLAND PARK ISD",
    # Regional authorities
    "TRINITY RIVER AUTHORITY",
    "TRINITY RIVER AUTHORITY OF TEXAS",
    "NORTH TEXAS TOLLWAY AUTHORITY",
    "NORTH TEXAS MUNICIPAL WATER DISTRICT",
    "DALLAS COUNTY WATER CONTROL AND IMPROVEMENT DISTRICT",
    "DALLAS AREA RAPID TRANSIT",
    "DART",
    # Common foreclosing servicers acting as govt agents (post-foreclosure)
    "FEDERAL HOME LOAN BANK OF DALLAS",
}


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns (compiled case-insensitive)
# ─────────────────────────────────────────────────────────────────────────────

# Texas cities frequently appearing as grantors (condemnations, tax sales,
# easements). Keep this list aligned with config.DALLAS_CITIES where they
# overlap; cities outside Dallas County are also included because some
# regional filings cross county lines.
_KNOWN_TEXAS_GOV_CITIES = (
    "DALLAS|FORT WORTH|ARLINGTON|GARLAND|MESQUITE|IRVING|GRAND PRAIRIE|"
    "RICHARDSON|PLANO|FRISCO|MCKINNEY|CARROLLTON|LEWISVILLE|DENTON|"
    "ROWLETT|DESOTO|LANCASTER|CEDAR HILL|DUNCANVILLE|FARMERS BRANCH|"
    "ADDISON|UNIVERSITY PARK|HIGHLAND PARK|COCKRELL HILL|BALCH SPRINGS|"
    "HUTCHINS|WILMER|SEAGOVILLE|GLENN HEIGHTS|COMBINE|SUNNYVALE|"
    "COPPELL|SACHSE|WYLIE|MURPHY|PARKER|FAIRVIEW|ALLEN"
)

GOVERNMENTAL_GRANTOR_PATTERNS: list[str] = [
    # Government roles / agencies — anchored
    r"\bSTATE\s+OF\s+TEXAS\b",
    r"\bUNITED\s+STATES\s+OF\s+AMERICA\b",
    r"\bUNITED\s+STATES\b\s+(?:OF\s+AMERICA\s+)?(?:ACTING|BY\s+AND\s+THROUGH|SECRETARY\s+OF)",
    r"\bU\.?S\.?\s+DEPARTMENT\s+OF\b",
    r"\bDEPARTMENT\s+OF\s+(?:HOUSING|VETERANS|TRANSPORTATION|JUSTICE|TREASURY|AGRICULTURE)\b",
    r"\bSECRETARY\s+OF\s+(?:HOUSING|VETERANS|TREASURY|STATE)\b",
    # City of <known TX city>
    rf"\bCITY\s+OF\s+(?:{_KNOWN_TEXAS_GOV_CITIES})\b",
    rf"\bTOWN\s+OF\s+(?:{_KNOWN_TEXAS_GOV_CITIES})\b",
    # Dallas County variants
    r"\bDALLAS\s+COUNTY\b",
    r"\bCOUNTY\s+OF\s+DALLAS\b",
    # School districts (broad — many districts file as grantors for tax sales)
    r"\b\w+\s+INDEPENDENT\s+SCHOOL\s+DISTRICT\b",
    r"\b[A-Z][A-Z]+\s+ISD\b",            # e.g. RICHARDSON ISD, DISD, GISD
    # Water / utility / regional authorities (qualified — avoid bare AUTHORITY)
    r"\b\w+\s+RIVER\s+AUTHORITY\b",
    r"\b\w+\s+TOLLWAY\s+AUTHORITY\b",
    r"\b\w+\s+WATER\s+CONTROL\s+AND\s+IMPROVEMENT\s+DISTRICT\b",
    r"\bMUNICIPAL\s+WATER\s+DISTRICT\b",
    r"\bMUNICIPAL\s+UTILITY\s+DISTRICT\b",
    r"\b\w+\s+MUNICIPAL\s+UTILITY\s+DISTRICT\b",
    r"\bRURAL\s+WATER\s+SUPPLY\b",
    r"\bWATER\s+SUPPLY\s+CORPORATION\b",
    # Federal lenders / agencies acting as grantors after foreclosure
    r"\bFEDERAL\s+HOME\s+LOAN\b",
    r"\bFEDERAL\s+DEPOSIT\s+INSURANCE\b",
    r"\bFEDERAL\s+NATIONAL\s+MORTGAGE\b",
    r"\bFANNIE\s+MAE\b",
    r"\bFREDDIE\s+MAC\b",
    r"\bGINNIE\s+MAE\b",
    # Tax / regulatory
    r"\bINTERNAL\s+REVENUE\s+SERVICE\b",
    r"\bCOMPTROLLER\s+OF\s+PUBLIC\s+ACCOUNTS\b",
    # Common acronym fragments (be precise — these can appear in legal names)
    r"\bHUD\s+SECRETARY\b",
    r"\bSBA\s+(?:U\.?S\.?\s+)?SMALL\s+BUSINESS\b",
    # Tax-collection law firms acting as agents for taxing entities — these
    # are NOT homeowner sellers, they're the collectors. Their tax sale
    # filings should be processed from the Linebarger tax-sale source
    # directly in Phase 4, not picked up here as motivated-seller LP records.
    r"\bLINEBARGER\s+GOGGAN\b",
    r"\bPERDUE\s+BRANDON\s+FIELDER\b",
    r"\bMCCREARY\s+VESELKA\s+BRAGG\b",
]


_COMPILED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in GOVERNMENTAL_GRANTOR_PATTERNS
]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_for_comparison(name: str) -> str:
    """Collapse whitespace and uppercase for denylist comparison."""
    return " ".join(name.upper().split())


def is_governmental_grantor(name: str | None) -> bool:
    """Return True if ``name`` matches a governmental entity.

    The check is two-stage:
      1. Exact-match against ``GOVERNMENTAL_GRANTOR_DENYLIST`` (after
         whitespace normalization, case-insensitive).
      2. Regex match against any pattern in ``GOVERNMENTAL_GRANTOR_PATTERNS``.
    """
    if not name:
        return False
    normalized = _normalize_for_comparison(name)
    if normalized in GOVERNMENTAL_GRANTOR_DENYLIST:
        return True
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(name):
            return True
    return False


def filter_governmental_records(
    records: Iterable[dict],
) -> tuple[list[dict], list[dict]]:
    """Partition records into (kept, removed).

    A record is removed if its ``grantor`` matches a governmental entity.
    Records with no grantor (None or empty) are kept — we cannot conclude
    they are governmental, and erring on the side of inclusion is correct
    for a downstream operator-review workflow.

    The ``removed`` list preserves the records that were suppressed so
    callers can log diagnostic counts. It is never written to the
    lead-list output.
    """
    kept: list[dict] = []
    removed: list[dict] = []
    for rec in records:
        grantor = rec.get("grantor")
        if is_governmental_grantor(grantor):
            removed.append(rec)
        else:
            kept.append(rec)
    return kept, removed


def filter_governmental_records_dual(
    records: Iterable[dict],
    *,
    also_check_grantee: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Variant that optionally also checks the grantee.

    Most acquisitions workflows want grantor-only filtering. Use this
    variant only when the target source legitimately puts the homeowner
    in the grantor slot and the government entity in the grantee slot
    (e.g., a deed FROM the homeowner TO the city — rare). Default keeps
    the safer grantor-only semantics.
    """
    kept: list[dict] = []
    removed: list[dict] = []
    for rec in records:
        grantor = rec.get("grantor")
        grantee = rec.get("grantee") if also_check_grantee else None
        if is_governmental_grantor(grantor) or (
            also_check_grantee and is_governmental_grantor(grantee)
        ):
            removed.append(rec)
        else:
            kept.append(rec)
    return kept, removed
