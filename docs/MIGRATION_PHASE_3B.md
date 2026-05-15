# Phase 3.B - Bankruptcy RSS scraper module

> **Status:** Standalone scraper module. Tests pass standalone (64 tests).
> Not wired into the pipeline yet — that's Phase 3.E.
> **Risk:** Low. Pure additive: one new module, one new test file. No
> existing file is modified.

## What's in this tarball

```
scraper/bankruptcy.py        NEW - RSS fetch + parse + extract module
tests/test_bankruptcy.py     NEW - 64 tests, no network access required
```

## What this ships

- `BankruptcyRecord` dataclass — one record per voluntary petition
- `fetch_voluntary_petitions(feed_url)` — top-level entry point
- `fetch_feed(url)` / `parse_feed(xml)` — composable pieces
- `extract_voluntary_petitions(items)` — filter + extraction
- Private parsing helpers (case number, debtor names, joint-filing split,
  business-name detection, chapter/office/trustee extraction)
- Exception types: `BankruptcyFeedError`, `BankruptcyParseError`

## What this does NOT ship (deferred)

- **DCAD owner-name matching** — Phase 3.C. The big question: can we
  convert "Eddie C. Watkins" (RSS format) into "WATKINS EDDIE C" (DCAD
  format) and find a match in the 700k-owner index?
- **Canonicalization to the pipeline schema** — Phase 3.C.
- **Scoring category** (`BANKRUPTCY_FED` weight) — Phase 3.D.
- **Pipeline integration in main.py** — Phase 3.E.
- **State management** for cross-run deduplication — handled by the
  existing `_merge_seen_dates` logic in main.py via stable record_ids
  (also Phase 3.C).

## Apply steps

```powershell
cd C:\Users\MarkFuller\Desktop\dallas-intel
tar -xzf $HOME\Downloads\dallas-intel-phase-3b.tar.gz
```

Verify the new files landed without touching anything else:

```powershell
git status
```

Should show:
```
Untracked files:
  scraper/bankruptcy.py
  tests/test_bankruptcy.py
```

Run the full test suite:

```powershell
pytest tests\
```

Expected: **331 passed** (267 existing + 64 new).

## Smoke test against the live feed

The module works end-to-end against the real RSS. Verify with a
one-liner (the same heredoc pattern we've been using):

```powershell
@'
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
print("Business filings in this batch:")
biz = [r for r in records if r.is_business]
for r in biz:
    print(f"  {r.case_number}  {r.debtor_names[0]}")
'@ | Set-Content -Encoding UTF8 smoke_bankruptcy.py

python smoke_bankruptcy.py
```

Expected output: ~20-40 voluntary petitions, mix of Ch.7 and Ch.13, a
small number flagged `is_business=True` (these get skipped from the
homeowner pipeline in Phase 3.C).

## Decisions logged

| Decision | Choice | Rationale |
|---|---|---|
| Filter level | Voluntary petitions only (skip other docket activity) | Highest-value lead signal. Discharge orders / motions are noise for new-lead generation. |
| Business detection timing | Before joint-split | "Watts and Watts LLC" should be classified business, not joint. |
| Business filings | Tagged, not removed | Same pattern as buy-box. The Phase 3.C matcher will skip them; preserved for audit. |
| Trustee field | Optional | Ch.7 voluntary petitions typically have no trustee at filing time. |
| Office codes | Treated as opaque IDs | Not all N.D. Texas filings are Dallas-area; the DCAD match in 3.C naturally filters. |
| Network IO | Isolated in `fetch_feed` | Unit tests use canned XML; smoke test uses real network. |
| Error handling | Custom exceptions (`BankruptcyFeedError`, `BankruptcyParseError`) | Pipeline integration in 3.E can catch and continue, matching the foreclosure-PDF pattern. |

## What to look for in the smoke-test output

If the smoke test produces:

- **~20-40 voluntary petitions** → volume matches the probe (28 in the
  last test run). Healthy.
- **Mix of Ch.7 and Ch.13** → consumer bankruptcy mix, which is what
  we want for motivated-seller leads.
- **Some `is_business=True`** → business-filter is doing its job.
- **Joint filers split correctly** (e.g. one record with 2 entries in
  `debtor_names`) → joint-split logic works.

If the smoke test fails or produces 0 records — paste the output and
we triage.

## Next step

Once this lands clean: Phase 3.C — DCAD owner-name matching. That's
where we find out the actual conversion rate from voluntary-petition
debtor to enriched lead. If 8-12 of the 28 daily petitions match a
Dallas County DCAD owner, you've roughly doubled the daily lead volume.
