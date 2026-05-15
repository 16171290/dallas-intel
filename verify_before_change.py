"""Verification step before parser change."""
import re
from pathlib import Path
import pdfplumber

# 1. Check the "Dated:" pattern across multiple PDFs to confirm it's
#    consistent. Sample 5 PDFs at random.
print("=" * 70)
print("PART 1: Does 'Dated:' pattern appear consistently across PDFs?")
print("=" * 70)
import random
all_pdfs = list(Path("data/foreclosure_pdfs").rglob("*.pdf"))
sample = random.sample(all_pdfs, min(5, len(all_pdfs)))

dated_pattern = re.compile(r"(?im)^\s*Dated:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})")

for pdf_path in sample:
    print()
    print(f"PDF: {pdf_path.name}")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        print(f"  skip: {e}")
        continue
    matches = dated_pattern.findall(text)
    print(f"  'Dated:' matches found: {len(matches)}")
    for i, m in enumerate(matches[:5]):
        print(f"    [{i}] {m!r}")
    if len(matches) > 5:
        print(f"    ...and {len(matches) - 5} more")

# 2. Find the file that maps parser output to canonical record fields.
#    Just grep for "filing_date" assignment across the scraper directory.
print()
print("=" * 70)
print("PART 2: Where is filing_date set on the canonical record?")
print("=" * 70)
for py in Path("scraper").rglob("*.py"):
    try:
        content = py.read_text(encoding="utf-8")
    except Exception:
        continue
    for lineno, line in enumerate(content.splitlines(), 1):
        if "filing_date" in line:
            print(f"  {py.name}:{lineno}: {line.rstrip()}")
