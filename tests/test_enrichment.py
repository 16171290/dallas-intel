"""Regression tests for scraper.enrichment."""

import pandas as pd
import pytest

from scraper import enrichment, foreclosure_pdfs, publicsearch


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Canonicalization â€” publicsearch
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
        assert out["address_normalized"] == "100 MAIN ST"

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Canonicalization â€” foreclosure PDFs
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DCAD enrichment
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@pytest.fixture
def dcad_fixture():
    """Synthetic DCAD tables matching the verified schema (2026-05-13)."""
    account_info = pd.DataFrame({
        "ACCOUNT_NUM":      ["10001", "10002"],
        "STREET_NUM":       ["100", "200"],
        "FULL_STREET_NAME": ["MAIN ST", "ELM AVE"],
        "OWNER_NAME1":      ["JOHN SMITH", "JANE DOE"],
        "OWNER_NAME2":      ["", ""],
        "APPRAISAL_YR":     ["2025", "2025"],
    })
    multi_owner = pd.DataFrame({
        "ACCOUNT_NUM":    ["10003"],     # only joint-ownership case
        "OWNER_SEQ_NUM":  ["1"],
        "OWNER_NAME":     ["BOB & ALICE TRUSTEES"],
    })
    appr_year = pd.DataFrame({
        "ACCOUNT_NUM":   ["10001", "10002"],
        "APPRAISAL_YR":  ["2025", "2025"],
        "TOT_VAL":       ["250000", "75000"],
        "IMPR_VAL":      ["200000", "60000"],
        "LAND_VAL":      ["50000",  "15000"],
    })
    # Synthetic exemption fixture using DCAD's real sentinel convention.
    # Account 10001 has homestead + over-65 + disabled + tax-deferred active;
    # account 10002 is in the table but with all UNASSIGNED (homestead-only
    # exemption claimed elsewhere, or pure UNASSIGNED row for some reason).
    exemptions = pd.DataFrame({
        "ACCOUNT_NUM":           ["10001", "10001",      "10002"],
        "APPRAISAL_YR":          ["2025",  "2025",       "2025"],
        "OWNER_SEQ_NUM":         ["1",     "2",          "1"],
        "HOMESTEAD_EFF_DT":      ["01/01/2019", "01/01/2019", "UNASSIGNED"],
        "OVER65_DESC":           ["OVER 65", "UNASSIGNED", "UNASSIGNED"],
        "DISABLED_DESC":         ["DISABLED", "UNASSIGNED", "UNASSIGNED"],
        "TAX_DEFERRED_DESC":     ["PERMANENT", "UNASSIGNED", "UNASSIGNED"],
    })
    tables = {
        "ACCOUNT_INFO":       account_info,
        "MULTI_OWNER":        multi_owner,
        "ACCOUNT_APPRL_YEAR": appr_year,
        "APPLIED_STD_EXEMPT": exemptions,
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
        assert rec["dcad_account"]      == "10001"
        assert rec["dcad_owner"]        == "JOHN SMITH"
        assert rec["dcad_market_value"] == 250000.0
        # Account has two rows; one with active exemptions, one all UNASSIGNED.
        # The 'any active' semantic means all four come back True.
        assert rec["dcad_homestead"]    is True
        assert rec["dcad_over65"]       is True
        assert rec["dcad_disabled"]     is True
        assert rec["dcad_tax_deferred"] is True

    def test_all_unassigned_yields_false(self, dcad_fixture):
        """Account 10002 has exemption row but every column is UNASSIGNED â†’ all False."""
        tables, idx = dcad_fixture
        rec = {"address_normalized": "200 ELM AVE"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_account"]      == "10002"
        assert rec["dcad_homestead"]    is False
        assert rec["dcad_over65"]       is False
        assert rec["dcad_disabled"]     is False
        assert rec["dcad_tax_deferred"] is False

    def test_surviving_spouse_counts_as_over65(self, dcad_fixture):
        """OVER65_DESC == 'SURVIVING SPOUSE' is also an active over-65 status."""
        tables, idx = dcad_fixture
        # Patch the over-65 column for account 10002 to surviving-spouse.
        ex = tables["APPLIED_STD_EXEMPT"]
        ex.loc[ex["ACCOUNT_NUM"] == "10002", "OVER65_DESC"] = "SURVIVING SPOUSE"
        rec = {"address_normalized": "200 ELM AVE"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_over65"] is True

    def test_owner_joins_two_names(self, dcad_fixture):
        """Both OWNER_NAME1 and OWNER_NAME2 â†’ joined with '&'."""
        tables, idx = dcad_fixture
        tables["ACCOUNT_INFO"].loc[0, "OWNER_NAME2"] = "JANE SMITH"
        rec = {"address_normalized": "100 MAIN ST"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_owner"] == "JOHN SMITH & JANE SMITH"

    def test_owner_trailing_ampersand_stripped(self, dcad_fixture):
        """DCAD's trailing '&' on OWNER_NAME1 should be cleaned off when NAME2 is empty."""
        tables, idx = dcad_fixture
        tables["ACCOUNT_INFO"].loc[0, "OWNER_NAME1"] = "VAZUEZ GABINO GARCIA &"
        tables["ACCOUNT_INFO"].loc[0, "OWNER_NAME2"] = ""
        rec = {"address_normalized": "100 MAIN ST"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_owner"] == "VAZUEZ GABINO GARCIA"

    def test_owner_with_trailing_ampersand_and_name2(self, dcad_fixture):
        """If NAME1 has trailing '&' AND NAME2 is present, strip the '&' before joining."""
        tables, idx = dcad_fixture
        tables["ACCOUNT_INFO"].loc[0, "OWNER_NAME1"] = "SANDOVAL MA & INOCENCIO &"
        tables["ACCOUNT_INFO"].loc[0, "OWNER_NAME2"] = "GARCIA"
        rec = {"address_normalized": "100 MAIN ST"}
        enrichment.enrich_record(rec, tables, idx)
        # The trailing & on NAME1 is cleaned; the join " & " is added explicitly.
        assert rec["dcad_owner"] == "SANDOVAL MA & INOCENCIO & GARCIA"

    def test_falls_back_to_multi_owner(self, dcad_fixture):
        """When ACCOUNT_INFO has no name, use MULTI_OWNER."""
        tables, idx = dcad_fixture
        tables["ACCOUNT_INFO"].loc[0, "OWNER_NAME1"] = ""
        tables["ACCOUNT_INFO"].loc[0, "ACCOUNT_NUM"] = "10003"
        idx["100 MAIN ST"] = "10003"
        rec = {"address_normalized": "100 MAIN ST"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_owner"] == "BOB & ALICE TRUSTEES"

    def test_no_exemption_row(self, dcad_fixture):
        """Account matched but no APPLIED_STD_EXEMPT row â†’ all four False."""
        tables, idx = dcad_fixture
        # Remove all exemption rows for 10002, then test.
        ex = tables["APPLIED_STD_EXEMPT"]
        tables["APPLIED_STD_EXEMPT"] = ex[ex["ACCOUNT_NUM"] != "10002"].reset_index(drop=True)
        rec = {"address_normalized": "200 ELM AVE"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_homestead"]    is False
        assert rec["dcad_over65"]       is False
        assert rec["dcad_disabled"]     is False
        assert rec["dcad_tax_deferred"] is False

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

    def test_tot_val_used_for_market_value(self, dcad_fixture):
        """Market value comes from TOT_VAL, not MARKET_VAL."""
        tables, idx = dcad_fixture
        rec = {"address_normalized": "200 ELM AVE"}
        enrichment.enrich_record(rec, tables, idx)
        assert rec["dcad_market_value"] == 75000.0


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

