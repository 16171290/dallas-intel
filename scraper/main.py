"""
Daily pipeline entrypoint.

Run with::

    python -m scraper.main

GitHub Actions invokes this from ``.github/workflows/scrape.yml``. Cron
jitter (§3.7.9), env-secret consumption, and failure-notification paths
all live here.

Pipeline stages (full order — each enclosed in try/except so a failure
in one stage notifies and exits non-zero):

  1. Cron jitter sleep (0–30 min random, configurable)
  2. DCAD bulk-data fetch (cached weekly)
  3. Foreclosure-PDF walk + extract
  4. publicsearch.us SPA scrape
  5. Canonicalize all records
  6. Read prior records.json, merge first_seen / last_seen
  7. Enrich with DCAD
  8. Score + stack + suppress + HOA filter
  9. Write records.json + daily CSV
 10. Notify Discord
"""

from __future__ import annotations

import logging
import random
import sys
import time
import traceback
from datetime import date
from pathlib import Path

from . import config, dcad_bulk, enrichment, foreclosure_pdfs, monitor, output, publicsearch, scorer

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
    except Exception as exc:  # last-resort catch — notify and exit non-zero
        logger.exception("Pipeline failed at top level")
        monitor.notify_failure(
            error=f"Uncaught exception: {type(exc).__name__}: {exc}",
            context={"traceback": traceback.format_exc()[-1500:]},
        )
        return 1


def _run_pipeline() -> int:
    # 1. DCAD bulk data ─────────────────────────────────────────────────────
    logger.info("[1/9] Fetching DCAD bulk data")
    try:
        zip_path = dcad_bulk.fetch_dcad_zip()
        dcad_tables = dcad_bulk.parse_dcad_tables(zip_path)
        address_index = dcad_bulk.build_address_index(dcad_tables)
    except Exception as exc:
        return _fail("DCAD fetch failed", exc, "dcad_bulk")

    # 2. Foreclosure-PDF walk ───────────────────────────────────────────────
    logger.info("[2/9] Walking foreclosure-PDF index")
    pdf_records_canonical: list[dict] = []
    try:
        index = foreclosure_pdfs.walk_foreclosure_index()
        pdf_dest = config.DATA_DIR / "foreclosure_pdfs"
        for ref in index:
            local_path, downloaded = foreclosure_pdfs.download_pdf(ref, pdf_dest)
            try:
                records = foreclosure_pdfs.extract_pdf_records(local_path)
                pdf_records_canonical.extend(
                    enrichment.canonicalize_foreclosure(r) for r in records
                )
            except Exception as exc:
                logger.warning("PDF extract failed for %s: %s", local_path.name, exc)
    except Exception as exc:
        # PDF-side failure shouldn't kill the whole run; log + continue
        logger.warning("Foreclosure-PDF stage failed: %s — continuing without PDFs", exc)
        monitor.notify_failure(
            error="Foreclosure-PDF stage failed (non-fatal)",
            context={"exception": str(exc)},
        )

    # 3. publicsearch.us scrape ─────────────────────────────────────────────
    logger.info("[3/9] Scraping publicsearch.us")
    ps_records_canonical: list[dict] = []
    try:
        date_from, date_to = publicsearch.daily_window()
        ps_records = publicsearch.scrape_all(date_from, date_to)
        ps_records_canonical = [enrichment.canonicalize_publicsearch(r) for r in ps_records]
    except publicsearch.CircuitBreakerTripped as exc:
        return _fail("publicsearch.us circuit breaker tripped", exc, "publicsearch")
    except Exception as exc:
        return _fail("publicsearch.us scrape failed", exc, "publicsearch")

    # 4. Merge sources ──────────────────────────────────────────────────────
    logger.info("[4/9] Merging %d publicsearch + %d PDF records",
                len(ps_records_canonical), len(pdf_records_canonical))
    all_records = ps_records_canonical + pdf_records_canonical

    # 5. Merge first_seen / last_seen from prior archive ────────────────────
    logger.info("[5/9] Merging with prior records.json")
    prior_records = output.read_records_json(config.RECORDS_JSON)
    all_records = _merge_seen_dates(all_records, prior_records)

    # 6. Enrich ─────────────────────────────────────────────────────────────
    logger.info("[6/9] Enriching with DCAD")
    all_records, enrich_stats = enrichment.enrich_batch(
        all_records, dcad_tables, address_index,
    )

    # 7. Score + stack + suppress + HOA filter ──────────────────────────────
    logger.info("[7/9] Scoring + filtering")
    all_records, scoring_summary = scorer.score_and_filter(all_records)

    # 8. Write outputs ──────────────────────────────────────────────────────
    logger.info("[8/9] Writing records.json + daily CSV")
    output.write_records_json(all_records, config.RECORDS_JSON)
    output.write_daily_csv(all_records, date.today(), config.EXPORTS_DIR)

    # 9. Notify ─────────────────────────────────────────────────────────────
    logger.info("[9/9] Sending Discord notification")
    summary = output.summarize_run(
        all_records,
        extra={
            "hit_rate":           enrich_stats.hit_rate,
            "matched_dcad":       enrich_stats.matched,
            "hoa_filtered_count": scoring_summary.get("hoa_filtered_count", 0),
        },
    )
    monitor.notify_run_complete(summary)

    logger.info("=" * 70)
    logger.info("Run complete: %d records, hit-rate %.1f%%",
                summary["total"], summary["address_resolution_rate"] * 100)
    logger.info("=" * 70)
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _configure_logging() -> None:
    """Standard log format; level controlled by LOG_LEVEL env var."""
    import os
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _jitter_sleep() -> None:
    """Sleep 0..CRON_JITTER_MINUTES (§3.7.9)."""
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
    by this function — that responsibility is on the caller if archival is
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


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(main())
