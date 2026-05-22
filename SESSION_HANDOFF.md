# Session Handoff — 2026-05-22

This file exists to brief the next AI session on (a) what was just shipped,
(b) what evidence justified it, (c) how to validate the next GHA run, and
(d) what's still outstanding. Delete this file once PR 12.21 is validated
and the parked items are picked up — it's a session artifact, not
permanent docs.

---

## 1. What was just shipped (PR 12.21, merged to main as `b23a4a7`)

**Two-line OCR speedup fix** in `scraper/foreclosure_ocr.py`:

```diff
-        text = pytesseract.image_to_string(img)
+        text = pytesseract.image_to_string(img, config="--psm 6 --oem 1")
```

Same change at both call sites:
- Line 566 — `_ocr_one_image_worker` (parallel hot path used by
  `multiprocessing.Pool`)
- Line 637 — serial fallback in `_ocr_pages`

`--psm 6` tells Tesseract "assume a single uniform block of text"
instead of running its expensive layout auto-detection (`--psm 3`,
which is the default). `--oem 1` pins the LSTM engine instead of the
default `--oem 3` (legacy + LSTM fallback).

---

## 2. Why this fix — the evidence trail

User explicitly instructed "FORENSIC BOTTLENECK RECON MODE ONLY" partway
through this session — no fixes until the bottleneck was **proven**, not
inferred. Five PRs of pure instrumentation built that proof:

| PR    | What it added                                            | What it proved / eliminated                                                |
| ----- | -------------------------------------------------------- | -------------------------------------------------------------------------- |
| 12.15 | Per-phase timing (goto, passive_wait, page_count, reload, per_page) | Bottleneck is OCR, not page capture (~95% of wall time is `_ocr_pages`)    |
| 12.16 | Fixed `NameError: t0` from 12.15; worker returns tuple   | (bugfix only)                                                              |
| 12.17 | `OMP_NUM_THREADS=1` in workflow + tesseract env logging  | OMP contention is NOT the bottleneck (only 6% improvement after pinning)   |
| 12.18 | Save first OCR PNG to `data/probes/ocr_sample_<rid>_p1.png` | Provided the captured image used by 12.19's PSM-compare                  |
| 12.19 | Multi-hypothesis recon: sysinfo, synthetic Tesseract baseline, in-run PSM-compare, per-image pixel/mode/dpi | **Proved PSM=3 is the bottleneck**: 343s (PSM=3) vs 2.0s (PSM=6) on the same 2552×3336 grayscale 299-DPI page |
| 12.20 | Capture-failure forensic dump (response log, screenshot, HTML, img-src list) | When 12.19 hit `no_images` on all records, this added the diagnostics needed to triage — turned out to be intermittent publicsearch.us SPA breakage, not our environment |

Key one-shot data points from CI logs:
- Synthetic Tesseract baseline (1000×1500 black-on-white text):
  **0.34s** — proves the CPU is fine.
- Pool parallelism verified: `sum_worker/wall ≈ 3.9×` on 4 workers.
- Per-page variance was **content-dependent**, not load-dependent
  (343s / 108s / 18s on three identical-format pages in the same record).
- Local Windows runs: ~10s/record. CI runs (pre-fix): ~250–300s/record.

---

## 3. How to validate the next GHA cron run

Look for these signals in the workflow log, in order:

1. **OCR env log line** (from PR 12.17): should show `tesseract 5.3.4`,
   `OMP_NUM_THREADS=1`, `cpu_count=4`.
2. **Synthetic baseline** (from PR 12.19): should still be ~0.3–0.5s.
3. **`_ocr_pages: ... wall=` line per record**: should drop from
   ~340s to ~2–5s per page. Total OCR per record should fall from
   5–10 min to under 30s.
4. **`PSM-compare` line on the first record** (still in code from PR
   12.19): will now show *both* legs running PSM=6 — that's harmless
   noise. After the run validates the fix, that block should be
   removed (see §5 cleanup).
5. **Extraction success counter**: `extracted=` per record should be
   equal-or-higher than before. If you see records where it drops to
   0, PSM=6 has failed on that template — go to §4.

End-to-end expectation: full pipeline should complete in **~10–15 min
total** instead of the 2-3 hour catastrophe we've been seeing.

---

## 4. If PSM=6 regresses extraction on some templates (fallback playbook)

PSM=6 assumes one uniform text block. Foreclosure notices ARE
single-column, so it should work — but if a template has heavy
multi-column layout (e.g. side-by-side trustor/beneficiary tables), some
field labels may not be recognised.

If the next run shows a drop in `extracted_count` for any record:

1. **Identify which field(s) got lost**: enable DEBUG logging and
   diff the OCR text from `data/probes/ocr_sample_*.png` against the
   patterns in `scraper/foreclosure_ocr.py:110-210`
   (`GRANTOR_PATTERNS`, `SALE_DATE_PATTERNS`, etc).
2. **Try alternate PSMs in priority order**:
   - `--psm 4` — single column of text of variable sizes (next-best
     for multi-column notice layouts)
   - `--psm 11` — sparse text, find as much as possible in no
     particular order (resilient but text order is not preserved,
     which can break section-anchored regexes)
   - `--psm 3` — original default. Only revert here if 4 and 11 also
     fail; it brings back the 171× slowdown.
3. **Use `probe_psm_compare.py`** (now in repo root) to A/B test
   locally before shipping any further config change:
   ```
   python probe_psm_compare.py data/probes/ocr_sample_<rid>_p1.png
   ```
4. **Last resort: per-template config.** The existing extractor is
   pattern-soup (no template classification) by design, and that
   has worked well — adding a config-per-template branch is a
   significant refactor and should NOT be the first move.

