"""Resolution paths — implementations of the multi-path lead-recovery
stages described in ``docs/RESOLUTION_PATHS_DESIGN.md``.

This module holds the actual resolver logic; ``scraper/resolution.py``
holds the schema scaffolding (constants, dataclass, history helpers).

Stages implemented:
  - PR 2: Stage 6.3 — Path B (raw_excerpt clean-address fallback)
  - PR 3: Stage 6.4 — Path A (NOF grantor → owner_index)  [pending]
  - PR 4: Stage 6.5 — Path C fix + fuzzy subdivision      [pending]
  - PR 5: Stage 6.6 — Cross-path agreement + sanity       [pending]

Each stage writes a ResolutionHistoryEntry to every record it considers
(matched, skipped, or no_match) so the resolution_history is a complete
audit trail per the design contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import normalize
from .resolution import (
    PATH_B_RAW_EXCERPT,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    STATUS_SKIPPED,
    VENUE_TRUSTEE_SIGS,
    WARN_PATH_B_USED_ALTERNATE,
    ResolutionHistoryEntry,
    add_warning,
    append_history,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Path B — raw_excerpt clean-address fallback
# ═══════════════════════════════════════════════════════════════════════════
#
# Triggered when:
#   - dcad_account is None (Stage 7 didn't already resolve), AND
#   - raw_excerpt is populated, AND
#   - address_normalized is suspect (empty/city-only/OCR-garbled/venue-trustee)
#
# Action: scan raw_excerpt for a clean "NNN STREET, CITY, [TX] ZIP" address
# that isn't a known venue or trustee. If found and matches DCAD's address
# index, stamp dcad_account + replace address_normalized.
#
# Recovery target (per forensic audit): records where OCR captured a
# garbled or trustee-office address into address_normalized, but the
# correct property address is sitting in raw_excerpt (Class 24).

# OCR-garble heuristic.
#
# We detect three signatures:
#   1. Stray symbols ({, }, @, #, %) that are never present in clean
#      address forms.
#   2. Fragmented very-short letter clusters: "X Y Z" where each token is
#      1-2 letters. Catches "MA 5: {E)" - style damage.
#   3. Three consecutive short tokens (each ≤ 5 letters) where NONE looks
#      like a street type or directional. Real addresses always have a
#      street suffix (ST/DR/LN/etc.) or directional (N/S/E/W) somewhere
#      in such windows; pure-noise tokens are the garble signal. Catches
#      "OWE AY TALLY NTY" - style damage from "VANGUARD WAY" gone wrong.

_OCR_STRAY_SYMBOLS_RE   = re.compile(r"[{}@#%&]")               # Cox: trailing "&"
_OCR_TINY_CLUSTER_RE    = re.compile(r"\b[A-Z]{1,2}\s+[A-Z]\s+[A-Z]{1,2}\b")
_OCR_THREE_SHORT_RE     = re.compile(r"\b[A-Z]{2,5}\s+[A-Z]{1,3}\s+[A-Z]{2,5}\b")
# Cox-style: address ends with a single letter (post-name fragment).
# Examples: "3031 CREST RDG D" (was "3031 CREST RIDGE DR" pre-corruption).
_OCR_TRAILING_LETTER_RE = re.compile(r"\b[A-Z]\s*$")
# Montgomery-style: two consecutive short tokens (1-4 chars each) where
# NEITHER is a recognized street type or directional. Catches "BEA OS"
# in "2206 BEA OS REEFGRAND PRAIRIE" — clearly fragmented from the
# real "BEAUMONT". The street-type / directional exclusion prevents
# false-positives on legit addresses like "S MAIN" or "OAK CIR".
_OCR_TWO_SHORT_RE       = re.compile(r"\b[A-Z]{1,4}\s+[A-Z]{1,4}\b")

# Tokens that, if present in a 3-short window, indicate the window is a
# real address fragment rather than OCR garble. Full forms + USPS abbreviations.
_STREET_TYPE_OR_DIRECTIONAL_TOKENS: frozenset[str] = frozenset({
    # Street suffixes (full + abbreviated)
    "ST", "STREET", "RD", "ROAD", "DR", "DRIVE", "AVE", "AVENUE",
    "BLVD", "BOULEVARD", "LN", "LANE", "CT", "COURT",
    "CIR", "CIRCLE", "PL", "PLACE", "WAY",
    "TRL", "TRAIL", "TER", "TERRACE",
    "HWY", "HIGHWAY", "PKWY", "PARKWAY",
    "EXPY", "EXPRESSWAY", "FWY", "FREEWAY",
    "CV", "COVE", "RUN", "ROW", "RDG", "RIDGE",
    # Directionals
    "N", "S", "E", "W", "NE", "NW", "SE", "SW",
    "NORTH", "SOUTH", "EAST", "WEST",
})


def _is_ocr_garbled(addr_upper: str) -> bool:
    """True if address looks OCR-corrupted by any garble signature."""
    if _OCR_STRAY_SYMBOLS_RE.search(addr_upper):
        return True
    if _OCR_TINY_CLUSTER_RE.search(addr_upper):
        return True
    if _OCR_TRAILING_LETTER_RE.search(addr_upper):
        return True
    # Three consecutive short tokens, none of which is a street type
    for m in _OCR_THREE_SHORT_RE.finditer(addr_upper):
        tokens = m.group(0).split()
        if not any(t in _STREET_TYPE_OR_DIRECTIONAL_TOKENS for t in tokens):
            return True
    # Two consecutive short tokens, neither street type nor directional.
    # Tighter than _OCR_THREE_SHORT_RE so we don't catch "OAK CIR" or
    # "S MAIN" — both contain a street type or directional.
    for m in _OCR_TWO_SHORT_RE.finditer(addr_upper):
        tokens = m.group(0).split()
        if not any(t in _STREET_TYPE_OR_DIRECTIONAL_TOKENS for t in tokens):
            # Additional guard: skip when both tokens look like real
            # English words (≥3 chars and start+end with consonant cluster
            # is too brittle — instead require AT LEAST ONE to be ≤2
            # chars, which is the OCR-fragmentation signature).
            if any(len(t) <= 2 for t in tokens):
                return True
    return False

# Address extractor for raw_excerpt. Pattern from design doc §4:
#   NNN STREET-NAME STREET-TYPE, CITY, [TX|TEXAS], ZIP
#
# Why this regex shape:
#   - Requires a street suffix (DR/AVE/ST/...) — reduces FP on city-only
#     phrases that happen to start with a number.
#   - Requires a 5-digit zip at the end — anchors the match to real US
#     mailing format and prevents matching arbitrary "NNN STREET, DALLAS"
#     fragments mid-paragraph.
#   - Optional TX/TEXAS — covers both abbreviated and spelled-out forms.
_RAW_EXCERPT_ADDR_RE = re.compile(
    r"\d{2,5}\s+"                                              # street number
    r"[A-Z][A-Za-z\s\.'-]{2,40}?\s+"                            # street name
    r"(?:STREET|DRIVE|DR|ST|AVENUE|AVE|ROAD|RD|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|WAY|"
    r"TRAIL|TRL|PARKWAY|PKWY|HIGHWAY|HWY|TERRACE|TER)"           # street suffix
    r"\b\s*,?\s+"
    r"[A-Z][A-Za-z\s]{2,30}?"                                   # city
    r"\s*,?\s*"
    r"(?:TEXAS|TX)?"                                            # optional state
    r"\s*,?\s*"
    r"\d{5}(?:-\d{4})?",                                        # zip (+ optional +4)
    re.I,
)


@dataclass
class PathBStats:
    """Per-run observability counters for Path B."""
    total_records:            int = 0
    skipped_already_resolved: int = 0   # dcad_account already set
    skipped_clean_address:    int = 0   # address_normalized was clean (not suspect)
    candidates:               int = 0   # records that DID trigger Path B
    skipped_no_raw_excerpt:   int = 0
    skipped_no_clean_address: int = 0   # raw_excerpt scanned but no clean addr
    rejected_venue_trustee:   int = 0   # raw_excerpt addr matched a venue/trustee sig
    matched:                  int = 0   # candidate -> address_index hit
    no_match:                 int = 0   # candidate extracted but address_index miss


def _is_suspect_address(addr_norm: Optional[str]) -> tuple[bool, Optional[str]]:
    """Return (is_suspect, reason).

    Path B fires only on records whose address_normalized fails one of
    the suspect-quality checks. Reason is for logging context.
    """
    if not addr_norm:
        return True, "empty"

    addr_upper = addr_norm.upper()

    # Venue/trustee signature contamination (Class 1 + Class 25).
    if any(sig in addr_upper for sig in VENUE_TRUSTEE_SIGS):
        return True, "venue_or_trustee"

    # City-only: no digit in the first comma-segment.
    first_seg = addr_upper.split(",")[0].strip()
    if not first_seg or not any(c.isdigit() for c in first_seg):
        return True, "city_only"

    # OCR-garbled signature.
    if _is_ocr_garbled(addr_upper):
        return True, "ocr_garbled"

    return False, None


def extract_clean_address_from_raw_excerpt(
    raw_excerpt: Optional[str],
) -> Optional[str]:
    """Find a clean street address in raw_excerpt that isn't a known
    venue or trustee. Returns the first valid match, or None.

    Multiple matches may exist in a raw_excerpt (e.g. property + deed
    address). The regex returns matches in document order; we take the
    first non-venue match.

    No DCAD lookup happens here — that's the caller's job.
    """
    if not raw_excerpt:
        return None

    for m in _RAW_EXCERPT_ADDR_RE.finditer(raw_excerpt):
        candidate = m.group(0)
        candidate_upper = candidate.upper()
        # Guard: reject known venue/trustee signatures even when extracted
        # from raw_excerpt (forensic audit showed raw_excerpt was clean in
        # this run, but future-proof against a publicsearch list-view change).
        if any(sig in candidate_upper for sig in VENUE_TRUSTEE_SIGS):
            continue
        return candidate.strip()

    return None


def run_path_b(
    records: list[dict],
    address_index: dict[str, str],
) -> PathBStats:
    """Stage 6.3 — Path B: raw_excerpt clean-address fallback.

    Walks every record; for those that meet the trigger condition,
    attempts to recover dcad_account via the raw_excerpt extraction
    described in docs/RESOLUTION_PATHS_DESIGN.md §4.

    Mutates records in place:
      - Stamps dcad_account + address_normalized on a successful match
      - Adds resolution_history entry for every record considered
        (matched / no_match / skipped — full audit trail per §3)
      - Adds WARN_PATH_B_USED_ALTERNATE if Path B overwrote a non-empty
        address_normalized (operator can see provenance)

    Returns PathBStats for run-level observability.
    """
    stats = PathBStats(total_records=len(records))

    for rec in records:
        # Guard 1: skip if Stage 7 already resolved
        if rec.get("dcad_account"):
            stats.skipped_already_resolved += 1
            continue

        # Trigger check: only run on records with a suspect address_normalized
        addr_norm = rec.get("address_normalized")
        is_suspect, suspect_reason = _is_suspect_address(addr_norm)
        if not is_suspect:
            stats.skipped_clean_address += 1
            continue

        raw_excerpt = rec.get("raw_excerpt")
        if not raw_excerpt:
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_B_RAW_EXCERPT,
                stage="6.3",
                input=None,
                status=STATUS_SKIPPED,
                skip_reason="no_raw_excerpt",
            ))
            stats.skipped_no_raw_excerpt += 1
            continue

        stats.candidates += 1

        candidate = extract_clean_address_from_raw_excerpt(raw_excerpt)

        if not candidate:
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_B_RAW_EXCERPT,
                stage="6.3",
                input=None,
                status=STATUS_SKIPPED,
                skip_reason="no_clean_address_in_raw_excerpt",
            ))
            stats.skipped_no_clean_address += 1
            continue

        # Normalize the candidate the same way address_index keys are built
        # (normalize.normalize_address strips city/state/zip suffix +
        # canonicalizes USPS forms).
        norm_candidate = normalize.normalize_address(candidate)
        if not norm_candidate:
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_B_RAW_EXCERPT,
                stage="6.3",
                input=candidate,
                status=STATUS_NO_MATCH,
            ))
            stats.no_match += 1
            continue

        acct = address_index.get(norm_candidate)

        if acct:
            # Match. Stamp dcad_account + replace address_normalized.
            old_addr = rec.get("address_normalized")
            rec["dcad_account"] = acct
            rec["address_normalized"] = norm_candidate

            # If we overwrote a non-empty address with a different one,
            # flag the swap so the operator sees provenance.
            if old_addr and old_addr.strip() and old_addr != norm_candidate:
                add_warning(rec, WARN_PATH_B_USED_ALTERNATE)

            append_history(rec, ResolutionHistoryEntry(
                path=PATH_B_RAW_EXCERPT,
                stage="6.3",
                input=candidate,
                status=STATUS_MATCHED,
                dcad_account=acct,
            ))
            stats.matched += 1
            logger.debug(
                "Path B matched record_id=%s candidate=%r -> acct=%s (was: addr=%r)",
                rec.get("record_id"), candidate, acct, old_addr,
            )
        else:
            # Candidate extracted but address not in DCAD index
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_B_RAW_EXCERPT,
                stage="6.3",
                input=candidate,
                status=STATUS_NO_MATCH,
            ))
            stats.no_match += 1

    return stats


def log_path_b_summary(stats: PathBStats) -> None:
    """Emit a single-line operations summary suitable for the daily-run log."""
    logger.info(
        "Path B (raw_excerpt fallback): "
        "%d/%d records | candidates=%d matched=%d no_match=%d "
        "(skipped: already_resolved=%d clean_address=%d no_raw_excerpt=%d no_clean_addr=%d)",
        stats.matched,
        stats.total_records,
        stats.candidates,
        stats.matched,
        stats.no_match,
        stats.skipped_already_resolved,
        stats.skipped_clean_address,
        stats.skipped_no_raw_excerpt,
        stats.skipped_no_clean_address,
    )
