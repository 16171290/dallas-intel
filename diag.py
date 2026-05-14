import json
d = json.load(open('data/records.json', encoding='utf-8'))
records = d['records']

print(f'Total: {len(records)}')
print()
print('Field population:')
for f in ['address','address_normalized','dcad_account','dcad_owner','dcad_market_value','dcad_homestead']:
    pop = sum(1 for x in records if x.get(f))
    pct = pop * 100 / len(records) if records else 0
    print(f'  {f:25s} {pop:4d} / {len(records)} ({pct:.1f}%)')

print()
print('Sample UNMATCHED records (have address, no DCAD):')
unmatched = [x for x in records if x.get('address') and not x.get('dcad_account')]
for x in unmatched[:8]:
    print(f'  addr=  {x["address"]!r}')
    print(f'  norm=  {x.get("address_normalized")!r}')
    print()
print(f'Total unmatched: {len(unmatched)}')
