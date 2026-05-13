# DCAD Bulk-Data Schema

> **STATUS: STRONG INFERENCE — pending Phase 1 verification.**
>
> Until the first real ZIP download is parsed against this doc and
> any drift is corrected, treat every column name below as a hypothesis,
> not a fact. The Phase 1 verification step (per `ARCHITECTURE.md` §D.3.1)
> is to run `dcad_bulk.parse_dcad_tables()` against a real download and
> reconcile.

## Source

- **URL:** `https://www.dallascad.org/DataProducts.aspx`
- **Auth:** None (free public bulk data per `OpenRecords.aspx`)
- **Format:** Comma-delimited (preferred) — yearly snapshot ZIP
- **Cache strategy:** Weekly local cache keyed by ISO year-week (§3.7.6)
- **Robots posture:** `/DataProducts.aspx` explicitly permitted by
  DCAD's `robots.txt` (§B.2.3)

## Expected tables (STRONG INFERENCE)

Based on the §A.4 inference from the recon phase. Names are uppercased
in the parsed dict (no extension).

| Table | Inferred purpose | Verified? |
|---|---|---|
| `ACCOUNT_INFO` | One row per parcel; address + situs fields | ⬜ |
| `ACCOUNT_APPRL_YEAR` | Per-year appraisal values | ⬜ |
| `MULTI_OWNER` | Owner names; can be 1..N per account | ⬜ |
| `RES_DETAIL` | Residential property attributes | ⬜ |
| `COM_DETAIL` | Commercial property attributes | ⬜ |
| `LAND` | Land segments per account | ⬜ |
| `TAXABLE_OBJECT` | Tax accounts + jurisdictions | ⬜ |
| `APPLIED_STD_EXEMPT` | Active exemptions (homestead, over-65, etc.) | ⬜ |

Check the box once the column structure has been confirmed against a
real ZIP download.

## Expected columns by table (STRONG INFERENCE)

### `ACCOUNT_INFO`
Used by `dcad_bulk.build_address_index()` to construct the
normalized-address → account-num lookup.

| Column | Type | Notes |
|---|---|---|
| `ACCOUNT_NUM` | string | Primary key. Treat as string (preserves leading zeros). |
| `STREET_NUM` | string | House/building number |
| `PREFIX_DIR` | string | Directional before street name (N, S, E, W) |
| `STREET_NAME` | string | Street name |
| `STREET_SUFFIX` | string | ST, AVE, BLVD, etc. |
| `SUFFIX_DIR` | string | Directional after suffix (rare) |

> **If column names differ:** update both `dcad_bulk.build_address_index()`
> and the `optional_cols` tuple inside that function. Add the verified
> names to this table and check the verified box above.

### `ACCOUNT_APPRL_YEAR`
Used by `enrichment.enrich_record()` to populate `dcad_market_value`.

| Column | Type | Notes |
|---|---|---|
| `ACCOUNT_NUM` | string | Join key |
| `APPRAISAL_YR` | string | "2026", etc. Filter to current `DCAD_TARGET_YEAR` |
| `MARKET_VAL` | string→float | Total market value; strip commas, convert downstream |
| `APPRAISED_VAL` | string→float | After-cap value (relevant for homestead-capped parcels) |

### `MULTI_OWNER`
Used by `enrichment.enrich_record()` to populate `dcad_owner`.

| Column | Type | Notes |
|---|---|---|
| `ACCOUNT_NUM` | string | Join key |
| `OWNER_SEQ_NUM` | string | Order; first row is the primary owner |
| `OWNER_NAME` | string | UPPERCASE typically |

### `APPLIED_STD_EXEMPT`
Used by `enrichment.enrich_record()` to populate `dcad_homestead`.

| Column | Type | Notes |
|---|---|---|
| `ACCOUNT_NUM` | string | Join key |
| `EXEMPT_CD` | string | Exemption code; `HS*` or `HOM*` → homestead |

## Verification procedure (Phase 1)

PowerShell, after a successful first run:

```powershell
# Activate env
.\.venv\Scripts\Activate.ps1

# Inspect the actual columns
python -c "from scraper import dcad_bulk; t = dcad_bulk.parse_dcad_tables(dcad_bulk.fetch_dcad_zip()); print({k: list(v.columns) for k, v in t.items()})"
```

For each table:
1. Confirm the expected columns exist
2. Update this document with the verified column list
3. Tick the **Verified?** box
4. If columns differ, update `dcad_bulk.py` and `enrichment.py` accordingly
   and add a regression test that pins the verified names

## Reference materials inside the ZIP

DCAD typically ships schema documentation as `.txt` or `.pdf` files
inside the ZIP. Files matching `(readme|field|schema|layout|documentation)`
in the stem are deliberately skipped by `_is_data_member()` — extract
them manually for reference:

```powershell
python -c "
import zipfile
from pathlib import Path
z = Path.home() / '.dcad_cache' / 'dcad-2026-week<YYYYWW>.zip'
with zipfile.ZipFile(z) as zf:
    for n in zf.namelist():
        if any(k in n.lower() for k in ['readme', 'field', 'layout']):
            print(n)
            zf.extract(n, Path.home() / 'dcad-docs')
"
```

## Open questions

- Does DCAD use `MARKET_VAL` or `TOTAL_VAL` for the current market value?
- Are exemption codes prefixed differently (e.g. `HS` vs `HOMESTEAD`)?
- Is there an `OVER65` exemption code we should also flag for the dashboard?
- Does the comma-delimited format quote text fields containing commas?
  (pandas handles either way, but worth noting.)

Resolve and document above as you verify.
