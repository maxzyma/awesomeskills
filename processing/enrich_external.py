"""External community grounding (batch 3): off-GitHub discussion signals.

Adds `external` to each repo in the index — currently Hacker News discussion via the public
Algolia HN API (no key). This surfaces whether a source repo has broken out beyond GitHub
stars into real third-party discussion (a strong anti-vanity signal). Most small repos will
have zero HN discussion — that absence is itself a signal.

Chinese community sources (WeChat/Bilibili/Zhihu) are a further step and are marked pending
in scope. Standard library only. Honors HTTPS_PROXY.

Run AFTER enrich_grounding.py (which builds repos[]); this only augments repos[].external.

  python enrich_external.py
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "registry" / "index.json"
SITE = ROOT / "site" / "public" / "index.json"
HN = "https://hn.algolia.com/api/v1/search?"


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "awesomeskills"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def hn_search(repo_id: str) -> dict:
    """Search HN stories mentioning this repo (by github url or name). Returns discussion signal."""
    name = repo_id.split("/")[-1].lower()
    url = HN + urllib.parse.urlencode({"query": f"github.com/{repo_id}", "tags": "story", "hitsPerPage": 10})
    try:
        hits = json.loads(_get(url)).get("hits", [])
    except Exception:
        return {"stories": 0, "error": "hn fetch failed"}

    rel = []
    for h in hits:
        hay = f"{h.get('url') or ''} {h.get('title') or ''}".lower()
        if repo_id.lower() in hay or f"/{name}" in hay or name in (h.get("title") or "").lower():
            rel.append(h)
    if not rel:
        return {"stories": 0}

    top = max(rel, key=lambda h: h.get("points") or 0)
    return {
        "stories": len(rel),
        "total_points": sum(h.get("points") or 0 for h in rel),
        "total_comments": sum(h.get("num_comments") or 0 for h in rel),
        "top_title": top.get("title"),
        "top_points": top.get("points"),
        "top_comments": top.get("num_comments"),
        "top_url": f"https://news.ycombinator.com/item?id={top.get('objectID')}",
    }


def main() -> int:
    data = json.loads(INDEX.read_text(encoding="utf-8"))
    repos = data.get("repos", {})
    if not repos:
        print("no repos[] in index — run enrich_grounding.py first")
        return 1
    repo_ids = sorted(repos.keys())
    print(f"fetching HN discussion signals for {len(repo_ids)} repos...")

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(hn_search, repo_ids))

    hit = 0
    for rid, hn in zip(repo_ids, results):
        repos[rid]["external"] = {"hn": hn, "scope": "Hacker News only; Chinese community pending"}
        if hn.get("stories"):
            hit += 1

    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    INDEX.write_text(blob, encoding="utf-8")
    SITE.write_text(blob, encoding="utf-8")
    print(f"done: {hit}/{len(repo_ids)} repos have HN discussion")
    for rid in repo_ids:
        hn = repos[rid]["external"]["hn"]
        if hn.get("stories"):
            print(f"  {rid}: {hn['stories']} HN stories · top {hn.get('top_points')}pts/{hn.get('top_comments')}c — {str(hn.get('top_title'))[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
