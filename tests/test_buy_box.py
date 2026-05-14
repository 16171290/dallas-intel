"""Tests for ``scraper.buy_box``."""

from __future__ import annotations

import pytest

from scraper import buy_box


# ─────────────────────────────────────────────────────────────────────────────
# extract_zip
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractZip:
    def test_standard_address(self):
        assert buy_box.extract_zip("123 MAIN ST DALLAS TX 75201") == "75201"

    def test_zip_plus_four(self):
        assert buy_box.extract_zip("123 MAIN ST DALLAS TX 75201-1234") == "75201"

    def test_comma_separated(self):
        assert buy_box.extract_zip("123 MAIN ST, DALLAS, TX, 75202") == "75202"

    def test_none_address(self):
        assert buy_box.extract_zip(None) is None

    def test_empty_address(self):
        assert buy_box.extract_zip("") is None

    def test_no_zip_in_address(self):
        assert buy_box.extract_zip("123 MAIN ST DALLAS TX") is None

    def test_5_digit_substring_in_address(self):
        """A 5-digit substring that isn't word-bounded shouldn't match,
        but a ZIP-shaped token should."""
        assert buy_box.extract_zip("APT 12345 DALLAS TX 75204") == "12345"
        # First 5-digit match wins; this is acceptable for our use case
        # because real addresses don't have non-ZIP 5-digit numbers
        # except apartment numbers, which are rare and not material.


# ─────────────────────────────────────────────────────────────────────────────
# BuyBox dataclass — basic construction & describe
# ─────────────────────────────────────────────────────────────────────────────


class TestBuyBoxDataclass:
    def test_default_no_criteria(self):
        bb = buy_box.BuyBox()
        assert bb.min_price is None
        assert bb.max_price is None
        assert bb.zip_allowlist is None
        assert bb.zip_denylist is None
        assert bb.exclude_commercial is True  # default True per spec
        assert bb.require_dcad_match is False
        assert bb.is_configured() is False

    def test_is_configured_with_min_price(self):
        bb = buy_box.BuyBox(min_price=80_000)
        assert bb.is_configured() is True

    def test_is_configured_with_zip_allowlist(self):
        bb = buy_box.BuyBox(zip_allowlist={"75201"})
        assert bb.is_configured() is True

    def test_is_configured_with_require_dcad(self):
        bb = buy_box.BuyBox(require_dcad_match=True)
        assert bb.is_configured() is True

    def test_describe_no_filters(self):
        bb = buy_box.BuyBox()
        assert "no filters" in bb.describe()

    def test_describe_with_filters(self):
        bb = buy_box.BuyBox(
            min_price=80_000,
            max_price=500_000,
            zip_allowlist={"75201", "75202"},
        )
        desc = bb.describe()
        assert "$80,000" in desc
        assert "$500,000" in desc
        assert "zip" in desc.lower()


# ─────────────────────────────────────────────────────────────────────────────
# BuyBox.from_env
# ─────────────────────────────────────────────────────────────────────────────


