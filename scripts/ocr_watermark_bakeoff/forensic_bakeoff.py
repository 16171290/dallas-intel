#!/usr/bin/env python3
"""Run DE-GAN unwatermark + OCR keyword recovery across the forensic cases.

Compares raw-Tesseract vs DE-GAN->Tesseract on the documented failure
keywords from OCR_WATERMARK_REMOVAL_INVESTIGATION.md Step 6.
"""
import sys, os, types, subprocess, tempfile, json

try:
    import scipy.misc  # noqa
except Exception:
    sys.modules['scipy.misc'] = types.ModuleType('scipy.misc')

import numpy as np
from PIL import Image
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from models.models import generator_model
from utils import split2, merge_image2

BASE = "forensic/ocr_forensic"
OUT = "results/forensic"; os.makedirs(OUT, exist_ok=True)

# record_id -> (label, [keywords]) from the MD Step 6 table
CASES = {
    "315561589": ("Montgomery", ["NICOLE", "BEAUMONT"]),
    "315561585": ("Cox",        ["LAURA", "COX", "CREST RIDGE"]),
    "315561578": ("Battie",     ["BATTIE", "MARLENE"]),
    "315561574": ("Betancourt", ["BETANCOURT", "VANGUARD"]),
    "315561570": ("Velasquez",  ["NOHEMY", "GRANADOS", "GRENOBLE"]),
    "315562554": ("Liable",     ["LIABLE"]),
    "315562562": ("Flores",     ["FLORES", "MATHIS"]),
}


def unwatermark(g, path):
    im = Image.open(path).convert('L')
    tmp = os.path.join(tempfile.gettempdir(), "cw.png"); im.save(tmp)
    t = plt.imread(tmp)
    h = ((t.shape[0]//256)+1)*256; w = ((t.shape[1]//256)+1)*256
    pad = np.zeros((h, w))+1; pad[:t.shape[0], :t.shape[1]] = t
    tiles = split2(pad.reshape(1, h, w, 1), 1, h, w)
    preds = [g.predict(tiles[l].reshape(1, 256, 256, 1), verbose=0) for l in range(tiles.shape[0])]
    m = merge_image2(np.array(preds), h, w)[:t.shape[0], :t.shape[1]]
    return m.reshape(m.shape[0], m.shape[1])


def ocr(path):
    return subprocess.run(["tesseract", path, "stdout"], capture_output=True, text=True).stdout.upper()


def main():
    g = generator_model(biggest_layer=512)
    g.load_weights("weights/watermark_rem_weights.h5")
    results = {}
    for rid, (label, kws) in CASES.items():
        page = f"{BASE}/{rid}/page_01.png"
        if not os.path.exists(page):
            continue
        clean_path = f"{OUT}/{rid}_degan.png"
        cleaned = unwatermark(g, page)
        plt.imsave(clean_path, cleaned, cmap='gray')
        raw, new = ocr(page), ocr(clean_path)
        row = []
        for kw in kws:
            row.append((kw, kw in raw, kw in new))
        results[rid] = (label, row)
        print(f"\n=== {label} ({rid}) ===")
        for kw, r, n in row:
            flag = "WIN" if (n and not r) else ("REGRESS" if (r and not n) else "")
            print(f"  {kw:14s} raw={'Y' if r else 'n'}  degan={'Y' if n else 'n'}  {flag}")
    # tally
    wins = sum(1 for _,row in results.values() for _,r,n in row if n and not r)
    regr = sum(1 for _,row in results.values() for _,r,n in row if r and not n)
    print(f"\nTOTAL keyword wins (degan recovered, raw missed): {wins}")
    print(f"TOTAL keyword regressions (raw had, degan lost):  {regr}")
    json.dump({rid:(lbl,[[k,r,n] for k,r,n in row]) for rid,(lbl,row) in results.items()},
              open(f"{OUT}/summary.json","w"), indent=2)


if __name__ == "__main__":
    main()
