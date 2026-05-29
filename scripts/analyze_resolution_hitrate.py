"""Resolution hit-rate dissection — why is the DCAD match rate low, and
where is the recoverable opportunity?

Reads a records.json and aggregates the resolution_history that the
pipeline already records per record. Answers:

  1. HIT RATE — resolved (has dcad_account) vs unresolved, overall and
     broken down by source + dallas_code.
  2. WHAT WORKS — for resolved records, which (path, stage, tier)
     combination produced the match.
  3. WHY UNRESOLVED FAIL — the terminal status/skip_reason each
     unresolved record hit on every path it tried.
  4. OPPORTUNITY — among unresolved records, how many still carry
     usable signal we're not converting:
       - a subdivision name in raw_excerpt (path C lever)
       - a clean address (path B lever)
       - a grantor/grantee name (owner-index lever)
     This sizes which fix raises the hit rate most.

Usage:
    python scripts/analyze_resolution_hitrate.py
    python scripts/analyze_resolution_hitrate.py --records-json /path/to/records.json
    python scripts/analyze_resolution_hitrate.py --source publicsearch.us
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECORDS = REPO_ROOT / "data" / "records.json"

SUBDIVISION_RE = re.compile(r"Subdivision-?\s*Name:\s*(.+?)(?:Lot:|Block:|CITY:|$)",
                            re.IGNORECASE)


def load_records(path: Path) -> list[dict]:
    env = json.load(path.open("r", encoding="utf-8"))
    return env.get("records", env) if isinstance(env, dict) else env


def is_resolved(r: dict) -> bool:
    return bool(r.get("dcad_account"))


def history(r: dict) -> list[dict]:
    sm = r.get("signal_metadata") or {}
    h = sm.get("resolution_history")
    return h if isinstance(h, list) else []


def winning_step(r: dict) -> dict | None:
    """The history step that produced the match (status matched + account)."""
    for step in history(r):
        if step.get("status") == "matched" and step.get("dcad_account"):
            return step
    return None


def has_subdivision(r: dict) -> bool:
    raw = r.get("raw_excerpt") or ""
    return bool(SUBDIVISION_RE.search(raw))


def pct(n: int, d: int) -> str:
    return f"{(100.0*n/d):.1f}%" if d else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records-json", type=Path, default=DEFAULT_RECORDS)
    ap.add_argument("--source", default=None,
                    help="Restrict analysis to one source (e.g. publicsearch.us)")
    args = ap.parse_args()

    records = load_records(args.records_json)
    if args.source:
        records = [r for r in records if r.get("source") == args.source]

    total = len(records)
    resolved = [r for r in records if is_resolved(r)]
    unresolved = [r for r in records if not is_resolved(r)]

    print("=" * 78)
    print(f"RESOLUTION HIT-RATE  ({args.records_json.name}"
          f"{', source=' + args.source if args.source else ''})")
    print("=" * 78)
    print(f"total records:  {total}")
    print(f"resolved:       {len(resolved)}  ({pct(len(resolved), total)})")
    print(f"unresolved:     {len(unresolved)}  ({pct(len(unresolved), total)})")
    print()

    # ---- Hit rate by source + dallas_code ----
    def breakdown(key):
        groups = defaultdict(lambda: [0, 0])  # key -> [resolved, total]
        for r in records:
            g = groups[r.get(key)]
            g[1] += 1
            if is_resolved(r):
                g[0] += 1
        print(f"By {key}:")
        for k, (res, tot) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
            print(f"  {str(k):<22} {res:>4}/{tot:<4}  ({pct(res, tot)})")
        print()

    breakdown("source")
    breakdown("dallas_code")

    # ---- WHAT WORKS: winning (path, stage, tier) ----
    win = Counter()
    for r in resolved:
        w = winning_step(r)
        if w:
            win[(w.get("path"), w.get("stage"), w.get("tier"))] += 1
        else:
            win[("(resolved, no matched step in history)", "", "")] += 1
    print("WHAT WORKS — winning (path, stage, tier) for resolved records:")
    for (path, stage, tier), n in win.most_common():
        print(f"  {n:>4}  {path}  [{stage}]  tier={tier}")
    print()

    # ---- WHY UNRESOLVED FAIL: terminal status/skip per path ----
    fail_by_path = defaultdict(Counter)  # path -> Counter(status:skip_reason)
    for r in unresolved:
        h = history(r)
        if not h:
            fail_by_path["(no resolution_history at all)"]["(none)"] += 1
            continue
        for step in h:
            label = step.get("status") or "?"
            if step.get("skip_reason"):
                label += f":{step['skip_reason']}"
            fail_by_path[step.get("path")][label] += 1
    print("WHY UNRESOLVED FAIL — per path, terminal status:skip_reason:")
    for path in sorted(fail_by_path):
        print(f"  {path}")
        for label, n in fail_by_path[path].most_common():
            print(f"      {n:>4}  {label}")
    print()

    # ---- OPPORTUNITY sizing among unresolved ----
    opp_subdiv = [r for r in unresolved if has_subdivision(r)]
    opp_address = [r for r in unresolved
                   if (r.get("address_normalized") or r.get("address"))]
    opp_grantor = [r for r in unresolved if r.get("grantor_raw") or r.get("grantor")]
    opp_raw = [r for r in unresolved if r.get("raw_excerpt")]
    print("OPPORTUNITY — unresolved records still carrying usable signal:")
    print(f"  unresolved total:                         {len(unresolved)}")
    print(f"  ...with a subdivision name in raw_excerpt: {len(opp_subdiv)}"
          f"  (path C lever)")
    print(f"  ...with a clean address already:           {len(opp_address)}"
          f"  (path B lever)")
    print(f"  ...with a grantor/grantee name:            {len(opp_grantor)}"
          f"  (owner-index lever)")
    print(f"  ...with any raw_excerpt at all:            {len(opp_raw)}")
    print()

    # Cross-tab: unresolved WITH subdivision — what did path C say?
    print("DRILL — unresolved records that HAVE a subdivision but path C "
          "didn't resolve:")
    pc_status = Counter()
    for r in opp_subdiv:
        steps = [s for s in history(r) if s.get("path") == "path_c_legal_resolver"]
        if not steps:
            pc_status["(path C never ran)"] += 1
        else:
            for s in steps:
                lbl = s.get("status") or "?"
                if s.get("skip_reason"):
                    lbl_extra = s.get("skip_reason")
                    lbl = f"{lbl}:{lbl_extra}"
                pc_status[lbl] += 1
    for lbl, n in pc_status.most_common():
        print(f"      {n:>4}  {lbl}")
    print()

    # Show a few concrete unresolved-with-subdivision examples for eyeballing
    print("SAMPLES — unresolved records with a subdivision (first 8):")
    for r in opp_subdiv[:8]:
        raw = r.get("raw_excerpt") or ""
        m = SUBDIVISION_RE.search(raw)
        sub = m.group(1).strip() if m else "?"
        print(f"  [{r.get('dallas_code')}] {r.get('record_id')}  "
              f"subdivision={sub!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
