# PR 8 — Probate Decedent Property Resolution: Recon Report

**Date:** 2026-05-27
**Status:** Investigation. No code changes proposed yet — scope decision required from operator.

## Today's miss rate

Production GHA run 2026-05-27 produced 50 PB records:

| Outcome | Count | % |
|---|---|---|
| Decedent matched to DCAD owner_index | 29 | 58% |
| **Unresolved (no `dcad_account`)** | **21** | **42%** |

(Note: GHA log says 29% but that's measured over the raw 99 probate records before dedup/filter. Of the 50 PBs that survived to final output, 29/50 = 58% matched.)

## Where the 21 unresolved records come from

| Bucket | Count | Source | Signal we have | Signal we'd need |
|---|---|---|---|---|
| Tyler — no signals | **12** | re:SearchTX case list | decedent name only | external data |
| Tyler — applicant matched, decedent didn't | 4 | re:SearchTX case list | applicant's mailing addr | decedent's DCAD entry |
| publicsearch — has legal description | 1 | publicsearch.us | subdivision + lot + block | Path C bug investigation |
| publicsearch — "N/A \| N/A" | 4 | publicsearch.us | nothing | external data |

**Take-away:** 16 of 21 unresolved are *Tyler-source* records. Tyler exposes only the case-list page in our current scrape — the decedent name is all we get. If DCAD owner_index doesn't match the name, dead end.

## What data sources exist for the decedent's property?

### Inside our current data, not yet exploited

| Source | What it could resolve | Cost | Confidence |
|---|---|---|---|
| **DCAD `OWNER_NAME2` column** | Joint-ownership properties where decedent is second-listed (Steinhart case fits this exactly — Phyllis is owner #2 at 25 ROBLEDO DR) | ~20 lines (extend `_OWNER_NAME_CANDIDATES`) | **High** |
| **DCAD `MULTI_OWNER` table** | Joint ownership beyond 2 (condos, family trust co-ownership) | ~40 lines (parse + merge into owner_index) | **High** |
| **DCAD "Estate of X" entries** | Properties already retitled to the estate after death | ~30 lines (regex on decedent surname in DCAD's "EST OF" or "ESTATE OF" rows) | High |
| **Path C fuzzy debug** | The 1 publicsearch PB with `SPRING CREEK Lot: 5 Block: 2` — Path C should have resolved. Worth checking why it didn't (legal_index miss vs. multi-match vs. norm mismatch) | ~1 hour debug | High |

### External sources (require new scrapers)

| Source | What it could resolve | Cost | Confidence |
|---|---|---|---|
| **Tyler case detail page** | The 12 Tyler "no signals" records. Each case has a Filings tab showing Inventory & Appraisement filings — those list property addresses (required by TX Estates Code §309.051). | ~300 lines + Playwright per-case nav (~2-3s per record × 100/week = ~5 min/run) | **High** but staged: applies to ~80% of probate types (Independent + Dependent Administration). Heirship cases sometimes skip inventory. |
| **Tyler Inventory PDF OCR** | Same target as above but parses the actual filed PDF (which includes detailed legal descriptions, addresses, valuations) | ~500 lines (clone foreclosure_ocr machinery) | High; redundant with case-detail scrape if both list same data |
| **publicsearch.us deed-history per decedent** | Decedent's name searched in Dallas County deed records — every property they ever owned via grantee position | ~200 lines + N searches per run | Medium — slow, many false positives on common names |
| **Cross-county appraisal districts** | Decedent might own property in Tarrant, Collin, Denton, Rockwall. Each county has its own bulk-data product. | ~200 lines per county + bulk download bandwidth | Medium-low — only matters if a Dallas-County probate decedent owned property elsewhere (uncommon; probate venue usually = property location) |

### What we know does NOT help

- **applicant_mailing** is the heir's *home* address, not necessarily the decedent's property. Sometimes they coincide (heir moved into inherited home) but often don't. Hein case proves this: applicant Quinn's home is in Sachse, Collin County — irrelevant to decedent Eddie Hein's Dallas property.

## Concrete spot-checks (5 unresolved cases)

| Record | decedent | applicant_mailing | Likely fix |
|---|---|---|---|
| `pro-d03bc7ce...` Steinhart | Phyllis Y. | Ronald G., 25 ROBLEDO DR (owns jointly w/ Phyllis based on garbled "PHYLLIS Y 25 ROBLEDO DR" string) | **OWNER_NAME2 / MULTI_OWNER indexing** would catch Phyllis at this address directly |
| `pro-719bcc26...` Grage | Constance Kihneman | Fleming Alison, 7 STONECOURT DR | Applicant inherited & lives there → 7 STONECOURT is Constance's old property. OWNER_NAME2 might catch this if it was joint. |
| `pro-b6909f40...` Hein | Eddie | Quinn Kevin, 6613 EASTVIEW DR Sachse (Collin County) | Applicant is unrelated, lives elsewhere → no inference. Need **Tyler case detail or DCAD deed search**. |
| `pro-dbec6151...` Dunn → Brown | Sarah Dunn | (none) | Applicant Norene Brown has no DCAD match — different surname → not a relative? Need **Tyler case detail**. |
| `pro-cb0251eb...` Eldridge | Margaret A. | (none) | Two Eldridges → likely parent/child but neither matches DCAD. Need **DCAD MULTI_OWNER** or **Tyler case detail**. |

## Recommended PR 8 staging

I'd propose splitting this into two phases:

### Phase 1 — DCAD-side improvements only (PR 8a)

**Scope:** index `OWNER_NAME2` + `MULTI_OWNER` table + handle "ESTATE OF X" patterns.

**Effort:** small (~100 lines + tests).

**Expected lift:** 3-6 records out of the 21 unresolved (the joint-ownership cases like Steinhart). Could be more — production probably has many more joint-ownership probates we're missing.

**Risk:** low. Adds candidates to owner_index without changing matcher tiers.

### Phase 2 — Tyler case detail scrape (PR 8b)

**Scope:** for each Tyler probate record, navigate to the case detail page; if an Inventory & Appraisement filing exists, parse property addresses out of its description.

**Effort:** medium-large (~300-500 lines). Per-case Playwright nav. May require I&A PDF OCR for some cases.

**Expected lift:** 5-12 records (the Tyler "no signals" bucket where the property is in the filed inventory).

**Risk:** medium. Tyler's filing list format may vary; some I&A filings are filed by attorney name only, not titled correctly. Sealed cases (rare in probate) would return nothing.

### Phase 3 — Path C debug (PR 8c, or could be part of PR 8a)

**Scope:** investigate why `315553797 Gordon, Finis Lee` with `SPRING CREEK Lot: 5 Block: 2` wasn't resolved by Path C.

**Effort:** ~1 hour debug + fix.

**Expected lift:** 1 record now, unknown how many in future runs.

## Open questions for you

1. **Are we OK with Tyler per-case Playwright navigation?** Each PB run would add ~5-10s of Playwright time for case detail fetches (similar cost to Stage 6.6 page tiebreaks). Acceptable?

2. **Do we ship Phase 1 (DCAD-side wins) first as PR 8a alone, then Phase 2 as a follow-up PR?** Or roll them into one big PR 8?

3. **Cross-county appraisal districts** — out of scope? Probate venue is usually where the decedent lived = where the property is. The case for cross-county is weak unless we have a specific failure mode driving it. Recommend skip.

4. **Inventory PDF OCR** — defer unless Phase 2 case-detail scrape proves insufficient?

Standing by for direction on which scope to implement.
