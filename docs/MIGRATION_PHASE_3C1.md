# Phase 3.C.1 - DCAD owner index + bankruptcy name matcher

> **Status:** Two new standalone modules + 49 new tests.
> **Risk:** Low. Pure additive — no existing file modified.
> **Validation:** Run the smoke test at the bottom. It's the moment of
> truth for whether Phase 3 is worth completing.

## What's in this tarball

```
scraper/dcad_owner_index.py     NEW - builds {owner_name: [accounts]} index
scraper/name_matcher.py         NEW - converts/matches bankruptcy -> DCAD names
tests/test_dcad_owner_index.py  NEW - 28 tests
tests/test_name_matcher.py      NEW - 21 tests (49 new tests total)
```

## What this ships

- `build_owner_index(dcad_tables)` — reads DCAD's ACCOUNT_INFO and
  returns `{normalized_owner_name: [account_nums]}`. Handles joint
  ownership ("SMITH JOHN & MARY" → both names indexed → same account),
  role suffixes (TRUSTEE/ETUX/JR/FAMILY TRUST stripped), and defensive
  field-name lookup (works whether DCAD calls the column OWNER_NAME or
  OWNER_NAME1 or OWNERS_NAME).

- `convert_bankruptcy_to_dcad_format(name)` — `"Eddie C. Watkins"` →
  `"WATKINS EDDIE C"`.

- `match_debtor_to_dcad(name, owner_index)` — 3-tier match strategy:
  1. **exact** — full reformatted name
  2. **no_middle** — drop the middle name
  3. **last_first_initial** — shorten middle to a single letter

  Returns a `MatchResult(matched_name, accounts, match_strategy)` or
  `None`. Includes the strategy used so you can audit match quality.

- `match_debtor_names([names], index)` — batch helper for joint filers.
  Each debtor matched independently; one record can produce multiple
  matched parcels.

## What this does NOT ship (deferred)

- **Canonicalization** to the pipeline schema — Phase 3.C.2
- **Pipeline integration** in main.py — Phase 3.E
- **Scoring** (BANKRUPTCY_FED category) — Phase 3.D
- The `inspect_account_info_fields(tables)` helper is included for
  debugging when the index comes back empty — see "If the smoke test
  shows 0 matches" below.

## Apply steps

```powershell
cd C:\Users\MarkFuller\Desktop\dallas-intel
tar -xzf $HOME\Downloads\dallas-intel-phase-3c1.tar.gz
git status
```

Should show 4 untracked files in `scraper/` and `tests/`, nothing
modified.

Run the full test suite:

```powershell
pytest tests\
```

Expected: **380 passed** (331 from before + 49 new).

## Moment-of-truth smoke test

This script answers the question Phase 3 has been driving toward:
**how many of today's bankruptcy filers actually own Dallas County
property?**

