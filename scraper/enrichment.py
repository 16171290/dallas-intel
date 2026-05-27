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
from typing import Any, Mapping, Optional

import pandas as pd

from . import config, normalize
from .probate_matcher import match_decedent_to_dcad
from .resolution import (
    PATH_PB_APPLICANT_OWNER_INDEX,
    PATH_PB_DECEDENT_OWNER_INDEX,
    STATUS_MATCHED,
    STATUS_MULTI_MATCH,
    STATUS_NO_MATCH,
    TIER_SURNAME_ONLY,
    WARN_COMMON_NAME_POLLUTION,
    WARN_MULTI_ACCOUNT,
    WARN_SURNAME_ONLY_TIER,
    ResolutionHistoryEntry,
    add_warning,
    append_history,
    set_alternate_accounts,
)


# Stricter criteria for the applicant (heir) DCAD match than the decedent
# match. The probe (probe_probate_applicant_dcad.py) showed that loose
# tiers produce ~14% FP rate on applicants vs essentially none on
# decedents — applicants tend to be younger with common surnames where
# initial_form / surname_only over-match. For the applicant mailing
# field we'd rather show nothing than show a wrong address.
_APPLICANT_MAILING_ACCEPT_TIERS = ("exact", "no_middle")
_APPLICANT_MAILING_MAX_ACCOUNTS = 2
from .foreclosure_pdfs import ForeclosureRecord
from .foreclosures_ps import ForeclosureNoticePSRecord
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
    # Doc-detail URL template verified 2026-05-21: clicking a row in the
    # publicsearch.us SPA navigates to /doc/{record_id}.
    source_url = (
        f"{config.PUBLICSEARCH_BASE}/doc/{rec.record_id}"
        if rec.record_id else None
    )
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
        "dcad_mailing_address": None,
        "dcad_mailing_city":    None,
        "dcad_mailing_state":   None,
        "dcad_mailing_zip":     None,
        "amount":             rec.amount,
        "trustee":            None,
        "sale_date":          None,
        "raw_excerpt":        rec.raw_html_snippet[:500] if rec.raw_html_snippet else None,
        "active":             True,
                "release_record_id":  None,
        "signal_metadata":    None,
        "address_city":       None,
        "address_state":      None,
        "address_zip":        None,
        "source_url":         source_url,
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
        "dcad_mailing_address": None,
        "dcad_mailing_city":    None,
        "dcad_mailing_state":   None,
        "dcad_mailing_zip":     None,
        "amount":             rec.original_loan_amount,
        "trustee":            rec.trustee,
        "sale_date":          rec.sale_date_iso,
        "raw_excerpt":        rec.raw_excerpt[:500] if rec.raw_excerpt else None,
        "active":             True,
                "release_record_id":  None,
        "signal_metadata":    None,
        "address_city":       None,
        "address_state":      None,
        "address_zip":        None,
        "source_url":         rec.source_pdf_url,
        "score":              0,
        "score_breakdown":    {},
        "parse_warnings":     list(rec.parse_warnings),
    }


