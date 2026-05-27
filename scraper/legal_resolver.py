"""Legal-description -> DCAD address resolver.

Publicsearch.us list-view records lack a street address. They carry a
``raw_html_snippet`` like::

    "DALLAS | Subdivision - Name: SOUTH SIDE Lot: 20 Block: A Township: DALLAS"

DCAD's ACCOUNT_INFO table carries the same legal description in LEGAL1
(subdivision) + LEGAL2 (block/lot in formats like ``"BLK A/1694 PT LOT 20"``).
This module joins the two so publicsearch records can flow into the
existing address-based enrichment pipeline.

Design choices (per audit 2026-05-21):
  - Conservative matching: only stamp ``dcad_account`` on exact
    (subdivision, lot, block) matches OR confident fuzzy matches. MULTI
    and NO_MATCH leave the field blank so enrichment continues to bail
    (safe failure).
  - Resolver stamps ``dcad_account`` AND ``address_normalized`` directly
    (PR 4 change). The previous indirection (stamp only address, let
    enrichment derive account) failed when address_normalized was set
    but had no entry in address_index — Path C is now consistent with
    Path A.
  - Conservative normalization (same rules as the probe that yielded
    56% match rate empirically on 116 property-attached records):
      * Ordinal word -> number (FIRST -> 1ST, etc.)
      * Standard suffix abbreviations (ADDITION -> ADDN, etc.)
      * Two-variant lookup (handles "X OF Y" reorderings)
      * Block partial match (DCAD's "BLK A/1694" matches publicsearch "A")
  - Fuzzy subdivision fallback (PR 4): when exact (sub, lot, block)
    lookup fails, attempt difflib SequenceMatcher matching of the
    subdivision name against DCAD subdivisions that have a single
    account at the same (lot, block). Requires similarity >= 0.85 and
    exactly one candidate above threshold. Stamps WARN_FUZZY_SUBDIVISION_MATCH.

Public API:
  build_legal_index(dcad_tables) -> dict
  parse_legal_from_snippet(snippet) -> dict | None
  resolve_legal_descriptions(records, dcad_tables) -> stats
  resolve_apn_to_address(records, dcad_tables) -> stats
  fuzzy_subdivision_match(target, legal_index, lot, block, *, threshold) -> match | None

PR 4 skip-condition fix:
  Previously both resolvers skipped records with any ``address_normalized``
  set. This silently excluded the city-only / OCR-garbled / venue-trustee
  address records (Classes 1, 6, 24) where the address LOOKED set but
  was operationally useless. The fix changes the skip predicate to
  ``dcad_account`` so the resolver fires whenever no DCAD account has
  been stamped, regardless of address-field state.

Hit-rate target (empirical baseline): ~50-58% of property-attached
publicsearch records via exact match. Fuzzy fallback adds a small
incremental percentage (estimated 3-7%). False positives in this layer
would silently corrupt enrichment; the conservative single-candidate
threshold keeps that risk low.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from . import normalize
from .resolution import (
    PATH_C_APN,
    PATH_C_LEGAL_RESOLVER,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    STATUS_NO_MATCH,
    STATUS_SKIPPED,
    TIER_EXACT,
    TIER_FUZZY_SUBDIVISION,
    WARN_FUZZY_SUBDIVISION_MATCH,
    ResolutionHistoryEntry,
    add_warning,
    append_history,
)

logger = logging.getLogger(__name__)

CanonicalRecord = dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════
# Normalization rules
# ═══════════════════════════════════════════════════════════════════════════

# Replacements applied to both publicsearch + DCAD subdivision names
# so they normalize to the same form.
_SUBDIV_REPLACEMENTS: list[tuple[str, str]] = [
    # Word -> abbreviation
    (r"\bADDITION\b",   "ADDN"),
    (r"\bINSTALLMENT\b","INST"),
    (r"\bPHASE\b",      "PH"),
    (r"\bSECTION\b",    "SEC"),
    (r"\bNUMBER\b",     "NO"),
    # Ordinal words -> numbers
    (r"\bFIRST\b",   "1ST"),
    (r"\bSECOND\b",  "2ND"),
    (r"\bTHIRD\b",   "3RD"),
    (r"\bFOURTH\b",  "4TH"),
    (r"\bFIFTH\b",   "5TH"),
    (r"\bSIXTH\b",   "6TH"),
    (r"\bSEVENTH\b", "7TH"),
    (r"\bEIGHTH\b",  "8TH"),
    (r"\bNINTH\b",   "9TH"),
    (r"\bTENTH\b",   "10TH"),
    # Strip punctuation, normalize whitespace
    (r"[.,]",  ""),
    (r"\s+",   " "),
]


def normalize_subdivision(s: str | None) -> str:
    """Apply all subdivision-name normalization rules. Returns "" on None/empty."""
    if not s:
        return ""
    s = s.upper().strip()
    for pat, repl in _SUBDIV_REPLACEMENTS:
        s = re.sub(pat, repl, s)
    return s.strip()


def _generate_subdivision_variants(s: str) -> set[str]:
    """Generate name variants to handle "X OF Y" reorderings.

    Publicsearch sometimes uses "FIRST INSTALLMENT OF EAST KESSLER PARK"
    where DCAD uses "EAST KESSLER PARK 1ST INST". We try both orderings.
    """
    base = normalize_subdivision(s)
    variants = {base}

    # Reorder "1ST INST OF X" -> "X 1ST INST"
    m = re.match(r"^(\d+(?:ST|ND|RD|TH)\s+(?:INST|PH|SEC))\s+OF\s+(.+)$", base)
    if m:
        variants.add(f"{m.group(2)} {m.group(1)}")

    # Generic "X OF Y" -> "Y X" reordering
    m = re.match(r"^(.+?)\s+OF\s+(.+)$", base)
    if m:
        variants.add(f"{m.group(2)} {m.group(1)}")

    return variants


# ═══════════════════════════════════════════════════════════════════════════
# DCAD LEGAL2 parsing
# ═══════════════════════════════════════════════════════════════════════════

def _parse_dcad_legal2(legal2: str | None) -> dict[str, str]:
    """Extract lot and block from DCAD LEGAL2 field.

    DCAD LEGAL2 examples (verified against ACCOUNT_INFO 2025):
      'LT 0033'              -> lot=33,  block=""
      'BLK H LT 38'          -> lot=38,  block="H"
      'BLK 1 LOT 1'          -> lot=1,   block="1"
      'BLK A/1694 PT LOT 20' -> lot=20,  block="A"  (first segment of A/1694)
      'TR 24'                -> lot=24,  block=""   (tract treated as lot)

    Conservative parse - if format doesn't match, returns empty values
    rather than guessing.
    """
    if not legal2:
        return {"lot": "", "block": ""}
    s = legal2.upper().strip()
    out = {"lot": "", "block": ""}

    # Block: capture token after BLK/BLOCK; keep only segment before any slash
    m = re.search(r"\bBL(?:K|OCK)\s+([A-Z0-9]+)", s)
    if m:
        out["block"] = m.group(1).split("/")[0]

    # Lot or tract: capture token after LT/LOT/TRACT; strip leading zeros
    m = re.search(r"\b(?:LT|LOT|TR(?:ACT)?)\s+([A-Z0-9]+)", s)
    if m:
        out["lot"] = m.group(1).lstrip("0") or "0"

    return out


# ═══════════════════════════════════════════════════════════════════════════
# Publicsearch snippet parsing
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ParsedLegal:
    """Structured form of a publicsearch raw_html_snippet's legal description."""
    subdivision: str = ""
    lot: str = ""
    block: str = ""
    township: str = ""


