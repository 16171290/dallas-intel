"""
Daily pipeline entrypoint.

Run with::

    python -m scraper.main

GitHub Actions invokes this from ``.github/workflows/scrape.yml``. Cron
jitter (Sec 3.7.9), env-secret consumption, and failure-notification paths
all live here.

Pipeline stages (full order - each enclosed in try/except so a failure
in one stage notifies and exits non-zero):

  1. Cron jitter sleep (0-30 min random, configurable)
  2. DCAD bulk-data fetch (cached weekly)
  3. Foreclosure-PDF walk + extract
  4. publicsearch.us SPA scrape       (DISABLED by default - see below)
  5. Merge sources + canonicalize
  6. Read prior records.json, merge first_seen / last_seen
  7. Enrich with DCAD
  8. Governmental-grantor suppression (Phase 0.A)
  9. Score + stack + suppress + HOA filter
 10. Buy-box annotation (Phase 0.A)
 11. Write records.json + daily CSV
 12. Notify Discord

publicsearch.us gating
----------------------
As of 2026-05-14, publicsearch.us is DISABLED by default. The source has
required repeated selector maintenance, and the migration plan is to
replace its high-value signals (PB, BR, LP, AT) with direct sources:
Tyler Odyssey for court records (Phase 1), PACER for federal bankruptcy
(Phase 3), and Linebarger for tax sales (Phase 4).

To re-enable publicsearch.us:
    Set environment variable  PUBLICSEARCH_ENABLED=true
    (in .env locally, or as a GitHub Actions secret for CI)

The publicsearch.py module is unchanged and still imported, so flipping
the env var is the only step needed to restore the source.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
import traceback
from datetime import date
from pathlib import Path

from . import (
    buy_box,
    config,
    dcad_bulk,
    enrichment,
    entity_filter,
    foreclosure_pdfs,
    governmental_grantor,
    legal_resolver,
    monitor,
    output,
    probate,
    publicsearch,
    scorer,
)

logger = logging.getLogger("scraper.main")


def main() -> int:
    """Run the daily pipeline. Returns the desired exit code (0 = success)."""
    _configure_logging()
    _jitter_sleep()

    logger.info("=" * 70)
    logger.info("dallas-intel run starting at %s", date.today().isoformat())
    logger.info("=" * 70)

    try:
        return _run_pipeline()
    except Exception as exc:  # last-resort catch - notify and exit non-zero
        logger.exception("Pipeline failed at top level")
        monitor.notify_failure(
            error=f"Uncaught exception: {type(exc).__name__}: {exc}",
            context={"traceback": traceback.format_exc()[-1500:]},
        )
        return 1


def _publicsearch_enabled() -> bool:
    """Read the PUBLICSEARCH_ENABLED env var. Defaults to False (disabled)."""
    return os.getenv("PUBLICSEARCH_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _run_pipeline() -> int:
    # 1. DCAD bulk data ------------------------------------------------------
    logger.info("[1/12] Fetching DCAD bulk data")
    try:
        zip_path = dcad_bulk.fetch_dcad_zip()
        dcad_tables = dcad_bulk.parse_dcad_tables(zip_path)
        address_index = dcad_bulk.build_address_index(dcad_tables)
    except Exception as exc:
        return _fail("DCAD fetch failed", exc, "dcad_bulk")

    # 2. Foreclosure-PDF walk ------------------------------------------------
    logger.info("[2/12] Walking foreclosure-PDF index")
    pdf_records_canonical: list[dict] = []
    try:
        index = foreclosure_pdfs.walk_foreclosure_index()
        pdf_dest = config.DATA_DIR / "foreclosure_pdfs"
        for ref in index:
            local_path, downloaded = foreclosure_pdfs.download_pdf(ref, pdf_dest)
            try:
                records = foreclosure_pdfs.extract_pdf_records(
                    local_path,
                    pdf_url=ref.pdf_url,
                )
                pdf_records_canonical.extend(
                    enrichment.canonicalize_foreclosure(r) for r in records
                )
            except Exception as exc:
                logger.warning("PDF extract failed for %s: %s", local_path.name, exc)
    except Exception as exc:
        # PDF-side failure shouldn't kill the whole run; log + continue
        logger.warning("Foreclosure-PDF stage failed: %s - continuing without PDFs", exc)
        monitor.notify_failure(
            error="Foreclosure-PDF stage failed (non-fatal)",
            context={"exception": str(exc)},
        )

    # 3. publicsearch.us scrape (gated) --------------------------------------
    logger.info("[3/12] publicsearch.us scrape")
    ps_records_canonical: list[dict] = []
    if not _publicsearch_enabled():
        logger.info(
            "publicsearch.us SKIPPED - PUBLICSEARCH_ENABLED env var not set. "
            "Disabled 2026-05-14 pending Phase 1 (Odyssey) replacement. "
            "Set PUBLICSEARCH_ENABLED=true to re-enable."
        )
    else:
        # Non-fatal: publicsearch.us is a third-party SPA aggregator with
        # known selector-drift risk. A failure here MUST NOT kill the whole
        # run - the foreclosure-PDF pipeline (and probate, if enabled) must
        # continue. Matches the Stage 2 (foreclosure-PDF) failure pattern.
        try:
            date_from, date_to = publicsearch.daily_window()
            ps_records = publicsearch.scrape_all(date_from, date_to)
            ps_records_canonical = [enrichment.canonicalize_publicsearch(r) for r in ps_records]
            logger.info("publicsearch.us: %d records fetched", len(ps_records_canonical))
        except publicsearch.CircuitBreakerTripped as exc:
            logger.warning(
                "publicsearch.us circuit breaker tripped: %s - "
                "continuing without publicsearch records", exc,
            )
            monitor.notify_failure(
                error="publicsearch.us circuit breaker tripped (non-fatal)",
                context={"stage": "publicsearch", "exception": str(exc)},
            )
        except Exception as exc:
            logger.warning(
                "publicsearch.us scrape failed: %s - "
                "continuing without publicsearch records", exc,
            )
            monitor.notify_failure(
                error="publicsearch.us scrape failed (non-fatal)",
                context={"stage": "publicsearch", "exception_type": type(exc).__name__, "exception": str(exc)},
            )

    # 4. Probate (re:SearchTX) ----------------------------------------------
    # Gated on config.PROBATE_ENABLED (default False). Non-fatal: probate is
    # additive, so failures here must not kill the whole run. The fetch
    # function itself returns [] on any error per its contract, so we only
    # need defensive wrapping for catastrophic faults (e.g. import failure
    # or unexpected exception types).
    logger.info("[4/12] Probate fetch (re:SearchTX)")
    probate_records_canonical: list[dict] = []
    if not config.PROBATE_ENABLED:
        logger.info(
            "Probate SKIPPED - PROBATE_ENABLED env var not set. "
            "Set PROBATE_ENABLED=true (with RESEARCH_TX_EMAIL and "
            "RESEARCH_TX_PASSWORD secrets) to enable."
        )
    else:
        try:
            probate_records = probate.fetch_dallas_probate(days_back=config.DAYS_BACK)
            probate_records_canonical = [
                enrichment.canonicalize_probate(r) for r in probate_records
            ]
            logger.info("Probate: %d records fetched", len(probate_records_canonical))
        except Exception as exc:
            logger.warning("Probate stage failed: %s - continuing without probate", exc)
            monitor.notify_failure(
                error="Probate stage failed (non-fatal)",
                context={"exception": str(exc)},
            )

    # 5. Merge sources -------------------------------------------------------
    logger.info("[5/12] Merging %d publicsearch + %d PDF + %d probate records",
                len(ps_records_canonical),
                len(pdf_records_canonical),
                len(probate_records_canonical))
    all_records = ps_records_canonical + pdf_records_canonical + probate_records_canonical

    if not all_records:
        logger.warning(
            "No records harvested from any enabled source. "
            "Continuing to write empty records.json so dashboard reflects "
            "the run, but this likely indicates a data-source problem."
        )

    # 6. Merge first_seen / last_seen from prior archive ---------------------
    logger.info("[6/12] Merging with prior records.json")
    prior_records = output.read_records_json(config.RECORDS_JSON)
    all_records = _merge_seen_dates(all_records, prior_records)

    # 6.5 Legal-description -> DCAD address resolution -----------------------
    # Publicsearch records lack a street address at canonicalization time;
    # they carry the property's legal description in raw_excerpt. This stage
    # parses that description, looks it up in DCAD's ACCOUNT_INFO via
    # subdivision/lot/block, and stamps address_normalized on matched records.
    # Stage 7's address-based DCAD enrichment then picks them up. Skips
    # records that already have an address (foreclosure-PDF source).
    logger.info("[6.5/12] Legal-description -> DCAD address resolution")
    legal_stats = legal_resolver.resolve_legal_descriptions(all_records, dcad_tables)
    logger.info(
        "Legal-description resolver: %d/%d resolved (%.1f%%); "
        "no_parse=%d no_match=%d multi=%d no_snippet=%d",
        legal_stats.resolved, legal_stats.total, legal_stats.resolution_rate * 100,
        legal_stats.no_parse, legal_stats.no_match, legal_stats.multi_match,
        legal_stats.no_snippet,
    )

    # 7. Enrich --------------------------------------------------------------
    logger.info("[7/12] Enriching with DCAD")
    all_records, enrich_stats = enrichment.enrich_batch(
        all_records, dcad_tables, address_index,
    )

    # 8. Governmental-grantor suppression (Phase 0.A) ------------------------
    # Removes records where the grantor is a government entity (Trinity
    # River Authority, City of Dallas, DISD, IRS, etc.). Such records are
    # never motivated-seller leads - they're easements, condemnations, or
    # tax sales filed BY the government. Done BEFORE scoring so we don't
    # waste scoring cycles on noise.
    logger.info("[8/12] Governmental-grantor suppression")
    before = len(all_records)
    all_records, gov_removed = governmental_grantor.filter_governmental_records(all_records)
    gov_filtered_count = len(gov_removed)
    logger.info(
        "Governmental-grantor filter: removed %d records (%d -> %d)",
        gov_filtered_count, before, len(all_records),
    )
    if gov_removed[:3]:
        logger.info(
            "Sample governmental grantors removed: %s",
            [r.get("grantor", "?") for r in gov_removed[:3]],
        )

    # 8.5 Corporate-entity grantee suppression -------------------------------
    # Removes records where the grantee is a corporate or institutional
    # entity (LLCs, banks, churches, hospitals, etc.) - not a motivated-
    # seller candidate. Family trusts are preserved (estate scenarios are
    # real lead targets). LC city-lien records swap to grantor when grantee
    # is governmental, since the property owner is on the grantor side for
    # those records.
    logger.info("[8.5/12] Corporate-entity grantee suppression")
    before = len(all_records)
    all_records, entity_removed = entity_filter.filter_entity_records(all_records)
    entity_filtered_count = len(entity_removed)
    logger.info(
        "Entity filter: removed %d records (%d -> %d)",
        entity_filtered_count, before, len(all_records),
    )
    if entity_removed[:3]:
        logger.info(
            "Sample entity grantees removed: %s",
            [r.get("grantee", "?") for r in entity_removed[:3]],
        )

    # 9. Score + stack + suppress + HOA filter -------------------------------
    logger.info("[9/12] Scoring + filtering")
    all_records, scoring_summary = scorer.score_and_filter(all_records)

    # 10. Buy-box annotation (Phase 0.A) -------------------------------------
    # Tags each record with `in_buy_box: bool` and `buy_box_reasons: list[str]`
    # based on operator-configured criteria (env vars: BUY_BOX_MIN_PRICE,
    # BUY_BOX_MAX_PRICE, BUY_BOX_ZIP_ALLOWLIST, etc). Does NOT remove records
    # - they remain in records.json for audit. The CSV writer filters on
    # in_buy_box=True so only in-box leads go to outreach.
    logger.info("[10/12] Buy-box annotation")
    bb = buy_box.BuyBox.from_env()
    buy_box_summary = buy_box.annotate_records(all_records, bb)
    logger.info(
        "Buy-box: %d / %d in buy-box (%s)",
        buy_box_summary["in_buy_box"],
        buy_box_summary["total"],
        buy_box_summary["criteria"],
    )
    if buy_box_summary["top_exclusion_reasons"]:
        logger.info(
            "Top exclusion reasons: %s",
            buy_box_summary["top_exclusion_reasons"][:3],
        )

    # 11. Write outputs -----------------------------------------------------
    logger.info("[11/12] Writing records.json + daily CSV")
    output.write_records_json(all_records, config.RECORDS_JSON)
    output.write_daily_csv(all_records, date.today(), config.EXPORTS_DIR)

    # 12. Notify ------------------------------------------------------------
    logger.info("[12/12] Sending Discord notification")
    summary = output.summarize_run(
        all_records,
        extra={
            "hit_rate":            enrich_stats.hit_rate,
            "matched_dcad":        enrich_stats.matched,
            "hoa_filtered_count":  scoring_summary.get("hoa_filtered_count", 0),
            "gov_filtered_count":  gov_filtered_count,
            "buy_box":             buy_box_summary,
            "publicsearch_enabled": _publicsearch_enabled(),
        },
    )
    monitor.notify_run_complete(summary)

    logger.info("=" * 70)
    logger.info(
        "Run complete: %d records, %d in buy-box, hit-rate %.1f%%",
        summary["total"],
        buy_box_summary["in_buy_box"],
        summary["address_resolution_rate"] * 100,
    )
    logger.info("=" * 70)
    return 0


# ============================================================================
# Helpers
# ============================================================================

def _configure_logging() -> None:
    """Standard log format; level controlled by LOG_LEVEL env var."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _jitter_sleep() -> None:
    """Sleep 0..CRON_JITTER_MINUTES (Sec 3.7.9)."""
    if config.CRON_JITTER_MINUTES <= 0:
        return
    minutes = random.uniform(0, config.CRON_JITTER_MINUTES)
    logger.info("Cron jitter: sleeping %.1f minutes", minutes)
    time.sleep(minutes * 60)


