"""LLM grounding enrichment (batch 2, v2): bilingual function analysis + per-repo community
grounding + repo-level overall rate.

Changes vs v1:
- community is computed ONCE PER REPO (not per skill — fixes the duplicate-LLM waste),
  cached and referenced by each skill via `community_repo`.
- function analysis and community grounding are BILINGUAL ({en, zh}) in one call each.
- adds a repo-level object per source repo: health + community + security summary +
  frontmatter pass-rate + skill_count → an overall score/grade (SourceRepo as a first-class
  graded object).

Runs incrementally on a sample (default top-N skill-level by health). Coverage recorded.
Creds env-only (never in repo): GEMINI_API_KEY (required), GITHUB_TOKEN (optional). Honors HTTPS_PROXY.

  python enrich_grounding.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
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
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def fetch_skill_body(e: dict) -> str:
    if e.get("level") != "skill" or not e.get("path"):
        return ""
    repo, url = e["source_repo"], e.get("source_url", "")
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
        "Assess this Claude/agent skill from its SKILL.md. Return JSON with two top-level keys "
        "`en` and `zh` (zh = Simplified Chinese), each an object with: purpose (1 sentence), "
        "scenarios (2-4 short use cases), io (1 sentence on inputs/outputs), "
        "dependencies (array of tools/services, e.g. \"browser\",\"dws\",\"none\"), "
        "boundary (1 sentence on limits / what it does NOT do).\n"
        f"name: {e['name']}\ndescription: {e.get('summary','')}\nbody (truncated):\n{body[:4000]}"
    )
    return gemini(prompt)


def community(repo_id: str, f: dict, titles: list[str]) -> dict:
    prompt = (
        "Assess the community health/reputation of this GitHub repo hosting agent skills, using "
        "ONLY the GitHub signals below (no external sites). Return JSON with two top-level keys "
        "`en` and `zh` (zh = Simplified Chinese), each: verdict (healthy|mixed|concern), "
        "summary (1-2 sentences), signals (array of short factual bullets), vanity_risk (low|medium|high).\n"
        f"repo: {repo_id}\n"
        f"stars {f.get('stars')} · forks {f.get('forks')} · watchers {f.get('watchers')} · "
        f"open_issues {f.get('open_issues')} · last push {f.get('recency_days')}d ago\n"
        f"recent open issue titles: {titles}"
    )
    return gemini(prompt)


def overall_rate(health: int, sec: Counter, fm_rate: float | None, comm: dict) -> tuple[int, str]:
    """Repo overall score (0-100) + letter grade. Heuristic, deliberately simple & tweakable."""
    total = sum(sec.values()) or 1
    fail_r = sec.get("fail", 0) / total
    warn_r = sec.get("warn", 0) / total
    score = health - fail_r * 40 - warn_r * 10
    if fm_rate is not None:
        score = score * 0.85 + fm_rate * 100 * 0.15
    en = comm.get("en", {}) if isinstance(comm, dict) else {}
    if en.get("verdict") == "concern":
        score -= 15
    elif en.get("verdict") == "healthy":
        score += 5
    if en.get("vanity_risk") == "high":
        score -= 10
    score = max(0, min(100, round(score)))
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"
    return score, grade


def build_repo_summary(repo_id: str, repo_skills: list[dict], comm: dict) -> dict:
    sec = Counter(e["trust"]["security"] for e in repo_skills)
    fm = [e for e in repo_skills if e.get("frontmatter")]
    fm_rate = (sum(1 for e in fm if e["frontmatter"]["valid"]) / len(fm)) if fm else None
    health = repo_skills[0]["trust"]["health"] if repo_skills else 0
    score, grade = overall_rate(health, sec, fm_rate, comm)
    return {
        "health": health,
        "community": comm,
        "security": dict(sec),
        "frontmatter_pass_rate": round(fm_rate, 2) if fm_rate is not None else None,
        "skill_count": len(repo_skills),
        "overall_score": score,
        "overall_grade": grade,
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

    target_repos = sorted({e["source_repo"] for e in targets})
    print(f"enriching {len(targets)} skills across {len(target_repos)} repos "
          f"(bilingual; community per-repo)...")

    def repo_factors(repo_id):
        for e in skills:
            if e["source_repo"] == repo_id and e.get("trust", {}).get("health_factors"):
                return e["trust"]["health_factors"]
        return {}

    # community once per repo (bilingual)
    with ThreadPoolExecutor(max_workers=4) as ex:
        comms = list(ex.map(lambda r: community(r, repo_factors(r), fetch_issue_titles(r)), target_repos))
    community_cache = dict(zip(target_repos, comms))

    # repo-level summaries (over ALL skills of that repo in the index)
    data.setdefault("repos", {})
    for r in target_repos:
        repo_skills = [e for e in skills if e["source_repo"] == r]
        data["repos"][r] = build_repo_summary(r, repo_skills, community_cache[r])

    # function analysis per target skill (bilingual)
    with ThreadPoolExecutor(max_workers=4) as ex:
        fns = list(ex.map(lambda e: analyze_function(e, fetch_skill_body(e)), targets))
    for e, fn in zip(targets, fns):
        e["grounding"] = {
            "function": fn,
            "community_repo": e["source_repo"],
            "model": MODEL,
            "scope": "function=SKILL.md; community=repo-level, GitHub signals only",
        }

    ok = sum(1 for fn in fns if not fn.get("error"))
    data["grounding_coverage"] = {
        "enriched_skills": len(targets), "repos": len(target_repos),
        "total_skills": len(skills), "function_ok": ok,
    }
    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    INDEX.write_text(blob, encoding="utf-8")
    SITE.write_text(blob, encoding="utf-8")
    print(f"done: {len(targets)} skills, {len(target_repos)} repos; function_ok {ok}")
    for r in target_repos[:3]:
        rr = data["repos"][r]
        print(f"  repo {r} → grade {rr['overall_grade']} ({rr['overall_score']}) · "
              f"sec {rr['security']} · {rr['skill_count']} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
