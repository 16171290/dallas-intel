"""Validate the proposed filing-date extractor against the full PDF corpus.
Read-only. Does not modify production code.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import pdfplumber

OUTPUT = Path("validation_output.txt")
PDF_DIR = Path("data/foreclosure_pdfs")
TODAY = datetime(2026, 5, 15)  # match the system date you ran on

# ---------- Date format normalizers ----------

_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

def _to_iso(raw):
    """Convert various date strings to YYYY-MM-DD. Returns None on failure."""
    if not raw:
        return None
    raw = raw.strip().replace("\n", " ").replace("  ", " ")

    # M/D/YYYY or M/D/YY
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", raw)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 50 else 1900
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # Month D, YYYY
    m = re.match(r"^([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})$", raw)
    if m:
        mo = _MONTH_NUM.get(m.group(1).lower())
        if mo:
            try:
                return datetime(int(m.group(3)), mo, int(m.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                return None

    # Xth day of Month, YYYY
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Z][a-z]+),?\s+(\d{4})$", raw, re.I)
    if m:
        mo = _MONTH_NUM.get(m.group(2).lower())
        if mo:
            try:
                return datetime(int(m.group(3)), mo, int(m.group(1))).strftime("%Y-%m-%d")
            except ValueError:
                return None
    return None

# ---------- Positive patterns (filing-date candidates) ----------

# Pattern 1: declarant-filed (HIGHEST confidence)
# "...on 02/19/26 I filed at the office of the DALLAS County Clerk..."
P1_DECLARANT = re.compile(
    r"on\s+(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+I\s+filed\s+at\s+the\s+office\s+of\s+(?:the\s+)?DALLAS\s+County",
    re.IGNORECASE,
)

# Pattern 2: trustee Dated: line
# Allow optional whitespace and require date format follows immediately.
P2_DATED = re.compile(
    r"(?im)(?:^|\n)\s*Dated:?\s*(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
)

# Pattern 3: "WITNESS MY HAND this <date>"
P3_WITNESS = re.compile(
    r"WITNESS\s+(?:MY|my)\s+hand\s+this\s+(?P<date>\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+[A-Z][a-z]+,?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    re.IGNORECASE,
)

# Pattern 4: "Date: <month> <day>, <year>" near trustee/clerk markers
# Higher false-positive risk; need rejection context check.
P4_DATE_LABEL = re.compile(
    r"(?im)(?:^|\n)\s*Date:\s*(?P<date>[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
)

# ---------- Rejection contexts ----------

# These phrases, when preceding a date candidate within ~200 chars,
# indicate the date is NOT a notice filing date (it's a Deed-of-Trust
# date, mortgage origination, prior recording, etc).
REJECT_PHRASES = [
    "deed of trust", "WHEREAS", "recorded", "Original Mortgagee",
    "INSTRUMENT TO BE FORECLOSED", "executed", "Mortgagor",
    "Grantor(s):", "Deed of Trust Date", "in payment of a debt",
    "Note dated", "promissory note", "Notice of Sale", "Notary",
    "Commission Expires", "DEED OF TRUST INFORMATION",
]

# Phrases that indicate a sale date, also rejectable.
SALE_DATE_PHRASES = [
    "Date of Sale", "NOTICE IS HEREBY GIVEN that on",
    "the foreclosure sale will be conducted",
    "sale is scheduled", "first Tuesday",
    "Time of Sale", "Place of Sale",
]

def _has_reject_context(text, position, window=200):
    """Check the N chars BEFORE position for rejection phrases."""
    start = max(0, position - window)
    snippet = text[start:position].lower()
    for phrase in REJECT_PHRASES + SALE_DATE_PHRASES:
        if phrase.lower() in snippet:
            return phrase
    return None

# ---------- Main extractor ----------

def extract_filing_date(text):
    """Try each positive pattern in confidence order. Apply rejection.
    Returns (iso_date_or_None, pattern_used_or_None, raw_matched).
    """
    candidates = []

    for pattern_name, pattern, confidence in [
        ("P1_DECLARANT", P1_DECLARANT, 1),
        ("P3_WITNESS",   P3_WITNESS,   2),
        ("P2_DATED",     P2_DATED,     3),
        ("P4_DATE_LABEL", P4_DATE_LABEL, 4),
    ]:
        for m in pattern.finditer(text):
            reject = _has_reject_context(text, m.start())
            raw = m.group("date").strip()
            iso = _to_iso(raw)
            candidates.append({
                "pattern": pattern_name,
                "confidence": confidence,
                "raw": raw,
                "iso": iso,
                "position": m.start(),
                "rejected_by": reject,
            })

    # Filter: keep only non-rejected with valid ISO
    valid = [c for c in candidates if c["iso"] and not c["rejected_by"]]

    if not valid:
        return None, None, None, candidates

    # Sort by confidence (lower number = higher confidence), then by position (earlier = better)
    valid.sort(key=lambda c: (c["confidence"], c["position"]))
    best = valid[0]
    return best["iso"], best["pattern"], best["raw"], candidates

# ---------- Walk corpus ----------

all_pdfs = sorted(PDF_DIR.rglob("*.pdf"))
results = []
failures = []

print(f"Processing {len(all_pdfs)} PDFs...")

with OUTPUT.open("w", encoding="utf-8") as f:
    for i, pdf_path in enumerate(all_pdfs, 1):
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:
            failures.append((pdf_path, str(e)))
            continue

        iso, pattern, raw, all_candidates = extract_filing_date(text)
        results.append({
            "pdf": str(pdf_path.relative_to(PDF_DIR)),
            "iso": iso,
            "pattern": pattern,
            "raw": raw,
            "size": len(text),
            "all_candidates": all_candidates,
        })

        # Print per-PDF outcome to file
        f.write("=" * 80 + "\n")
        f.write(f"PDF: {pdf_path.relative_to(PDF_DIR)}\n")
        f.write(f"Text size: {len(text):,} chars\n")
        if iso:
            f.write(f"BEST: {iso}  via {pattern}  (raw={raw!r})\n")
        else:
            f.write(f"NO EXTRACTION\n")
        f.write(f"\nAll candidates considered:\n")
        for c in all_candidates:
            status = "REJECTED" if c["rejected_by"] else ("ACCEPTED" if c["iso"] else "PARSE_FAIL")
            f.write(f"  [{c['pattern']:14s}] {status:10s}  raw={c['raw']!r:<30s}  iso={c['iso']!r}")
            if c["rejected_by"]:
                f.write(f"  rejected_by={c['rejected_by']!r}")
            f.write("\n")
        f.write("\n")

        if i % 10 == 0:
            print(f"  ... {i}/{len(all_pdfs)}")

    # ---------- Summary ----------
    f.write("=" * 80 + "\n")
    f.write("CORPUS SUMMARY\n")
    f.write("=" * 80 + "\n")

    total = len(results)
    extracted = sum(1 for r in results if r["iso"])
    not_extracted = total - extracted

    f.write(f"\nTotal PDFs processed: {total}\n")
    f.write(f"  Failed to read:     {len(failures)}\n")
    f.write(f"  Extracted date:     {extracted}  ({100*extracted/total:.1f}%)\n")
    f.write(f"  No extraction:      {not_extracted}  ({100*not_extracted/total:.1f}%)\n")

    # Pattern usage
    pattern_counts = Counter(r["pattern"] for r in results if r["pattern"])
    f.write(f"\nPatterns used (for extractions):\n")
    for p, c in pattern_counts.most_common():
        f.write(f"  {p:14s}  {c}\n")

    # Age distribution
    f.write(f"\nAge of extracted dates (today = {TODAY.strftime('%Y-%m-%d')}):\n")
    age_buckets = Counter()
    for r in results:
        if not r["iso"]:
            continue
        try:
            d = datetime.strptime(r["iso"], "%Y-%m-%d")
            age = (TODAY - d).days
            if age <= 7:
                age_buckets["0-7 days"] += 1
            elif age <= 30:
                age_buckets["8-30 days"] += 1
            elif age <= 90:
                age_buckets["31-90 days"] += 1
            elif age <= 365:
                age_buckets["91-365 days"] += 1
            else:
                age_buckets["over 1 year"] += 1
        except ValueError:
            age_buckets["unparseable"] += 1
    for bucket in ["0-7 days", "8-30 days", "31-90 days", "91-365 days", "over 1 year", "unparseable"]:
        if age_buckets[bucket]:
            f.write(f"  {bucket:15s} {age_buckets[bucket]}\n")

    # Suspicious extractions: extracted but date is OLD (likely false positive)
    f.write(f"\nSUSPICIOUS - extracted dates older than 90 days:\n")
    for r in results:
        if not r["iso"]:
            continue
        try:
            d = datetime.strptime(r["iso"], "%Y-%m-%d")
            if (TODAY - d).days > 90:
                f.write(f"  {r['pdf']:50s} -> {r['iso']}  via {r['pattern']}  raw={r['raw']!r}\n")
        except ValueError:
            pass

    # PDFs with no extraction
    f.write(f"\nPDFs WITH NO EXTRACTION ({not_extracted}):\n")
    for r in results:
        if not r["iso"]:
            f.write(f"  {r['pdf']}\n")

    if failures:
        f.write(f"\nFailed to read ({len(failures)}):\n")
        for p, err in failures:
            f.write(f"  {p}: {err}\n")

print()
print(f"Wrote: {OUTPUT}")
print()
print(f"=== Summary ===")
print(f"Total PDFs:     {len(all_pdfs)}")
print(f"Failed to read: {len(failures)}")
print(f"Extracted:      {extracted}  ({100*extracted/len(results):.1f}%)")
print(f"No extraction:  {not_extracted}  ({100*not_extracted/len(results):.1f}%)")
print()
print("Pattern usage:")
for p, c in pattern_counts.most_common():
    print(f"  {p:14s}  {c}")
print()
print(f"Age distribution:")
for bucket in ["0-7 days", "8-30 days", "31-90 days", "91-365 days", "over 1 year", "unparseable"]:
    if age_buckets[bucket]:
        print(f"  {bucket:15s} {age_buckets[bucket]}")
