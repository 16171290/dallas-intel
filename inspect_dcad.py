"""DCAD schema observation. Read-only. No writes."""
from scraper import dcad_bulk

print("Loading DCAD tables...")
zip_path = dcad_bulk.fetch_dcad_zip()
tables = dcad_bulk.parse_dcad_tables(zip_path)

for name in ["ACCOUNT_INFO", "MULTI_OWNER"]:
    print()
    print("=" * 70)
    print(f"TABLE: {name}")
    print("=" * 70)
    t = tables.get(name)
    if t is None:
        print("  NOT PRESENT")
        continue
    print(f"  Type:    {type(t).__name__}")
    if hasattr(t, "shape"):
        print(f"  Shape:   {t.shape}")
    if hasattr(t, "columns"):
        print(f"  Columns: {list(t.columns)}")
        print()
        print("  First 5 rows:")
        print(t.head(5).to_string(max_colwidth=40))
