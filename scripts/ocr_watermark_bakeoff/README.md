# OCR watermark bake-off harness

Experimental scripts from the 2026-05-29 follow-up documented in
[`docs/OCR_WATERMARK_REMOVAL_INVESTIGATION.md`](../../docs/OCR_WATERMARK_REMOVAL_INVESTIGATION.md)
(see the "Addendum" section). They were used to test whether the DE-GAN
document-enhancement GAN recovers the publicsearch.us "Unofficial Copy"
watermark better than raw Tesseract.

**Conclusion: it does not.** Net 0 wins / 1 regression on the forensic
case set — no better than the cv2 inpainting the original investigation
deferred. Kept here for reproducibility only. The path forward is
context-aware extraction (vision-LLM / cloud OCR), not watermark removal.

## Scripts

- `run_bakeoff.py WEIGHTS.h5 img1.png [img2.png ...]`
  Runs DE-GAN `unwatermark` on each image, then OCRs the raw and cleaned
  versions and prints both texts side by side.

- `forensic_bakeoff.py`
  Runs DE-GAN across the forensic case set (`forensic/ocr_forensic/`) and
  reports keyword recovery (raw vs DE-GAN) with a win/regression tally,
  matching the Step 6 table in the investigation doc.

## Setup (not self-contained — depends on the external DE-GAN repo)

```bash
git clone https://github.com/dali92002/DE-GAN && cd DE-GAN
pip install "tensorflow-cpu>=2.15,<2.17" pillow matplotlib scipy numpy
# DE-GAN weights are GPL-3.0 / academic-use; fetch watermark_rem_weights.h5
# from the upstream repo's Google Drive link into ./weights/
# tesseract must be on PATH (apt-get install tesseract-ocr)
cp /path/to/this/repo/scripts/ocr_watermark_bakeoff/*.py .
python3 run_bakeoff.py weights/watermark_rem_weights.h5 some_page.png
```

The model code is written against `tensorflow.keras`, so it loads on
modern TF2 once the removed `scipy.misc` import is stubbed (the scripts
do this automatically).
