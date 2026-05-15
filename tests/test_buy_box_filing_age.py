"""Tests for the filing-date-max-age criterion in scraper.buy_box.

Phase 3.E semantic shift (2026-05-15):
    The strict "missing date = excluded" behavior from Phase 3.D was
    operationally wrong: parser uncertainty (PDF text without a clear
    "Dated:" line) is not lead invalidity. The current foreclosure
    index contains records we just can't date-extract from. Those
    are still actionable.

    New semantic: only CONFIRMED-stale records get excluded.
    Missing/unparseable filing dates pass through (in-buy-box).
"""

from __future__ import annotations

import os
from datetime import date
from unittest import mock

import pytest

from scraper import buy_box


# ============================================================================
# _parse_iso_date
# ============================================================================


class TestParseIsoDate:
    def test_valid_iso(self):
        assert buy_box._parse_iso_date("2026-05-15") == date(2026, 5, 15)
        assert buy_box._parse_iso_date("2026-01-01") == date(2026, 1, 1)

    def test_alternate_separator(self):
        assert buy_box._parse_iso_date("2026/05/15") == date(2026, 5, 15)

    def test_empty_returns_none(self):
        assert buy_box._parse_iso_date(None) is None
        assert buy_box._parse_iso_date("") is None
        assert buy_box._parse_iso_date("   ") is None

    def test_garbage_returns_none(self):
        assert buy_box._parse_iso_date("not a date") is None
        assert buy_box._parse_iso_date("2026") is None
        assert buy_box._parse_iso_date("2026-13-01") is None  # invalid month

    def test_already_date_object(self):
        d = date(2026, 5, 15)
        assert buy_box._parse_iso_date(d) == d


# ============================================================================
# BuyBox.from_env reads the new env var
# ============================================================================


class TestBuyBoxFromEnv:
    def test_filing_date_max_age_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            bb = buy_box.BuyBox.from_env()
        assert bb.filing_date_max_age_days is None

    def test_filing_date_max_age_30(self):
        with mock.patch.dict(
            os.environ,
            {"BUY_BOX_FILING_DATE_MAX_AGE_DAYS": "30"},
            clear=True,
        ):
            bb = buy_box.BuyBox.from_env()
        assert bb.filing_date_max_age_days == 30

    def test_filing_date_max_age_bad_value_ignored(self):
        with mock.patch.dict(
            os.environ,
            {"BUY_BOX_FILING_DATE_MAX_AGE_DAYS": "not_a_number"},
            clear=True,
        ):
            bb = buy_box.BuyBox.from_env()
        assert bb.filing_date_max_age_days is None


# ============================================================================
# describe() and is_configured()
# ============================================================================


class TestBuyBoxDescribeIsConfigured:
    def test_filing_date_in_describe(self):
        bb = buy_box.BuyBox(filing_date_max_age_days=30)
        desc = bb.describe()
        # Phase 3.E describe uses "reject-if-filed>Nd-ago" to make the
        # permissive semantic explicit (only excludes confirmed-old dates).
        assert "reject-if-filed>30d-ago" in desc

    def test_describe_no_unicode(self):
        # Regression: ASCII-only output to avoid cp1252 logging crash.
        bb = buy_box.BuyBox(
            min_price=80000.0,
            max_price=750000.0,
            filing_date_max_age_days=30,
        )
        desc = bb.describe()
        # Verify it encodes as cp1252 without exception.
        desc.encode("cp1252")

    def test_is_configured_with_filing_age(self):
        bb = buy_box.BuyBox(filing_date_max_age_days=30)
        assert bb.is_configured() is True

    def test_is_configured_without_anything(self):
        bb = buy_box.BuyBox()
        assert bb.is_configured() is False


# ============================================================================
# evaluate() - filing-date age semantics (Phase 3.E permissive)
# ============================================================================


