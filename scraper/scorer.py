"""
Heuristic scoring, stacking, suppression, and HOA filtering.

Pipeline order (after enrichment, before output):

  1. score_record()      → assigns base score to each record (§E.4)
  2. apply_stacking()    → +30 for address matches across categories (§3.4.4)
                            +20 for LP+FC combo at same address (§E.4)
  3. suppress_released() → REL/RLP marks prior records inactive (§E.1)
                            REL records themselves are dropped from output
  4. filter_hoa()        → drops records where grantor matches HOA pattern
                            and no grantee is identified (§3.3.1)

Each pass is a pure function over the record list. Order matters.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from . import config, normalize

logger = logging.getLogger(__name__)

CanonicalRecord = dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════
# Per-record scoring
# ═══════════════════════════════════════════════════════════════════════════

def score_record(record: CanonicalRecord, today: date | None = None) -> tuple[int, dict]:
    """Compute the base score for a single record per §E.4.

    Returns ``(score, breakdown_dict)``. The breakdown lists each component
    that contributed; useful for UI surfacing and debugging score weights.

    Stacking (+30) and LP+FC (+20) bonuses are applied separately in
    :func:`apply_stacking` because they need cross-record visibility.
    """
    today = today or date.today()
    breakdown: dict[str, int] = {"base": config.SCORE_BASE}
    total = config.SCORE_BASE

    # +10 per matched instrument category (one record always matches one)
    breakdown["category_flag"] = config.SCORE_PER_CATEGORY_FLAG
    total += config.SCORE_PER_CATEGORY_FLAG

    # Value bands.
    mv = record.get("dcad_market_value")
    if isinstance(mv, (int, float)):
        if mv >= config.VALUE_BAND_HIGH:
            breakdown["value_100k"] = config.SCORE_VALUE_100K
            total += config.SCORE_VALUE_100K
        elif mv >= config.VALUE_BAND_MID:
            breakdown["value_50k"] = config.SCORE_VALUE_50K
            total += config.SCORE_VALUE_50K

    # Freshness — first_seen within the last 7 days.
    if first_seen := record.get("first_seen"):
        try:
            fs_date = datetime.fromisoformat(first_seen).date()
            if (today - fs_date) <= timedelta(days=7):
                breakdown["new_this_week"] = config.SCORE_NEW_THIS_WEEK
                total += config.SCORE_NEW_THIS_WEEK
        except (ValueError, TypeError):
            pass

    # Address resolution.
    if record.get("dcad_account"):
        breakdown["has_address"] = config.SCORE_HAS_ADDRESS
        total += config.SCORE_HAS_ADDRESS

    total = min(total, config.SCORE_CAP)
    return total, breakdown


# ═══════════════════════════════════════════════════════════════════════════
# Stacking — cross-record bonuses
# ═══════════════════════════════════════════════════════════════════════════

def apply_stacking(records: list[CanonicalRecord]) -> list[CanonicalRecord]:
    """Apply stacking and LP+FC combo bonuses based on address overlap.

    +30 (§3.4.4) — same address appears across two or more categories.
    +20 (§E.4)  — same address has both an L/P and a NOTICE record.

    Mutates each record's ``score`` and ``score_breakdown``. Returns the
    same list.
    """
    # Group categories per normalized address.
    addr_to_categories: dict[str, set[str]] = {}
    for rec in records:
        addr = rec.get("address_normalized")
        cat = rec.get("category")
        if addr and cat:
            addr_to_categories.setdefault(addr, set()).add(cat)

    for rec in records:
        addr = rec.get("address_normalized")
        if not addr:
            continue
        cats = addr_to_categories.get(addr, set())

        # Multi-category stacking bonus.
        if len(cats) >= 2:
            rec["score"] += config.SCORE_STACKING_ADDRESS_MATCH
            rec.setdefault("score_breakdown", {})["stacking"] = (
                config.SCORE_STACKING_ADDRESS_MATCH
            )

        # LP + NOTICE (FC) combo bonus.
        lp_cat, fc_cat = config.LP_FC_PAIR
        if lp_cat in cats and fc_cat in cats:
            rec["score"] += config.SCORE_LP_FC_COMBO
            rec.setdefault("score_breakdown", {})["lp_fc_combo"] = (
                config.SCORE_LP_FC_COMBO
            )

        # Re-cap after additions.
        if rec["score"] > config.SCORE_CAP:
            rec["score"] = config.SCORE_CAP

    return records


# ═══════════════════════════════════════════════════════════════════════════
# Release / suppression
# ═══════════════════════════════════════════════════════════════════════════

def suppress_released(records: list[CanonicalRecord]) -> list[CanonicalRecord]:
    """Apply REL/RLP records as suppression signals.

    For each REL or RLP record:
      - Find prior records at the same normalized address whose filing_date
        is on or before the release's filing_date.
      - Mark them ``active = False`` and stamp ``release_record_id`` with
        the releasing record's id.

    The REL/RLP records themselves are removed from the returned list
    (they aren't leads; they're metadata).
    """
    rels = [r for r in records if normalize.is_suppression_code(r.get("dallas_code", ""))]
    kept = [r for r in records if not normalize.is_suppression_code(r.get("dallas_code", ""))]

    for rel in rels:
        rel_addr = rel.get("address_normalized")
        rel_date = rel.get("filing_date")
        if not rel_addr:
            continue

        for rec in kept:
            if rec.get("address_normalized") != rel_addr:
                continue
            if rel_date and rec.get("filing_date") and rec["filing_date"] > rel_date:
                continue  # only suppress prior records
            rec["active"] = False
            rec["release_record_id"] = rel["record_id"]

    logger.info(
        "Suppression: %d REL/RLP records applied; %d records marked inactive",
        len(rels),
        sum(1 for r in kept if not r.get("active", True)),
    )
    return kept


# ═══════════════════════════════════════════════════════════════════════════
# HOA filter — §3.3.1 fix
# ═══════════════════════════════════════════════════════════════════════════

def filter_hoa(records: list[CanonicalRecord]) -> tuple[list[CanonicalRecord], int]:
    """Drop records that look like HOA-as-plaintiff with no real lead target.

    A record is dropped iff:
      - grantor matches HOA patterns AND
      - grantee is missing or also looks like an HOA / corporate entity

    Records where grantor is an HOA but grantee is a clear individual
    (the homeowner) are KEPT — the homeowner is the actual lead.

    Returns (kept_records, dropped_count).
    """
    kept: list[CanonicalRecord] = []
    dropped = 0

    for rec in records:
        grantor = rec.get("grantor") or ""
        grantee = rec.get("grantee") or ""

        grantor_is_hoa = normalize.is_hoa_entity(grantor)
        grantee_is_hoa = normalize.is_hoa_entity(grantee)
        grantee_present = bool(grantee.strip())

        # Drop only when grantor is HOA AND (no grantee OR grantee is also HOA).
        if grantor_is_hoa and (not grantee_present or grantee_is_hoa):
            dropped += 1
            continue

        kept.append(rec)

    logger.info("HOA filter: dropped %d / %d records", dropped, len(records))
    return kept, dropped


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════════════════

def score_and_filter(records: list[CanonicalRecord]) -> tuple[list[CanonicalRecord], dict]:
    """Run the full scoring + filtering pipeline.

    Returns ``(final_records, summary)`` where ``summary`` is a dict with
    counts at each stage, useful for the run-summary notification.
    """
    summary: dict[str, int] = {"input": len(records)}

    # 1. Per-record base score.
    for rec in records:
        score, breakdown = score_record(rec)
        rec["score"] = score
        rec["score_breakdown"] = breakdown
    summary["scored"] = len(records)

    # 2. Stacking + combo.
    apply_stacking(records)

    # 3. Suppression.
    records = suppress_released(records)
    summary["after_suppression"] = len(records)

    # 4. HOA filter.
    records, hoa_dropped = filter_hoa(records)
    summary["hoa_filtered_count"] = hoa_dropped
    summary["final"] = len(records)

    return records, summary