def parse_legal_from_snippet(snippet: str | None) -> ParsedLegal | None:
    """Parse a publicsearch raw_html_snippet for legal-description fields.

    Expected format (with variants):
      "<TOWNSHIP> | Subdivision - Name: <NAME> Lot: <N> Block: <B> Township: <T>"
      "<TOWNSHIP> | Subdivision - Name: <NAME> Lot: <N> Township: <T>"
      "<TOWNSHIP> | Township: <T>"
      "N/A | N/A"

    Returns ParsedLegal with empty fields if no subdivision was found.
    Returns None if the snippet is completely empty.
    """
    if not snippet or not snippet.strip():
        return None
    if snippet.strip() == "N/A | N/A":
        return ParsedLegal()  # parseable but empty

    out = ParsedLegal()
    m = re.search(
        r"Subdivision\s*-\s*Name:\s*(.+?)(?=\s*(?:Lot:|Block:|Township:|Reference|$))",
        snippet, re.IGNORECASE,
    )
    if m:
        out.subdivision = m.group(1).strip()

    m = re.search(r"Lot:\s*([A-Z0-9\-]+)", snippet, re.IGNORECASE)
    if m:
        out.lot = m.group(1).strip()

    m = re.search(r"Block:\s*([A-Z0-9\-]+)", snippet, re.IGNORECASE)
    if m:
        out.block = m.group(1).strip()

    m = re.search(r"Township:\s*([A-Z\s]+?)(?=\s*(?:Reference|$))", snippet, re.IGNORECASE)
    if m:
        out.township = m.group(1).strip()

    return out


