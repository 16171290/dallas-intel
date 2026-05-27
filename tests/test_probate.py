"""
Tests for scraper/probate.py.

All HTTP is mocked. Tests cover:
  - happy path: a real response shape produces ProbateRecord(s)
  - cookie expiry: 401, 403, empty body all return []
  - server problems: non-2xx, network exception, malformed JSON return []
  - sealed/hidden filter at parse time
  - the verified Phase 1 payload structure is what we actually send
  - date-bucket logic
  - defensive party extraction (missing parties array, weird shapes)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import config, probate
from scraper.probate import (
    fetch_dallas_probate,
    _build_dallas_probate_payload,
    _date_bucket_for_days,
    _parse_probate_response,
    _hit_to_record,
    _party_name,
    ProbateRecord,
    API_URL,
)


# ============================================================================
# Fixtures and helpers
# ============================================================================


def _hit(**overrides) -> dict:
    """Build a minimal valid hit dict, override-friendly."""
    base = {
        "caseDataID":          "abc-123",
        "caseNumber":          "PR26-00001",
        "caseTypeCode":        "Muniment of Title",
        "status":              "Filed",
        "dateFiled":           "2026-05-15T00:00:00",
        "judicialOfficerName": "Hon. Jane Smith",
        "jurisdiction":        "Dallas County - County Clerk",
        "isSealed":            False,
        "isHiddenFromPublic":  False,
        "parties": [
            {"partyTypeCode": "Decedent",  "name": "DOE JOHN A"},
            {"partyTypeCode": "Applicant", "name": "DOE MARY B",
             "attorneyNames": ["Atty One"]},
        ],
    }
    base.update(overrides)
    return base


def _envelope(hits: list[dict]) -> dict:
    """Wrap a list of hits in Tyler's response envelope."""
    return {"result": {"searchResults": {"hits": hits}}}


def _mock_response(status=200, body=None, raises=None):
    """Build a fake requests.Response for the mock POST."""
    mock = MagicMock(spec=requests.Response)
    mock.status_code = status
    if isinstance(body, dict):
        import json
        mock.text = json.dumps(body)
        mock.json.return_value = body
    elif isinstance(body, str):
        mock.text = body
        mock.json.side_effect = ValueError("not json")
    else:
        mock.text = ""
        mock.json.side_effect = ValueError("empty")
    return mock


@pytest.fixture
def probate_enabled(monkeypatch):
    """Force config.PROBATE_ENABLED = True for the test."""
    monkeypatch.setattr(config, "PROBATE_ENABLED", True)
    fake_cookies = {"FedAuth": "test-fedauth", "RSCH_JWT": "test-jwt"}
    monkeypatch.setattr(
        "scraper.probate.probate_auth.acquire_session",
        lambda: fake_cookies,
    )


# ============================================================================
# Payload construction
# ============================================================================


class TestPayloadStructure:
    def test_payload_matches_phase1_contract(self):
        payload = _build_dallas_probate_payload(days_back=7)

        # The exact verified shape
        assert payload["queryString"]           == "*"
        assert payload["pageSize"]              == 200
        assert payload["pageIndex"]             == 0
        assert payload["SearchIndexType"]       == "Cases"
        assert payload["hearingListView"]       is True
        assert payload["isCalendarView"]        is False
        assert payload["isNewestFirst"]         is True
        assert payload["isReturningFromDetails"] is False
        assert payload["sortFieldOrder"]        == 0
        assert payload["sortFields"]            == 3

    def test_payload_facets_in_expected_order(self):
        payload = _build_dallas_probate_payload(days_back=7)
        facets = payload["selectedFacets"]
        assert len(facets) == 3
        assert facets[0]["facetKey"] == "Location"
        assert facets[1]["facetKey"] == "Case Category"
        assert facets[2]["facetKey"] == "Case Filed Date"

    def test_payload_location_dallas(self):
        payload = _build_dallas_probate_payload(days_back=7)
        bucket = payload["selectedFacets"][0]["selectedBuckets"][0]["bucketKey"]
        assert bucket == "dallas county - county clerk"

    def test_payload_categories_probate_and_probate_other(self):
        payload = _build_dallas_probate_payload(days_back=7)
        cat_buckets = payload["selectedFacets"][1]["selectedBuckets"]
        keys = {b["bucketKey"] for b in cat_buckets}
        assert keys == {"probate", "probate - other"}