def canonicalize_foreclosure_notice_ps(rec: ForeclosureNoticePSRecord) -> CanonicalRecord:
    """Convert a ForeclosureNoticePSRecord (publicsearch.us Foreclosures
    department) into the canonical dict shape.

    These records are the motivated-seller core: a recorded notice of an
    upcoming trustee's sale. `filing_date` is the recorded date (when the
    notice was filed with the County Clerk). `sale_date` is the auction
    date, populated from the search results (unlike Real-Property
    publicsearch records where sale_date is None).

    Source-URL template verified 2026-05-21: clicking a row in the SPA
    navigates to /doc/{record_id}, same as Real-Property records.
    """
    norm_addr = (
        normalize.normalize_address(rec.property_address) if rec.property_address else None
    )
    source_url = (
        f"{config.PUBLICSEARCH_BASE}/doc/{rec.record_id}"
        if rec.record_id else None
    )
    return {
        "record_id":          rec.record_id,
        "source":             "publicsearch.us",
        "dallas_code":        "NOF",
        "category":           "NOTICE",
        "filing_date":        rec.recorded_date,
        "instrument_num":     rec.doc_number,
        "grantor":            None,   # not in list view
        "grantee":            None,   # not in list view
        "address":            rec.property_address,
        "address_normalized": norm_addr or None,
        "dcad_account":       None,
        "dcad_owner":         None,
        "dcad_market_value":  None,
        "dcad_homestead":     None,
        "dcad_over65":        None,
        "dcad_disabled":      None,
        "dcad_tax_deferred":  None,
        "dcad_mailing_address": None,
        "dcad_mailing_city":    None,
        "dcad_mailing_state":   None,
        "dcad_mailing_zip":     None,
        "amount":             None,
        "trustee":            None,
        "sale_date":          rec.sale_date,
        "raw_excerpt":        rec.raw_html_snippet[:500] if rec.raw_html_snippet else None,
        "active":             True,
        "release_record_id":  None,
        "signal_metadata":    None,
        "address_city":       None,
        "address_state":      None,
        "address_zip":        None,
        "source_url":         source_url,
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
                acct_row = rows.iloc[0]
                name1 = _clean_owner(str(acct_row.get("OWNER_NAME1", "")))
                name2 = _clean_owner(str(acct_row.get("OWNER_NAME2", "")))
                if name1 and name2:
                    record["dcad_owner"] = f"{name1} & {name2}"
                elif name1:
                    record["dcad_owner"] = name1

                # Mailing address — OWNER_ADDRESS_LINE1..4 + OWNER_CITY/STATE/ZIPCODE
                # per docs/DCAD_SCHEMA.md. Stronger motivated-seller signal when it
                # differs from the property address (out-of-area landlord).
                mailing_lines = [
                    str(acct_row.get(c, "") or "").strip()
                    for c in ("OWNER_ADDRESS_LINE1", "OWNER_ADDRESS_LINE2",
                              "OWNER_ADDRESS_LINE3", "OWNER_ADDRESS_LINE4")
                ]
                mailing_street = "\n".join(l for l in mailing_lines if l) or None
                record["dcad_mailing_address"] = mailing_street
                record["dcad_mailing_city"]  = str(acct_row.get("OWNER_CITY",   "") or "").strip() or None
                record["dcad_mailing_state"] = str(acct_row.get("OWNER_STATE",  "") or "").strip() or None
                record["dcad_mailing_zip"]   = str(acct_row.get("OWNER_ZIPCODE","") or "").strip() or None

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



def canonicalize_probate(
    rec: "ProbateRecord",
    *,
    owner_index: Optional[Mapping[str, list[str]]] = None,
    account_address_index: Optional[Mapping[str, dict]] = None,
    mailing_index: Optional[Mapping[str, dict]] = None,
) -> CanonicalRecord:
    """Convert a ProbateRecord into the canonical dict shape (Sec E.2).

    Probate cases map to the ``PROB`` category with literal Dallas code ``PB``
    (see Sec A.3). Per Phase 3 Option A decisions:
      - ``grantor`` is the decedent (the party from whom the estate flows)
      - ``grantee`` is the applicant (the party petitioning for letters)

    DCAD fan-out: probate filings themselves carry no property address.
    To surface the decedent's owned Dallas property as a motivated-seller
    lead, we look up the decedent_name in DCAD's owner_index via
    ``probate_matcher.match_decedent_to_dcad``. When a match is found:
      - ``dcad_account`` is stamped to the FIRST matched account (PR 6).
        The downstream ``enrich_record`` then fills ``dcad_owner`` /
        market value / exemptions from that account.
      - ``address_normalized`` is set to the FIRST matched property's
        normalized address.
      - ``signal_metadata.alternate_accounts`` carries any additional
        accounts the decedent owns (PR 6).
      - ``signal_metadata.decedent_owned_properties`` (legacy, retained
        for backward compat) carries the FULL list with address details
        per parcel.
      - ``signal_metadata.resolution_history`` carries a
        ``PATH_PB_DECEDENT_OWNER_INDEX`` entry so Stage 6.6 can audit it
        and the operator sees provenance in the dashboard (PR 6).
      - ``signal_metadata.confidence_warnings`` includes:
          * ``WARN_MULTI_ACCOUNT`` when the decedent owns 2+ properties
          * ``WARN_SURNAME_ONLY_TIER`` when the match was on surname alone
          * ``WARN_COMMON_NAME_POLLUTION`` when 4+ accounts matched
        (PR 6).
      - ``signal_metadata.dcad_match_tier`` / ``dcad_match_warning``
        retained for backward compatibility.

    Applicant fan-out: stricter criteria for mailing-address resolution.
    Only exact/no_middle tiers, no common-name-pollution warning, <= 2
    accounts. PR 6 adds a ``PATH_PB_APPLICANT_OWNER_INDEX`` history entry
    so applicant provenance is visible alongside decedent.

    When ``owner_index`` is not provided (e.g. unit tests, or backwards
    compatibility) the function falls back to address=None and no
    fan-out happens. No regression vs the pre-fan-out behavior.

    Probate-specific context (judge, attorneys, case_type, case_status,
    jurisdiction, additional_applicants) is preserved under
    ``signal_metadata`` rather than top-level keys, keeping the canonical
    schema stable across sources.
    """
    # Strip Tyler ISO timestamp to YYYY-MM-DD to match the date-only convention
    # used by publicsearch. Tyler returns dates as either "2026-05-14T17:00:00"
    # or "2026-05-14T17:00:00+00:00"; slicing the first 10 chars yields the date
    # portion deterministically.
    filing_date = rec.date_filed[:10] if rec.date_filed else None

    # DCAD fan-out — find properties owned by the decedent
    decedent_properties: list[dict] = []
    match_tier: Optional[str] = None
    match_warning: Optional[str] = None
    primary_address_normalized: Optional[str] = None
    primary_dcad_account: Optional[str] = None
    decedent_alternates: list[str] = []
    decedent_history_warnings: list[str] = []
    decedent_match = None

    if owner_index and rec.decedent_name:
        match = match_decedent_to_dcad(rec.decedent_name, owner_index)
        if match:
            decedent_match = match
            match_tier = match.tier
            match_warning = match.warning
            for acct in match.accounts:
                addr_info = (account_address_index or {}).get(acct, {})
                decedent_properties.append({
                    "account_num":        acct,
                    "address_normalized": addr_info.get("address_normalized"),
                    "address_city":       addr_info.get("address_city"),
                    "address_state":      addr_info.get("address_state"),
                    "address_zip":        addr_info.get("address_zip"),
                })
            if decedent_properties:
                # PR 6: stamp dcad_account directly (consistent with Paths A/C)
                primary_dcad_account = match.accounts[0]
                primary_address_normalized = decedent_properties[0]["address_normalized"]
                decedent_alternates = list(match.accounts[1:])

            # Build the warnings list that will be attached BOTH to the
            # history entry AND to record-level confidence_warnings.
            if len(match.accounts) > 1:
                decedent_history_warnings.append(WARN_MULTI_ACCOUNT)
            if match.tier == TIER_SURNAME_ONLY:
                decedent_history_warnings.append(WARN_SURNAME_ONLY_TIER)
            if match.warning == "common_name_pollution":
                decedent_history_warnings.append(WARN_COMMON_NAME_POLLUTION)

    # Applicant fan-out — find a mailing address for the heir/petitioner.
    # STRICTER criteria than decedent fan-out: only exact/no_middle tiers,
    # no common-name-pollution warning, and <= 2 accounts. Why stricter:
    # applicant hit rate is lower (~48%) and looser tiers produce too many
    # FPs on common names (Sylvia Garcia matching one of 10 different
    # Sylvia Garcias would give a junk mailing address). We'd rather
    # leave the field blank than mislead the operator.
    applicant_mailing: Optional[dict] = None
    applicant_match = None
    applicant_accepted = False
    if owner_index and rec.applicant_name and mailing_index:
        amatch = match_decedent_to_dcad(rec.applicant_name, owner_index)
        applicant_match = amatch
        if (
            amatch is not None
            and amatch.tier in _APPLICANT_MAILING_ACCEPT_TIERS
            and amatch.warning is None
            and len(amatch.accounts) <= _APPLICANT_MAILING_MAX_ACCOUNTS
        ):
            primary_acct = amatch.accounts[0]
            mail = mailing_index.get(primary_acct, {})
            if mail.get("address"):
                applicant_mailing = {
                    "tier":         amatch.tier,
                    "matched_name": amatch.matched_name,
                    "account_num":  primary_acct,
                    "address":      mail.get("address"),
                    "city":         mail.get("city"),
                    "state":        mail.get("state"),
                    "zip":          mail.get("zip"),
                }
                applicant_accepted = True

    record = {
        "record_id":          f"pro-{rec.case_data_id}",
        "source":             "probate.txcourts.gov",
        "dallas_code":        "PB",
        "category":           "PROB",
        "filing_date":        filing_date,
        "instrument_num":     rec.case_number,
        "grantor":            rec.decedent_name,
        "grantee":            rec.applicant_name,
        "address":            None,
        "address_normalized": primary_address_normalized,
        "dcad_account":       primary_dcad_account,
        "dcad_owner":         None,
        "dcad_market_value":  None,
        "dcad_homestead":     None,
        "dcad_over65":        None,
        "dcad_disabled":      None,
        "dcad_tax_deferred":  None,
        "dcad_mailing_address": None,
        "dcad_mailing_city":    None,
        "dcad_mailing_state":   None,
        "dcad_mailing_zip":     None,
        "amount":             None,
        "trustee":            None,
        "sale_date":          None,
        "raw_excerpt":        None,
        "active":             True,
        "release_record_id":  None,
        "signal_metadata":    {
            "case_type":                 rec.case_type,
            "case_status":               rec.case_status,
            "judge":                     rec.judge,
            "attorneys":                 list(rec.attorneys),
            "jurisdiction":              rec.jurisdiction,
            "additional_applicants":     list(rec.additional_applicants),
            "decedent_owned_properties": decedent_properties,
            "dcad_match_tier":           match_tier,
            "dcad_match_warning":        match_warning,
            "applicant_mailing":         applicant_mailing,
        },
        "address_city":       None,
        "address_state":      None,
        "address_zip":        None,
        "source_url":         None,
        "score":              0,
        "score_breakdown":    {},
        "parse_warnings":     list(rec.parse_warnings),
    }

    # ─── PR 6 retrofit: provenance via resolution_history + warnings ───
    # Write a decedent-match history entry (matched / no_match) AND record
    # alternate_accounts + canonical confidence_warnings via the helpers
    # from scraper.resolution. Stage 6.6 reads these the same way it reads
    # Path A / B / C output.
    if owner_index and rec.decedent_name:
        if decedent_match is not None:
            status = (
                STATUS_MULTI_MATCH if len(decedent_match.accounts) > 1
                else STATUS_MATCHED
            )
            append_history(record, ResolutionHistoryEntry(
                path=PATH_PB_DECEDENT_OWNER_INDEX,
                stage="canonicalize",
                input=rec.decedent_name,
                status=status,
                tier=decedent_match.tier,
                dcad_account=primary_dcad_account,
                alternates=decedent_alternates,
                warnings=decedent_history_warnings,
            ))
            # Mirror history warnings onto record-level confidence_warnings
            for w in decedent_history_warnings:
                add_warning(record, w)
            if decedent_alternates:
                set_alternate_accounts(record, decedent_alternates)
        else:
            append_history(record, ResolutionHistoryEntry(
                path=PATH_PB_DECEDENT_OWNER_INDEX,
                stage="canonicalize",
                input=rec.decedent_name,
                status=STATUS_NO_MATCH,
            ))

    # Applicant match: history entry too, so operator sees applicant_mailing
    # provenance side-by-side with decedent provenance. Note this is
    # mailing-address resolution, NOT primary record resolution.
    if owner_index and rec.applicant_name and mailing_index:
        if applicant_match is not None:
            # Whether or not the strict criteria accepted the match for
            # mailing_address, the match attempt itself is recorded so
            # Stage 6.6 and the operator can see what was considered.
            applicant_warnings: list[str] = []
            if applicant_match.warning == "common_name_pollution":
                applicant_warnings.append(WARN_COMMON_NAME_POLLUTION)
            if applicant_match.tier == TIER_SURNAME_ONLY:
                applicant_warnings.append(WARN_SURNAME_ONLY_TIER)
            if len(applicant_match.accounts) > 1:
                applicant_warnings.append(WARN_MULTI_ACCOUNT)

            status = (
                STATUS_MATCHED if applicant_accepted
                else STATUS_MULTI_MATCH if len(applicant_match.accounts) > 1
                else STATUS_MATCHED
            )
            append_history(record, ResolutionHistoryEntry(
                path=PATH_PB_APPLICANT_OWNER_INDEX,
                stage="canonicalize",
                input=rec.applicant_name,
                status=status,
                tier=applicant_match.tier,
                dcad_account=(applicant_match.accounts[0]
                              if applicant_match.accounts else None),
                alternates=list(applicant_match.accounts[1:]),
                warnings=applicant_warnings,
                skip_reason=(None if applicant_accepted
                             else "applicant_match_too_loose"),
            ))
        else:
            append_history(record, ResolutionHistoryEntry(
                path=PATH_PB_APPLICANT_OWNER_INDEX,
                stage="canonicalize",
                input=rec.applicant_name,
                status=STATUS_NO_MATCH,
            ))

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
