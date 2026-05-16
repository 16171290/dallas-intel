"""
PR 3 regression tests for the schema extensions.

Verifies:
  - Both canonicalize_* functions emit signal_metadata, address_city,
    address_state, address_zip as None by default. Existing records that
    don't set these fields explicitly continue to work.
  - dcad_bulk.build_account_index returns the expected shape.
  - Missing optional columns (PROPERTY_CITY, PROPERTY_ZIPCODE) are
    tolerated.
  - Missing ACCOUNT_INFO returns an empty dict (no exception).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.enrichment import canonicalize_foreclosure, canonicalize_publicsearch
from scraper.dcad_bulk import build_account_index
from scraper.foreclosure_pdfs import ForeclosureRecord
from scraper.publicsearch import PublicSearchRecord


# ============================================================================
# canonicalize_* emit the new optional fields with None defaults
#
# Construction patterns mirror tests/test_enrichment.py so we keep coupling
# to the real dataclass signatures (ForeclosureRecord uses property_address /
# original_loan_amount; PublicSearchRecord uses record_id / dallas_code).
# ============================================================================


def test_pr3_canonicalize_foreclosure_emits_signal_metadata_none():
    fc = ForeclosureRecord(
        source_pdf="test.pdf",
        sale_date="October 7, 2025",
        sale_date_iso="2025-10-07",
        property_address="1234 ELM ST DALLAS, TX 75201",
        debtor="JOHN SMITH",
    )
    rec = canonicalize_foreclosure(fc)
    assert "signal_metadata" in rec
    assert rec["signal_metadata"] is None


def test_pr3_canonicalize_foreclosure_emits_address_city_state_zip_none():
    fc = ForeclosureRecord(
        source_pdf="test.pdf",
        sale_date="October 7, 2025",
        sale_date_iso="2025-10-07",
        property_address="1234 ELM ST DALLAS, TX 75201",
        debtor="JOHN SMITH",
    )
    rec = canonicalize_foreclosure(fc)
    for field in ("address_city", "address_state", "address_zip"):
        assert field in rec, f"missing field: {field}"
        assert rec[field] is None, f"{field} should default to None, got {rec[field]!r}"


def test_pr3_canonicalize_publicsearch_emits_signal_metadata_none():
    ps = PublicSearchRecord(
        record_id="ABC123",
        dallas_code="LP",
        filing_date="2026-06-01",
        grantor="GRANTOR INC",
        grantee="GRANTEE LLC",
        address="1234 ELM ST",
    )
    rec = canonicalize_publicsearch(ps)
    assert "signal_metadata" in rec
    assert rec["signal_metadata"] is None


def test_pr3_canonicalize_publicsearch_emits_address_city_state_zip_none():
    ps = PublicSearchRecord(
        record_id="ABC123",
        dallas_code="LP",
        filing_date="2026-06-01",
        grantor="GRANTOR INC",
        grantee="GRANTEE LLC",
        address="1234 ELM ST",
    )
    rec = canonicalize_publicsearch(ps)
    for field in ("address_city", "address_state", "address_zip"):
        assert field in rec
        assert rec[field] is None


# ============================================================================
# build_account_index: shape and edge cases
# ============================================================================


def _df(rows):
    """Build a pandas DataFrame from a list of dicts, string-typed."""
    return pd.DataFrame(rows, dtype=str)


def test_pr3_build_account_index_basic_shape():
    tables = {
        "ACCOUNT_INFO": _df([
            {
                "ACCOUNT_NUM": "00001",
                "STREET_NUM": "1234",
                "STREET_HALF_NUM": "",
                "FULL_STREET_NAME": "ELM ST",
                "PROPERTY_CITY": "DALLAS",
                "PROPERTY_ZIPCODE": "75201",
            },
        ])
    }
    idx = build_account_index(tables)
    assert "00001" in idx
    entry = idx["00001"]
    assert set(entry.keys()) == {"address_normalized", "address_city",
                                  "address_state", "address_zip"}
    assert entry["address_city"] == "DALLAS"
    assert entry["address_state"] == "TX"
    assert entry["address_zip"] == "75201"
    assert entry["address_normalized"] is not None


def test_pr3_build_account_index_handles_missing_optional_columns():
    """If PROPERTY_CITY / PROPERTY_ZIPCODE absent, fields are None."""
    tables = {
        "ACCOUNT_INFO": _df([
            {
                "ACCOUNT_NUM": "00001",
                "STREET_NUM": "1234",
                "STREET_HALF_NUM": "",
                "FULL_STREET_NAME": "ELM ST",
            },
        ])
    }
    idx = build_account_index(tables)
    assert idx["00001"]["address_city"]  is None
    assert idx["00001"]["address_zip"]   is None
    assert idx["00001"]["address_state"] == "TX"


def test_pr3_build_account_index_handles_empty_city_zip_values():
    """Empty strings in PROPERTY_CITY/ZIP become None, not empty string."""
    tables = {
        "ACCOUNT_INFO": _df([
            {
                "ACCOUNT_NUM": "00001",
                "STREET_NUM": "1234",
                "STREET_HALF_NUM": "",
                "FULL_STREET_NAME": "ELM ST",
                "PROPERTY_CITY": "",
                "PROPERTY_ZIPCODE": "",
            },
        ])
    }
    idx = build_account_index(tables)
    assert idx["00001"]["address_city"] is None
    assert idx["00001"]["address_zip"]  is None


def test_pr3_build_account_index_skips_rows_without_account_num():
    tables = {
        "ACCOUNT_INFO": _df([
            {"ACCOUNT_NUM": "", "STREET_NUM": "1", "STREET_HALF_NUM": "",
             "FULL_STREET_NAME": "ELM ST"},
            {"ACCOUNT_NUM": "00002", "STREET_NUM": "2", "STREET_HALF_NUM": "",
             "FULL_STREET_NAME": "OAK AVE"},
        ])
    }
    idx = build_account_index(tables)
    assert "00002" in idx
    assert len(idx) == 1


def test_pr3_build_account_index_missing_table_returns_empty():
    assert build_account_index({}) == {}


def test_pr3_build_account_index_missing_required_columns_returns_empty():
    tables = {
        "ACCOUNT_INFO": _df([
            {"SOMETHING_ELSE": "foo"},
        ])
    }
    assert build_account_index(tables) == {}


def test_pr3_build_account_index_multiple_accounts():
    tables = {
        "ACCOUNT_INFO": _df([
            {"ACCOUNT_NUM": "00001", "STREET_NUM": "1234",
             "STREET_HALF_NUM": "", "FULL_STREET_NAME": "ELM ST",
             "PROPERTY_CITY": "DALLAS", "PROPERTY_ZIPCODE": "75201"},
            {"ACCOUNT_NUM": "00002", "STREET_NUM": "5678",
             "STREET_HALF_NUM": "", "FULL_STREET_NAME": "OAK AVE",
             "PROPERTY_CITY": "GARLAND", "PROPERTY_ZIPCODE": "75040"},
        ])
    }
    idx = build_account_index(tables)
    assert len(idx) == 2
    assert idx["00001"]["address_city"] == "DALLAS"
    assert idx["00002"]["address_city"] == "GARLAND"
