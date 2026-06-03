# NOF Data Quality Recon

**Date:** 2026-06-03
**Branch:** `claude/nof-data-quality-recon` (recon report only — no code changed)
**Scope:** Notice of Foreclosure (`dallas_code == "NOF"`) records only. Probate (PB) records were excluded from every count and example below.
**Sample:** the `data/records.json` snapshot from the **2026-05-29 daily scrape** (the first clean scrape after the embargo-flood fix landed). 92 total records → **48 NOF** (all `source == "publicsearch.us"`), **0 ambiguous** (every record had a `dallas_code`).
**Mode:** read-only scan. No files modified; no data cleaned or normalized; no fixes proposed in this document.

---

## TL;DR

The NOF dataset is mostly clean, with concentrated defects in a small, internally-consistent subset.

- **Property addresses:** 48/48 populated. 1 city-only, 1 with an OCR `©` glyph, 1 duplicated pair pointing at the auction venue (`600 COMMERCE ST`) instead of two distinct properties.
- **Mailing addresses:** 44/48 populated. The 4 blanks are exactly the 4 records with `dcad_account = None` (i.e. unresolved → no DCAD record to read mailing from). No record has a mailing address tied to multiple distinct properties.
- **Owners (`dcad_owner`):** 44/48 populated, otherwise clean. 3 entity owners (LLC / bank).
- **Grantors:** mostly OK but **systematically degraded by OCR/parse artifacts** — 9 records carry a stray short token before the name (e.g. `"G, Santry Michael"`), 4 records have a date fragment glued into the name (e.g. `"Folk, 10/3 1/2023 Raigine Tierra"`), and 1 carries a non-ASCII glyph.
- A naive `grantor` ↔ `dcad_owner` surname compare flags 39 "mismatches", but **most are just `grantor` formatting noise** (same person, comma-flipped or initial-stripped). After token-overlap filtering, only **8 are genuinely different names**, and 5 of those are the duplicate-`600 COMMERCE` pair + entity owners + the city-only Garland record.
- **The 4 unresolved records are a coherent set**: `316689877`, `316708049`, `316730746`, `316730731`. They account for all 4 missing owners, all 4 missing mailings, both of the missing/junky property addresses (`GARLAND`, `7321 VAA © DRIVE`), and 2 of the 2 grantor blanks.

---

## Field 1 — `property_address` (`address` / `address_normalized`)

| Check | Count | Example record IDs |
|---|---|---|
| Missing / empty | 0 / 48 | — |
| No street number in `address_normalized` | 1 | `316730731` (`"GARLAND"`) |
| Placeholder text (`UNKNOWN` / `N/A` / etc.) | 0 | — |
| Non-ASCII / `©§®` glyphs | 1 | `316708049` (`"7321 VAA © DRIVE DALLAS, TX 75227"`) |
| Duplicates across records | 1 pair | `316730723` + `316730734` — both `"600 COMMERCE ST"` (the George Allen Courts Building / auction venue, not the foreclosed property) |

## Field 2 — `mailing_address` (`dcad_mailing_address` + city / state / zip)

| Check | Count | Example record IDs |
|---|---|---|
| Mailing populated | 44 / 48 | — |
| Missing / empty | 4 | `316689877`, `316708049`, `316730746`, `316730731` (all `dcad_account = None`) |
| Street with no number | 0 | — |
| Populated mailing missing city or zip | 0 | — |
| Non-ASCII / junk glyphs | 0 | — |
| **One mailing tied to multiple distinct properties** | **0** | — |

### Property ↔ mailing comparison (44 records with both populated)

| Relationship | Count | Example record IDs |
|---|---|---|
| Exact match (mailing == property, single line) | 25 | `316708050`, `316708051`, `316731152`, `316731167`, ... |
| Cosmetic diff (same street + extra name line or formatting) | 10 | `315584731` (mailing has `"KING ELISE EST OF\n<same street>"`), `316730722`, `316730735`, ... |
| Genuine diff (different location) | 9 | `316712316` (property `3030 O'BANNON DR`, mailing `2626 DUNCANVILLE RD APT 1008`), `316731158`, `316730737`, ... |

## Field 3 — `grantor`

