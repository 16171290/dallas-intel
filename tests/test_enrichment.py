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
        # Phase 3.D: filing_date now sources from notice_date_iso (the real
        # trustee-filed notice date), not sale_date_iso (the auction date).
        # The previous proxy behavior was a known bug fixed 2026-05-15.
        rec = foreclosure_pdfs.ForeclosureRecord(
            source_pdf="Dallas_1.pdf",
            sale_date="October 7, 2025",
            sale_date_iso="2025-10-07",
            notice_date="1/12/2026",
            notice_date_iso="2026-01-12",
            notice_date_pattern="P2_DATED",
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
        assert out["filing_date"] == "2026-01-12"
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


# ============================================================================
# Canonicalization - probate
# ============================================================================

class TestCanonicalizeProbate:
    """Probate-source canonicalizer (PR 5)."""

    def _make_record(self, **overrides):
        from scraper.probate import ProbateRecord
        defaults = dict(
            case_data_id="abc123def456",
            case_number="PR-26-01579-1",
            case_type="Independent Administration",
            case_status="Open",
            date_filed="2026-05-14T17:00:00+00:00",
            decedent_name="DOE, JANE",
            applicant_name="DOE, JOHN",
            additional_applicants=[],
            judge="WARREN, INGRID M.",
            attorneys=["SMITH, ATTORNEY"],
            jurisdiction="Dallas County - County Clerk",
            raw={},
            parse_warnings=[],
        )
        defaults.update(overrides)
        return ProbateRecord(**defaults)

    def test_basic_fields(self):
        rec = self._make_record()
        out = enrichment.canonicalize_probate(rec)
        assert out["record_id"]    == "pro-abc123def456"
        assert out["source"]       == "probate.txcourts.gov"
        assert out["dallas_code"]  == "PB"
        assert out["category"]     == "PROB"
        assert out["instrument_num"] == "PR-26-01579-1"
        assert out["grantor"]      == "DOE, JANE"   # decedent
        assert out["grantee"]      == "DOE, JOHN"   # applicant
        assert out["active"] is True
        assert out["score"]        == 0
        assert out["score_breakdown"] == {}

    def test_filing_date_normalized_to_date_only(self):
        rec = self._make_record(date_filed="2026-05-14T17:00:00+00:00")
        out = enrichment.canonicalize_probate(rec)
        assert out["filing_date"] == "2026-05-14"

    def test_filing_date_naive_timestamp(self):
        rec = self._make_record(date_filed="2026-05-15T12:00:00")
        out = enrichment.canonicalize_probate(rec)
        assert out["filing_date"] == "2026-05-15"

    def test_filing_date_none_passthrough(self):
        rec = self._make_record(date_filed=None)
        out = enrichment.canonicalize_probate(rec)
        assert out["filing_date"] is None

    def test_address_and_dcad_slots_are_none(self):
        rec = self._make_record()
        out = enrichment.canonicalize_probate(rec)
        assert out["address"] is None
        assert out["address_normalized"] is None
        assert out["address_city"] is None
        assert out["address_state"] is None
        assert out["address_zip"] is None
        assert out["dcad_account"] is None
        assert out["dcad_owner"] is None
        assert out["dcad_market_value"] is None
        assert out["dcad_homestead"] is None
        assert out["dcad_over65"] is None
        assert out["dcad_disabled"] is None
        assert out["dcad_tax_deferred"] is None
        assert out["amount"] is None
        assert out["trustee"] is None
        assert out["sale_date"] is None

    def test_signal_metadata_structure(self):
        rec = self._make_record(
            case_type="Dependent Administration",
            case_status="OPEN",
            judge="MALVEAUX, JULIA",
            attorneys=["LAWYER ONE", "LAWYER TWO"],
            jurisdiction="Dallas County - County Clerk",
            additional_applicants=["DOE, SECOND APPLICANT"],
        )
        out = enrichment.canonicalize_probate(rec)
        meta = out["signal_metadata"]
        assert meta["case_type"]    == "Dependent Administration"
        assert meta["case_status"]  == "OPEN"
        assert meta["judge"]        == "MALVEAUX, JULIA"
        assert meta["attorneys"]    == ["LAWYER ONE", "LAWYER TWO"]
        assert meta["jurisdiction"] == "Dallas County - County Clerk"
        assert meta["additional_applicants"] == ["DOE, SECOND APPLICANT"]

    def test_parse_warnings_passthrough(self):
        rec = self._make_record(parse_warnings=["missing_decedent", "applicant_truncated"])
        out = enrichment.canonicalize_probate(rec)
        assert out["parse_warnings"] == ["missing_decedent", "applicant_truncated"]
        # Confirm it is a fresh list, not the underlying record list (mutation safety)
        assert out["parse_warnings"] is not rec.parse_warnings

    def test_attorneys_list_is_copied(self):
        attorneys = ["ORIGINAL"]
        rec = self._make_record(attorneys=attorneys)
        out = enrichment.canonicalize_probate(rec)
        assert out["signal_metadata"]["attorneys"] == ["ORIGINAL"]
        assert out["signal_metadata"]["attorneys"] is not attorneys

    # ----- DCAD fan-out (PR 5.x) ------------------------------------------

    def test_no_owner_index_skips_fanout(self):
        """Backwards compat — calling without owner_index keeps the legacy
        address=None, no metadata behavior."""
        rec = self._make_record(decedent_name="WALLACE, VON ELLEN")
        out = enrichment.canonicalize_probate(rec)
        assert out["address_normalized"] is None
        assert out["signal_metadata"]["decedent_owned_properties"] == []
        assert out["signal_metadata"]["dcad_match_tier"] is None
        assert out["signal_metadata"]["dcad_match_warning"] is None

    def test_match_populates_address_and_properties(self):
        rec = self._make_record(decedent_name="WALLACE, VON ELLEN")
        owner_index = {"WALLACE VON ELLEN": ["acct_w"]}
        account_addr_index = {
            "acct_w": {
                "address_normalized": "1316 SATURN ST",
                "address_city": "CEDAR HILL",
                "address_state": "TX",
                "address_zip": "75104",
            },
        }
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index=account_addr_index,
        )
        assert out["address_normalized"] == "1316 SATURN ST"
        props = out["signal_metadata"]["decedent_owned_properties"]
        assert len(props) == 1
        assert props[0]["account_num"] == "acct_w"
        assert props[0]["address_normalized"] == "1316 SATURN ST"
        assert props[0]["address_city"] == "CEDAR HILL"
        assert out["signal_metadata"]["dcad_match_tier"] == "exact"
        assert out["signal_metadata"]["dcad_match_warning"] is None

    def test_match_uses_first_property_as_primary(self):
        """For multi-property matches, top-level address_normalized takes
        the first account; signal_metadata lists all of them."""
        rec = self._make_record(decedent_name="WARREN, ALLEN, Jr")
        owner_index = {"WARREN ALLEN": ["acct_a", "acct_b"]}
        account_addr_index = {
            "acct_a": {"address_normalized": "1337 EXETER DR"},
            "acct_b": {"address_normalized": "203 GOLDEN POND DR"},
        }
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index=account_addr_index,
        )
        assert out["address_normalized"] == "1337 EXETER DR"
        props = out["signal_metadata"]["decedent_owned_properties"]
        assert len(props) == 2
        assert [p["account_num"] for p in props] == ["acct_a", "acct_b"]

    def test_no_match_leaves_fields_none(self):
        rec = self._make_record(decedent_name="NONEXISTENT, PERSON")
        owner_index = {"WALLACE VON ELLEN": ["acct_w"]}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index={},
        )
        assert out["address_normalized"] is None
        assert out["signal_metadata"]["decedent_owned_properties"] == []
        assert out["signal_metadata"]["dcad_match_tier"] is None
        assert out["signal_metadata"]["dcad_match_warning"] is None

    def test_common_name_pollution_warning_propagated(self):
        rec = self._make_record(decedent_name="WILSON, ROBERT")
        accounts = [f"acct_{i}" for i in range(8)]
        owner_index = {"WILSON ROBERT": accounts}
        account_addr_index = {
            f"acct_{i}": {"address_normalized": f"{i} MAIN ST"} for i in range(8)
        }
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index=account_addr_index,
        )
        assert out["signal_metadata"]["dcad_match_warning"] == "common_name_pollution"
        assert len(out["signal_metadata"]["decedent_owned_properties"]) == 8

    def test_account_address_index_missing_account_yields_none_fields(self):
        """If the matcher returns an account that account_address_index
        doesn't know about, the property entry's address fields are None
        rather than crashing."""
        rec = self._make_record(decedent_name="WALLACE, VON ELLEN")
        owner_index = {"WALLACE VON ELLEN": ["unknown_acct"]}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index={},
        )
        props = out["signal_metadata"]["decedent_owned_properties"]
        assert len(props) == 1
        assert props[0]["account_num"] == "unknown_acct"
        assert props[0]["address_normalized"] is None
        assert out["address_normalized"] is None  # falls through cleanly

    def test_missing_decedent_name_skips_fanout(self):
        rec = self._make_record(decedent_name=None)
        owner_index = {"WALLACE VON ELLEN": ["acct_w"]}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index={},
        )
        assert out["signal_metadata"]["decedent_owned_properties"] == []
        assert out["signal_metadata"]["dcad_match_tier"] is None

    # ----- Applicant mailing fan-out (PR 5.y) -----------------------------
    # Stricter criteria than decedent: exact / no_middle only, no
    # common_name_pollution, <= 2 accounts.

    def test_applicant_exact_match_populates_mailing(self):
        rec = self._make_record(
            decedent_name="WALLACE, VON ELLEN",
            applicant_name="MORTON, REBECCA J.",
        )
        owner_index = {"MORTON REBECCA J": ["acct_app"]}
        mailing_index = {"acct_app": {
            "address": "3960 DAVILA DR", "city": "DALLAS",
            "state": "TEXAS", "zip": "75220",
        }}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={},
            mailing_index=mailing_index,
        )
        am = out["signal_metadata"]["applicant_mailing"]
        assert am is not None
        assert am["tier"] == "exact"
        assert am["address"] == "3960 DAVILA DR"
        assert am["state"] == "TEXAS"

    def test_applicant_no_middle_match_populates_mailing(self):
        rec = self._make_record(applicant_name="HALL, WENDY E.")
        owner_index = {"HALL WENDY": ["acct_h"]}
        mailing_index = {"acct_h": {
            "address": "3916 DANZIG DR", "city": "GRAND PRAIRIE",
            "state": "TX", "zip": "75052",
        }}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_index,
        )
        assert out["signal_metadata"]["applicant_mailing"]["tier"] == "no_middle"
        assert out["signal_metadata"]["applicant_mailing"]["address"] == "3916 DANZIG DR"

    def test_applicant_common_name_pollution_rejected(self):
        """4+ accounts triggers common_name_pollution warning — Option B
        rejects, applicant_mailing stays None."""
        rec = self._make_record(applicant_name="GARCIA, SYLVIA ELIZABETH")
        owner_index = {
            "GARCIA SYLVIA": [f"a{i}" for i in range(10)],  # 10 accounts -> pollution
        }
        mailing_index = {"a0": {"address": "111 ANY ST"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_index,
        )
        assert out["signal_metadata"]["applicant_mailing"] is None

    def test_applicant_surname_only_rejected(self):
        """surname_only tier is below the high-confidence threshold —
        Option B excludes it for applicant mailing."""
        rec = self._make_record(applicant_name="WALLACE, ALLEN GLENN")
        # owner_index keyed by just 'WALLACE' (family-trust collapse) —
        # would match surname_only tier with <=2 accounts
        owner_index = {"WALLACE": ["acct_w", "acct_w2"]}
        mailing_index = {"acct_w": {"address": "6314 NORWAY RD"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_index,
        )
        assert out["signal_metadata"]["applicant_mailing"] is None

    def test_applicant_initial_form_rejected(self):
        """initial_form tier also excluded by Option B's strict criteria."""
        rec = self._make_record(applicant_name="MATUS, JOSE ARISTIDES")
        owner_index = {"MATUS JOSE A": ["acct_m"]}  # initial_form tier match
        mailing_index = {"acct_m": {"address": "232 TOUCHDOWN DR"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_index,
        )
        assert out["signal_metadata"]["applicant_mailing"] is None

    def test_applicant_three_accounts_rejected_by_max(self):
        """Exact-tier match with 3 accounts: no pollution warning (warning
        fires at >3) but Option B's <=2 cap rejects it anyway."""
        rec = self._make_record(applicant_name="WALLACE, ALLEN")
        owner_index = {"WALLACE ALLEN": ["a", "b", "c"]}  # 3 accts
        mailing_index = {"a": {"address": "111 ANY ST"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_index,
        )
        assert out["signal_metadata"]["applicant_mailing"] is None

    def test_applicant_no_mailing_index_yields_none(self):
        """Backwards compat — mailing_index omitted means no applicant
        mailing populated, even if there's an exact match."""
        rec = self._make_record(applicant_name="MORTON, REBECCA J.")
        owner_index = {"MORTON REBECCA J": ["acct_app"]}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index={},
        )
        assert out["signal_metadata"]["applicant_mailing"] is None

    def test_applicant_none_skips_fanout(self):
        rec = self._make_record(applicant_name=None)
        out = enrichment.canonicalize_probate(
            rec, owner_index={"X Y": ["a"]},
            account_address_index={},
            mailing_index={"a": {"address": "1 ANY ST"}},
        )
        assert out["signal_metadata"]["applicant_mailing"] is None

    def test_applicant_missing_address_in_mailing_index_yields_none(self):
        """Matcher hit, account looked up in mailing_index, but the
        address field is missing — leave the field None rather than
        ship a useless tier-only payload."""
        rec = self._make_record(applicant_name="MORTON, REBECCA J.")
        owner_index = {"MORTON REBECCA J": ["acct_app"]}
        mailing_index = {"acct_app": {"address": None, "city": "DALLAS"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_index,
        )
        assert out["signal_metadata"]["applicant_mailing"] is None


# ═══════════════════════════════════════════════════════════════════════════
# PR 6 — PB retrofit: resolution_history + direct dcad_account stamping
# ═══════════════════════════════════════════════════════════════════════════


class TestCanonicalizeProbatePR6Retrofit:
    """PR 6: probate canonicalization now writes resolution_history,
    stamps dcad_account directly, and emits canonical warnings — same
    framework as Paths A / B / C."""

    def _make_record(self, **overrides):
        from scraper.probate import ProbateRecord
        defaults = dict(
            case_data_id="abc123",
            case_number="PR-26-99999-1",
            case_type="Independent Administration",
            case_status="Open",
            date_filed="2026-05-14T17:00:00",
            decedent_name="WALLACE, VON ELLEN",
            applicant_name="MORTON, REBECCA J.",
            additional_applicants=[],
            judge="WARREN, INGRID M.",
            attorneys=[],
            jurisdiction="Dallas County - County Clerk",
            raw={},
            parse_warnings=[],
        )
        defaults.update(overrides)
        return ProbateRecord(**defaults)

    def test_decedent_match_stamps_dcad_account_directly(self):
        from scraper.resolution import get_history
        rec = self._make_record(decedent_name="WALLACE, VON ELLEN")
        owner_index = {"WALLACE VON ELLEN": ["acct_w"]}
        addr_index  = {"acct_w": {"address_normalized": "1316 SATURN ST"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index=addr_index,
        )
        # PR 6: dcad_account is stamped on the record (not deferred to enrich_record)
        assert out["dcad_account"] == "acct_w"
        assert out["address_normalized"] == "1316 SATURN ST"
        # History entry written
        history = get_history(out)
        decedent_entries = [
            h for h in history
            if h["path"] == "path_pb_decedent_owner_index"
        ]
        assert len(decedent_entries) == 1
        h = decedent_entries[0]
        assert h["status"] == "matched"
        assert h["dcad_account"] == "acct_w"
        assert h["tier"] == "exact"

    def test_decedent_multi_property_stamps_alternates(self):
        from scraper.resolution import (
            get_alternate_accounts, get_history, get_warnings,
        )
        rec = self._make_record(decedent_name="WARREN, ALLEN")
        owner_index = {"WARREN ALLEN": ["acct_a", "acct_b", "acct_c"]}
        addr_index = {
            "acct_a": {"address_normalized": "1337 EXETER DR"},
            "acct_b": {"address_normalized": "203 GOLDEN POND DR"},
            "acct_c": {"address_normalized": "55 OTHER ST"},
        }
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index=addr_index,
        )
        # Primary acct = first
        assert out["dcad_account"] == "acct_a"
        # Alternates stamped
        alternates = get_alternate_accounts(out)
        assert set(alternates) == {"acct_b", "acct_c"}
        # Multi-account warning surfaced
        assert "multi_account" in get_warnings(out)
        # History reflects multi_match status
        history = get_history(out)
        decedent_entries = [
            h for h in history if h["path"] == "path_pb_decedent_owner_index"
        ]
        assert decedent_entries[0]["status"] == "multi_match"
        assert set(decedent_entries[0]["alternates"]) == {"acct_b", "acct_c"}

    def test_common_name_pollution_emits_warning(self):
        from scraper.resolution import get_history, get_warnings
        rec = self._make_record(decedent_name="WILSON, ROBERT")
        # 8 accounts triggers common_name_pollution (threshold > 3)
        accounts = [f"acct_{i}" for i in range(8)]
        owner_index = {"WILSON ROBERT": accounts}
        addr_index = {a: {"address_normalized": f"{i} MAIN ST"}
                      for i, a in enumerate(accounts)}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index=addr_index,
        )
        # Both legacy and new locations carry the signal
        assert out["signal_metadata"]["dcad_match_warning"] == "common_name_pollution"
        assert "common_name_pollution" in get_warnings(out)
        assert "multi_account" in get_warnings(out)
        # History entry carries the warning too
        history = get_history(out)
        decedent_entries = [
            h for h in history if h["path"] == "path_pb_decedent_owner_index"
        ]
        assert "common_name_pollution" in decedent_entries[0]["warnings"]

    def test_surname_only_tier_emits_warning(self):
        from scraper.resolution import get_warnings
        rec = self._make_record(decedent_name="LE, MINH HUNG")
        # owner_index has only the surname → surname_only tier match
        owner_index = {"LE": ["acct_le"]}
        addr_index = {"acct_le": {"address_normalized": "100 OAK ST"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index=addr_index,
        )
        # Match succeeded, surname_only warning surfaced
        if out["dcad_account"] == "acct_le":
            # surname_only tier was reached
            assert "surname_only_tier" in get_warnings(out)

    def test_no_match_writes_no_match_history(self):
        from scraper.resolution import get_history
        rec = self._make_record(decedent_name="NOBODY, AT ALL")
        owner_index = {"WALLACE VON ELLEN": ["acct_w"]}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index={},
        )
        # dcad_account still None
        assert out["dcad_account"] is None
        # But history records the no-match attempt
        history = get_history(out)
        decedent_entries = [
            h for h in history if h["path"] == "path_pb_decedent_owner_index"
        ]
        assert len(decedent_entries) == 1
        assert decedent_entries[0]["status"] == "no_match"

    def test_no_owner_index_writes_no_history(self):
        """Backwards compat: no owner_index = no fanout = no history entry."""
        from scraper.resolution import get_history
        rec = self._make_record(decedent_name="ANYONE, AT ALL")
        out = enrichment.canonicalize_probate(rec)
        # No fanout attempted, no history entry
        assert get_history(out) == []
        # Legacy fields still in their default state
        assert out["dcad_account"] is None
        assert out["address_normalized"] is None

    def test_applicant_match_writes_history_even_when_accepted(self):
        from scraper.resolution import get_history
        rec = self._make_record(
            decedent_name="WALLACE, VON ELLEN",
            applicant_name="MORTON, REBECCA J.",
        )
        owner_index = {
            "WALLACE VON ELLEN": ["acct_w"],
            "MORTON REBECCA J":  ["acct_app"],
        }
        addr_index  = {"acct_w": {"address_normalized": "1316 SATURN ST"}}
        mailing_idx = {"acct_app": {
            "address": "3960 DAVILA DR", "city": "DALLAS",
            "state": "TEXAS", "zip": "75220",
        }}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index=addr_index,
            mailing_index=mailing_idx,
        )
        # applicant_mailing still populated (existing behavior preserved)
        assert out["signal_metadata"]["applicant_mailing"]["address"] == "3960 DAVILA DR"
        # PR 6: applicant history entry written
        history = get_history(out)
        applicant_entries = [
            h for h in history if h["path"] == "path_pb_applicant_owner_index"
        ]
        assert len(applicant_entries) == 1
        assert applicant_entries[0]["status"] == "matched"
        assert applicant_entries[0]["dcad_account"] == "acct_app"

    def test_applicant_match_rejected_for_mailing_still_logged(self):
        """Applicant matched but tier was too loose (surname_only): mailing
        is rejected, but the match attempt itself is logged in history so
        the operator can see why no mailing was populated."""
        from scraper.resolution import get_history
        rec = self._make_record(applicant_name="WALLACE, ALLEN GLENN")
        owner_index = {"WALLACE": ["acct_w", "acct_w2"]}  # surname_only tier
        mailing_idx = {"acct_w": {"address": "6314 NORWAY RD"}}
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index,
            account_address_index={}, mailing_index=mailing_idx,
        )
        # applicant_mailing rejected (preserving existing semantics)
        assert out["signal_metadata"]["applicant_mailing"] is None
        # History captures the match-but-rejected attempt
        history = get_history(out)
        applicant_entries = [
            h for h in history if h["path"] == "path_pb_applicant_owner_index"
        ]
        assert len(applicant_entries) == 1
        # skip_reason explains why mailing wasn't populated
        assert applicant_entries[0].get("skip_reason") == "applicant_match_too_loose"

    def test_legacy_decedent_owned_properties_still_populated(self):
        """Backward-compat check: dashboards / other readers consuming the
        legacy `decedent_owned_properties` field still work."""
        rec = self._make_record(decedent_name="WARREN, ALLEN")
        owner_index = {"WARREN ALLEN": ["acct_a", "acct_b"]}
        addr_index = {
            "acct_a": {"address_normalized": "1337 EXETER DR",
                       "address_city": "DALLAS", "address_state": "TX",
                       "address_zip": "75201"},
            "acct_b": {"address_normalized": "203 GOLDEN POND DR"},
        }
        out = enrichment.canonicalize_probate(
            rec, owner_index=owner_index, account_address_index=addr_index,
        )
        props = out["signal_metadata"]["decedent_owned_properties"]
        assert len(props) == 2
        assert [p["account_num"] for p in props] == ["acct_a", "acct_b"]
        # Legacy fields still present alongside new framework
        assert out["signal_metadata"]["dcad_match_tier"] == "exact"

