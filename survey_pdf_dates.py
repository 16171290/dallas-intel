"""Survey filing-date patterns across the PDF corpus. Read-only."""
import re
import random
from collections import Counter
from pathlib import Path
import pdfplumber

OUTPUT = Path("survey_output.txt")
SAMPLE_SIZE = 15

# Generic "any date" detector - we cast wide on purpose. Catches:
#   1/12/2026, 01/12/2026, 1-12-26, January 12, 2026, 12th day of January, 2026
DATE_PATTERNS = [
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"\b[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+[A-Z][a-z]+,?\s+\d{4}\b", re.IGNORECASE),
]

# Context labels to classify each date by surrounding text.
CONTEXT_LABELS = [
    ("trustee_dated",   re.compile(r"(?im)\bdated[:\s]+[^\n]{0,40}$")),       # "Dated: <date>"
    ("whereas_dot",     re.compile(r"(?im)WHEREAS,?\s+on\b")),                # WHEREAS, on <date>
    ("filed_label",     re.compile(r"(?im)\b(?:filed|recorded)\s+(?:on|in)\b")),
    ("sale_notice",     re.compile(r"(?im)NOTICE\s+IS\s+HEREBY\s+GIVEN\s+that\s+on")),
    ("day_of",          re.compile(r"(?im)\bday\s+of\b")),
    ("instrument_ref",  re.compile(r"(?im)Clerk['\u2019]?s?\s+File|Instrument\s+Number")),
]

cache_dir = Path("data/foreclosure_pdfs")
all_pdfs = list(cache_dir.rglob("*.pdf"))
print(f"Total PDFs in corpus: {len(all_pdfs)}")

# Sample across different cities/months to maximize variance coverage.
random.seed(42)  # deterministic so re-runs match
sample = random.sample(all_pdfs, min(SAMPLE_SIZE, len(all_pdfs)))

with OUTPUT.open("w", encoding="utf-8") as out:
    summary_counts = Counter()

    for pdf_path in sample:
        out.write("=" * 80 + "\n")
        out.write(f"PDF: {pdf_path.relative_to(cache_dir)}\n")
        out.write("=" * 80 + "\n")

        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:
            out.write(f"  EXTRACTION FAILED: {e}\n\n")
            continue

        out.write(f"Total text length: {len(text):,} chars\n\n")

        # Find all dates, with 80 chars of context on each side.
        seen = set()
        for pattern in DATE_PATTERNS:
            for m in pattern.finditer(text):
                date_str = m.group(0)
                if date_str in seen:
                    continue
                seen.add(date_str)

                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(text), m.end() + 80)
                context = text[ctx_start:ctx_end].replace("\n", " | ")

                # Classify by context label.
                pre_text = text[max(0, m.start() - 200):m.start()]
                labels = []
                for label_name, label_re in CONTEXT_LABELS:
                    if label_re.search(pre_text) or label_re.search(text[max(0, m.start()-40):m.end()+40]):
                        labels.append(label_name)
                if not labels:
                    labels = ["unclassified"]
                for lab in labels:
                    summary_counts[lab] += 1

                out.write(f"  [{','.join(labels):20s}] {date_str!r}\n")
                out.write(f"      ...{context}...\n")
        out.write("\n")

    # Summary
    out.write("=" * 80 + "\n")
    out.write("SUMMARY ACROSS SAMPLE\n")
    out.write("=" * 80 + "\n")
    for label, count in summary_counts.most_common():
        out.write(f"  {label:20s} {count}\n")

print()
print(f"Wrote survey to: {OUTPUT}")
print(f"Sampled {len(sample)} PDFs.")
print()
print("Top-level summary:")
for label, count in summary_counts.most_common():
    print(f"  {label:20s} {count}")