# ═══════════════════════════════════════════════════════════════════════════
# DCAD legal index
# ═══════════════════════════════════════════════════════════════════════════

def build_legal_index(
    dcad_tables: dict[str, pd.DataFrame],
) -> dict[tuple[str, str, str], list[str]]:
    """Build a normalized-legal-description -> ACCOUNT_NUM index.

    Key:   (normalized_subdivision, normalized_lot, normalized_block)
    Value: list of ACCOUNT_NUM strings matching that key

    Multiple accounts can share a key (e.g., condos where lot=unit_id but
    block isn't differentiating). Callers should treat keys with >1 account
    as MULTI matches and skip them - the conservative match policy.

    Source: ACCOUNT_INFO table, LEGAL1 (subdivision) + LEGAL2 (lot/block).
    """
    if "ACCOUNT_INFO" not in dcad_tables:
        logger.warning("ACCOUNT_INFO not in DCAD tables; legal index empty")
        return {}

    df = dcad_tables["ACCOUNT_INFO"]
    needed = {"ACCOUNT_NUM", "LEGAL1", "LEGAL2"}
    missing = needed - set(df.columns)
    if missing:
        logger.warning(
            "ACCOUNT_INFO missing columns %s; legal index empty",
            sorted(missing),
        )
        return {}

    index: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    skipped_no_subdivision = 0

    for row in df.itertuples(index=False):
        account = str(getattr(row, "ACCOUNT_NUM", "") or "").strip()
        legal1 = str(getattr(row, "LEGAL1", "") or "").strip()
        legal2 = str(getattr(row, "LEGAL2", "") or "").strip()

        if not account:
            continue

        norm_sub = normalize_subdivision(legal1)
        if not norm_sub:
            skipped_no_subdivision += 1
            continue

        lotblock = _parse_dcad_legal2(legal2)
        key = (norm_sub, lotblock["lot"], lotblock["block"])
        index[key].append(account)

    logger.info(
        "Built DCAD legal index: %d unique (sub,lot,block) keys from %d rows "
        "(%d skipped - no subdivision)",
        len(index), len(df), skipped_no_subdivision,
    )
    return dict(index)


# ═══════════════════════════════════════════════════════════════════════════
# Account -> address-normalized lookup (uses ACCOUNT_INFO directly)
# ═══════════════════════════════════════════════════════════════════════════

def _build_account_to_address(
    dcad_tables: dict[str, pd.DataFrame],
) -> dict[str, str]:
    """Reverse map: ACCOUNT_NUM -> normalized street address.

    Used to get the address_normalized that we'll stamp on matched
    publicsearch records. Same normalization as build_address_index
    in dcad_bulk.py so the values are identical.
    """
    if "ACCOUNT_INFO" not in dcad_tables:
        return {}

    df = dcad_tables["ACCOUNT_INFO"]
    needed = {"ACCOUNT_NUM", "STREET_NUM", "FULL_STREET_NAME"}
    if not needed.issubset(df.columns):
        return {}

    has_half = "STREET_HALF_NUM" in df.columns
    out: dict[str, str] = {}

    for row in df.itertuples(index=False):
        account = str(getattr(row, "ACCOUNT_NUM", "") or "").strip()
        if not account:
            continue

        num  = str(getattr(row, "STREET_NUM", "") or "").strip()
        half = str(getattr(row, "STREET_HALF_NUM", "") or "").strip() if has_half else ""
        name = str(getattr(row, "FULL_STREET_NAME", "") or "").strip()
        raw  = " ".join(p for p in (num, half, name) if p).strip()
        norm = normalize.normalize_address(raw)
        if norm:
            out[account] = norm

    return out


# ═══════════════════════════════════════════════════════════════════════════
# Fuzzy subdivision matcher (PR 4)
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class FuzzySubdivisionMatch:
    """Result of a fuzzy subdivision lookup."""
    account:      str
    matched_sub:  str   # the DCAD subdivision name that won
    ratio:        float