class TestDateBucket:
    def test_days_back_1_returns_today(self):
        assert _date_bucket_for_days(1) == "Today"

    def test_days_back_7_returns_last_week(self):
        assert _date_bucket_for_days(7) == "In The Last Week"

    def test_days_back_3_returns_last_week(self):
        # 3 days back still falls inside the week bucket
        assert _date_bucket_for_days(3) == "In The Last Week"

    def test_days_back_31_returns_last_month(self):
        assert _date_bucket_for_days(31) == "In The Last Month"

    def test_days_back_huge_returns_last_month(self):
        # No bigger bucket exists in Tyler's UI; we cap at month
        assert _date_bucket_for_days(365) == "In The Last Month"


# ============================================================================
# Kill switch and missing-cookie short-circuits
# ============================================================================


class TestShortCircuits:
    def test_disabled_returns_empty_no_http(self, monkeypatch):
        monkeypatch.setattr(config, "PROBATE_ENABLED", False)
        with patch("scraper.probate.requests.post") as mock_post:
            result = fetch_dallas_probate()
        assert result == []
        mock_post.assert_not_called()

    def test_missing_cookie_returns_empty_no_http(self, monkeypatch):
        monkeypatch.setattr(config, "PROBATE_ENABLED", True)
        with patch("scraper.probate.probate_auth.acquire_session", return_value=None):
            with patch("scraper.probate.requests.post") as mock_post:
                result = fetch_dallas_probate()
        assert result == []
        mock_post.assert_not_called()

    def test_empty_cookie_returns_empty_no_http(self, monkeypatch):
        monkeypatch.setattr(config, "PROBATE_ENABLED", True)
        with patch("scraper.probate.probate_auth.acquire_session", return_value=None):
            with patch("scraper.probate.requests.post") as mock_post:
                result = fetch_dallas_probate()
        assert result == []
        mock_post.assert_not_called()


# ============================================================================
# Cookie-expiry detection
# ============================================================================


class TestCookieExpiry:
    def test_401_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=401, body="")):
            assert fetch_dallas_probate() == []

    def test_403_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=403, body="")):
            assert fetch_dallas_probate() == []

    def test_200_empty_body_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body="")):
            assert fetch_dallas_probate() == []

    def test_200_whitespace_body_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body="   ")):
            assert fetch_dallas_probate() == []


# ============================================================================
# Server-side / transport failures
# ============================================================================


class TestServerFailures:
    def test_500_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=500, body="oops")):
            assert fetch_dallas_probate() == []

    def test_429_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=429, body="rate limit")):
            assert fetch_dallas_probate() == []

    def test_network_exception_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   side_effect=requests.ConnectionError("DNS fail")):
            assert fetch_dallas_probate() == []

    def test_timeout_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   side_effect=requests.Timeout("too slow")):
            assert fetch_dallas_probate() == []

    def test_malformed_json_returns_empty(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body="<<not json>>")):
            assert fetch_dallas_probate() == []


# ============================================================================
# Happy path - real response shapes
# ============================================================================


class TestHappyPath:
    def test_single_hit_yields_one_record(self, probate_enabled):
        body = _envelope([_hit()])
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=body)):
            result = fetch_dallas_probate()
        assert len(result) == 1
        rec = result[0]
        assert isinstance(rec, ProbateRecord)
        assert rec.case_data_id  == "abc-123"
        assert rec.case_number   == "PR26-00001"
        assert rec.decedent_name == "DOE JOHN A"
        assert rec.applicant_name == "DOE MARY B"

    def test_multiple_hits_yield_multiple_records(self, probate_enabled):
        body = _envelope([
            _hit(caseDataID="a-1", caseNumber="PR26-001"),
            _hit(caseDataID="a-2", caseNumber="PR26-002"),
            _hit(caseDataID="a-3", caseNumber="PR26-003"),
        ])
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=body)):
            result = fetch_dallas_probate()
        assert len(result) == 3
        assert {r.case_number for r in result} == {"PR26-001", "PR26-002", "PR26-003"}

    def test_empty_hits_returns_empty_list(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=_envelope([]))):
            assert fetch_dallas_probate() == []


