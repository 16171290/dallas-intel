import logging
from datetime import date
from playwright.sync_api import sync_playwright
from scraper import publicsearch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

date_from = date(2026, 4, 1)
date_to   = date(2026, 5, 1)

print(f"Scraping LP records for {date_from} to {date_to}...")
print("(A Chromium window will open. Leave it alone; the script drives it.)")
print()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=200)
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    publicsearch._open_home(page)
    records = publicsearch.search_by_code(page, "LP", date_from, date_to)
    context.close()
    browser.close()

print()
print(f"Got {len(records)} records.")
print()
for i, rec in enumerate(records[:5], 1):
    print(f"{i}. record_id={rec.record_id}  filed={rec.filing_date}")
    print(f"   grantor:    {rec.grantor}")
    print(f"   grantee:    {rec.grantee}")
    print(f"   instrument: {rec.instrument_num}")
    print(f"   snippet:    {rec.raw_html_snippet}")
    print()