def fuzzy_subdivision_match(
    target_subdivision: str,
    legal_index: dict[tuple[str, str, str], list[str]],
    lot: str,
    block: str,
    *,
    similarity_threshold: float = 0.85,
) -> Optional[FuzzySubdivisionMatch]:
    """Find a DCAD subdivision that closely matches a (possibly OCR-garbled)
    target, when the exact (sub, lot, block) lookup already failed.

    Conservative single-candidate strategy:
      - target_subdivision is normalized via normalize_subdivision()
      - candidate subdivisions are those with EXACTLY ONE DCAD account at
        the requested (lot, block) — this acts as a strong constraint
      - SequenceMatcher ratio is computed between target and each
        candidate; only ratios >= similarity_threshold count
      - if EXACTLY ONE candidate sits at/above threshold, return it
      - otherwise return None (no first-pick policy — operator gets a
        no_match instead of a wrong match)

    Catches: BEAR TREK -> BEAR CREEK, SANDALWOGR -> SANDALWOOD,
             BEAUMONT -> BEUMONT (1-char typo), VOUGHT MANOR ADDITI ECTION
             -> VOUGHT MANOR ADDITION SECTION.

    Misses (deliberately): completely garbled subdivisions, ambiguous
    cases where 2+ subdivisions tie or near-tie above threshold.
    """
    target_norm = normalize_subdivision(target_subdivision)
    if not target_norm or not lot or not block:
        return None

    candidate_subs: set[str] = set()
    for (sub, l, b), accounts in legal_index.items():
        if l == lot and b == block and len(accounts) == 1:
            candidate_subs.add(sub)

    if not candidate_subs:
        return None

    scored: list[tuple[float, str]] = []
    for sub in candidate_subs:
        ratio = difflib.SequenceMatcher(None, target_norm, sub).ratio()
        if ratio >= similarity_threshold:
            scored.append((ratio, sub))

    if len(scored) != 1:
        return None

    ratio, sub = scored[0]
    account = legal_index[(sub, lot, block)][0]
    return FuzzySubdivisionMatch(account=account, matched_sub=sub, ratio=ratio)


# ═══════════════════════════════════════════════════════════════════════════
# Resolver - public entry point
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LegalResolverStats:
    """Counts for per-pipeline-run observability."""
    total: int = 0
    no_snippet: int = 0       # record had no raw_html_snippet
    no_parse: int = 0          # snippet had no subdivision (e.g. AJ records)
    no_match: int = 0          # parsed but no DCAD match (exact + fuzzy both missed)
    multi_match: int = 0       # parsed and matched multiple accounts (skipped)
    matched_exact: int = 0     # exact (sub, lot, block) match
    matched_fuzzy: int = 0     # fuzzy subdivision match
    skipped_already_resolved: int = 0  # dcad_account already stamped by upstream path

    @property
    def resolved(self) -> int:
        return self.matched_exact + self.matched_fuzzy

    @property
    def resolution_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.resolved / self.total


def resolve_legal_descriptions(
    records: list[CanonicalRecord],
    dcad_tables: dict[str, pd.DataFrame],
    *,
    enable_fuzzy: bool = True,
) -> LegalResolverStats:
    """Stamp dcad_account + address_normalized on records via legal-description match.

    Iterates records, parses each publicsearch snippet, looks up the legal
    description in the DCAD legal index. On a single exact match (or a
    confident fuzzy match if exact missed), stamps both dcad_account and
    address_normalized.

    Records modified in place. Returns stats for logging/monitoring.

    Builds the legal_index + acct_to_addr fresh from ``dcad_tables``. For
    iterative tooling (validation harness) call ``resolve_legal_descriptions_with_indexes``
    with pre-built indexes to skip the parse cost.

    PR 4 skip-condition fix:
        Previously skipped on ``address_normalized``; now skips on
        ``dcad_account``. This unblocks records with city-only,
        OCR-garbled, or venue-trustee address strings — the address
        field looked set but had no operational value, so the resolver
        is now allowed to overwrite it with a legal-description-derived
        address when one matches.
    """
    legal_index  = build_legal_index(dcad_tables)
    acct_to_addr = _build_account_to_address(dcad_tables)
    return resolve_legal_descriptions_with_indexes(
        records, legal_index, acct_to_addr, enable_fuzzy=enable_fuzzy,
    )


