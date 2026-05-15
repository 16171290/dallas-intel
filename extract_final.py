import re
from pathlib import Path

html = Path("probe_results.html").read_text(encoding="utf-8")

# -- 1. Results table: thead + one sample row ------------------------------
print("=" * 60)
print("TABLE STRUCTURE")
print("=" * 60)

table_match = re.search(
    r'<section class="search-results__results-wrap".*?</section>',
    html, re.DOTALL,
)
if table_match:
    section_html = table_match.group(0)
    # Pull the thead
    thead = re.search(r"<thead.*?</thead>", section_html, re.DOTALL)
    print("--- THEAD (column headers) ---")
    print(thead.group(0)[:2500] if thead else "(no thead found)")
    print()
    # Pull the first <tr> from tbody
    tbody = re.search(r"<tbody.*?</tbody>", section_html, re.DOTALL)
    if tbody:
        tr_matches = re.findall(r"<tr[^>]*>.*?</tr>", tbody.group(0), re.DOTALL)
        print(f"--- FIRST TBODY ROW (1 of {len(tr_matches)}) ---")
        if tr_matches:
            print(tr_matches[0][:3500])
        print()

# -- 2. Full RP doc-type code/description mapping -------------------------
print("=" * 60)
print("DOC TYPE MAPPING (RP department)")
print("=" * 60)

rp_match = re.search(r'"code":"RP","docType":\[(.+?)\}\]', html, re.DOTALL)
if rp_match:
    pairs = re.findall(
        r'"code":"([^"]+)","description":"([^"]+)"',
        rp_match.group(1),
    )
    print(f"Found {len(pairs)} doc types in RP department:")
    for code, desc in pairs:
        desc = desc.replace("\\u002F", "/")
        print(f"  {code:8s} = {desc}")
else:
    print("(RP docType block not located)")
