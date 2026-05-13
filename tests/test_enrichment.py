"""Regression tests for scraper.enrichment."""

import pandas as pd
import pytest

from scraper import enrichment, foreclosure_pdfs, publicsearch


# ═══════════════════════════════════════════════════════════════════════════
# Canonicalization — publicsearch
# ═══════════════════════════════════════════════════════════════════════════

class TestCanonicalizePublicsearch:
    def test_basic_fields(self):
        rec = publicsearch.PublicSearchRecord(
            record_id="ABC123",
            dallas_code="LP",
            filing_date="2026-05-13",
            instrument_num="2026000123",
            grantor="ABC Bank",
            grantee="John Smith",
            address="100 Main St, Dallas, TX",
        )
        out = enrichment.canonicalize_publicsearch(rec)
        assert out["record_id"]      == "ABC123"
        assert out["source"]         == "publicsearch.us"
        assert out["dallas_code"]    == "LP"
        assert out["category"]       == "L/P"
        assert out["grantor"]        == "ABC Bank"
        assert out["grantee"]        == "John Smith"

    def test_address_normalized(self):
        rec = publicsearch.PublicSearchRecord(
            record_id="X", dallas_code="LP",
            address="100 Main Street, Dallas, TX",
        )
        out = enrichment.canonicalize_publicsearch(rec)
        assert out["address_normalized"] == "100 MAIN ST DALLAS TX"

    def test_missing_address_yields_none(self):
        rec = publicsearch.PublicSearchRecord(record_id="X", dallas_code="LP")
        out = enrichment.canonicalize_publicsearch(rec)
        assert out["address_normalized"] is None

    def test_initial_score_zero(self):
        rec = publicsearch.PublicSearchRecord(record_id="X", dallas_code="LP")
        out = enrichment.canonicalize_publicsearch(rec)
        assert out["score"]            == 0
        assert out["active"]           is True
        assert out["dcad_account"]     is None
        assert out["release_record_id"] is None


# ═══════════════════════════════════════════════════════════════════════════
# Canonicalization — foreclosure PDFs
# ═══════════════════════════════════════════════════════════════════════════

class TestCanonicalizeForeclosure:
    def test_basic(self):
        rec = foreclosure_pdfs.ForeclosureRecord(
            source_pdf="Dallas_1.pdf",
            sale_date="October 7, 2025",
            sale_date_iso="2025-10-07",
            property_address="1004 Seago Drive, Seagoville, TX 75159",
            debtor="John Borrower",
            trustee="ABC Trustee Corp",
            original_loan_amount="245000.00",
        )
        out = enrichment.canonicalize_foreclosure(rec)
        assert out["source"]      == "foreclosure_pdf"
        assert out["dallas_code"] == "NOF"
        assert out["category"]    == "NOTICE"
        assert out["sale_date"]   == "2025-10-07"
        assert out["filing_date"] == "2025-10-07"
        assert out["grantor"]     == "ABC Trustee Corp"
        assert out["grantee"]     == "John Borrower"

    def test_synthetic_id_deterministic(self):
        """Same inputs should produce the same synthetic record ID."""
        rec = foreclosure_pdfs.ForeclosureRecord(
            source_pdf="Dallas_1.pdf",
            sale_date_iso="2025-10-07",
            property_address="1004 Seago Drive",
            debtor="John Borrower",
        )
        a = enrichment.canonicalize_foreclosure(rec)
        b = enrichment.canonicalize_foreclosure(rec)
        assert a["record_id"] == b["record_id"]
        assert a["record_id"].startswith("pdf-")

    def test_synthetic_id_differs_for_different_records(self):
        a_rec = foreclosure_pdfs.ForeclosureRecord(
            source_pdf="Dallas_1.pdf",
            sale_date_iso="2025-10-07",
            property_address="100 Main St",
            debtor="A",
        )
        b_rec = foreclosure_pdfs.ForeclosureRecord(
            source_pdf="Dallas_1.pdf",
            sale_date_iso="2025-10-07",
            property_address="200 Elm Ave",
            debtor="A",
        )
        a = enrichment.canonicalize_foreclosure(a_rec)
        b = enrichment.canonicalize_foreclosure(b_rec)
        assert a["record_id"] != b["record_id"]


# ═══════════════════════════════════════════════════════════════════════════
# DCAD enrichment
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def dcad_fixture():
    """Synthetic DCAD tables and address index."""
    account_info = pd.DataFrame({
        "ACCOUNT_NUM":   ["10001", "10002"],
        "STREET_NUM":    ["100", "200"],
        "STREET_NAME":   ["MAIN", "ELM"],
        "STREET_SUFFIX": ["ST", "AVE"],
    })
    multi_owner = pd.DataFrame({
        "ACCOUNT_NUM":  ["10001", "10002"],
        "OWNER_SEQ_NUM": ["1", "1"],
        "OWNER_NAME":   ["JOHN SMITH", "JANE DOE"],
    })
    appr_year = pd.DataFrame({
        "ACCOUNT_NUM":   ["10001", "10002"],
        "APPRAISAL_YR":  ["2026", "2026"],
        "MARKET_VAL":    ["250000", "75000"],
        "APPRAISED_VAL": ["240000", "72000"],
    })
    exemptions = pd.DataFrame({
        "ACCOUNT_NUM": ["10001"],
        "EXEMPT_CD":   ["HS"],
    })
    tables = {
        "ACCOUNT_INFO":        account_info,
        "MULTI_OWNER":         multi_owner,
        "ACCOUNT_APPRL_YEAR":  appr_year,
        "APPLIED_STD_EXEMPT":  exemptions,
    }
    address_index = {
        "100 MAIN ST": "10001",
        "200 ELM AVE": "10002",
    }
    return tables, address_index


class TestEnrichRecord:
    def test_matched_address_populates_account(self, dcad_fixture):
        tables, idx = dcad_fixture
        rec = {"address_normalized": "100 MAIN ST"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_account"] == "10001"
        assert rec["dcad_owner"]   == "JOHN SMITH"
        assert rec["dcad_market_value"] == 250000.0
        assert rec["dcad_homestead"]    is True

    def test_unmatched_address_leaves_account_none(self, dcad_fixture):
        tables, idx = dcad_fixture
        rec = {"address_normalized": "999 UNKNOWN BLVD"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec.get("dcad_account") is None

    def test_missing_address_does_nothing(self, dcad_fixture):
        tables, idx = dcad_fixture
        rec = {"address_normalized": None}
        enrichment.enrich_record(rec, tables, idx)
        assert rec.get("dcad_account") is None

    def test_no_homestead(self, dcad_fixture):
        tables, idx = dcad_fixture
        rec = {"address_normalized": "200 ELM AVE"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_homestead"] is False


class TestEnrichBatch:
    def test_stats_computed(self, dcad_fixture):
        tables, idx = dcad_fixture
        records = [
            {"address_normalized": "100 MAIN ST"},
            {"address_normalized": "200 ELM AVE"},
            {"address_normalized": "999 UNKNOWN BLVD"},
        ]
        enriched, stats = enrichment.enrich_batch(records, tables, idx)
        assert stats.total   == 3
        assert stats.matched == 2
        assert abs(stats.hit_rate - (2 / 3)) < 1e-9

    def test_empty_input(self, dcad_fixture):
        tables, idx = dcad_fixture
        enriched, stats = enrichment.enrich_batch([], tables, idx)
        assert enriched == []
        assert stats.total    == 0
        assert stats.matched  == 0
        assert stats.hit_rate == 0.0
