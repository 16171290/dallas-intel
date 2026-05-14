# Phase 0.A — Governmental-grantor suppression + Buy-box filter

> **Status:** Pre-flight foundation work. Independent of all source migrations.
> **Risk:** Low. Two new modules, two test files, ~10 lines of integration in
> `main.py`. Reversible via git revert.
> **Outcome:** Immediately reduces caller-time waste on (1) governmental-entity
> records that aren't sellers and (2) properties outside your buy-box.

## What this patch contains

```
scraper/
  governmental_grantor.py    NEW — suppression module
  buy_box.py                 NEW — buy-box filter
tests/
  test_governmental_grantor.py    NEW — 28 tests
  test_buy_box.py                 NEW — 50 tests
```

All 78 new tests pass standalone (run during build).

Nothing existing is modified by these files. Integration into `main.py`
is a separate manual step described in §3 below.

---

## 1. Apply the new files

```powershell
cd C:\Users\MarkFuller\Desktop\dallas-intel
tar -xzf $HOME\Downloads\dallas-intel-phase-0a.tar.gz
```

This drops:
- `scraper/governmental_grantor.py`
- `scraper/buy_box.py`
- `tests/test_governmental_grantor.py`
- `tests/test_buy_box.py`

Verify nothing existing was overwritten:

```powershell
git status
# Should show 4 untracked files only — no modified files.
```

## 2. Verify the new tests pass

```powershell
.\.venv\Scripts\Activate.ps1
pytest tests\
# Should now show 189 + 78 = 267 passing tests.
```

If your existing test count differs from 189, the new total will differ
correspondingly. The key thing is that the 78 new tests pass.

## 3. Wire into `main.py`

The integration point is **after canonicalization+enrichment+scoring,
before output**. In the current pipeline that's between the existing
HOA-filter step and the records.json write.

Find the section in `scraper/main.py` that looks roughly like this
(the exact form may differ — match by intent, not by exact text):

```python
# Existing: HOA filtering happens here
records = normalize.filter_hoa_records(records)
# Existing: dedup
records = _dedup_by_record_id(records)
# Existing: output
output.write_records(records, ...)
```

Insert two new steps between the HOA filter and the output:

```python
from scraper import buy_box, governmental_grantor

# ... existing HOA filter ...
records = normalize.filter_hoa_records(records)

# NEW — governmental-grantor suppression
records, gov_removed = governmental_grantor.filter_governmental_records(records)
logger.info(
    "Governmental-grantor filter: removed %d records (e.g. %s)",
    len(gov_removed),
    [r.get("grantor", "?") for r in gov_removed[:3]],
)

# NEW — buy-box annotation (does NOT remove records; tags them)
bb = buy_box.BuyBox.from_env()
buy_box_summary = buy_box.annotate_records(records, bb)
logger.info("Buy-box: %s", buy_box_summary)

# ... existing dedup + output ...
records = _dedup_by_record_id(records)
output.write_records(records, ...)
```

**Important semantic distinction:**

- `filter_governmental_records` **removes** records entirely (returns
  `(kept, removed)` tuple). Governmental-grantor records are not seller
  leads, full stop.
- `buy_box.annotate_records` **annotates** records with
  `in_buy_box: bool` and `buy_box_reasons: list[str]` fields. Records
  remain in `records.json` for audit, but downstream consumers (CSV
  export, dashboard) should filter on `in_buy_box=True`.

This split lets you see what was excluded from the call list and why,
without destroying the data.

## 4. Filter the CSV export on `in_buy_box`

In `scraper/output.py`, find the CSV writer. Add a filter on records:

```python
def write_csv_export(records, path):
    # Existing CSV write logic — filter to in-buy-box only
    csv_records = [r for r in records if r.get("in_buy_box", True)]
    # ... rest of existing CSV write ...
```

The `.get("in_buy_box", True)` default ensures records written by older
pipeline runs (before this patch) still appear in the CSV.

The full records.json keeps everything so the dashboard can show "we
scraped 500, 320 in-buy-box, 180 outside" if you choose to surface that.

## 5. Configure the buy-box (env vars)

The buy-box reads from environment variables. Set whichever apply.
All are optional — leaving them unset means the filter is inert.

Edit `.env` (local) and add corresponding GitHub Actions secrets for CI:

```env
# Price range — DCAD market value bounds
BUY_BOX_MIN_PRICE=80000
BUY_BOX_MAX_PRICE=750000

# ZIP allowlist — only records in these ZIPs are in-buy-box
# Comma-separated. Leave unset to allow all Dallas County ZIPs.
BUY_BOX_ZIP_ALLOWLIST=75201,75202,75204,75206,75214,75218,75223,75228

# Optional: ZIP denylist (excluded even if allowlist includes them)
# BUY_BOX_ZIP_DENYLIST=

# Optional: require DCAD match (skip records whose address couldn't be
# resolved to a DCAD account). Defaults False so unenriched records
# still appear in the audit view.
# BUY_BOX_REQUIRE_DCAD_MATCH=false
```

