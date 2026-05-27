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
    address_variants,
    buy_box,
    config,
    dcad_bulk,
    dcad_owner_index,
    enrichment,
    entity_filter,
    foreclosure_ocr,
    foreclosure_pdfs,
    foreclosures_ps,
    governmental_grantor,
    legal_resolver,
    monitor,
    output,
    page_fetcher,
    probate,
    publicsearch,
    resolution,
    resolution_paths,
    scorer,
    stage_6_6_agreement,
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


def _foreclosure_pdf_enabled() -> bool:
    """Read the FORECLOSURE_PDF_ENABLED env var. Defaults to False.

    The dallascounty.org PDF feed was deprecated for fresh foreclosure
    notices on 2026-02-24 (per the county's own banner pointing operators
    to publicsearch.us). The walker is kept available for archival
    research but is off by default to avoid producing dead leads.
    """
    return os.getenv("FORECLOSURE_PDF_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _foreclosure_ocr_enabled() -> bool:
    """Read the FORECLOSURE_OCR_ENABLED env var. Defaults to False.

    The OCR stage downloads each foreclosure notice's PNG pages from
    publicsearch.us and runs Tesseract over them to extract owner name,
    full property address, loan amount, and legal description. Adds
    ~6-10 seconds per record (~10-15 min for a typical 60-80 record
    weekly run). Gated so operators can keep fast iteration on other
    pipeline stages when not actively working foreclosure leads.

    Set FORECLOSURE_OCR_ENABLED=true to enable. Requires Tesseract
    binary installed locally (see scraper/foreclosure_ocr.py docstring).
    """
    return os.getenv("FORECLOSURE_OCR_ENABLED", "false").strip().lower() in (
        "true", "1", "yes", "on",
    )


def apply_grantor_fallback_from_dcad_owner(records: list[dict]) -> int:
    """When ``grantor`` is null but ``dcad_owner`` is set, copy
    dcad_owner into grantor with a ``WARN_GRANTOR_FROM_DCAD`` warning.

    NOF records frequently come out of OCR with grantor=None because the
    "Unofficial Copy" watermark on publicsearch.us page renders mangles
    the labeled grantor line. By the time enrich_batch has run, the
    address-keyed DCAD lookup has populated dcad_owner from ACCOUNT_INFO.
    For foreclosure records, the DCAD owner IS the foreclosed homeowner
    — the current recorded owner is the lead the operator wants to dial.

    Surfaces dcad_owner as grantor with WARN_GRANTOR_FROM_DCAD so the
    operator sees this name came from DCAD ownership records (not the
    document OCR). Does not overwrite a non-null grantor.

    Returns the number of records mutated.
    """
    count = 0
    for rec in records:
        if not (rec.get("grantor") or "").strip():
            owner = (rec.get("dcad_owner") or "").strip()
            if owner:
                rec["grantor"] = owner
                resolution.add_warning(rec, resolution.WARN_GRANTOR_FROM_DCAD)
                count += 1
    return count


def _run_pipeline() -> int:
    # 1. DCAD bulk data ------------------------------------------------------
    logger.info("[1/12] Fetching DCAD bulk data")
    try:
        zip_path = dcad_bulk.fetch_dcad_zip()
        dcad_tables = dcad_bulk.parse_dcad_tables(zip_path)
        address_index = dcad_bulk.build_address_index(dcad_tables)
        # Owner-keyed indexes for probate decedent->property fan-out
        # plus applicant->mailing fan-out (PR 5.y). Built once here so
        # all probate canonicalizations share them.
        owner_index = dcad_owner_index.build_owner_index(dcad_tables)
        account_address_index = dcad_bulk.build_account_index(dcad_tables)
        mailing_index = dcad_bulk.build_mailing_index(dcad_tables)
        logger.info(
            "DCAD indexes ready: %d address, %d owner, %d account, %d mailing keys",
            len(address_index), len(owner_index),
            len(account_address_index), len(mailing_index),
        )
    except Exception as exc:
        return _fail("DCAD fetch failed", exc, "dcad_bulk")

    # 2. Foreclosure-PDF walk ------------------------------------------------
    # dallascounty.org's foreclosure-PDF feed was deprecated as a source of
    # fresh notices on 2026-02-24 (the county redirected operators to
    # publicsearch.us). Default-off; set FORECLOSURE_PDF_ENABLED=true to
    # re-enable for archival research.
    logger.info("[2/12] Walking foreclosure-PDF index")
    pdf_records_canonical: list[dict] = []
    if not _foreclosure_pdf_enabled():
        logger.info(
            "Foreclosure-PDF source SKIPPED - dallascounty.org PDF feed "
            "deprecated 2026-02-24. Fresh foreclosure notices now live on "
            "publicsearch.us. Set FORECLOSURE_PDF_ENABLED=true to re-enable."
        )
    else:
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

    # 3.5. publicsearch.us Foreclosures department (replaces deprecated PDF feed)
    # Replaces the dallascounty.org foreclosure-PDF source (Stage 2, default-off
    # since 2026-02-24). Notice-of-Foreclosure filings now live on publicsearch.us
    # under the Foreclosures department. Each record is an upcoming trustee's
    # sale -- the highest-motivation seller in the market.
    logger.info("[3.5/12] publicsearch.us foreclosures scrape")
    foreclosure_ps_canonical: list[dict] = []
    if not _publicsearch_enabled():
        logger.info("foreclosures scrape SKIPPED - PUBLICSEARCH_ENABLED not set.")
    else:
        try:
            recorded_lookback = int(os.getenv("FORECLOSURE_RECORDED_LOOKBACK_DAYS", "7"))
            sale_lookahead    = int(os.getenv("FORECLOSURE_SALE_LOOKAHEAD_DAYS",    "180"))
            fps_records = foreclosures_ps.scrape_foreclosures(
                recorded_lookback_days=recorded_lookback,
                sale_lookahead_days=sale_lookahead,
            )
            foreclosure_ps_canonical = [
                enrichment.canonicalize_foreclosure_notice_ps(r) for r in fps_records
            ]
            logger.info(
                "publicsearch foreclosures: %d records fetched",
                len(foreclosure_ps_canonical),
            )
        except publicsearch.CircuitBreakerTripped as exc:
            logger.warning(
                "publicsearch foreclosures circuit breaker tripped: %s - continuing",
                exc,
            )
            monitor.notify_failure(
                error="publicsearch foreclosures circuit breaker tripped (non-fatal)",
                context={"stage": "foreclosures_ps", "exception": str(exc)},
            )
        except Exception as exc:
            logger.warning(
                "publicsearch foreclosures scrape failed: %s - continuing",
                exc,
            )
            monitor.notify_failure(
                error="publicsearch foreclosures scrape failed (non-fatal)",
                context={
                    "stage": "foreclosures_ps",
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )

    # 3.6. OCR enrichment of foreclosure notices ----------------------------
    # Pulls owner name, full property address, loan amount, legal description
    # from the PNG document scans on publicsearch.us. ~6-10s per record.
    # Gated by FORECLOSURE_OCR_ENABLED (default False); requires Tesseract.
    # HOA-assessment-lien records get active=False so they appear in
    # records.json for audit but vanish from the CSV.
    if foreclosure_ps_canonical and _foreclosure_ocr_enabled():
        logger.info("[3.6/12] OCR enrichment of %d foreclosure records",
                    len(foreclosure_ps_canonical))
        try:
            foreclosure_ps_canonical, ocr_stats = foreclosure_ocr.enrich_foreclosure_records(
                foreclosure_ps_canonical
            )
            logger.info(
                "OCR: %d/%d captured, grantor=%d sale=%d addr=%d amount=%d legal=%d "
                "(hoa_suppressed=%d, past_sale_skipped=%d, no_images=%d, errors=%d)",
                ocr_stats.captured_ok, ocr_stats.total,
                ocr_stats.grantor_extracted, ocr_stats.sale_date_extracted,
                ocr_stats.address_extracted, ocr_stats.loan_amount_extracted,
                ocr_stats.legal_desc_extracted,
                ocr_stats.hoa_lien_suppressed, ocr_stats.skipped_past_sale_date,
                ocr_stats.no_images, ocr_stats.capture_errors,
            )
        except Exception as exc:
            logger.warning(
                "OCR enrichment failed: %s - continuing with un-enriched records", exc,
            )
            monitor.notify_failure(
                error="OCR enrichment failed (non-fatal)",
                context={
                    "stage": "foreclosure_ocr",
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )
    elif foreclosure_ps_canonical:
        logger.info(
            "[3.6/12] OCR enrichment SKIPPED - set FORECLOSURE_OCR_ENABLED=true to enable. "
            "Records will have list-view fields only (no owner name, no full address)."
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
                enrichment.canonicalize_probate(
                    r,
                    owner_index=owner_index,
                    account_address_index=account_address_index,
                    mailing_index=mailing_index,
                )
                for r in probate_records
            ]
            n_dec_matched = sum(
                1 for r in probate_records_canonical
                if r["signal_metadata"].get("decedent_owned_properties")
            )
            n_app_matched = sum(
                1 for r in probate_records_canonical
                if r["signal_metadata"].get("applicant_mailing")
            )
            total = max(len(probate_records_canonical), 1)
            logger.info(
                "Probate: %d records fetched | %d decedent->DCAD (%d%%) | %d applicant->mailing high-conf (%d%%)",
                len(probate_records_canonical),
                n_dec_matched, 100 * n_dec_matched // total,
                n_app_matched, 100 * n_app_matched // total,
            )
        except Exception as exc:
            logger.warning("Probate stage failed: %s - continuing without probate", exc)
            monitor.notify_failure(
                error="Probate stage failed (non-fatal)",
                context={"exception": str(exc)},
            )

    # 5. Merge sources -------------------------------------------------------
    logger.info(
        "[5/12] Merging %d publicsearch + %d foreclosure-PS + %d PDF + %d probate records",
        len(ps_records_canonical),
        len(foreclosure_ps_canonical),
        len(pdf_records_canonical),
        len(probate_records_canonical),
    )
    all_records = (
        ps_records_canonical
        + foreclosure_ps_canonical
        + pdf_records_canonical
        + probate_records_canonical
    )

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

    # PR 5: every resolution path runs in `always_run=True` (diagnostic
    # mode) so Stage 6.6 can audit cross-path agreement. Each path
    # records what it WOULD pick to signal_metadata.resolution_history,
    # but only the first path to reach a record stamps dcad_account;
    # subsequent paths preserve the upstream stamp and just log.

    # 6.3 Path B — raw_excerpt clean-address fallback ------------------------
    # Records sometimes have a corrupted / city-only / venue-trustee value in
    # address_normalized but contain the correct property address inside
    # raw_excerpt (Class 24 + Class 1 + Class 25 per docs/FORENSIC_AUDIT_*).
    # Path B retries the address-index lookup with whatever clean street
    # address is in raw_excerpt, guarded against known venue/trustee signatures.
    # See docs/RESOLUTION_PATHS_DESIGN.md §4.
    logger.info("[6.3/12] Path B: raw_excerpt clean-address fallback")
    path_b_stats = resolution_paths.run_path_b(
        all_records, address_index, always_run=True,
    )
    resolution_paths.log_path_b_summary(path_b_stats)

    # 6.35 Variant Lookup (Class 26 family) ----------------------------------
    # DCAD's web search supports forgiving partial matching that our strict
    # bulk address_index doesn't. This stage performs a fuzzy lookup keyed
    # by street number for records where address_normalized looks clean
    # but Stage 7 + Path B both missed (e.g. '4314 HAMILTON' vs DCAD's
    # '4314 HAMILTON AVE'; '2828 S LAKEVIEW DR' vs DCAD's '2828 LAKE VIEW DR').
    # See scraper/address_variants.py for matching strategy.
    logger.info("[6.35/12] Variant lookup (Class 26 family)")
    fuzzy_index = address_variants.build_fuzzy_index(address_index)
    variant_stats = resolution_paths.run_variant_lookup(
        all_records, address_index, fuzzy_index, always_run=True,
    )
    resolution_paths.log_variant_lookup_summary(variant_stats)

    # 6.4 APN -> DCAD account resolution -------------------------------------
    # ServiceLink-format NOFs carry APN (DCAD account_num with leading zeros)
    # on page 1. OCR extracts it into signal_metadata.ocr.apn; this stage
    # maps APN -> account -> normalized address. Runs BEFORE the legal-desc
    # resolver because APN is the highest-confidence identifier when present.
    logger.info("[6.4/12] APN -> DCAD address resolution")
    apn_stats = legal_resolver.resolve_apn_to_address(
        all_records, dcad_tables, always_run=True,
    )
    logger.info(
        "APN resolver: %d/%d resolved (%.1f%%); no_apn=%d no_match=%d",
        apn_stats.resolved, apn_stats.total, apn_stats.resolution_rate * 100,
        apn_stats.no_apn, apn_stats.no_match,
    )

    # 6.45 Path A — NOF grantor → DCAD owner_index ---------------------------
    # NOF records whose grantor (the foreclosed homeowner) is extracted by
    # OCR but whose address didn't resolve via Stage 7 / Path B / Variant /
    # APN can often be matched directly via the grantor name. Same machinery
    # as PB record decedent fan-out; tier ladder + class 19a guard prevent
    # the wrong-person-called failure mode. See docs/RESOLUTION_PATHS_DESIGN §5.
    logger.info("[6.45/12] Path A: NOF grantor -> DCAD owner_index")
    path_a_account_owner_index = dcad_owner_index.build_account_to_owner_name(dcad_tables)
    path_a_account_address_index = legal_resolver._build_account_to_address(dcad_tables)
    path_a_stats = resolution_paths.run_path_a(
        all_records,
        owner_index,
        path_a_account_address_index,
        path_a_account_owner_index,
        always_run=True,
    )
    resolution_paths.log_path_a_summary(path_a_stats)

    # 6.5 Legal-description -> DCAD address resolution -----------------------
    # Publicsearch records lack a street address at canonicalization time;
    # they carry the property's legal description in raw_excerpt. This stage
    # parses that description, looks it up in DCAD's ACCOUNT_INFO via
    # subdivision/lot/block, and stamps address_normalized on matched records.
    # Stage 7's address-based DCAD enrichment then picks them up. Skips
    # records that already have an address (foreclosure-PDF source).
    logger.info("[6.5/12] Legal-description -> DCAD address resolution")
    legal_stats = legal_resolver.resolve_legal_descriptions(
        all_records, dcad_tables, always_run=True,
    )
    logger.info(
        "Legal-description resolver: %d/%d resolved (%.1f%%); "
        "no_parse=%d no_match=%d multi=%d no_snippet=%d",
        legal_stats.resolved, legal_stats.total, legal_stats.resolution_rate * 100,
        legal_stats.no_parse, legal_stats.no_match, legal_stats.multi_match,
        legal_stats.no_snippet,
    )

    # 6.6 Cross-path agreement audit + page tiebreaker -----------------------
    # All paths above ran in always_run mode, recording their would-be
    # picks to signal_metadata.resolution_history without overwriting the
    # first-stamping path's choice. Stage 6.6 audits that history:
    #   - Agreement (2+ paths picked the same account): WARN_PATH_AGREEMENT
    #   - Disagreement (paths picked different accounts): fetches the
    #     publicsearch.us /doc/ Summary panel's Property Address text and
    #     uses it as referee via token-overlap scoring. Winner becomes
    #     primary; losers move to alternate_accounts.
    # See docs/RESOLUTION_PATHS_DESIGN.md §7 + scraper/stage_6_6_agreement.py.
    logger.info("[6.6/12] Stage 6.6: cross-path agreement + page tiebreaker")
    with page_fetcher.PageFetcher() as fetcher:
        stage_6_6_stats = stage_6_6_agreement.run_stage_6_6(
            all_records,
            path_a_account_address_index,   # reuse the acct -> addr map
            page_fetcher=fetcher,
        )
    stage_6_6_agreement.log_stage_6_6_summary(stage_6_6_stats)

    # 7. Enrich --------------------------------------------------------------
    logger.info("[7/12] Enriching with DCAD")
    all_records, enrich_stats = enrichment.enrich_batch(
        all_records, dcad_tables, address_index,
    )

    # 7.5 Grantor fallback from dcad_owner (PR 7.6) --------------------------
    grantor_fallback_count = apply_grantor_fallback_from_dcad_owner(all_records)
    logger.info(
        "Grantor fallback from dcad_owner: filled %d/%d records",
        grantor_fallback_count, len(all_records),
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
            "publicsearch_enabled":   _publicsearch_enabled(),
            "foreclosure_pdf_enabled": _foreclosure_pdf_enabled(),
            "foreclosure_ocr_enabled": _foreclosure_ocr_enabled(),
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
