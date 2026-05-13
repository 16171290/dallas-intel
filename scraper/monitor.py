"""
Discord webhook notifier for run completion and failure events.

The webhook URL is read from ``config.DISCORD_WEBHOOK_URL`` which sources
``$env:DISCORD_WEBHOOK_URL`` (a GitHub Actions secret in production). The
URL itself is never logged — only its presence/absence.

If ``DISCORD_WEBHOOK_URL`` is empty (typical for local-dev runs without
a webhook), notifications are no-ops and a single INFO log is emitted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)

# Discord embed color bytes (decimal).
_COLOR_GREEN = 0x2ECC71   # success
_COLOR_RED   = 0xE74C3C   # failure
_COLOR_AMBER = 0xF39C12   # warning


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def notify_run_complete(summary: dict[str, Any]) -> None:
    """Post a run-summary embed to Discord.

    ``summary`` is the dict produced by :func:`output.summarize_run`.
    Silently no-ops if no webhook is configured.
    """
    if not _webhook_configured():
        return

    address_rate = summary.get("address_resolution_rate", 0.0)
    color = _COLOR_GREEN
    if address_rate < 0.85:  # hit-rate target per §F.3.1
        color = _COLOR_AMBER

    embed = {
        "title":       "✅ dallas-intel run complete",
        "color":       color,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "description": _summary_description(summary),
        "fields":      _summary_fields(summary),
        "footer":      {"text": _footer_text()},
    }
    _post_embed(embed)


def notify_failure(error: str, context: dict[str, Any] | None = None) -> None:
    """Post a failure embed to Discord.

    ``error`` is a short headline; ``context`` provides structured detail
    (e.g. which stage failed, exception type). Silently no-ops if no
    webhook is configured.
    """
    if not _webhook_configured():
        return

    fields: list[dict[str, Any]] = []
    if context:
        for k, v in context.items():
            # Keep field values under Discord's 1024-char per-value limit.
            value_str = str(v)
            if len(value_str) > 1000:
                value_str = value_str[:997] + "..."
            fields.append({"name": str(k), "value": value_str, "inline": False})

    embed = {
        "title":       "🚨 dallas-intel run failed",
        "color":       _COLOR_RED,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "description": error[:2000],
        "fields":      fields,
        "footer":      {"text": _footer_text()},
    }
    _post_embed(embed)


# ═══════════════════════════════════════════════════════════════════════════
# Internals
# ═══════════════════════════════════════════════════════════════════════════

def _webhook_configured() -> bool:
    """True iff a non-empty webhook URL is in config. URL itself is not logged."""
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        logger.info("Discord webhook not configured; notifications disabled")
        return False
    # Cheap sanity check — Discord webhook URLs all start with this prefix.
    return url.startswith("https://discord.com/api/webhooks/")


def _post_embed(embed: dict[str, Any]) -> None:
    """POST a single embed to the webhook. Errors are logged but never raised."""
    payload = {"embeds": [embed]}
    try:
        # Timeout short — Discord's response is non-critical; we don't
        # want notifier hiccups to mask the actual pipeline outcome.
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json=payload,
            headers={"User-Agent": config.USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Never log the URL — log status code and reason only.
        logger.warning(
            "Discord webhook POST returned %s %s",
            getattr(e.response, "status_code", "?"),
            getattr(e.response, "reason", ""),
        )
    except requests.RequestException as e:
        logger.warning("Discord webhook POST failed: %s", type(e).__name__)


def _summary_description(summary: dict[str, Any]) -> str:
    total = summary.get("total", 0)
    active = summary.get("active", 0)
    rate = summary.get("address_resolution_rate", 0.0)
    return (
        f"**{active}** active records ({total} total). "
        f"Address resolution: **{rate * 100:.1f}%**."
    )


def _summary_fields(summary: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []

    if by_cat := summary.get("by_category"):
        # Sort categories by count descending; limit to top 12 to fit embed.
        items = sorted(by_cat.items(), key=lambda kv: -int(kv[1]))[:12]
        lines = [f"`{cat:<10}` {count}" for cat, count in items]
        fields.append({
            "name":   "By category",
            "value":  "\n".join(lines) or "—",
            "inline": True,
        })

    if by_band := summary.get("by_score_band"):
        order = ["EXTREME", "HIGH", "MEDIUM", "LOW", "NONE"]
        lines = [f"`{b:<8}` {by_band.get(b, 0)}" for b in order if by_band.get(b)]
        fields.append({
            "name":   "By score",
            "value":  "\n".join(lines) or "—",
            "inline": True,
        })

    if hoa_dropped := summary.get("hoa_filtered_count", 0):
        fields.append({
            "name":   "HOA filtered",
            "value":  str(hoa_dropped),
            "inline": True,
        })

    return fields


def _footer_text() -> str:
    return f"dallas-intel • runs daily 07:00 UTC"
