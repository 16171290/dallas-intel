"""Unit tests for scraper.main module-level helpers."""
from __future__ import annotations

from scraper.main import (
    apply_grantor_fallback_from_dcad_owner,
    _publicsearch_property_records_enabled,
)
from scraper.resolution import WARN_GRANTOR_FROM_DCAD, get_warnings


class TestPropertyRecordsGate:
    """The publicsearch Property-Records scrape (LP/TXL/PB/BR/SZS/REL/RLP)
    is disabled by default after the 2026-05-29 embargo flood; NOFs come
    from a separate, unembargoed path."""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PUBLICSEARCH_PROPERTY_RECORDS_ENABLED", raising=False)
        assert _publicsearch_property_records_enabled() is False

    def test_enabled_when_set_true(self, monkeypatch):
        monkeypatch.setenv("PUBLICSEARCH_PROPERTY_RECORDS_ENABLED", "true")
        assert _publicsearch_property_records_enabled() is True

    def test_garbage_value_stays_disabled(self, monkeypatch):
        monkeypatch.setenv("PUBLICSEARCH_PROPERTY_RECORDS_ENABLED", "maybe")
        assert _publicsearch_property_records_enabled() is False


class TestGrantorFallbackFromDcadOwner:
    """PR 7.6: when grantor is null but dcad_owner is set, surface
    dcad_owner as the grantor with WARN_GRANTOR_FROM_DCAD.

    Scenario: NOF records frequently have grantor=None because the
    "Unofficial Copy" watermark mangles OCR. By the time DCAD enrichment
    runs, dcad_owner is populated from ACCOUNT_INFO. We surface that
    name so the operator has a real person to dial."""

    def test_fills_grantor_when_null(self):
        rec = {
            "record_id":   "r1",
            "grantor":     None,
            "dcad_owner":  "Meeking, Steven & Meeking, Gina",
            "dcad_account":"abc",
        }
        n = apply_grantor_fallback_from_dcad_owner([rec])
        assert n == 1
        assert rec["grantor"] == "Meeking, Steven & Meeking, Gina"
        assert WARN_GRANTOR_FROM_DCAD in get_warnings(rec)

    def test_fills_grantor_when_empty_string(self):
        rec = {"record_id": "r1", "grantor": "", "dcad_owner": "Bravo, Francisco"}
        n = apply_grantor_fallback_from_dcad_owner([rec])
        assert n == 1
        assert rec["grantor"] == "Bravo, Francisco"
        assert WARN_GRANTOR_FROM_DCAD in get_warnings(rec)

    def test_fills_grantor_when_whitespace(self):
        rec = {"record_id": "r1", "grantor": "   ", "dcad_owner": "Smith, Jane"}
        n = apply_grantor_fallback_from_dcad_owner([rec])
        assert n == 1
        assert rec["grantor"] == "Smith, Jane"

    def test_does_not_overwrite_existing_grantor(self):
        """The OCR-extracted grantor name is preserved — DCAD owner
        is only used as a fallback when grantor is genuinely missing."""
        rec = {
            "record_id":  "r1",
            "grantor":    "Cox, Laura Annette",   # already populated from OCR
            "dcad_owner": "Cox, L.A.",            # would lose detail if we overwrote
        }
        n = apply_grantor_fallback_from_dcad_owner([rec])
        assert n == 0
        assert rec["grantor"] == "Cox, Laura Annette"
        assert WARN_GRANTOR_FROM_DCAD not in get_warnings(rec)

    def test_no_dcad_owner_leaves_grantor_null(self):
        """No DCAD enrichment ran — grantor stays null. Don't fabricate."""
        rec = {"record_id": "r1", "grantor": None, "dcad_owner": None}
        n = apply_grantor_fallback_from_dcad_owner([rec])
        assert n == 0
        assert rec.get("grantor") is None

    def test_no_dcad_owner_empty_string_leaves_grantor_null(self):
        rec = {"record_id": "r1", "grantor": None, "dcad_owner": ""}
        n = apply_grantor_fallback_from_dcad_owner([rec])
        assert n == 0
        assert rec.get("grantor") is None

    def test_multiple_records_mixed(self):
        """Mixed batch: some get filled, some don't."""
        records = [
            {"record_id": "ocr_good", "grantor": "Real, Person", "dcad_owner": "X"},
            {"record_id": "ocr_null", "grantor": None,           "dcad_owner": "Y, Person"},
            {"record_id": "no_dcad",  "grantor": None,           "dcad_owner": None},
            {"record_id": "ocr_null2","grantor": "",             "dcad_owner": "Z, Other"},
        ]
        n = apply_grantor_fallback_from_dcad_owner(records)
        assert n == 2
        assert records[0]["grantor"] == "Real, Person"           # untouched
        assert records[1]["grantor"] == "Y, Person"              # filled
        assert records[2]["grantor"] is None                     # not fabricated
        assert records[3]["grantor"] == "Z, Other"               # filled

    def test_returns_count(self):
        records = [{"record_id": f"r{i}", "grantor": None, "dcad_owner": f"Owner {i}"}
                   for i in range(5)]
        n = apply_grantor_fallback_from_dcad_owner(records)
        assert n == 5