class TestEvaluateFilingDateAge:
    def setup_method(self):
        self.today = date(2026, 5, 15)
        self.bb = buy_box.BuyBox(filing_date_max_age_days=30)

    def test_fresh_record_accepted(self):
        # 14 days old - well within 30 days.
        rec = {"filing_date": "2026-05-01"}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is True
        assert reasons == []

    def test_boundary_exactly_30_days_accepted(self):
        # Exactly 30 days ago - should be accepted.
        rec = {"filing_date": "2026-04-15"}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is True

    def test_boundary_31_days_rejected(self):
        # 31 days ago - one day too old.
        rec = {"filing_date": "2026-04-14"}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is False
        assert any("filing date too old" in r for r in reasons)

    def test_old_record_rejected(self):
        # Months old, still has a parseable date.
        rec = {"filing_date": "2026-02-01"}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is False
        assert any("filing date too old" in r for r in reasons)

    def test_missing_filing_date_PASSES_phase_3e_semantic(self):
        # Phase 3.E: missing filing_date is no longer grounds for
        # exclusion. Parser uncertainty != lead invalidity.
        rec = {"filing_date": None}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is True
        assert reasons == []

    def test_empty_string_filing_date_PASSES_phase_3e_semantic(self):
        # Phase 3.E: empty-string filing_date passes through.
        rec = {"filing_date": ""}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is True
        assert reasons == []

    def test_garbage_filing_date_PASSES_phase_3e_semantic(self):
        # Phase 3.E: unparseable date passes through (treated as unknown,
        # not as invalid).
        rec = {"filing_date": "not a real date"}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is True
        assert reasons == []

    def test_no_filing_age_check_when_unset(self):
        bb = buy_box.BuyBox()  # filing_date_max_age_days defaults to None
        rec = {"filing_date": None}
        in_bb, reasons = bb.evaluate(rec, today=self.today)
        # No criteria configured at all - everything passes.
        assert in_bb is True

    def test_future_filing_date_accepted(self):
        # Future dates are not "too old" by definition.
        rec = {"filing_date": "2026-06-01"}
        in_bb, reasons = self.bb.evaluate(rec, today=self.today)
        assert in_bb is True

    def test_only_confirmed_old_excluded(self):
        # Sanity check on the core Phase 3.E semantic with a batch.
        old_with_date    = {"filing_date": "2026-01-01"}  # excluded
        recent_with_date = {"filing_date": "2026-05-10"}  # in
        no_date          = {"filing_date": None}          # in (was excluded in 3.D)
        bad_date         = {"filing_date": "garbage"}     # in (was excluded in 3.D)

        for rec in [old_with_date, recent_with_date, no_date, bad_date]:
            in_bb, reasons = self.bb.evaluate(rec, today=self.today)
            rec["_result"] = in_bb

        assert old_with_date["_result"]    is False, "confirmed-old should still exclude"
        assert recent_with_date["_result"] is True,  "confirmed-fresh should pass"
        assert no_date["_result"]          is True,  "missing date now passes (3.E)"
        assert bad_date["_result"]         is True,  "unparseable now passes (3.E)"


# ============================================================================
# Composition with other criteria
# ============================================================================


class TestCriteriaComposition:
    def test_price_and_age_both_required(self):
        bb = buy_box.BuyBox(
            min_price=80000.0,
            max_price=750000.0,
            filing_date_max_age_days=30,
        )
        today = date(2026, 5, 15)

        # Record A: good price, fresh date -> in
        rec_a = {"dcad_market_value": 250000.0, "filing_date": "2026-05-10"}
        in_a, _ = bb.evaluate(rec_a, today=today)
        assert in_a is True

        # Record B: good price, confirmed-old date -> out
        rec_b = {"dcad_market_value": 250000.0, "filing_date": "2026-01-01"}
        in_b, reasons_b = bb.evaluate(rec_b, today=today)
        assert in_b is False
        assert any("filing date too old" in r for r in reasons_b)

        # Record C: bad price, fresh date -> out (price exclusion)
        rec_c = {"dcad_market_value": 50000.0, "filing_date": "2026-05-10"}
        in_c, reasons_c = bb.evaluate(rec_c, today=today)
        assert in_c is False
        assert any("price below min" in r for r in reasons_c)

        # Record D: bad price AND old date -> out (both reasons listed)
        rec_d = {"dcad_market_value": 50000.0, "filing_date": "2026-01-01"}
        in_d, reasons_d = bb.evaluate(rec_d, today=today)
        assert in_d is False
        assert any("price below min" in r for r in reasons_d)
        assert any("filing date too old" in r for r in reasons_d)

        # Record E: good price, MISSING date -> IN (3.E semantic)
        rec_e = {"dcad_market_value": 250000.0, "filing_date": None}
        in_e, reasons_e = bb.evaluate(rec_e, today=today)
        assert in_e is True
        assert reasons_e == []

        # Record F: bad price, missing date -> out (price only)
        rec_f = {"dcad_market_value": 50000.0, "filing_date": None}
        in_f, reasons_f = bb.evaluate(rec_f, today=today)
        assert in_f is False
        assert any("price below min" in r for r in reasons_f)
        # Confirm filing-date didn't add a reason for this record
        assert not any("filing date" in r for r in reasons_f)


# ============================================================================
# annotate_records integration
# ============================================================================


class TestAnnotateRecords:
    def test_annotate_propagates_today_3e_semantic(self):
        bb = buy_box.BuyBox(filing_date_max_age_days=30)
        today = date(2026, 5, 15)

        recs = [
            {"record_id": "fresh",   "filing_date": "2026-05-10"},
            {"record_id": "stale",   "filing_date": "2026-01-01"},
            {"record_id": "missing", "filing_date": None},
        ]

        summary = buy_box.annotate_records(recs, bb, today=today)

        # Read back per-record flags.
        by_id = {r["record_id"]: r for r in recs}
        assert by_id["fresh"]["in_buy_box"]   is True
        assert by_id["stale"]["in_buy_box"]   is False
        # Phase 3.E: missing date passes through, in buy-box.
        assert by_id["missing"]["in_buy_box"] is True

        assert summary["total"] == 3
        assert summary["in_buy_box"] == 2    # fresh + missing
        assert summary["outside_buy_box"] == 1   # only confirmed-stale