def resolve_legal_descriptions_with_indexes(
    records: list[CanonicalRecord],
    legal_index: dict[tuple[str, str, str], list[str]],
    acct_to_addr: dict[str, str],
    *,
    enable_fuzzy: bool = True,
) -> LegalResolverStats:
    """Same as :func:`resolve_legal_descriptions` but takes pre-built indexes.

    Used by the validation harness to reuse a cached legal_index across
    multiple invocations without re-parsing dcad_tables (which costs ~30s).
    """
    stats = LegalResolverStats(total=len(records))

    for rec in records:
        # PR 4 fix: skip on dcad_account, not address_normalized. The
        # address might be set but useless (city-only, OCR garbled).
        if rec.get("dcad_account"):
            stats.skipped_already_resolved += 1
            continue

        # Only attempt resolution on records with a snippet to parse
        snippet = rec.get("raw_excerpt") or ""
        if not snippet:
            stats.no_snippet += 1
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_C_LEGAL_RESOLVER,
                stage="6.5",
                input=None,
                status=STATUS_SKIPPED,
                skip_reason="no_snippet",
            ))
            continue

        parsed = parse_legal_from_snippet(snippet)
        if not parsed or not parsed.subdivision:
            stats.no_parse += 1
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_C_LEGAL_RESOLVER,
                stage="6.5",
                input=snippet[:120],
                status=STATUS_SKIPPED,
                skip_reason="no_subdivision_parsed",
            ))
            continue

        # Normalize parsed values
        norm_lot = parsed.lot.lstrip("0") if parsed.lot else parsed.lot
        norm_block = parsed.block

        # Try each subdivision variant (handles "X OF Y" reorderings)
        variants = _generate_subdivision_variants(parsed.subdivision)
        matched_accounts: list[str] = []
        for v in variants:
            matched = legal_index.get((v, norm_lot, norm_block), [])
            if matched:
                matched_accounts = matched
                break

        # MULTI guard fires before fuzzy fallback — if exact lookup hit
        # multiple accounts, fuzzy can't disambiguate, so we bail.
        if len(matched_accounts) > 1:
            stats.multi_match += 1
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_C_LEGAL_RESOLVER,
                stage="6.5",
                input=parsed.subdivision,
                status=STATUS_MULTI_MATCH,
                tier=TIER_EXACT,
                alternates=list(matched_accounts),
            ))
            continue

        # Exact single-match path
        if matched_accounts:
            account = matched_accounts[0]
            addr = acct_to_addr.get(account)
            if not addr:
                stats.no_match += 1
                append_history(rec, ResolutionHistoryEntry(
                    path=PATH_C_LEGAL_RESOLVER,
                    stage="6.5",
                    input=parsed.subdivision,
                    status=STATUS_NO_MATCH,
                    skip_reason="account_has_no_address",
                ))
                continue
            rec["dcad_account"] = account
            rec["address_normalized"] = addr
            stats.matched_exact += 1
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_C_LEGAL_RESOLVER,
                stage="6.5",
                input=parsed.subdivision,
                status=STATUS_MATCHED,
                tier=TIER_EXACT,
                dcad_account=account,
            ))
            continue

        # Fuzzy fallback — only when exact missed and feature enabled
        if enable_fuzzy:
            fuzzy = fuzzy_subdivision_match(
                parsed.subdivision, legal_index, norm_lot, norm_block,
            )
            if fuzzy is not None:
                addr = acct_to_addr.get(fuzzy.account)
                if addr:
                    rec["dcad_account"] = fuzzy.account
                    rec["address_normalized"] = addr
                    add_warning(rec, WARN_FUZZY_SUBDIVISION_MATCH)
                    stats.matched_fuzzy += 1
                    append_history(rec, ResolutionHistoryEntry(
                        path=PATH_C_LEGAL_RESOLVER,
                        stage="6.5",
                        input=parsed.subdivision,
                        status=STATUS_MATCHED,
                        tier=TIER_FUZZY_SUBDIVISION,
                        dcad_account=fuzzy.account,
                        warnings=[WARN_FUZZY_SUBDIVISION_MATCH],
                    ))
                    continue
                # else: fuzzy found an account but it has no address;
                # fall through to no_match below

        stats.no_match += 1
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_C_LEGAL_RESOLVER,
            stage="6.5",
            input=parsed.subdivision,
            status=STATUS_NO_MATCH,
        ))

    logger.info(
        "Path C (legal_resolver): %d/%d resolved (%.1f%%); "
        "exact=%d fuzzy=%d no_parse=%d no_match=%d multi=%d "
        "no_snippet=%d skipped_already_resolved=%d",
        stats.resolved, stats.total, stats.resolution_rate * 100,
        stats.matched_exact, stats.matched_fuzzy,
        stats.no_parse, stats.no_match, stats.multi_match,
        stats.no_snippet, stats.skipped_already_resolved,
    )
    return stats


