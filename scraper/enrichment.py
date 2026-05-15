"""
Canonicalization and DCAD enrichment for County Clerk records.

Source modules each produce their own record type:
  - publicsearch.PublicSearchRecord
  - foreclosure_pdfs.ForeclosureRecord

This module converts them into a single canonical dict shape (Sec E.2) and
joins each record to its DCAD parcel via the address index built by
``dcad_bulk.build_address_index``.

Sec F.3.1 hit-rate target: >=85% on first run, >=95% within 3 months. The
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


# Canonical record is just a plain dict. Field set documented in Sec E.2.
CanonicalRecord = dict[str, Any]


def _clean_owner(raw: str) -> str:
    """Strip whitespace and trailing '&' separators from DCAD owner fields.

    DCAD uses a trailing '&' on OWNER_NAME1 to signal that ownership continues
    in OWNER_NAME2 or MULTI_OWNER. When NAME2 happens to be empty, the dangling
    '&' would otherwise leak into the displayed name.
    """
    return raw.strip().rstrip("&").strip()


# ============================================================================
# Canonicalization
# ============================================================================

def canonicalize_publicsearch(rec: PublicSearchRecord) -> CanonicalRecord:
    """Convert a PublicSearchRecord into the canonical dict shape (Sec E.2)."""
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
        "dcad_over65":        None,
        "dcad_disabled":      None,
        "dcad_tax_deferred":  None,
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
    """Convert a ForeclosureRecord into the canonical dict shape (Sec E.2).

    Foreclosure PDFs map to the ``NOTICE`` category (Notice of Foreclosure)
    with literal Dallas code ``NOF`` - see Sec A.3.

    2026-05-15: ``filing_date`` is now sourced from ``rec.notice_date_iso``
    (the real clerk-filed notice date extracted from the PDF text) rather
    than ``rec.sale_date_iso`` (the auction date, which was a misleading
    proxy). When the parser can't extract a real notice date, filing_date
    is None - the strict buy-box filter excludes such records.
    """
    norm_addr = (
        normalize.normalize_address(rec.property_address) if rec.property_address else None
    )
    # Synthesize a record_id from the PDF source + sale date + address.
    # Intentionally still uses sale_date_iso to preserve record_id stability
    # across runs for already-tracked records.
    synthetic_id = _synthesize_pdf_record_id(rec)
    return {
        "record_id":          synthetic_id,
        "source":             "foreclosure_pdf",
        "dallas_code":        "NOF",
        "category":           "NOTICE",
        "filing_date":        rec.notice_date_iso,         # real notice filing date (None if not extractable)
        "instrument_num":     None,                         # not in PDF
        "grantor":            rec.trustee,
        "grantee":            rec.debtor,
        "address":            rec.property_address,
        "address_normalized": norm_addr or None,
        "dcad_account":       None,
        "dcad_owner":         None,
        "dcad_market_value":  None,
        "dcad_homestead":     None,
        "dcad_over65":        None,
        "dcad_disabled":      None,
        "dcad_tax_deferred":  None,
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
    """Deterministic synthetic ID for foreclosure-PDF records (no native ID).

    Preserves stability across runs even after the filing_date source
    changed: the seed still uses sale_date_iso so existing records keep
    the same record_id and the dedup/first_seen logic continues to work.
    """
    import hashlib
    seed = "|".join([
        rec.source_pdf or "",
        rec.sale_date_iso or rec.sale_date or "",
        (rec.property_address or "").upper().strip(),
        (rec.debtor or "").upper().strip(),
    ])
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"pdf-{h}"


# ============================================================================
# DCAD enrichment
# ============================================================================

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
    resolve the DCAD account number, then pulls owner / value / exemption
    fields from the verified DCAD tables (see docs/DCAD_SCHEMA.md).

    Adds these fields when matched:
      - ``dcad_account``       - DCAD account number
      - ``dcad_owner``         - primary owner (OWNER_NAME1 from ACCOUNT_INFO,
                                  joined with OWNER_NAME2 if both present)
      - ``dcad_market_value``  - TOT_VAL from ACCOUNT_APPRL_YEAR for target year
      - ``dcad_homestead``     - True if HOMESTEAD_EFF_DT populated
      - ``dcad_over65``        - True if OVER65_DESC present (distressed signal)
      - ``dcad_disabled``      - True if DISABLED_DESC present (distressed signal)
      - ``dcad_tax_deferred``  - True if TAX_DEFERRED_DESC present (strong signal)

    Returns the same record dict (mutated).
    """
    norm = record.get("address_normalized")
    if not norm:
        return record

    account_num = address_index.get(norm)
    if not account_num:
        return record

    record["dcad_account"] = account_num

    # Primary owner - from ACCOUNT_INFO.OWNER_NAME1 (and NAME2 if joint).
    # ACCOUNT_INFO covers all 858k+ parcels; MULTI_OWNER (107k rows) is
    # only for accounts with 3+ owners or fractional ownership.
    #
    # DCAD convention: a trailing "&" on NAME1 signals "joint owner continued
    # in NAME2 / MULTI_OWNER". Strip it so the displayed name doesn't dangle.
    if "ACCOUNT_INFO" in dcad_tables:
        df = dcad_tables["ACCOUNT_INFO"]
        if "ACCOUNT_NUM" in df.columns and "OWNER_NAME1" in df.columns:
            rows = df[df["ACCOUNT_NUM"] == account_num]
            if not rows.empty:
                name1 = _clean_owner(str(rows.iloc[0].get("OWNER_NAME1", "")))
                name2 = _clean_owner(str(rows.iloc[0].get("OWNER_NAME2", "")))
                if name1 and name2:
                    record["dcad_owner"] = f"{name1} & {name2}"
                elif name1:
                    record["dcad_owner"] = name1

    # Fall back to MULTI_OWNER only if ACCOUNT_INFO didn't yield a name.
    if not record.get("dcad_owner") and "MULTI_OWNER" in dcad_tables:
        df = dcad_tables["MULTI_OWNER"]
        if "ACCOUNT_NUM" in df.columns and "OWNER_NAME" in df.columns:
            rows = df[df["ACCOUNT_NUM"] == account_num]
            if not rows.empty:
                name = str(rows.iloc[0]["OWNER_NAME"]).strip()
                record["dcad_owner"] = name or None

    # Total appraised value (TOT_VAL = IMPR_VAL + LAND_VAL) for target year.
    if "ACCOUNT_APPRL_YEAR" in dcad_tables:
        df = dcad_tables["ACCOUNT_APPRL_YEAR"]
        if "ACCOUNT_NUM" in df.columns and "TOT_VAL" in df.columns:
            year_col = "APPRAISAL_YR" if "APPRAISAL_YR" in df.columns else None
            rows = df[df["ACCOUNT_NUM"] == account_num]
            if year_col:
                rows = rows[rows[year_col] == str(config.DCAD_TARGET_YEAR)]
            if not rows.empty:
                raw_val = str(rows.iloc[0]["TOT_VAL"]).replace(",", "").strip()
                try:
                    record["dcad_market_value"] = float(raw_val) if raw_val else None
                except ValueError:
                    record["dcad_market_value"] = None

    # Homestead + distressed-seller signals from APPLIED_STD_EXEMPT.
    #
    # DCAD sentinel convention (verified against DCAD2025_CURRENT):
    #   HOMESTEAD_EFF_DT:   date like "01/01/2022" = active; "UNASSIGNED"/"" = none
    #   OVER65_DESC:        "OVER 65" or "SURVIVING SPOUSE" = active; "UNASSIGNED"/"" = none
    #   DISABLED_DESC:      "DISABLED" = active; "UNASSIGNED"/"" = none
    #   TAX_DEFERRED_DESC:  "PERMANENT" = active; "UNASSIGNED"/"" = none
    #
    # An account may have multiple rows (one per OWNER_SEQ_NUM). If any owner
    # has the exemption, the property gets the flag.
    if "APPLIED_STD_EXEMPT" in dcad_tables:
        df = dcad_tables["APPLIED_STD_EXEMPT"]
        if "ACCOUNT_NUM" in df.columns:
            rows = df[df["ACCOUNT_NUM"] == account_num]
            if not rows.empty:
                record["dcad_homestead"]     = _any_active(rows, "HOMESTEAD_EFF_DT")
                record["dcad_over65"]        = _any_active(rows, "OVER65_DESC")
                record["dcad_disabled"]      = _any_active(rows, "DISABLED_DESC")
                record["dcad_tax_deferred"]  = _any_active(rows, "TAX_DEFERRED_DESC")
            else:
                # Account matched but no exemption row -> all four are confirmed False.
                record["dcad_homestead"]    = False
                record["dcad_over65"]       = False
                record["dcad_disabled"]     = False
                record["dcad_tax_deferred"] = False

    return record


# DCAD sentinel values meaning "this exemption does not apply".
_EXEMPTION_FALSY_SENTINELS = frozenset({"", "UNASSIGNED", "NAN", "NONE"})


def _any_active(rows: pd.DataFrame, col: str) -> bool:
    """True if any row in ``rows`` has a truthy (non-sentinel) value in ``col``.

    Handles DCAD's "UNASSIGNED" sentinel - distinct from empty string but
    semantically equivalent to "no, this exemption is not active".
    """
    if col not in rows.columns:
        return False
    for v in rows[col]:
        cleaned = str(v).strip().upper()
        if cleaned and cleaned not in _EXEMPTION_FALSY_SENTINELS:
            return True
    return False


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
