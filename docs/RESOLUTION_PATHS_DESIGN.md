# Resolution Paths Design — DCAD Lead-Match Recovery Architecture

**Status**: Design document. No code implementation yet. This is the blueprint produced by the 2026-05-27 forensic audit findings + operator design decisions.

**Companion document**: `docs/FORENSIC_AUDIT_2026-05-27.md` — the audit that motivated this design.

**Operator decisions captured**:
1. Trust threshold: **single-path acceptable with confidence-low badging** (loose-tier matches stamp dcad_account but carry confidence warnings)
2. Path C scope: **skip-bug fix + fuzzy subdivision matching**

---

## TABLE OF CONTENTS

- [1. Goals and non-goals](#1-goals-and-non-goals)
- [2. Resolution paths overview](#2-resolution-paths-overview)
- [3. signal_metadata.resolution_history schema](#3-signal_metadataresolution_history-schema)
- [4. Stage 6.3 — Path B (raw_excerpt clean-address fallback)](#4-stage-63--path-b-raw_excerpt-clean-address-fallback)
- [5. Stage 6.4 — Path A (NOF grantor → owner_index)](#5-stage-64--path-a-nof-grantor--owner_index)
- [6. Stage 6.5 — Path C (resolver skip-fix + fuzzy subdivision)](#6-stage-65--path-c-resolver-skip-fix--fuzzy-subdivision)
- [7. Stage 6.6 — Cross-path agreement + confidence stamping](#7-stage-66--cross-path-agreement--confidence-stamping)
- [8. Sanity-check additions](#8-sanity-check-additions)
- [9. Dashboard implications](#9-dashboard-implications)
- [10. CSV export implications](#10-csv-export-implications)
- [11. Test design](#11-test-design)
- [12. Expected operational outcomes](#12-expected-operational-outcomes)
- [13. Open questions](#13-open-questions)

---

## 1. Goals and Non-Goals

### Goals

- **Lift NOF lead-recovery rate**: from current ~55% (21/38 NOFs) toward 90%+ via three complementary paths.
- **Make confidence visible**: every record carries an explicit confidence label (`high`/`medium`/`low`) and any applicable warnings, surfaced in the dashboard and CSV.
- **Preserve provenance**: every resolution attempt (including misses) is recorded in `signal_metadata.resolution_history` so future audits can trace exactly what happened to any record.
- **Defeat the failure classes identified in the audit**: especially Classes 1, 6, 19, 24, 25.
- **Fail safely**: when paths disagree, mark `confidence: low` instead of silently picking. The operator reviews disagreements; the pipeline doesn't make unilateral wrong-person calls.

### Non-Goals (this design)

- **Rolling-window amnesia fix** (Class 11) — separate architectural problem; out of scope here.
- **CSV name-split fix** (Class 17) and **CSV address truncation** (Class 18) — separate output-layer fixes; out of scope here. (Should be addressed in a parallel design.)
- **Dashboard `hideCommercial` default fix** (Class 23) — separate dashboard concern.
- **Tyler party-role expansion** (Class 21) and **attorney surfacing** (Class 22) — separate probate-scraper improvements.

---

## 2. Resolution Paths Overview

| Path | Trigger | Input | Lookup target | Estimated recovery (Bucket B today) |
|---|---|---|---|---|
| Stage 7 (existing) | always | `address_normalized` | `address_index` | already recovers ~55% of NOFs |
| **Path B** (new, Stage 6.3) | `address_normalized` is suspect or empty | `raw_excerpt` clean street address | `address_index` (retry) | ~5 of 11 unresolved NOFs |
| **Path A** (new, Stage 6.4) | NOF + `dcad_account` still null | `grantor` name | `owner_index` | ~10 of 11 (filters Liable boilerplate) |
| **Path C** (modified Stage 6.5) | `dcad_account` still null + has snippet/legal-desc | `raw_excerpt` subdivision/lot/block | `legal_index` (fuzzy) | ~5-10 of 11 |
| Stage 6.6 (new) | always, after all paths | per-record agreement audit | confidence labels + warnings | applies to all records |

**Each path runs in sequence.** If an earlier path stamps `dcad_account`, later paths still execute *for the purpose of cross-path verification* (Stage 6.6) but their results don't change `dcad_account` once set. Their attempts are recorded in `resolution_history`.

### Why this ordering

1. **Path B first** — when address extraction was wrong (Class 1, 24, 25), the doc itself often contains the correct address in `raw_excerpt`. Cheapest fix; just an alternate field lookup.
2. **Path A second** — when grantor name is clean, it's a strong identifier. But it has known FP modes (Class 19) so we want Path B's cleaner signal first when available.
3. **Path C last** — fuzzy subdivision matching is the broadest net but has the highest FP risk. Run after stronger paths.

---

## 3. `signal_metadata.resolution_history` Schema

Every record gets a new field `signal_metadata.resolution_history` containing the full audit trail. Existing fields like `decedent_owned_properties`, `applicant_mailing`, etc. remain unchanged.

### Field shape

```python
signal_metadata = {
    # ... existing fields unchanged ...

    "resolution_history": [
        # one entry per resolution attempt, in execution order
        {
            "path": str,                # "stage_7_address_index" | "path_b_raw_excerpt" |
                                        # "path_a_grantor_owner_index" | "path_c_legal_resolver" |
                                        # "path_c_apn"
            "stage": str,               # "6.3" | "6.4" | "6.5" | "7" — for grep-ability in logs
            "input": str | None,        # what was queried (e.g. "OUSLEY SKYLER", "4314 HAMILTON")
            "status": str,              # "matched" | "no_match" | "skipped" | "multi_match" |
                                        # "guarded" | "error"
            "skip_reason": str | None,  # if status="skipped": "already_resolved" | "no_input" | "guard_..."
            "tier": str | None,         # Path A/C: "exact" | "no_middle" | "initial_form" |
                                        # "double_initial" | "surname_only" | "fuzzy_subdivision"
            "dcad_account": str | None, # primary account result (None on miss)
            "alternates": list[str],    # other candidate accounts in multi_match cases
            "warnings": list[str],      # path-specific concerns: "common_name_pollution", etc.
        },
    ],

    "primary_resolution": str | None,   # which path's dcad_account became the final one,
                                        # or None if no path succeeded
    "confidence": str,                  # "high" | "medium" | "low" | "none"
    "confidence_warnings": list[str],   # see §8 for the canonical warning list
}
```

### Confidence levels

| Level | Stamping rule |
|---|---|
| `high` | 2+ paths produced the SAME `dcad_account` |
| `medium` | exactly 1 path produced a clean match (exact tier, single account) |
| `low` | match found but with loose tier (`no_middle`, `initial_form`, `double_initial`, `surname_only`, `fuzzy_subdivision`), multi-account, or sanity-check warning |
| `none` | no path produced a `dcad_account` |

### `confidence_warnings` canonical list

| Warning | When it fires |
|---|---|
| `multi_account` | Path A/C matched a name to ≥2 DCAD accounts; pipeline picked first |
| `surname_only_tier` | Path A matched via surname-only tier (riskiest tier) |
| `common_name_pollution` | Match returned >3 accounts (existing warning, preserved) |
| `surname_drift` | NOF grantor surname ≠ dcad_owner surname after match |
| `low_market_value` | DCAD market_value < $50k AND grantor is a person (Class 25 signal) |
| `surname_in_trust_first_name` | Path A surname matched against a first-name-position token inside a trust string |
| `path_disagreement` | Two paths produced DIFFERENT dcad_accounts |
| `path_b_used_alternate` | Path B retried with a raw_excerpt address that differed from address_normalized |
| `fuzzy_subdivision_match` | Path C matched via fuzzy subdivision (one of: typo-tolerance, token-prefix) |

---

## 4. Stage 6.3 — Path B (raw_excerpt clean-address fallback)

### Purpose

Recover records where `address_normalized` is set to a corrupted, city-only, OCR-garbled, or known-venue/trustee value — but `raw_excerpt` contains a clean street address that wasn't picked up by `ADDRESS_PATTERNS`.

Addresses Classes 1, 8, 24, 25.

### Trigger condition

Record qualifies for Path B retry when ALL of:
- `dcad_account is None` (Stage 7 didn't already resolve)
- `raw_excerpt is not None and not ""`
- Either:
  - `address_normalized is None`, OR
  - `address_normalized` matches a suspect signature (city-only, OCR-garble, venue/trustee — see below)

### Suspect address signatures

```python
# city-only (no street number)
_first_seg = address_normalized.split(",")[0].strip()
is_city_only = (not _first_seg) or not any(c.isdigit() for c in _first_seg)

# OCR-garbled
ocr_garble_re = re.compile(r"[{}@#%]|\b[A-Z]{1,2}\s+[A-Z]\s+[A-Z]{1,2}\b")
is_ocr_garbled = bool(ocr_garble_re.search(address_normalized))

# Known venue/trustee signatures (rejected by Path B + flagged elsewhere)
VENUE_TRUSTEE_SIGS = [
    "600 COMMERCE",          # George Allen Courts Building (auction venue)
    "GEORGE ALLEN",
    "20405 STATE HWY",       # Codilis & Moody Houston
    "20405 STATE HIGHWAY",
    "15851 N DALLAS PKWY",   # Addison law firm
    "15851 N. DALLAS PARKWAY",
    "7730 MARKET CENTER",    # El Paso trustee
    "17100 GILLETTE",        # ServiceLink Irvine CA
    "5204 VILLAGE CREEK",    # Veracity Inc Plano
    "14800 LANDMARK",        # Addison law firm
]
is_venue_trustee = any(sig in address_normalized.upper() for sig in VENUE_TRUSTEE_SIGS)

# trigger Path B
should_run_path_b = (dcad_account is None) and raw_excerpt and (
    address_normalized is None
    or is_city_only
    or is_ocr_garbled
    or is_venue_trustee
)
```

### Algorithm

```python
def path_b_extract_address(raw_excerpt: str) -> Optional[str]:
    """Find a clean Dallas-county street address in raw_excerpt.
    Returns the address string suitable for address_index lookup, or None."""

    # Pattern: NNN STREET-NAME STREET-TYPE, CITY, (TX|TEXAS)? ZIP
    pattern = re.compile(
        r"(\d{2,5}\s+[A-Z][A-Z\s\.'-]{2,40}?\s+"
        r"(?:STREET|DRIVE|DR|ST|AVENUE|AVE|ROAD|RD|LANE|LN|BOULEVARD|BLVD|"
        r"COURT|CT|CIRCLE|CIR|PLACE|PL|WAY|TRAIL|TRL|PARKWAY|PKWY|"
        r"HIGHWAY|HWY|TERRACE|TER))\s*,?\s+"
        r"([A-Z][A-Z\s]+?)\s*,?\s*(?:TEXAS|TX)?\s*,?\s*(\d{5})",
        re.I,
    )

    for m in pattern.finditer(raw_excerpt):
        candidate = m.group(0)
        candidate_upper = candidate.upper()

        # GUARD: reject venue/trustee signatures
        if any(sig in candidate_upper for sig in VENUE_TRUSTEE_SIGS):
            continue

        return candidate

    return None


def stage_6_3_path_b(records, address_index):
    """Stage 6.3 — Path B retry on records with suspect address_normalized."""

    for rec in records:
        if not should_run_path_b(rec):
            continue

        candidate_addr = path_b_extract_address(rec.get("raw_excerpt") or "")

        history_entry = {
            "path": "path_b_raw_excerpt",
            "stage": "6.3",
            "input": candidate_addr,
            "status": "no_match",
            "skip_reason": None,
            "tier": None,
            "dcad_account": None,
            "alternates": [],
            "warnings": [],
        }

        if not candidate_addr:
            history_entry["status"] = "skipped"
            history_entry["skip_reason"] = "no_clean_address_in_raw_excerpt"
        else:
            norm = normalize_address(candidate_addr)
            acct = address_index.get(norm)
            if acct:
                history_entry["status"] = "matched"
                history_entry["dcad_account"] = acct
                # Stamp on record only if previously empty
                rec["address_normalized"] = norm
                rec["dcad_account"] = acct
                rec.setdefault("signal_metadata", {})["path_b_used_alternate"] = True

        rec.setdefault("signal_metadata", {}).setdefault("resolution_history", []).append(history_entry)
```

### Guards summary

| Guard | What it prevents |
|---|---|
| Venue/trustee signature reject | Picking up auction venue (600 Commerce) or trustee office address from raw_excerpt |
| Pattern must include zip | Reduces false positives on city-name-only matches |
| Only fires when address_normalized is suspect | Doesn't overwrite a clean address that just happens to not match DCAD (different problem) |

### Structured log format

```
[6.3/N] Path B (raw_excerpt fallback) starting on N candidate records
[6.3/N] record_id=XXX raw_excerpt_addr='4314 HAMILTON, DALLAS, TX, 75228' -> matched acct=00000123...
[6.3/N] record_id=YYY raw_excerpt_addr=None -> skipped (no_clean_address_in_raw_excerpt)
[6.3/N] record_id=ZZZ raw_excerpt_addr='600 Commerce Street' -> rejected (venue_signature)
[6.3/N] Path B summary: N candidates, M matched, K rejected (venue/trustee), L unmatched
```

---

## 5. Stage 6.4 — Path A (NOF grantor → owner_index)

### Purpose

Recover NOF records by looking up the foreclosed grantor's name in DCAD's owner_index — the same machinery PB records currently use via `match_decedent_to_dcad()`.

Addresses Classes 1 (alternative path), 4 (extends owner-index use), and provides confidence-check capability against Bucket A.

### Trigger condition

Record qualifies for Path A when ALL of:
- `dallas_code == "NOF"` (extended later: could also apply to PB records sourced from publicsearch — Class 4)
- `dcad_account is None` (after Stage 6.3)
- `grantor` is populated and is NOT in the boilerplate blocklist

### Grantor boilerplate blocklist

```python
GRANTOR_BOILERPLATE_BLOCKLIST = {
    # single tokens that frequently leak from OCR boilerplate
    "LIABLE",
    "MORTGAGOR",
    "BORROWER",
    "TRUSTEE",
    "DEBTOR",
    "GRANTOR",
    "EXECUTOR",
    "ATTORNEY",
}

def is_boilerplate_grantor(grantor: str) -> bool:
    if not grantor:
        return True
    g_clean = grantor.strip().upper()
    # Single-token boilerplate
    if g_clean in GRANTOR_BOILERPLATE_BLOCKLIST:
        return True
    # Multi-token but first non-comma token is boilerplate
    tokens = re.split(r'[\s,]+', g_clean)
    if tokens and tokens[0] in GRANTOR_BOILERPLATE_BLOCKLIST:
        return True
    return False
```

### Algorithm

```python
def stage_6_4_path_a(records, owner_index, account_address_index):
    """Stage 6.4 — Path A: NOF grantor → DCAD owner_index lookup."""

    for rec in records:
        if rec.get("dallas_code") != "NOF":
            continue
        if rec.get("dcad_account"):
            # Already resolved — but RUN PATH A ANYWAY for cross-path verification
            run_for_verification = True
        else:
            run_for_verification = False

        grantor = rec.get("grantor") or ""

        history_entry = {
            "path": "path_a_grantor_owner_index",
            "stage": "6.4",
            "input": grantor,
            "status": "no_match",
            "skip_reason": None,
            "tier": None,
            "dcad_account": None,
            "alternates": [],
            "warnings": [],
        }

        if is_boilerplate_grantor(grantor):
            history_entry["status"] = "skipped"
            history_entry["skip_reason"] = "boilerplate_grantor"
            _append_history(rec, history_entry)
            continue

        match = match_decedent_to_dcad(grantor, owner_index)

        if match is None:
            _append_history(rec, history_entry)
            continue

        # Path A match obtained. Apply guards:

        # GUARD: surname-only matched first-name-in-trust (Class 19a)
        if match.tier == "surname_only" and len(match.accounts) >= 1:
            # Check if the matched DCAD owner is a TRUST/ENTITY where the
            # surname appears MID-string (not as the first token).
            first_acct_addr = (account_address_index.get(match.accounts[0]) or {})
            # We need to look up the DCAD owner string for this account.
            # If the surname appears after "& " or after a first-name token, guard.
            dcad_owner = _get_dcad_owner(match.accounts[0], dcad_tables)
            if dcad_owner and _is_trust_first_name_pattern(grantor, dcad_owner):
                history_entry["status"] = "guarded"
                history_entry["skip_reason"] = "surname_in_trust_first_name"
                history_entry["alternates"] = match.accounts
                _append_history(rec, history_entry)
                continue

        # Match passes guards. Stamp it (or compare if already resolved).
        history_entry["status"] = "matched" if len(match.accounts) == 1 else "multi_match"
        history_entry["tier"] = match.tier
        history_entry["dcad_account"] = match.accounts[0]
        history_entry["alternates"] = match.accounts[1:]

        # Warnings on this entry
        if len(match.accounts) > 1:
            history_entry["warnings"].append("multi_account")
        if match.tier == "surname_only":
            history_entry["warnings"].append("surname_only_tier")
        if match.warning == "common_name_pollution":
            history_entry["warnings"].append("common_name_pollution")

        if run_for_verification:
            # Don't overwrite; let Stage 6.6 do the agreement check
            pass
        elif rec.get("dcad_account") is None:
            # Path A is the first to find a match → stamp
            rec["dcad_account"] = match.accounts[0]
            # Derive address_normalized from the matched account
            acct_addr = (account_address_index.get(match.accounts[0]) or {}).get("address_normalized")
            if acct_addr:
                rec["address_normalized"] = acct_addr

        _append_history(rec, history_entry)


def _is_trust_first_name_pattern(grantor: str, dcad_owner: str) -> bool:
    """Detect Class 19a false-positive pattern.

    Example: grantor='Heather, Linda A.' (surname=HEATHER)
             dcad_owner='Bass Jeffery & Heather Revocable Trust The'
             The surname 'HEATHER' is the FIRST NAME of a trust grantor,
             not the matched person's last name.
    """
    if "," in grantor:
        grantor_surname = grantor.split(",")[0].strip().upper()
    else:
        grantor_surname = ""
    if not grantor_surname:
        return False

    owner_upper = dcad_owner.upper()
    if grantor_surname not in owner_upper:
        return False

    # Trust/entity indicator
    has_trust = any(kw in owner_upper for kw in ["TRUST", "LLC", "INC", "CORP", "LP"])
    if not has_trust:
        return False

    # Position of the surname in the owner string
    idx = owner_upper.index(grantor_surname)
    before = owner_upper[:idx].strip()

    # If surname comes after "& " or after another name token,
    # it's likely a first-name-position match in the trust.
    if "&" in before or (before and not any(c in before for c in ".,")):
        if len(before) > 3:  # there's substantive text before the surname
            return True

    return False
```

### Multi-account first-pick policy

When the grantor name matches multiple DCAD accounts:

- **`alternates`** in history_entry carries all other candidate accounts
- **`dcad_account`** gets the first account (existing behavior, preserved for backward compat)
- **`confidence_warnings`** on the record includes `multi_account`
- **Dashboard** renders the multi_account badge so operator can review alternates
- The list `signal_metadata.alternate_accounts` will hold the full list for operator UI

### Structured log format

```
[6.4/N] Path A (grantor->owner_index) starting on N candidate records
[6.4/N] grantor='OUSLEY SKYLER' tier=exact accounts=1 -> matched acct=00000555358000000
[6.4/N] grantor='WILSON ROBERT' tier=exact accounts=8 -> multi_match first=acct=22137400... warn=multi_account,common_name_pollution
[6.4/N] grantor='HEATHER LINDA A' tier=surname_only accounts=1 -> guarded (surname_in_trust_first_name)
[6.4/N] grantor='LIABLE' -> skipped (boilerplate_grantor)
[6.4/N] grantor='WALKER DAVID & WALKER LINDA E' tier=exact accounts=0 -> no_match
[6.4/N] Path A summary: N candidates, M matched (k high-conf, j multi-conf), L no_match, G guarded
```

---

## 6. Stage 6.5 — Path C (resolver skip-fix + fuzzy subdivision)

### Purpose

Two changes to existing `legal_resolver`:
1. **Skip-condition fix**: change `if rec.get("address_normalized"): continue` to `if rec.get("dcad_account"): continue`. Allows the resolver to fire when address_normalized is set but DCAD lookup failed (Class 6).
2. **Fuzzy subdivision matching**: when an exact subdivision lookup fails, attempt fuzzy matching to handle OCR garbles (BEAR TREK → BEAR CREEK).

### Skip-condition fix

```python
# scraper/legal_resolver.py (resolve_legal_descriptions and resolve_apn_to_address)

# OLD:
# if rec.get("address_normalized"):
#     continue

# NEW:
if rec.get("dcad_account"):
    continue
```

This unblocks the resolver for records where address_normalized is city-only, OCR-garbled, or a venue/trustee address.

### Fuzzy subdivision matching

The fuzzy matcher should be **conservative**: prefer no_match over wrong_match.

```python
import difflib

def fuzzy_subdivision_match(target_subdivision: str,
                            legal_index: dict,
                            lot: str,
                            block: str,
                            *,
                            similarity_threshold: float = 0.85) -> Optional[str]:
    """Try to find a DCAD subdivision that closely matches the (possibly
    OCR-garbled) target, when the exact lookup failed.

    Returns the DCAD account on a confident fuzzy match, None otherwise.

    Conservative strategy:
      - Only attempts when (lot, block) provides additional constraint
      - Requires similarity ratio >= 0.85
      - Requires the (sub, lot, block) match returns EXACTLY 1 account
      - Token-prefix match also considered (e.g., 'BEAR TREK' vs 'BEAR CREEK'
        share 'BEAR ' token prefix and 5+ char overlap)
    """
    target_norm = normalize_subdivision(target_subdivision)
    if not target_norm or not lot or not block:
        return None

    # Get all subdivisions that have an account at (sub, lot, block) — i.e.,
    # those subdivisions where (sub, lot, block) → exactly one DCAD account.
    candidate_subs = set()
    for (sub, l, b), accounts in legal_index.items():
        if l == lot and b == block and len(accounts) == 1:
            candidate_subs.add(sub)

    if not candidate_subs:
        return None

    # Score each candidate by similarity
    scored = []
    for sub in candidate_subs:
        ratio = difflib.SequenceMatcher(None, target_norm, sub).ratio()
        if ratio >= similarity_threshold:
            scored.append((ratio, sub))

    # If exactly one fuzzy candidate above threshold, use it
    if len(scored) == 1:
        ratio, sub = scored[0]
        return legal_index[(sub, lot, block)][0]

    return None
```

### Failure-mode coverage

| Case | Fuzzy resolves? | Why |
|---|---|---|
| BEAR TREK vs BEAR CREEK | ✓ Yes | Token prefix + similarity ≥ 0.85, lot+block additional constraint |
| SANDALWOGR vs SANDALWOOD | ✓ Yes | High similarity ratio |
| VOUGHT MANOR ADDITI ECTION vs VOUGHT MANOR ADDITION SECTION | ✓ Yes | Tokens align, similarity high |
| BEAUMONT vs BEUMONT (1-char typo) | ✓ Yes | High similarity |
| Completely garbled (single-char per token, etc.) | ✗ No | Below threshold; return None |
| Multiple equally-close fuzzy candidates | ✗ No | Returns None (conservative; no first-pick) |

### Structured log format

```
[6.5/N] Path C (legal_resolver) starting on N candidate records
[6.5/N] record_id=XXX subdivision='PLEASANTWOOD ADDITION' lot=8 block=9/6262 -> matched acct=00000555... (tier=exact)
[6.5/N] record_id=YYY subdivision='BEAR TREK RANCH-PHASE 4' lot=12 block=B -> fuzzy_matched 'BEAR CREEK RANCH-PHASE 4' (ratio=0.91)
[6.5/N] record_id=ZZZ subdivision='RANDOM GARBLE' -> no_match (exact)+ no_match (fuzzy)
[6.5/N] Path C summary: N candidates, M matched-exact, K matched-fuzzy, L no_match
```

---

## 7. Stage 6.6 — Cross-Path Agreement + Confidence Stamping

### Purpose

After all paths run, audit each record's `resolution_history` to determine:
- Which paths produced matches?
- Did multiple paths produce the SAME `dcad_account` (agreement)?
- Did paths produce DIFFERENT accounts (disagreement)?

Then stamp `confidence` and any `confidence_warnings` accordingly.

### Algorithm

```python
def stage_6_6_confidence_audit(records):
    for rec in records:
        history = rec.get("signal_metadata", {}).get("resolution_history", [])

        # Collect all distinct dcad_accounts produced by any successful path
        matched_paths = [h for h in history if h["status"] in ("matched", "multi_match")]
        accounts_produced = set()
        for h in matched_paths:
            if h["dcad_account"]:
                accounts_produced.add(h["dcad_account"])

        sm = rec.setdefault("signal_metadata", {})

        if not matched_paths or not accounts_produced:
            sm["confidence"] = "none"
            sm["primary_resolution"] = None
            continue

        # Determine primary_resolution (which path's account is stamped)
        primary_account = rec.get("dcad_account")
        for h in matched_paths:
            if h["dcad_account"] == primary_account:
                sm["primary_resolution"] = h["path"]
                break

        warnings = list(sm.get("confidence_warnings", []))

        if len(accounts_produced) >= 2:
            # DISAGREEMENT
            warnings.append("path_disagreement")
            sm["confidence"] = "low"
            sm["confidence_warnings"] = warnings
            continue

        # Single account produced. Check tiers.
        # If primary path is exact tier AND no path-level warnings → high or medium
        primary_history = next((h for h in matched_paths
                                if h["dcad_account"] == primary_account), None)

        if not primary_history:
            sm["confidence"] = "low"
            continue

        # Aggregate warnings from the primary history entry
        for w in primary_history["warnings"]:
            if w not in warnings:
                warnings.append(w)

        # Sanity-check warnings (see §8)
        sanity_warnings = _sanity_check_warnings(rec)
        for w in sanity_warnings:
            if w not in warnings:
                warnings.append(w)

        # Confidence classification
        if len(matched_paths) >= 2 and len(accounts_produced) == 1:
            # MULTI-PATH AGREEMENT
            sm["confidence"] = "high"
        elif (primary_history["tier"] in (None, "exact") and not warnings):
            sm["confidence"] = "medium"
        else:
            sm["confidence"] = "low"

        sm["confidence_warnings"] = warnings
```

### Cross-path agreement examples

For the Ousley Skyler record (315562561):
- Path A: `OUSLEY SKYLER` → tier=exact → acct=00000555358000000
- Path C: `PLEASANTWOOD Lot 8 Block 9/6262` → tier=exact → acct=00000555358000000
- Both produced the SAME account → **confidence=high**

For the LAPRENSA GRANT record (315562554, today "Liable"):
- Path A: skipped (boilerplate)
- Path C: `WALNUT CREEK ESTATES Lot 16 Block E/8443` → tier=exact → acct=X
- Single-path match, exact tier → **confidence=medium**

For Wilson, Robert (PB, currently):
- Path A: tier=exact, 8 accounts, picks first
- Path C: (none, no legal-desc in raw_excerpt for PB)
- Single-path match BUT multi_account warning → **confidence=low**

---

## 8. Sanity-Check Additions

These run AFTER `dcad_account` is stamped (in Stage 7 or earlier paths). They produce `confidence_warnings` that downgrade confidence labels.

### Sanity-check warning generators

```python
def _sanity_check_warnings(rec) -> list[str]:
    warnings = []

    # Low market value (Class 25 signal)
    mv = rec.get("dcad_market_value")
    if isinstance(mv, (int, float)) and mv > 0 and mv < 50000:
        warnings.append("low_market_value")

    # Surname drift (NOF grantor surname ≠ dcad_owner surname)
    if rec.get("dallas_code") == "NOF":
        g = rec.get("grantor") or ""
        o = rec.get("dcad_owner") or ""
        if g and o and "," in g and "," in o:
            g_sur = g.split(",")[0].strip().upper()
            o_sur = o.split(",")[0].strip().upper()
            if g_sur and o_sur and g_sur not in o_sur and o_sur not in g_sur:
                warnings.append("surname_drift")

    # Address looks like a venue/trustee even after all paths ran
    addr = (rec.get("address_normalized") or "").upper()
    if any(sig in addr for sig in VENUE_TRUSTEE_SIGS):
        warnings.append("venue_or_trustee_address")

    return warnings
```

### Confidence-downgrade interactions

| Warning | Effect on confidence |
|---|---|
| `multi_account` | Caps at `low` if was `medium`; otherwise unchanged |
| `surname_only_tier` | Caps at `low` |
| `common_name_pollution` | Caps at `low` |
| `surname_drift` | Caps at `low` (high-signal: recent sale OR wrong match) |
| `low_market_value` | Caps at `low` (Class 25 signal) |
| `surname_in_trust_first_name` | Match is GUARDED (no stamp); not stamped as low — match doesn't proceed |
| `path_disagreement` | Confidence=low forced |
| `path_b_used_alternate` | Informational only; doesn't change confidence |
| `fuzzy_subdivision_match` | Caps at `medium` (still single-path) |
| `venue_or_trustee_address` | Caps at `low` |

---

## 9. Dashboard Implications

### New columns / badges

1. **Confidence column** — colored badge: `high` (green), `medium` (yellow), `low` (orange), `none` (gray)
2. **Warnings chip** — small chips for each confidence_warning. Click for tooltip explanation.
3. **Resolution path indicator** — small icon showing which path won (address-index = home icon, owner_index = person icon, legal_resolver = blueprint icon, raw_excerpt = magnifying glass)

### New filter

`Hide low-confidence` — checkbox, off by default. When on, hides records with `confidence in ("low", "none")`.

### Detail panel additions

When operator opens a record, the detail panel shows:
- **Resolution audit trail**: scrollable list of every path attempted, with status + tier
- **Alternate accounts**: when `alternates` populated, shows the OTHER candidate properties the system found but didn't pick. Each row has "verify this instead" button (operator workflow not in scope for this design).

### Updated COMMERCIAL_TOKENS rationale

The existing `hideCommercial` filter (Class 23) hides 11% of records by default — disproportionately probate-trust records. Recommendation in this design:
- Move `TRUST` out of `COMMERCIAL_TOKENS` (trust-owned probate properties are HIGH-VALUE, not commercial)
- Keep entity tokens (LLC, INC, CORP, etc.) — those are genuine commercial entities
- Provide separate filter `hideTrustOwned` (default off) so operator can opt-in

---

## 10. CSV Export Implications

### New columns

| Column name | Source | Notes |
|---|---|---|
| `Confidence` | `signal_metadata.confidence` | High/Medium/Low/None |
| `Confidence Warnings` | `signal_metadata.confidence_warnings` joined with `;` | For CRM rule-based routing |
| `Resolution Path` | `signal_metadata.primary_resolution` | Human-readable: "Address Index" / "Owner Index" / "Legal Description" / "Raw Excerpt Fallback" |
| `Alternate Accounts` | `signal_metadata.alternate_accounts` (when populated) | Comma-separated; for CRM-side dedup |

### Updated existing columns

- `Address`, `City`, `State`, `Zip` — derived from the FINAL `dcad_account`'s DCAD address, not the original OCR-extracted text. This ensures CRM mail goes to the right place even when the original OCR misclassified.
- (Class 17, 18 fixes are out of scope here but downstream of confidence design)

### CSV row example (proposed schema)

```csv
First Name,Last Name,...,Address,City,State,Zip,Confidence,Confidence Warnings,Resolution Path,Alternate Accounts,...
Skyler,Ousley,...,6840 DART AVE,DALLAS,TX,75217,high,,Owner Index,,...
Robert,Wilson,...,355 CARDINAL CREEK DR,DALLAS,TX,75220,low,multi_account;common_name_pollution,Owner Index,"3613 BERMUDA DR;4216 BEAVER BROOK PL;...",...
```

---

## 11. Test Design

### Path B tests (`tests/test_path_b_raw_excerpt.py`)

| Test case | Setup | Expected |
|---|---|---|
| Recovers clean address from raw_excerpt | record with address_normalized=garbled, raw_excerpt has '4314 HAMILTON, DALLAS, TX, 75228' | dcad_account stamped |
| Rejects venue address | raw_excerpt has '600 Commerce Street' | skipped (status=skipped, reason=venue_signature) |
| Rejects trustee address | raw_excerpt has '20405 State Highway, Houston' | skipped |
| Skips when address_normalized is clean | record with valid Dallas-county address | skipped (status=skipped, reason=address_already_clean) |
| Doesn't fire on already-resolved | dcad_account set | skipped (status=skipped, reason=already_resolved) |
| Logs miss when raw_excerpt has no address | raw_excerpt='Subdivision: PLEASANTWOOD' | history entry with status=skipped, reason=no_clean_address_in_raw_excerpt |

### Path A tests (`tests/test_path_a_grantor_owner_index.py`)

| Test case | Setup | Expected |
|---|---|---|
| Exact-tier single match | grantor='Ousley, Skyler', owner_index has 1 OUSLEY SKYLER | matched, tier=exact, account stamped |
| Exact-tier multi-account | grantor='Wilson, Robert', owner_index has 8 WILSON ROBERT | multi_match, alternates populated, multi_account warning |
| Boilerplate grantor skipped | grantor='Liable' | skipped (reason=boilerplate_grantor) |
| Surname-only first-name-in-trust guard | grantor='Heather, Linda A.', dcad_owner='Bass Jeffery & Heather Revocable Trust The' | guarded (skip_reason=surname_in_trust_first_name) |
| No match returns no_match | grantor='Nobody, Doesnt Exist' | status=no_match, account=None |
| Already-resolved verification mode | dcad_account set | runs but doesn't overwrite; logs history |

### Path C tests (`tests/test_legal_resolver.py` — add to existing)

| Test case | Setup | Expected |
|---|---|---|
| Skip-condition uses dcad_account not address_normalized | record with address_normalized='DALLAS', dcad_account=None | resolver fires |
| Fuzzy subdivision matches typo | target='BEAR TREK RANCH-PHASE 4', legal_index has 'BEAR CREEK RANCH-PHASE 4' at same (lot,block) | matched-fuzzy with ratio≥0.85 |
| Fuzzy rejects multiple equal candidates | target garble matches two subs at threshold | no_match (conservative) |
| Fuzzy rejects below threshold | similarity ratio 0.6 | no_match |

### Stage 6.6 tests (`tests/test_confidence_audit.py`)

| Test case | Setup | Expected confidence |
|---|---|---|
| Multi-path agreement (Path A + Path C same account) | history has 2 matched entries with same dcad_account | high |
| Single-path exact tier | one matched entry, tier=exact, no warnings | medium |
| Single-path with multi_account | one matched entry with multi_account warning | low |
| Multi-path disagreement | two matched entries with different accounts | low + path_disagreement warning |
| Low market value triggers warning | dcad_market_value=$490 | low + low_market_value warning |
| No paths matched | no successful history entries | none |

---

## 12. Expected Operational Outcomes

Projection against today's 91-record set (post-implementation):

| Metric | Today | Post-design (projected) |
|---|---|---|
| NOF address-extraction count | 21/38 = 55% | ~30/38 = 79% (Path B + A + C recovery) |
| Records with `dcad_account` | 55/91 = 60% | ~75/91 = 82% |
| Records flagged `confidence=high` | n/a | ~25 (multi-path agreement) |
| Records flagged `confidence=medium` | n/a | ~40 (single clean match) |
| Records flagged `confidence=low` | n/a | ~10 (loose tier or warnings) |
| Records flagged `confidence=none` | n/a | ~15 (truly unrecoverable) |
| **Wrong-person-call risk** (forensic estimate) | ~5 records | ~1-2 records (Heather/Bass case → guarded; Wilson Robert → low_confidence badge for operator review) |
| Operator-required manual cleanup | unknown (high) | clearly badged via warnings list |

### Acceptable wrong-person-call rate

The new system aims for: **wrong-person calls drop from ~5%+ to <2%, with clear visibility on the remaining cases via low-confidence badges.**

Zero wrong-person calls is impossible without sacrificing recovery (e.g., refusing all loose-tier matches). The design accepts ~2% with visibility, rather than ~5%+ with no signal.

---

## 13. Open Questions

These need operator-side decisions before code lands:

1. **Path C fuzzy subdivision threshold**: 0.85 is conservative. Should it be 0.80? Empirical testing against today's BEAR TREK / SANDALWOGR cases would calibrate.

2. **`alternate_accounts` UI surface**: should the dashboard show all alternates inline, behind a "show alternates" toggle, or only in the detail panel?

3. **CRM/CSV semantics for low_confidence records**: should low_confidence records be exported to CRM at all, or held back for operator review first?

4. **Resolution_history retention**: every record carries the full attempt history. After 3+ paths × 91 records that's ~270 history entries per run. records.json grows. Consider whether to retain in records.json forever or summarize after N days.

5. **Backwards compatibility**: existing records.json doesn't have `resolution_history`. Migration strategy: lazy — populate when records are re-processed; absent on legacy records is fine.

6. **Class 11 (rolling-window amnesia) coupling**: this design doesn't fix Class 11, but Class 11 means the confidence_warnings on a record vanish when the record falls off the window. The operator never sees the warning context after Day 7. Should the dashboard preserve high-signal records (those with confidence=low) past the window even if pipeline regenerates them?

7. **Existing Path A for PB records**: today's `match_decedent_to_dcad` runs against `decedent_name` for PB records but doesn't carry the same provenance/confidence framework. Should the PB path be retrofitted to write `resolution_history` and `confidence` too? (Recommended yes, for consistency.)

---

## 14. Decisions Resolving Open Questions

All §13 open questions resolved 2026-05-27. These supersede the open-question text above and lock the design for implementation.

| # | Question | Decision | Rationale |
|---|---|---|---|
| Q1 | Path C fuzzy subdivision threshold | **0.85** | Catches OCR garbles (BEAR TREK ↔ BEAR CREEK) while keeping false-positive surface small. Recalibratable after first production run. |
| Q2 | Where `alternate_accounts` surfaces in UI | **Detail panel only (initially)** | Keeps main table uncluttered for the common single-account case. Inline-toggle UI can be added later if operator needs it. |
| Q3 | CSV/CRM semantics for `low_confidence` records | **Export with badging** (Confidence + Confidence Warnings columns) | Operator filters/prioritizes in their CRM. Holding records back creates a hidden review queue that may never get worked. |
| Q4 | `resolution_history` retention | **Keep forever in records.json** | ~50 KB/run growth is acceptable. Audit value of full provenance outweighs storage cost. Class 11 amnesia limits accumulation anyway. |
| Q5 | Backwards-compatibility migration | **Lazy migration** | Next pipeline run touches every active record and stamps the new fields. Legacy records treated as `confidence: none` (no provenance known). No script needed. |
| Q6 | Class 11 (rolling-window amnesia) coupling | **Defer** — document the coupling but don't special-case retention | Preserving low-confidence records past the window is a patch, not a fix. Class 11 is separate architectural work. Operator reviews and dispositions records within the 7-day window. |
| Q7 | PB record retrofit to new framework | **Yes, retrofit** | Unifying NOF + PB under `resolution_history` + `confidence` provides consistent dashboard rendering and automatically covers Class 4 (publicsearch-sourced PB records bypass). Modest extra work in the PR series. |

## 15. Implementation Sequence

Based on the decisions above, the implementation order is:

1. **PR 1 — Schema scaffolding**: add `resolution_history`, `confidence`, `confidence_warnings`, `primary_resolution`, `alternate_accounts` to `signal_metadata`. Lazy population. No new resolver logic yet.
2. **PR 2 — Stage 6.3 (Path B)**: raw_excerpt clean-address fallback with venue/trustee guards.
3. **PR 3 — Stage 6.4 (Path A for NOFs)**: NOF grantor → owner_index with boilerplate blocklist and Class-19a `surname_in_trust_first_name` guard.
4. **PR 4 — Stage 6.5 (Path C fix + fuzzy)**: skip-condition fix from `address_normalized` to `dcad_account` + fuzzy subdivision matcher at threshold 0.85.
5. **PR 5 — Stage 6.6 (Confidence audit + sanity checks)**: cross-path agreement, `low_market_value`, `surname_drift`, `venue_or_trustee_address` warnings.
6. **PR 6 — PB retrofit**: unify PB resolution into `resolution_history` + `confidence` framework. Covers Class 4 (publicsearch-PB bypass).
7. **PR 7 — Dashboard surface**: confidence badges, warnings chips, resolution-path indicator, alternates in detail panel. Also move `TRUST` out of `COMMERCIAL_TOKENS` (Class 23 mitigation).
8. **PR 8 — CSV export schema**: new `Confidence`, `Confidence Warnings`, `Resolution Path`, `Alternate Accounts` columns.

Each PR is independently shippable and auditable. The design intentionally lands schema first (PR 1) so subsequent PRs have somewhere to write provenance from the start.

Out-of-scope (separate work, not this design):
- Class 11 (rolling-window amnesia)
- Class 17 (CSV name-split bug)
- Class 18 (CSV address truncation)
- Class 21 (Tyler party-role completeness)
- Class 22 (attorney capture surfacing)

## CHANGE LOG

- **2026-05-27** — Initial design document. Captures operator decisions: single-path acceptable with confidence-low badging + skip-bug fix + fuzzy subdivision matching.
- **2026-05-27** — Added §14 (Decisions Resolving Open Questions) + §15 (Implementation Sequence). All §13 open questions resolved; design is locked for implementation.
