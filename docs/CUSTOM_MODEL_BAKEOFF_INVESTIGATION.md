# Custom NOF Extraction Model — Bake-off Investigation Plan

**Mode:** INVESTIGATION / BAKE-OFF ONLY.
**Decision sought:** Is a fine-tuned small model (distilled from our own auto-labeled
data) worth building, versus keeping the existing vision-LLM fallback?
**Companion docs:** `OCR_LLM_FALLBACK_HANDOFF.md`, `OCR_WATERMARK_REMOVAL_INVESTIGATION.md`.

---

## 0. Framing — read this first

**This is a feasibility bake-off, not a build.** The output is a report and a
go/no-go recommendation backed by numbers. You MAY write scratch scripts, train
a candidate model, and run evals to produce that evidence. You MUST NOT:

- wire anything into `main.py` or `enrich_foreclosure_records`,
- modify `scraper/foreclosure_ocr.py`, `scraper/llm_ocr.py`, or any production path,
- add a model to the pipeline or change how production extracts fields.

Everything you create lives in a scratch directory (`scripts/custom_model_bakeoff/`,
mirroring the existing `scripts/ocr_watermark_bakeoff/`). If a phase's **kill
criterion** trips, stop and report — do not push on to the next phase. When the
investigation is done, ask before anything moves toward production.

**Scope: NOF records only.** We also have probate (PB) records. Exclude them
entirely from training data, test data, and scoring. If a record's type is
ambiguous, set it aside and report the count separately rather than guessing.

**The bar this has to clear (state it up front, judge against it at the end):**
the current vision-LLM fallback costs ~$1–2/year because it fires on ~3/961
records. A custom model that merely *matches* that on accuracy is **not worth
building** — the only justifications are non-cost: removing the external API
dependency, keeping owner/property data fully on-premises, running free at much
higher volume, or a *material* accuracy gain (especially on the false-confidence
metric in §3). Hold the verdict to that bar.

---

## 1. Establish the baselines to beat

Before training anything, record the numbers the candidate must beat. Reuse the
existing harness — do not reinvent scoring.

1. **Tesseract + regex** (current production extractor) — run on the held-out
   test set (defined in §2) and record per-field recall.
2. **Vision-LLM fallback** (`scripts/llm_ocr_probe.py --forensic`) — run on the
   7 forensic cases and the held-out clean sample. Record per-field recall and,
   critically, its false-confidence rate (§3).

Output: a baseline table (`baseline_scores.json` + a short markdown summary).
These two rows are the bar.

**Kill criterion:** if the vision-LLM baseline already scores at or near the
ceiling on both recall and false-confidence, note that the headroom for a custom
model is small and flag it before proceeding.

---

## 2. Data feasibility — can we auto-label a corpus cheaply?

This is the make-or-break phase. The hypothesis is that we are sitting on a
self-labeling corpus and don't need manual annotation.

1. **Auto-label the easy majority.** From a recent clean run, select records
   where regex extraction succeeded AND DCAD resolution agreed (high-confidence
   labels). Each becomes a training pair: page PNG(s) → `{grantor,
   property_address, sale_date}`. Report the count.
2. **Audit label quality.** Hand-check a random sample (e.g. 30–50) — does the
   regex/DCAD field actually match what's on the page? Report the agreement rate.
   Auto-labels are only useful if they're actually correct.
3. **Label the hard minority by distillation.** For watermark-shredded records
   (null/garbled fields), use the existing vision-LLM to produce labels. Spot-
   audit a sample of these too. Report how many hard cases you can label.
4. **Class balance.** Report how many training pairs are clean vs
   watermark-crossed. A model trained almost entirely on clean pages won't learn
   the hard case — note if the hard-case count is too thin.
5. **Hold out a test set NOW, before any training.** Build a stratified
   held-out set that includes the 7 forensic cases (`FORENSIC_TRUTH`) plus a
   sample of clean records. **This set must never appear in training.** Score
   everything against it later.

Output: `dataset_report.md` with counts, label-agreement rates, class balance,
and the train/test split manifest (record IDs only).

**Kill criterion:** if you can't assemble at least a few hundred audited clean
labels and a usable number of hard-case labels, stop. Without data the rest is
moot — report that the distillation-corpus assumption didn't hold.

