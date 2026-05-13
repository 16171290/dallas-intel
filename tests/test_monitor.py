"""
Regression tests for scraper.monitor.

Critical invariant: the webhook URL is never logged. The test suite
captures logs during simulated failures and asserts the URL doesn't
appear in any record.
"""

import logging

import pytest
import requests

from scraper import config, monitor


# ═══════════════════════════════════════════════════════════════════════════
# Webhook-presence detection
# ═══════════════════════════════════════════════════════════════════════════

class TestWebhookConfigured:
    def test_empty_url_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
        assert monitor._webhook_configured() is False

    def test_non_discord_url_disabled(self, monkeypatch):
        """A non-Discord URL is rejected (catches misconfiguration)."""
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL",
                            "https://example.com/webhook")
        assert monitor._webhook_configured() is False

    def test_discord_url_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL",
                            "https://discord.com/api/webhooks/123/abc")
        assert monitor._webhook_configured() is True


# ═══════════════════════════════════════════════════════════════════════════
# Notify methods — no-op when unconfigured
# ═══════════════════════════════════════════════════════════════════════════

class TestNotifyNoop:
    def test_complete_noop_when_unconfigured(self, monkeypatch):
        """notify_run_complete should not POST when webhook is empty."""
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")

        posted: list = []
        def fake_post(*a, **kw):
            posted.append((a, kw))
            return None
        monkeypatch.setattr(requests, "post", fake_post)

        monitor.notify_run_complete({"total": 0, "active": 0})
        assert posted == []

    def test_failure_noop_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")

        posted: list = []
        monkeypatch.setattr(requests, "post",
                            lambda *a, **kw: posted.append((a, kw)))

        monitor.notify_failure("test error", {})
        assert posted == []


# ═══════════════════════════════════════════════════════════════════════════
# Payload structure
# ═══════════════════════════════════════════════════════════════════════════

class _FakeResponse:
    """Minimal stand-in for requests.Response."""
    status_code = 204
    reason = "No Content"
    def raise_for_status(self):
        pass


class TestPayloadStructure:
    def test_success_embed_shape(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL",
                            "https://discord.com/api/webhooks/123/abc")
        captured: dict = {}
        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"]     = url
            captured["json"]    = json
            captured["headers"] = headers
            return _FakeResponse()
        monkeypatch.setattr(requests, "post", fake_post)

        summary = {
            "total":                    100,
            "active":                   95,
            "address_resolution_rate":  0.92,
            "by_category":              {"L/P": 50, "NOTICE": 30},
            "by_score_band":            {"HIGH": 20, "MEDIUM": 30},
            "hoa_filtered_count":       5,
        }
        monitor.notify_run_complete(summary)

        # Posted to the configured URL.
        assert captured["url"] == config.DISCORD_WEBHOOK_URL
        # Embed is a list with one item.
        embeds = captured["json"]["embeds"]
        assert len(embeds) == 1
        embed = embeds[0]
        assert "title"     in embed
        assert "color"     in embed
        assert "description" in embed
        assert "fields"    in embed

    def test_failure_embed_shape(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL",
                            "https://discord.com/api/webhooks/123/abc")
        captured: dict = {}
        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeResponse()
        monkeypatch.setattr(requests, "post", fake_post)

        monitor.notify_failure(
            "Something exploded",
            context={"stage": "publicsearch", "exception_type": "TimeoutError"},
        )

        embed = captured["json"]["embeds"][0]
        assert "failed" in embed["title"].lower()
        assert embed["description"] == "Something exploded"
        # Context fields present.
        field_names = {f["name"] for f in embed["fields"]}
        assert "stage"          in field_names
        assert "exception_type" in field_names

    def test_amber_color_on_low_hit_rate(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL",
                            "https://discord.com/api/webhooks/123/abc")
        captured: dict = {}
        monkeypatch.setattr(requests, "post",
                            lambda url, json=None, headers=None, timeout=None: (
                                captured.update(json=json) or _FakeResponse()
                            ))
        monitor.notify_run_complete({
            "total": 100, "active": 100,
            "address_resolution_rate": 0.50,  # below 85% threshold
        })
        # Amber color when hit rate is degraded.
        assert captured["json"]["embeds"][0]["color"] == 0xF39C12

    def test_failure_context_value_truncated(self, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL",
                            "https://discord.com/api/webhooks/123/abc")
        captured: dict = {}
        monkeypatch.setattr(requests, "post",
                            lambda url, json=None, headers=None, timeout=None: (
                                captured.update(json=json) or _FakeResponse()
                            ))
        long_value = "X" * 5000  # exceeds Discord's 1024 char limit
        monitor.notify_failure("err", context={"traceback": long_value})
        # Value should be truncated, not raise.
        field_value = captured["json"]["embeds"][0]["fields"][0]["value"]
        assert len(field_value) <= 1000


# ═══════════════════════════════════════════════════════════════════════════
# Critical invariant: webhook URL never logged
# ═══════════════════════════════════════════════════════════════════════════

class TestWebhookUrlNeverLogged:
    def test_url_not_in_logs_on_success(self, monkeypatch, caplog):
        secret_url = "https://discord.com/api/webhooks/123/SUPER_SECRET_TOKEN"
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", secret_url)
        monkeypatch.setattr(requests, "post",
                            lambda *a, **kw: _FakeResponse())

        with caplog.at_level(logging.DEBUG):
            monitor.notify_run_complete({"total": 1, "active": 1})

        # The URL must not appear in any log record.
        for record in caplog.records:
            assert "SUPER_SECRET_TOKEN" not in record.getMessage()

    def test_url_not_in_logs_on_failure(self, monkeypatch, caplog):
        secret_url = "https://discord.com/api/webhooks/999/ULTRA_SECRET"
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", secret_url)

        class FailingResponse:
            status_code = 500
            reason = "Internal Server Error"
            def raise_for_status(self):
                raise requests.HTTPError(response=self)

        monkeypatch.setattr(requests, "post",
                            lambda *a, **kw: FailingResponse())

        with caplog.at_level(logging.DEBUG):
            monitor.notify_run_complete({"total": 1, "active": 1})

        for record in caplog.records:
            assert "ULTRA_SECRET" not in record.getMessage()

    def test_url_not_in_logs_on_network_error(self, monkeypatch, caplog):
        secret_url = "https://discord.com/api/webhooks/777/NETWORK_TOKEN"
        monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", secret_url)

        def fail(*a, **kw):
            raise requests.ConnectionError("connection refused")
        monkeypatch.setattr(requests, "post", fail)

        with caplog.at_level(logging.DEBUG):
            monitor.notify_run_complete({"total": 1, "active": 1})

        for record in caplog.records:
            assert "NETWORK_TOKEN" not in record.getMessage()
