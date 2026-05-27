"""Multi-case discovery probe — does Tyler's case-detail JSON carry the
decedent's property address?

PR 8.4 followup. The first round of probing identified the case-detail
APIs:
  - ``GET  /CourtRecordsSearch/case/{caseDataID}``         (case header)
  - ``POST /CourtRecordsSearch/case/{caseDataID}/events``  (filings list)

But it only captured the first 500 chars of each body, and only against
ONE case. The real question for PR 8 is: **is the decedent's property
address present in any of these JSON bodies?** If yes, PR 8 ships as a
small extractor. If no, the only remaining path is downloading +
OCR'ing the Application / Inventory PDFs.

This probe:
  1. Picks N unresolved Tyler PB records from data/records.json
     (default 5, controlled by --limit). Some decedents won't own
     property at all, so we test across multiple cases.
  2. Uses ``probate_auth.live_authenticated_session`` to authenticate
     ONCE and reuse the session for all N cases.
  3. For each case: navigates to /ui/case/{caseDataID}, captures every
     research.txcourts.gov XHR response, and saves each JSON body to
     its OWN file (full body, no truncation).
  4. After all cases done, recursively scans every captured JSON body
     for any key whose name matches address/street/city/state/zip/etc.
     Prints a finding table per case.

If address-like fields appear consistently across cases, we have our
answer with zero new infrastructure — just extract the field in PR 8.5.

Usage:
    python scripts/probe_tyler_case_api_discovery.py
    python scripts/probe_tyler_case_api_discovery.py --limit 10
    python scripts/probe_tyler_case_api_discovery.py --case-data-ids <UUID1> <UUID2>
    python scripts/probe_tyler_case_api_discovery.py --wait 15 --limit 8

Requires PROBATE_ENABLED + RESEARCH_TX_EMAIL + RESEARCH_TX_PASSWORD.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
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

# Keys likely to indicate an address-bearing field.
# Conservative: matches whole words or substrings within key names.
ADDRESS_KEY_RE = re.compile(
    r"address|street|city|state|zip|postal|residence|mailing|location",
    re.IGNORECASE,
)


def pick_target_case_data_ids(records_json: Path, limit: int) -> list[str]:
    """Pick up to N unresolved Tyler PB record_ids from records.json."""
    if not records_json.exists():
        return []
    with records_json.open("r", encoding="utf-8") as f:
        env = json.load(f)
    records = env.get("records", env) if isinstance(env, dict) else env
    picks: list[str] = []
    for r in records:
        if r.get("dallas_code") != "PB":
            continue
        if r.get("source") != "probate.txcourts.gov":
            continue
        if r.get("dcad_account"):
            continue
        rid = r.get("record_id") or ""
        if rid.startswith("pro-"):
            picks.append(rid.removeprefix("pro-"))
            if len(picks) >= limit:
                break
    return picks


def url_to_slug(url: str) -> str:
    """Convert a URL path into a filesystem-safe slug for dump files."""
    path = url.replace("https://research.txcourts.gov", "")
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "_", path).strip("_")
    return slug[:120] or "root"


def find_address_fields(obj, path: str = "") -> list[tuple[str, object]]:
    """Recursively walk obj (dict/list/scalar). Return [(json_path, value)]
    for every dict-key whose NAME matches ADDRESS_KEY_RE, regardless of
    where it appears in the tree."""
    hits: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if isinstance(k, str) and ADDRESS_KEY_RE.search(k):
                # Record the field as a hit. Only record scalars / short
                # strings as values; for nested objects/arrays we still
                # descend so child address-fields are also reported.
                if isinstance(v, (str, int, float, bool)) or v is None:
                    hits.append((new_path, v))
                else:
                    hits.append((new_path, f"<{type(v).__name__}>"))
            hits.extend(find_address_fields(v, new_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(find_address_fields(item, f"{path}[{i}]"))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--case-data-ids", nargs="+", default=None,
        help="Specific Tyler caseDataIDs to probe (space-separated). "
             "Default: auto-pick first --limit unresolved Tyler PBs from "
             "records.json.",
    )
    ap.add_argument(
        "--limit", type=int, default=5,
        help="When auto-picking from records.json, how many cases to probe "
             "(default 5). Ignored if --case-data-ids is provided.",
    )
    ap.add_argument(
        "--wait", type=int, default=10,
        help="Seconds to wait after nav for SPA bootstrap XHRs (default 10).",
    )
    ap.add_argument(
        "--records-json", type=Path, default=DEFAULT_RECORDS,
        help="records.json to pick target cases from "
             "(default: data/records.json)",
    )
    ap.add_argument(
        "--dump-dir", type=Path, default=DEFAULT_DUMP_DIR,
        help="Where to write capture artifacts "
             "(default: data/probes/tyler_case_api_discovery/)",
    )
    args = ap.parse_args()

    if args.case_data_ids:
        case_ids = list(args.case_data_ids)
    else:
        case_ids = pick_target_case_data_ids(args.records_json, args.limit)
        if not case_ids:
            print("No --case-data-ids provided and no unresolved Tyler PB "
                  "found in records.json. Pass --case-data-ids <UUID>.")
            return 1
        logger.info("Auto-picked %d case_data_ids: %s",
                    len(case_ids), ", ".join(case_ids))

    args.dump_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Dump dir: %s", args.dump_dir)

    # Per-case captures keyed by case_data_id. Each capture is a
    # dict {url, status, method, ctype, body, body_full_len}. The 'body'
    # field holds the FULL response body for JSON responses (no truncation).
    captures_by_case: dict[str, list[dict]] = {cid: [] for cid in case_ids}

    # Mutable holder for "which case are we currently navigating to" so
    # the response handler can tag captures correctly. Playwright's
    # page.on() handler runs in the same thread but with no context arg.
    current_case = [None]

    def on_response(response):
        try:
            cid = current_case[0]
            if cid is None:
                return
            url = response.url
            if "research.txcourts.gov" not in url.lower():
                return
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
            # For JSON responses: save the FULL body both inline and as
            # a separate per-URL file in the dump dir.
            ctype = entry["ctype"].lower()
            if "json" in ctype and 200 <= response.status < 300:
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="replace")
                    entry["body"] = text
                    entry["body_full_len"] = len(text)
                    # Save to per-URL file for easy diffing across cases.
                    slug = url_to_slug(url)
                    out = args.dump_dir / f"{cid}__{slug}.json"
                    out.write_text(text, encoding="utf-8")
                except Exception as exc:
                    entry["body"] = f"<read failed: {exc}>"
            captures_by_case[cid].append(entry)
        except Exception:
            pass

    from scraper.probate_auth import live_authenticated_session

    with live_authenticated_session() as (cookies, page):
        if cookies is None or page is None:
            logger.error("Auth failed; cannot proceed with discovery.")
            return 2
        logger.info("Auth succeeded; %d cookies. Subscribing to responses.",
                    len(cookies))
        page.on("response", on_response)

        for idx, cid in enumerate(case_ids, 1):
            current_case[0] = cid
            detail_url = (
                f"https://research.txcourts.gov/CourtRecordsSearch/ui/case/"
                f"{cid}"
            )
            logger.info("[%d/%d] Navigating: %s", idx, len(case_ids), detail_url)
            try:
                page.goto(detail_url, wait_until="networkidle", timeout=30_000)
            except Exception as e:
                logger.warning("[%d/%d] Navigation timed out / errored: %s",
                               idx, len(case_ids), e)
            logger.info("[%d/%d] Settling %ds for SPA XHRs...",
                        idx, len(case_ids), args.wait)
            try:
                page.wait_for_timeout(args.wait * 1000)
            except Exception:
                pass
            # Dump rendered page text for completeness.
            try:
                text = page.inner_text("body", timeout=5_000)
                (args.dump_dir / f"{cid}_page.txt").write_text(
                    text or "", encoding="utf-8",
                )
                logger.info("[%d/%d] Page text len: %d",
                            idx, len(case_ids), len(text or ""))
            except Exception as e:
                logger.warning("[%d/%d] Page text extract failed: %s",
                               idx, len(case_ids), e)

    # ===================================================================
    # Per-case capture summary file (small; bodies trimmed inline since
    # full bodies are in per-URL files already)
    # ===================================================================
    summary_path = args.dump_dir / "_run_summary.json"
    summary_blob = {}
    for cid, caps in captures_by_case.items():
        light = []
        for c in caps:
            lc = {k: v for k, v in c.items() if k != "body"}
            if "body" in c:
                lc["body_preview"] = c["body"][:300]
            light.append(lc)
        summary_blob[cid] = light
    summary_path.write_text(
        json.dumps(summary_blob, indent=2, default=str),
        encoding="utf-8",
    )

    # ===================================================================
    # Pretty-print: per-case address-field findings
    # ===================================================================
    print()
    print("=" * 78)
    print("ADDRESS-FIELD DISCOVERY  (does Tyler's JSON carry decedent address?)")
    print("=" * 78)
    print(f"cases probed: {len(case_ids)}")
    print(f"summary:      {summary_path}")
    print(f"per-URL JSON: {args.dump_dir}/{{cid}}__{{url-slug}}.json")
    print()

    for cid in case_ids:
        caps = captures_by_case[cid]
        json_caps = [c for c in caps if "json" in c.get("ctype", "").lower()
                     and "body" in c and isinstance(c["body"], str)
                     and c["body"].startswith(("{", "["))]
        print(f"── case {cid}  ({len(caps)} captures, {len(json_caps)} JSON bodies)")
        any_hit = False
        for c in json_caps:
            url_short = c["url"].replace("https://research.txcourts.gov", "")
            try:
                parsed = json.loads(c["body"])
            except Exception:
                continue
            hits = find_address_fields(parsed)
            if not hits:
                continue
            any_hit = True
            print(f"  [{c['status']}] {c['method']} {url_short}  →  "
                  f"{len(hits)} address-like field(s)")
            # Show first 10 hits with value previews
            for path, val in hits[:10]:
                if isinstance(val, str):
                    preview = val.replace("\n", " ").replace("\r", "")
                    if len(preview) > 80:
                        preview = preview[:80] + "..."
                    print(f"      {path} = {preview!r}")
                else:
                    print(f"      {path} = {val!r}")
            if len(hits) > 10:
                print(f"      ... and {len(hits) - 10} more")
        if not any_hit:
            print("  (no address-like fields found in any JSON body)")
        print()

    # ===================================================================
    # Cross-case URL coverage — which APIs always fired vs sometimes
    # ===================================================================
    url_counts: dict[str, int] = {}
    for caps in captures_by_case.values():
        seen = set()
        for c in caps:
            short = c["url"].replace("https://research.txcourts.gov", "")
            # Normalize case-id out of path so we can group cross-case
            short_norm = re.sub(r"[0-9a-f]{32}", "{caseDataID}", short)
            short_norm = re.sub(r"\?.*$", "", short_norm)
            seen.add(short_norm)
        for s in seen:
            url_counts[s] = url_counts.get(s, 0) + 1
    print("=" * 78)
    print("URL COVERAGE  (which APIs fired across N cases)")
    print("=" * 78)
    for url, count in sorted(url_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count}/{len(case_ids)}  {url}")
    print()

    print("Inspect full bodies via:")
    print(f"  ls {args.dump_dir}")
    print("If any address-field hit looks like the decedent's residence,")
    print("PR 8.5 is a 50-line extractor. If none look right, the decedent")
    print("address only lives in the Application/Inventory PDFs and we'll")
    print("need PDF download + OCR.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
