#!/usr/bin/env python3
"""Thin client: query the awesomeskills static index for trustworthy skills.

No server. Reads a static index.json (hosted URL or local repo copy), filters by
the requested capability, ranks by real-activity health, prints ranked candidates.

  python3 find_skill.py --query "pdf" [--zh-only] [--min-health 70] [--limit 5]

Index resolution order: --index-url > $AWSOMESKILLS_INDEX_URL > hosted default > local repo copy.
Standard library only. Honors HTTPS_PROXY for remote URLs.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

# Published index (M1): GitHub raw off the public repo. awesomeskills.io can front this later.
HOSTED_DEFAULT = "https://raw.githubusercontent.com/maxzyma/awesomeskills/main/registry/index.json"
LOCAL_FALLBACK = Path(__file__).resolve().parents[3] / "registry" / "index.json"


def resolve_index_url(cli_url: str | None) -> str:
    import os
    return cli_url or os.environ.get("AWSOMESKILLS_INDEX_URL") or HOSTED_DEFAULT or str(LOCAL_FALLBACK)


def load_index(url: str) -> dict:
    if url.startswith(("http://", "https://")):
        req = urllib.request.Request(url, headers={"User-Agent": "awesomeskills"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return json.loads(Path(url).read_text(encoding="utf-8"))


def match(entry: dict, query: str) -> bool:
    if not query:
        return True
    hay = f"{entry.get('id','')} {entry.get('name','')} {entry.get('summary','')} {entry.get('kind','')}".lower()
    return all(term in hay for term in query.lower().split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="", help="capability keywords (all must match)")
    ap.add_argument("--index-url", default=None)
    ap.add_argument("--zh-only", action="store_true")
    ap.add_argument("--min-health", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    url = resolve_index_url(args.index_url)
    try:
        index = load_index(url)
    except Exception as e:  # noqa: BLE001 — surface any resolution/parse error to the agent
        print(json.dumps({"error": f"failed to load index from {url}: {e}"}), file=sys.stderr)
        return 1

    candidates = [
        e for e in index.get("skills", [])
        if match(e, args.query)
        and e.get("trust", {}).get("health", 0) >= args.min_health
        and (not args.zh_only or e.get("trust", {}).get("zh"))
    ]
    candidates.sort(key=lambda e: e.get("trust", {}).get("health", 0), reverse=True)

    out = {
        "index_source": url,
        "index_generated_at": index.get("generated_at"),
        "query": args.query,
        "count": len(candidates),
        "candidates": candidates[: args.limit],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
