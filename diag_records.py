"""Diagnostic for owner-name and filing-date faults observed in dashboard."""
import json
from collections import Counter
from pathlib import Path

REC_PATH = Path("data/records.json")
data = json.loads(REC_PATH.read_text(encoding="utf-8"))
records = data.get("records", [])

print(f"Total records: {len(records)}")
print(f"generated_at:  {data.get('generated_at')!r}")
print()

# ----- Question 1: the specific record -----
print("=" * 70)
print("THE MIRELES MARIA RECORD")
print("=" * 70)
target = [r for r in records
          if (r.get("address_normalized") or "").upper().startswith("5605 EVERGLADE")
             or "MIRELES" in (r.get("grantee") or "").upper()
             or "MIRELES" in (r.get("dcad_owner") or "").upper()]
print(f"Matches found: {len(target)}")
for rec in target:
    print()
    for k in ("record_id", "source", "address", "address_normalized",
             "grantor", "grantee", "dcad_owner", "dcad_account",
             "category", "dallas_code", "instrument_num",
             "filing_date", "sale_date", "first_seen", "last_seen",
             "dcad_market_value", "dcad_homestead", "score",
             "active", "in_buy_box", "buy_box_reason"):
        v = rec.get(k)
        if v is not None and v != "":
            print(f"  {k:24s}: {v!r}")
    pw = rec.get("parse_warnings") or []
    if pw:
        print(f"  parse_warnings          : {pw!r}")

# ----- Question 2: filing_date distribution -----
print()
print("=" * 70)
print("FILING DATE DISTRIBUTION (all 189 records)")
print("=" * 70)
date_year_counts = Counter()
date_examples_by_year = {}
missing_date = 0
for r in records:
    d = r.get("filing_date") or ""
    if not d:
        missing_date += 1
        continue
    yr = d[:4] if len(d) >= 4 and d[:4].isdigit() else "?????"
    date_year_counts[yr] += 1
    date_examples_by_year.setdefault(yr, []).append(
        f"{d} | {(r.get('address_normalized') or r.get('address') or '?')[:40]:40s} "
        f"| {(r.get('grantee') or r.get('dcad_owner') or '?')[:25]:25s} "
        f"| score={r.get('score')}"
    )
print(f"Records with no filing_date: {missing_date}")
print()
for yr in sorted(date_year_counts):
    print(f"  {yr}: {date_year_counts[yr]:4d} records")
print()
print("OLDEST 10 by filing_date:")
dated = [r for r in records if r.get("filing_date")]
for r in sorted(dated, key=lambda x: x["filing_date"])[:10]:
    print(f"  {r['filing_date']} | {(r.get('address_normalized') or '?')[:40]:40s}"
          f" | {(r.get('grantee') or r.get('dcad_owner') or '?')[:25]:25s}"
          f" | score={r.get('score')}")
print()
print("NEWEST 5 by filing_date:")
for r in sorted(dated, key=lambda x: x["filing_date"])[-5:]:
    print(f"  {r['filing_date']} | {(r.get('address_normalized') or '?')[:40]:40s}"
          f" | {(r.get('grantee') or r.get('dcad_owner') or '?')[:25]:25s}"
          f" | score={r.get('score')}")

# ----- Question 3: name-field consistency -----
print()
print("=" * 70)
print("NAME FIELD VARIANCE (first 20 of records with dcad_account)")
print("=" * 70)
print(f"{'GRANTEE':<35} | {'DCAD_OWNER':<35} | match?")
print("-" * 88)
enriched = [r for r in records if r.get("dcad_account")]
for r in enriched[:20]:
    g  = (r.get("grantee") or "").strip()
    do = (r.get("dcad_owner") or "").strip()
    same = (g.upper() == do.upper()) if (g and do) else "n/a"
    print(f"{g[:35]:<35} | {do[:35]:<35} | {same}")

# ----- Question 4: source breakdown -----
print()
print("=" * 70)
print("SOURCE BREAKDOWN")
print("=" * 70)
src_counts = Counter(r.get("source") or "(none)" for r in records)
for src, n in src_counts.most_common():
    print(f"  {src:<30s} {n}")
