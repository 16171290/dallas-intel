"""
Regression tests for scraper.publicsearch.

Limited coverage — most of the module is Playwright-driven SPA
interaction that needs a live page. Unit tests focus on:
  - Circuit-breaker correctness
  - Polite-delay configuration
  - Date-window helpers
  - Record dataclass behavior

The actual SPA scrape is verified in Phase 3 (§D.3) against the
live publicsearch.us page.
"""

from datetime import date, timedelta

import pytest

from scraper import publicsearch, config


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker
# ═══════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_initial_state(self):
        cb = publicsearch.CircuitBreaker(threshold=3)
        assert cb.consecutive_failures == 0

    def test_failure_increments(self):
        cb = publicsearch.CircuitBreaker(threshold=3)
        cb.record_failure()
        assert cb.consecutive_failures == 1
        cb.record_failure()
        assert cb.consecutive_failures == 2

    def test_success_resets(self):
        cb = publicsearch.CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0

    def test_trips_at_threshold(self):
        cb = publicsearch.CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        # Third failure trips.
        with pytest.raises(publicsearch.CircuitBreakerTripped):
            cb.record_failure()

    def test_default_threshold_from_config(self):
        cb = publicsearch.CircuitBreaker()
        assert cb.threshold == config.CIRCUIT_BREAKER_THRESHOLD

    def test_intermittent_failures_do_not_trip(self):
        """Failures separated by successes never trip the breaker."""
        cb = publicsearch.CircuitBreaker(threshold=3)
        for _ in range(10):
            cb.record_failure()
            cb.record_failure()
            cb.record_success()  # resets
        # No exception raised → pass


# ═══════════════════════════════════════════════════════════════════════════
# Polite delay
# ═══════════════════════════════════════════════════════════════════════════

class TestPoliteDelay:
    def test_uses_configured_publicsearch_range(self, monkeypatch):
        called_with = []
        monkeypatch.setattr(
            publicsearch.time, "sleep",
            lambda s: called_with.append(s),
        )
        monkeypatch.setattr(
            publicsearch.random, "uniform",
            lambda lo, hi: (lo + hi) / 2,
        )

        publicsearch._polite_delay("publicsearch.us")
        lo, hi = config.RATE_LIMITS["publicsearch.us"]
        assert called_with == [(lo + hi) / 2]

    def test_unknown_host_falls_back_to_default(self, monkeypatch):
        called_with = []
        monkeypatch.setattr(
            publicsearch.time, "sleep",
            lambda s: called_with.append(s),
        )
        monkeypatch.setattr(
            publicsearch.random, "uniform",
            lambda lo, hi: lo,
        )
        publicsearch._polite_delay("unknown.example.com")
        # Falls back to the publicsearch.us default (2.0, 4.0) per signature
        assert called_with[0] == 2.0


# ═══════════════════════════════════════════════════════════════════════════
# Date window helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestDateWindows:
    def test_daily_window_default(self):
        f, t = publicsearch.daily_window()
        assert t == date.today()
        assert f == date.today() - timedelta(days=config.DAYS_BACK)

    def test_daily_window_explicit_days(self):
        f, t = publicsearch.daily_window(days_back=14)
        assert t == date.today()
        assert f == date.today() - timedelta(days=14)

    def test_backfill_window(self):
        f, t = publicsearch.backfill_window()
        assert t == date.today()
        assert f == date.today() - timedelta(days=config.BACKFILL_DAYS)

    def test_backfill_wider_than_daily(self):
        """Sanity: backfill should reach further back than daily window."""
        df, _ = publicsearch.daily_window()
        bf, _ = publicsearch.backfill_window()
        assert bf <= df


# ═══════════════════════════════════════════════════════════════════════════
# Record dataclass
# ═══════════════════════════════════════════════════════════════════════════

class TestPublicSearchRecord:
    def test_defaults(self):
        rec = publicsearch.PublicSearchRecord(record_id="X", dallas_code="LP")
        assert rec.record_id == "X"
        assert rec.dallas_code == "LP"
        assert rec.harris_category is None
        assert rec.parse_warnings == []

    def test_fields_populate(self):
        rec = publicsearch.PublicSearchRecord(
            record_id="123",
            dallas_code="NOF",
            filing_date="2026-05-13",
            instrument_num="2026000123",
            grantor="Bank of America",
            grantee="John Smith",
            address="100 MAIN ST",
            amount="250000",
        )
        assert rec.filing_date == "2026-05-13"
        assert rec.amount == "250000"

    def test_warnings_independent_per_instance(self):
        a = publicsearch.PublicSearchRecord(record_id="1", dallas_code="LP")
        b = publicsearch.PublicSearchRecord(record_id="2", dallas_code="LP")
        a.parse_warnings.append("flag")
        assert b.parse_warnings == []