---

## 5. Cleanup work pending (do AFTER the next run validates the fix)

The forensic instrumentation from PRs 12.15–12.20 is now noise. Once
the next GHA run confirms PSM=6 ships clean numbers, remove:

- **`scraper/foreclosure_ocr.py`**: synthetic baseline benchmark
  (the `_pyt.image_to_string(synth)` block around line 829), and
  the per-first-record PSM-compare block (around line 1019).
- **`scraper/foreclosure_ocr.py`**: sysinfo dump
  (`/proc/cpuinfo`, `/proc/meminfo` reads). Useful one-time; just
  noise from here.
- **`.github/workflows/scrape.yml`**: `OMP_NUM_THREADS: "1"` is
  cheap insurance — leave it, but the long explanatory comment block
  can be shortened to one line.
- Per-phase timing logs in `CaptureResult` (PR 12.15) — keep
  `t_per_page` and `attempts` (operationally useful for diagnosing
  slow records), drop the rest.
- **`probe_psm_compare.py`** at repo root — keep, it's a useful
  dev tool (same as `probe_probate_auth.py`).

Do this cleanup as a single PR (call it 12.22). Do NOT bundle it
with any other change. The diff should be subtraction-only.

---

## 6. Other parked work

### Probate (re:SearchTX) — PARKED, user request

- Status: 0 records ever in production, despite credentials being
  valid (`probe_probate_auth.py` succeeds locally in 11.8s, captures
  9 cookies).
- PR 12.14 added failure-diagnostic capture: on signin timeout, it
  saves `data/probes/probate_signin_timeout_<ts>.{png,html}`, logs
  `navigator.webdriver`, scans 10 error-message selectors, and
  inventories form inputs.
- User explicitly said "WHile it runs, can we look into the probate
  problem?" then later "Keep investigating probate now" — then
  parked it to focus on OCR. The diagnostics are in place; next
  failure on GHA will produce artifacts to triage.
- Hypothesis (UNVERIFIED): probable GHA-IP-specific block — the
  re:SearchTX IdP may be tagging the GitHub Actions IP ranges as
  bot traffic. If diagnostics confirm this, options are: (a) proxy
  the probate stage through a residential IP, (b) move probate to a
  separate workflow run from a different runner, (c) skip probate
  in CI and run it manually from local on a cadence.

### Dashboard NOF-grantor display (PR 12.11) — needs visual verify

- Code change is in `dashboard/index.html`: `ownerName(rec)` helper
  picks `grantor` for NOF records and `grantee` for everything
  else, falling back to `dcad_owner`.
- Has NOT been visually verified against a real post-PR-12.21
  dataset because every recent run produced 0 OCR extractions.
- Action: after PR 12.21 produces a clean run with non-empty
  `grantor` fields on NOF records, open the dashboard and confirm
  the owner column shows the homeowner names (not the lender).

### Foreclosures list reload-on-empty (PR 12.13)

- Believed working but watch for recurrence. The publicsearch.us SPA
  has an intermittent "spinning circle on empty first page" bug
  (root cause is their backend's flaky signed-URL minting). PR 12.13
  adds a single retry on the *list* page (`/results?...`); PR 12.4
  added the same for the *document* page (`/doc/{id}`).

---

## 7. Repo / branch state

- **Default branch**: `main` (cron source for GHA scrape workflow)
- **Active feature branch**: `claude/dallas-intel-pipeline-ut2pI`
- **Main tip after PR 12.21**: `b23a4a7` (merge commit)
- **Feature tip**: `b08d99b` (PR 12.21 fix itself)
- Feature branch is fully merged into main; safe to keep branching
  from it for follow-up work, or to delete and re-branch from main.

---

## 8. Constraints the user has repeatedly enforced — DO NOT VIOLATE

The user is technical, careful, and has corrected this AI multiple times
when it overreached. The corrections are durable rules:

1. **NEVER auto-commit.** The user decides when work is ready. When
   they say commit: stage carefully (no probe artifacts, no junk
   files like `cookie_temp.txt`), show what's staged, ask for
   approval, then commit.
2. **NEVER push to a branch without explicit permission.** The
   feature branch is `claude/dallas-intel-pipeline-ut2pI`.
   Pushing to main requires explicit per-task authorization (the
   user gave it for PR 12.21 because of the chat-upload caching
   error; do not assume it for next time).
3. **Evidence-first reasoning.** Label claims as `[VERIFIED]`,
   `[STRONG INFERENCE]`, or `[UNKNOWN]`. Do not infer behavior
   from filenames; read the actual code.
4. **Audit before edit.** Read the whole file before changing it.
   Multiple times this session, partial-context edits introduced
   regressions that better reading would have caught.
5. **Never fabricate** module behavior, function signatures, or
   field names. If unsure, `grep` or `Read` first.
6. **Don't include the model identifier in commits, PR text, code
   comments, or any pushed artifact.** Chat replies only.
7. **No backwards-compatibility shims, no half-finished
   implementations, no defensive code for impossible states.**
   See the top-level system prompt's "Doing tasks" section for the
   full taste profile.

---

## 9. First action for the next session

Ask the user: "Did the GHA cron run after PR 12.21 produce the expected
~10-15 min total runtime and non-zero `extracted` counters per record?"

- If YES → proceed to §5 cleanup (PR 12.22).
- If NO with `extracted=0` on some records → §4 fallback playbook.
- If NO with capture failures (`no_images`) → that's the parked
  publicsearch.us SPA flakiness, not the OCR fix. Confirm with the
  user before changing anything; PR 12.20 diagnostics should tell
  you which records broke and why.
