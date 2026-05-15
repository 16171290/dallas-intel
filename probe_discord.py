import logging
logging.basicConfig(level=logging.INFO)

from scraper import config, monitor

# Sanity-check the env loaded
if not config.DISCORD_WEBHOOK_URL:
    print("ERROR: DISCORD_WEBHOOK_URL is empty.")
    print("       Check that .env exists in the project root and contains the URL.")
    raise SystemExit(1)

if not config.DISCORD_WEBHOOK_URL.startswith("https://discord.com/api/webhooks/"):
    print("ERROR: DISCORD_WEBHOOK_URL does not look like a Discord webhook URL.")
    raise SystemExit(1)

print("Webhook configured. Sending test embed...")

# Fire a synthetic "run complete" embed
monitor.notify_run_complete({
    "total":                    42,
    "active":                   38,
    "address_resolution_rate":  0.91,
    "by_category":              {"L/P": 12, "NOTICE": 8, "LIEN": 18, "DEED": 4},
    "by_score_band":            {"EXTREME": 3, "HIGH": 9, "MEDIUM": 14, "LOW": 16},
    "hoa_filtered_count":       7,
})

print("Test notification sent.")
print("Check your Discord channel for an embed titled 'dallas-intel run complete'.")
