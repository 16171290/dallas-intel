"""Find the PDF that produced the MIRELES record, dump its text."""
import json, pdfplumber
from pathlib import Path

# Identify the source PDF by re-running the parser against each
# foreclosure PDF in the cache and finding the one containing
# the MIRELES address.
cache_dir = Path("data/foreclosure_pdfs")
if not cache_dir.exists():
    # Fallback - try a few common locations.
    for cand in ["pdfs", "foreclosures", "data/pdfs", "foreclosure_cache"]:
        if Path(cand).exists():
            cache_dir = Path(cand)
            break

print(f"Searching PDFs under: {cache_dir}")
print()

target_address = "5605 EVERGLADE"
found = False

for pdf_path in cache_dir.rglob("*.pdf"):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        print(f"  skip {pdf_path.name}: {e}")
        continue
    if target_address in text.upper():
        found = True
        print(f"FOUND in: {pdf_path}")
        print(f"Modified: {pdf_path.stat().st_mtime}")
        print()
        # Find the text around the target address - 800 chars before, 1500 after.
        idx = text.upper().find(target_address)
        start = max(0, idx - 800)
        end = min(len(text), idx + 1500)
        print("=" * 70)
        print(f"CONTEXT WINDOW around 5605 EVERGLADE (chars {start}-{end}):")
        print("=" * 70)
        print(text[start:end])
        print("=" * 70)
        break

if not found:
    print(f"Did not find 5605 EVERGLADE in any PDF under {cache_dir}")
    print("Listing what's there:")
    for p in cache_dir.rglob("*.pdf"):
        print(f"  {p}")
