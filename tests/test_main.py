"""Unit tests for scraper.main module-level helpers."""
from __future__ import annotations

from scraper.main import apply_grantor_fallback_from_dcad_owner
from scraper.resolution import WARN_GRANTOR_FROM_DCAD, get_warnings


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