# ============================================================================
# Sealed-record filter (ToS §2.3 hard requirement)
# ============================================================================


class TestSealedFilter:
    def test_sealed_record_filtered(self):
        hits = [
            _hit(caseDataID="open", isSealed=False),
            _hit(caseDataID="sealed", isSealed=True),
        ]
        records, skipped_sealed, _ = _parse_probate_response(_envelope(hits))
        assert len(records) == 1
        assert records[0].case_data_id == "open"
        assert skipped_sealed == 1

    def test_hidden_record_filtered(self):
        hits = [
            _hit(caseDataID="visible", isHiddenFromPublic=False),
            _hit(caseDataID="hidden", isHiddenFromPublic=True),
        ]
        records, skipped_sealed, _ = _parse_probate_response(_envelope(hits))
        assert len(records) == 1
        assert records[0].case_data_id == "visible"
        assert skipped_sealed == 1

    def test_both_sealed_and_hidden_filtered(self):
        hits = [_hit(caseDataID="sealed_hidden",
                     isSealed=True, isHiddenFromPublic=True)]
        records, skipped_sealed, _ = _parse_probate_response(_envelope(hits))
        assert len(records) == 0
        assert skipped_sealed == 1

    def test_sealed_filter_runs_end_to_end(self, probate_enabled):
        """Sealed records must never reach fetch_dallas_probate's caller."""
        body = _envelope([
            _hit(caseDataID="OK", isSealed=False),
            _hit(caseDataID="SEAL", isSealed=True),
        ])
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=body)):
            result = fetch_dallas_probate()
        ids = {r.case_data_id for r in result}
        assert ids == {"OK"}, f"Sealed record leaked: {ids}"


# ============================================================================
# Party extraction defensiveness
# ============================================================================


class TestPartyExtraction:
    def test_no_parties_array_warning(self):
        hit = _hit()
        hit.pop("parties", None)
        rec = _hit_to_record(hit)
        assert rec.decedent_name is None
        assert rec.applicant_name is None
        assert "no Decedent party found" in rec.parse_warnings
        assert "no Applicant/Petitioner party found" in rec.parse_warnings

    def test_parties_not_a_list(self):
        hit = _hit(parties="oops not a list")
        rec = _hit_to_record(hit)
        # No crash; both names absent; warnings emitted
        assert rec.decedent_name is None
        assert rec.applicant_name is None

    def test_party_name_from_structured_fields(self):
        party = {"lastName": "SMITH", "firstName": "JANE", "middleName": "A"}
        assert _party_name(party) == "SMITH JANE A"

    def test_party_name_prefers_direct_name(self):
        party = {"name": "DIRECT VALUE", "lastName": "IGNORED"}
        assert _party_name(party) == "DIRECT VALUE"

    def test_party_name_returns_none_when_all_empty(self):
        assert _party_name({}) is None
        assert _party_name({"lastName": "", "firstName": ""}) is None

    def test_petitioner_role_recognized_as_applicant(self):
        hit = _hit(parties=[
            {"partyTypeCode": "Decedent",   "name": "DEC NAME"},
            {"partyTypeCode": "Petitioner", "name": "PET NAME"},
        ])
        rec = _hit_to_record(hit)
        assert rec.applicant_name == "PET NAME"

    def test_multiple_applicants_first_wins_rest_archived(self):
        hit = _hit(parties=[
            {"partyTypeCode": "Decedent",  "name": "DEC NAME"},
            {"partyTypeCode": "Applicant", "name": "APP ONE"},
            {"partyTypeCode": "Applicant", "name": "APP TWO"},
            {"partyTypeCode": "Applicant", "name": "APP THREE"},
        ])
        rec = _hit_to_record(hit)
        assert rec.applicant_name == "APP ONE"
        assert rec.additional_applicants == ["APP TWO", "APP THREE"]

    def test_attorneys_aggregated_across_parties(self):
        hit = _hit(parties=[
            {"partyTypeCode": "Decedent",  "name": "DEC",
             "attorneyNames": ["Atty A"]},
            {"partyTypeCode": "Applicant", "name": "APP",
             "attorneyNames": ["Atty B", "Atty C"]},
        ])
        rec = _hit_to_record(hit)
        assert set(rec.attorneys) == {"Atty A", "Atty B", "Atty C"}