```powershell
@'
"""
Phase 3.C.1 smoke test.

End-to-end match of today's bankruptcy filers against DCAD owners.
"""
from scraper import dcad_bulk, dcad_owner_index, bankruptcy, name_matcher
from collections import Counter
import os

# --- Step 1: load DCAD ---
print("Loading DCAD bulk data...")
zip_path = dcad_bulk.fetch_dcad_zip()
tables = dcad_bulk.parse_dcad_tables(zip_path)
print(f"  Tables: {sorted(tables.keys())}")

# --- Step 2: build owner index ---
print()
print("Building owner index...")
index = dcad_owner_index.build_owner_index(tables)
print(f"  Indexed {len(index):,} unique owner names")
if len(index) == 0:
    print()
    print("  WARNING: owner index is empty. Running diagnostic...")
    diag = dcad_owner_index.inspect_account_info_fields(tables)
    print(f"    table_name:     {diag['table_name']}")
    print(f"    row_count:      {diag['row_count']:,}")
    print(f"    owner_field:    {diag['matched_owner_field']}")
    print(f"    account_field:  {diag['matched_account_field']}")
    print(f"    keys_observed:  {diag['keys_observed'][:15]}")
    if diag['sample_rows']:
        print(f"    first row owner: {diag['sample_rows'][0]['owner_field']!r}")
        print(f"    first row acct:  {diag['sample_rows'][0]['account']!r}")
    raise SystemExit("Cannot proceed - owner index is empty.")

# --- Step 3: fetch today's bankruptcy filings ---
print()
print("Fetching today's bankruptcy RSS feed...")
records = bankruptcy.fetch_voluntary_petitions()
print(f"  {len(records)} voluntary petitions in last 24 hours")

# --- Step 4: attempt match for each debtor ---
print()
print("Attempting DCAD matches...")
matched     = []
unmatched   = []
businesses  = []
strategies  = Counter()

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

# --- Step 5: report ---
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
    n_accts = len(result.accounts)
    note = f"{n_accts} property" if n_accts == 1 else f"{n_accts} properties"
    print(f"  {rec.case_number_raw}  Ch.{rec.chapter}  {debtor!r}")
    print(f"      -> {result.matched_name!r}  via {result.match_strategy}  ({note})")
print()

print("Sample unmatched (first 10) - these debtors don't own under their")
print("filing-name in DCAD (likely renters or out-of-county):")
for rec, debtor in unmatched[:10]:
    converted = name_matcher.convert_bankruptcy_to_dcad_format(debtor)
    print(f"  {rec.case_number_raw}  {debtor!r}  ->  {converted!r}  NO MATCH")
'@ | Set-Content -Encoding UTF8 smoke_phase_3c1.py

python smoke_phase_3c1.py
```

## How to interpret the results

| Match rate | Verdict | Next step |
|---|---|---|
| **25-40%** | Healthy. Real homeowner population overlap. | Build Phase 3.C.2 (canonicalize) + 3.D (scoring) + 3.E (pipeline wiring). |
| **15-25%** | OK but not great. | Same as above; we still get a lead-volume boost. |
| **5-15%** | Marginal. | Worth shipping anyway; even a handful of high-distress leads per day has value. Consider expanding match strategies (Phase 3.C.3). |
| **0-5%** | Either a bug or genuinely low overlap. | Inspect: are converted names plausible? (Use the "Sample unmatched" output.) Are DCAD owner names in an unexpected format? Run the diagnostic. |

Based on the math:
- N.D. Texas covers many counties (Dallas, Tarrant, Collin, Denton, etc.)
- Maybe half the daily filings are from Dallas County properties
- Many bankruptcy filers rent; homeowner rate among filers is ~30-40%
- So expected: 15-25% of filers match Dallas-County DCAD owners

## If the smoke test shows 0 matches

Most likely cause: DCAD's ACCOUNT_INFO field name differs from our
defensive list. The diagnostic in the script prints what fields exist.
Compare against the constants in `scraper/dcad_owner_index.py`:

```python
_OWNER_NAME_CANDIDATES = (
    "OWNER_NAME", "OWNER_NAME1", "OWNERS_NAME",
    "OWNERNAME", "OWNER1", "NAME",
)
```

If DCAD uses a name not in the list, paste the diagnostic output and
we add it.

## Decisions logged

| Decision | Choice | Rationale |
|---|---|---|
| Index module is NEW, not added to dcad_bulk.py | Follows "drop-in adds only" preference | Lower risk, easier to revert. |
| Joint owner expansion | "SMITH JOHN & MARY" -> both names | Joint ownership is the norm in real estate. |
| Role suffix stripping | TRUSTEE, JR, ETUX, etc. | A person named JOHN SMITH should match whether DCAD has him as primary or as trustee. |
| 3-tier match ladder | exact -> no_middle -> first_initial | Each tier strictly looser. Last-name-only deliberately not implemented (common surnames over-match). |
| Match strategy tagged on result | Audit visibility | Lets us calibrate scoring later: an "exact" match is higher confidence than "no_middle". |
| Defensive field-name lookup | Try 6 common variants | DCAD's published schema has varied; this absorbs that drift. |

## Next step

Run the smoke test. Paste the output. Three possible outcomes:

1. **Healthy match rate** (15%+) → I build Phase 3.C.2 next.
2. **Low match rate but the diagnostic looks fine** → we look at sample
   unmatched names together and decide what to relax.
3. **Zero matches / empty index** → diagnostic output tells us exactly
   what to fix.
