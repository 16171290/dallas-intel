"""Probe publicsearch.us with one full search and dump the results page."""
from datetime import date
import sys
from playwright.sync_api import sync_playwright

START_DATE = date(2026, 4, 1)
END_DATE   = date(2026, 5, 1)
CODE       = "LP"  # try the literal Dallas code first


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=400)
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()

        print(">> Opening publicsearch.us")
        page.goto("https://dallas.tx.publicsearch.us/", wait_until="networkidle")

        print(">> Clicking Advanced Search")
        page.get_by_role("link", name="Advanced Search").click()
        page.wait_for_load_state("networkidle")

        print(f">> Filling date range {START_DATE} to {END_DATE}")
        fmt = "%m/%d/%Y"
        for selector, value in (
            ("#recordedDateRange-start", START_DATE.strftime(fmt)),
            ("#recordedDateRange-end",   END_DATE.strftime(fmt)),
        ):
            page.locator(selector).click()
            page.locator(selector).press("ControlOrMeta+a")
            page.locator(selector).fill(value)
        page.keyboard.press("Escape")

        print(f">> Filtering doc type with {CODE!r}")
        combo = page.get_by_role("combobox", name="Filter Document Types")
        combo.click()
        combo.fill(CODE)
        page.wait_for_timeout(800)

        match_count = page.locator(".checkbox__container").count()
        print(f"   Filter matched {match_count} checkbox(es)")

        if match_count == 0:
            print(f"   WARN: {CODE!r} did not match any doc type.")
            print("   Try CODE = 'Lis ' (display-name prefix) on next run.")
            print("   Continuing without a doc-type filter so we still see results.")
            combo.fill("")
        else:
            page.locator(".checkbox__container").first.click()
            combo.fill("")

        print(">> Submitting search")
        page.get_by_role("button", name="Search").click()

        print(">> Waiting for results to render")
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            print("   (networkidle did not fire within 20s, continuing)")
        page.wait_for_timeout(2_000)

        html = page.content()
        with open("probe_results.html", "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path="probe_results.png", full_page=True)

        print()
        print("-" * 60)
        print("PROBE RESULTS")
        print("-" * 60)
        print(f"  Final URL:   {page.url}")
        print(f"  HTML size:   {len(html):,} chars")
        print(f"  Files:       probe_results.html, probe_results.png")
        print()
        print("  Pattern hits in HTML (hints for the row selector):")
        for pattern in [
            'data-testid="result-row"',
            'data-testid="search-result"',
            'class="result-row"',
            'class="search-result',
            'role="row"',
            'role="gridcell"',
            "<tr ",
            'data-testid="pagination',
            "Lis Pendens",
            "Notice of",
            "No results",
            "no results found",
        ]:
            count = html.lower().count(pattern.lower())
            print(f"    {pattern!r:50s} {count}")

        print()
        print("Press Enter to close the browser ...")
        sys.stdout.flush()
        try:
            input()
        except EOFError:
            pass

        context.close()
        browser.close()


if __name__ == "__main__":
    run()
