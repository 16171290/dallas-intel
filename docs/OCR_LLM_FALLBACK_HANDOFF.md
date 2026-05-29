# OCR Watermark → LLM Fallback — Work Handoff

**Last updated:** 2026-05-29
**Branch:** `claude/kind-lovelace-J7RSf`
**Audience:** the next engineer/AI picking this up cold. This is the
single source of truth for the watermark-OCR investigation and the
vision-LLM extraction fallback that came out of it. Read this top to
bottom and you have full context.

Companion doc (deeper investigation detail):
[`OCR_WATERMARK_REMOVAL_INVESTIGATION.md`](./OCR_WATERMARK_REMOVAL_INVESTIGATION.md)
(see its **"Addendum — Follow-up evaluation (2026-05-29)"** section).

---

## 1. The problem

publicsearch.us serves Notice-of-Foreclosure (NOF) documents as **flattened
raster PNGs** with a diagonal **"Unofficial Copy"** watermark burned into the
pixels. Our pipeline (`scraper/foreclosure_ocr.py`) OCRs those PNGs with
Tesseract and extracts fields (grantor, property address, sale date, etc.) via
regex. Where the watermark crosses **bold text** (grantor names especially),
Tesseract garbles it — e.g. "NICOLE" → "COLE", "BEAUMONT" → "BEA OS REEF" —
and the regex layer can't recover what the OCR destroyed.

publicsearch NOFs are **live in production** (confirmed by the operator
2026-05-29), so this is an active data-quality issue, not a dormant one.

## 2. What we investigated (and the verdict)

The operator asked whether off-the-shelf GitHub "watermark remover" projects
were worth wiring into the scraper. We evaluated five tools across two
categories and ran real bake-offs.

### Watermark-*removal* tools (all rejected)

| Tool | Method | Why rejected |
|---|---|---|
| `braindotai/Watermark-Removal-Pytorch` | Deep Image Prior (per-image GPU, needs mask) | Same fill-from-neighbors ceiling as cv2; far more expensive; outputs image not text |
| `SamurAIGPT/seedance-2.0-watermark-remover` | Video corner-badge removal | Built for video; temporal frame-averaging doesn't apply to single-page scans |
| `ZingZing001/WaterMarkRemovalTool` | pikepdf layer-strip / RGB-HSV threshold | We receive flattened PNGs (no PDF layer); thresholding already failed (watermark = same blackness as text) |
| `watermarkremover.io` (SaaS) | DL inpainting | Proprietary version of the same idea; cleans image not text; external paid dep |
| `dali92002/DE-GAN` (document GAN) | Conditional U-Net GAN, text-preserving, no mask | **Tested end-to-end** — see below |

**DE-GAN bake-off result (the one we actually ran):** loaded the real
pretrained `watermark_rem_weights.h5`, ran it on all 7 forensic failure cases,
re-OCR'd with Tesseract, scored keyword recovery. Net: **0 keyword wins, 1
regression** (it broke "LIABLE"). No better than the cv2 inpainting the
original investigation had already deferred. Conclusion: **image-cleaning
cannot recover text destroyed by ink-on-ink overlap** — this is fundamental,
and confirmed twice (cv2 and DE-GAN).

### The category that actually works: read the document with context

We then read the *same* failing pages with a **multimodal model** (vision LLM
reading the PNG directly). It recovered **every** hard field that cv2 and
DE-GAN missed:

| Case | Field | Tesseract | DE-GAN | Vision LLM |
|---|---|---|---|---|
| Montgomery (315561589) | grantor | ✗ "COLE" | ✗ | ✅ NICOLE A. MONTGOMERY |
| Montgomery | address | ✗ "BEA OS REEF" | ✗ | ✅ 2206 BEAUMONT STREET |
| Battie (315561578) | address | ✗ | ✗ | ✅ 1442 MARLENE PLACE |
| Liable (315562554) | grantor | ✗ "LIABLE" | ✗ | ✅ LAPRENA GRANT & REGINALD GRANT |

A pixel cleaner makes a prettier *image*; our problem is recovering *text*. The
LLM reasons about document structure and language, so it reads through the
watermark. **Decision: drop the watermark-removal family; build a vision-LLM
extraction fallback.**

## 3. The "Liable" finding — NOT a live bug (already guarded)

While investigating, we found record 315562554's production grantor `'Liable'`
is a **misparse**: the page reads *"…BUT NOT TO OTHERWISE BE **LIABLE** as
Grantor/Borrower…"* and an old regex grabbed "LIABLE". The true grantors are
**LAPRENA GRANT and REGINALD GRANT**.

**This is already fixed.** PR 7's `_is_valid_grantor()`
(`scraper/foreclosure_ocr.py:394`) rejects single-token boilerplate like
"LIABLE". We verified against the live code: current extraction returns
`grantor=None`, not "Liable". A scan of the 2026-05-29 run (961 records) found
**0** boilerplate/single-token grantors. The stale "Liable" value predated the
fix.

