# DCAD Bulk-Data Schema

> **STATUS: VERIFIED 2026-05-13** against `DCAD2025_CURRENT.ZIP`.
> All column references in `scraper/dcad_bulk.py` and `scraper/enrichment.py`
> match the schema below.

## Source

- **URL:** `https://www.dallascad.org/DataProducts.aspx`
- **Resolved ZIP for year Y:** `DCAD{Y}_CURRENT.ZIP`
- **Auth:** None (free public bulk data per `OpenRecords.aspx`)
- **Format:** Comma-delimited (`.csv` / `.txt`)
- **Cache strategy:** Weekly local cache keyed by ISO year-week
- **Robots posture:** `/DataProducts.aspx` explicitly permitted (§B.2.3)

## Bundle contents (14 tables)

| Table | Rows (2025) | Used by pipeline? | Purpose |
|---|---:|---|---|
| `ACCOUNT_INFO` | 858,513 | ✅ address index + owner | One row per parcel; situs address, owner, mailing address |
| `ACCOUNT_APPRL_YEAR` | 858,513 | ✅ market value | Per-year appraisal values + jurisdictional splits |
| `APPLIED_STD_EXEMPT` | 430,957 | ✅ homestead + signals | Active standard exemptions (homestead, over-65, disabled, deferred) |
| `MULTI_OWNER` | 107,821 | ✅ fallback only | Multi-party ownership (used when ACCOUNT_INFO owner is blank) |
| `RES_DETAIL` | 680,454 | ⬜ future scoring | Residential property attributes (year built, sqft, beds/baths) |
| `RES_ADDL` | 779,955 | ⬜ | Additional residential improvements |
| `COM_DETAIL` | 92,025 | ⬜ | Commercial property attributes |
| `LAND` | 747,075 | ⬜ | Land segments per account |
| `TAXABLE_OBJECT` | 874,332 | ⬜ | Tax accounts + jurisdictions |
| `ACCT_EXEMPT_VALUE` | 1,504,867 | ⬜ | Per-jurisdiction exemption-applied values |
| `ACCOUNT_TIF` | 40,193 | ⬜ | Tax Increment Financing zone overlays |
| `ABATEMENT_EXEMPT` | 2,841 | ⬜ | Abatement exemptions |
| `TOTAL_EXEMPTION` | 38,665 | ⬜ | Fully-exempt accounts (govt, religious, etc.) |
| `FREEPORT_EXEMPTION` | 2,050 | ⬜ | Freeport inventory exemptions |

## Tables the pipeline reads — key columns

### `ACCOUNT_INFO`

Used by `dcad_bulk.build_address_index()` for the address → ACCOUNT_NUM
lookup, and by `enrichment.enrich_record()` for the primary owner name.

| Column | Type | Use |
|---|---|---|
| `ACCOUNT_NUM` | string | Primary key |
| `STREET_NUM` | string | House/building number |
| `STREET_HALF_NUM` | string | Fractional (e.g. "1/2") |
| `FULL_STREET_NAME` | string | Directional + name + suffix (one field, e.g. "N MAIN ST") |
| `OWNER_NAME1` | string | Primary owner (UPPERCASE) |
| `OWNER_NAME2` | string | Secondary owner — joined to NAME1 with `&` |
| `BIZ_NAME` | string | Business name (commercial parcels) |
| `APPRAISAL_YR` | string | Year of this snapshot |

Other useful columns not currently consumed: `PROPERTY_CITY`, `PROPERTY_ZIPCODE`,
`OWNER_ADDRESS_LINE1`-`4`, `OWNER_CITY`, `OWNER_STATE`, `OWNER_ZIPCODE`,
`DEED_TXFR_DATE` (last deed-transfer date — potential lead-age signal),
`LEGAL1`-`5`, `MAPSCO`, `NBHD_CD`, `BLDG_ID`, `UNIT_ID`.

### `ACCOUNT_APPRL_YEAR`

Used by `enrichment.enrich_record()` for the market value.

| Column | Type | Use |
|---|---|---|
| `ACCOUNT_NUM` | string | Join key |
| `APPRAISAL_YR` | string | Filter to `DCAD_TARGET_YEAR` |
| `TOT_VAL` | string→float | Total market value (`IMPR_VAL + LAND_VAL`) |
| `IMPR_VAL` | string | Improvement (building) value |
| `LAND_VAL` | string | Land value |
| `PREV_MKT_VAL` | string | Last year's total — useful for value-change scoring |
| `HMSTD_CAP_VAL` | string | Homestead-cap value (if capped) |

Other available: `LAND_AG_EXEMPT`, `AG_USE_VAL`, `REVAL_YR`, `PREV_REVAL_YR`,
`GIS_PARCEL_ID`, `APPRAISAL_METH_CD`, `BLDG_CLASS_CD`, `SPTD_CODE`, plus
per-jurisdiction taxable/ceiling fields for city / county / ISD / hospital /
college / special district (`CITY_TAXABLE_VAL`, etc.).

### `APPLIED_STD_EXEMPT`

Used by `enrichment.enrich_record()` for homestead status and distressed-seller
signals (over-65, disabled, tax-deferred).

