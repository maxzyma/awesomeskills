#!/usr/bin/env python3
"""Thin client: query the awesomeskills static index for trustworthy skills.

No server. Reads a static index.json (hosted URL or local repo copy), filters by
the requested capability, ranks by real-activity health, prints ranked candidates.

  python3 find_skill.py --query "pdf" [--zh-only] [--min-health 70] [--limit 5]

Skills whose static scan rated `fail` are withheld by default: the scan flags things like
`rm -rf` against $HOME, and a capability match is not a reason to put one in front of an
agent. `--security` widens or narrows that gate, and withheld candidates are always
reported as a count so the omission is visible rather than silent.

Index resolution: --index-url > $AWESOMESKILLS_INDEX_URL > hosted default, with the local
repo copy as an offline fallback.
Standard library only. Honors HTTPS_PROXY for remote URLs.
"""

from __future__ import annotations

import argparse
import json
import sys

from index_client import load_index, resolve_index_url

# Ordered least to most dangerous. `unrated` means not yet assessed, which is not a pass.
SECURITY_ORDER = ["pass", "unrated", "warn", "fail"]
DEFAULT_MAX_SECURITY = "warn"


def match(entry: dict, query: str) -> bool:
    if not query:
        return True
    hay = f"{entry.get('id','')} {entry.get('name','')} {entry.get('summary','')} {entry.get('kind','')}".lower()
    return all(term in hay for term in query.lower().split())


def security_rank(entry: dict) -> int:
    rating = entry.get("trust", {}).get("security", "unrated")
    return SECURITY_ORDER.index(rating) if rating in SECURITY_ORDER else len(SECURITY_ORDER)


def passes_gate(entry: dict, max_security: str) -> bool:
    return security_rank(entry) <= SECURITY_ORDER.index(max_security)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="", help="capability keywords (all must match)")
    ap.add_argument("--index-url", default=None)
    ap.add_argument("--zh-only", action="store_true")
    ap.add_argument("--min-health", type=int, default=0)
    ap.add_argument(
        "--security", choices=SECURITY_ORDER, default=DEFAULT_MAX_SECURITY,
        help=f"worst security rating to return (default: {DEFAULT_MAX_SECURITY}; "
             "'fail' returns everything, including skills flagged as destructive)",
    )
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    url = resolve_index_url(args.index_url)
    try:
        index, used = load_index(url)
    except Exception as e:  # noqa: BLE001 — surface any resolution/parse error to the agent
        print(json.dumps({"error": f"failed to load index from {url}: {e}"}), file=sys.stderr)
        return 1

    relevant = [
        e for e in index.get("skills", [])
        if match(e, args.query)
        and e.get("trust", {}).get("health", 0) >= args.min_health
        and (not args.zh_only or e.get("trust", {}).get("zh"))
    ]
    candidates = [e for e in relevant if passes_gate(e, args.security)]
    withheld = [e for e in relevant if not passes_gate(e, args.security)]
    candidates.sort(key=lambda e: e.get("trust", {}).get("health", 0), reverse=True)

    out = {
        "index_source": used,
        "index_generated_at": index.get("generated_at"),
        "query": args.query,
        "security_gate": args.security,
        "count": len(candidates),
        "withheld_by_security_gate": [
            {"id": e["id"], "security": e.get("trust", {}).get("security")} for e in withheld
        ],
        "candidates": candidates[: args.limit],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
