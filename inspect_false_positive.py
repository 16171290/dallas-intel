import re
from pathlib import Path
import pdfplumber

pdf = Path("data/foreclosure_pdfs/March/Dallas_4 (2).pdf")
with pdfplumber.open(pdf) as p:
    text = "\n".join((page.extract_text() or "") for page in p.pages)

# Find the "February 5, 2025" match and show context around it.
target = "February 5, 2025"
idx = text.find(target)
if idx < 0:
    print(f"Could not find {target!r} in PDF")
else:
    start = max(0, idx - 400)
    end = min(len(text), idx + 200)
    print(f"Found at position {idx}")
    print(f"Context (chars {start}-{end}):")
    print("-" * 70)
    print(text[start:end])
    print("-" * 70)
