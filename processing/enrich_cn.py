"""Chinese community grounding (batch 3b): Bilibili topic signals via agent-browser.

Per your rule, Chinese word-of-mouth must come from Chinese sources directly. Bilibili's API is
gated by gaia anti-bot (a plain wbi-signed request returns only a `v_voucher` challenge — verified),
so we drive a real browser via agent-browser, which passes the challenge. We search each repo's
short name and record how much Chinese-community video content exists on that topic.

This is a TOPIC-attention signal (Chinese videos rarely name an exact owner/repo), not precise
per-repo word-of-mouth — recorded honestly in scope. WeChat/Zhihu remain pending (login/anti-bot).

Run AFTER enrich_grounding.py (augments repos[].external.cn). Needs agent-browser on PATH.

  python enrich_cn.py [--limit N]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "registry" / "index.json"
SITE = ROOT / "site" / "public" / "index.json"

EVAL_JS = r"""(()=>{
  const cards=[...document.querySelectorAll('.bili-video-card')].filter(c=>c.querySelector('a[href*="/video/BV"]'));
  const pick=c=>{
    const t=(c.querySelector('.bili-video-card__info--tit')?.textContent||c.querySelector('h3')?.getAttribute('title')||'').trim();
    const plays=(c.querySelector('.bili-video-card__stats--item')?.textContent||'').trim();
    const href=c.querySelector('a[href*="/video/BV"]')?.getAttribute('href')||'';
    return {t,plays,href:href.startsWith('//')?'https:'+href:href};
  };
  const v=cards.slice(0,8).map(pick).filter(x=>x.t);
  return JSON.stringify({videos:v.length, top:v[0]||null, sample:v.slice(0,3).map(x=>x.t)});
})()"""


def ab(args: list[str], timeout: int = 40) -> str:
    return subprocess.run(["agent-browser", *args], capture_output=True, text=True, timeout=timeout).stdout


def _parse_eval(out: str) -> dict:
    """agent-browser prints the eval result; our expr returns a JSON string. Robustly unwrap."""
    s = out.strip()
    # take the last non-empty line (skip any ✓/status lines)
    for line in reversed(s.splitlines()):
        line = line.strip()
        if not line or line.startswith(("✓", "http")):
            continue
        try:
            val = json.loads(line)              # outer: the printed string
            return json.loads(val) if isinstance(val, str) else val
        except Exception:
            try:
                return json.loads(line)
            except Exception:
                continue
    return {}


def bili_topic(keyword: str) -> dict:
    url = "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(keyword)
    try:
        ab(["open", url])
        ab(["wait", "2000"])
        data = _parse_eval(ab(["eval", EVAL_JS]))
    except Exception as e:  # noqa: BLE001
        return {"videos": 0, "error": str(e)[:80]}
    top = data.get("top") or {}
    return {
        "videos": data.get("videos", 0),
        "top_title": top.get("t"),
        "top_plays": top.get("plays"),
        "top_url": top.get("href"),
        "sample": data.get("sample", []),
    }


def keyword_for(repo_id: str) -> str:
    return repo_id.split("/")[-1].replace("-", " ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    data = json.loads(INDEX.read_text(encoding="utf-8"))
    repos = data.get("repos", {})
    if not repos:
        print("no repos[] — run enrich_grounding.py first")
        return 1
    repo_ids = sorted(repos.keys())
    if args.limit:
        repo_ids = repo_ids[: args.limit]
    print(f"fetching Bilibili topic signals for {len(repo_ids)} repos (via agent-browser)...")

    hit = 0
    for rid in repo_ids:
        bili = bili_topic(keyword_for(rid))
        repos[rid].setdefault("external", {})
        repos[rid]["external"]["cn"] = {"bilibili": bili, "scope": "Bilibili topic search (agent-browser); WeChat/Zhihu pending"}
        if bili.get("videos"):
            hit += 1
        print(f"  {rid}: {bili.get('videos', 0)} videos" + (f" — {str(bili.get('top_title'))[:40]}" if bili.get("videos") else ""))

    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    INDEX.write_text(blob, encoding="utf-8")
    SITE.write_text(blob, encoding="utf-8")
    print(f"done: {hit}/{len(repo_ids)} repos have Bilibili topic videos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