What remains is **not a bug** — it's a *recovery gap*: 3 of 961 records this
run have a null grantor (all watermarked NOFs: 316689877, 316730745,
316730746) because the watermark shredded the anchor text so the good regex
pattern can't fire. That gap is exactly what the LLM fallback closes. **No
further regex work is warranted here.**

## 4. What we built

### `scraper/llm_ocr.py` — the fallback module

A context-aware extraction fallback. Key properties:

- **Inert by default.** Does nothing unless `FORECLOSURE_LLM_OCR_ENABLED=true`.
- **Anthropic SDK + structured output.** Uses `client.messages.parse()` with a
  Pydantic schema (`grantor`, `property_address`, `sale_date`, `confidence`)
  so the result is **validated JSON**, not hand-parsed.
- **Vision.** Sends up to `LLM_OCR_MAX_PAGES` page PNGs (base64) per record.
- **Self-reported confidence + abstain floor** (`CONFIDENCE_FLOOR`, default 0.5).
- **Caches** per `record_id` to `data/cache/llm_ocr/<id>.json` (gitignored) —
  re-runs don't re-bill.
- **Graceful degradation.** Returns `None` (no crash) if the SDK isn't
  installed, the API key is missing, or the API errors — mirroring the
  Tesseract-missing path already in the pipeline.

**Public surface:** `enabled() -> bool`, `extract(record_id, page_pngs) ->
Optional[LLMFields]`. `LLMFields` = dataclass(grantor, property_address,
sale_date, confidence, from_cache).

**Environment variables (all optional except the gate + key):**

| Var | Default | Purpose |
|---|---|---|
| `FORECLOSURE_LLM_OCR_ENABLED` | `false` | Master gate. Must be `true` to run. |
| `ANTHROPIC_API_KEY` | — | Required at runtime. Get from console.anthropic.com → API Keys. |
| `LLM_OCR_MODEL` | `claude-opus-4-8` | Model override. Operator wants **`claude-sonnet-4-6`** for cost. |
| `LLM_OCR_THINKING` | `adaptive` | Set to `disabled` to hard-cap output tokens (no thinking). |
| `LLM_OCR_MAX_PAGES` | `2` | Pages sent per record. Use `3` for testing (grantor can be on a later page — e.g. Betancourt). |
| `LLM_OCR_CONFIDENCE_FLOOR` | `0.5` | Below this, abstain. |
| `LLM_OCR_CACHE_DIR` | `data/cache/llm_ocr` | Cache location. |

### `scripts/llm_ocr_probe.py` — standalone test harness

Calls `llm_ocr.extract()` **directly** on page images — no Playwright, no
Tesseract, no `main.py`. Force-enables the gate. Two modes:

- `python scripts/llm_ocr_probe.py page_01.png [page_02.png ...]` — one record.
- `python scripts/llm_ocr_probe.py --forensic DIR` — runs all 7 forensic cases
  under `DIR/<record_id>/page_0*.png`, scores extraction against known answers,
  prints a keyword-recall tally. `--fresh` ignores cache.

### Other changes

- `scraper/requirements.txt` — added `anthropic>=0.105,<1.0`.
- `.gitignore` — added `data/cache/llm_ocr/`.

## 5. Current state — what's done vs. NOT done

**Done & committed** (branch `claude/kind-lovelace-J7RSf`):

| Commit | What |
|---|---|
| `214117c` | Investigation addendum + DE-GAN bake-off harness (`scripts/ocr_watermark_bakeoff/`) |
| `d41bd71` | Corrected the "Liable" framing (already-guarded, not a live bug) |
| `bb8b1b1` | `scraper/llm_ocr.py` + `scripts/llm_ocr_probe.py` + requirements + gitignore |
| `efc00a3` | `LLM_OCR_THINKING` token-cap lever |

**NOT done — deliberately deferred:**

1. **Wiring into `main.py` / `enrich_foreclosure_records`.** The operator wants
   to validate the LLM via the probe *before* it touches the pipeline. The
   module is currently called by nothing in production.
2. **The operator had not yet run the probe** as of this handoff (was setting
   up local env: Windows PowerShell, venv at
   `C:\Users\MarkFuller\Desktop\dallas-intel`, Sonnet, needs an API key).

### The planned `main.py` wiring (when approved)

Verified integration point: `enrich_foreclosure_records()` in
`scraper/foreclosure_ocr.py`. After the regex field-stamping block (after
**line ~1430**, where `rec`, `rid`, `cap.pages`, and the `meta` dict are in
scope), insert a guarded fallback:

```python
if llm_ocr.enabled() and (not rec.get("grantor") or not rec.get("address")):
    llm = llm_ocr.extract(rid, [cap.pages[k] for k in sorted(cap.pages)])
    if llm and llm.grantor and not rec.get("grantor"):
        cand = _clean_grantor(llm.grantor)
        if _is_valid_grantor(cand):                 # SAME validator as regex
            rec["grantor"] = cand
            meta["ocr"]["grantor_source"] = "llm"
            rec.setdefault("parse_warnings", []).append("WARN_LLM_OCR_USED")
            stats.llm_ocr_used += 1
    if llm and llm.property_address and not rec.get("address"):
        if _is_valid_address(llm.property_address): # SAME validator as regex
            rec["address"] = llm.property_address
            rec["address_normalized"] = _norm.normalize_address(llm.property_address)
            meta["ocr"]["address_source"] = "llm"
```

Plus: `from . import llm_ocr` at module top; add `llm_ocr_used: int = 0` to
`OCREnrichmentStats` (line ~961). Invariants: off by default; fill-don't-
overwrite (matches existing line ~1369 convention); LLM output passes the
**same** `_is_valid_grantor`/`_is_valid_address` gates as regex; only fires on
the ~3/961 null-field records; never bypasses Stage 6.6 downstream resolution.

### Tests not yet written

A `tests/test_llm_ocr.py` is planned (mirroring `tests/test_foreclosure_ocr.py`
pure-function style, **mocked client, no network**): schema parse,
abstain-on-low-confidence, gate-off returns None, cache hit skips API, and the
validator-reuse gate (a mocked "LIABLE" is rejected). Not yet implemented.

## 6. How to test locally (Windows PowerShell, Sonnet)

From the repo root (`C:\Users\MarkFuller\Desktop\dallas-intel`, venv active):

```powershell
git fetch origin claude/kind-lovelace-J7RSf
git checkout claude/kind-lovelace-J7RSf
git pull origin claude/kind-lovelace-J7RSf

# Unzip the forensic bundle into the repo (match the actual zip name):
Expand-Archive -Path .\ocr_forensic_bundle.zip -DestinationPath .\ocr_test -Force
dir .\ocr_test\ocr_forensic

pip install anthropic

$env:ANTHROPIC_API_KEY = "sk-ant-...your key..."
$env:LLM_OCR_MODEL     = "claude-sonnet-4-6"
$env:LLM_OCR_THINKING  = "disabled"
$env:LLM_OCR_MAX_PAGES = "3"

# One record first, then all 7 scored:
python scripts\llm_ocr_probe.py .\ocr_test\ocr_forensic\315561589\page_01.png
python scripts\llm_ocr_probe.py --forensic .\ocr_test\ocr_forensic
```

The forensic bundle (`ocr_forensic/<record_id>/page_0*.png` + extraction.json +
REPORT.md per case) was supplied by the operator; it is **not** committed to
the repo. The 7 cases: 315561589 Montgomery, 315561578 Battie, 315561574
Betancourt, 315562554 Liable, 315561570 Velasquez, 315561585 Cox, 315562562
Flores. Ground-truth keywords are embedded in `llm_ocr_probe.py:FORENSIC_TRUTH`.

## 7. Cost notes (the operator's key concern)

- Claude API is **pay-as-you-go**, separate from any Claude.ai chat plan; add
  credits in console.anthropic.com → Billing.
- With **Sonnet 4.6 + thinking disabled**: ~**$0.005 per page** (image input
  dominates; a page downscales to ~1,500 tokens). **$20 ≈ ~4,000 pages.**
- In production the fallback fires only on ~3/961 records/run → **~$1–2/year**.
- **Caching** makes probe re-runs free.
- ⚠️ Turning thinking **on** (`LLM_OCR_THINKING=adaptive`) can swing cost 5–20×
  (thinking tokens bill as output at $15/1M). Keep it `disabled` unless a hard
  case needs it.

## 8. Open items / suggested next steps

1. **Operator runs the probe** on the 7 forensic cases (Sonnet) and reviews
   recall vs. the DE-GAN/cv2 baseline (which scored 0). Decide go/no-go.
2. If go: **wire into `main.py`** per §5, flag-defaulted off.
3. **Write `tests/test_llm_ocr.py`** (mocked, no network).
4. Consider whether `LLM_OCR_MAX_PAGES=2` is the right production default
   (Betancourt's grantor was on a later page — may need 3, or a smarter page
   selector).
5. Decide the production confidence floor and whether LLM-filled fields should
   carry a distinct dashboard provenance badge beyond `WARN_LLM_OCR_USED`.

## 9. File & artifact index

| Path | Role |
|---|---|
| `scraper/llm_ocr.py` | The vision-LLM extraction fallback module |
| `scripts/llm_ocr_probe.py` | Standalone tester (run before wiring) |
| `scripts/ocr_watermark_bakeoff/` | DE-GAN bake-off harness + README (reproducibility) |
| `docs/OCR_WATERMARK_REMOVAL_INVESTIGATION.md` | Full investigation + 2026-05-29 addendum |
| `docs/OCR_LLM_FALLBACK_HANDOFF.md` | **This file** |
| `scraper/foreclosure_ocr.py` | Existing OCR+regex extractor; `enrich_foreclosure_records` is the wiring target |
| `data/cache/llm_ocr/` | Runtime LLM result cache (gitignored) |
