# Legal & Operational Posture

Distilled from `ARCHITECTURE.md` §B (ToS / robots.txt audit) and the
§3.7 decision ledger. For full audit excerpts and verbatim source language,
read the full Section B in `ARCHITECTURE.md` — this document is a
quick-reference summary, not a substitute.

## Summary

dallas-intel reads three sources, each with a distinct legal posture:

| Source | Posture | Risk |
|---|---|---|
| `dallas.tx.publicsearch.us` | robots.txt restrictive; treated as **advisory** | Medium |
| `www.dallascounty.org` | robots.txt **permissive**; sanctioned bulk access | Low |
| `www.dallascad.org` | robots.txt selectively permits `/DataProducts.aspx` | Low |

All three serve public records under Texas Government Code Ch. 552
(Public Information Act). The dispute is not about the records themselves
(unambiguously public) but about machine-readable access patterns.

## Per-source detail

### `dallas.tx.publicsearch.us` (Neumo Inc. SPA)

- **Operator:** Third-party vendor (Neumo Inc.) hosting Dallas County's
  Official Public Records search interface.
- **robots.txt:** Maximally restrictive — `Disallow: /` for all user-agents.
- **ToS:** Generic vendor terms; no explicit reference to scraping or
  automated access.
- **Decision (§3.7.5 = (b)):** robots.txt treated as **advisory** rather
  than dispositive, on the rationale that:
  1. The records are public under Texas law
  2. The County is the data owner; the vendor is a hosting layer
  3. The County's own portal (dallascounty.org) is robots-permissive
- **Risk acknowledged (§F.1.1):** This is the most legally-exposed
  decision in the project. A vendor cease-and-desist would force a
  pivot to bulk export from the County's primary system.

### `www.dallascounty.org` (Dallas County government)

- **Operator:** Dallas County government (primary).
- **robots.txt:** `Allow: /`. No specific scraping prohibitions.
- **ToS:** No separate scraping-restrictive ToS observed.
- **Privacy policy:** General government privacy policy dated 2017.
- **Decision:** Treat as low-risk; standard polite-crawler conduct applies.

### `www.dallascad.org` (Dallas Central Appraisal District)

- **Operator:** DCAD (separate political subdivision).
- **robots.txt:** Surgical — blocks `/Acct*` detail pages but **explicitly
  permits** `/DataProducts.aspx`.
- **Bulk data:** Free per `OpenRecords.aspx`; the page explicitly
  invites bulk download.
- **Decision:** Bulk download via `/DataProducts.aspx` (§3.7.6 = (a)).
  No per-account scraping; bulk-only access pattern.

## Polite-crawler conduct profile (§3.7.7)

Applied to all three sources regardless of robots posture, with stricter
limits on the most legally-exposed (publicsearch.us):

| Host | Inter-request gap | Concurrency |
|---|---|---|
| `publicsearch.us` | 2–4 s random | 1 |
| `dallascounty.org` | 1.5–2 s random | 1 |
| `dallascad.org` | 2–3 s random | 1 |

Plus:
- **User-Agent:** `dallas-intel/0.1 (+<CONTACT_EMAIL>)` — identifies the
  crawler with a working contact address so the operator can reach us if
  there's a concern, rather than IP-blocking blind.
- **Circuit breaker:** Halts the run after 5 consecutive 4xx/5xx responses
  (§3.7.7, `scraper.publicsearch.CircuitBreaker`).
- **Cron jitter:** ±30 min random sleep at the start of each run
  (§3.7.9, `scraper.main._jitter_sleep`).
- **Conditional GET:** Foreclosure PDFs use `If-Modified-Since` to avoid
  re-downloading unchanged files.
- **Bulk preference:** DCAD bulk ZIP cached weekly (§3.7.6); never
  per-account scraping.

## Data we collect

Only records that are public under Texas Government Code Ch. 552:

- County Clerk Official Public Records (filings, instruments, parties)
- Foreclosure notices (posted publicly by trustees)
- DCAD parcel data (owner names, addresses, market values, exemptions)

We do **not** collect:
- Personally-identifying information beyond what is in the public record
- Records from sealed proceedings (probate sealed minor records, etc.)
- Records explicitly marked confidential under Texas law
- Skip-trace augmentation data (deferred per §3.4.1)

## Data we publish

Records published to the dashboard and JSON archive are derivative
analyses of public records. The scoring, stacking, and HOA-filtering
logic is our own commentary; the underlying records remain public.

**Repository visibility:** Private (§3.7.2 = (a)). The dashboard
deploys to GitHub Pages only after explicit per-deploy authorization
(§3.7.3 = (c)). No automatic public mirroring.

## Operational guardrails

1. **No skip-trace API integration** in current scope (§3.4.1 deferred).
   We do not enrich records with phone numbers, email addresses, or
   contact information from external skip-trace providers in this build.
2. **GHL push deferred** (§3.4.2). The daily CSV export is the manual
   import path; no automated CRM push.
3. **Tax-list automation deferred** (§3.4.3). The dashboard supports
   manual XLSX import for cross-reference only.
4. **Suppression honored.** REL/RLP records mark prior records inactive
   (§E.1, `scraper.scorer.suppress_released`). Inactive records are
   excluded from the daily CSV export but retained in records.json for
   historical context.
5. **HOA filter applied.** Records where the grantor is an HOA pattern
   *and* there is no individual grantee are dropped (§3.3.1). This was
   the critical bug-fix from Harris-Intel; tests in `test_scorer.py::TestFilterHoa`
   pin the behavior.

## When to revisit

This posture should be re-evaluated if:
- publicsearch.us issues a cease-and-desist or rate-limits us in
  a way that suggests deliberate counter-measures
- Texas case law changes the legal treatment of robots.txt as ToS
- Dallas County moves to a different vendor with a different posture
- The repository becomes public (§3.7.2) — would require additional
  review of what's exposed in records.json

Update this document and `ARCHITECTURE.md` §B together when revisited.
