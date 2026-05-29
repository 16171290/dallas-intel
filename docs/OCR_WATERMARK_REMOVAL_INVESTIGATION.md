# OCR Watermark Removal — Investigation Report

**Date:** 2026-05-27
**Branch:** `claude/pr7-ocr-watermark-investigation` (investigation only — no code shipped)
**Outcome:** Deferred (Option C — status quo). Documented for future revisit.

---

## TL;DR

Publicsearch.us renders every Notice-of-Foreclosure PDF as a PNG with a
diagonal `Unofficial Copy` watermark overlaid in black ink. Where the
watermark crosses key fields, Tesseract garbles the text (Montgomery
"NICOLE" → "COLE", Cox "BEAUMONT" → "BEA OS REEF", etc.).

We investigated whether the watermark can be removed via image
preprocessing before OCR. Three approaches tested:

1. **Pixel thresholding** — failed; watermark is black ink, same intensity
   as document text.
2. **Whitewash by template mask** — failed; punches holes through text
   where watermark crosses it.
3. **cv2 inpainting with multi-document template mask** — partial success;
   recovers Montgomery "NICOLE" but causes a regression on Velasquez
   "GRANADOS" and leaves residual letter errors ("BEAUMONT" → "BEALMONT")
   where text and watermark overlap.

**Decision:** Defer. The actual operational risk (wrong-property phone
calls) is already mitigated by Stage 6.6 cross-path agreement +
publicsearch page tiebreaker. The remaining issue is cosmetic (dashboard
grantor names occasionally look garbled). Engineering effort is high
relative to the marginal cosmetic gain.

