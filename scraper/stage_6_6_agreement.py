"""Stage 6.6 — cross-path agreement audit + page-text tiebreaker.

After Paths A/B/C/variant all run in diagnostic mode (PR 5: ``always_run=True``),
each record's ``signal_metadata.resolution_history`` carries an entry for
every path that produced a matched (or guarded) result. Stage 6.6 audits
those entries to surface two outcomes:

  AGREEMENT: 2+ paths converged on the same ``dcad_account``. High-confidence
             signal that we have the right property. Stamp WARN_PATH_AGREEMENT.

  DISAGREEMENT: 2+ paths picked DIFFERENT ``dcad_account``s. Low-confidence.
                Optionally fetch the publicsearch.us /doc/{record_id} page
                summary (which carries the address text the County Clerk
                site itself parsed) and use it as a referee: score each
                candidate's address_normalized against the page text via
                token overlap. Highest-score candidate becomes the primary
                ``dcad_account``; others move to ``alternate_accounts``.

                When the page text is sparse (just a city, or empty), the
                tiebreaker is inconclusive — keep whatever was already
                stamped and add WARN_PAGE_TIEBREAK_INCONCLUSIVE. No false
                confidence.

Per the user's clarification (2026-05-27): the publicsearch page is a
*referee*, not a primary source. Most pages carry only partial address
info, which is enough to break a tie between known candidates but is
NOT a reliable standalone resolution path.

Public API:
  audit_agreement(rec) -> AgreementState
  score_address_match(candidate, page_text) -> float
  run_stage_6_6(records, acct_to_addr, page_fetcher=None) -> Stage66Stats

The ``page_fetcher`` is a callable ``record_id -> Optional[str]`` that
returns the page's Property Address text (possibly partial). Caller
constructs it (typically via ``scraper.page_fetcher.PageFetcher``).
``None`` disables external tiebreaking; disagreements still get the
warning but no automatic resolution.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .resolution import (
    PATH_A_GRANTOR_OWNER_INDEX,
    PATH_B_RAW_EXCERPT,
    PATH_C_APN,
    PATH_C_LEGAL_RESOLVER,
    PATH_PB_DECEDENT_OWNER_INDEX,
    PATH_STAGE_6_6,
    PATH_STAGE_7_ADDRESS_INDEX,
    PATH_VARIANT_LOOKUP,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    STATUS_SKIPPED,
    WARN_PAGE_TIEBREAK_INCONCLUSIVE,
    WARN_PATH_AGREEMENT,
    WARN_PATH_DISAGREEMENT,
    WARN_TIEBROKEN_BY_PAGE,
    ResolutionHistoryEntry,
    add_warning,
    append_history,
    get_history,
    set_alternate_accounts,
)


# Paths whose ``dcad_account`` represents the PRIMARY PROPERTY of interest
# for the record (vs. ancillary fields like the heir's mailing address).
# Stage 6.6 uses this to scope agreement/disagreement detection so that
# probate applicant-mailing matches (which legitimately differ from the
# decedent's property) don't get treated as conflicting candidates.
#
# Excluded deliberately:
#   PATH_PB_APPLICANT_OWNER_INDEX — applicant mailing-address resolution,
#                                   not the property being adjudicated
_PROPERTY_CANDIDATE_PATHS: frozenset[str] = frozenset({
    PATH_STAGE_7_ADDRESS_INDEX,
    PATH_B_RAW_EXCERPT,
    PATH_VARIANT_LOOKUP,
    PATH_A_GRANTOR_OWNER_INDEX,
    PATH_C_LEGAL_RESOLVER,
    PATH_C_APN,
    PATH_PB_DECEDENT_OWNER_INDEX,
})

logger = logging.getLogger(__name__)


# Address tokens that carry no disambiguating information. When scoring
# candidate addresses against page text, these are stripped from both
# sides so a tie between "706 CORDOVA ST" and "3031 CREST RIDGE DR"
# isn't influenced by the noise tokens "ST" / "DR".
_NOISE_TOKENS = frozenset({
    "ST", "AVE", "DR", "RD", "BLVD", "LN", "CT", "WAY", "PL", "PKWY",
    "TER", "CIR", "TRL", "HWY", "STREET", "AVENUE", "DRIVE", "ROAD",
    "BOULEVARD", "LANE", "COURT", "PLACE", "PARKWAY", "TERRACE",
    "CIRCLE", "TRAIL", "HIGHWAY",
    "N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST",
    "NE", "NW", "SE", "SW",
    "DALLAS", "TX", "TEXAS",
    # Common Dallas-area cities (so "GRAND PRAIRIE" appearing in both
    # candidates doesn't dominate; the differentiator is street name + number)
    "IRVING", "GARLAND", "MESQUITE", "PLANO", "RICHARDSON", "ROWLETT",
    "DESOTO", "DUNCANVILLE", "FARMERSBRANCH", "FARMERS", "BRANCH",
    "GRAND", "PRAIRIE", "CARROLLTON",
    # USPS-style separators that survive normalization
    "OF", "AT", "THE",
})

# Token splitter: alphanumeric runs (preserves "1ST", "I35", etc.)
_TOKEN_RE = re.compile(r"[A-Z0-9]+")


# ─────────────────────────────────────────────────────────────────────────
# Agreement detection
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgreementState:
    """Result of auditing one record's resolution_history."""
    kind:           str                    # "no_match" | "single" | "agreement" | "disagreement"
    picks:          dict[str, list[str]]   # {dcad_account: [paths that picked it]}
    primary_acct:   Optional[str]          # what's currently stamped on the record

    @property
    def has_disagreement(self) -> bool:
        return self.kind == "disagreement"

    @property
    def has_agreement(self) -> bool:
        return self.kind == "agreement"


