from pathlib import Path

html = Path("probe_results.html").read_text(encoding="utf-8")

def slice_around(text, anchor, before=1500, after=800):
    idx = text.lower().find(anchor.lower())
    if idx < 0:
        return f"(anchor {anchor!r} not found)"
    start = max(0, idx - before)
    end = min(len(text), idx + len(anchor) + after)
    return text[start:end]

print("=" * 60)
print("SNIPPET 1: around first 'class=\"search-result'")
print("=" * 60)
print(slice_around(html, 'class="search-result'))

print()
print("=" * 60)
print("SNIPPET 2: around first 'Lis Pendens'")
print("=" * 60)
print(slice_around(html, "Lis Pendens", before=2200, after=1200))

print()
print("=" * 60)
print("SNIPPET 3: pagination markup hits")
print("=" * 60)
for term in ["pagination", "page-number", 'aria-label="Next', 'aria-label="Previous',
             ">Next<", ">Previous<", "perPage", "Showing", "of " ]:
    idx = html.lower().find(term.lower())
    if idx >= 0:
        start = max(0, idx - 250)
        end = min(len(html), idx + 500)
        print(f"--- {term!r} at offset {idx} ---")
        print(html[start:end])
        print()