**Recommendation:** start with just `BUY_BOX_MIN_PRICE` and
`BUY_BOX_MAX_PRICE` set to your actual range. Skip ZIP filtering on
the first run so you can see the full geographic spread. Add ZIP
filtering after a week of observation.

For GitHub Actions, add each as a secret in
`Settings → Secrets and variables → Actions`, and the workflow YAML
already references env-vars so they'll flow through. (No workflow
change needed.)

## 6. Surface counts in Discord

Optional but high-value. In `scraper/monitor.py`, find the
`notify_run_complete` function and extend the embed:

```python
# Existing fields...
embed.add_field(
    name="Governmental filter",
    value=f"{stats.get('gov_filtered_count', 0)} records removed",
    inline=True,
)
embed.add_field(
    name="Buy-box",
    value=(
        f"{stats['buy_box']['in_buy_box']} / {stats['buy_box']['total']} "
        f"in buy-box ({stats['buy_box']['criteria']})"
    ),
    inline=True,
)
```

And in `main.py`, when you build the stats dict that gets passed to
`monitor.notify_run_complete`, include the new fields:

```python
stats = {
    # ... existing fields ...
    "gov_filtered_count": len(gov_removed),
    "buy_box": buy_box_summary,
}
monitor.notify_run_complete(stats)
```

## 7. Validate end-to-end

Run the pipeline once locally with the new filters active:

```powershell
$env:BUY_BOX_MIN_PRICE = "80000"
$env:BUY_BOX_MAX_PRICE = "750000"
$env:CRON_JITTER_MINUTES = "0"  # skip the jitter sleep for this manual run
python -m scraper.main
```

Then check the output:

```powershell
# Records.json should have new fields on every record
python -c "
import json
data = json.load(open('data/records.json', encoding='utf-8'))
records = data['records']
print(f'Total records: {len(records)}')
print(f'In buy-box:    {sum(1 for r in records if r.get(\"in_buy_box\"))}')
print(f'Outside:       {sum(1 for r in records if not r.get(\"in_buy_box\"))}')
print()
print('Sample outside-buy-box reasons:')
for r in records[:5]:
    if not r.get('in_buy_box') and r.get('buy_box_reasons'):
        print(f'  - {r[\"buy_box_reasons\"]}')
"
```

Expected observations:
- Governmental-grantor records (Trinity River Authority et al.) no
  longer appear in records.json at all.
- Records outside your price range show `in_buy_box: false` with a
  human-readable reason.
- CSV export contains only in-buy-box records.

## 8. Rollback

If anything is wrong:

```powershell
git revert <commit-sha>     # if already committed
# OR
git checkout -- scraper\main.py scraper\output.py scraper\monitor.py
Remove-Item scraper\governmental_grantor.py
Remove-Item scraper\buy_box.py
Remove-Item tests\test_governmental_grantor.py
Remove-Item tests\test_buy_box.py
```

The pipeline returns to pre-patch behavior.

---

## What this DOES NOT do (deferred to later phases)

- **Skip-trace integration.** Big operational win, but it's a paid
  service integration with its own design decisions. Phase 0.C.
- **Call-script-ready summary.** Each lead getting a one-line
  human-readable reason-to-call. Phase 0.C.
- **Hot list separation.** Pulling NOF/AT/PB urgent records into a
  separate hot-list output with same-day Discord push. Phase 0.B.
- **Structured logging + per-source metrics + DOM-snapshot-on-failure.**
  Observability foundation. Phase 0.B.
- **Commercial-property exclusion via DCAD COM_DETAIL.** The
  `exclude_commercial=True` flag is plumbed but not yet enforced.
  Phase 0.B (when we add the COM_DETAIL cross-reference).

## What this enables for future phases

Once the buy-box filter exists, every new source we add in Phase 1+
(Odyssey, PACER, Linebarger, etc.) flows through it automatically as
long as their canonicalizer produces records with the standard
`dcad_market_value` and `address` fields. Same with the governmental
filter — Phase 4 Linebarger records will land with `LINEBARGER GOGGAN`
as the grantor (the tax-collection firm acting as agent), and the
filter catches them so we surface only the actual taxpayer-grantee.

## Decisions logged

| Decision | Choice | Rationale |
|---|---|---|
| Governmental-grantor: remove or tag? | **Remove** | Pure noise, never actionable. Audit log via `removed` return value. |
| Buy-box: remove or tag? | **Tag** (`in_buy_box`) | Audit visibility. Out-of-buy-box records may be useful for spotting market shifts. |
| Missing data behavior | **Don't exclude** | We don't penalize unenriched records by default. Operator can flip `require_dcad_match=True` to opt-in to stricter behavior. |
| ZIP allowlist source | **Env var, not config.py** | Operator can tune without code changes. |
| Reason-string format | **`category: detail`** | Allows downstream bucketing by category prefix. |