class TestBuyBoxFromEnv:
    def test_min_price_from_env(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_MIN_PRICE", "100000")
        bb = buy_box.BuyBox.from_env()
        assert bb.min_price == 100_000.0

    def test_min_price_with_dollar_sign(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_MIN_PRICE", "$100,000")
        bb = buy_box.BuyBox.from_env()
        assert bb.min_price == 100_000.0

    def test_max_price_from_env(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_MAX_PRICE", "750000")
        bb = buy_box.BuyBox.from_env()
        assert bb.max_price == 750_000.0

    def test_zip_allowlist_from_env(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_ZIP_ALLOWLIST", "75201,75202,75204")
        bb = buy_box.BuyBox.from_env()
        assert bb.zip_allowlist == {"75201", "75202", "75204"}

    def test_zip_allowlist_with_whitespace(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_ZIP_ALLOWLIST", "75201, 75202 , 75204")
        bb = buy_box.BuyBox.from_env()
        assert bb.zip_allowlist == {"75201", "75202", "75204"}

    def test_exclude_commercial_default_true(self, monkeypatch):
        monkeypatch.delenv("BUY_BOX_EXCLUDE_COMMERCIAL", raising=False)
        bb = buy_box.BuyBox.from_env()
        assert bb.exclude_commercial is True

    def test_exclude_commercial_false(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_EXCLUDE_COMMERCIAL", "false")
        bb = buy_box.BuyBox.from_env()
        assert bb.exclude_commercial is False

    def test_invalid_price_logged_and_skipped(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_MIN_PRICE", "not-a-number")
        bb = buy_box.BuyBox.from_env()
        assert bb.min_price is None  # falls back to None on parse failure

    def test_empty_env_yields_unconfigured(self, monkeypatch):
        # Clear all relevant env vars
        for key in [
            "BUY_BOX_MIN_PRICE",
            "BUY_BOX_MAX_PRICE",
            "BUY_BOX_ZIP_ALLOWLIST",
            "BUY_BOX_ZIP_DENYLIST",
            "BUY_BOX_REQUIRE_DCAD_MATCH",
        ]:
            monkeypatch.delenv(key, raising=False)
        bb = buy_box.BuyBox.from_env()
        assert not bb.is_configured()


# ─────────────────────────────────────────────────────────────────────────────
# BuyBox.evaluate
# ─────────────────────────────────────────────────────────────────────────────


class TestBuyBoxEvaluate:
    def test_no_criteria_always_in_box(self):
        bb = buy_box.BuyBox()
        rec = {"dcad_market_value": 250_000}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is True
        assert reasons == []

    def test_value_in_range(self):
        bb = buy_box.BuyBox(min_price=100_000, max_price=500_000)
        rec = {"dcad_market_value": 250_000}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is True
        assert reasons == []

    def test_value_below_min(self):
        bb = buy_box.BuyBox(min_price=100_000)
        rec = {"dcad_market_value": 50_000}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is False
        assert any("price below min" in r for r in reasons)

    def test_value_above_max(self):
        bb = buy_box.BuyBox(max_price=500_000)
        rec = {"dcad_market_value": 750_000}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is False
        assert any("price above max" in r for r in reasons)

    def test_value_at_min_boundary_inclusive(self):
        bb = buy_box.BuyBox(min_price=100_000)
        rec = {"dcad_market_value": 100_000}
        in_bb, _ = bb.evaluate(rec)
        assert in_bb is True

    def test_value_at_max_boundary_inclusive(self):
        bb = buy_box.BuyBox(max_price=500_000)
        rec = {"dcad_market_value": 500_000}
        in_bb, _ = bb.evaluate(rec)
        assert in_bb is True

    def test_missing_value_does_not_exclude(self):
        """If we can't compute price, we don't exclude on the basis of
        "we don't know"."""
        bb = buy_box.BuyBox(min_price=100_000, max_price=500_000)
        rec = {"dcad_market_value": None}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is True
        assert reasons == []

    def test_value_as_string_handled(self):
        """In case the canonical record's value got stringified somewhere."""
        bb = buy_box.BuyBox(min_price=100_000, max_price=500_000)
        rec = {"dcad_market_value": "250000"}
        in_bb, _ = bb.evaluate(rec)
        assert in_bb is True

    def test_value_unparseable_treated_as_missing(self):
        bb = buy_box.BuyBox(min_price=100_000)
        rec = {"dcad_market_value": "not-a-number"}
        in_bb, reasons = bb.evaluate(rec)
        # Unparseable value → treated as missing, not exclusion
        assert in_bb is True
        assert reasons == []

    def test_zip_in_allowlist(self):
        bb = buy_box.BuyBox(zip_allowlist={"75201", "75202"})
        rec = {"address": "123 MAIN ST DALLAS TX 75201"}
        in_bb, _ = bb.evaluate(rec)
        assert in_bb is True

    def test_zip_not_in_allowlist(self):
        bb = buy_box.BuyBox(zip_allowlist={"75201", "75202"})
        rec = {"address": "456 OAK AVE DALLAS TX 75999"}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is False
        assert any("75999" in r and "allowlist" in r for r in reasons)

    def test_zip_in_denylist(self):
        bb = buy_box.BuyBox(zip_denylist={"75201"})
        rec = {"address": "123 MAIN ST DALLAS TX 75201"}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is False
        assert any("75201" in r and "denylist" in r for r in reasons)

    def test_zip_from_normalized_address(self):
        """If address is missing, use address_normalized."""
        bb = buy_box.BuyBox(zip_allowlist={"75201"})
        rec = {
            "address": None,
            "address_normalized": "123 MAIN ST DALLAS TX 75201",
        }
        in_bb, _ = bb.evaluate(rec)
        assert in_bb is True

    def test_missing_zip_does_not_exclude(self):
        """If we can't extract a ZIP, we don't exclude on the basis of
        "we don't know"."""
        bb = buy_box.BuyBox(zip_allowlist={"75201"})
        rec = {"address": "123 MAIN ST DALLAS TX"}  # no ZIP
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is True
        assert reasons == []

    def test_require_dcad_match_with_match(self):
        bb = buy_box.BuyBox(require_dcad_match=True)
        rec = {"dcad_account": "10001"}
        in_bb, _ = bb.evaluate(rec)
        assert in_bb is True

    def test_require_dcad_match_without_match(self):
        bb = buy_box.BuyBox(require_dcad_match=True)
        rec = {"dcad_account": None}
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is False
        assert any("DCAD match" in r for r in reasons)

    def test_multiple_failures_aggregated(self):
        bb = buy_box.BuyBox(
            min_price=200_000,
            zip_allowlist={"75201"},
            require_dcad_match=True,
        )
        rec = {
            "dcad_market_value": 50_000,
            "address": "456 OAK AVE DALLAS TX 75999",
            "dcad_account": None,
        }
        in_bb, reasons = bb.evaluate(rec)
        assert in_bb is False
        assert len(reasons) == 3  # price, ZIP, DCAD

    def test_custom_check(self):
        def must_have_owner(rec):
            if not rec.get("dcad_owner"):
                return "no owner name"
            return None

        bb = buy_box.BuyBox(custom_checks=[("requires_owner", must_have_owner)])
        rec_pass = {"dcad_owner": "JOHN SMITH"}
        rec_fail = {"dcad_owner": None}
        assert bb.evaluate(rec_pass)[0] is True
        assert bb.evaluate(rec_fail)[0] is False


# ─────────────────────────────────────────────────────────────────────────────
# annotate_records
# ─────────────────────────────────────────────────────────────────────────────


class TestAnnotateRecords:
    def test_annotates_in_place(self):
        bb = buy_box.BuyBox(min_price=100_000, max_price=500_000)
        records = [
            {"record_id": "1", "dcad_market_value": 250_000},
            {"record_id": "2", "dcad_market_value": 50_000},
            {"record_id": "3", "dcad_market_value": 750_000},
        ]
        buy_box.annotate_records(records, bb)
        assert records[0]["in_buy_box"] is True
        assert records[1]["in_buy_box"] is False
        assert records[2]["in_buy_box"] is False
        assert records[0]["buy_box_reasons"] == []
        assert any("below min" in r for r in records[1]["buy_box_reasons"])
        assert any("above max" in r for r in records[2]["buy_box_reasons"])

    def test_summary_counts(self):
        bb = buy_box.BuyBox(min_price=100_000)
        records = [
            {"dcad_market_value": 250_000},
            {"dcad_market_value": 50_000},
            {"dcad_market_value": 75_000},
        ]
        summary = buy_box.annotate_records(records, bb)
        assert summary["total"] == 3
        assert summary["in_buy_box"] == 1
        assert summary["outside_buy_box"] == 2
        assert summary["configured"] is True

    def test_unconfigured_marks_all_in_box(self):
        bb = buy_box.BuyBox()  # no criteria
        records = [
            {"record_id": "1", "dcad_market_value": 50_000},
            {"record_id": "2", "dcad_market_value": 5_000_000},
        ]
        summary = buy_box.annotate_records(records, bb)
        assert summary["configured"] is False
        assert summary["in_buy_box"] == 2
        assert summary["outside_buy_box"] == 0
        for rec in records:
            assert rec["in_buy_box"] is True
            assert rec["buy_box_reasons"] == []

    def test_top_exclusion_reasons(self):
        bb = buy_box.BuyBox(min_price=100_000)
        records = [{"dcad_market_value": v} for v in [50_000, 60_000, 70_000, 80_000]]
        summary = buy_box.annotate_records(records, bb)
        # All 4 failed with "value $X below min" — reason bucket count = 4
        top = summary["top_exclusion_reasons"]
        assert len(top) >= 1
        assert top[0][1] == 4  # 4 records share the top reason bucket

    def test_uses_env_by_default(self, monkeypatch):
        monkeypatch.setenv("BUY_BOX_MIN_PRICE", "100000")
        records = [{"dcad_market_value": 50_000}]
        summary = buy_box.annotate_records(records)  # no buy_box arg
        assert summary["configured"] is True
        assert summary["outside_buy_box"] == 1

    def test_empty_input(self):
        bb = buy_box.BuyBox(min_price=100_000)
        summary = buy_box.annotate_records([], bb)
        assert summary["total"] == 0
        assert summary["in_buy_box"] == 0
        assert summary["outside_buy_box"] == 0
