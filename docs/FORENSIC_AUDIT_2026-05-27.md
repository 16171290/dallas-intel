# Forensic Audit — Dallas-Intel Pipeline Operational State

**Date**: 2026-05-27
**Run audited**: post-merge of PR #1 (foreclosure OCR fixes) + PR #2 (name formatting)
**records.json snapshot**: 91 records, generated_at=2026-05-27
**Posture**: forensic recon only — no fixes implemented as part of this audit
**Forensic filter (round 3)**: *"Does this materially affect whether we can successfully contact the correct distressed owner?"*

---

## EXECUTIVE SUMMARY

The pipeline produces JSON that *looks* valid but contains **16 distinct classes of silent failure** that don't log errors and don't trigger warnings. Aggregated impact: of 91 records in today's records.json, approximately **72 are operationally usable as leads** (≈79%), and of those 72 a substantial fraction require manual cleanup before they can be skip-traced or imported into a CRM.

Critical structural finding: the pipeline is a **daily-snapshot generator**, not a lead-management system. Records that appeared yesterday but aren't in today's scrape window silently disappear (empirically: 13 records vanished overnight). The REL/RLP suppression machinery cannot function correctly under this regime because target NOFs vanish from the dataset weeks before their release notices arrive.

Operationally most damaging individual finding: **CSV export name-split is broken on 80% of rows** (`"Bryan, Holly"` becomes First Name = `"Bryan,"`, Last Name = `"Holly"`). Every GHL/CRM import inherits this corruption.

---

## TABLE OF CONTENTS

