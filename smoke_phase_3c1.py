"""Phase 3.C.1 smoke test - moment of truth."""
from scraper import dcad_bulk, dcad_owner_index, bankruptcy, name_matcher
from collections import Counter

# Step 1: load DCAD
print("Loading DCAD bulk data...")
zip_path = dcad_bulk.fetch_dcad_zip()
tables = dcad_bulk.parse_dcad_tables(zip_path)
print(f"  Tables: {sorted(tables.keys())}")

# Step 2: build owner index
print()
print("Building owner index...")
index = dcad_owner_index.build_owner_index(tables)
print(f"  Indexed {len(index):,} unique owner names")
if len(index) == 0:
    print("  WARNING: owner index is empty. Running diagnostic...")
    diag = dcad_owner_index.inspect_account_info_fields(tables)
    print(f"    owner_field:    {diag['matched_owner_field']}")
    print(f"    account_field:  {diag['matched_account_field']}")
    print(f"    keys_observed:  {diag['keys_observed'][:15]}")
    if diag['sample_rows']:
        print(f"    first row owner: {diag['sample_rows'][0]['owner_field']!r}")
    raise SystemExit("Cannot proceed - owner index is empty.")

# Step 3: fetch today's filings
print()
print("Fetching today's bankruptcy RSS feed...")
records = bankruptcy.fetch_voluntary_petitions()
print(f"  {len(records)} voluntary petitions in last 24 hours")

# Step 4: attempt matches
print()
print("Attempting DCAD matches...")
matched, unmatched, businesses = [], [], []
strategies = Counter()
for rec in records:
    if rec.is_business:
        businesses.append(rec)
        continue
    for debtor in rec.debtor_names:
        result = name_matcher.match_debtor_to_dcad(debtor, index)
        if result:
            strategies[result.match_strategy] += 1
            matched.append((rec, debtor, result))
        else:
            unmatched.append((rec, debtor))

# Step 5: report
print()
print("=" * 70)
print("RESULTS")
print("=" * 70)
print(f"Total petitions:         {len(records)}")
print(f"Business filings (skip): {len(businesses)}")
print(f"Person debtors tried:    {len(matched) + len(unmatched)}")
print()
print(f"  MATCHED:    {len(matched)}")
print(f"  UNMATCHED:  {len(unmatched)}")
if matched or unmatched:
    rate = 100 * len(matched) / (len(matched) + len(unmatched))
    print(f"  Match rate: {rate:.1f}%")
print()
print(f"Match strategies used:")
for strat, count in strategies.most_common():
    print(f"  {strat:>22s}: {count}")
print()
print("Sample matches (first 10):")
for rec, debtor, result in matched[:10]:
    n = len(result.accounts)
    note = f"{n} property" if n == 1 else f"{n} properties"
    print(f"  {rec.case_number_raw}  Ch.{rec.chapter}  {debtor!r}")
    print(f"      -> {result.matched_name!r}  via {result.match_strategy}  ({note})")
print()
print("Sample unmatched (first 10):")
for rec, debtor in unmatched[:10]:
    converted = name_matcher.convert_bankruptcy_to_dcad_format(debtor)
    print(f"  {rec.case_number_raw}  {debtor!r}  ->  {converted!r}  NO MATCH")