**Revisit triggers:** see [§ When to revisit](#when-to-revisit).

---

## Background

PR 7 (`claude/pr7-ocr-forensic`) added forensic OCR probe
(`scripts/probe_ocr_pages.py`) and four post-extraction validators
(boilerplate rejection, garble rejection, wrong-county-zip rejection,
leading-punctuation strip). These addressed Liable / Adama / Battie /
Flores at the regex layer.

Three failure cases remained that the regex layer cannot fix because
the OCR output itself was wrong:

| Case | record_id | Production OCR | Actual document |
|---|---|---|---|
| Montgomery | 315561589 | grantor=`COLE A. MONTGOMERY` | `NICOLE A. MONTGOMERY` |
| Velasquez | 315561570 | wife=`ZULMA .NOH OS ARRIAZA` | `ZULMA NOHEMY GRANADOS ARRIAZA` |
| Cox (address) | 315561585 | `3031 CREST RDG D &` | `3031 CREST RIDGE DRIVE` |

In each case, the diagonal "Unofficial Copy" watermark intersects the
target text and Tesseract loses character information.

User question: **how hard is it to remove the watermark before OCR, and
is it worth the time?**

---

## Investigation

### Step 1: Pixel-distribution analysis

Goal: determine whether the watermark is brighter than the text (so
brightness thresholding can separate them).

```python
img = Image.open('315561589/page_01.png').convert('L')
arr = np.array(img)
# Histogram on 8-bit greyscale, 16 bins
```

| Pixel value | Count |
|---|---|
| 0-16 | 484,649 (text) |
| 16-238 | ~80,000 (watermark + anti-aliasing) |
| 239-255 | 7,956,324 (background) |

**Finding:** distribution is bimodal. Watermark sits in the ~16-238
middle range BUT only when the watermark is anti-aliased on white
background. Where the watermark crosses text, both contribute and the
result is in the 0-16 range — indistinguishable from pure text by
intensity alone.

### Step 2: Brightness thresholding

Tried thresholds 128, 180, 200, 230. None recovered "NICOLE":

| Threshold | NICOLE recovered? | COLE A. MONTGOMERY? | BEAUMONT? |
|---|---|---|---|
| baseline (no preprocessing) | ✗ | ✓ | ✗ |
| 128 | ✗ | ✓ | ✗ |
| 180 | ✗ | ✗ | ✗ |
| 200 | ✗ | ✗ | ✗ |
| 230 | ✗ | partial | ✗ |

**Finding:** thresholding cannot separate watermark from text by intensity.
Higher thresholds destroy text before they touch the watermark.

### Step 3: Cross-document watermark consistency

Goal: determine whether the watermark is identical across documents
(enabling template-based removal).

```python
# Pixels dark in ALL docs at the same position = watermark
dark_masks = [img < 100 for img in arrays]
common_dark = reduce(operator.and_, dark_masks)
```

Result image (saved as `/tmp/watermark_mask.png` during investigation):

The diagonal "Unofficial Copy" text appears clearly as a stable outline
across all sampled documents.

**Finding:** the watermark IS pixel-stable across same-size documents.
Template subtraction is technically feasible.

Important caveat: documents have **multiple page sizes**. Sampled values:

| Page size (H × W) | Doc count |
|---|---|
| 3344 × 2552 | 6 |
| 3336 × 2552 | 1 (Liable) |
| 4200 × 2552 | 1 (Flores) |

A single template won't cover all docs — would need per-size templates
or runtime auto-detection.

### Step 4: Whitewash by template mask

Approach: where mask says "watermark pixel", set pixel to background
(white).

```python
cleaned = original.copy()
cleaned[watermark_mask] = 255
```

Result on Montgomery: **OCR found nothing** about Montgomery / NICOLE /
COLE / BEAUMONT. The whitewash punched holes through the text strokes
where the watermark intersects them.

**Finding:** simple whitewash is destructive. Need an inpainting
approach that fills with surrounding-pixel information.

### Step 5: cv2 inpainting

Used `opencv-python-headless` for image manipulation. Two algorithms
tested:

- `cv2.INPAINT_TELEA` (fast, Alexandru Telea's method)
- `cv2.INPAINT_NS` (Navier-Stokes-based, slower but smoother)

Two radii tested: 3 and 7 pixels.

Process:
1. Build watermark mask from 6 same-size documents (>= 70% darkness
   consensus per pixel)
2. Dilate mask by 2 iterations of a 3×3 kernel (cover anti-aliased edges)
3. Apply inpainting to target document
4. Run Tesseract on inpainted result

**Results on Montgomery (TELEA, radius 3):**

| Field | Before | After inpainting |
|---|---|---|
| Grantor | `COLE A. MONTGOMERY, SINGLE WOMAN` | ✓ `NICOLE A. MONTGOMERY, SINGLE WOMAN` |
| Address | `2206 BEA OS REEFGRAND PRAIRIE, TX 75051` | partial `2206 BEALMONT STPELT GRAND PRAIRIE, TX 75051` |

Big win on the grantor name. Address recovery is partial — text-on-text
overlap still leaves letter substitutions because the inpainting
interpolates from neighborhood pixels (which were themselves watermark
pixels).

### Step 6: Net effect across all 7 watermarked cases

Tested inpainting on every failure case at the same page size:

| Case | Keyword | Before | After | Net |
|---|---|---|---|---|
| Montgomery | NICOLE | ✗ | ✓ | ✨ WIN |
| Montgomery | BEAUMONT | ✗ | ✗ | — |
| Cox | LAURA / COX / CREST RIDGE | ✓ ✓ ✓ | ✓ ✓ ✓ | — |
| Battie | BATTIE / MARLENE | ✓ ✗ | ✓ ✗ | — |
| Betancourt | BETANCOURT / VANGUARD | ✗ ✓ | ✗ ✓ | — |
| Velasquez | NOHEMY / GRANADOS / GRENOBLE | ✓ ✓ ✓ | ✓ **✗** ✓ | ⚠ REGRESSION |
| Liable | (skipped — different page size 3336×2552) | | | |
| Flores | (skipped — different page size 4200×2552) | | | |

**Summary:** 1 win (Montgomery NICOLE), 1 regression (Velasquez
GRANADOS), 5 unchanged, 2 untestable with single-template approach.

---

## Three options evaluated

### Option A: In-house watermark removal

**Effort:** 3-5 days for a basic implementation, 1-2 weeks production-grade.

Production-grade requirements:
- Auto-detect page dimensions; maintain template per size
- Periodically refresh templates from recent document samples (in case
  publicsearch.us changes the watermark)
- Regression-test against the forensic-case set every CI run
- Fail-soft: when inpainting causes more harm than good (Velasquez
  GRANADOS), don't ship the worse output

**Quality:** 1 of 7 cases recovered fully; 1 regression. Net impact
small.

**Risk:** new image-processing dependency (cv2, ~50 MB), regression risk
on cases that currently work.

**Cost:** $0 runtime; ~$0 maintenance after initial build.

### Option B: Cloud OCR for low-confidence records

**Effort:** 2-3 days integration.

Approach: when current Tesseract output trips the PR 7 validators
(boilerplate grantor / garbled address / wrong-county zip), re-OCR that
single record via Google Vision API or AWS Textract — both handle
watermarked documents substantially better than Tesseract.

**Quality:** likely recovers ALL 7 failure cases cleanly (Google Vision
explicitly handles overlapping text via better layout analysis).

**Risk:** new external dependency. Cloud API can have downtime, rate
limits, billing edge cases.

**Cost:**
- Google Vision Document Text Detection: $1.50 / 1000 pages
- Current volume: ~91 records/week × 3 pages average = ~273 pages/week
  → $0.41/week → ~$21/year
- If only retry the ~5-10% that fail validators: ~$1-2/year

### Option C: Status quo

**Effort:** $0.

Stage 6.6 (PR 5) already prevents wrong-property calls via cross-path
agreement + publicsearch page tiebreaker. The remaining issue is
cosmetic: dashboard grantor names occasionally show garbled values
("Cole A. Montgomery", `null` for Liable). Operator can spot-check
these by clicking the source_url before calling.

---

## Decision

**Option C selected.** Reasoning:

1. **Operational risk already mitigated** — Stage 6.6's page tiebreaker
   resolved 4 of 4 disagreements correctly in the validation run. Cox /
   Montgomery / Battie / Betancourt all have correct dcad_account in
   production today.
2. **In-house engineering not justified** by the marginal gain (1 case
   improved, 1 regressed) and template-per-page-size complexity.
3. **Cloud OCR is the right pursuit if grantor quality becomes
   operationally critical** — but operators have not reported the
   dashboard names as unusable. Premature optimization without that
   signal.

---

## When to revisit

Bring this back if any of the following happen:

1. **Operators report wrong-person calls** in the field that trace back
   to OCR-garbled grantor names. (Stage 6.6 should prevent this, but
   it's the operationally-critical metric.)
2. **A weekly run produces 10+ records with rejected/null grantors**
   that aren't recoverable by Path A's owner_index lookup. Today the
   typical run has 2-3 such records.
3. **Publicsearch.us changes the watermark** (or removes it). Either
   would invalidate Tesseract's current behavior; revisit OCR strategy
   from scratch.
4. **Cloud OCR pricing drops to near-zero** OR Anthropic/similar
   releases a free document-OCR API that beats Tesseract on watermarked
   docs.

---

## Reproduction recipe (if revisiting)

All experimental artifacts were ephemeral (`/tmp/`); to reproduce:

```bash
# 1. Get the forensic PNG bundle
git checkout claude/pr7-ocr-forensic  # or whatever branch is current
python scripts/probe_ocr_pages.py     # produces docs/ocr_forensic/

# 2. Watermark-template computation (one-time per page size)
python - << 'EOF'
import numpy as np, glob
from PIL import Image
from collections import Counter

docs = sorted(glob.glob('docs/ocr_forensic/*/page_01.png'))
arrays = [np.array(Image.open(d).convert('L')) for d in docs]
common_size = Counter(a.shape for a in arrays).most_common(1)[0][0]
arrays = [a for a in arrays if a.shape == common_size]
stack = np.stack(arrays)
mask = (stack < 100).sum(axis=0) >= int(0.7 * len(arrays))
Image.fromarray((mask * 255).astype('uint8')).save(
    f'docs/ocr_forensic/watermark_template_{common_size[0]}x{common_size[1]}.png'
)
EOF

# 3. Apply inpainting at OCR time (pseudocode)
import cv2
template = cv2.imread('watermark_template_3344x2552.png', cv2.IMREAD_GRAYSCALE)
mask = cv2.dilate(template, np.ones((3,3),np.uint8), iterations=2)
img = cv2.imread('input.png', cv2.IMREAD_GRAYSCALE)
cleaned = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
# Pass `cleaned` to Tesseract instead of raw img
```

## Concrete next-steps checklist (if implementing Option A later)

- [ ] Build watermark template per observed page size (3336×2552,
      3344×2552, 4200×2552). Sample 10+ docs per size for stable masks.
- [ ] Store templates in `data/watermark_templates/` versioned by date.
- [ ] Auto-refresh templates if any captured doc shows < 50% mask
      overlap with the stored template at the same coords (sentinel for
      "watermark changed").
- [ ] Add `--enable-watermark-removal` flag to `foreclosure_ocr` so
      operators can A/B compare in production.
- [ ] Regression test: re-run the OCR validator suite (1040 tests) PLUS
      the 7 forensic-case regressions BOTH before and after applying
      inpainting. Fail CI if any case that currently works regresses.
- [ ] Per-record: emit `WARN_OCR_WATERMARK_INPAINTED` so the operator
      sees which records had image preprocessing applied.

## Concrete next-steps checklist (if implementing Option B later)

- [ ] Sign up for Google Cloud Vision API; obtain JSON service-account
      credentials.
- [ ] Add `scraper/cloud_ocr.py` module that wraps
      `google.cloud.vision_v1.ImageAnnotatorClient.text_detection()`.
- [ ] Wire into `foreclosure_ocr.enrich_foreclosure_records` so that
      records failing the PR 7 validators (boilerplate grantor / garbled
      address / wrong-county zip) trigger a single re-OCR pass via cloud.
- [ ] Cache cloud OCR results in `data/cache/cloud_ocr/<record_id>.txt`
      to avoid re-billing on subsequent runs.
- [ ] Set monthly billing alert at $5 (any cost beyond that means
      something is wrong with the trigger logic).
- [ ] Emit `WARN_CLOUD_OCR_USED` per record so operator sees provenance.

---

# Addendum — Follow-up evaluation (2026-05-29)

**Branch:** `claude/kind-lovelace-J7RSf`
**Trigger:** Operator asked whether off-the-shelf GitHub "watermark remover"
projects are worth wiring into the scraper. Also clarified that
publicsearch.us **NOFs are fully enabled** — so the watermark issue is
**live in production**, not dormant (this weakens the original Option-C
"it's only cosmetic" rationale).

## TL;DR of the follow-up

We evaluated four public watermark-remover repos plus one
document-specific GAN (DE-GAN), and ran DE-GAN end-to-end against the
forensic case set. **Every pixel-based remover hits the same wall the
original investigation found: at bold ink-on-ink overlap the character
information is gone, and cleaning the image cannot invent it back.**

We then tested a *different category* — reading the document with a
context-aware multimodal model — and it **recovered every hard field that
both cv2 and DE-GAN failed on**, and additionally exposed a data-quality
bug ("Liable" is a misparse, not a name).

**Revised recommendation:** stop pursuing the watermark-*removal* family.
The fix is a context-aware **extraction** pass (vision-LLM / cloud
document-OCR), gated behind the existing validators — essentially the
original Option B, but stronger.

## Repos evaluated

| Repo | Method | Verdict |
|---|---|---|
| `braindotai/Watermark-Removal-Pytorch` | Deep Image Prior (per-image GPU optimization, needs mask) | Reject — same fill-from-neighbors ceiling as cv2, far more expensive, outputs image not text |
| `SamurAIGPT/seedance-2.0-watermark-remover` | Video corner-badge removal (frame-averaging + cv2 TELEA / optional LaMa) | Reject — built for video; our docs are single-page; core trick (temporal averaging) doesn't apply |
| `ZingZing001/WaterMarkRemovalTool` | (a) pikepdf layer-strip, (b) RGB/HSV color threshold | Reject — we receive flattened **raster PNGs** (no PDF layer to strip); thresholding already failed in Step 2 (watermark = same blackness as text) |
| `watermarkremover.io` (SaaS) | Deep-learning inpainting | Reject — proprietary version of Option A; cleans image not text; external paid dependency |
| `dali92002/DE-GAN` | Conditional GAN, **document-specific**, text-preserving, no mask | **Tested in depth** (closest match) — see below |

**Pattern:** all four are *image cleaners*. Our bottleneck is *text
recovery* from overlap. Wrong category.

## DE-GAN deep test

DE-GAN (TPAMI 2020) was the only document-and-text-aware tool. We ran it
for real:

- TF1.13 → ported to TensorFlow 2.16 (model code was already
  `tensorflow.keras`; patched removed `scipy.misc` import). U-Net
  generator, ~15.8M params, `biggest_layer=512`, weights
  `watermark_rem_weights.h5` (63 MB).
- Inference tiles the page into 256×256 patches → handles our large page
  sizes with no per-size template (advantage over the cv2 Option-A plan).
- Sanity check on DE-GAN's own sample (`960.png`) recovered text →
  pipeline validated.

### Forensic bake-off (page_01, keyword recovery vs raw Tesseract)

| Case | Keyword | Raw | DE-GAN | Note |
|---|---|---|---|---|
| Montgomery (315561589) | NICOLE | ✗ | ✗ | cv2 recovered this; DE-GAN did not |
| Montgomery | BEAUMONT | ✗ | ✗ | — |
| Cox (315561585) | LAURA / COX / CREST RIDGE | ✓ | ✓ | already fine |
| Battie (315561578) | BATTIE | ✓ | ✓ | — |
| Battie | MARLENE | ✗ | ✗ | — |
| Betancourt (315561574) | VANGUARD | ✓ | ✓ | — |
| Betancourt | BETANCOURT | ✗ | ✗ | name not on page 1 (test-scope artifact) |
| Velasquez (315561570) | NOHEMY / GRANADOS / GRENOBLE | ✓ | ✓ | DE-GAN did **not** regress GRANADOS (cv2 did) |
| Liable (315562554) | LIABLE | ✓ | ✗ | DE-GAN regressed — but see below, "LIABLE" was garbage |
| Flores (315562562) | FLORES / MATHIS | ✓ | ✓ | — |

**Tally: 0 keyword wins, 1 regression.** Versus cv2 (Step 6): 1 win
(NICOLE), 1 regression (GRANADOS). So **DE-GAN is no better than cv2** —
arguably marginally worse — on the canonical hard cases.

Why DE-GAN looked promising on one ad-hoc sample but not here: it lifts
the watermark off **normal-weight body text** well, but the forensic
keywords are all **bold grantor names at peak watermark overlap**, where
stock weights have nothing to reconstruct from. Same wall as cv2.

Caveats: stock weights were trained on a *different* (faint book-page)
watermark; fine-tuning on synthetic "Unofficial Copy" pairs *might* help
(the watermark is pixel-stable per page size, so synthetic pairs are
easy), but it needs a GPU and the overlap problem is fundamental, so the
expected payoff is low.

## Three-way comparison — the decisive result

Read the *same* failing pages with a context-aware multimodal model
(vision-LLM reading the PNG directly):

| Case | Field | Raw Tesseract | DE-GAN | Multimodal LLM |
|---|---|---|---|---|
| Montgomery | grantor | ✗ "COLE" | ✗ | ✅ NICOLE A. MONTGOMERY |
| Montgomery | address | ✗ "BEA OS REEF" | ✗ | ✅ 2206 BEAUMONT STREET |
| Battie | address | ✗ | ✗ | ✅ 1442 MARLENE PLACE |
| Betancourt | address | ✓ | ✓ | ✅ 43 Vanguard Way |
| Liable | grantor | ✗ "LIABLE" (garbage) | ✗ | ✅ LAPRENA GRANT & REGINALD GRANT |

The multimodal read recovered **every** hard field both pixel approaches
missed — because it reasons about document structure, not just pixels.

### Data-quality bug surfaced: the "Liable" misparse

`315562554`'s production grantor `'Liable'` is **not a name**. The page
reads: *"…LAPRENA GRANT and REGINALD GRANT, WIFE AND HUSBAND, WITH HIM
JOINING HEREIN TO PERFECT THE SECURITY INTEREST BUT NOT TO OTHERWISE BE
**LIABLE** as Grantor/Borrower…"*. The extractor's regex grabbed "LIABLE"
because it preceded "as Grantor/Borrower." True grantors: **LAPRENA GRANT
and REGINALD GRANT**. This is independent of the watermark and worth
fixing in the regex layer regardless of OCR strategy.

## Complexity reality-check (vision-LLM extraction module)

Concern raised: an LLM extraction module sounds "extremely advanced." It
is **not** — it is one of the *simpler* options:

- The scraper already captures the PNG and already does HTTP + JSON. The
  module is: base64 the image → POST with a "return grantor/address/dates
  as JSON" prompt → parse JSON. ~100–150 lines.
- **No GPU, no weights, no TensorFlow, no TF1→TF2 port, no tiling, no
  second OCR/regex pass** — i.e. strictly less machinery than the DE-GAN
  path we just exercised.
- The non-trivial part is *responsibility*, not code: hallucination
  guardrails, keeping it behind Stage 6.6 cross-checks + an abstain path,
  cost/caching/retry plumbing. Careful engineering, not research.

Grade: basic version ≈ an afternoon; production-grade ≈ moderate.

## Revised decision

1. **Drop the watermark-removal family** (the 4 repos + DE-GAN). cv2 and
   DE-GAN independently confirm: cleaning the image does not recover
   bold-text overlap.
2. **Pursue context-aware extraction** (vision-LLM or cloud document-OCR),
   triggered only on records that trip the existing validators
   (boilerplate / garbled / wrong-zip) — the original Option B trigger
   logic, returning structured fields with a confidence/abstain signal,
   kept behind Stage 6.6.
3. **Fix the "Liable" regex misparse** in the extraction layer (separate
   from OCR strategy).

## Reproduction (this addendum)

Harness committed under `scripts/ocr_watermark_bakeoff/`
(`run_bakeoff.py`, `forensic_bakeoff.py`). Both expect to run from a clone
of `dali92002/DE-GAN` with `weights/watermark_rem_weights.h5` in place and
`tesseract` on PATH. See that folder's `README.md`. DE-GAN weights are not
redistributed here (GPL-3.0, academic-use); fetch from the upstream repo.
