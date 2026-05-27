"""Resolution-paths schema scaffolding.

This module defines the ``signal_metadata`` extensions used by the
multi-path DCAD lead-match recovery design (see
``docs/RESOLUTION_PATHS_DESIGN.md``). It is intentionally a foundation
layer — no resolver logic lives here. Subsequent PRs (Path B, Path A,
Path C, confidence audit, PB retrofit) write to and read from these
helpers.

Schema additions to ``signal_metadata``::

    resolution_history: list[ResolutionHistoryEntry]   # full audit trail
    primary_resolution: str | None                      # which path won
    confidence:         str                              # high|medium|low|none
    confidence_warnings: list[str]                       # canonical warning codes
    alternate_accounts: list[str]                        # multi_match candidates

All helpers are lazy-safe: they accept records that lack
``signal_metadata`` entirely (probate.txcourts.gov-sourced records
historically have ``signal_metadata: None``). On first write the helper
materializes the dict.

This module deliberately has no dependency on other scraper modules so
it can be imported by all consumers without circular-import risk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Canonical enums — PRs 2-7 must use these constants when writing history
# ═══════════════════════════════════════════════════════════════════════════

# Path identifiers. Used in ResolutionHistoryEntry.path and
# signal_metadata.primary_resolution.
PATH_STAGE_7_ADDRESS_INDEX     = "stage_7_address_index"
PATH_B_RAW_EXCERPT             = "path_b_raw_excerpt"
PATH_VARIANT_LOOKUP            = "path_variant_lookup"             # PR 2.5
PATH_A_GRANTOR_OWNER_INDEX     = "path_a_grantor_owner_index"
PATH_C_LEGAL_RESOLVER          = "path_c_legal_resolver"
PATH_C_APN                     = "path_c_apn"
PATH_STAGE_6_6                 = "stage_6_6_agreement"            # PR 5
PATH_PB_DECEDENT_OWNER_INDEX   = "path_pb_decedent_owner_index"   # PB retrofit (PR 6)
PATH_PB_APPLICANT_OWNER_INDEX  = "path_pb_applicant_owner_index"  # PB retrofit (PR 6)

VALID_PATHS: frozenset[str] = frozenset({
    PATH_STAGE_7_ADDRESS_INDEX,
    PATH_B_RAW_EXCERPT,
    PATH_VARIANT_LOOKUP,
    PATH_A_GRANTOR_OWNER_INDEX,
    PATH_C_LEGAL_RESOLVER,
    PATH_C_APN,
    PATH_STAGE_6_6,
    PATH_PB_DECEDENT_OWNER_INDEX,
    PATH_PB_APPLICANT_OWNER_INDEX,
})

# Status values for a single resolution attempt.
STATUS_MATCHED      = "matched"
STATUS_NO_MATCH     = "no_match"
STATUS_MULTI_MATCH  = "multi_match"
STATUS_SKIPPED      = "skipped"
STATUS_GUARDED      = "guarded"
STATUS_ERROR        = "error"

VALID_STATUSES: frozenset[str] = frozenset({
    STATUS_MATCHED, STATUS_NO_MATCH, STATUS_MULTI_MATCH,
    STATUS_SKIPPED, STATUS_GUARDED, STATUS_ERROR,
})

# Tier values for Path A / Path C matches. None for paths that don't use tiers.
TIER_EXACT             = "exact"
TIER_NO_MIDDLE         = "no_middle"
TIER_INITIAL_FORM      = "initial_form"
TIER_DOUBLE_INITIAL    = "double_initial"
TIER_SURNAME_ONLY      = "surname_only"
TIER_FUZZY_SUBDIVISION = "fuzzy_subdivision"

VALID_TIERS: frozenset[str] = frozenset({
    TIER_EXACT, TIER_NO_MIDDLE, TIER_INITIAL_FORM,
    TIER_DOUBLE_INITIAL, TIER_SURNAME_ONLY, TIER_FUZZY_SUBDIVISION,
})

# Confidence levels for the overall record (assigned at Stage 6.6).
CONFIDENCE_HIGH   = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW    = "low"
CONFIDENCE_NONE   = "none"

VALID_CONFIDENCES: frozenset[str] = frozenset({
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_NONE,
})

# Canonical warning codes. Stamped onto signal_metadata.confidence_warnings
# (record-level) and ResolutionHistoryEntry.warnings (per-attempt).
WARN_MULTI_ACCOUNT                = "multi_account"
WARN_SURNAME_ONLY_TIER            = "surname_only_tier"
WARN_COMMON_NAME_POLLUTION        = "common_name_pollution"
WARN_SURNAME_DRIFT                = "surname_drift"
WARN_LOW_MARKET_VALUE             = "low_market_value"
WARN_SURNAME_IN_TRUST_FIRST_NAME  = "surname_in_trust_first_name"
WARN_PATH_DISAGREEMENT            = "path_disagreement"
WARN_PATH_AGREEMENT               = "path_agreement"               # PR 5: 2+ paths agreed on same dcad_account
WARN_TIEBROKEN_BY_PAGE            = "tiebroken_by_page"            # PR 5: Stage 6.6 used publicsearch page as referee
WARN_PAGE_TIEBREAK_INCONCLUSIVE   = "page_tiebreak_inconclusive"   # PR 5: page text gave no signal between candidates
WARN_PATH_B_USED_ALTERNATE        = "path_b_used_alternate"
WARN_FUZZY_SUBDIVISION_MATCH      = "fuzzy_subdivision_match"
WARN_VENUE_OR_TRUSTEE_ADDRESS     = "venue_or_trustee_address"
WARN_LOOKUP_VARIANT_USED          = "lookup_variant_used"

CANONICAL_WARNINGS: frozenset[str] = frozenset({
    WARN_MULTI_ACCOUNT,
    WARN_SURNAME_ONLY_TIER,
    WARN_COMMON_NAME_POLLUTION,
    WARN_SURNAME_DRIFT,
    WARN_LOW_MARKET_VALUE,
    WARN_SURNAME_IN_TRUST_FIRST_NAME,
    WARN_PATH_DISAGREEMENT,
    WARN_PATH_AGREEMENT,
    WARN_TIEBROKEN_BY_PAGE,
    WARN_PAGE_TIEBREAK_INCONCLUSIVE,
    WARN_PATH_B_USED_ALTERNATE,
    WARN_FUZZY_SUBDIVISION_MATCH,
    WARN_VENUE_OR_TRUSTEE_ADDRESS,
    WARN_LOOKUP_VARIANT_USED,
})


# ═══════════════════════════════════════════════════════════════════════════
# Shared reject/blocklists — Path B (PR 2) and Path A (PR 3) consume these
# ═══════════════════════════════════════════════════════════════════════════

# Known venue / trustee / law-firm addresses that have been observed
# leaking into the ``address`` field via OCR pattern matches. Compared
# against ``address.upper()`` via substring containment.
#
# 600 COMMERCE — George Allen Courts Building (Dallas foreclosure
#   auction venue), present on every NOF doc body.
# 20405 STATE HWY — Codilis & Moody P.C. Houston office (trustee firm).
# 15851 N DALLAS PKWY — Addison law firm.
# 7730 MARKET CENTER — El Paso trustee.
# 17100 GILLETTE — ServiceLink Agency Sales, Irvine CA.
# 5204 VILLAGE CREEK — Veracity Inc, Plano (foreclosure-services firm).
# 14800 LANDMARK — Addison law firm.
VENUE_TRUSTEE_SIGS: tuple[str, ...] = (
    "600 COMMERCE",
    "GEORGE ALLEN",
    "20405 STATE HWY",
    "20405 STATE HIGHWAY",
    "15851 N DALLAS PKWY",
    "15851 N. DALLAS PARKWAY",
    "15851 N DALLAS PARKWAY",       # no-period variant
    "7730 MARKET CENTER",
    "17100 GILLETTE",
    "5204 VILLAGE CREEK",
    "14800 LANDMARK",
)


# Single-token grantor strings observed leaking from foreclosure-notice
# OCR boilerplate. Path A (PR 3) skips records whose grantor reduces to
# one of these tokens because they are NEVER real person names.
#
# Example leakage: the regex ``([A-Z]+) as Grantor/Borrower`` captured
# ``LIABLE`` from "...BUT NOT TO OTHERWISE BE LIABLE as Grantor/Borrower".
GRANTOR_BOILERPLATE_BLOCKLIST: frozenset[str] = frozenset({
    "LIABLE",
    "MORTGAGOR",
    "BORROWER",
    "TRUSTEE",
    "DEBTOR",
    "GRANTOR",
    "EXECUTOR",
    "ATTORNEY",
    "AGENT",
    "BENEFICIARY",
})


# ═══════════════════════════════════════════════════════════════════════════
# History entry — dataclass + dict-conversion
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ResolutionHistoryEntry:
    """One resolution attempt's audit trail.

    Designed to be a plain data carrier — no behavior. The
    ``to_dict()`` form is what lands in ``signal_metadata.resolution_history``
    (and therefore in records.json + CSV exports).

    Fields:
      path          — one of the PATH_* constants (validated)
      stage         — pipeline stage label (e.g. "6.3", "6.4", "7")
      input         — the lookup key actually queried (e.g. "OUSLEY SKYLER",
                      "4314 HAMILTON")
      status        — one of the STATUS_* constants (validated)
      skip_reason   — set only when status=STATUS_SKIPPED; free-form short
                      identifier (e.g. "already_resolved", "boilerplate_grantor")
      tier          — one of the TIER_* constants for Path A/C, else None
      dcad_account  — primary account result; None on miss/skip
      alternates    — other candidate accounts in multi_match cases
      warnings      — per-attempt concerns (subset of CANONICAL_WARNINGS)
    """

    path:         str
    stage:        str
    input:        Optional[str]    = None
    status:       str              = STATUS_NO_MATCH
    skip_reason:  Optional[str]    = None
    tier:         Optional[str]    = None
    dcad_account: Optional[str]    = None
    alternates:   list[str]        = field(default_factory=list)
    warnings:     list[str]        = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path not in VALID_PATHS:
            raise ValueError(
                f"ResolutionHistoryEntry.path={self.path!r} not in VALID_PATHS. "
                f"Use one of the PATH_* constants from scraper.resolution."
            )
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"ResolutionHistoryEntry.status={self.status!r} not in VALID_STATUSES."
            )
        if self.tier is not None and self.tier not in VALID_TIERS:
            raise ValueError(
                f"ResolutionHistoryEntry.tier={self.tier!r} not in VALID_TIERS."
            )
        unknown_warnings = set(self.warnings) - CANONICAL_WARNINGS
        if unknown_warnings:
            raise ValueError(
                f"ResolutionHistoryEntry.warnings contains non-canonical values: "
                f"{sorted(unknown_warnings)}. Use WARN_* constants."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for records.json. Matches docs/RESOLUTION_PATHS_DESIGN.md §3."""
        return {
            "path":         self.path,
            "stage":        self.stage,
            "input":        self.input,
            "status":       self.status,
            "skip_reason":  self.skip_reason,
            "tier":         self.tier,
            "dcad_account": self.dcad_account,
            "alternates":   list(self.alternates),
            "warnings":     list(self.warnings),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Record-level helpers — lazy-safe ``signal_metadata`` mutation
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_signal_metadata(record: dict) -> dict:
    """Return ``record["signal_metadata"]``, materializing it if absent or None.

    Some sources (notably probate.txcourts.gov canonicalization) set
    ``signal_metadata: None`` rather than {}. This helper normalizes both
    to a usable dict so downstream writes don't AttributeError.
    """
    sm = record.get("signal_metadata")
    if sm is None:
        sm = {}
        record["signal_metadata"] = sm
    elif not isinstance(sm, dict):
        # Unexpected shape — replace with empty dict and log. Don't silently
        # corrupt; this is a forensic signal.
        logger.warning(
            "signal_metadata had non-dict type %s on record %s; replacing with empty dict",
            type(sm).__name__, record.get("record_id"),
        )
        sm = {}
        record["signal_metadata"] = sm
    return sm


def append_history(record: dict, entry: ResolutionHistoryEntry) -> None:
    """Append a resolution-attempt entry to the record's history.

    Lazy-safe: materializes ``signal_metadata`` and ``resolution_history``
    on first write. Idempotency is NOT enforced — callers may append the
    same path multiple times if the resolver is re-run.
    """
    sm = _ensure_signal_metadata(record)
    history = sm.get("resolution_history")
    if not isinstance(history, list):
        history = []
        sm["resolution_history"] = history
    history.append(entry.to_dict())


def get_history(record: dict) -> list[dict]:
    """Return the resolution history list (read-only — caller should not mutate).

    Returns ``[]`` if absent. Never raises on malformed records.
    """
    sm = record.get("signal_metadata")
    if not isinstance(sm, dict):
        return []
    history = sm.get("resolution_history")
    if not isinstance(history, list):
        return []
    return history


def set_primary_resolution(record: dict, path: str) -> None:
    """Mark which path's match became the canonical ``dcad_account``.

    Validates against VALID_PATHS so a typo in a PR doesn't silently
    corrupt the provenance field.
    """
    if path not in VALID_PATHS:
        raise ValueError(
            f"set_primary_resolution: path={path!r} not in VALID_PATHS. "
            f"Use one of the PATH_* constants."
        )
    sm = _ensure_signal_metadata(record)
    sm["primary_resolution"] = path


def get_primary_resolution(record: dict) -> Optional[str]:
    """Which path stamped this record's ``dcad_account``? ``None`` if not set."""
    sm = record.get("signal_metadata")
    if not isinstance(sm, dict):
        return None
    val = sm.get("primary_resolution")
    return val if isinstance(val, str) else None


def set_confidence(record: dict, level: str) -> None:
    """Stamp the record's overall confidence level.

    Validates against VALID_CONFIDENCES so unknown levels can't leak
    into records.json and downstream consumers (dashboard, CSV).
    """
    if level not in VALID_CONFIDENCES:
        raise ValueError(
            f"set_confidence: level={level!r} not in VALID_CONFIDENCES. "
            f"Use CONFIDENCE_HIGH/MEDIUM/LOW/NONE."
        )
    sm = _ensure_signal_metadata(record)
    sm["confidence"] = level


def get_confidence(record: dict) -> str:
    """Return the record's confidence level. Defaults to ``CONFIDENCE_NONE``
    for legacy records that lack the field (lazy-migration semantics per
    docs/RESOLUTION_PATHS_DESIGN.md §14 Q5)."""
    sm = record.get("signal_metadata")
    if not isinstance(sm, dict):
        return CONFIDENCE_NONE
    val = sm.get("confidence")
    if isinstance(val, str) and val in VALID_CONFIDENCES:
        return val
    return CONFIDENCE_NONE


def add_warning(record: dict, warning: str) -> None:
    """Append a confidence warning to the record (idempotent — no duplicates).

    Validates against CANONICAL_WARNINGS so PRs can't introduce ad-hoc
    warning strings that the dashboard / CSV won't know how to render.
    """
    if warning not in CANONICAL_WARNINGS:
        raise ValueError(
            f"add_warning: warning={warning!r} not in CANONICAL_WARNINGS. "
            f"Use one of the WARN_* constants."
        )
    sm = _ensure_signal_metadata(record)
    warnings = sm.get("confidence_warnings")
    if not isinstance(warnings, list):
        warnings = []
        sm["confidence_warnings"] = warnings
    if warning not in warnings:
        warnings.append(warning)


def get_warnings(record: dict) -> list[str]:
    """Return the record's confidence warnings. ``[]`` if absent."""
    sm = record.get("signal_metadata")
    if not isinstance(sm, dict):
        return []
    val = sm.get("confidence_warnings")
    return list(val) if isinstance(val, list) else []


def set_alternate_accounts(record: dict, accounts: Iterable[str]) -> None:
    """Stamp the list of alternate DCAD accounts (multi_match secondary candidates).

    Always writes a fresh list — caller is responsible for deduplication
    if calling multiple times. Empty iterable clears the field.
    """
    sm = _ensure_signal_metadata(record)
    sm["alternate_accounts"] = [str(a) for a in accounts if a]


def get_alternate_accounts(record: dict) -> list[str]:
    """Return the alternate DCAD accounts. ``[]`` if absent."""
    sm = record.get("signal_metadata")
    if not isinstance(sm, dict):
        return []
    val = sm.get("alternate_accounts")
    return list(val) if isinstance(val, list) else []


# ═══════════════════════════════════════════════════════════════════════════
# Read-only convenience accessors — used by Stage 6.6 (PR 5) and audits
# ═══════════════════════════════════════════════════════════════════════════

def get_matched_entries(record: dict) -> list[dict]:
    """Return only the history entries that produced a match.

    Used by Stage 6.6 (cross-path agreement audit) to find paths that
    succeeded so they can be cross-compared.
    """
    return [
        h for h in get_history(record)
        if h.get("status") in (STATUS_MATCHED, STATUS_MULTI_MATCH)
        and h.get("dcad_account")
    ]


def get_accounts_produced(record: dict) -> set[str]:
    """Return the set of distinct ``dcad_account`` values any path produced.

    Stage 6.6 uses this to determine path agreement vs disagreement.
    """
    return {h["dcad_account"] for h in get_matched_entries(record)}


def get_paths_attempted(record: dict) -> set[str]:
    """Return the set of distinct paths that ran (regardless of outcome).

    Useful for audits like 'did Path A even run on this record?'.
    """
    return {h["path"] for h in get_history(record) if h.get("path")}
