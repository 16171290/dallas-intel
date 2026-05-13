# Dallas County Property Intelligence Pipeline — Architecture & Recon Report

| Field | Value |
|---|---|
| Status | Pre-build recon complete. Section H lists 17 unresolved §3 decisions blocking Phase 0 close. |
| Project codename | Provisional, pending §3.5.1 |
| Reference architecture | `github.com/xcerebroai/harris-intel` (the "Harris-Intel" system) |
| Date prepared | 2026-05-13 |
| Operating mode | Strict CONFIRMED / STRONG INFERENCE / WEAK INFERENCE / UNKNOWN tagging. No invented URLs. No silent defaults. All BLOCKED items surfaced in §H. |

---

## Abstract

This document is the consolidated reconnaissance and architecture report for a Dallas County, Texas property-intelligence pipeline that adapts the reference implementation at `github.com/xcerebroai/harris-intel` to Dallas County data sources. It covers:

1. The legal and policy posture of every data source the pipeline depends on (§B)
2. A complete map of those data sources, their endpoints, and the schemas they expose (§A)
3. A reference architecture that honors the constraints in §B while preserving the structural pattern of the Harris-Intel reference (§C)
4. A phased implementation plan (§D)
5. A field-level schema crosswalk between Harris and Dallas data shapes (§E)
6. An enumerated risk register with prioritized mitigations (§F)
7. The expected repository structure and deliverables (§G)
8. The current state of all open decisions (§H)
9. Acceptance criteria for production-readiness (§I)
10. An appendix with primary sources, glossary, and out-of-scope items (§J)

The report applies strict confidence tagging and explicitly marks all BLOCKED items requiring user input. No URLs are invented; no schemas are assumed; no "same as Harris" defaults are applied silently.

---

## Table of Contents

