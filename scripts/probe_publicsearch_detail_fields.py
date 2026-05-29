"""Information-gathering probe — catalog EVERYTHING on a publicsearch
/doc/{id} detail page.

Reframe (2026-05-29): the flood records are not leads — they are samples
for optimizing how accurately we extract name / address / legal info from
source documents. Our scraper today reads only the list-view table (no
street address) and, separately, Stage 6.6 pulls ONLY the "Property
Address" field off the detail page as a tiebreaker. So we have never
catalogued what ELSE the /doc/{id} Summary panel exposes.

This probe answers "what information is actually available to gather?" by
dumping the COMPLETE detail page for a sample of records:
  - every <dt>/<dd> Summary-panel field label + value
  - the full rendered body text
  - saved per-record so we can diff a "N/A | N/A" record (list view gave
    us nothing) against a record that DID resolve.

It mutates nothing. It tells us whether the address / legal description we
lack is sitting on the detail page unharvested, and what field labels to
target if so.

Usage:
    python scripts/probe_publicsearch_detail_fields.py
    python scripts/probe_publicsearch_detail_fields.py --na-samples 8 --resolved-samples 4
    python scripts/probe_publicsearch_detail_fields.py --record-ids 72090170 69809596

Requires PUBLICSEARCH_ENABLED machinery (Playwright + network to
publicsearch.us). No login needed — the doc pages are public.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("probe_publicsearch_detail_fields")

DEFAULT_RECORDS = REPO_ROOT / "data" / "records.json"
DEFAULT_DUMP_DIR = REPO_ROOT / "data" / "probes" / "publicsearch_detail_fields"


def load_records(path: Path) -> list[dict]:
    env = json.load(path.open("r", encoding="utf-8"))
    return env.get("records", env) if isinstance(env, dict) else env


def is_na_excerpt(r: dict) -> bool:
    raw = (r.get("raw_excerpt") or "").strip().upper()
    return raw in ("", "N/A | N/A", "N/A", "N/A | N/A |")


def pick_samples(records: list[dict], na_n: int, resolved_n: int) -> list[dict]:
    """Pick PB records with 'N/A | N/A' list-view excerpt (the gap), plus a
    few that DID resolve via legal description (the control group)."""
    ps = [r for r in records if r.get("source") == "publicsearch.us"]
    na_pb = [r for r in ps
             if r.get("dallas_code") == "PB" and is_na_excerpt(r)][:na_n]
    resolved = [r for r in ps
                if r.get("dcad_account") and not is_na_excerpt(r)][:resolved_n]
    return na_pb + resolved


def dump_summary_fields(page) -> dict:
    """Extract every <dt>/<dd> pair on the page (the Summary panel layout),
    plus any label/value rows rendered as adjacent elements."""
    try:
        return page.evaluate(
            """() => {
                const out = {};
                // Standard definition-list layout
                document.querySelectorAll('dt').forEach(dt => {
                    const label = (dt.innerText || '').trim();
                    const dd = dt.nextElementSibling;
                    const value = dd ? (dd.innerText || '').trim() : '';
                    if (label) out[label] = value;
                });
                return out;
            }"""
        ) or {}
    except Exception as e:
        logger.debug("summary-field extract failed: %s", e)
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records-json", type=Path, default=DEFAULT_RECORDS)
    ap.add_argument("--dump-dir", type=Path, default=DEFAULT_DUMP_DIR)
    ap.add_argument("--na-samples", type=int, default=8,
                    help="How many 'N/A | N/A' PB records to dump (the gap)")
    ap.add_argument("--resolved-samples", type=int, default=4,
                    help="How many already-resolved records to dump (control)")
    ap.add_argument("--record-ids", nargs="+", default=None,
                    help="Explicit record_ids to dump instead of auto-pick")
    args = ap.parse_args()

    records = load_records(args.records_json)
    by_id = {r.get("record_id"): r for r in records}

    if args.record_ids:
        targets = [by_id.get(rid, {"record_id": rid}) for rid in args.record_ids]
    else:
        targets = pick_samples(records, args.na_samples, args.resolved_samples)
    if not targets:
        print("No targets found.")
        return 1

    args.dump_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Dumping %d detail pages to %s", len(targets), args.dump_dir)

    from scraper import config
    from scraper.page_fetcher import PageFetcher

    base = config.PUBLICSEARCH_BASE
    label_counter: Counter = Counter()
    per_record: list[dict] = []

    with PageFetcher() as fetcher:
        page = fetcher._ensure_page()
        if page is None:
            logger.error("Playwright page init failed — cannot fetch.")
            return 2

        for rec in targets:
            rid = rec.get("record_id")
            url = f"{base}/doc/{rid}"
            logger.info("Fetching %s", url)
            fields, body = {}, ""
            try:
                page.goto(url, wait_until="networkidle", timeout=25_000)
                page.wait_for_timeout(1500)  # let Summary panel hydrate
                fields = dump_summary_fields(page)
                body = page.inner_text("body", timeout=5_000) or ""
            except Exception as e:
                logger.warning("fetch failed for %s: %s", rid, e)

            for label in fields:
                label_counter[label] += 1

            # Save per-record artifacts
            (args.dump_dir / f"{rid}_fields.json").write_text(
                json.dumps(fields, indent=2, ensure_ascii=False), encoding="utf-8")
            (args.dump_dir / f"{rid}_body.txt").write_text(body, encoding="utf-8")

            per_record.append({
                "record_id": rid,
                "dallas_code": rec.get("dallas_code"),
                "list_view_excerpt": rec.get("raw_excerpt"),
                "list_view_grantor_raw": rec.get("grantor_raw"),
                "resolved_account": rec.get("dcad_account"),
                "n_summary_fields": len(fields),
                "field_labels": list(fields.keys()),
                "fields": fields,
            })

    # ---- Report ----
    print()
    print("=" * 78)
    print("PUBLICSEARCH /doc/ DETAIL-PAGE FIELD CATALOG")
    print("=" * 78)
    print(f"records dumped: {len(targets)}")
    print(f"artifacts:      {args.dump_dir}/{{record_id}}_fields.json + _body.txt")
    print()

    print("FIELD LABELS seen across all sampled detail pages (count = how many"
          " pages had it):")
    for label, n in label_counter.most_common():
        print(f"  {n:>3}/{len(targets)}  {label!r}")
    print()

    for pr in per_record:
        tag = "N/A-excerpt" if is_na_excerpt({"raw_excerpt": pr["list_view_excerpt"]}) \
              else "has-excerpt"
        print(f"── [{pr['dallas_code']}/{tag}] {pr['record_id']}  "
              f"({pr['n_summary_fields']} fields)")
        print(f"     list-view excerpt: {pr['list_view_excerpt']!r}")
        print(f"     grantor_raw:       {pr['list_view_grantor_raw']!r}")
        for label, value in pr["fields"].items():
            v = (value or "").replace("\n", " / ")
            if len(v) > 90:
                v = v[:90] + "..."
            print(f"       {label}: {v}")
        print()

    print("Interpretation: any field carrying a street address or full legal")
    print("description that the list view marked 'N/A' is an unharvested source.")
    print("Compare the N/A-excerpt PB pages against the has-excerpt control to")
    print("see exactly what the detail page exposes that we are not capturing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
