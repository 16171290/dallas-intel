from scraper import bankruptcy
records = bankruptcy.fetch_voluntary_petitions()
print(f"Voluntary petitions in last 24 hours: {len(records)}")
print()
print("First 5:")
for r in records[:5]:
    print(f"  {r.case_number_raw}  Ch.{r.chapter}  Office:{r.office}")
    print(f"    debtors: {r.debtor_names}")
    print(f"    business: {r.is_business}  trustee: {r.trustee}")
    print()
biz = [r for r in records if r.is_business]
print(f"Business filings: {len(biz)}")
for r in biz[:5]:
    print(f"  {r.case_number}  {r.debtor_names[0]}")
