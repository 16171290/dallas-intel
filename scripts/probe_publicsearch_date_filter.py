"""Diagnostic probe — why did publicsearch.us's date-range filter stop
constraining results?

The 2026-05-29 daily scrape asked for the last 7 days (DAYS_BACK=7) but
ingested records dated 2020-2026 (~2150 days). scraper/publicsearch.py
_fill_date_range fills #recordedDateRange-start / -end and presses Escape,
with NO verification that the values stuck. When the fill silently no-ops,
publicsearch falls back to a default ~6-year window and we ingest
everything. No scraper code changed before the flood, so it's an external
DOM/behavior change on publicsearch.us OR a latent race in the fill.

This probe observes the LIVE advanced-search date widget to pin the exact
failure. It reuses the real publicsearch.py functions (browser_context,
_open_home, _open_advanced_search, _fill_date_range) so we test the actual
code path. Two passes:

  PASS 1 — call the REAL _fill_date_range, then read both inputs back.
           This is the production end-state: do the inputs hold our dates?

  PASS 2 — replicate _fill_date_range step-by-step on a fresh form, reading
           the input value after EACH operation, to isolate where it
           breaks: do the selectors exist? does .fill() stick? does the
           Escape keypress clear the value?

Saves a screenshot + the date-widget HTML so we can see the current DOM.
Measurement only; performs no search ingestion.

Usage:
    python scripts/probe_publicsearch_date_filter.py
    python scripts/probe_publicsearch_date_filter.py --days-back 7

Requires PUBLICSEARCH machinery (Playwright + network). No login.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("probe_publicsearch_date_filter")

DEFAULT_DUMP_DIR = REPO_ROOT / "data" / "probes" / "publicsearch_date_filter"

# The selectors _fill_date_range targets (publicsearch.py:258-260).
SEL_START = "#recordedDateRange-start"
SEL_END = "#recordedDateRange-end"
FMT = "%m/%d/%Y"


def read_value(page, selector: str) -> str:
    """Read an input's CURRENT value (the .value property, not the static
    HTML attribute — React-controlled inputs differ)."""
    try:
        loc = page.locator(selector)
        if loc.count() == 0:
            return "<SELECTOR NOT FOUND>"
        try:
            return loc.first.input_value(timeout=2_000)
        except Exception:
            # Fallback: read the DOM property directly.
            return loc.first.evaluate("el => el.value")
    except Exception as e:
        return f"<READ ERROR: {e}>"


def selector_exists(page, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-back", type=int, default=7,
                    help="Window size to fill (default 7, matching DAYS_BACK)")
    ap.add_argument("--dump-dir", type=Path, default=DEFAULT_DUMP_DIR)
    args = ap.parse_args()

    args.dump_dir.mkdir(parents=True, exist_ok=True)

    today = date.today()
    date_from = today - timedelta(days=args.days_back)
    date_to = today
    want_start = date_from.strftime(FMT)
    want_end = date_to.strftime(FMT)
    logger.info("Target window: %s .. %s", want_start, want_end)

    from scraper.publicsearch import (
        browser_context, _open_home, _open_advanced_search, _fill_date_range,
    )

    report: list[str] = []

    def note(line: str):
        print(line)
        report.append(line)

    with browser_context() as (_browser, _context, page):
        # ───────────────────────── PASS 1 ─────────────────────────
        note("=" * 78)
        note("PASS 1 — production code path (real _fill_date_range)")
        note("=" * 78)
        _open_home(page)
        _open_advanced_search(page)

        note(f"selector {SEL_START!r} count: {selector_exists(page, SEL_START)}")
        note(f"selector {SEL_END!r} count:   {selector_exists(page, SEL_END)}")
        note(f"initial start value: {read_value(page, SEL_START)!r}")
        note(f"initial end value:   {read_value(page, SEL_END)!r}")

        fill_error = None
        try:
            _fill_date_range(page, date_from, date_to)
        except Exception as e:
            fill_error = repr(e)
        note(f"_fill_date_range raised: {fill_error}")
        got_start = read_value(page, SEL_START)
        got_end = read_value(page, SEL_END)
        note(f"AFTER fill — start: {got_start!r}  (wanted {want_start!r})")
        note(f"AFTER fill — end:   {got_end!r}  (wanted {want_end!r})")
        ok1 = (got_start == want_start and got_end == want_end)
        note(f"PASS 1 verdict: dates {'STUCK ✓' if ok1 else 'DID NOT STICK ✗'}")

        try:
            page.screenshot(path=str(args.dump_dir / "pass1_after_fill.png"))
        except Exception as e:
            note(f"(screenshot failed: {e})")

        # ───────────────────────── PASS 2 ─────────────────────────
        note("")
        note("=" * 78)
        note("PASS 2 — step-by-step isolation (replicating _fill_date_range)")
        note("=" * 78)
        _open_home(page)
        _open_advanced_search(page)

        for selector, value, label in (
            (SEL_START, want_start, "start"),
            (SEL_END, want_end, "end"),
        ):
            if selector_exists(page, selector) <= 0:
                note(f"[{label}] SELECTOR MISSING: {selector!r} — DOM changed")
                continue
            try:
                page.locator(selector).click()
                note(f"[{label}] after click:        {read_value(page, selector)!r}")
                page.locator(selector).press("ControlOrMeta+a")
                page.locator(selector).fill(value)
                note(f"[{label}] after fill({value!r}): {read_value(page, selector)!r}")
            except Exception as e:
                note(f"[{label}] step error: {e!r}")

        # Isolate the Escape effect — _fill_date_range presses Escape to
        # dismiss the calendar overlay; test whether it also clears the value.
        before_esc_start = read_value(page, SEL_START)
        before_esc_end = read_value(page, SEL_END)
        try:
            page.keyboard.press("Escape")
        except Exception as e:
            note(f"(escape press failed: {e})")
        after_esc_start = read_value(page, SEL_START)
        after_esc_end = read_value(page, SEL_END)
        note(f"start: before Escape {before_esc_start!r} -> after {after_esc_start!r}")
        note(f"end:   before Escape {before_esc_end!r} -> after {after_esc_end!r}")
        esc_cleared = (before_esc_start and not after_esc_start) or \
                      (before_esc_end and not after_esc_end)
        note(f"Escape cleared the value: {'YES — that is the bug' if esc_cleared else 'no'}")

        # Dump the date-widget HTML region for inspection.
        try:
            html = page.content()
            (args.dump_dir / "advanced_search.html").write_text(html, encoding="utf-8")
            note(f"page HTML saved to {args.dump_dir / 'advanced_search.html'}")
        except Exception as e:
            note(f"(html dump failed: {e})")
        try:
            page.screenshot(path=str(args.dump_dir / "pass2_after_escape.png"))
        except Exception:
            pass

    (args.dump_dir / "_report.txt").write_text("\n".join(report), encoding="utf-8")
    print()
    print("Diagnosis guide:")
    print("  - SELECTOR NOT FOUND / count 0  -> publicsearch renamed the date")
    print("    inputs; update SEL_START/SEL_END in _fill_date_range.")
    print("  - fill sticks in PASS 2 but Escape clears it -> drop/replace the")
    print("    Escape keypress (e.g. click elsewhere to dismiss the calendar).")
    print("  - fill never sticks even before Escape -> React-controlled input")
    print("    needs a different commit (type() + blur, or press Enter).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