- [Methodology](#methodology)
- [Severity matrix](#severity-matrix)
- [Failure class catalog](#failure-class-catalog)
  - [Class 1 — Address semantic corruption (trustee offices)](#class-1)
  - [Class 2 — Grantor semantic corruption (boilerplate as name)](#class-2)
  - [Class 3 — Dashboard phantom rows](#class-3)
  - [Class 4 — Silent enrichment bypass (publicsearch PB records)](#class-4)
  - [Class 5 — Probate dropped by entity filter](#class-5)
  - [Class 6 — Resolver skip-when-address-set bug](#class-6)
  - [Class 7 — Missing source_url for probate](#class-7)
  - [Class 8 — OCR-garbled addresses pass through](#class-8)
  - [Class 9 — Owner/grantor drift unflagged](#class-9)
  - [Class 10 — Suspicious valid output (CITY, EST OF)](#class-10)
  - [Class 11 — Rolling-window amnesia](#class-11)
  - [Class 12 — DCAD address-index collision](#class-12)
  - [Class 13 — Probate 53.6% drop rate](#class-13)
  - [Class 14 — Resolver no fuzzy matching](#class-14)
  - [Class 15 — DCAD parse silent drop](#class-15)
  - [Class 16 — Inactive-marker lost to output](#class-16)
  - [Class 17 — CSV first/last name split broken](#class-17)
  - [Class 18 — CSV address truncation](#class-18)
  - [Class 19 — Wrong-person match (surname_only FP)](#class-19)
  - [Class 20 — Mailing-address name-prepended](#class-20)
  - [Class 21 — Tyler party-role completeness](#class-21)
  - [Class 22 — Attorney capture unsurfaced](#class-22)
  - [Class 23 — Dashboard default-hidden trust records](#class-23)
- [Operational trustworthiness assessment](#operational-trustworthiness-assessment)
- [Quantified lead-quality degradation](#quantified-lead-quality-degradation)
- [Investigations not yet performed](#investigations-not-yet-performed)
- [Glossary of pipeline stages](#glossary-of-pipeline-stages)

---

## METHODOLOGY

This audit was performed over three rounds:

1. **Round 1** — output-first audit of records.json, identifying record-level anomalies.
2. **Round 2** — investigation of DCAD parse fidelity, probate loss bucket, raw_excerpt content, and historical merge behavior.
3. **Round 3** — operational-correctness lens: owner-correctness, skip-trace quality, outbound viability, duplicate-call risk, dashboard visibility, escalation intelligence.

Each finding was verified against the live `data/records.json` and the actual source code paths. Where empirical verification required re-running the pipeline against external services, hypotheses are clearly labeled.

No code changes were made during the audit. This document is the durable record.

---

## SEVERITY MATRIX

Operational-impact-weighted ranking, applying the forensic filter ("does this materially affect whether we can contact the correct distressed owner?"):

### 🔴 BLOCKERS — operator cannot rely on this data for outbound work

| Class | Title | Records affected | Operational consequence |
|---|---|---|---|
| 11 | Rolling-window amnesia | 13 records vanished day-over-day | Operator's tracking work is destroyed nightly. Cannot follow a lead across days. |
| 12 | DCAD address-index collision | 157,687 accts silently dropped | Any condo/apartment/multi-unit foreclosure returns a random unit's owner. **Wrong person called.** |
| 17 | CSV name split broken | 73 of 91 (80%) CSV rows | GHL/CRM import lands names with stray commas as First Name. `"Bryan, Holly"` → FN=`"Bryan,"` LN=`"Holly"`. Auto-dialer and merge-tag rendering broken. |
| 18 | CSV address truncation | 34 of 91 (37%) | City/state/zip dropped. Mailers undeliverable. Phone-system area-code routing wrong. |
| 19 | Surname-only false positive | At least 1 confirmed | Operator calls 7018 Clear Springs expecting Linda Heather's family, reaches Bass family. |
| 19b | Multi-account exact match | Wilson, Robert: 8 accounts | We pick "first" — could be any of 8 Robert Wilsons. **Wrong-person-called silent.** |
| 20 | Mailing-address name-prepended | 13 of 59 (22%) | Skip-trace tools can't parse `"SHIRLEY M 4304 VILLAGE GREEN DR"`. Operator manually edits 13 leads. |

### 🟡 HIGH — silent loss of callable leads

| Class | Title | Records affected | Operational consequence |
|---|---|---|---|
| 13 | Probate 53.6% drop rate | 52 records lost | Half of probate decedents (high-value motivated-seller indicator) silently filtered out. |
| 23 | Default-hidden trust records | 10 of 91 (11%) | All trust-owned probate properties hidden by default. Operator opens dashboard and never sees them. |
| 21 | Tyler party-role completeness | All PB records | Executors, Administrators, Heirs, Beneficiaries silently dropped. Only Decedent+Applicant captured. |
| 22 | Attorney capture unsurfaced | 44 of 45 PB records | Attorneys are the BEST skip-trace target. Captured to `signal_metadata` but never shown on dashboard or in CSV. |
| 1 | Trustee-office addresses | 8 NOFs | Dashboard shows `"20405 State Highway, Houston"` as the property. Operator calls Houston, no answer. |
| 3 | Phantom dashboard rows | 6 NOFs | Blank Owner column + no DCAD enrichment + wrong address. Operator wastes effort triaging. |
| 4 | publicsearch-PB decedent bypass | 5 records | The 5 publicsearch-sourced PB records never run through decedent→DCAD fan-out. |
| 7 | Missing source_url | 45 of 91 (49.5%) | Half the leads cannot be verified — no way to drill into the original notice. |

### 🟢 MEDIUM — quality degradation, not immediate blocker

| Class | Title | Records affected |
|---|---|---|
| 6 | Resolver skip-when-address-set | ~10-12 records |
| 8 | OCR-garbled addresses pass through | 3 NOFs |
| 9 | Owner/grantor surname drift unflagged | 3 NOFs |
| 2 | "Liable" as grantor | 1 NOF |
| 10 | LP cities formatted as people | 3 LP records |
| 14 | Resolver no fuzzy matching | 4+ correctable misses |
| 15 | DCAD parse silent drop | unmeasured |
| 16 | Inactive-marker lost to output | 1+ records per run |

---

## FAILURE CLASS CATALOG

### Class 1 — Address Semantic Corruption (Trustee Offices) <a id="class-1"></a>

**Severity**: 🟡 HIGH (was 🔴 in original audit; reclassified because operator can detect via blank owner + non-Dallas city)

**Affected**: 8 NOFs (21% of all NOFs in current run)

**Records**:

| record_id | extracted "address" | actual entity |
|---|---|---|
| 315562551, 315562563 | `15851 N Dallas Pkwy, Addison` | Addison law firm |
| 315562552, 315562553, 315562567 | `20405 State Highway, Houston` | Codilis & Moody P.C. trustee office |
| 315561580 | `7730 Market Center Ave, El Paso` | El Paso trustee |
| 316689877 | `3332 Dilworth Drive, Grand Prairie` | (possibly legit — needs verification) |

**Root cause chain (compound silent failure)**:

1. `scraper/foreclosure_ocr.py` mixed-case-two-line pattern (added in PR #1) matches any mixed-case street address anywhere in the document, including law-firm signature blocks.
2. `address_normalized` gets set to the lawyer's office.
3. APN resolver and legal_resolver both check `if rec.get("address_normalized"): continue` (`scraper/legal_resolver.py:351`, similar in APN resolver) → never run.
4. DCAD enrichment fails silently (Houston/Addison/El Paso addresses not in Dallas County address_index).
5. Record persists in output with phantom address + no owner + no DCAD info.

**Evidence**: `signal_metadata.ocr.address_pattern == "mixed-case-two-line"` for all 6 confirmed cases. Their `raw_excerpt` fields actually contain the correct legal descriptions (`BEAR TREK RANCH-PHASE 4 Lot 12 Block B`, `VOUGHT MANOR ADDITI ECTION Lot 15 Block 10`, `LOT 169, OF LAKE RIDGE`) — which the resolvers should have used.

**Most ironic case**: Record 315561580 has `raw_excerpt: "2828 SOUTH LAKEVIEW DRIVE, CEDAR HILL, TEXAS, 75104"` — the correct address is literally there in the captured snippet, but the El Paso office address was extracted instead.

**Detection**: Records with `dcad_account=None` AND `address_normalized` matching any of: `15851 N DALLAS PKWY`, `20405 STATE HWY`, `7730 MARKET CENTER`, `17100 GILLETTE`, `5204 VILLAGE CREEK`, `14800 LANDMARK`.

---

### Class 2 — Grantor Semantic Corruption (Boilerplate as Name) <a id="class-2"></a>

**Severity**: 🟢 MEDIUM (today: 1 record; latent class)

**Affected**: 1 NOF (record 315562554) — `grantor: "Liable"`

**Root cause**: `name-as-grantor-borrower` pattern in `scraper/foreclosure_ocr.py` (`GRANTOR_PATTERNS` added in PR #1) matches `([A-Z]+) as Grantor/Borrower` greedily. The LAPRENSA GRANT NOF's OCR-garbled text contains "...BE LIABLE as Grantor/Borrower" and the regex captured "LIABLE".

**Class concern**: No mechanism prevents other boilerplate keywords (TRUSTEE, MORTGAGOR, BORROWER, DEBTOR, EXECUTOR, etc.) from being captured as names. The matcher doesn't validate that the captured token is plausibly a name.

**Detection**: Grantor field matches single-token capitalized verbs/legal-jargon. Approximate filter: 1-token grantor with length < 8 or matching a known-boilerplate wordlist.

---

### Class 3 — Dashboard Phantom Rows <a id="class-3"></a>

**Severity**: 🟡 HIGH

**Affected**: 6 NOFs with completely blank Owner/Grantee column

**Records**: 315539866, 315562551, 315562552, 315562553, 315562563, 316689877

**Will render in dashboard as**:
- Property address column: a lawyer's office (5 of 6 overlap with Class 1)
- Owner column: blank (no grantor, no grantee, no dcad_owner)
- Score: 45-65 like any normal record

**Operational consequence**: Operator sees a row with NO useful information yet the lead count is inflated. There is no filter dropping rows with zero usable identifying information.

**Detection**: `record.dallas_code=="NOF" AND not record.grantor AND not record.grantee AND not record.dcad_owner`.

---

### Class 4 — Silent Enrichment Bypass: publicsearch-Sourced PB Records <a id="class-4"></a>

**Severity**: 🟡 HIGH

**Affected**: 5 PB records (Gordon Vertis Oneta, Flynn Linda Barfield, Gordon Finis Lee, Gross Dennis Lester, Murdock David H)

**Root cause**: `match_decedent_to_dcad()` is only called inside `canonicalize_probate()` (the re:SearchTX-scraper path) in `scraper/enrichment.py`. `canonicalize_publicsearch()` does not call it. The publicsearch list-view shows these as historical probate filings — they get tagged `PB` but never get the decedent→property lookup that re:SearchTX-sourced records receive.

**Detection signal**: `signal_metadata.decedent_owned_properties == None` (vs `[]` for "attempted, no match" and `[...]` for matched). The dashboard cannot distinguish "lookup attempted, no match" from "lookup never attempted."

**Impact**: 5 valid decedent leads silently miss the DCAD owner-name fan-out path.

---

### Class 5 — Probate Records Dropped by Entity Filter <a id="class-5"></a>

**Severity**: 🟡 HIGH (also tracked as Class 13 with quantification)

**Affected**: Approximately 52 probate records (97 fetched → 45 surviving from probate.txcourts.gov)

**Root cause**: `entity_filter._resolve_target()` in `scraper/entity_filter.py` returns `grantee` for non-NOF records. For PB records, grantee == `applicant_name`. When Tyler returns a probate hit with no Applicant/Petitioner role yet (case just opened, applicant TBD), `applicant_name` is None → `entity_filter` drops via:

```python
if not target:
    removed.append(rec)
    continue
```

The decedent (the actual lead, in `grantor`) is preserved on the record but never gets a chance because the filter never considers it for PB records.

**Why this is invisible in logs**: `entity_filter` log says "Entity filter: removed 83 records (181 → 98)" — one lumped count. The breakdown of *empty-target* drops vs *legit-LLC* drops isn't recorded.

---

### Class 6 — Resolver Skip-When-Address-Set Bug <a id="class-6"></a>

**Severity**: 🟢 MEDIUM (cascades into Class 1 amplification)

The APN and legal_description resolvers both check:
```python
if rec.get("address_normalized"):
    continue
```

This skips records whose `address_normalized` is any truthy string — including:
- City-only strings (`"DALLAS"`, `"COPPELL"`, `"IRVING"`) from publicsearch list-view scrape
- The law-firm addresses my new pattern incorrectly extracted (Class 1)

**Affected records**:
- 4 NOFs with city-only address but valid APN or legal-desc available
- The 8 trustee-address NOFs from Class 1

**Total impact**: ~10-12 records that could have been DCAD-resolved aren't.

---

### Class 7 — Missing source_url for Probate Records <a id="class-7"></a>

**Severity**: 🟡 HIGH

**Affected**: 45 of 91 records (49.5%) — all probate-source records (`pro-*` record_ids)

**Root cause**: `canonicalize_probate()` sets `source_url: None` (`scraper/enrichment.py:545`).

**Operational consequence**: The dashboard's "click to verify" workflow doesn't exist for these. The operator cannot drill into the original re:SearchTX case page. The only identifying info is the decedent's name + the synthetic `pro-{hash}` record_id. If the decedent name + DCAD lookup is wrong, there's no way for a human to cross-reference.

---

### Class 8 — OCR-Garbled Addresses Pass Through <a id="class-8"></a>

**Severity**: 🟢 MEDIUM

**Affected**: 3 NOFs

| record | extracted address |
|---|---|
| (id) | `'1442 MA 5: {E) DESOTO, TX 75115'` |
| (id) | `'2206 BEA OS REEFGRAND PRAIRIE, TX 75051'` |
| (id) | `'3031 CREST RIDGE D &, DALLAS, TX 75228'` |

These look like real addresses to the system, get normalized, and fail DCAD lookup. **No quality flag** indicates the OCR text was garbled. There's no `signal_metadata.ocr_confidence` or similar.

---

### Class 9 — Owner/Grantor Surname Drift Unflagged <a id="class-9"></a>

**Severity**: 🟢 MEDIUM (operationally meaningful but not corrupted data)

**Affected**: 3 NOFs

| grantor | dcad_owner | likely meaning |
|---|---|---|
| Hines, Hartford Jr & Hi, Kim | Winans, Kim A | Recent sale OR Kim Winans bought from Hines via marriage etc. |
| Langwell, Lucy J. | Gulf Coast Western LLC | Commercial mineral foreclosure, not residential |
| Salcedo, Erika | Dimoulakis, Nick | Strong recent-sale or wrong-property signal |

The pipeline silently accepts these without raising a "name drift" warning. Operationally, these are high-value flags requiring human attention but they're indistinguishable from clean records.

---

### Class 10 — Suspicious Valid Output <a id="class-10"></a>

**Severity**: 🟢 MEDIUM

Records that look valid but have semantic problems:

- `grantor: "City, Hutchins"` (LP record 315548936) — formatted from "HUTCHINS CITY"; the formatter inverted the city's name as if it were a person.
- `grantor: "City, Dallas"` (2 LP records) — same issue with "DALLAS CITY".
- `dcad_owner: "Bell, Angela B Est Of"` — "EST OF" abbreviation survived formatting (entity check uses full "ESTATE OF" only).
- `grantor: "Gaddis, Protho"` vs `dcad_owner: "Gaddis Revocable Trust"` — Protho is a real first name but the property is trust-owned; the lead is the trustee not "Gaddis" personally.

---

### Class 11 — Rolling-Window Amnesia (STRUCTURAL) <a id="class-11"></a>

**Severity**: 🔴 BLOCKER

**Architectural mismatch between pipeline behavior and "operational intelligence" requirements.**

**Evidence**:
- `_merge_seen_dates()` in `scraper/main.py:553` drops prior records not in today's scrape:
  > "Records from prior that aren't in the new batch are NOT carried forward by this function."
- **13 records present in yesterday's records.json silently disappeared today** — empirically confirmed by diffing yesterday vs today's records.json from git.
- Dropped today: 11 PB + 2 NOF — all had `filing_date` 2026-05-18/19/20.
- NOF `filing_date` range in current records.json: **6 days only** (`2026-05-20..2026-05-26`).
- NOF `sale_date` range: 2026-07-07 to 2026-08-04 (6-8 weeks out).

**Consequences (cascading)**:

| Time | What happens |
|---|---|
| Day 0 | NOF filed, appears in dashboard |
| Day 7 | Date window rolls past — **NOF vanishes** silently |
| Day 21 | Foreclosure sale occurs at courthouse |
| Day 30-45 | REL/RLP filed if sale cancelled or property released |
| Day 30+ | Pipeline tries to suppress the NOF via address match → **nothing to suppress** |

**Empirical confirmation of REL/RLP defeat**: Today's log says `"Suppression: 4 REL/RLP records applied; 1 records marked inactive"`. **3 of 4 REL/RLPs operationally did nothing** because their target NOFs had already vanished from the rolling window.

**Operational impact**:
- Operator can never see "what foreclosures sell in the next 4 weeks" — they all vanish before their sale date
- Probate cases (active for months in court) drop after 7 days
- A record the operator was investigating yesterday is gone today
- The REL/RLP suppression machinery exists but is structurally defeated
- `first_seen` IS preserved correctly for the 81 surviving records (100% preservation) — the data plumbing works, but the data isn't kept

---

### Class 12 — DCAD Address-Index Collision (SILENT CORRUPTION) <a id="class-12"></a>

**Severity**: 🔴 BLOCKER

**Evidence from production log**:
```
Address index built: 700,826 unique addresses
Built DCAD account index: 858,513 accounts
```

**157,687 DCAD accounts (18.4%) share an address with another account and are silently discarded** because `scraper/dcad_bulk.py:145` does `index[norm] = acct` (overwrite on collision):

```python
for _, row in df.iterrows():
    ...
    index[norm] = str(row["ACCOUNT_NUM"]).strip()
```

**Root cause**: `normalize_address` deliberately strips unit info (`APT 5` → blank) for USPS-style canonicalization (`scraper/normalize.py:232`):

> "6. Strip unit-portion (APT/STE/etc.) - use :func:`extract_unit` to retrieve it separately."

But DCAD has separate accounts per unit. Apartment building with 100 units → 1 entry in address_index, last-iterated wins. Condo with 50 units → 1 entry, arbitrary unit.

**Empirical impact on current records**: Only 1 current record has explicit "BUILDING/UNIT" mention (record 315562563 — UNIT 1138 IN BUILDING F, already broken by Class 1). The collision impact is *masked* in this run because most current foreclosures happen to be single-family homes. But on any future condo/apartment foreclosure, the dcad_owner returned will be **a random other unit's owner**, not the foreclosed unit's owner. No warning fires.

**Detection signal**: None. Records with collisions look identical to clean records in the JSON.

---

### Class 13 — Probate 53.6% Drop Rate (DETAILED) <a id="class-13"></a>

**Severity**: 🟡 HIGH

**Numbers**: 97 fetched → 45 surviving (probate.txcourts.gov source) = **52 records lost (53.6%)**.

**Root cause**: See Class 5. The dominant theory is empty `applicant_name` triggering `entity_filter`'s `not target` branch.

**Operational impact**:
- The decedent → DCAD owner-name fan-out works at 35% hit rate on records that survive.
- If the 52 lost records have similar hit rates, **~18 additional property-resolved probate leads are being silently discarded daily**.

---

### Class 14 — Resolver No Fuzzy Matching <a id="class-14"></a>

**Severity**: 🟢 MEDIUM

The legal-description resolver doesn't fuzzy-match. OCR-garbled subdivision names silently fail lookup:

| OCR-garbled (in raw_excerpt) | Likely correct name | Resolver outcome |
|---|---|---|
| `SANDALWOGR ADDITIO Pp` | SANDALWOOD ADDITION PT | no_match |
| `VOUGHT MANOR ADDITI ECTION` | VOUGHT MANOR ADDITION SECTION | no_match |
| `BEAR TREK RANCH-PHASE 4` | BEAR CREEK RANCH-PHASE 4 | no_match |
| `THE FIFTH INCREMENT OF PLYMOUTH PARK NORTH` | (uncertain whether exists in DCAD) | no_match |

The log line "Legal-description resolution: 6/182 resolved (3.3%)" tells us only the bottom line. It doesn't surface that ≥4 records have correctable OCR errors that fuzzy normalization could rescue.

---

### Class 15 — DCAD Parse Silent Drop <a id="class-15"></a>

**Severity**: 🟢 MEDIUM (unmeasured)

`scraper/dcad_bulk.py:97-98`:
```python
df = pd.read_csv(
    f, sep=",", dtype=str,
    keep_default_na=False,
    on_bad_lines="warn",        # pandas warns to stderr, NOT structured log
    encoding_errors="replace",  # non-UTF8 -> U+FFFD silently
)
```

**What we can't see from logs**:
- How many ACCOUNT_INFO rows were dropped on parse
- Which fields contain replacement characters (`U+FFFD`)
- Whether any owner names / addresses are silently corrupted by encoding replacements

**Hypothesis**: DCAD's bulk CSV is reasonably clean, so drops are probably low single digits. But there is **no measurement** — the system is operationally blind to its own parse health.

---

### Class 16 — Inactive-Marker Lost to Output <a id="class-16"></a>

**Severity**: 🟢 MEDIUM

**Evidence**:
- Stage 9 log: `"Suppression: 4 REL/RLP records applied; 1 records marked inactive"`
- Final records.json: 0 inactive records

The 1 inactive record didn't survive to the final output. So even within a single run, the inactive marker doesn't persist to the dashboard. Compounds with Class 11 — even when suppression works within a run, the result isn't visible.

---

### Class 17 — CSV First/Last Name Split Broken <a id="class-17"></a>

**Severity**: 🔴 BLOCKER

**Affected**: 73 of 91 (80%) CSV rows in `exports/ghl_export_20260527.csv`

**Root cause**: `scraper/output.py:250-258`:
```python
def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return ("", parts[0])
    # Assume last token is surname; rest is first/middle.
    return (" ".join(parts[:-1]), parts[-1])
```

The CSV exporter operates on the POST-FORMATTER name strings (PR #2 produced "Last, First Middle" format), but `_split_name` doesn't understand the comma form. It naively splits on whitespace.

**Examples**:
- `"Bryan, Holly"` → `parts=["Bryan,", "Holly"]` → FN=`"Bryan,"`, LN=`"Holly"` ← LAST name in FIRST column with stray comma
- `"Flynn, Linda Barfield"` → FN=`"Flynn, Linda"`, LN=`"Barfield"` ← treats middle name as surname
- `"Ross, Jennifer"` → FN=`"Ross,"`, LN=`"Jennifer"`
- `"Murdock, David H"` → FN=`"Murdock, David"`, LN=`"H"`

**Operational consequence**: GoHighLevel import lands names with broken first/last fields. When operator searches GHL for "Holly Bryan", the contact may exist but be filed under last name "Holly" with first name "Bryan," (with comma). Merge tags `{{first_name}}` render as `"Bryan,"` in outbound SMS/email. The PR #2 name-formatter work compounded a latent pre-existing bug — pre-formatting only ~50% had this issue (raw "BRYAN, HOLLY" form); post-formatting ~80% hit it.

**Note**: This was introduced/amplified by the name-formatting PR (#2). The CSV exporter wasn't updated to understand the new canonical name shape.

---

### Class 18 — CSV Address Truncation <a id="class-18"></a>

**Severity**: 🔴 BLOCKER

**Affected**: 34 of 91 (37%) CSV rows have truncated Address column

**Root cause**: `scraper/output.py:_split_address` assumes 3 comma-separated parts: `"STREET, CITY, STATE ZIP"`. For records.json addresses with different shapes, the parsing fails:

| Address form | Parts | Behavior |
|---|---|---|
| `"9915 LINGO LANE, DALLAS, TX 75228"` | 3, with state | Works correctly |
| `"3332 Dilworth Drive, Grand Prairie, 75050"` | 3, no state | `parts[-1]="75050"` doesn't match `^([A-Z]{2})` → state="75050", zip="" |
| `"509 BLANCO DRIVE, DESOTO, TEXAS, 75115"` | 4 parts | `parts[-1]="75115"` doesn't match → state="75115", zip="" |
| `"1103 CHRISTOPHER COURT, IRVIN G, TX 75060"` | 3 | OCR-garbled "IRVIN G" as city |

**Result in CSV**: City/State/Zip columns empty, Address truncated to street-only.

**Operational consequence**: When operator imports CSV to a CRM, the lead has no city/state/zip → undeliverable mail. Phone number routing decisions based on area code default to Dallas when actual property is in (say) Grand Prairie zip 75050.

---

### Class 19 — Wrong-Person-Called: Surname-Only False Positive <a id="class-19"></a>

**Severity**: 🔴 BLOCKER (confirmed wrong-person matches)

#### 19a — Surname matches first-name in trust string

**Confirmed case**: `'Heather, Linda A.'` matched to `'Bass Jeffery & Heather Revocable Trust The'` at 7018 Clear Springs Pkwy.

- "Heather" is Linda's surname (e.g., "Linda Heather")
- "Heather" in the trust is a first name of one of the trust grantors
- Tier: `surname_only` (riskiest tier)
- The `expand_joint_owners` splits `BASS JEFFERY & HEATHER REVOCABLE TRUST THE` into two owner strings; the second normalizes to just `HEATHER` (after stripping TRUST role-suffix); this owner-index entry then matches Linda Heather's surname

**Operational impact**: Operator calling 7018 Clear Springs reaches Bass family — wrong person.

#### 19b — Multi-account exact match (silent first-pick)

**Records affected**:

| decedent | tier | accounts matched | properties |
|---|---|---|---|
| Wilson, Robert | exact | **8** | 355 CARDINAL CREEK DR, 3613 BERMUDA DR, 4216 BEAVER BROOK PL, 4509 CHIESA RD, 1220 FRENCHMANS DR, 2506 SPRINGDALE DR, 2934 GREEN OAKS DR, 1024 RIDGEWOOD LN |
| Moore, Albert Jr | exact | 2 | 1916 STOVALL DR, 6640 BRADDOCK PL |
| Stafford, Sharon M. | no_middle | 2 | 4518 AMANDA CT, 4709 CHALK CT |
| Thompson, Shirley Mayhugh | no_middle | 3 | 11117 WEBB CHAPEL RD, 2612 PEACH TREE LN, 6427 LAS COLINAS BLVD |
| Garcia, Efren Martinez | no_middle | 5 (warning fired) | 5 different addresses |
| Wilson, Robert (above warning fired at 8 accounts) | | | |
| Martinez, Anita N. | no_middle (warning fired) | 4 | 4 different addresses |

**Logic** (`scraper/enrichment.py:471`):
```python
if decedent_properties:
    primary_address_normalized = decedent_properties[0]["address_normalized"]
```

System silently picks the **first** account from the matched list (DCAD's natural insertion order). When there are 8 Robert Wilsons in Dallas, we have no basis for which one is the actual probate decedent. The dashboard surfaces the first account's data and labels it as "Robert Wilson's property."

**Operational impact**: Operator skip-traces 355 Cardinal Creek Dr → calls the family there → it's a totally unrelated Robert Wilson.

#### 19c — Tier-ladder match-confidence audit (current run)

Decedent matches in today's records.json (34 PB records with match):

| Tier | Count | Risk profile |
|---|---|---|
| exact | 8 | Same normalized name; can still match multiple unrelated people with same name |
| no_middle | 12 | Drops middle name; common-name risk |
| initial_form | 5 | Substitutes middle initial; medium risk |
| double_initial | 1 | Two middle initials; medium risk |
| surname_only | 8 | LOWEST CONFIDENCE; uniqueness-gated to ≤2 accounts; still produces false positives like Class 19a |

**Accounts-per-match distribution**:
- 1 account: 24 matches
- 2 accounts: 6 matches
- 3 accounts: 1 match
- 4 accounts: 1 match (Martinez Anita)
- 5 accounts: 1 match (Garcia Efren)
- 8 accounts: 1 match (Wilson Robert)

---

### Class 20 — Mailing-Address Name-Prepended Corruption <a id="class-20"></a>

**Severity**: 🔴 BLOCKER (22% of populated mailing fields)

**Affected**: 13 of 59 (22%) records with mailing-address data

**Root cause**: `scraper/enrichment.py` (around line 315-321) concatenates DCAD's `OWNER_ADDRESS_LINE1..4` with `"\n".join()`. DCAD's LINE1 sometimes contains a *name* (trustee, c/o party, executor), not part of the street address.

**Examples**:

| decedent | mailing field value (corrupted) |
|---|---|
| Bryan, Michael Sanford | `'HOLLY CELESTE\n514 NESBIT DR'` |
| Hawkins, Cynthia | `'COGGINS CONSTANCE\n2126 KESSLER CT'` |
| Wilson, Robert | `'HARDIN JEFFREY\n355 CARDINAL CREEK DR'` |
| Rollings, Raymond Leith Martin | `'KAREN L\n3704 CRANSTON DR'` |
| Glazer, Bennett Joe | `'MARION L\n5314 LOBELLO DR'` |
| Martinez, Anita N. | `'MARTINEZ ELENA\n2915 HEDGEROW DR'` |
| (NOF, no decedent) | `'VALDEZ ARACELY ARMENDARIZ\n7821 LARCHRIDGE DR'` |
| (NOF) | `'GONZALEZ ALBA LIRIA RODRIGUEZ\n1817 MILLWICK ST'` |
| (NOF) | `'MARTHA\n5605 SADDLEBACK RD'` |
| (NOF) | `'REYES CANDIDA ZULEYMA REYES\n509 BLANCO DR'` |
| (NOF) | `'KING ELISE EST OF\n2639 LENWAY ST'` |
| Jones, Arthur Lee Jr | `'JAY L ARTHUR ETAL TRUSTEE\n347 TANGLEWOOD LN'` |
| Steinhart, Phyllis Y. | `'PHYLLIS Y 25 ROBLEDO DR'` (one-line variant, no `\n`) |

**Operational consequence**:
- Operator pasting these into USPS/PostGrid for verification: address parser will treat "SHIRLEY M" as part of the street name and fail.
- Mail sent to these "addresses" returns as undeliverable.
- Skip-trace tools (BatchSkipTracing, etc.) won't normalize these — they reject malformed input.

**Hidden upside**: The prepended names are often valuable skip-trace data themselves (the c/o party is usually a family member, executor, or trustee). The current concat destroys both the address AND the name as separate signals.

---

### Class 21 — Tyler Party-Role Completeness <a id="class-21"></a>

**Severity**: 🟡 HIGH

**Affected**: All PB records from probate.txcourts.gov (45 records, applies to all 97 fetched)

**Root cause**: `scraper/probate.py:_hit_to_record` only extracts party roles:
```python
if role.casefold() == "decedent":      # captured
    ...
elif role.casefold() in ("applicant", "petitioner"):  # captured
    ...
```

**Silently dropped party roles** (per typical Tyler/Odyssey TX-Probate schema):
- ❌ EXE / Executor — named in the will, fiduciary duty over estate. Most likely person to sell property.
- ❌ ADM / Administrator — court-appointed when no will exists.
- ❌ BEN / Beneficiary — named in the will to receive specific property.
- ❌ HER / Heir — listed in heirship determinations. Multiple heirs are common.
- ❌ GUA / Guardian — for minor heirs.
- ❌ GAL / Guardian Ad Litem
- ❌ CRD / Creditor
- ❌ RES / Respondent (contested cases)
- ❌ OBJ / Objector
- ❌ INT / Intervenor

**Operational impact**: For heirship cases naming 4 heirs, the pipeline captures only the petitioner. The 3 other heirs are valuable callable contacts — they may be the actual sellers — but are invisible.

**Note**: Only 2 of 45 PB records have any `additional_applicants` populated. Either Tyler doesn't return many co-applicants in its parties list, or the extraction logic for `applicant_names[1:]` is also fragile.

---

### Class 22 — Attorney Capture Unsurfaced <a id="class-22"></a>

**Severity**: 🟡 HIGH

**Affected**: 44 of 45 PB records (97% of probate records have an attorney captured)

**Status**: Attorneys ARE captured into `signal_metadata.attorneys: list[str]`, but:
- Dashboard renders only grantor / grantee / dcad_owner — attorneys never appear in any column
- CSV export doesn't include attorneys
- No detail-panel display

**Examples**:

| decedent | attorneys (captured but invisible) |
|---|---|
| Bryan, Michael Sanford | STRANN, TIMOTHY RANDALL |
| Matus, Jose Aristides | WILLIS, CAROL BERNARD |
| Hawkins, Cynthia | ZACKARY HOBBS |
| Garcia, Efren Martinez | ELKINS, HEIDI JILL |
| Fitzgerald, John R. | NEUHOFF, THOMAS HUDSON |
| Jordan, Brenda G. | HALES, JACK ROBERT |

**Operational significance**: The probate attorney is arguably the **highest-value skip-trace target** in a probate workflow:
- They know exactly who the executor, administrator, and heirs are
- They have direct phone/email contact info for the family
- They are professionally obligated to respond to property-related inquiries
- They often welcome qualified buyer outreach (helps close the estate faster)

We capture this data on every probate record and never surface it.

---

### Class 23 — Dashboard Default-Hidden Trust Records <a id="class-23"></a>

**Severity**: 🟡 HIGH

**Affected**: 10 of 91 records (11%) — all 9 of the trust-owned probate properties + Gulf Coast Western LLC

**Root cause**: `dashboard/index.html:1022-1029` defines `COMMERCIAL_TOKENS`:
```javascript
const COMMERCIAL_TOKENS = [
  "LLC", "INC", "CORP", "CORPORATION", "INCORPORATED", "COMPANY",
  "HOLDINGS", "DEVELOPMENT", "PROPERTIES", "PARTNERS", "PARTNERSHIP",
  "TRUST", "BROTHERS", "BROS", "INVESTMENTS", "ENTERPRISES",
  "GROUP", "VENTURES", "REALTY", "REAL ESTATE",
  "LTD", "LP", "LLP", "PLLC", "PA", "PC", "ASSOCIATES",
  "BANK", "MORTGAGE", "FUND", "CAPITAL", "EQUITY",
];
```

Combined with `filter-hide-commercial` defaulting to `checked` and the filter rule `if (f.hideCommercial && isCommercialEntity(r)) return false;` (dashboard/index.html:1362).

**Records hidden by default**:

| dallas_code | grantor | dcad_owner | token that triggers hiding |
|---|---|---|---|
| PB | Jordan, Brenda G. | Jordan Living Trust | TRUST |
| NOF | Langwell, Lucy J. | Gulf Coast Western LLC | LLC |
| PB | Heather, Linda A. | Bass Jeffery & Heather Revocable Trust The | TRUST |
| PB | Rhodes, Margaret | Rhodes Family Trust | TRUST |
| PB | Gaddis, Protho | Gaddis Revocable Trust | TRUST |
| PB | Short, Ina L. | Short Family Trust | TRUST |
| PB | Jensen, John Terence Jr | Jensen Family Trust | TRUST |
| PB | Earls, Richard Lawrence | Earls Trust | TRUST |
| PB | Jones, Arthur Lee Jr | Arthur Revocable Trust The | TRUST |
| PB | Jones, Berti Schulz | Jones Berti Revocable Trust | TRUST |

**Operational impact**: Probate-trust properties are precisely the kind of estate that's likely to dissolve and sell. The dashboard's default filter hides them — operator opens the dashboard expecting 91 records and sees 81. Trust opportunities silently invisible.

---

## OPERATIONAL TRUSTWORTHINESS ASSESSMENT

For each link in the outbound chain, can the operator trust it?

| Link | Trustworthy? | Why not |
|---|---|---|
| Owner name | ⚠ Partially | 80% of CSV exports have broken First/Last split (Class 17). At least 1 confirmed wrong-person match (Class 19a). Multiple Wilson Roberts silently merged (Class 19b). |
| Property address | ⚠ Partially | 8 NOFs have lawyer-office addresses (Class 1). 3 have OCR garbage (Class 8). Condo unit info lost in normalization (Class 12). |
| DCAD match | ⚠ Partially | 157k accounts dropped from address-index (Class 12). Surname_only tier produces FP on first-name-as-surname pattern (Class 19a). Trust matches via shared keyword (potentially valid, but unverified). |
| CSV export | ❌ Untrustworthy | 80% name-split broken (Class 17). 37% address-truncated (Class 18). Mailing-address name-prefix not stripped (Class 20). |
| Dedupe | ⚠ Partially | No record_id duplicates today. But "multi-NOF" detection is corrupted by Class 1 (phantom dupes on lawyer addresses). True historical dedup impossible (Class 11). |
| Dashboard visibility | ❌ Hides high-value leads | 11% silently hidden by default `hideCommercial` filter (Class 23) — disproportionately the high-value probate-trust records. |
| Callable lead | ❌ Risky | Aggregated risk of: wrong owner + wrong address + wrong mailing + lost history → **operator may call the wrong person, the wrong number, or no one at all.** |

---

## QUANTIFIED LEAD-QUALITY DEGRADATION

For today's 91 records, best estimate of usable leads:

```
Total in records.json                              :  91
- Class 1 (phantom-address NOFs)                   :  -8
- Class 3 (blank-owner NOFs, mostly overlap)       :  -3   (3 not overlapping with Class 1)
- Class 19 (high-risk wrong-person matches)        :  -2   (Wilson Robert 8-acct, Heather→Bass)
- Class 8 (OCR-garbled addresses)                  :  -3
- Class 10 (cities as people)                      :  -3
                                                   ---
TRULY OPERATIONALLY-USABLE LEADS                   ≈  72   (≈79% of count)
```

But of those 72:
- ~13 have name-prepended mailing addresses requiring manual cleanup (Class 20)
- ~10 are hidden by default `hideCommercial` filter (Class 23) — operator must un-hide
- ~45 lack source_url (Class 7) — operator can't drill-in to verify
- 80% will export with broken First/Last in CSV (Class 17)

And the system-level issues:
- 52 probate records were lost upstream at entity_filter (Class 13)
- 13 records that were in yesterday's data are gone today (Class 11)
- The REL/RLP suppression flow can't function on this rolling window (Class 11 cascade)
- 157,687 DCAD accounts silently dropped from address-index (Class 12) — affects any future condo/apartment foreclosure

---

## INVESTIGATIONS NOT YET PERFORMED

Areas the audit did not yet cover. Listed in case future forensic rounds want to continue.

### Lead-correctness adjacent (high priority for future investigation)
1. Joint-owner `&` parsing consistency across modules (`expand_joint_owners`, `_clean_owner`, normalize, formatter, owner_index)
2. Probate `_party_name` extraction logic — what does it do with "SMITH JOHN ETAL"
3. `dcad_homestead` semantics — is the homestead bit reliably set when it should be?
4. `OWNER_ADDRESS_LINE1..4` deterministic semantic meaning (which line is always c/o, which is street, which is city/state/zip)
5. The `convert_bankruptcy_to_dcad_format` path — separate from probate_matcher, what's its FP rate?

### Operational-correctness adjacent (medium priority)
6. Buy-box flag logic — currently all 91 records have `in_buy_box: true`. What does the gate actually do?
7. Score discriminative power — all 91 records score 45-65 (20-point band). Is the scorer providing real signal?
8. Multiple NOFs on same property (historical) — given Class 11, can't see this today, but may matter if Class 11 fixed
9. Dashboard workflow-state (localStorage) — does the disposition/notes/phone state survive across pipeline runs?
10. The 30+ records with `dcad_account` set — sanity-check a sample against actual DCAD.

### Adjacent system properties (lower priority)
11. GitHub Actions: Daily-scrape commit-message accuracy
12. GHL/CRM destination semantics
13. Discord notification fidelity
14. Cache invalidation behavior (DCAD ZIP, Playwright Chromium)
15. Cross-county leakage (mostly resolved as Class 1 corruption with a few true cross-county records)

---

## GLOSSARY OF PIPELINE STAGES

For cross-referencing failure classes against the pipeline flow as logged:

| Stage | Code reference | What it does |
|---|---|---|
| 1 | `dcad_bulk.parse_dcad_tables` | Fetch + parse DCAD bulk ZIP into DataFrames |
| 2 | (skipped) | Foreclosure-PDF source (deprecated 2026-02-24) |
| 3 | `publicsearch.scrape_all` | Walk publicsearch.us for non-NOF instrument codes (LP, RLP, TXL, PB, BR, SZS, REL) |
| 3.5 | `foreclosures_ps.scrape_foreclosures` | Walk publicsearch.us Foreclosures department for NOF records |
| 3.6 | `foreclosure_ocr.enrich_foreclosure_records` | OCR + extract grantor/sale_date/address/legal_desc/APN per NOF |
| 4 | `probate.fetch_dallas_probate` | Query re:SearchTX Tyler API for Dallas probate cases |
| 5 | `main._merge_*` | Merge records from all sources into `all_records` |
| 6 | `main._merge_seen_dates` | **CLASS 11 LIVES HERE.** Update first_seen / last_seen from prior records.json |
| 6.4 | `legal_resolver.resolve_apn_to_address` | APN → DCAD account → address (added in PR #1) |
| 6.5 | `legal_resolver.resolve_legal_descriptions` | Subdivision/lot/block → DCAD account → address |
| 7 | `enrichment.enrich_batch` | Address-based DCAD lookup; stamp owner/value/mailing fields |
| 8 | `governmental_grantor` filter | Drop records with US govt as grantor |
| 8.5 | `entity_filter.filter_entity_records` | **CLASS 13 LIVES HERE.** Drop records whose target is empty or corporate-entity |
| 9 | `scorer.score_and_filter` | Per-record scoring, REL/RLP suppression, HOA filter |
| 10 | `main.annotate_buybox` | Stamp `in_buy_box` flag (currently no-op — all records pass) |
| 11 | `output.write_records_json` + `output.write_daily_csv` | **CLASSES 17, 18 LIVE HERE.** Atomic write + CSV export |
| 12 | `monitor.notify_discord` | Discord run summary notification |

---

## REPRODUCING THIS AUDIT

To verify any finding against a fresh records.json, the audit was performed by:

1. Pulling latest `main` (containing `data/records.json` and `exports/ghl_export_YYYYMMDD.csv`)
2. Running ad-hoc Python scripts that joined records.json fields to identify each anomaly class
3. Cross-referencing against the source code at the relevant module paths

No special tooling was used — pure inspection. Future rounds can use the same approach.

---

## CHANGE LOG

- **2026-05-27** — initial audit captured (rounds 1-3 complete)
