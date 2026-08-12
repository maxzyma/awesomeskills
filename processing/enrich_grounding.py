"""LLM grounding enrichment (batch 2): function/scenario analysis + community grounding.

Adds the two LLM-dependent grounding layers on top of the static index (batch 1 = health +
security). Runs INCREMENTALLY on a sample (default: top-N skill-level by health) — full
coverage is an ongoing/operational cost, and coverage is recorded honestly in the index.

  python enrich_grounding.py --limit 20     # top-20 skill-level by health
  python enrich_grounding.py --all          # everything (expensive)

Credentials from env only (never in repo): GEMINI_API_KEY (required), GITHUB_TOKEN (optional,
for issue signals). Honors HTTPS_PROXY. Model: gemini-flash-latest.

Community grounding here uses GitHub signals only — external community (HN/Reddit/Chinese
sites) is a further step reusing the github-trends grounding kernel; scope is noted per entry.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "registry" / "index.json"
SITE = ROOT / "site" / "public" / "index.json"
MODEL = "gemini-flash-latest"
KEY = os.environ.get("GEMINI_API_KEY")
GH = os.environ.get("GITHUB_TOKEN")
RAW = "https://raw.githubusercontent.com/"
GAPI = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def _get(url: str, headers: dict | None = None, data: bytes | None = None, timeout: int = 60) -> str:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def gemini(prompt: str) -> dict:
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()
    try:
        raw = _get(f"{GAPI}?key={KEY}", {"content-type": "application/json"}, body)
        d = json.loads(raw)
        if not d.get("candidates"):
            return {"error": (d.get("error", {}).get("message") or "no candidates")[:120]}
        return json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:  # noqa: BLE001 — surface any failure into the field
        return {"error": str(e)[:120]}


def fetch_skill_body(e: dict) -> str:
    if e.get("level") != "skill" or not e.get("path"):
        return ""
    repo = e["source_repo"]
    url = e.get("source_url", "")
    branch = url.split("/tree/")[1].split("/")[0] if "/tree/" in url else "main"
    try:
        return _get(f"{RAW}{repo}/{branch}/{e['path']}", {"User-Agent": "awesomeskills"})
    except Exception:
        return ""


def fetch_issue_titles(repo_id: str) -> list[str]:
    if not GH:
        return []
    try:
        raw = _get(
            f"https://api.github.com/repos/{repo_id}/issues?state=open&per_page=5",
            {"Authorization": f"Bearer {GH}", "User-Agent": "awesomeskills", "Accept": "application/vnd.github+json"},
        )
        return [i["title"] for i in json.loads(raw) if "pull_request" not in i][:5]
    except Exception:
        return []


def analyze_function(e: dict, body: str) -> dict:
    prompt = (
        "Assess this Claude/agent skill from its SKILL.md. Return JSON with keys: "
        "purpose (1 sentence), scenarios (2-4 short use cases), io (1 sentence on inputs/outputs), "
        "dependencies (array of tools/services it needs, e.g. \"browser\",\"dws\",\"none\"), "
        "boundary (1 sentence on limits / what it does NOT do).\n"
        f"name: {e['name']}\ndescription: {e.get('summary','')}\nbody (truncated):\n{body[:4000]}"
    )
    return gemini(prompt)


def community(e: dict, titles: list[str]) -> dict:
    f = e["trust"].get("health_factors", {})
    prompt = (
        "Assess the community health/reputation of the GitHub repo hosting this agent skill, "
        "using ONLY the GitHub signals below (no external sites). Return JSON: "
        "verdict (one of: healthy, mixed, concern), summary (1-2 sentences), "
        "signals (array of short factual bullets), vanity_risk (low|medium|high — do stars look inflated vs real engagement?).\n"
        f"repo: {e['source_repo']}\n"
        f"stars {f.get('stars')} · forks {f.get('forks')} · watchers {f.get('watchers')} · "
        f"open_issues {f.get('open_issues')} · last push {f.get('recency_days')}d ago\n"
        f"recent open issue titles: {titles}"
    )
    return gemini(prompt)


def enrich_one(e: dict) -> dict:
    body = fetch_skill_body(e)
    titles = fetch_issue_titles(e["source_repo"])
    fn = analyze_function(e, body) if body else {"error": "no SKILL.md body"}
    co = community(e, titles)
    return {
        "function": fn,
        "community": co,
        "model": MODEL,
        "scope": "function=SKILL.md; community=GitHub signals only (external sites not yet included)",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not KEY:
        sys.exit("missing GEMINI_API_KEY (env only)")

    data = json.loads(INDEX.read_text(encoding="utf-8"))
    skills = data["skills"]
    targets = [e for e in skills if e.get("level") == "skill"]
    targets.sort(key=lambda e: -e["trust"]["health"])
    if not args.all:
        targets = targets[: args.limit]
    print(f"enriching {len(targets)}/{len(skills)} (skill-level, top by health)...")

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(enrich_one, targets))
    for e, g in zip(targets, results):
        e["grounding"] = g

    ok = sum(1 for g in results if not g["function"].get("error") and not g["community"].get("error"))
    data["grounding_coverage"] = {"enriched": len(targets), "total": len(skills), "ok": ok}
    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    INDEX.write_text(blob, encoding="utf-8")
    SITE.write_text(blob, encoding="utf-8")
    print(f"done: enriched {len(targets)} ({ok} fully ok); coverage {len(targets)}/{len(skills)}")
    for e in targets[:3]:
        fn = e["grounding"]["function"]
        print(f"  {e['id']} → {str(fn.get('purpose') or fn.get('error'))[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
