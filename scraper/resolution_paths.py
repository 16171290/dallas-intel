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

from . import address_variants, normalize
from .probate_matcher import match_decedent_to_dcad
from .resolution import (
    GRANTOR_BOILERPLATE_BLOCKLIST,
    PATH_A_GRANTOR_OWNER_INDEX,
    PATH_B_RAW_EXCERPT,
    PATH_VARIANT_LOOKUP,
    STATUS_GUARDED,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    STATUS_NO_MATCH,
    STATUS_SKIPPED,
    TIER_DOUBLE_INITIAL,
    TIER_EXACT,
    TIER_INITIAL_FORM,
    TIER_NO_MIDDLE,
    TIER_SURNAME_ONLY,
    VENUE_TRUSTEE_SIGS,
    WARN_COMMON_NAME_POLLUTION,
    WARN_LOOKUP_VARIANT_USED,
    WARN_MULTI_ACCOUNT,
    WARN_PATH_B_USED_ALTERNATE,
    WARN_SURNAME_DRIFT,
    WARN_SURNAME_ONLY_TIER,
    WARN_SURNAME_IN_TRUST_FIRST_NAME,
    ResolutionHistoryEntry,
    add_warning,
    append_history,
    set_alternate_accounts,
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
    # Guard: require AT LEAST ONE token <= 2 chars so legit multi-word
    # names like "BIG OAK" or "PARK ROW" don't false-positive.
    for m in _OCR_TWO_SHORT_RE.finditer(addr_upper):
        tokens = m.group(0).split()
        if not any(t in _STREET_TYPE_OR_DIRECTIONAL_TOKENS for t in tokens):
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
    *,
    always_run: bool = False,
) -> PathBStats:
    """Stage 6.3 — Path B: raw_excerpt clean-address fallback.

    Walks every record; for those that meet the trigger condition,
    attempts to recover dcad_account via the raw_excerpt extraction
    described in docs/RESOLUTION_PATHS_DESIGN.md §4.

    When ``always_run=True`` (PR 5 diagnostic mode):
      - dcad_account-already-stamped records are NOT short-circuited
      - Path B still attempts and writes a history entry
      - Record-level fields (dcad_account, address_normalized, warnings)
        are NOT modified for already-resolved records

    Mutates records in place (only when path was the first to resolve):
      - Stamps dcad_account + address_normalized on a successful match
      - Adds resolution_history entry for every record considered
        (matched / no_match / skipped — full audit trail per §3)
      - Adds WARN_PATH_B_USED_ALTERNATE if Path B overwrote a non-empty
        address_normalized (operator can see provenance)

    Returns PathBStats for run-level observability.
    """
    stats = PathBStats(total_records=len(records))

    for rec in records:
        already_resolved = bool(rec.get("dcad_account"))
        # Default behavior: skip-early. always_run mode: proceed for
        # diagnostic visibility; stats counter still bumps.
        if already_resolved:
            stats.skipped_already_resolved += 1
            if not always_run:
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
            # Match. Stamp only if Path B is the first to resolve this rec.
            old_addr = rec.get("address_normalized")
            if not already_resolved:
                rec["dcad_account"] = acct
                rec["address_normalized"] = norm_candidate
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


# ═══════════════════════════════════════════════════════════════════════════
# Stage 6.35 — Variant Lookup (Class 26 family)
# ═══════════════════════════════════════════════════════════════════════════
#
# Runs AFTER Path B. Targets records where Stage 7 + Path B both missed
# despite the source carrying a clean-looking address that's *almost*
# correct. The forensic motivation (per docs/FORENSIC_AUDIT_*):
#
#   - DCAD's web search supports forgiving partial / suffix-less /
#     directional-drop / compound-split matching
#   - Our bulk address_index is strict equality
#   - Records like '4314 HAMILTON', '309 BILLY WICKLIFFE',
#     '2828 S LAKEVIEW DR' have correct property data in DCAD bulk but
#     under slightly different forms ('4314 HAMILTON AVE',
#     '309 BILLY WICKLIFFE DR', '2828 LAKE VIEW DR')
#
# Stage 6.35 fixes this by performing a fuzzy lookup keyed by street
# number, with similarity scoring on the post-number portion. See
# scraper/address_variants.py for the matching strategy.


