# Dallas County Instrument Codes

Maps Harris-Intel's 12 instrument categories (§3.1.2) to the literal
Dallas County document-type codes captured from
`window.__data.configuration.docTypeMappings` on `dallas.tx.publicsearch.us`.

The authoritative source for the mapping is `scraper/config.INSTRUMENT_CODES`.
This document explains *why* each mapping exists; the code is the
canonical reference.

## Category map

| # | Harris category | Dallas codes | Rationale |
|---|---|---|---|
| 1 | **L/P** (lis pendens) | `LP` | Notice of pending lawsuit affecting title. `RLP` = Release of Lis Pendens (suppression signal — see §E.1) |
| 2 | **NOTICE** (notice of foreclosure) | `NOF` | Notice of Foreclosure recorded by trustee. Also covered by the dallascounty.org foreclosure-PDF stream (§3.7.8). |
| 3 | **TRSALE** (trustee sale) | `TD` | Trustee's Deed — record of the sale itself. |
| 4 | **LIEN** | `LN`, `LC`, `ML` | `LN` = general lien; `LC` = lien claim/contractor; `ML` = mechanic's lien. |
| 5 | **T/L** (tax lien) | `TXL` | Texas state/county/city tax lien. |
| 6 | **JUDGE** (judgment lien) | `JUD` | Court judgment recorded as lien against property. |
| 7 | **A/J** (abstract of judgment) | `AJ` | Abstract filed by judgment creditor. Distinct from `JUD` — different filing posture. |
| 8 | **PROB** (probate) | `PB` | Probate-related filings (will admitted, executor letters, etc.). |
| 9 | **DEED** | `D`, `WD`, `SWD`, `GWD`, `QCD`, `SD` | All deed flavors: General deed, Warranty Deed, Special Warranty Deed, General Warranty Deed, Quitclaim Deed, Sheriff's Deed. |
| 10 | **BNKRCY** (bankruptcy) | `BR` | Bankruptcy filings affecting property. |
| 11 | **LEVY** | `SZS`, `TXL` | `SZS` = Sheriff's Seizure; `TXL` overlaps with T/L — note the dual mapping. |
| 12 | **REL/FTL** (release / federal tax lien) | `REL`, `FTL` | `REL` = generic release (suppression signal); `FTL` = federal tax lien. |

## Suppression codes (§E.1)

Codes that mark a *prior* record as released/inactive rather than
creating a new lead:

| Code | Suppresses | Notes |
|---|---|---|
| `REL` | All same-address prior records | Generic release |
| `RLP` | Same-address prior `LP` records | Specifically releases a lis pendens |

Suppression logic lives in `scraper.scorer.suppress_released()`. The
REL/RLP records themselves are dropped from the output (they're not
leads); the records they release are marked `active = False` and stamped
with `release_record_id`.

## LP+FC combo (§E.4)

The +20 bonus for an L/P + NOTICE combination at the same address fires
when both:
- A record with code `LP` (Category L/P) **and**
- A record with code `NOF` (Category NOTICE)

are present at the same `address_normalized`. This is one of the
strongest distressed-property signals — lis pendens typically precedes
or accompanies foreclosure proceedings. See `config.LP_FC_PAIR`.

## Verifying codes against publicsearch.us

The literal codes were captured from the page's embedded configuration:

```javascript
// In the browser console on dallas.tx.publicsearch.us:
window.__data.configuration.docTypeMappings
```

If the SPA ships new codes (or renames existing ones) the search will
silently miss them. To re-verify periodically:

```powershell
# PowerShell, in the project root
.\.venv\Scripts\Activate.ps1
playwright codegen https://dallas.tx.publicsearch.us/
```

In the codegen window, open the Advanced Search → Doc Type dropdown
and observe the values. Reconcile against `config.INSTRUMENT_CODES`.

## Open questions for verification

- Confirm `BR` is the active code (vs `BNK`, `BKY`, etc.)
- Confirm `AJ` and `JUD` are still separate codes (some counties merged)
- Whether `TXL` resolves to T/L vs LEVY in practice (currently both;
  scoring stacks if so)
- Whether `SD` (Sheriff's Deed) is a separate code or rolled into `D`