> **Sentinel convention** — verified 2026-05-13.
> DCAD does **not** use empty/non-empty to indicate exemption presence. The
> sentinel string `UNASSIGNED` is used in the `_DESC` columns and in
> `HOMESTEAD_EFF_DT` to mean "this exemption is not active". The truthy
> values are:
>
> | Column | Active values | Inactive |
> |---|---|---|
> | `HOMESTEAD_EFF_DT` | a date (e.g. `01/01/2022`) | `UNASSIGNED`, empty |
> | `OVER65_DESC` | `OVER 65`, `SURVIVING SPOUSE` | `UNASSIGNED`, empty |
> | `DISABLED_DESC` | `DISABLED` | `UNASSIGNED`, empty |
> | `TAX_DEFERRED_DESC` | `PERMANENT` | `UNASSIGNED`, empty |
>
> `SURVIVING SPOUSE` retains the over-65 benefit for the deceased's spouse
> and is treated as an active over-65 exemption for our purposes.
> The `_any_active()` helper in `scraper/enrichment.py` enforces this.

| Column | Type | Use |
|---|---|---|
| `ACCOUNT_NUM` | string | Join key |
| `APPRAISAL_YR` | string | Year |
| `HOMESTEAD_EFF_DT` | string | Date or `UNASSIGNED` (see sentinel convention) |
| `OVER65_DESC` | string | See sentinel convention above |
| `DISABLED_DESC` | string | See sentinel convention above |
| `TAX_DEFERRED_DESC` | string | See sentinel convention above |

A single account may have **multiple rows** in `APPLIED_STD_EXEMPT`
(one per `OWNER_SEQ_NUM`). The pipeline applies `any()` across rows —
if any owner of a parcel has the exemption, the parcel gets flagged.

Other available: `VET_DISABLE_PCT`, `VET_FLAT_AMT` (and a second set `VET2_*`),
`CITY_CEIL_*` / `COUNTY_CEIL_*` / `ISD_CEIL_*` / `COLLEGE_CEIL_*` (per-jurisdiction
tax-ceiling tracking for senior/disabled), `CAPPED_HS_AMT`, `HS_PCT`,
`OWNER_SEQ_NUM`, `APPLICANT_NAME`, `PRORATE_IND` + related dates, `CIRCUIT_BK_FLG`
(circuit-breaker flag — also a distress signal).

### `MULTI_OWNER`

Used by `enrichment.enrich_record()` **only as a fallback** when
`ACCOUNT_INFO.OWNER_NAME1` is blank. Covers the 12.5% of accounts with 3+
owners or fractional ownership.

| Column | Type | Use |
|---|---|---|
| `ACCOUNT_NUM` | string | Join key |
| `OWNER_SEQ_NUM` | string | Order; first row = primary |
| `OWNER_NAME` | string | Name |
| `OWNERSHIP_PCT` | string | Percentage |

## Verified canonical-record fields after enrichment

A successfully-enriched record (canonical dict) gets these populated:

```
dcad_account        → ACCOUNT_INFO.ACCOUNT_NUM (string)
dcad_owner          → OWNER_NAME1 + " & " + OWNER_NAME2 (or MULTI_OWNER fallback)
dcad_market_value   → ACCOUNT_APPRL_YEAR.TOT_VAL (float)
dcad_homestead      → APPLIED_STD_EXEMPT.HOMESTEAD_EFF_DT non-empty (bool)
dcad_over65         → APPLIED_STD_EXEMPT.OVER65_DESC non-empty (bool)
dcad_disabled       → APPLIED_STD_EXEMPT.DISABLED_DESC non-empty (bool)
dcad_tax_deferred   → APPLIED_STD_EXEMPT.TAX_DEFERRED_DESC non-empty (bool)
```

## Tables NOT yet consumed (potential future enrichment)

- `RES_DETAIL` / `RES_ADDL` — square footage, year built, bed/bath counts.
  Useful for value-stratified scoring and for excluding mobile homes if needed.
- `COM_DETAIL` — commercial buildings; identifies commercial parcels for
  filtering or separate scoring.
- `LAND` — agricultural-use and oversize-lot detection.
- `ACCT_EXEMPT_VALUE` — per-jurisdiction dollar amounts of each exemption.
  More granular than `APPLIED_STD_EXEMPT` if you need exact tax savings.
- `ACCOUNT_TIF` — TIF-zone overlays. Properties in TIF zones may have
  reinvestment requirements or restrictions.

Each represents a potential phase-2 scoring or filter signal. None block
the current pipeline.

## Re-verification

The schema can drift if DCAD changes their data products. Re-verify after
the annual certification cycle (late July) or if `EnrichmentStats.hit_rate`
suddenly drops:

```powershell
.\.venv\Scripts\Activate.ps1
python -c "
from scraper import dcad_bulk
tables = dcad_bulk.parse_dcad_tables(dcad_bulk.fetch_dcad_zip())
for name in sorted(tables):
    print(f'{name}:')
    print(f'  {list(tables[name].columns)}')
"
```

Compare against this document. If any column referenced in
`scraper/dcad_bulk.py` or `scraper/enrichment.py` is missing or renamed,
update the code and add a regression test.
