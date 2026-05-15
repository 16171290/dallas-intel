from scraper import dcad_bulk, enrichment

tables = dcad_bulk.parse_dcad_tables(dcad_bulk.fetch_dcad_zip())
idx = dcad_bulk.build_address_index(tables)

# Walk 1,000 random addresses and tally exemption rates
import random
random.seed(42)
sample = random.sample(list(idx.keys()), 1000)

counts = {"homestead": 0, "over65": 0, "disabled": 0, "tax_def": 0}
for addr in sample:
    rec = {"address_normalized": addr}
    enrichment.enrich_record(rec, tables, idx)
    if rec.get("dcad_homestead"):    counts["homestead"] += 1
    if rec.get("dcad_over65"):       counts["over65"]    += 1
    if rec.get("dcad_disabled"):     counts["disabled"]  += 1
    if rec.get("dcad_tax_deferred"): counts["tax_def"]   += 1

print("Exemption rates across 1,000 random parcels:")
for k, v in counts.items():
    print(f"  {k:10s}  {v:4d}  ({v/10:.1f}%)")