@dataclass
class VariantLookupStats:
    """Per-run observability counters for the variant-lookup stage."""
    total_records:           int = 0
    skipped_already_resolved: int = 0    # dcad_account already set
    skipped_suspect_address: int = 0     # address is suspect (Path B territory)
    skipped_no_address:      int = 0     # no address_normalized to look up
    candidates:              int = 0     # records that DID trigger variant lookup
    matched:                 int = 0     # fuzzy lookup hit DCAD
    no_match:                int = 0     # candidate but no acceptable variant


def _address_eligible_for_variant_lookup(addr_norm: Optional[str]) -> bool:
    """Variant lookup fires only on addresses that LOOK clean — Path B
    has already handled the suspect ones."""
    if not addr_norm:
        return False
    # Skip suspect addresses — Path B owns those
    is_suspect, _ = _is_suspect_address(addr_norm)
    if is_suspect:
        return False
    # Must have a leading digit run (the street number for the lookup key)
    return bool(re.match(r"^\d+\s+", addr_norm))


def run_variant_lookup(
    records: list[dict],
    address_index: dict[str, str],
    fuzzy_index: dict[str, list[tuple[str, str]]],
    *,
    always_run: bool = False,
) -> VariantLookupStats:
    """Stage 6.35 — Variant Lookup.

    For each record where dcad_account is None AND address_normalized
    looks clean (not suspect), attempt a fuzzy lookup. On a confident
    match, stamp dcad_account + replace address_normalized with the
    canonical DCAD form.

    When ``always_run=True`` (PR 5 diagnostic mode): already-resolved
    records are not short-circuited; variant still attempts and records
    history but does NOT overwrite record-level fields.

    Mutates records in place (only when path was the first to resolve):
      - Stamps dcad_account on match
      - Replaces address_normalized with the matched DCAD form
      - Adds WARN_LOOKUP_VARIANT_USED if the address was changed
      - Appends resolution_history entry for every considered record

    Returns VariantLookupStats for run-level observability.
    """
    stats = VariantLookupStats(total_records=len(records))

    for rec in records:
        already_resolved = bool(rec.get("dcad_account"))
        if already_resolved:
            stats.skipped_already_resolved += 1
            if not always_run:
                continue

        addr_norm = rec.get("address_normalized")

        # Guard 2: skip suspect addresses (Path B's domain)
        if not addr_norm:
            stats.skipped_no_address += 1
            continue
        if not _address_eligible_for_variant_lookup(addr_norm):
            stats.skipped_suspect_address += 1
            continue

        stats.candidates += 1

        matched_addr, acct = address_variants.fuzzy_lookup(
            addr_norm, address_index, fuzzy_index,
        )

        if acct and matched_addr:
            old_addr = addr_norm
            if not already_resolved:
                rec["dcad_account"] = acct
                if matched_addr != old_addr:
                    rec["address_normalized"] = matched_addr
                    add_warning(rec, WARN_LOOKUP_VARIANT_USED)
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_VARIANT_LOOKUP,
                stage="6.35",
                input=old_addr,
                status=STATUS_MATCHED,
                dcad_account=acct,
            ))
            stats.matched += 1
            logger.debug(
                "Variant lookup matched record_id=%s %r -> %r (acct=%s)",
                rec.get("record_id"), old_addr, matched_addr, acct,
            )
        else:
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_VARIANT_LOOKUP,
                stage="6.35",
                input=addr_norm,
                status=STATUS_NO_MATCH,
            ))
            stats.no_match += 1

    return stats


