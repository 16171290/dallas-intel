"""Regression tests for scraper.scorer."""

from datetime import date, timedelta

import pytest

from scraper import config, scorer


# ═══════════════════════════════════════════════════════════════════════════
# Per-record scoring
# ═══════════════════════════════════════════════════════════════════════════

class TestScoreRecord:
    def test_minimum_score_is_base_plus_one_category(self):
        rec = {"category": "LIEN"}
        score, breakdown = scorer.score_record(rec)
        # 30 base + 10 category flag
        assert score == config.SCORE_BASE + config.SCORE_PER_CATEGORY_FLAG
        assert breakdown["base"]          == config.SCORE_BASE
        assert breakdown["category_flag"] == config.SCORE_PER_CATEGORY_FLAG

    def test_value_band_high(self):
        rec = {"category": "LIEN", "dcad_market_value": 250_000.0}
        score, breakdown = scorer.score_record(rec)
        assert breakdown.get("value_100k") == config.SCORE_VALUE_100K
        assert "value_50k" not in breakdown

    def test_value_band_mid(self):
        rec = {"category": "LIEN", "dcad_market_value": 75_000.0}
        score, breakdown = scorer.score_record(rec)
        assert breakdown.get("value_50k") == config.SCORE_VALUE_50K
        assert "value_100k" not in breakdown

    def test_value_band_below_threshold(self):
        rec = {"category": "LIEN", "dcad_market_value": 25_000.0}
        score, breakdown = scorer.score_record(rec)
        assert "value_50k"  not in breakdown
        assert "value_100k" not in breakdown

    def test_new_this_week(self):
        today = date.today()
        rec = {
            "category":   "LIEN",
            "first_seen": (today - timedelta(days=2)).isoformat(),
        }
        score, breakdown = scorer.score_record(rec, today=today)
        assert breakdown.get("new_this_week") == config.SCORE_NEW_THIS_WEEK

    def test_old_record_not_new(self):
        today = date.today()
        rec = {
            "category":   "LIEN",
            "first_seen": (today - timedelta(days=30)).isoformat(),
        }
        score, breakdown = scorer.score_record(rec, today=today)
        assert "new_this_week" not in breakdown

    def test_has_dcad_address(self):
        rec = {"category": "LIEN", "dcad_account": "10001"}
        score, breakdown = scorer.score_record(rec)
        assert breakdown.get("has_address") == config.SCORE_HAS_ADDRESS

    def test_no_dcad_account(self):
        rec = {"category": "LIEN", "dcad_account": None}
        score, breakdown = scorer.score_record(rec)
        assert "has_address" not in breakdown

    def test_score_capped_at_100(self):
        today = date.today()
        rec = {
            "category":          "LIEN",
            "dcad_market_value": 500_000.0,
            "first_seen":        today.isoformat(),
            "dcad_account":      "10001",
        }
        # 30 + 10 + 15 + 5 + 5 = 65 — well under 100, so no cap test here.
        # Cap is tested via stacking which can push above 100.
        score, _ = scorer.score_record(rec, today=today)
        assert score == 65


# ═══════════════════════════════════════════════════════════════════════════
# Stacking
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyStacking:
    def test_no_stacking_with_single_record(self):
        records = [
            {"address_normalized": "100 MAIN ST", "category": "LIEN", "score": 40,
             "score_breakdown": {}},
        ]
        scorer.apply_stacking(records)
        assert records[0]["score"] == 40
        assert "stacking" not in records[0]["score_breakdown"]

    def test_two_categories_same_address_stacks(self):
        records = [
            {"address_normalized": "100 MAIN ST", "category": "LIEN",
             "score": 40, "score_breakdown": {}},
            {"address_normalized": "100 MAIN ST", "category": "DEED",
             "score": 40, "score_breakdown": {}},
        ]
        scorer.apply_stacking(records)
        # Both should receive the stacking bonus.
        assert records[0]["score_breakdown"].get("stacking") == config.SCORE_STACKING_ADDRESS_MATCH
        assert records[1]["score_breakdown"].get("stacking") == config.SCORE_STACKING_ADDRESS_MATCH
        assert records[0]["score"] == 40 + config.SCORE_STACKING_ADDRESS_MATCH

    def test_lp_fc_combo(self):
        records = [
            {"address_normalized": "100 MAIN ST", "category": "L/P",
             "score": 40, "score_breakdown": {}},
            {"address_normalized": "100 MAIN ST", "category": "NOTICE",
             "score": 40, "score_breakdown": {}},
        ]
        scorer.apply_stacking(records)
        # Both records get both bonuses.
        for rec in records:
            assert rec["score_breakdown"].get("stacking")  == config.SCORE_STACKING_ADDRESS_MATCH
            assert rec["score_breakdown"].get("lp_fc_combo") == config.SCORE_LP_FC_COMBO

    def test_score_capped_after_stacking(self):
        records = [
            {"address_normalized": "100 MAIN ST", "category": "L/P",
             "score": 80, "score_breakdown": {}},
            {"address_normalized": "100 MAIN ST", "category": "NOTICE",
             "score": 80, "score_breakdown": {}},
        ]
        scorer.apply_stacking(records)
        # 80 + 30 + 20 = 130 → capped at 100
        assert records[0]["score"] == config.SCORE_CAP

    def test_different_addresses_no_stacking(self):
        records = [
            {"address_normalized": "100 MAIN ST", "category": "LIEN",
             "score": 40, "score_breakdown": {}},
            {"address_normalized": "200 ELM AVE", "category": "DEED",
             "score": 40, "score_breakdown": {}},
        ]
        scorer.apply_stacking(records)
        assert records[0]["score"] == 40
        assert records[1]["score"] == 40


# ═══════════════════════════════════════════════════════════════════════════
# REL suppression
# ═══════════════════════════════════════════════════════════════════════════