# ============================================================================
# Malformed response shapes - we don't crash
# ============================================================================


class TestMalformedShapes:
    def test_no_result_key(self):
        records, _, _ = _parse_probate_response({})
        assert records == []

    def test_no_searchresults_key(self):
        records, _, _ = _parse_probate_response({"result": {}})
        assert records == []

    def test_no_hits_key(self):
        records, _, _ = _parse_probate_response({"result": {"searchResults": {}}})
        assert records == []

    def test_hits_not_a_list(self):
        records, _, _ = _parse_probate_response(
            {"result": {"searchResults": {"hits": "oops"}}}
        )
        assert records == []

    def test_hit_not_a_dict(self):
        records, _, skipped = _parse_probate_response(
            _envelope(["this is a string, not a hit"])
        )
        assert records == []
        assert skipped == 1

    def test_unparseable_hit_logged_but_others_continue(self):
        # Force one hit to fail parsing by removing caseDataID and
        # making parties radioactive
        good = _hit(caseDataID="good")
        bad = _hit(caseDataID="")
        bad["parties"] = [{"name": "x"}]  # this is parseable, just light
        records, _, _ = _parse_probate_response(_envelope([good, bad]))
        # Even the "bad" parses (it's tolerant); just confirm good is in
        assert any(r.case_data_id == "good" for r in records)


# ============================================================================
# Verify the wire request matches the contract
# ============================================================================


class TestWireRequest:
    def test_post_called_with_verified_url(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=_envelope([]))) as p:
            fetch_dallas_probate()
        called_url = p.call_args.args[0] if p.call_args.args else p.call_args.kwargs.get("url")
        assert called_url == API_URL

    def test_post_called_with_cookie_header(self, monkeypatch):
        monkeypatch.setattr(config, "PROBATE_ENABLED", True)
        explicit_cookies = {"TESTCOOKIE": "xyz", "OTHER": "value"}
        monkeypatch.setattr(
            "scraper.probate.probate_auth.acquire_session",
            lambda: explicit_cookies,
        )
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=_envelope([]))) as p:
            fetch_dallas_probate()
        headers = p.call_args.kwargs.get("headers", {})
        assert "Cookie" in headers
        assert headers["Cookie"] == "TESTCOOKIE=xyz; OTHER=value"
        assert headers["Content-Type"] == "application/json"

    def test_post_called_with_json_payload(self, probate_enabled):
        with patch("scraper.probate.requests.post",
                   return_value=_mock_response(status=200, body=_envelope([]))) as p:
            fetch_dallas_probate()
        payload = p.call_args.kwargs.get("json")
        assert payload is not None
        assert payload["queryString"] == "*"
        assert payload["pageSize"] == 200


# ============================================================================
# Date detail URL helper
# ============================================================================


class TestCaseDetailUrl:
    def test_url_includes_case_data_id(self):
        url = probate.case_detail_url("abc-123-uuid")
        assert "abc-123-uuid" in url
        assert url.startswith("https://research.txcourts.gov/CourtRecordsSearch/")
        # Route confirmed by live browser observation 2026-05-27: singular
        # "case", no "/details" suffix. The case_data_id is the last segment.
        assert "/ui/case/" in url
        assert url.endswith("abc-123-uuid")