def _merge_seen_dates(
    new_records: list[dict],
    prior_records: list[dict],
) -> list[dict]:
    """Copy first_seen from prior records (by record_id) and update last_seen.

    New records (not in prior) get today as both first_seen and last_seen.
    Records from prior that aren't in the new batch are NOT carried forward
    by this function - that responsibility is on the caller if archival is
    needed (currently we keep records.json fresh from the daily scrape).
    """
    today_iso = date.today().isoformat()
    prior_by_id = {r.get("record_id"): r for r in prior_records if r.get("record_id")}

    for rec in new_records:
        rec_id = rec.get("record_id")
        if rec_id in prior_by_id:
            old = prior_by_id[rec_id]
            rec["first_seen"] = old.get("first_seen") or today_iso
        else:
            rec["first_seen"] = today_iso
        rec["last_seen"] = today_iso

    return new_records


def _fail(headline: str, exc: Exception, stage: str) -> int:
    """Notify + log + return non-zero exit code."""
    logger.exception(headline)
    monitor.notify_failure(
        error=f"{headline}: {type(exc).__name__}: {exc}",
        context={"stage": stage, "exception_type": type(exc).__name__},
    )
    return 1


# ============================================================================
# CLI entry
# ============================================================================

if __name__ == "__main__":
    sys.exit(main())
