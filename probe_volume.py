"""Probe publicsearch.us for date-range-only result volumes.

Question: does the site cap records when no doc-type filter is applied?
"""
import re
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=30)

def collect_page_numbers(page):
    btns = page.locator("nav[aria-label='pagination'] button[aria-label^='page ']").all()
    nums = []
    for btn in btns:
        label = btn.get_attribute("aria-label") or ""
        m = re.match(r"page (\d+)", label)
        if m:
            nums.append(int(m.group(1)))
    return nums


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=250)
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()

    print(f">> Probing date range: {START_DATE} to {END_DATE} (30 days, NO doc-type filter)")
    print()

    page.goto("https://dallas.tx.publicsearch.us/", wait_until="networkidle")
    page.get_by_role("link", name="Advanced Search").click()
    page.wait_for_load_state("networkidle")

    fmt = "%m/%d/%Y"
    for selector, value in (
        ("#recordedDateRange-start", START_DATE.strftime(fmt)),
        ("#recordedDateRange-end",   END_DATE.strftime(fmt)),
    ):
        page.locator(selector).click()
        page.locator(selector).press("ControlOrMeta+a")
        page.locator(selector).fill(value)
    page.keyboard.press("Escape")

    page.get_by_role("button", name="Search").click()
    page.wait_for_load_state("networkidle", timeout=30_000)
    page.wait_for_timeout(2_000)

    rows_page1 = page.locator("section.search-results__results-wrap table tbody tr").count()
    print(f"   Rows on page 1: {rows_page1}")

    visible_pages = collect_page_numbers(page)
    max_visible = max(visible_pages) if visible_pages else 1
    print(f"   Initially-visible page numbers: {visible_pages}")
    print(f"   Highest visible page: {max_visible}")

    next_btn = page.get_by_role("button", name="next page")
    next_disabled = next_btn.is_disabled() if next_btn.count() else True
    print(f"   Next button disabled on page 1: {next_disabled}")

    html = page.content()
    print()
    print("   Total-count text candidates in HTML:")
    found_any = False
    for pattern in [
        r"Showing\s+[\d,]+\s+of\s+[\d,]+\s+\w+",
        r"[\d,]+\s+(?:results?|matches|documents|records)\s+found",
        r"of\s+[\d,]+\s+(?:results?|documents|records)",
        r"[\d,]+\s+total",
        r"Found\s+[\d,]+",
    ]:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            print(f"     {m.group(0)!r}")
            found_any = True
    if not found_any:
        print("     (none found)")

    print()
    print(">> Jumping to highest-visible page to see if range expands")
    try:
        page.locator(
            f"nav[aria-label='pagination'] button[aria-label='page {max_visible}']"
        ).first.click()
        page.wait_for_load_state("networkidle", timeout=15_000)
        page.wait_for_timeout(1_500)

        new_pages = collect_page_numbers(page)
        new_max = max(new_pages) if new_pages else max_visible
        print(f"   New visible page numbers: {new_pages}")
        print(f"   New highest visible: {new_max}")

        nb = page.get_by_role("button", name="next page")
        now_disabled = nb.is_disabled() if nb.count() else True
        print(f"   Next button disabled now: {now_disabled}")

        # If next is still enabled, keep clicking next to find the actual last page
        click_count = 0
        while not now_disabled and click_count < 50:
            nb.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(1_000)
            click_count += 1
            new_pages = collect_page_numbers(page)
            new_max = max(new_pages) if new_pages else new_max
            nb = page.get_by_role("button", name="next page")
            now_disabled = nb.is_disabled() if nb.count() else True
        print(f"   After {click_count} next-clicks, last reachable page: {new_max}")
        rows_last = page.locator("section.search-results__results-wrap table tbody tr").count()
        print(f"   Rows on last page: {rows_last}")

        # Estimated total
        est = (new_max - 1) * rows_page1 + rows_last
        print(f"   Estimated total records over 30 days: {est:,}")

    except Exception as e:
        print(f"   Couldn't probe further: {e}")

    print()
    print("Press Enter to close...")
    input()
    context.close()
    browser.close()