---

## 3. Candidate models — what to train, what to avoid

Train on a single GPU; LoRA / parameter-efficient only; short runs. Note the
cloud-GPU cost incurred.

- **Candidate A — LoRA fine-tune of a small open vision-language model** (e.g.
  a 3B–7B VLM in the Qwen2.5-VL family or current equivalent). Reads pixels
  directly; learns to ignore the "Unofficial Copy" watermark and to anchor on
  NOF layout.
- **Candidate B (optional) — Donut / Pix2Struct fine-tune.** OCR-free,
  image→structured-fields, smaller and cheaper to run. Worth including if time
  allows, since it may be the better production shape if this goes forward.
- **Explicitly EXCLUDE OCR-token layout models (LayoutLM family).** They consume
  Tesseract tokens+boxes as input, which is the exact layer the watermark
  breaks. Do not spend time on them; state that exclusion in the report.

Output: trained LoRA adapter(s) in the scratch dir, plus a training log
(steps, loss, GPU time, cost estimate).

---

## 4. The bake-off — score candidates vs baselines

Score every candidate against the **§2 held-out test set** (never the training
data) using the **existing probe scoring** (`llm_ocr_probe.py` recall logic +
`FORENSIC_TRUTH`). Two test slices:

- **Held-out clean sample** — does specialization *hurt* the easy cases the
  current pipeline already gets right? (Regression check.)
- **The 7 forensic hard cases** — the recovery the whole effort is about.

Report, per candidate, per field (grantor / property_address / sale_date):

1. **Recall** — exact-match and partial-match.
2. **False-confidence rate (the metric that matters most).** Count cases where
   the model emits a *well-formed, plausible* value that is *wrong* — a real
   street that's the wrong street, a real name that's the wrong name. This is
   more dangerous than an obvious garble because it passes a human glance and the
   format validators. Measure it explicitly and compare it head-to-head with the
   vision-LLM baseline's rate. A custom model that wins recall but loses here is
   not a win.
3. **Abstention behavior** — if the model can express low confidence, how often
   does it correctly decline on the cases it would have gotten wrong?

Output: `bakeoff_scores.md` — one table, baselines and candidates side by side,
the three metrics above.

---

## 5. Cost & operational comparison

A short, factual comparison — no recommendation yet, just the numbers:

- **Inference:** self-hosted latency and hardware requirement vs the API's
  per-page cost.
- **One-time costs:** vision-LLM calls to label the hard cases (§2.3) + GPU
  training time (§3).
- **Dependency / data residency:** does self-hosting actually remove the
  external API dependency and keep owner/property data on-premises? This is one
  of the few non-cost reasons that could justify a build — state plainly whether
  it's achieved.

Output: a cost/ops table in the final report.

---

## 6. Verdict

Write a go/no-go recommendation, judged against the bar in §0. It must answer,
with evidence from §4–§5:

- Does a fine-tuned model beat the vision-LLM baseline on **recall** AND
  **false-confidence** by a margin that matters?
- If the only advantage is cost: recommend **no-go** (the baseline is ~$1–2/yr).
- If there's a real non-cost case (dependency removal, data residency, scale, or
  a genuine accuracy gain): say so, and scope what a production build would
  involve — but do not start it. Ask first.

Default expectation to test against, not assume: given the tiny failure volume,
the likely honest answer is "keep the vision-LLM fallback unless data residency
or scale is the real driver." Let the numbers overturn that if they can.

---

## 7. Deliverables index

| Path | Role |
|---|---|
| `scripts/custom_model_bakeoff/` | All scratch code for this investigation (read-only to production) |
| `baseline_scores.json` | §1 baseline numbers |
| `dataset_report.md` | §2 corpus feasibility + train/test manifest |
| training log | §3 steps/loss/GPU cost |
| `bakeoff_scores.md` | §4 candidates vs baselines, three metrics |
| **`CUSTOM_MODEL_BAKEOFF_REPORT.md`** | Final report: dataset, scores, cost, §6 verdict |

Nothing in this investigation touches `main.py`, `foreclosure_ocr.py`, or
`llm_ocr.py`. If anything is ambiguous, ask before assuming.