class TestSuppressReleased:
    def test_rel_marks_prior_record_inactive(self):
        records = [
            {"record_id": "A", "dallas_code": "LP",  "address_normalized": "100 MAIN ST",
             "filing_date": "2026-01-15", "active": True},
            {"record_id": "B", "dallas_code": "RLP", "address_normalized": "100 MAIN ST",
             "filing_date": "2026-03-20", "active": True},
        ]
        kept = scorer.suppress_released(records)
        # RLP record is removed from output; LP is marked inactive.
        assert len(kept) == 1
        assert kept[0]["record_id"] == "A"
        assert kept[0]["active"]    is False
        assert kept[0]["release_record_id"] == "B"

    def test_rel_does_not_suppress_later_records(self):
        records = [
            {"record_id": "A", "dallas_code": "LP",  "address_normalized": "100 MAIN ST",
             "filing_date": "2026-05-15", "active": True},
            {"record_id": "B", "dallas_code": "RLP", "address_normalized": "100 MAIN ST",
             "filing_date": "2026-03-20", "active": True},
        ]
        kept = scorer.suppress_released(records)
        # The LP filed AFTER the RLP is not suppressed.
        assert len(kept) == 1
        assert kept[0]["active"] is True

    def test_rel_only_suppresses_same_address(self):
        records = [
            {"record_id": "A", "dallas_code": "LP",  "address_normalized": "100 MAIN ST",
             "filing_date": "2026-01-15", "active": True},
            {"record_id": "C", "dallas_code": "LP",  "address_normalized": "999 OTHER ST",
             "filing_date": "2026-01-15", "active": True},
            {"record_id": "B", "dallas_code": "RLP", "address_normalized": "100 MAIN ST",
             "filing_date": "2026-03-20", "active": True},
        ]
        kept = scorer.suppress_released(records)
        addr_to_active = {r["address_normalized"]: r["active"] for r in kept}
        assert addr_to_active["100 MAIN ST"]  is False
        assert addr_to_active["999 OTHER ST"] is True

    def test_no_rels(self):
        records = [
            {"record_id": "A", "dallas_code": "LP", "address_normalized": "100 MAIN ST",
             "filing_date": "2026-01-15", "active": True},
        ]
        kept = scorer.suppress_released(records)
        assert len(kept) == 1
        assert kept[0]["active"] is True


# ═══════════════════════════════════════════════════════════════════════════
# HOA filter — §3.3.1 fix verification
# ═══════════════════════════════════════════════════════════════════════════

class TestFilterHoa:
    def test_hoa_alone_filtered(self):
        """HOA grantor + no grantee → DROP."""
        records = [
            {"grantor": "Highland Park HOA", "grantee": ""},
        ]
        kept, dropped = scorer.filter_hoa(records)
        assert dropped == 1
        assert kept    == []

    def test_hoa_vs_individual_kept(self):
        """HOA grantor + individual grantee → KEEP (homeowner is the lead)."""
        records = [
            {"grantor": "Highland Park HOA", "grantee": "John Smith"},
        ]
        kept, dropped = scorer.filter_hoa(records)
        assert dropped == 0
        assert len(kept) == 1

    def test_two_hoas_filtered(self):
        records = [
            {"grantor": "Highland Park HOA", "grantee": "Master Community Association"},
        ]
        kept, dropped = scorer.filter_hoa(records)
        assert dropped == 1

    def test_bank_vs_individual_kept(self):
        """Non-HOA grantor — keep regardless."""
        records = [
            {"grantor": "Bank of America", "grantee": "John Smith"},
        ]
        kept, dropped = scorer.filter_hoa(records)
        assert dropped == 0

    def test_no_grantor_at_all(self):
        records = [
            {"grantor": "", "grantee": "John Smith"},
        ]
        kept, dropped = scorer.filter_hoa(records)
        assert dropped == 0


# ═══════════════════════════════════════════════════════════════════════════
# Full pipeline integration
# ═══════════════════════════════════════════════════════════════════════════

class TestScoreAndFilter:
    def test_empty_input(self):
        records, summary = scorer.score_and_filter([])
        assert records == []
        assert summary["input"] == 0
        assert summary["final"] == 0

    def test_full_pipeline(self):
        today = date.today()
        records = [
            {
                "record_id": "A",
                "dallas_code": "LP",
                "category": "L/P",
                "address_normalized": "100 MAIN ST",
                "grantor": "Bank of America",
                "grantee": "John Smith",
                "filing_date": "2026-05-13",
                "first_seen": today.isoformat(),
                "dcad_account": "10001",
                "dcad_market_value": 250000.0,
                "active": True,
            },
            {
                "record_id": "B",
                "dallas_code": "NOF",
                "category": "NOTICE",
                "address_normalized": "100 MAIN ST",
                "grantor": "ABC Trustee Corp",
                "grantee": "John Smith",
                "filing_date": "2026-05-13",
                "first_seen": today.isoformat(),
                "dcad_account": "10001",
                "dcad_market_value": 250000.0,
                "active": True,
            },
            # HOA-only record — should drop
            {
                "record_id": "C",
                "dallas_code": "LN",
                "category": "LIEN",
                "address_normalized": "200 ELM AVE",
                "grantor": "Some Owners Association",
                "grantee": "",
                "filing_date": "2026-05-13",
                "first_seen": today.isoformat(),
                "active": True,
            },
        ]
        kept, summary = scorer.score_and_filter(records)
        # Two records survive.
        assert summary["input"] == 3
        assert summary["final"] == 2
        assert summary["hoa_filtered_count"] == 1
        # Both surviving records hit the score cap (LP+FC + stacking).
        for rec in kept:
            assert rec["score"] == config.SCORE_CAP