# ═══════════════════════════════════════════════════════════════════════════
# PR 8 Phase 2 — resolve_pb_via_tyler_case_detail integration
# ═══════════════════════════════════════════════════════════════════════════
#
# We mock the CaseDetailFetcher class so the unit test exercises the
# orchestration logic (filtering, address-index lookup, history writes,
# fail-open behavior) without spinning up Playwright.


from unittest.mock import MagicMock, patch

from scraper.main import resolve_pb_via_tyler_case_detail
from scraper.probate_case_detail import CaseDetailResult
from scraper.resolution import (
    PATH_PB_CASE_DETAIL_INVENTORY,
    WARN_PB_CASE_DETAIL_USED,
    WARN_PB_INVENTORY_NO_ADDRESS,
    get_history,
    get_warnings,
)


class TestResolvePbViaTylerCaseDetail:
    def _addr_index(self):
        return {
            "1234 MAIN ST": "acct_main",
            "5678 OAK AVE": "acct_oak",
        }

    def test_resolves_when_address_matches(self):
        rec = {
            "record_id": "pro-abc123",
            "dallas_code": "PB",
            "source": "probate.txcourts.gov",
            "grantor": "Smith, John",
        }
        result = CaseDetailResult(
            case_data_id="abc123", status="ok",
            page_text="...",
            addresses_found=["1234 Main St, Dallas, TX 75201"],
            has_inventory=True,
        )
        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            instance = MagicMock()
            instance.__enter__ = MagicMock(return_value=lambda cid: result)
            instance.__exit__ = MagicMock(return_value=None)
            MockFetcher.return_value = instance
            stats = resolve_pb_via_tyler_case_detail(
                [rec], self._addr_index(), {"sess": "cookie"},
            )
        assert stats["attempted"] == 1
        assert stats["resolved"] == 1
        assert rec["dcad_account"] == "acct_main"
        assert rec["address_normalized"] == "1234 MAIN ST"
        # History entry written
        history = get_history(rec)
        case_entries = [h for h in history if h["path"] == PATH_PB_CASE_DETAIL_INVENTORY]
        assert len(case_entries) == 1
        assert case_entries[0]["status"] == "matched"
        # Warning surfaces the provenance
        assert WARN_PB_CASE_DETAIL_USED in get_warnings(rec)

    def test_records_inventory_only_when_no_address_matches_dcad(self):
        """Case detail had inventory filing visible but the extracted
        address didn't match anything in DCAD (different county, typo,
        OR no address could be extracted from the page text)."""
        rec = {
            "record_id": "pro-no_addr",
            "dallas_code": "PB",
            "source": "probate.txcourts.gov",
        }
        result = CaseDetailResult(
            case_data_id="no_addr", status="ok",
            page_text="...",
            addresses_found=[],   # nothing extractable
            has_inventory=True,
        )
        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            instance = MagicMock()
            instance.__enter__ = MagicMock(return_value=lambda cid: result)
            instance.__exit__ = MagicMock(return_value=None)
            MockFetcher.return_value = instance
            stats = resolve_pb_via_tyler_case_detail(
                [rec], self._addr_index(), {"sess": "c"},
            )
        assert stats["inventory_only"] == 1
        assert stats["resolved"] == 0
        assert WARN_PB_INVENTORY_NO_ADDRESS in get_warnings(rec)
        # No dcad_account stamped — we don't have a property match
        assert rec.get("dcad_account") is None

    def test_skips_already_resolved_records(self):
        rec = {
            "record_id": "pro-already",
            "dallas_code": "PB",
            "source": "probate.txcourts.gov",
            "dcad_account": "already_set",
        }
        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            fetch_mock = MagicMock()
            MockFetcher.return_value.__enter__.return_value = fetch_mock
            MockFetcher.return_value.__exit__.return_value = None
            stats = resolve_pb_via_tyler_case_detail(
                [rec], self._addr_index(), {},
            )
        assert stats["considered"] == 1
        assert stats["skipped_resolved"] == 1
        assert stats["attempted"] == 0
        fetch_mock.assert_not_called()

    def test_skips_non_pb_records(self):
        rec = {
            "record_id": "315551161",
            "dallas_code": "NOF",
            "source": "publicsearch.us",
        }
        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            MockFetcher.return_value.__enter__.return_value = MagicMock()
            MockFetcher.return_value.__exit__.return_value = None
            stats = resolve_pb_via_tyler_case_detail(
                [rec], self._addr_index(), {},
            )
        assert stats["considered"] == 0

    def test_skips_publicsearch_pb_records(self):
        """publicsearch.us PB records don't have a Tyler case_data_id
        (their record_id is the publicsearch instrument id, not the
        Tyler caseDataID). The Tyler API would return nothing for them."""
        rec = {
            "record_id": "315553795",
            "dallas_code": "PB",
            "source": "publicsearch.us",
        }
        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            MockFetcher.return_value.__enter__.return_value = MagicMock()
            MockFetcher.return_value.__exit__.return_value = None
            stats = resolve_pb_via_tyler_case_detail(
                [rec], self._addr_index(), {},
            )
        # The record IS PB and not resolved — but source != Tyler so we skip
        assert stats["considered"] == 0

    def test_fetch_error_does_not_crash(self):
        """A page-fetch exception must not propagate — fail-open behavior."""
        rec = {
            "record_id": "pro-crash",
            "dallas_code": "PB",
            "source": "probate.txcourts.gov",
        }

        def crashing_fetcher(cid):
            raise RuntimeError("Playwright crashed")

        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            MockFetcher.return_value.__enter__.return_value = crashing_fetcher
            MockFetcher.return_value.__exit__.return_value = None
            stats = resolve_pb_via_tyler_case_detail(
                [rec], self._addr_index(), {},
            )
        assert stats["errors"] == 1
        assert stats["resolved"] == 0
        # Record left alone — no dcad_account set
        assert rec.get("dcad_account") is None

    def test_no_targets_skips_fetcher_init(self):
        """When there are no unresolved Tyler PBs, we shouldn't even
        instantiate the fetcher (saves Playwright startup cost)."""
        records = [
            {"record_id": "pro-done", "dallas_code": "PB",
             "source": "probate.txcourts.gov", "dcad_account": "x"},
        ]
        with patch("scraper.probate_case_detail.CaseDetailFetcher") as MockFetcher:
            stats = resolve_pb_via_tyler_case_detail(
                records, self._addr_index(), {},
            )
        MockFetcher.assert_not_called()
        assert stats["considered"] == 1
        assert stats["skipped_resolved"] == 1