def audit_agreement(rec: dict) -> AgreementState:
    """Inspect a record's resolution_history and return the agreement state.

    Counts each (path, dcad_account) pair where status is MATCHED or
    MULTI_MATCH. Groups by account; reports kind.
    """
    history = get_history(rec)
    # PR 7.5: only count matches from paths that resolve the PRIMARY
    # property of the record. Excludes applicant-mailing matches (which
    # legitimately differ from the decedent's property in probate cases)
    # so they don't generate false-positive cross-path disagreements.
    matching_entries = [
        h for h in history
        if h.get("status") in (STATUS_MATCHED, STATUS_MULTI_MATCH)
        and h.get("dcad_account")
        and h.get("path") in _PROPERTY_CANDIDATE_PATHS
    ]

    picks: dict[str, list[str]] = {}
    for h in matching_entries:
        acct = h["dcad_account"]
        path = h.get("path", "?")
        picks.setdefault(acct, []).append(path)

    primary = rec.get("dcad_account")

    if not picks:
        return AgreementState(kind="no_match", picks={}, primary_acct=primary)
    if len(picks) == 1 and len(next(iter(picks.values()))) == 1:
        return AgreementState(kind="single", picks=picks, primary_acct=primary)
    if len(picks) == 1:
        # One account picked by multiple paths
        return AgreementState(kind="agreement", picks=picks, primary_acct=primary)
    return AgreementState(kind="disagreement", picks=picks, primary_acct=primary)


# ─────────────────────────────────────────────────────────────────────────
# Token-overlap scorer
# ─────────────────────────────────────────────────────────────────────────


def _significant_tokens(text: Optional[str]) -> set[str]:
    """Tokenize and strip noise. Returns uppercased tokens only."""
    if not text:
        return set()
    return {
        t for t in _TOKEN_RE.findall(text.upper())
        if t not in _NOISE_TOKENS
    }


def score_address_match(candidate: Optional[str], page_text: Optional[str]) -> float:
    """Token-overlap score between candidate address and page text, 0.0-1.0.

    Both sides are uppercased, tokenized, and stripped of noise tokens
    (street-type suffixes, directional letters, common cities). The score
    is ``|common| / |candidate_tokens|`` — biased to reward "candidate
    fully present in page text" (the page often has more context than
    needed; the candidate is the canonical address we want to verify).

    Returns 0.0 on empty inputs. Returns 1.0 when every significant
    candidate token appears in the page text.
    """
    cand_tokens = _significant_tokens(candidate)
    page_tokens = _significant_tokens(page_text)
    if not cand_tokens or not page_tokens:
        return 0.0
    common = cand_tokens & page_tokens
    return len(common) / len(cand_tokens)


