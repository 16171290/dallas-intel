"""Reproduce the false-positive context check exactly as validator does."""
import re
from pathlib import Path
import pdfplumber

PDF = Path("data/foreclosure_pdfs/March/Dallas_4 (2).pdf")
with pdfplumber.open(PDF) as p:
    text = "\n".join((page.extract_text() or "") for page in p.pages)

# Replicate the exact P2_DATED regex from validate_notice_date.py
P2_DATED = re.compile(
    r"(?im)(?:^|\n)\s*Dated:?\s*(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
)

# Replicate REJECT_PHRASES from validate
REJECT_PHRASES = [
    "deed of trust", "WHEREAS", "recorded", "Original Mortgagee",
    "INSTRUMENT TO BE FORECLOSED", "executed", "Mortgagor",
    "Grantor(s):", "Deed of Trust Date", "in payment of a debt",
    "Note dated", "promissory note", "Notice of Sale", "Notary",
    "Commission Expires", "DEED OF TRUST INFORMATION",
]

print(f"PDF: {PDF.name}")
print(f"Text length: {len(text):,}")
print()

# Find every P2_DATED match
for i, m in enumerate(P2_DATED.finditer(text)):
    raw = m.group("date")
    pos = m.start()

    # Replicate _has_reject_context exactly: 200 chars BEFORE position
    win_start = max(0, pos - 200)
    snippet = text[win_start:pos].lower()

    print(f"Match #{i}: raw={raw!r} at position {pos}")
    print(f"  Preceding 200 chars (lowercased):")
    print(f"  >>> {snippet!r}")
    print()

    rejected_by = None
    for phrase in REJECT_PHRASES:
        if phrase.lower() in snippet:
            rejected_by = phrase
            break
    if rejected_by:
        print(f"  REJECTED BY: {rejected_by!r}")
    else:
        print(f"  WOULD ACCEPT")
    print("-" * 60)
