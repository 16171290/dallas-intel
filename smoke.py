from scraper import dcad_bulk, enrichment

tables = dcad_bulk.parse_dcad_tables(dcad_bulk.fetch_dcad_zip())
idx = dcad_bulk.build_address_index(tables)
print(f"Address index: {len(idx):,} unique addresses")
print()

for addr in list(idx.keys())[:5]:
    rec = {"address_normalized": addr}
    enrichment.enrich_record(rec, tables, idx)
    mv = rec.get("dcad_market_value")
    val_str = f"${mv:,.0f}" if mv else "-"
    print(addr)
    print(f"  account:    {rec.get('dcad_account')}")
    print(f"  owner:      {rec.get('dcad_owner')}")
    print(f"  value:      {val_str}")
    print(f"  homestead:  {rec.get('dcad_homestead')}")
    print(f"  over-65:    {rec.get('dcad_over65')}")
    print(f"  disabled:   {rec.get('dcad_disabled')}")
    print(f"  tax def:    {rec.get('dcad_tax_deferred')}")
    print()