# ─────────────────────────────────────────────────────────────────────────
# Stage 6.6 entry point
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Stage66Stats:
    """Per-run observability counters for Stage 6.6."""
    total_records:        int = 0
    no_match_records:     int = 0   # 0 paths matched
    single_path_records:  int = 0   # 1 path matched — no agreement signal
    agreement_records:    int = 0   # 2+ paths agreed
    disagreement_records: int = 0   # 2+ paths disagreed
    pages_fetched:        int = 0
    tiebroken:            int = 0   # disagreement resolved by page text
    inconclusive:         int = 0   # disagreement could not be resolved


def run_stage_6_6(
    records: list[dict],
    acct_to_addr: dict[str, str],
    page_fetcher: Optional[Callable[[str], Optional[str]]] = None,
    *,
    tiebreak_min_score: float = 0.30,
    tiebreak_min_margin: float = 0.15,
) -> Stage66Stats:
    """Audit each record's resolution_history; surface agreement /
    disagreement signals; optionally fetch publicsearch.us /doc/ pages
    to break ties.

    Args:
        records: in-place mutated list of canonical records.
        acct_to_addr: ``{dcad_account: address_normalized}`` (same map
            legal_resolver builds) — used to score each disagreement
            candidate's address against page text.
        page_fetcher: optional ``record_id -> page text`` callable.
            ``None`` (default) disables external tiebreaking; disagreements
            still get a warning but the existing stamp is left alone.
        tiebreak_min_score: minimum token-overlap score for the winning
            candidate to be considered a valid tiebreak. Below this the
            tiebreaker is marked inconclusive.
        tiebreak_min_margin: minimum lead (winner.score - runner_up.score)
            to count as a tiebreak. Below this it's inconclusive.

    Returns:
        Stage66Stats with run-level counters.

    Side effects on each record:
        - Adds WARN_PATH_AGREEMENT when 2+ paths converged on the same
          account
        - Adds WARN_PATH_DISAGREEMENT when 2+ paths disagreed
        - When a disagreement is resolved by page text:
            * dcad_account is reassigned to the winning candidate
            * address_normalized is updated from acct_to_addr
            * losing accounts move into signal_metadata.alternate_accounts
            * adds WARN_TIEBROKEN_BY_PAGE
            * appends a Stage66 history entry
        - When inconclusive: adds WARN_PAGE_TIEBREAK_INCONCLUSIVE
    """
    stats = Stage66Stats(total_records=len(records))

    for rec in records:
        state = audit_agreement(rec)

        if state.kind == "no_match":
            stats.no_match_records += 1
            continue

        if state.kind == "single":
            stats.single_path_records += 1
            continue

        if state.kind == "agreement":
            stats.agreement_records += 1
            add_warning(rec, WARN_PATH_AGREEMENT)
            paths = sorted(state.picks[next(iter(state.picks))])
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_STAGE_6_6,
                stage="6.6",
                input="|".join(paths),
                status=STATUS_MATCHED,
                dcad_account=next(iter(state.picks)),
            ))
            continue

        # DISAGREEMENT — try to break the tie with the publicsearch page
        stats.disagreement_records += 1
        add_warning(rec, WARN_PATH_DISAGREEMENT)
        candidates = list(state.picks.keys())  # the disagreeing accounts

        if page_fetcher is None:
            # No external referee; record disagreement but don't touch stamp
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_STAGE_6_6,
                stage="6.6",
                input="|".join(sorted(
                    f"{p}->{a}" for a, paths in state.picks.items() for p in paths
                )),
                status=STATUS_SKIPPED,
                skip_reason="disagreement_no_referee",
                alternates=candidates,
            ))
            continue

        record_id = rec.get("record_id")
        if not record_id:
            stats.inconclusive += 1
            add_warning(rec, WARN_PAGE_TIEBREAK_INCONCLUSIVE)
            continue

        page_text = None
        fetch_error = None
        try:
            page_text = page_fetcher(record_id)
            stats.pages_fetched += 1
        except Exception as e:
            fetch_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "Stage 6.6: page fetch failed for record_id=%s: %s",
                record_id, fetch_error,
            )

        # Score each candidate against the page text
        scored: list[tuple[float, str]] = []
        for acct in candidates:
            addr = acct_to_addr.get(acct, "")
            ratio = score_address_match(addr, page_text)
            scored.append((ratio, acct))
        scored.sort(reverse=True)

        # Diagnostic log — one INFO-level line per disagreement so the
        # operator can see exactly what the page fetcher returned, how
        # each candidate scored, and why a record ended up tiebroken vs.
        # inconclusive. Indispensable for diagnosing fetcher failures or
        # threshold-tuning needs.
        top_score = scored[0][0] if scored else 0.0
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        margin    = top_score - runner_up
        if fetch_error:
            page_text_display = f"<FETCH_FAILED: {fetch_error}>"
        elif page_text is None:
            page_text_display = "<FETCHER_RETURNED_NONE>"
        elif not page_text.strip():
            page_text_display = "<EMPTY_STRING>"
        else:
            page_text_display = page_text[:200]

        candidates_dump = ", ".join(
            f"{a}:'{acct_to_addr.get(a, '<no_address>')}'={s:.3f}"
            for s, a in scored
        )

        # Decide inconclusive vs. tiebroken
        is_inconclusive = (
            not page_text
            or top_score < tiebreak_min_score
            or margin < tiebreak_min_margin
        )

        if is_inconclusive:
            if not page_text:
                reason = "no_page_text"
            elif top_score < tiebreak_min_score:
                reason = f"top_score_{top_score:.3f}_below_threshold_{tiebreak_min_score}"
            else:
                reason = (f"margin_{margin:.3f}_below_threshold_"
                          f"{tiebreak_min_margin}_(top={top_score:.3f} "
                          f"runner_up={runner_up:.3f})")
        else:
            reason = (f"tiebroken_top={top_score:.3f} "
                      f"runner_up={runner_up:.3f} margin={margin:.3f}")

        logger.info(
            "Stage 6.6 disagreement record_id=%s | page=%r | "
            "candidates=[%s] | decision=%s",
            record_id, page_text_display, candidates_dump, reason,
        )

        if is_inconclusive:
            stats.inconclusive += 1
            add_warning(rec, WARN_PAGE_TIEBREAK_INCONCLUSIVE)
            append_history(rec, ResolutionHistoryEntry(
                path=PATH_STAGE_6_6,
                stage="6.6",
                input=page_text[:120] if page_text else None,
                status=STATUS_SKIPPED,
                skip_reason="tiebreak_inconclusive",
                alternates=candidates,
            ))
            continue

        # Definitive winner
        winner_acct = scored[0][1]
        losers = [a for a in candidates if a != winner_acct]

        # Re-stamp record fields with the winner
        rec["dcad_account"] = winner_acct
        winner_addr = acct_to_addr.get(winner_acct)
        if winner_addr:
            rec["address_normalized"] = winner_addr
        set_alternate_accounts(rec, losers)
        add_warning(rec, WARN_TIEBROKEN_BY_PAGE)
        stats.tiebroken += 1

        append_history(rec, ResolutionHistoryEntry(
            path=PATH_STAGE_6_6,
            stage="6.6",
            input=page_text[:120],
            status=STATUS_MATCHED,
            dcad_account=winner_acct,
            alternates=losers,
            warnings=[WARN_TIEBROKEN_BY_PAGE],
        ))

    return stats


def log_stage_6_6_summary(stats: Stage66Stats) -> None:
    """Single-line operations summary."""
    logger.info(
        "Stage 6.6 (cross-path agreement): "
        "%d records | no_match=%d single=%d agreement=%d disagreement=%d "
        "(pages_fetched=%d tiebroken=%d inconclusive=%d)",
        stats.total_records,
        stats.no_match_records,
        stats.single_path_records,
        stats.agreement_records,
        stats.disagreement_records,
        stats.pages_fetched,
        stats.tiebroken,
        stats.inconclusive,
    )
