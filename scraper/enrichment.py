"""
Canonicalization and DCAD enrichment for County Clerk records.

Source modules each produce their own record type:
  - publicsearch.PublicSearchRecord
  - foreclosure_pdfs.ForeclosureRecord

This module converts them into a single canonical dict shape (§E.2) and
joins each record to its DCAD parcel via the address index built by
``dcad_bulk.build_address_index``.

§F.3.1 hit-rate target: ≥85% on first run, ≥95% within 3 months. The
:class:`EnrichmentStats` returned by :func:`enrich_batch` surfaces the
current rate so downstream monitoring can alert on drops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from . import config, normalize
from .foreclosure_pdfs import ForeclosureRecord
from .publicsearch import PublicSearchRecord

logger = logging.getLogger(__name__)


# Canonical record is just a plain dict. Field set documented in §E.2.
CanonicalRecord = dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════
# Canonicalization
# ═══════════════════════════════════════════════════════════════════════════

def canonicalize_publicsearch(rec: PublicSearchRecord) -> CanonicalRecord:
    """Convert a PublicSearchRecord into the canonical dict shape (§E.2)."""
    norm_addr = normalize.normalize_address(rec.address) if rec.address else None
    return {
        "record_id":          rec.record_id,
        "source":             "publicsearch.us",
        "dallas_code":        rec.dallas_code,
        "category":           rec.harris_category or normalize.dallas_code_to_category(rec.dallas_code),
        "filing_date":        rec.filing_date,
        "instrument_num":     rec.instrument_num,
        "grantor":            rec.grantor,
        "grantee":            rec.grantee,
        "address":            rec.address,
        "address_normalized": norm_addr or None,
        "dcad_account":       None,
        "dcad_owner":         None,
        "dcad_market_value":  None,
        "dcad_homestead":     None,
        "amount":             rec.amount,
        "trustee":            None,
        "sale_date":          None,
        "raw_excerpt":        rec.raw_html_snippet[:500] if rec.raw_html_snippet else None,
        "active":             True,
        "release_record_id":  None,
        "score":              0,
        "score_breakdown":    {},
        "parse_warnings":     list(rec.parse_warnings),
    }


def canonicalize_foreclosure(rec: ForeclosureRecord) -> CanonicalRecord:
    """Convert a ForeclosureRecord into the canonical dict shape (§E.2).

    Foreclosure PDFs map to the ``NOTICE`` category (Notice of Foreclosure)
    with literal Dallas code ``NOF`` — see §A.3.
    """
    norm_addr = (
        normalize.normalize_address(rec.property_address) if rec.property_address else None
    )
    # Synthesize a record_id from the PDF source + sale date + address.
    synthetic_id = _synthesize_pdf_record_id(rec)
    return {
        "record_id":          synthetic_id,
        "source":             "foreclosure_pdf",
        "dallas_code":        "NOF",
        "category":           "NOTICE",
        "filing_date":        rec.sale_date_iso,           # use sale date as a proxy
        "instrument_num":     None,                         # not in PDF
        "grantor":            rec.trustee,
        "grantee":            rec.debtor,
        "address":            rec.property_address,
        "address_normalized": norm_addr or None,
        "dcad_account":       None,
        "dcad_owner":         None,
        "dcad_market_value":  None,
        "dcad_homestead":     None,
        "amount":             rec.original_loan_amount,
        "trustee":            rec.trustee,
        "sale_date":          rec.sale_date_iso,
        "raw_excerpt":        rec.raw_excerpt[:500] if rec.raw_excerpt else None,
        "active":             True,
        "release_record_id":  None,
        "score":              0,
        "score_breakdown":    {},
        "parse_warnings":     list(rec.parse_warnings),
    }


def _synthesize_pdf_record_id(rec: ForeclosureRecord) -> str:
    """Deterministic synthetic ID for foreclosure-PDF records (no native ID)."""
    import hashlib
    seed = "|".join([
        rec.source_pdf or "",
        rec.sale_date_iso or rec.sale_date or "",
        (rec.property_address or "").upper().strip(),
        (rec.debtor or "").upper().strip(),
    ])
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"pdf-{h}"


# ═══════════════════════════════════════════════════════════════════════════
# DCAD enrichment
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EnrichmentStats:
    """Counts produced by enrich_batch for downstream monitoring."""
    total: int = 0
    matched: int = 0

    @property
    def hit_rate(self) -> float:
        return self.matched / self.total if self.total else 0.0


def enrich_record(
    record: CanonicalRecord,
    dcad_tables: dict[str, pd.DataFrame],
    address_index: dict[str, str],
) -> CanonicalRecord:
    """Add DCAD fields in place to a canonical record.

    Looks up the record's normalized address in ``address_index`` to
    resolve the DCAD account number, then pulls owner / market-value /
    homestead fields from the relevant DCAD tables.

    Returns the same record dict (mutated).
    """
    norm = record.get("address_normalized")
    if not norm:
        return record

    account_num = address_index.get(norm)
    if not account_num:
        return record

    record["dcad_account"] = account_num

    # Owner — first row from MULTI_OWNER for this account.
    if "MULTI_OWNER" in dcad_tables:
        df = dcad_tables["MULTI_OWNER"]
        if "ACCOUNT_NUM" in df.columns and "OWNER_NAME" in df.columns:
            rows = df[df["ACCOUNT_NUM"] == account_num]
            if not rows.empty:
                record["dcad_owner"] = rows.iloc[0]["OWNER_NAME"].strip() or None

    # Market value — current target-year row from ACCOUNT_APPRL_YEAR.
    if "ACCOUNT_APPRL_YEAR" in dcad_tables:
        df = dcad_tables["ACCOUNT_APPRL_YEAR"]
        if "ACCOUNT_NUM" in df.columns and "MARKET_VAL" in df.columns:
            year_col = "APPRAISAL_YR" if "APPRAISAL_YR" in df.columns else None
            rows = df[df["ACCOUNT_NUM"] == account_num]
            if year_col:
                rows = rows[rows[year_col] == str(config.DCAD_TARGET_YEAR)]
            if not rows.empty:
                raw_val = str(rows.iloc[0]["MARKET_VAL"]).replace(",", "").strip()
                try:
                    record["dcad_market_value"] = float(raw_val) if raw_val else None
                except ValueError:
                    record["dcad_market_value"] = None

    # Homestead exemption — presence of any APPLIED_STD_EXEMPT row with a
    # homestead-style code (HS, HOM, etc.).
    if "APPLIED_STD_EXEMPT" in dcad_tables:
        df = dcad_tables["APPLIED_STD_EXEMPT"]
        if "ACCOUNT_NUM" in df.columns and "EXEMPT_CD" in df.columns:
            rows = df[df["ACCOUNT_NUM"] == account_num]
            codes = {str(c).upper().strip() for c in rows["EXEMPT_CD"].tolist()}
            record["dcad_homestead"] = any(
                code.startswith("HS") or code.startswith("HOM")
                for code in codes
            )

    return record


def enrich_batch(
    records: list[CanonicalRecord],
    dcad_tables: dict[str, pd.DataFrame],
    address_index: dict[str, str],
) -> tuple[list[CanonicalRecord], EnrichmentStats]:
    """Apply :func:`enrich_record` to a batch; return (records, stats)."""
    stats = EnrichmentStats(total=len(records))
    for rec in records:
        enrich_record(rec, dcad_tables, address_index)
        if rec.get("dcad_account"):
            stats.matched += 1

    logger.info(
        "Enrichment: %d / %d records matched DCAD (hit-rate %.1f%%)",
        stats.matched, stats.total, stats.hit_rate * 100,
    )
    return records, stats
