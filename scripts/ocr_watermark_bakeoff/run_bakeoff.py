#!/usr/bin/env python3
"""
DE-GAN watermark-removal bake-off harness.

Usage:
    python3 run_bakeoff.py WEIGHTS.h5 img1.png [img2.png ...]

For each input image it:
  1. Runs the DE-GAN `unwatermark` generator (tiled 256x256, like enhance.py).
  2. Saves the cleaned PNG next to a results dir.
  3. OCRs BOTH the raw and the cleaned image with Tesseract.
  4. Prints the two texts side by side so we can eyeball text recovery.

This is the experiment that decides whether DE-GAN beats raw Tesseract on the
publicsearch.us "Unofficial Copy" watermark.
"""
import sys, os, types, subprocess, tempfile

# scipy.misc was removed in modern scipy; models.py imports it but doesn't use it.
try:
    import scipy.misc  # noqa
except Exception:
    sys.modules['scipy.misc'] = types.ModuleType('scipy.misc')

import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from models.models import generator_model
from utils import split2, merge_image2

OUT_DIR = "results/bakeoff"
os.makedirs(OUT_DIR, exist_ok=True)


def unwatermark(generator, deg_image_path):
    """Port of enhance.py 'unwatermark' inference path."""
    deg_image = Image.open(deg_image_path).convert('L')
    tmp = os.path.join(tempfile.gettempdir(), "curr_image.png")
    deg_image.save(tmp)
    test_image = plt.imread(tmp)

    h = ((test_image.shape[0] // 256) + 1) * 256
    w = ((test_image.shape[1] // 256) + 1) * 256
    pad = np.zeros((h, w)) + 1
    pad[:test_image.shape[0], :test_image.shape[1]] = test_image

    tiles = split2(pad.reshape(1, h, w, 1), 1, h, w)
    preds = [generator.predict(tiles[l].reshape(1, 256, 256, 1), verbose=0)
             for l in range(tiles.shape[0])]
    merged = merge_image2(np.array(preds), h, w)
    out = merged[:test_image.shape[0], :test_image.shape[1]]
    return out.reshape(out.shape[0], out.shape[1])


def ocr(img_path):
    r = subprocess.run(["tesseract", img_path, "stdout"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def main():
    weights = sys.argv[1]
    images = sys.argv[2:]
    print(f"Loading unwatermark generator + weights: {weights}")
    g = generator_model(biggest_layer=512)
    g.load_weights(weights)
    print("Weights loaded OK\n")

    for img in images:
        name = os.path.splitext(os.path.basename(img))[0]
        cleaned_path = os.path.join(OUT_DIR, f"{name}_degan.png")
        print("=" * 72)
        print(f"IMAGE: {img}")
        cleaned = unwatermark(g, img)
        plt.imsave(cleaned_path, cleaned, cmap='gray')
        print(f"cleaned -> {cleaned_path}")

        raw_txt = ocr(img)
        new_txt = ocr(cleaned_path)
        print("\n----- RAW Tesseract -----\n")
        print(raw_txt)
        print("\n----- DE-GAN -> Tesseract -----\n")
        print(new_txt)
        print()


if __name__ == "__main__":
    main()
