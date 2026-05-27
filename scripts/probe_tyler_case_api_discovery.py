"""One-shot discovery probe — find Tyler's case-detail API URL.

PR 8.4. We've established that recreating Tyler's SPA session in a
fresh Playwright context (PR 8.2 / 8.3) doesn't work — the SPA's
``/api/auth/claims`` bootstrap needs more than cookies alone. The
working pattern in this codebase is ``probate.fetch_dallas_probate``:
plain ``requests.post`` against ``/CourtRecordsSearch/search`` with the
session cookies as a ``Cookie:`` header. Server-side, stateless.

We want to do the same for case detail. Question: WHAT URL does Tyler
return case data on?

This probe:
  1. Uses ``probate_auth.live_authenticated_session`` to get a LIVE
     Playwright session that already authenticated (avoids the
     re-injection problem).
  2. Picks ONE unresolved Tyler PB record from data/records.json (or
     accepts --case-data-id on the CLI).
  3. Subscribes to every HTTP response Tyler's SPA makes.
  4. Navigates to /CourtRecordsSearch/ui/cases/{case_data_id}/details.
  5. Waits ~10s for the SPA to make its bootstrap XHR calls.
  6. Prints + dumps every research.txcourts.gov response with status,
     content-type, and a preview of the body for JSON responses.

The output answers: which URL does Tyler return case data on? Once we
know, we replace ``scraper.probate_case_detail`` with a requests-based
fetcher matching ``probate.fetch_dallas_probate``'s pattern.

Usage:
    python scripts/probe_tyler_case_api_discovery.py
    python scripts/probe_tyler_case_api_discovery.py --case-data-id <UUID>
    python scripts/probe_tyler_case_api_discovery.py --wait 15

Requires PROBATE_ENABLED + RESEARCH_TX_EMAIL + RESEARCH_TX_PASSWORD.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("probe_tyler_case_api_discovery")


DEFAULT_RECORDS = REPO_ROOT / "data" / "records.json"
DEFAULT_DUMP_DIR = REPO_ROOT / "data" / "probes" / "tyler_case_api_discovery"


def pick_target_case_data_id(records_json: Path) -> str | None:
    """Pick the first unresolved Tyler PB record_id from records.json."""
    if not records_json.exists():
        return None
    with records_json.open("r", encoding="utf-8") as f:
        env = json.load(f)
    records = env.get("records", env) if isinstance(env, dict) else env
    for r in records:
        if r.get("dallas_code") != "PB":
            continue
        if r.get("source") != "probate.txcourts.gov":
            continue
        if r.get("dcad_account"):
            continue
        rid = r.get("record_id") or ""
        if rid.startswith("pro-"):
            return rid.removeprefix("pro-")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--case-data-id", default=None,
        help="Tyler caseDataID to probe. Default: first unresolved Tyler PB "
             "in data/records.json.",
    )
    ap.add_argument(
        "--wait", type=int, default=10,
        help="Seconds to wait after nav for SPA bootstrap XHRs (default 10).",
    )
    ap.add_argument(
        "--records-json", type=Path, default=DEFAULT_RECORDS,
        help="records.json to pick the target case from "
             "(default: data/records.json)",
    )
    ap.add_argument(
        "--dump-dir", type=Path, default=DEFAULT_DUMP_DIR,
        help="Where to write capture artifacts "
             "(default: data/probes/tyler_case_api_discovery/)",
    )
    args = ap.parse_args()

    case_data_id = args.case_data_id
    if not case_data_id:
        case_data_id = pick_target_case_data_id(args.records_json)
        if not case_data_id:
            print("No --case-data-id provided and no unresolved Tyler PB "
                  "found in records.json. Pass --case-data-id <UUID>.")
            return 1
        logger.info("Using auto-picked case_data_id: %s", case_data_id)

    args.dump_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Dump dir: %s", args.dump_dir)

    # Capture every research.txcourts.gov response the SPA makes
    captures: list[dict] = []

    def on_response(response):
        try:
            url = response.url
            if "research.txcourts.gov" not in url.lower():
                return
            # Skip static assets that aren't interesting for API discovery
            url_lower = url.lower()
            if any(skip in url_lower for skip in
                   (".css", ".js", ".woff", ".png", ".jpg", ".svg", ".ico",
                    ".map", "/assets/")):
                return
            entry = {
                "url":    url,
                "status": response.status,
                "method": response.request.method if hasattr(response, "request") else "?",
                "ctype":  response.headers.get("content-type", "")
                          if hasattr(response, "headers") else "",
            }
            # Grab body preview for JSON responses — that's where the
            # case-detail payload would be.
            ctype = entry["ctype"].lower()
            if "json" in ctype and 200 <= response.status < 300:
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="replace")
                    entry["body_preview"] = text[:500]
                    entry["body_full_len"] = len(text)
                except Exception:
                    entry["body_preview"] = "<read failed>"
            captures.append(entry)
        except Exception:
            pass

    from scraper.probate_auth import live_authenticated_session

    detail_url = (
        f"https://research.txcourts.gov/CourtRecordsSearch/ui/cases/"
        f"{case_data_id}/details"
    )

    with live_authenticated_session() as (cookies, page):
        if cookies is None or page is None:
            logger.error("Auth failed; cannot proceed with discovery.")
            return 2
        logger.info("Auth succeeded; %d cookies. Subscribing to responses.",
                    len(cookies))

        page.on("response", on_response)

        logger.info("Navigating to case-detail SPA URL: %s", detail_url)
        try:
            page.goto(detail_url, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            logger.warning("Navigation timed out / errored: %s", e)

        logger.info("Settling %ds for SPA bootstrap XHRs...", args.wait)
        try:
            page.wait_for_timeout(args.wait * 1000)
        except Exception:
            pass

        # Dump rendered page text + HTML for completeness
        try:
            text = page.inner_text("body", timeout=5_000)
            (args.dump_dir / f"{case_data_id}_page.txt").write_text(
                text or "", encoding="utf-8",
            )
            logger.info("Page text len: %d", len(text or ""))
        except Exception as e:
            logger.warning("Page text extract failed: %s", e)
        try:
            html = page.content()
            (args.dump_dir / f"{case_data_id}_page.html").write_text(
                html, encoding="utf-8",
            )
        except Exception:
            pass

    # Dump captures
    captures_path = args.dump_dir / f"{case_data_id}_captures.json"
    captures_path.write_text(
        json.dumps(captures, indent=2, default=str),
        encoding="utf-8",
    )

    # Pretty-print summary
    print()
    print("=" * 78)
    print("DISCOVERY SUMMARY")
    print("=" * 78)
    print(f"case_data_id: {case_data_id}")
    print(f"navigation:   {detail_url}")
    print(f"captures:     {len(captures)}")
    print(f"saved to:     {captures_path}")
    print()

    # Group by status class
    by_status: dict[int, list[dict]] = {}
    for c in captures:
        s = c["status"]
        by_status.setdefault(s, []).append(c)
    print("By status code:")
    for s in sorted(by_status):
        print(f"  {s}: {len(by_status[s])} response(s)")
    print()

    # Highlight JSON responses — these are where the case data lives
    json_resps = [c for c in captures if "json" in c.get("ctype", "").lower()]
    print(f"JSON responses (likely API): {len(json_resps)}")
    for c in json_resps:
        url_short = c["url"].replace("https://research.txcourts.gov", "")
        print(f"  [{c['status']}] {c['method']} {url_short}")
        if c.get("body_preview"):
            preview = c["body_preview"].replace("\n", " ")[:200]
            print(f"      body: {preview}{'...' if len(c['body_preview']) > 200 else ''}")
    print()

    # Anything that returned non-2xx is a likely auth/bootstrap failure
    failures = [c for c in captures if not (200 <= c["status"] < 300)]
    print(f"Non-2xx responses (potential bootstrap failures): {len(failures)}")
    for c in failures:
        url_short = c["url"].replace("https://research.txcourts.gov", "")
        print(f"  [{c['status']}] {c['method']} {url_short}")
    print()
    print("Next step: identify which URL above returns the case-detail JSON.")
    print("Look for paths like /case/{id}, /cases/{id}, /api/cases/{id}, etc.")
    print("That URL becomes the basis for the requests-based fetcher.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