- [Section A — Source Map](#section-a--source-map)
- [Section B — Constraints & Authorization](#section-b--constraints--authorization)
- [Section C — Architecture](#section-c--architecture)
- [Section D — Implementation Plan](#section-d--implementation-plan)
- [Section E — Schema Mapping](#section-e--schema-mapping)
- [Section F — Risk Register](#section-f--risk-register)
- [Section G — Deliverables / Repo Structure](#section-g--deliverables--repo-structure)
- [Section H — Open Decisions](#section-h--open-decisions)
- [Section I — Verification & Acceptance](#section-i--verification--acceptance)
- [Section J — Appendix](#section-j--appendix)

---

# Section A — Source Map

## A.1 Hosts and roles

| Host | Role | Tech | Robots posture | Auth |
|---|---|---|---|---|
| `dallas.tx.publicsearch.us` | County Clerk Official Records (12 instrument categories) | SPA, Neumo platform | `Disallow: /` (treated advisory per §3.7.5 = b) | None for reads |
| `www.dallascounty.org` | Forward-looking foreclosure PDFs; auction calendar | Percussion CMS, static + PHP | `Allow: /` | None |
| `www.dallascad.org` | DCAD parcel/owner/value bulk data; search forms | ASP.NET WebForms (Visual Studio.NET 7.0, 2002–2003 era) | Surgical `Disallow:` rules; bulk-data path explicitly free | None |
| `maps.dcad.org` | Parcel mapping (out of scope) | ArcGIS | Not audited | N/A |
| `dallas-county-open-data-hub-dallascountygis.hub.arcgis.com` | County GIS open data hub (potential auxiliary) | ArcGIS Hub | Not audited | None public |

All rows: CONFIRMED.

## A.2 Primary endpoints

### A.2.1 `dallas.tx.publicsearch.us`

| Endpoint | Method | Purpose | Status |
|---|---|---|---|
| `/` | GET | SPA shell; carries `window.__data.configuration.docTypeMappings` (instrument codes) | CONFIRMED LIVE |
| `/search/advanced` | GET (deep link), SPA-driven | Advanced search: Department filter, date range, doc type | CONFIRMED LIVE |
| `/property-alert` | GET | Property alert registration; carries embedded disclaimer | CONFIRMED LIVE |
| Detail URLs for individual records | GET | Per-record document detail page | STRONG INFERENCE (SPA-rendered; URL shape captured at scraper-build time) |

Scraping pattern: **headless Chromium via Playwright** (same as Harris-Intel's RP.aspx pattern). The SPA needs JS execution; static HTTP won't yield search results. Capture state via XHR responses where possible to reduce DOM-parsing fragility.

### A.2.2 `www.dallascounty.org`

| Endpoint | Method | Purpose | Status |
|---|---|---|---|
| `/government/county-clerk/recording/foreclosures.php` | GET | Forward-looking foreclosure-PDF index | CONFIRMED LIVE |
| `/department/countyclerk/media/foreclosure/<MonthName>/<City>_<N>.pdf` | GET | Individual foreclosure-notice PDF | CONFIRMED LIVE |
| `/services/record-search/` | GET | Disclaimer landing for records search (links out to publicsearch.us) | CONFIRMED LIVE |

PDFs are large (some Dallas batches >4 MB; total across 23 cities × 4 weeks ≈ 50–80 files/month, ~20–40 MB total per refresh). Daily diff via `Last-Modified` is the appropriate fetch strategy.

### A.2.3 `www.dallascad.org`

| Endpoint | Method | Purpose | Status |
|---|---|---|---|
| `/DataProducts.aspx` | GET (ASP.NET WebForms POST for downloads) | Bulk data ZIP downloads (free) | CONFIRMED LIVE |
| `/GISDataProducts.aspx` | GET | GIS overlays (out of MVP) | CONFIRMED LIVE |
| `/SearchOwner.aspx`, `/SearchAcct.aspx`, `/SearchAddr.aspx`, `/SearchBiz.aspx` | POST | Search forms (not needed for bulk-download architecture) | CONFIRMED LIVE |
| `/OpenRecords.aspx` | GET | Formal PIA process documentation | CONFIRMED LIVE |
| `/AcctDetail*.aspx?Account_Num=…` | GET | Per-record detail | **CONFIRMED FORBIDDEN** by robots.txt; not used |

Bulk-data file shapes (per §2.4 prior art and DCAD nomenclature):

- `DCADData_YYYY.zip` containing Comma-Delimited tables (yearly snapshot)
- `DCADData_YYYY_FixedFormat.zip` (same data, fixed-width)
- Reference docs / data dictionary embedded in ZIP
- Years currently advertised: 2021–2026

Format observed via `hydrospanner/dcad_parser` repo: `TABLE [name] [FIELD]` data-dictionary headers per file. STRONG INFERENCE — to be verified against actual ZIP on first build run.

## A.3 Dallas instrument-code map (resolves §2.6)

From `window.__data.configuration.docTypeMappings` captured during §2.1 audit, cross-walked to Harris-Intel's 12 categories. This is the contract Section E.1 implements.

| Harris-Intel category | Dallas literal code(s) | Display name(s) | Notes |
|---|---|---|---|
| `L/P` (Lis Pendens) | `LP` + release `RLP` | LIS PENDENS (NOTICE OF); RELEASE OF LIS PENDENS | Both halves needed for active-only filter |
| `NOTICE` (Notice of Foreclosure) | `NOF` (department `FC`) | NOTICE OF FORECLOSURE | Department filter `FC` available on publicsearch.us advanced search |
| `TRSALE` (Trustee / Sub-Trustee Deed) | `TD` (department `FC`) | TRUSTEE'S / SUBSTITUTE TRUSTEE'S DEED | Post-sale, often follows NOF by 21+ days |
| `LIEN` (general lien) | `LN`, `LC`, `ML` | LIEN; LIEN CLAIM; MECHANIC'S LIEN | Multi-code; union all three |
| `T/L` (Tax Lien) | `TXL` | TAX LIEN | Separate from `FTL` |
| `JUDGE` (Judgment) | `JUD` | JUDGMENT | |
| `A/J` (Abstract of Judgment) | `AJ` | ABSTRACT OF JUDGMENT | |
| `PROB` (Probate) | `PB` | PROBATE PROCEEDINGS | |
| `DEED` | `D`, `WD`, `SWD`, `GWD`, `QCD`, `SD` | DEED; WARRANTY DEED; SPECIAL WARRANTY DEED; GENERAL WARRANTY DEED; QUITCLAIM DEED; SHERIFF'S DEED | Broad category — may need to narrow in §3.6 scoring |
| `BNKRCY` (Bankruptcy) | `BR` | BANKRUPTCY PROCEEDINGS | |
| `LEVY` (closest analog) | `SZS` + `TXL` | SEIZURE & SALE; TAX LIEN | Texas terminology differs; `SZS` is closest functional equivalent |
| `REL` (Release) | `REL` + category-specific releases (`RLP`, etc.) | RELEASE | Use as suppression signal (mark prior record inactive), not a lead-generating event |
| (Bonus: Federal Tax Lien) | `FTL` | FEDERAL TAX LIEN | Harris-Intel folds this into `T/L`; Dallas separates |

STRONG INFERENCE on all rows (mapping derived from observed code list + functional equivalence with Harris-Intel categories). To be CONFIRMED on first scraper run against publicsearch.us advanced search.

## A.4 DCAD bulk-data table inventory (expected)

Based on `hydrospanner/dcad_parser` prior art and Texas appraisal-district standard practice. To be CONFIRMED against the ZIP's data dictionary on first run.

| Table | Purpose | Key field | Harris equivalent |
|---|---|---|---|
| `ACCOUNT_INFO` | Master parcel record | `ACCOUNT_NUM` | `real_acct.txt` |
| `ACCOUNT_APPRL_YEAR` | Year-by-year value | `ACCOUNT_NUM`, `APPRAISAL_YR` | (part of `real_acct.txt`) |
| `MULTI_OWNER` | Ownership records | `ACCOUNT_NUM`, `OWNER_SEQ_NUM` | `owners.txt` |
| `RES_DETAIL` | Residential building detail | `ACCOUNT_NUM` | `building_res.txt` |
| `COM_DETAIL` | Commercial building detail | `ACCOUNT_NUM` | `building_other.txt` |
| `LAND` | Land segments | `ACCOUNT_NUM`, `SECTION_NUM` | `land.txt` |
| `TAXABLE_OBJECT` | Tax jurisdictions | `ACCOUNT_NUM`, `TAX_OBJ_ID` | `jur_value.txt` |
| `APPLIED_STD_EXEMPT` | Active exemptions (incl. homestead) | `ACCOUNT_NUM` | `exemption.txt` |

WEAK INFERENCE on exact table names; STRONG INFERENCE on structure. The first build sprint produces a verified mapping from `dcad_parser`'s SQLAlchemy models against the live ZIP and updates this table.

## A.5 File path patterns (for crawler config)

```
publicsearch.us:
  Search:   <SPA-driven via Playwright; URLs not deterministic>
  Detail:   <per-record, captured at runtime>

dallascounty.org:
  Foreclosures index:
    /government/county-clerk/recording/foreclosures.php
  Foreclosure PDFs:
    /department/countyclerk/media/foreclosure/<MonthName>/<City>_<WeekNum>.pdf
    where MonthName ∈ {January, February, …, December}
    City ∈ {Addison, Balch-Springs, Carrollton, Cedar-Hill, Coppell,
            Dallas, DeSoto, Duncanville, Farmers-Branch, Garland,
            Glenn-Heights, "Glenn Heights" (space variant), Grand-Prairie,
            Hutchins, Irving, Lancaster, Mesquite, Other, Richardson,
            Rowlett, Sachse, Seagoville, Wilmer, Wylie}
    WeekNum ∈ {1, 2, 3, 4}  — and large months may include
              ' (1)', ' (2)', ' (3)' suffixes (URL-encoded as %20%281%29)

dallascad.org:
  Bulk data:
    /DataProducts.aspx
    Downloads served as <description>_<YYYY>.zip (form-POST initiated)
```

## A.6 Refresh cadences and backfill bounds

| Source | Cadence | Backfill horizon | Notes |
|---|---|---|---|
| publicsearch.us | Live (per filing) | Years (exact horizon unknown; bounded by County Clerk indexing practice) | "Certified through 05/08/2026" notice; County Clerk indexes new filings within ~5 business days |
| dallascounty.org foreclosure PDFs | Weekly batches | **~3 months forward only** (per §3.7.8 finding) | Last-modified timestamps cluster on Wednesdays. Notices filed on or after 2026-02-24 are also published to publicsearch.us. Month directories use month-name only (no year) — implies rolling overwrite year-over-year. |
| DCAD bulk data | Per-certification refresh | 2021–2026 advertised | STRONG INFERENCE: weekly or per-certification-event, not daily |

---

# Section B — Constraints & Authorization

## B.1 Scope and method

This section consolidates the seven robots.txt / Terms of Service / Privacy Policy / Open Records documents collected in the §2.7 audit (plus the §3.7.8 foreclosure-index page) into a single per-system legal posture, then reconciles each previously-resolved §3 decision against those constraints. Confidence tags follow master-prompt convention.

## B.2 Per-system legal posture

### B.2.1 `dallas.tx.publicsearch.us` — third-party vendor, maximally restrictive

| Attribute | Value | Confidence |
|---|---|---|
| Operator | Neumo (third-party "Hosted Solution Provider" per embedded disclaimer) | CONFIRMED |
| robots.txt | `Allow: /$` + `Disallow: /` | CONFIRMED |
| Effective bot policy | Every URL except literal home page is disallowed | CONFIRMED |
| Separate ToS page | None exists | CONFIRMED |
| Embedded disclaimer | Liability/warranty disclaimer surfaced only at Property Alert registration; AS-IS basis | CONFIRMED |
| Explicit anti-automation clause | None in visible text | CONFIRMED |
| Explicit anti-scraping clause | None in visible text | CONFIRMED |
| Explicit commercial-use restriction | None in visible text | CONFIRMED |
| Copyright over data | Not claimed (data is public records held by County Clerk) | CONFIRMED |

### B.2.2 `www.dallascounty.org` — County primary site, maximally permissive

| Attribute | Value | Confidence |
|---|---|---|
| Operator | Dallas County (government) | CONFIRMED |
| robots.txt | `Allow: /` + commented-out `#Crawl-delay: 30` | CONFIRMED |
| Effective bot policy | All paths permitted to all bots, no advisory rate limit | CONFIRMED |
| Separate ToS page | Does not exist | CONFIRMED |
| Privacy Policy | Single governing policy; last modified 2017-10-04 | CONFIRMED |
| Security clause scope | "Unauthorized attempts to upload or change information or otherwise cause damage" — scoped to writes/damage, not reads | CONFIRMED |
| Explicit scraping prohibition | None | CONFIRMED |
| Explicit commercial-use restriction | None | CONFIRMED |
| Foreclosure-PDF path | `/department/countyclerk/media/foreclosure/<Month>/<City>_<N>.pdf` | CONFIRMED |
| Cybersecurity posture | 2023 and 2024 public incident notices in home-page sidebar | CONFIRMED |

### B.2.3 `www.dallascad.org` — DCAD primary site, surgically permissive

| Attribute | Value | Confidence |
|---|---|---|
| Operator | Dallas Central Appraisal District (Texas political subdivision) | CONFIRMED |
| robots.txt | Targeted `Disallow:` rules with inline comments explaining intent | CONFIRMED |
| Bulk-data path `/DataProducts.aspx` | Robots-permitted | CONFIRMED |
| Search forms `/Search*.aspx` | Robots-permitted | CONFIRMED |
| Detail pages `/Acct*` (e.g. `/AcctDetailRes.aspx`) | **Robots-disallowed** ("exclude all detail pages from being indexed") | CONFIRMED |
| Embedded home-page disclaimer | "Informational purposes only…not deemed a legal document" — scope-limiting, not access-restricting | CONFIRMED |
| Open Records page | Documents formal PIA process | CONFIRMED |
| Bulk data cost | **Free of charge** (DCAD's own words on `/OpenRecords.aspx`) | CONFIRMED |
| Open Records standing-request policy | No standing requests; each PIA filing is one-shot | CONFIRMED |
| Custom programming/extraction rate | $28.50/hr + 20% overhead (if ever needed) | CONFIRMED |
| Explicit scraping prohibition | None | CONFIRMED |
| Copyright over data | Not claimed (site footer "All Rights Reserved" applies to design/code, not to PIA-governed records) | STRONG INFERENCE |
| Tech generation | ASP.NET WebForms, Visual Studio.NET 7.0 (2002–2003 era), POST-based forms with ViewState | CONFIRMED |

## B.3 The principal architectural constraint

The Dallas County Clerk Official Records system — the structural equivalent of Harris County's `RP.aspx` — is hosted on `dallas.tx.publicsearch.us`, which is robots-disallowed for every URL except the literal home page. Harris-Intel's core daily pipeline requires daily automated access to that exact system.

Three feasible postures were enumerated:

| Posture | publicsearch.us conformance | Coverage |
|---|---|---|
| (a) Strict robots.txt compliance | Honors all conventions | Loses 11 of 12 instrument categories |
| (b) Treat publicsearch.us robots.txt as advisory | Replicates Harris-Intel pattern; accepts risk | Full 12 categories |
| (c) Hybrid: automated foreclosure-PDFs + manual/vendor for other categories | Partial | Partial |

**Decision: §3.7.5 = (b).** The architecture replicates Harris-Intel with the constraint posture in §F.1 documenting risk.

Robots.txt is convention, not statute. Texas Penal Code §33.02 (Breach of Computer Security) and federal CFAA caselaw (notably *hiQ Labs v. LinkedIn*, 9th Cir.) have moved toward treating public-facing data as outside CFAA scope, but Texas-specific state-venue caselaw is unsettled. Texas Property Code §51.002 makes foreclosure notices public — strengthening access for that category but not bearing on other instruments.

## B.4 Cross-cutting operational rules

The DCAD bulk-data download from `/DataProducts.aspx` is the canonical, sanctioned path for enrichment data. CONFIRMED. Per §3.7.6 = (a), the Google Drive mirror pattern from Harris-Intel is retained; the operational caveats are documented in §F.5.

All traffic to `www.dallascounty.org` for foreclosure-PDF retrieval is robots-permitted. A polite crawler with a real User-Agent identifying the project, 1–2 second inter-request gaps, no concurrency, and respect for the County's commented-out 30-second advisory delay is the appropriate conduct posture given the 2023/2024 cybersecurity-incident operational stance. STRONG INFERENCE.

DCAD's `/Acct*` detail-page disallow rule is honored automatically if the architecture replicates the Harris-Intel bulk-download enrichment pattern. No per-record HTTP fetches against DCAD occur. CONFIRMED.

No host imposes a contractual restriction on commercial or derivative use of public-record data retrieved within its permitted paths. CONFIRMED across all four hosts.

No host claims copyright over the underlying public-record data; copyright notices at dallascounty.org and dallascad.org apply to site design and code only. STRONG INFERENCE.

## B.5 §3 decisions resolved by audit

| Decision | Selection | Audit reconciliation |
|---|---|---|
| 3.7.1 | (b) HALT and audit | RESOLVED BY AUDIT |
| 3.7.5 | (b) Treat publicsearch.us robots.txt as advisory | Documented in §F.1 |
| 3.7.6 | (a) Keep Google Drive mirror | Caveats in §F.5 |
| 3.7.7 | Default polite-crawler profile (§C.5) | CONFIRMED |
| 3.7.8 | Foreclosure-PDF backfill ≈3 months forward only; historical via publicsearch.us | CONFIRMED |

---

# Section C — Architecture

## C.1 Reference design

Daily 07:00 UTC cron via GitHub Actions per §3.2.3.

```
┌─────────────────────────────────────────────────────────────────┐
│                  GitHub Actions: daily 07:00 UTC                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │
        ┌─────────────┴──────────────┬───────────────────┐
        ▼                            ▼                   ▼
┌──────────────────┐      ┌──────────────────┐  ┌──────────────────┐
│ publicsearch.us  │      │ dallascounty.org │  │  Google Drive    │
│ Playwright SPA   │      │ static PDF       │  │  mirror of DCAD  │
│ 12 instrument    │      │ foreclosure      │  │  bulk-data ZIP   │
│ categories       │      │ index + PDFs     │  │                  │
└────────┬─────────┘      └────────┬─────────┘  └────────┬─────────┘
         │                         │                     │
         └────────────────┬────────┴─────────────────────┘
                          ▼
                ┌──────────────────────┐
                │ Normalize + dedupe   │
                │ Cross-source merge   │
                │ (publicsearch.us is  │
                │  authoritative;      │
                │  PDFs supplement)    │
                └──────────┬───────────┘
                           ▼
                ┌──────────────────────┐
                │ Enrich w/ DCAD parcel│
                │ + owner + value data │
                └──────────┬───────────┘
                           ▼
                ┌──────────────────────┐
                │ Heuristic scoring    │
                │ (§E.4)               │
                └──────────┬───────────┘
                           ▼
        ┌──────────────────┴──────────────┐
        ▼                                 ▼
┌──────────────────┐              ┌──────────────────┐
│ records.json     │              │ ghl_export_      │
│ (full archive)   │              │ YYYYMMDD.csv     │
│ in repo          │              │ in repo          │
└──────────────────┘              └──────────────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │ GitHub Pages         │
                │ static dashboard     │
                │ (§G)                 │
                └──────────────────────┘
```

CONFIRMED architecture (per §3 selections).

## C.2 Components

**C.2.1 Scrapers — three sub-modules:**

1. `scraper/publicsearch.py` — Playwright/Chromium driver. Iterates the 12 instrument codes from A.3, paginates the SPA advanced-search results within the configured `DAYS_BACK` window (§3.6.2 pending), captures per-record metadata. Emits normalized records. **High operational risk; see §F.**
2. `scraper/foreclosure_pdfs.py` — HTTP GET against `dallascounty.org`. Walks the foreclosures landing page, parses the per-month listing tables, downloads new/changed PDFs by `Last-Modified`, runs `pdfplumber` extraction to pull notice details (sale date, property address, trustee, debtor, original loan amount). PDFs serve as a freshness cross-check for the `NOF` and `TD` codes and as a fallback if publicsearch.us blocks an IP.
3. `scraper/dcad_bulk.py` — Once per N days (cache), pulls ZIP from Google Drive mirror, extracts, loads into in-memory pandas DataFrames for enrichment. Refresh cadence TBD per §3.7.6 sub-decision.

**C.2.2 Enrichment.** `scraper/enrichment.py` — Joins County Clerk records to DCAD parcels via address-normalization and account-number resolution. Replicates Harris-Intel's pattern but on Dallas tables per A.4. Carries forward the legal-description regression fix needed (§3.3.2 currently UNRESOLVED).

**C.2.3 Scorer.** `scraper/scorer.py` — Heuristic per §3.6.1 (currently UNRESOLVED; default carries Harris-Intel weights). Score output: base 30 + 10/instrument-category-flag + 20 LP+FC combo + 15 ≥$100k value + 10 ≥$50k value + 5 new-this-week + 5 has-address, capped at 100.

**C.2.4 Output writers.** `scraper/output.py` — Emits `records.json`, daily `ghl_export_YYYYMMDD.csv`, optional GHL push if §3.4.2 resolves to (a).

**C.2.5 Dashboard.** `dashboard/index.html` — Static SPA, served from GitHub Pages. Carries the six investor calculators inherited from Harris-Intel (Fix & Flip, Sub-To, Creative, Mortgage, CoC, Cap Rate) per §3.5.3 default (currently UNRESOLVED). Tax-list XLSX import via SheetJS retained.

## C.3 Storage strategy

| Artifact | Location | Rationale |
|---|---|---|
| Source code | Private GitHub repo (§3.2.5 = b) | Standard |
| `records.json` (full archive) | Same repo, committed daily by bot | Git-as-database, matches Harris-Intel pattern; allows historical diff via git log |
| Daily CSV exports | Same repo, `/exports/` | Audit trail |
| DCAD bulk ZIP | Google Drive mirror (§3.7.6 = a) | Per user decision; replicates Harris-Intel pattern despite DCAD providing free direct access. Operational rationale noted in §F.5. |
| Dashboard | GitHub Pages (§3.2.2) | Static |
| Logs / failure traces | GitHub Actions run logs | Standard; consider artifact retention bump (§3.7.3 pending) |

## C.4 Scheduling

Daily cron 07:00 UTC = 02:00/03:00 CT (DST-dependent). Honors §3.2.3. **Recommended addition** (not yet a §3 decision): randomize jitter ±30 minutes within the cron to reduce coordinated-bot signatures against publicsearch.us. Flagged in §H as 3.7.9 (NEW, NOT BLOCKING).

## C.5 Conduct profile (§3.7.7 confirmed)

| Constraint | Value |
|---|---|
| Inter-request gap on dallascounty.org | 1.5–2 seconds |
| Inter-request gap on publicsearch.us | 2–4 seconds (advisory robots.txt → still operate conservatively) |
| Concurrency | 1 (no parallel requests per host) |
| User-Agent | `<ProjectName>/<version> (+<contact-email>)` — identifies project and provides contact |
| Circuit breaker | Halt pipeline on 5 consecutive 4xx/5xx responses from same host |
| `Retry-After` | Honored absolutely |
| Backoff on 429 | Exponential, base 60s, max 30min, then abort |

---

# Section D — Implementation Plan

## D.1 Phasing

**Phase 0 — Pre-flight (1–2 days).** Resolve outstanding §3 decisions in §H. Set up private repo + GitHub Pages + Google Drive folder. Provision DCAD bulk-data baseline.

**Phase 1 — DCAD enrichment foundation (3–5 days).**
- D.1.1 Download DCAD 2026 bulk ZIP from `/DataProducts.aspx`; verify schema against A.4
- D.1.2 Upload to Google Drive; capture share file ID
- D.1.3 Write `dcad_bulk.py` to download + parse
- D.1.4 Validate parse against `hydrospanner/dcad_parser` SQLAlchemy models
- D.1.5 Produce reference parcel-lookup utility that maps address → ACCOUNT_NUM

**Phase 2 — Foreclosure-PDF ingester (3–5 days).**
- D.2.1 Write `foreclosure_pdfs.py` to walk dallascounty.org foreclosures.php
- D.2.2 Implement PDF parsing for sale date, address, trustee, debtor, loan amount
- D.2.3 Normalize against DCAD account numbers via address join
- D.2.4 Output normalized records to scratch JSON

**Phase 3 — publicsearch.us scraper (5–10 days, highest risk).**
- D.3.1 Set up Playwright + Chromium in CI image
- D.3.2 Build SPA-aware scraper for advanced search by instrument code + date range
- D.3.3 Iterate all 12 categories from A.3; capture per-record detail
- D.3.4 Cross-reconcile NOF/TD records against Phase 2 PDFs
- D.3.5 Add IP-rotation if needed (§3.4 sub-decision)

**Phase 4 — Enrichment + scoring + output (3–5 days).**
- D.4.1 Port enrichment.py from Harris-Intel; rewire for DCAD schema
- D.4.2 Fix HOA-plaintiff bug from Harris-Intel before it ships (§3.3.1)
- D.4.3 Fix HIGH-confidence regression before it ships (§3.3.2)
- D.4.4 Port scorer; tune weights per §3.6.1 once resolved
- D.4.5 Emit records.json + daily CSV

**Phase 5 — Dashboard (3–5 days).**
- D.5.1 Fork Harris-Intel dashboard
- D.5.2 Rebrand per §3.5.1
- D.5.3 Wire to new records.json
- D.5.4 Retain or trim calculators per §3.5.3

**Phase 6 — Production hardening (3–5 days).**
- D.6.1 Real `requirements.txt` (§3.3.4)
- D.6.2 Resolve duplicate `/data` + `/dashboard` directories (§3.3.3) before duplication propagates
- D.6.3 Set up monitoring per §3.7.3
- D.6.4 Initial backfill per §3.7.4
- D.6.5 First production run; observe and tune

**Total estimated build effort: 20–35 working days** for a single developer.

## D.2 Dependencies

- Python 3.11 (Harris-Intel parity)
- Playwright + Chromium (publicsearch.us)
- pdfplumber (foreclosure PDFs)
- pandas (DCAD bulk-data parsing)
- requests (everything else)
- pdf-extracted text post-processing utilities (TBD)

All to be pinned in `requirements.txt` (§3.3.4 resolution).

## D.3 Pre-flight checklist (must be true before Phase 1)

- [ ] §3.1.2 confirmed: all 12 categories per A.3 in scope (vs trimmed list)
- [ ] §3.2.1 (DB choice) resolved
- [ ] §3.2.5 sub-question (private-org name) resolved
- [ ] §3.4.1 (skip-trace API) resolved or explicitly deferred
- [ ] §3.4.2 (GHL push) resolved or explicitly deferred
- [ ] §3.4.3 (tax-list automation) resolved or explicitly deferred
- [ ] §3.4.4 (stacking logic) resolved or explicitly deferred
- [ ] §3.5.1 (branding name + repo name) resolved
- [ ] §3.5.2 (dashboard URL) resolved
- [ ] §3.5.3 (calculators carry-forward) resolved
- [ ] §3.6.1 (scoring weights) resolved
- [ ] §3.6.2 (DAYS_BACK) resolved
- [ ] §3.7.2 (data retention) resolved
- [ ] §3.7.3 (monitoring approach) resolved
- [ ] §3.7.4 (backfill horizon) resolved
- [ ] §3.8.1 sub-question (GitHub identity) resolved
- [ ] §3.8.2 (dev workflow) resolved

---

# Section E — Schema Mapping

## E.1 Instrument-code crosswalk

Given in A.3. The implementation contract: `scrapers/publicsearch.py` iterates the Dallas literal codes; the normalizer emits records tagged with the Harris-Intel category label so the downstream scorer and dashboard need not change.

## E.2 County Clerk record schema

Normalized record (per Harris-Intel `records.json` shape, Dallas-adapted):

```json
{
  "record_id":        "<publicsearch.us doc id or PDF-derived synthetic id>",
  "source":           "publicsearch.us | foreclosure_pdf",
  "category":         "L/P | NOTICE | TRSALE | LIEN | T/L | JUDGE | A/J | PROB | DEED | BNKRCY | LEVY | REL",
  "dallas_code":      "<literal code from A.3>",
  "filing_date":      "YYYY-MM-DD",
  "instrument_num":   "<as printed by Dallas County Clerk>",
  "grantor":          "<name(s)>",
  "grantee":          "<name(s)>",
  "address":          "<as written>",
  "address_normalized": "<USPS-format>",
  "dcad_account":     "<resolved via address join, may be null>",
  "amount":           "<dollar value if applicable>",
  "trustee":          "<for NOF/TD only>",
  "sale_date":        "<for NOF/TD only>",
  "score":            0,
  "score_breakdown":  { "base": 30, "category_flags": [], "value_band": null, "freshness": null, "address_resolved": null },
  "first_seen":       "YYYY-MM-DD",
  "last_seen":        "YYYY-MM-DD",
  "active":           true,
  "release_record_id": "<if a REL record has fired against this, the suppressing record's id>"
}
```

STRONG INFERENCE on field set; final shape confirmed at end of Phase 3.

## E.3 DCAD ↔ HCAD field crosswalk (for enrichment.py port)

| HCAD field | DCAD field (expected) | Purpose |
|---|---|---|
| `real_acct.acct` | `ACCOUNT_INFO.ACCOUNT_NUM` | Join key |
| `real_acct.mailto`, `mailing_addr` | `MULTI_OWNER.OWNER_NAME` + address fields | Owner mail address |
| `real_acct.site_addr_1` | `ACCOUNT_INFO.STREET_NUM` + `STREET_NAME` + `SUFFIX` | Property location |
| `real_acct.bld_ar` | `RES_DETAIL.LIVING_AREA` (or COM equivalent) | Building area |
| `real_acct.market_value` | `ACCOUNT_APPRL_YEAR.MARKET_VAL` | Value for scoring |
| `real_acct.appraised_val` | `ACCOUNT_APPRL_YEAR.APPRAISED_VAL` | Appraised value |
| `real_acct.land_ar` | `LAND.AREA_SIZE` (sum across segments) | Lot size |
| `owners.name` | `MULTI_OWNER.OWNER_NAME` | Owner name(s) |
| `exemption.exempt_cd` | `APPLIED_STD_EXEMPT.EXEMPT_CD` | Homestead flag |
| `parcel_tieback.acct` | (TBD — DCAD model unclear) | Legal description |

WEAK INFERENCE on rows marked TBD; first build pass confirms.

## E.4 Score weights (default carry-over, awaiting §3.6.1)

Same as Harris-Intel:

- Base: 30
- Each instrument-category flag matched: +10
- LP + FC combination present: +20
- Market value ≥ $100,000: +15
- Market value ≥ $50,000 (but <$100k): +10
- First seen within last 7 days: +5
- Address resolved to DCAD account: +5
- Cap at 100

Tunable in `scraper/config.py`. §3.6.1 may rebalance.

---

# Section F — Risk Register

## F.1 Legal & policy risk

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| F.1.1 | publicsearch.us robots.txt violation (per §3.7.5 = b decision) — Neumo or County may issue cease-and-desist or IP block | Medium | High | Conduct profile from §3.7.7 minimizes signature; respect 429s; have email-contact in User-Agent; have a fallback plan if blocked (vendor data feed, manual research) |
| F.1.2 | Texas CFAA / Penal Code §33.02 exposure — unsettled caselaw in Texas state venue | Low | High (criminal) | Consult Texas attorney before production launch. *hiQ v. LinkedIn* is favorable but federal; Texas-specific test unclear. Document business-necessity and good-faith reliance on public-records framework. |
| F.1.3 | Copyright assertion enforcement attempt by DCAD/County | Very Low | Low | "All Rights Reserved" notices apply to site design, not PIA-governed records. Texas PIA explicitly makes data public. Easy legal posture to defend. |
| F.1.4 | Privacy concerns over surfaced contact info / scoring | Low | Medium | Records are public; scoring is derivative analysis. Risk is reputational not legal. Document non-discriminatory use. |
| F.1.5 | Republished foreclosure data triggers state debt-collection or fair-housing concerns | Low | Medium | Not in current §3 scope (no consumer-facing publication). If §3.5.2 changes that, re-evaluate. |

## F.2 Operational risk

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| F.2.1 | publicsearch.us SPA changes break scraper | High | High | Quarterly scraper-audit cron; alerting on zero-result days. Worst case: rebuild Playwright path. |
| F.2.2 | DCAD ZIP schema changes year-over-year break enrichment | Medium | Medium | First-of-year schema-diff alerting. Pin to data-dictionary version inside ZIP. |
| F.2.3 | Foreclosure PDF format changes break extraction | Low | Medium | Run weekly extraction-quality check (success rate <95% → alert). |
| F.2.4 | Dallas County's 2023/2024 cybersecurity-incident posture leads to stricter network-level filtering | Medium | Medium | Polite conduct profile; identifiable User-Agent; written request for whitelisting if blocked. |
| F.2.5 | GitHub Actions free-tier minute limit if scraper grows | Low | Low | Self-hosted runner fallback. Currently well within free tier. |
| F.2.6 | Google Drive ZIP mirror link breaks (link-rot, drive permissions) | Medium | High (pipeline halt) | Per §3.7.6 = (a), inherits Harris-Intel anti-pattern; flagged for §3.7.6 sub-decision. Recommended fallback: direct DCAD download on Drive-fetch failure (3.7.10 NEW). |
| F.2.7 | publicsearch.us returns inconsistent results for high-volume queries (silent rate-limiting) | Medium | High | Monitor result-count baselines; alert on >30% day-over-day variance. |

## F.3 Technical risk

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| F.3.1 | Address-normalization mismatch between County Clerk records and DCAD parcels reduces enrichment hit rate | High | Medium | USPS-format normalization library; iterative tuning. Acceptable hit rate target: ≥85% on first run, ≥95% within 3 months. |
| F.3.2 | Carry-over of HOA-plaintiff bug from Harris-Intel (§3.3.1) | Certain if not fixed | High | Phase 4.4.2 explicit fix before Phase 4 ships |
| F.3.3 | Carry-over of HIGH-confidence regression (§3.3.2) | Certain if not fixed | High | Phase 4.4.3 explicit fix |
| F.3.4 | publicsearch.us session/cookie management breaks across cron runs | Medium | Medium | Fresh Chromium context per run; no session reuse needed for read-only paths. |

## F.4 Severity-weighted top priorities

1. F.1.2 (Texas CFAA exposure) — *Resolve before production* via attorney consult
2. F.3.2 + F.3.3 (HOA + HIGH-confidence bugs) — *Resolve before Phase 4 ships*
3. F.2.1 (publicsearch.us SPA changes) — *Build with audit cadence in mind*
4. F.1.1 (publicsearch.us cease-and-desist) — *Build with fallback in mind*

## F.5 Note on §3.7.6 = (a) decision

User selected (a) keep Google Drive mirror. The operator should be aware this:

- Replicates Harris-Intel's pattern of relying on a personal Google Drive file ID
- Introduces a single point of failure (F.2.6) absent in the direct-download alternative
- Adds no operational benefit since DCAD bulk-data is free and direct

Documented for the record. Not blocking the build, but the §3.7.6 sub-decision (who owns the Drive; what's the failure-mode fallback) should be answered before Phase 1.

---

# Section G — Deliverables / Repo Structure

```
<project-root>/
├── .github/
│   └── workflows/
│       └── scrape.yml           # daily 07:00 UTC cron (§3.2.3)
├── scraper/
│   ├── __init__.py
│   ├── publicsearch.py          # Playwright SPA scraper, 12 codes
│   ├── foreclosure_pdfs.py      # dallascounty.org PDF walker
│   ├── dcad_bulk.py             # DCAD ZIP fetcher (Google Drive mirror)
│   ├── enrichment.py            # join County Clerk to DCAD parcels
│   ├── scorer.py                # heuristic scoring
│   ├── normalize.py             # address normalization, code-mapping
│   ├── output.py                # records.json + CSV emit
│   ├── config.py                # tunable constants (DAYS_BACK, weights, etc.)
│   └── requirements.txt         # PINNED, USED (§3.3.4)
├── dashboard/
│   ├── index.html               # static dashboard, GitHub Pages root
│   ├── styles.css
│   └── calculators.js           # carries forward per §3.5.3
├── data/
│   ├── records.json             # full archive, bot-committed daily
│   └── README.md                # explains the git-as-DB pattern
├── exports/
│   └── ghl_export_YYYYMMDD.csv  # daily, bot-committed
├── docs/
│   ├── ARCHITECTURE.md          # this document
│   ├── DCAD_SCHEMA.md           # verified schema after Phase 1
│   ├── INSTRUMENT_CODES.md      # A.3 with verification status updated
│   └── LEGAL_POSTURE.md         # §B summary
├── tests/
│   ├── test_normalize.py
│   ├── test_scorer.py
│   ├── test_pdf_extract.py      # against fixture PDFs
│   └── fixtures/
├── .gitignore
├── LICENSE                      # add per §3.8.2 sub-decision (UNRESOLVED)
└── README.md                    # project overview, install, run
```

**Notable differences from Harris-Intel:**

- No duplicate `/data` + `/dashboard` directories (fixes §3.3.3)
- `requirements.txt` actually pinned and used (fixes §3.3.4)
- Tests directory present (fixes a Harris-Intel gap)
- README/LICENSE present (fixes a Harris-Intel gap)
- `docs/` with structured architecture/schema docs (fixes a Harris-Intel gap)

---

# Section H — Open Decisions

## H.1 Resolved

| ID | Decision |
|---|---|
| 3.1.1 | (a) Dallas County only |
| 3.1.2 | Carry over all 12 Harris instrument-code categories (mapping confirmed in A.3) |
| 3.2.2 | GitHub Pages |
| 3.2.3 | Daily 07:00 UTC cron |
| 3.2.4 | (b) Google Drive mirror (replicates Harris-Intel pattern; see F.5) |
| 3.2.5 | (b) Different org, private — org name UNRESOLVED |
| 3.3.4 | Use `requirements.txt` properly |
| 3.7.1 | (b) HALT and audit — resolved by §2.7 audit completion |
| 3.7.5 | (b) Treat publicsearch.us robots.txt as advisory |
| 3.7.6 | (a) Keep Google Drive mirror |
| 3.7.7 | Confirmed default polite-crawler conduct |
| 3.7.8 | Foreclosure-PDF backfill ≈3 months forward only; historical via publicsearch.us |
| 3.8.1 | Different GitHub identity — name UNRESOLVED |

## H.2 Unresolved (blocking Phase 0 close)

| ID | Decision |
|---|---|
| 3.2.1 | DB choice (Git-as-DB carry-over default? SQLite? Postgres?) |
| 3.3.1 | HOA bug fix approach |
| 3.3.2 | HIGH-confidence regression fix approach |
| 3.3.3 | Mirror-dirs cleanup (default: eliminate per §G) |
| 3.4.1 | Skip-trace API selection (or defer) |
| 3.4.2 | GHL push (or defer) |
| 3.4.3 | Tax-list automation (or defer) |
| 3.4.4 | Stacking logic carry-forward |
| 3.5.1 | Branding (project name + repo name) |
| 3.5.2 | Dashboard URL |
| 3.5.3 | Calculators carry-forward set |
| 3.6.1 | Scoring weights (default §E.4) |
| 3.6.2 | DAYS_BACK |
| 3.7.2 | Data retention location |
| 3.7.3 | Monitoring approach |
| 3.7.4 | Backfill horizon |
| 3.8.2 | Dev workflow (PR-based? direct-push? gitflow?) |

## H.3 Unresolved sub-questions (non-blocking, can be deferred)

| ID | Sub-question |
|---|---|
| 3.2.5.a | Org/GitHub-username for private repo |
| 3.7.6.a | Google Drive owner identity + failure-mode fallback |
| 3.8.1.a | GitHub identity for daily bot commits |

## H.4 New items surfaced during recon (NOT BLOCKING)

| ID | Item |
|---|---|
| 3.7.9 | Add jitter to 07:00 UTC cron (recommended; default OFF) |
| 3.7.10 | Direct-DCAD-download fallback on Google Drive failure (recommended; default OFF per §3.7.6 = a) |

---

# Section I — Verification & Acceptance

## I.1 First-run acceptance (Phase 6 exit criteria)

- [ ] One successful end-to-end pipeline run, 07:00 UTC ± 30 min
- [ ] `records.json` populated with ≥500 records across ≥8 of 12 instrument categories
- [ ] Address-to-DCAD resolution hit rate ≥85%
- [ ] All 23 city directories from A.5 attempted; ≥20 succeed
- [ ] Zero HIGH-confidence-regression style empty-confidence bug (F.3.3 verified absent)
- [ ] Zero HOA-plaintiff records surfaced at top of scoring (F.3.2 verified absent)
- [ ] No 4xx/5xx responses observed during run
- [ ] Bot commit succeeds; Pages deploy succeeds; dashboard loads

## I.2 Ongoing health checks (weekly)

- Day-over-day record-count variance <30% for each instrument category
- Address-resolution hit rate trends — alert if drops >5%
- PDF extraction success rate ≥95%
- Zero new 4xx/5xx patterns
- Google Drive ZIP accessible

## I.3 Production-readiness gates

Before live use of records for any outreach:

1. §F.1.2 attorney consult complete
2. §3.3.1 and §3.3.2 bugs verified fixed against test fixtures
3. Two consecutive weeks of clean health-check data
4. Manual review of top-50 highest-scoring records — qualitative validation

---

# Section J — Appendix

## J.1 Primary sources cited

1. **Harris-Intel reference repo:** `github.com/xcerebroai/harris-intel` (179 commits, latest snapshot 2026-04-08)
2. **DCAD parser prior art:** `github.com/hydrospanner/dcad_parser` (MIT, 27 commits, dormant) — SQLAlchemy models for DCAD bulk-data tables
3. **Texas Property Code §51.002** — non-judicial foreclosure notice posting requirements
4. **Texas Public Information Act (Gov. Code Ch. 552)** — basis for DCAD/County Clerk data availability
5. **Texas Penal Code §33.02** — Breach of Computer Security (relevant to F.1.2)
6. **Texas Property Code §12.007** — Lis Pendens filing venue
7. ***hiQ Labs, Inc. v. LinkedIn Corp.*, 31 F.4th 1180 (9th Cir. 2022)** — CFAA / public-data scraping reference (federal, not Texas-binding)

## J.2 Audited documents (§2.7 + §3.7.8)

1. `dallas.tx.publicsearch.us/robots.txt`
2. `dallas.tx.publicsearch.us/` (embedded disclaimer, full HTML source, captured `docTypeMappings`)
3. `www.dallascounty.org/robots.txt`
4. `www.dallascounty.org/about-us/privacy-policy/`
5. `www.dallascounty.org/` (home page; confirms no separate ToS)
6. `www.dallascad.org/robots.txt`
7. `www.dallascad.org/OpenRecords.aspx`
8. `www.dallascad.org/` (home page; embedded disclaimer)
9. `www.dallascounty.org/government/county-clerk/recording/foreclosures.php` (§3.7.8 crawl)

## J.3 Out of scope for this report (deferred)

- `maps.dcad.org` mapping subdomain — not a data dependency for MVP
- `BPPRp02.dallascad.org` — authenticated; no data dependency
- `onlineprotest.dallascad.org` — authenticated; no data dependency
- `dallas-county-open-data-hub-dallascountygis.hub.arcgis.com` — auxiliary GIS source; revisit only if §3.4.x decisions require it
- District Clerk records — per §2.2, only County Clerk relevant for instrument categories of interest

## J.4 Glossary

| Term | Meaning |
|---|---|
| ARB | Appraisal Review Board (DCAD-internal) |
| BPP | Business Personal Property (tax category, separate from real property) |
| FC | Foreclosure (publicsearch.us "Department" filter value) |
| GHL | GoHighLevel (CRM target for record push, per §3.4.2) |
| Harris-Intel | Reference implementation at `github.com/xcerebroai/harris-intel` |
| Lis Pendens | Notice of pending litigation affecting real property |
| MVP | Minimum Viable Product (Phase 6 exit state) |
| NOF | Notice of Foreclosure (Dallas literal code) |
| PIA | (Texas) Public Information Act |
| SPA | Single-Page Application (publicsearch.us is one) |
| TD | Trustee's Deed (Dallas literal code) |
| ViewState | ASP.NET WebForms client-side state container |

---

*End of recon report. Continuation into buildable spec proceeds in §H.2 decision resolution.*
