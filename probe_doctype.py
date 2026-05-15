"""Capture the doc-type filter's expanded HTML to find where label text lives."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=300)
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()

    print(">> Opening publicsearch.us advanced search")
    page.goto("https://dallas.tx.publicsearch.us/", wait_until="networkidle")
    page.get_by_role("link", name="Advanced Search").click()
    page.wait_for_load_state("networkidle")

    print(">> Opening the doc-type combobox and typing 'WARRANTY DEED'")
    combo = page.get_by_role("combobox", name="Filter Document Types")
    combo.click()
    combo.fill("WARRANTY DEED")
    page.wait_for_timeout(800)

    # Find the dropdown that holds the filtered options. We don't know its
    # selector, so dump everything around any visible 'WARRANTY DEED' text.
    html = page.content()

    # Find where 'WARRANTY DEED' appears in the rendered HTML and dump
    # ~3000 chars around each occurrence.
    import re
    print()
    print("=" * 60)
    print("HTML around each visible 'WARRANTY DEED' occurrence")
    print("=" * 60)

    found_positions = []
    for m in re.finditer(r"WARRANTY DEED", html):
        found_positions.append(m.start())

    print(f"Found {len(found_positions)} occurrences of 'WARRANTY DEED' in HTML")
    print()

    for i, pos in enumerate(found_positions[:6], 1):
        start = max(0, pos - 1500)
        end = min(len(html), pos + 1500)
        print(f"--- Occurrence {i} at offset {pos} ---")
        print(html[start:end])
        print()
        print()

    # Save full HTML for offline inspection
    with open("probe_doctype.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Full HTML saved to probe_doctype.html ({len(html):,} chars)")

    print()
    print("Press Enter to close...")
    input()
    context.close()
    browser.close()