def log_variant_lookup_summary(stats: VariantLookupStats) -> None:
    """Emit a single-line operations summary suitable for the daily-run log."""
    logger.info(
        "Variant lookup (Class 26 family): "
        "%d/%d records | candidates=%d matched=%d no_match=%d "
        "(skipped: already_resolved=%d suspect_addr=%d no_addr=%d)",
        stats.matched,
        stats.total_records,
        stats.candidates,
        stats.matched,
        stats.no_match,
        stats.skipped_already_resolved,
        stats.skipped_suspect_address,
        stats.skipped_no_address,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Stage 6.45 — Path A (NOF grantor → DCAD owner_index)
# ═══════════════════════════════════════════════════════════════════════════
#
# Mirrors what match_decedent_to_dcad already does for PB records, but
# applied to NOF grantors. For records where:
#   - dallas_code == "NOF"
#   - dcad_account is still None after Stage 6.3/6.35/6.4
#   - grantor is populated and looks like a real person name
#
# Look up the grantor in DCAD's owner_index. On a confident match, stamp
# dcad_account + derive address_normalized from the matched account's
# property address.
#
# Per design §5 + forensic findings, several guards prevent wrong-person
# matches that would lead to wrong-person calls:
#   - Boilerplate-grantor blocklist (Class 2: "Liable" etc.)
#   - Class 19a: surname_only tier where surname appears mid-string in
#     a trust/entity DCAD owner — guarded against (Heather → Bass case)
#   - Multi-account matches: stamp first + alternates + warning so the
#     operator sees alternates rather than silent first-pick
#   - Surname drift: when matched dcad_owner surname ≠ grantor surname
#     (e.g. trust grantor name doesn't match decedent), stamp warning


@dataclass
class PathAStats:
    """Per-run observability counters for Path A."""
    total_records:            int = 0
    skipped_not_nof:          int = 0
    skipped_already_resolved: int = 0
    skipped_no_grantor:       int = 0
    skipped_boilerplate:      int = 0
    candidates:               int = 0
    matched:                  int = 0    # 1 account, clean
    multi_account_matched:    int = 0    # 2+ accounts, first stamped + warned
    guarded:                  int = 0    # match found but Class 19a filter rejected
    no_match:                 int = 0


def _is_boilerplate_grantor(grantor: Optional[str]) -> bool:
    """True if the grantor string reduces to a known boilerplate token.

    Catches Class 2 (OCR-boilerplate-as-grantor) — e.g. "Liable" extracted
    from "BUT NOT TO OTHERWISE BE LIABLE as Grantor/Borrower" text.
    """
    if not grantor:
        return True
    g = grantor.strip().upper()
    if not g:
        return True
    # Strip post-comma material so 'LIABLE, FOO' collapses to 'LIABLE'
    main = g.split(",")[0].strip()
    if main in GRANTOR_BOILERPLATE_BLOCKLIST:
        return True
    # First token alone — catches "LIABLE FOO BAR" too
    first_token = main.split()[0] if main.split() else ""
    if first_token in GRANTOR_BOILERPLATE_BLOCKLIST:
        return True
    return False


def _is_trust_first_name_pattern(grantor: str, dcad_owner: str) -> bool:
    """Class 19a guard: detect when grantor surname matches a first-name
    position INSIDE a trust/entity DCAD owner string.

    Example FP (without guard):
      grantor='Heather, Linda A.'       (surname=HEATHER)
      dcad_owner='Bass Jeffery & Heather Revocable Trust The'

    'HEATHER' appears in DCAD owner but as the trust grantor's FIRST name,
    not as anybody's surname. The surname_only tier matched anyway because
    expand_joint_owners + normalize stripped trust suffixes down to just
    'HEATHER' in owner_index.

    Returns True when:
      - DCAD owner contains TRUST/LLC/INC/CORP/LP/LIFE ESTATE keyword
      - grantor surname is present mid-string (not at the start)
      - text before the surname is non-trivial (i.e. there's a real
        first-name before "HEATHER" rather than the surname being the
        first word).
    """
    if not grantor or not dcad_owner:
        return False
    if "," not in grantor:
        return False
    grantor_surname = grantor.split(",")[0].strip().upper()
    if not grantor_surname:
        return False
    owner_upper = dcad_owner.upper()
    if grantor_surname not in owner_upper:
        return False
    # Entity / trust indicator
    has_entity = bool(re.search(
        r"\b(TRUST|LLC|INC|CORP|LP|LTD|ESTATE)\b", owner_upper,
    ))
    if not has_entity:
        return False
    idx = owner_upper.index(grantor_surname)
    before = owner_upper[:idx].strip()
    # If surname is at the very start of the DCAD owner, it really is
    # acting as a surname (e.g. "Earls Trust" — Earls IS the surname).
    if not before:
        return False
    # If text before the surname includes '&' (joint owner) OR another
    # capitalized name token, the surname is in a first-name position.
    if "&" in before:
        return True
    # If there are 2+ word tokens before the surname, surname is mid-string
    if len(before.split()) >= 1:
        return True
    return False


def _surname_of(name: str) -> str:
    """Extract the canonical surname token from a grantor / dcad_owner string.

    Handles 'LAST, FIRST [MIDDLE]' (publicsearch comma form) and 'LAST FIRST [MIDDLE]'
    (DCAD bulk first-token form). Returns uppercased surname or '' on empty.
    """
    if not name:
        return ""
    name_upper = name.strip().upper()
    if "," in name_upper:
        return name_upper.split(",")[0].strip()
    tokens = name_upper.split()
    return tokens[0] if tokens else ""


def _account_owner_lookup(
    account_num: str,
    dcad_account_owner_index: dict[str, str],
) -> Optional[str]:
    """Look up the DCAD owner_name1 string for an account number.

    Caller provides ``dcad_account_owner_index`` — a precomputed
    ``{account_num: cleaned_owner_name}`` map. The harness builds this
    from ACCOUNT_INFO at startup; main.py does the same.
    """
    return dcad_account_owner_index.get(account_num)


def run_path_a(
    records: list[dict],
    owner_index: dict[str, list[str]],
    account_address_index: dict[str, dict],
    dcad_account_owner_index: dict[str, str],
    *,
    always_run: bool = False,
) -> PathAStats:
    """Stage 6.45 — Path A: NOF grantor → DCAD owner_index lookup.

    Fires on NOF records. By default (``always_run=False``) skips records
    that already have ``dcad_account`` set; the upstream path's result
    wins.

    When ``always_run=True`` (PR 5: Stage 6.6 diagnostic mode):
      - Records that already have ``dcad_account`` are NOT short-circuited
      - Path A still computes its would-be match and writes a full history
        entry, so Stage 6.6 can audit cross-path agreement / disagreement
      - dcad_account, address_normalized, alternate_accounts, and
        confidence_warnings are NOT modified for already-resolved records;
        the upstream stamp is preserved verbatim
      - For not-yet-resolved records, behavior is identical to default

    Mutates records in place (only when path was the first to resolve):
      - Stamps dcad_account on match
      - Sets address_normalized from the matched account's property address
      - Sets signal_metadata.alternate_accounts on multi-account matches
      - Adds warnings: WARN_MULTI_ACCOUNT, WARN_SURNAME_ONLY_TIER,
        WARN_COMMON_NAME_POLLUTION, WARN_SURNAME_DRIFT,
        WARN_SURNAME_IN_TRUST_FIRST_NAME (latter only when GUARDED)
      - Appends resolution_history entry for every considered record
        (this happens regardless of always_run, since history is the
        audit trail Stage 6.6 reads from)

    Returns PathAStats for run-level observability.
    """
    stats = PathAStats(total_records=len(records))

    for rec in records:
        # Path A only runs on NOF records (PB records use the existing
        # canonicalize_probate->match_decedent_to_dcad path).
        if rec.get("dallas_code") != "NOF":
            stats.skipped_not_nof += 1
            continue

        already_resolved = bool(rec.get("dcad_account"))
        # In default mode (always_run=False) skip-early; in always_run mode
        # we proceed so Stage 6.6 can see what Path A would have picked.
        if already_resolved and not always_run:
            stats.skipped_already_resolved += 1
            continue
        # In always_run mode, count the already-resolved diagnostic pass so
        # the operator can see Path A still ran (and the field mutations
        # were suppressed).
        if already_resolved:
            stats.skipped_already_resolved += 1

        grantor = rec.get("grantor")
        if not grantor:
            stats.skipped_no_grantor += 1
            continue

        if _is_boilerplate_grantor(grantor):
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_A_GRANTOR_OWNER_INDEX,
                stage="6.45",
                input=grantor,
                status=STATUS_SKIPPED,
                skip_reason="boilerplate_grantor",
            ))
            stats.skipped_boilerplate += 1
            continue

        stats.candidates += 1

        match = match_decedent_to_dcad(grantor, owner_index)

        if match is None:
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_A_GRANTOR_OWNER_INDEX,
                stage="6.45",
                input=grantor,
                status=STATUS_NO_MATCH,
            ))
            stats.no_match += 1
            continue

        # We have a match. Class 19a guard for surname_only matches:
        # peek at the DCAD owner of the matched account to see if the
        # surname is in a first-name position inside a trust.
        if match.tier == TIER_SURNAME_ONLY and match.accounts:
            first_acct = match.accounts[0]
            dcad_owner = _account_owner_lookup(first_acct, dcad_account_owner_index) or ""
            if _is_trust_first_name_pattern(grantor, dcad_owner):
                append_history(rec, ResolutionHistoryEntry(
                    path=PATH_A_GRANTOR_OWNER_INDEX,
                    stage="6.45",
                    input=grantor,
                    status=STATUS_GUARDED,
                    skip_reason="surname_in_trust_first_name",
                    tier=match.tier,
                    alternates=list(match.accounts),
                ))
                # The Class 19a guard's warning is record-level only when
                # we are actually stamping. Diagnostic-mode runs leave the
                # upstream record's warnings untouched.
                if not already_resolved:
                    add_warning(rec, WARN_SURNAME_IN_TRUST_FIRST_NAME)
                stats.guarded += 1
                continue

        # Match passes guards. Compute what we would stamp.
        primary_acct = match.accounts[0]
        alternates = list(match.accounts[1:])
        acct_addr = account_address_index.get(primary_acct)

        # Build the history-entry warnings list (this is recorded
        # regardless of stamping mode).
        history_warnings: list[str] = []
        if len(match.accounts) > 1:
            history_warnings.append(WARN_MULTI_ACCOUNT)
        if match.tier == TIER_SURNAME_ONLY:
            history_warnings.append(WARN_SURNAME_ONLY_TIER)
        if match.warning == "common_name_pollution":
            history_warnings.append(WARN_COMMON_NAME_POLLUTION)

        # Surname-drift check uses the matched account's DCAD owner.
        dcad_owner_str = _account_owner_lookup(primary_acct, dcad_account_owner_index) or ""
        grantor_surname = _surname_of(grantor)
        owner_surname = _surname_of(dcad_owner_str)
        surname_drift = (
            grantor_surname and owner_surname
            and grantor_surname != owner_surname
            and grantor_surname not in owner_surname
            and owner_surname not in grantor_surname
        )
        if surname_drift:
            history_warnings.append(WARN_SURNAME_DRIFT)

        # Stamp record-level fields ONLY when Path A is the first path to
        # resolve this record. always_run mode preserves upstream stamps.
        if not already_resolved:
            rec["dcad_account"] = primary_acct
            if acct_addr:
                rec["address_normalized"] = acct_addr
            if len(match.accounts) > 1:
                add_warning(rec, WARN_MULTI_ACCOUNT)
                set_alternate_accounts(rec, alternates)
            if match.tier == TIER_SURNAME_ONLY:
                add_warning(rec, WARN_SURNAME_ONLY_TIER)
            if match.warning == "common_name_pollution":
                add_warning(rec, WARN_COMMON_NAME_POLLUTION)
            if surname_drift:
                add_warning(rec, WARN_SURNAME_DRIFT)

        if len(match.accounts) > 1:
            stats.multi_account_matched += 1
        else:
            stats.matched += 1

        status = STATUS_MULTI_MATCH if len(match.accounts) > 1 else STATUS_MATCHED
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_A_GRANTOR_OWNER_INDEX,
            stage="6.45",
            input=grantor,
            status=status,
            tier=match.tier,
            dcad_account=primary_acct,
            alternates=alternates,
            warnings=history_warnings,
        ))

    return stats


def log_path_a_summary(stats: PathAStats) -> None:
    """Emit a single-line operations summary suitable for the daily-run log."""
    logger.info(
        "Path A (NOF grantor->owner_index): "
        "%d/%d NOFs | candidates=%d matched=%d multi=%d guarded=%d no_match=%d "
        "(skipped: not_nof=%d already_resolved=%d no_grantor=%d boilerplate=%d)",
        stats.matched + stats.multi_account_matched,
        stats.total_records,
        stats.candidates,
        stats.matched,
        stats.multi_account_matched,
        stats.guarded,
        stats.no_match,
        stats.skipped_not_nof,
        stats.skipped_already_resolved,
        stats.skipped_no_grantor,
        stats.skipped_boilerplate,
    )
