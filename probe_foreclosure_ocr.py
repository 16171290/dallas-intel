"""End-to-end recon probe for the foreclosure-OCR pipeline.

For ONE foreclosure record:
  1. Open /doc/{record_id} in Playwright.
  2. Intercept every PNG response matching /files/documents/.../images/*.png
     (Playwright captures the response body, so the signed URL's expiry
     doesn't matter — we already have the bytes).
  3. Detect total page count from the viewer DOM.
  4. If not all pages have loaded passively, drive the viewer (ArrowRight)
     to force the remaining pages to fetch.
  5. Save every captured PNG to data/probes/ocr_{record_id}/.
  6. Run Tesseract OCR on each.
  7. Run the concatenated OCR text through
     scraper.foreclosure_pdfs._parse_fields_into_record (which already
     extracts Grantor, Trustee, Sale Date, Loan Amount from this exact
     notice format).
  8. Pull out Legal Description via a focused regex.
  9. Print everything.

Usage:
  python probe_foreclosure_ocr.py 315566857       # Walker doc, 2 pages
  python probe_foreclosure_ocr.py 315561571       # 2002 Berkley, 4 pages
  python probe_foreclosure_ocr.py                 # default: 315566857

Setup (one-time):
  Install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
  pip install pytesseract Pillow playwright
  playwright install chromium

Detection order for the Tesseract binary:
  1. TESSERACT_CMD env var
  2. tesseract on $PATH
  3. C:\\Program Files\\Tesseract-OCR\\tesseract.exe
  4. /usr/bin/tesseract, /usr/local/bin/tesseract, /opt/homebrew/bin/tesseract
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _try_navigate_next(page, target_page: int) -> str:
    """Try to advance the publicsearch.us viewer to the next document page.

    The viewer's pagination toolbar contains 4 nav buttons + a page-number
    input + an "of N" label. Both prev (◁) and next (▷) buttons use the
    same icon — operator-confirmed source:
        /img/icon_jump_triangle.{hash}.svg
    The hash changes across deploys; we match on the filename prefix only.

    Prev/next differ by visual rotation and by being enabled/disabled at
    the boundaries (prev disabled on page 1; next disabled on page N).
    Strategy: filter to ENABLED buttons matching that icon, take the LAST
    one — that's always the next-page button when not at the final page.

    Returns the name of the strategy that didn't raise. The caller wraps
    the click in page.expect_response so timing/race issues with the
    SPA's image-fetch are eliminated.
    """
    strategies: list[tuple[str, callable]] = [
        # PRIMARY — operator-confirmed exact aria-label (2026-05-21 diagnostic
        # enumeration). publicsearch.us viewer uses literal "Go To Next Page".
        ("button[aria-label='Go To Next Page']",
            lambda: page.locator("button[aria-label='Go To Next Page']").click(timeout=3000)),

        # Fallback variants in case the label or markup drifts.
        ("button[aria-label*='Next Page' i]",
            lambda: page.locator("button[aria-label*='Next Page' i]").first.click(timeout=2500)),
        ("button[aria-label*='Next' i]",
            lambda: page.locator("button[aria-label*='Next' i]").first.click(timeout=2500)),
        ("get_by_role(button, name=re'next page')",
            lambda: page.get_by_role("button", name=re.compile(r"next\s*page", re.I)).first.click(timeout=2000)),

        # Icon-based fallback (operator-supplied SVG asset).
        ("button:has(img[src*='icon_jump_triangle']):last",
            lambda: page.locator(
                "button:not([disabled]):not([aria-disabled='true'])"
                ":has(img[src*='icon_jump_triangle'])"
            ).last.click(timeout=2500)),

        # Last-resort fallbacks
        ("page-number input fill+Enter",
            lambda: _fill_page_input(page, target_page)),
        ("keyboard ArrowRight on body",
            lambda: (page.locator("body").click(timeout=1000), page.keyboard.press("ArrowRight"))),
    ]
    for name, fn in strategies:
        try:
            fn()
            return name
        except Exception:
            continue
    return "NONE"


def _fill_page_input(page, target_page: int) -> None:
    """Find the page-number text input and fill it with the target page."""
    candidates = [
        "input[aria-label*='page number' i]",
        "input[aria-label*='page' i][type='text']",
        "input[type='text']",
        "input[type='number']",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        if loc.count() > 0:
            loc.click(timeout=1000)
            loc.fill(str(target_page))
            page.keyboard.press("Enter")
            return
    raise RuntimeError("no page-number input found")


def _dump_buttons(page) -> None:
    """Diagnostic: log up to 30 button/role=button elements with their
    text and aria-label so we can identify the right selector if nav
    keeps failing."""
    print(f"\n  [DIAGNOSTIC] enumerating up to 30 buttons on the page:")
    try:
        buttons = page.locator("button, [role='button']").all()
        for i, b in enumerate(buttons[:30]):
            try:
                text = (b.inner_text(timeout=500) or "").strip()[:50]
                aria = b.get_attribute("aria-label") or ""
                title = b.get_attribute("title") or ""
                if text or aria or title:
                    print(f"    [{i}] text={text!r}  aria-label={aria!r}  title={title!r}")
            except Exception:
                pass
    except Exception as e:
        print(f"    button enumeration failed: {e}")


def find_tesseract() -> Optional[str]:
    """Locate the Tesseract binary across platforms."""
    if cmd := os.environ.get("TESSERACT_CMD"):
        if Path(cmd).exists():
            return cmd
    if which := shutil.which("tesseract"):
        return which
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "record_id", nargs="?", default="315566857",
        help="publicsearch.us vendor doc id (default: 315566857 = Walker)",
    )
    ap.add_argument(
        "--passive-wait", type=float, default=3.0,
        help="Seconds to wait after initial page load before trying to navigate (default 3)",
    )
    ap.add_argument(
        "--per-page-wait", type=float, default=2.5,
        help="Seconds to wait after each page-nav action (default 2.5)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    record_id = args.record_id
    print("=" * 70)
    print(f"OCR probe for record_id={record_id}")
    print("=" * 70)

    # ---------- dependency checks ----------
    tess_path = find_tesseract()
    if not tess_path:
        print("\nERROR: Tesseract not found.")
        print("Install: https://github.com/UB-Mannheim/tesseract/wiki")
        print("Or set TESSERACT_CMD env var to the tesseract.exe path.")
        return 1
    print(f"  Tesseract:  {tess_path}")

    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        print(f"\nERROR: missing {e.name}. Run: pip install pytesseract Pillow")
        return 1
    pytesseract.pytesseract.tesseract_cmd = tess_path

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError:
        print("\nERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
        return 1

    from scraper import config
    from scraper.foreclosure_pdfs import ForeclosureRecord, _parse_fields_into_record

    out_dir = config.DATA_DIR / "probes" / f"ocr_{record_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Output dir: {out_dir}")

    # ---------- capture stage ----------
    print(f"\n[1/5] Opening /doc/{record_id} ...")
    # Bodies captured by page number. Read inline via response.body() while
    # the Playwright page is still alive -- avoids both the close() race
    # and the bot-rejection issue we hit with plain requests.get earlier.
    unique: dict[int, bytes] = {}

    def on_response_initial(response):
        """Handler for the initial passive load: pages that arrive without
        active navigation. body() succeeds because the page is still active."""
        url = response.url
        m = re.search(r"_(\d+)\.png", url)
        if not (m and "/files/documents/" in url and ".png" in url):
            return
        pn = int(m.group(1))
        if pn in unique:
            return
        try:
            body = response.body()
            if body and len(body) > 100:  # guard against error pages
                unique[pn] = body
                logger.info(f"  passively captured page {pn}: {len(body):,} bytes")
        except Exception as e:
            logger.warning(f"  passive body() failed for page {pn}: {e}")

    total_pages: Optional[int] = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        page.on("response", on_response_initial)

        url = f"{config.PUBLICSEARCH_BASE}/doc/{record_id}"
        page.goto(url, wait_until="networkidle", timeout=30_000)

        print(f"  Passive wait {args.passive_wait}s for initial fetches ...")
        time.sleep(args.passive_wait)
        print(f"  After passive wait: {len(unique)} page(s) captured")

        # Detect total page count from any "of N" text in the body.
        try:
            body_text = page.locator("body").inner_text()
            m = re.search(r"\bof\s+(\d+)\b", body_text)
            if m:
                total_pages = int(m.group(1))
        except Exception as e:
            logger.warning(f"  could not detect total page count: {e}")
        print(f"  Total pages reported by viewer: {total_pages or 'unknown'}")

        # For each remaining page: click the next-page button and wait
        # for the matching image response. expect_response actively
        # waits, so we don't have to guess timing -- click triggers the
        # SPA's image fetch, and body() runs while the page is still
        # alive (no close() race).
        if total_pages and len(unique) < total_pages:
            print(f"\n[2/5] Need {total_pages - len(unique)} more page(s); clicking through ...")

            for target_page in range(2, total_pages + 1):
                if target_page in unique:
                    continue
                url_pat = re.compile(rf"/files/documents/\d+/images/\d+_{target_page}\.png")
                try:
                    with page.expect_response(
                        lambda r, pat=url_pat: bool(pat.search(r.url)),
                        timeout=15_000,
                    ) as response_info:
                        strategy_used = _try_navigate_next(page, target_page)
                    response = response_info.value
                    body = response.body()
                    if body and len(body) > 100:
                        unique[target_page] = body
                        print(f"  page {target_page}: captured via [{strategy_used}], {len(body):,} bytes")
                    else:
                        print(f"  page {target_page}: response body empty/tiny ({len(body) if body else 0} bytes)")
                except PlaywrightTimeoutError:
                    print(f"  page {target_page}: TIMEOUT (15s) waiting for image response")
                    if target_page == 2:  # diagnose only once
                        _dump_buttons(page)
                except Exception as e:
                    print(f"  page {target_page}: FAILED: {e}")
                    if target_page == 2:
                        _dump_buttons(page)
        else:
            if total_pages:
                print(f"  All {total_pages} pages already captured passively")

        page.close()
        context.close()
        browser.close()

    if not unique:
        print("\nERROR: no PNG bodies captured. SPA never fetched any document images.")
        return 1

    print(f"\n[3/5] Saving {len(unique)} unique page(s)")
    saved_paths: dict[int, Path] = {}
    for pn in sorted(unique.keys()):
        body = unique[pn]
        path = out_dir / f"page_{pn}.png"
        path.write_bytes(body)
        saved_paths[pn] = path
        print(f"  page {pn} → {path} ({len(body):,} bytes)")

    # ---------- OCR ----------
    print(f"\n[4/5] Running Tesseract OCR ({len(saved_paths)} pages) ...")
    page_texts: dict[int, str] = {}
    for pn in sorted(saved_paths.keys()):
        path = saved_paths[pn]
        t0 = time.time()
        try:
            img = Image.open(path)
            text = pytesseract.image_to_string(img)
            page_texts[pn] = text
            print(f"  page {pn}: {len(text):,} chars  ({time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"  page {pn}: OCR FAILED: {e}")

    concatenated = "\n\n".join(page_texts[p] for p in sorted(page_texts.keys()))

    text_path = out_dir / "ocr_text.txt"
    text_path.write_text(concatenated, encoding="utf-8")
    print(f"  combined text → {text_path} ({len(concatenated):,} chars)")

    # ---------- parse ----------
    print(f"\n[5/5] Extracting fields from OCR text")
    print("-" * 70)

    # Reuse the existing parser from the legacy PDF pipeline -- same notice format.
    rec = ForeclosureRecord(source_pdf=f"ocr_{record_id}")
    try:
        _parse_fields_into_record(rec, concatenated)
    except Exception as e:
        print(f"  _parse_fields_into_record FAILED: {e}")

    print(f"  debtor (grantor)         = {rec.debtor!r}")
    print(f"  trustee                  = {rec.trustee!r}")
    print(f"  sale_date                = {rec.sale_date!r}")
    print(f"  sale_date_iso            = {rec.sale_date_iso!r}")
    print(f"  notice_date              = {rec.notice_date!r}")
    print(f"  notice_date_iso          = {rec.notice_date_iso!r}")
    print(f"  notice_date_pattern      = {rec.notice_date_pattern!r}")
    print(f"  property_address         = {rec.property_address!r}")
    print(f"  original_loan_amount     = {rec.original_loan_amount!r}")
    print(f"  parse_warnings           = {rec.parse_warnings}")

    # Focused regex for the notice-specific "Grantor(s):" line (the existing
    # _MAKER_RE catches "Grantor" but we want to see the raw match too).
    print(f"\n  Additional field extraction:")
    grantor_m = re.search(
        r"Grantor\(?s?\)?[\s:]+([^\n\r]+)",
        concatenated, re.IGNORECASE,
    )
    if grantor_m:
        print(f"  grantor_line             = {grantor_m.group(1).strip()!r}")

    legal_m = re.search(
        r"Legal\s+Description[\s:]+(.*?)(?:\n\s*\n|Whereas|Date\s+of\s+Sale|Earliest)",
        concatenated, re.IGNORECASE | re.DOTALL,
    )
    if legal_m:
        legal = re.sub(r"\s+", " ", legal_m.group(1).strip())[:500]
        print(f"  legal_description        = {legal!r}")

    amount_m = re.search(
        r"Amount[\s:$]+([\d,]+(?:\.\d{2})?)",
        concatenated, re.IGNORECASE,
    )
    if amount_m:
        print(f"  amount_line              = {amount_m.group(1).strip()!r}")

    deed_m = re.search(
        r"Deed\s+of\s+Trust\s+Dated[\s:]+([^\n\r]+)",
        concatenated, re.IGNORECASE,
    )
    if deed_m:
        print(f"  deed_of_trust_dated      = {deed_m.group(1).strip()!r}")

    mortgagee_m = re.search(
        r"Current\s+Mortgagee[\s:]+([^\n\r]+)",
        concatenated, re.IGNORECASE,
    )
    if mortgagee_m:
        print(f"  current_mortgagee        = {mortgagee_m.group(1).strip()!r}")

    print()
    print("=" * 70)
    print(f"Probe complete. {len(saved_paths)} page(s) captured + OCR'd.")
    print(f"Raw OCR text:  {text_path}")
    print(f"PNG images:    {out_dir}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