| Check | Count | Example record IDs |
|---|---|---|
| Missing / empty | 2 / 48 | `316689877`, `316730746` |
| Entity grantor (`EST OF` / `LLC` / `TRUST` / etc.) | 0 | — |
| Multiple grantors (`AND` / `&`) on one record | 4 | `316731171`, `316731167`, `316730732`, `316731157` |
| **Date fragment bled into the grantor name** | **4** | `316730722` (`"Hernandez, 12/16/2004 Rosa Alicia Adame & Roy"`), `316730723` (`"Carter, 5/4/2016 Tony Odell"`), `316730734` (`"Folk, 10/3 1/2023 Raigine Tierra"`), `316730731` (`"Sands, 4/15/2025 Lauren Janice"`) |
| Non-ASCII glyph | 1 | `316731163` (`"Lofton-Hunter, Weraedés"`) |

## Field 4 — `dcad_owner`

| Check | Count | Example record IDs |
|---|---|---|
| Missing / empty | 4 / 48 | `316689877`, `316708049`, `316730746`, `316730731` (same set as missing mailing, all `dcad_account = None`) |
| Entity owners | 3 | `316731155` (`"Gulf Coast Western LLC"`), `316731153` (`"Wells, Fargo Bank Na"`), `316731157` (`"Lone Star Lex Enterprises LLC"`) |
| Multiple owners on one record | 0 | — |
| Looks like an address / contains street tokens | 0 | — |
| Non-ASCII glyph | 0 | — |

### `grantor` vs `dcad_owner` presence

| State | Count | Records |
|---|---|---|
| Both present | 44 | — |
| `grantor` present, `dcad_owner` missing | 2 | `316708049`, `316730731` |
| `dcad_owner` present, `grantor` missing | 0 | — |
| Both missing | 2 | `316689877`, `316730746` |

### `grantor` vs `dcad_owner` name comparison (44 with both)

A raw surname check flags **39 / 44** as different. Filtering to records that share **zero** name tokens between `grantor` and `dcad_owner` (i.e. ignoring comma-flips and stripped initials):

- **Same party, `grantor` formatted differently:** 36. Examples: `315584731` (`"Jennifer, Ross"` vs `"Ross, Jennifer"`), `316712316` (`"\, Ronald Green"` vs `"Green, Ronald"`), `316731158` (`"G, Santry Michael"` vs `"Santry, Michael G"`).
- **Genuinely different name:** 8.
  - 3 are entity owners vs person grantor: `316731155` (Gulf Coast Western LLC), `316731153` (Wells Fargo Bank), `316731157` (Lone Star Lex Enterprises LLC).
  - 1 entity-ish owner: `316730732` grantor `"And, Chelsea Chiu"` vs owner `"South, Dallas Rentals"`.
  - 2 are the courthouse-venue pair, both resolving to the same wrong owner: `316730723` and `316730734` → `"Dimoulakis, Nick"`.
  - 2 unrelated person names: `316730750` (grantor `"Neal, Tony"` vs owner `"Rungpiti, Valinda"`), `316731163` (grantor `"Lofton-Hunter, Weraedés"` vs owner `"Bartlett, Jack"`).

---

## Cross-field oddities

1. **Grantor name formatting is systematically degraded.** Two recurring shapes, both visible in the data above:
   - **Stray short leading token before the name** — 9 records like `"G, Santry Michael"`, `"L, Taranetha"`, `"\, Ronald Green"`, `"F, Brown Ramona"`. Looks like a single letter / initial / stray character ended up in the surname slot.
   - **Date glued into the name** — the 4 cases listed under Field 3.

   The same records' `dcad_owner` strings are clean and correct, so the property/owner pipeline is fine; the artefact is specific to the OCR-derived `grantor` field.

2. **The `600 COMMERCE ST` cluster is internally self-consistent across all four fields.** `316730723` and `316730734` share the duplicate property address, share the same (wrong) DCAD parcel, share the same owner (`Dimoulakis, Nick`), and both show date-bled grantors. Coherent record-internally, but anchored to the auction venue rather than the actual foreclosed property.