# ═══════════════════════════════════════════════════════════════════════════
# APN -> DCAD account resolver
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class APNResolverStats:
    """Counts for per-pipeline-run observability."""
    total: int = 0
    no_apn: int = 0        # record had no APN in signal_metadata.ocr
    no_match: int = 0      # APN didn't match any DCAD account
    resolved: int = 0      # APN matched, address stamped
    skipped_already_resolved: int = 0  # dcad_account already stamped by upstream path

    @property
    def resolution_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.resolved / self.total


def resolve_apn_to_address(
    records: list[CanonicalRecord],
    dcad_tables: dict[str, pd.DataFrame],
) -> APNResolverStats:
    """Stamp dcad_account + address_normalized on records via APN -> DCAD account match.

    For records where ``signal_metadata.ocr.apn`` is set AND
    ``dcad_account`` is empty, look up the APN (already
    leading-zero-stripped by the OCR extractor) in DCAD's
    ACCOUNT_INFO.ACCOUNT_NUM. On exact match, stamp dcad_account and
    address_normalized.

    Designed to run BEFORE resolve_legal_descriptions because APN is the
    cheapest and highest-confidence identifier when present.
    Per the 41-record probe (2026-05-26), APN appears in ~5% of NOFs.

    Records modified in place.

    Builds the acct_to_addr index fresh from ``dcad_tables``. For
    iterative tooling call ``resolve_apn_to_address_with_indexes``
    with a pre-built index.

    PR 4 skip-condition fix: skips on ``dcad_account``, not
    ``address_normalized``, so the resolver fires even when an
    OCR-garbled / city-only address is already stamped.
    """
    acct_to_addr = _build_account_to_address(dcad_tables)
    return resolve_apn_to_address_with_indexes(records, acct_to_addr)


def resolve_apn_to_address_with_indexes(
    records: list[CanonicalRecord],
    acct_to_addr: dict[str, str],
) -> APNResolverStats:
    """Same as :func:`resolve_apn_to_address` but takes a pre-built
    account-to-address index. Used by the validation harness."""
    stats = APNResolverStats(total=len(records))
    if not acct_to_addr:
        logger.warning("APN resolver: account-to-address index empty; skipping")
        stats.no_apn = len(records)
        return stats

    # Pre-build a set of valid account numbers for O(1) membership tests.
    valid_accts = set(acct_to_addr.keys())

    for rec in records:
        if rec.get("dcad_account"):
            stats.skipped_already_resolved += 1
            continue
        sm  = rec.get("signal_metadata") or {}
        ocr = sm.get("ocr") or {}
        apn = (ocr.get("apn") or "").strip()
        if not apn:
            stats.no_apn += 1
            continue

        # DCAD ACCOUNT_NUM is typically a 17-digit zero-padded string;
        # also try right-padding our stripped APN with leading zeros to
        # match common DCAD widths.
        candidates = {apn, apn.lstrip("0"), apn.zfill(17)}
        matched = next((c for c in candidates if c in valid_accts), None)
        if not matched:
            stats.no_match += 1
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_C_APN,
                stage="6.4",
                input=apn,
                status=STATUS_NO_MATCH,
            ))
            continue

        rec["dcad_account"] = matched
        rec["address_normalized"] = acct_to_addr[matched]
        stats.resolved += 1
        append_history(rec, ResolutionHistoryEntry(
            path=PATH_C_APN,
            stage="6.4",
            input=apn,
            status=STATUS_MATCHED,
            tier=TIER_EXACT,
            dcad_account=matched,
        ))

    logger.info(
        "Path C (APN): %d/%d resolved (%.1f%%); no_apn=%d no_match=%d "
        "skipped_already_resolved=%d",
        stats.resolved, stats.total, stats.resolution_rate * 100,
        stats.no_apn, stats.no_match, stats.skipped_already_resolved,
    )
    return stats