3. **The 4 unresolved records are a clean, single set.** `316689877`, `316708049`, `316730746`, `316730731` are exactly the records with `dcad_account = None`, and they account for all 4 missing mailing addresses and all 4 missing owners. The property-address defects (city-only `GARLAND` on `316730731`, `©`-junk on `316708049`) and 2 of the 2 missing grantors (`316689877`, `316730746`) all fall inside this same set.

4. **No mailing address is shared across multiple distinct properties.** Worth noting because that's the pattern that would indicate landlords / property managers / a courthouse-style false anchor at scale; we don't see it here.

---

## Snapshot caveat

This report describes the **2026-05-29 records.json snapshot**. The latest `main` has since accumulated additional daily scrapes (through 2026-06-03 at the time of writing). Whether specific record IDs above still appear, and whether the same patterns recur in newer data, is a separate question that this recon does not address. The methodology below can be re-run against any later snapshot.

---

## Reproduction recipe

```bash
# 1. Fetch the snapshot the recon used (or substitute a different commit)
git show e40eb0a:data/records.json > /tmp/nof_recon.json
# OR pull current main:
# git show origin/main:data/records.json > /tmp/nof_recon.json

# 2. Run the per-field scans (pure-stdlib python)
python3 - <<'EOF'
import json, re
from collections import Counter
env = json.load(open('/tmp/nof_recon.json'))
records = env.get('records', env) if isinstance(env, dict) else env
nof = [r for r in records if r.get('dallas_code') == 'NOF']

# Field 1 — property address
missing_prop = [r for r in nof if not (r.get('address') or r.get('address_normalized'))]
city_only    = [r for r in nof if not re.search(r'\d', r.get('address_normalized') or r.get('address') or '')]
junk_prop    = [r for r in nof if re.search(r'[^\x00-\x7F]|[©§®]', r.get('address') or '')]
prop_counts  = Counter((r.get('address_normalized') or '').upper() for r in nof if r.get('address_normalized'))
dup_prop     = {k: v for k, v in prop_counts.items() if v > 1}

# Field 2 — mailing
has_mail = [r for r in nof if r.get('dcad_mailing_address')]
no_mail  = [r for r in nof if not r.get('dcad_mailing_address')]

def norm(s):
    return re.sub(r'[^A-Z0-9 ]', '', re.sub(r'\s+', ' ', (s or '').upper())).strip()

exact = cosmetic = genuine = 0
for r in [r for r in nof if r.get('address_normalized') and r.get('dcad_mailing_address')]:
    prop = norm(r['address_normalized'])
    mail_lines = [norm(x) for x in r['dcad_mailing_address'].split('\n')]
    mail_full  = norm(r['dcad_mailing_address'].replace('\n', ' '))
    if any(prop == m for m in mail_lines) and len(mail_lines) == 1:
        exact += 1
    elif any(prop == m for m in mail_lines) or prop in mail_full:
        cosmetic += 1
    else:
        genuine += 1

# Field 3 — grantor artefacts
date_bleed   = [r for r in nof if r.get('grantor') and re.search(r'\d{1,2}/\d', r['grantor'])]
stray_leader = [r for r in nof if r.get('grantor') and re.match(r'^\s*([A-Za-z]{1,2}|\\)[, ]', r['grantor'])]

# Field 4 — owner-vs-grantor token overlap
def tokens(s):
    return {t for t in re.findall(r'[A-Z]{2,}', (s or '').upper())
            if t not in {'AND', 'THE', 'PROVIDESTHAT', 'EST', 'OF'}}

both  = [r for r in nof if r.get('grantor') and r.get('dcad_owner')]
genu  = [r for r in both if not (tokens(r['grantor']) & tokens(r['dcad_owner']))]

print(f"NOF total: {len(nof)}")
print(f"  missing property: {len(missing_prop)}  city-only: {len(city_only)}  junk: {len(junk_prop)}")
print(f"  duplicate properties: {dup_prop}")
print(f"  mailing missing: {len(no_mail)}  ids: {[r.get('record_id') for r in no_mail]}")
print(f"  property/mailing: exact={exact} cosmetic={cosmetic} genuine={genuine}")
print(f"  grantor date-bleed: {len(date_bleed)}")
print(f"  grantor stray short leader: {len(stray_leader)}")
print(f"  grantor/owner genuinely different name: {len(genu)}")
EOF
```
