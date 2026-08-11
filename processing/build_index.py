#!/usr/bin/env python3
"""Build the awesomeskills static index from registry/sources.toml.

Offline batch job — NO server. For each curated source repo it:
  - queries the GitHub API for real activity signals -> heuristic health (repo-level)
  - finds every SKILL.md in the repo tree and parses it into a SKILL-LEVEL entry
    (name/description/validation), so the index granularity is the *skill*, not the repo
  - repos with no SKILL.md (awesome-lists, hubs) fall back to a single repo-level entry

Emits two static artifacts:
  - registry/index.json          machine-readable skill index (see docs §6)
  - site/public/{index.json,llm.txt}   deployable copies

Standard library only. Honors HTTPS_PROXY. Optional GITHUB_TOKEN raises rate limit.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from skill_parser import parse_skill_md

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = REPO_ROOT / "registry" / "sources.toml"
INDEX_OUT = REPO_ROOT / "registry" / "index.json"
LLM_OUT = REPO_ROOT / "site" / "public" / "llm.txt"

API = "https://api.github.com/repos/"
RAW = "https://raw.githubusercontent.com/"
SCHEMA_VERSION = "0.2"  # 0.1 was repo-level; 0.2 is skill-level
MAX_SKILLS_PER_REPO = 15  # cap per repo; truncation is logged, never silent

_GITHUB_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def is_public_github_id(repo_id: str) -> bool:
    """Guard: only public github.com owner/repo ids allowed (no internal git-inner refs)."""
    if "://" in repo_id or "git-inner" in repo_id or repo_id.count("/") != 1:
        return False
    return bool(_GITHUB_ID.match(repo_id))


# ---------- sources.toml ----------

def load_sources(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
        return tomllib.loads(text).get("source", [])
    except ModuleNotFoundError:
        return _parse_sources_fallback(text)


def _parse_sources_fallback(text: str) -> list[dict]:
    sources: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "[[source]]":
            current = {}
            sources.append(current)
            continue
        if current is None or "=" not in line:
            continue
        key, val = line.split("=", 1)
        current[key.strip()] = val.strip().strip('"')
    return sources


# ---------- HTTP ----------

def _get(url: str, token: str | None, accept: str = "application/vnd.github+json", raw: bool = False):
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "awesomeskills-build-index")
    if token and not raw:
        req.add_header("Authorization", f"Bearer {token}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8")
                return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            print(f"  ! {url}: HTTP {e.code}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 2:
                print(f"  ! {url}: {e} (gave up)", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_repo(repo_id: str, token: str | None) -> dict | None:
    return _get(API + repo_id, token)


def fetch_skill_paths(repo_id: str, branch: str, token: str | None) -> list[str]:
    tree = _get(f"{API}{repo_id}/git/trees/{branch}?recursive=1", token)
    if not tree:
        return []
    return sorted(
        item["path"] for item in tree.get("tree", [])
        if item.get("type") == "blob" and item.get("path", "").endswith("SKILL.md")
    )


def fetch_raw(repo_id: str, branch: str, path: str, token: str | None) -> str | None:
    return _get(f"{RAW}{repo_id}/{branch}/{path}", token, accept="text/plain", raw=True)


# ---------- signals ----------

def _has_chinese(*texts: str | None) -> bool:
    for t in texts:
        if t and any("一" <= ch <= "鿿" for ch in t):
            return True
    return False


def compute_health(repo: dict, now: datetime) -> tuple[int, dict]:
    """Heuristic v0: activity-driven, NOT star-driven. Recency dominates; archived penalized."""
    pushed_at = repo.get("pushed_at")
    recency_days = None
    if pushed_at:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        recency_days = (now - dt).days
    base = 0.0 if recency_days is None else max(0.0, min(100.0, 100.0 - recency_days * 0.3))
    if repo.get("archived"):
        base *= 0.3
    factors = {
        "recency_days": recency_days,
        "stars": repo.get("stargazers_count"),
        "open_issues": repo.get("open_issues_count"),
        "archived": bool(repo.get("archived")),
    }
    return round(base), factors


# ---------- entry builders ----------

def _skill_dir(path: str) -> str:
    d = path[: -len("SKILL.md")].rstrip("/")
    return d or "."


def build_skill_entries(src: dict, repo: dict, now: datetime, token: str | None) -> list[dict]:
    repo_id = src["id"]
    health, factors = compute_health(repo, now)
    branch = repo.get("default_branch") or "main"

    paths = fetch_skill_paths(repo_id, branch, token)
    truncated = len(paths) > MAX_SKILLS_PER_REPO
    if truncated:
        print(f"  … {repo_id}: {len(paths)} SKILL.md found, capping at {MAX_SKILLS_PER_REPO}", file=sys.stderr)
        paths = paths[:MAX_SKILLS_PER_REPO]

    # Repo-level fallback: no SKILL.md (awesome-list / hub / registry).
    if not paths:
        desc = repo.get("description") or ""
        return [{
            "id": repo_id,
            "name": repo.get("name") or repo_id.split("/")[-1],
            "summary": desc,
            "source_repo": repo_id,
            "source_url": repo.get("html_url") or f"https://github.com/{repo_id}",
            "kind": src.get("kind", "skill"),
            "level": "repo",
            "trust": {"health": health, "health_factors": factors, "security": "unrated", "zh": _has_chinese(desc)},
            "frontmatter": None,
        }]

    entries: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        texts = list(ex.map(lambda p: fetch_raw(repo_id, branch, p, token), paths))
    for path, text in zip(paths, texts):
        skill_dir = _skill_dir(path)
        if text is None:
            parsed = {"name": "", "description": "", "frontmatter_valid": False,
                      "issues": ["failed to fetch SKILL.md"], "body_headings": 0, "body_code_blocks": 0}
        else:
            parsed = parse_skill_md(text)
        name = parsed["name"] or (skill_dir.split("/")[-1] if skill_dir != "." else repo.get("name") or repo_id)
        entries.append({
            "id": f"{repo_id}/{skill_dir}" if skill_dir != "." else repo_id,
            "name": name,
            "summary": parsed["description"],
            "source_repo": repo_id,
            "source_url": f"https://github.com/{repo_id}/tree/{branch}/{skill_dir}" if skill_dir != "."
                          else f"https://github.com/{repo_id}",
            "kind": "skill",
            "level": "skill",
            "path": path,
            "trust": {
                "health": health,  # inherited from repo
                "health_factors": factors,
                "security": "unrated",
                "zh": _has_chinese(parsed["description"], parsed["name"]),
            },
            "frontmatter": {
                "valid": parsed["frontmatter_valid"],
                "issues": parsed["issues"],
                "headings": parsed["body_headings"],
                "code_blocks": parsed["body_code_blocks"],
            },
        })
    return entries


# ---------- artifacts ----------

def write_index(entries: list[dict], generated_at: str) -> None:
    entries.sort(key=lambda e: (e["trust"]["health"], e["name"]), reverse=True)
    payload = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at, "skills": entries}
    blob = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    INDEX_OUT.write_text(blob, encoding="utf-8")
    print(f"  wrote {INDEX_OUT.relative_to(REPO_ROOT)} ({len(entries)} entries)")
    site_copy = LLM_OUT.parent / "index.json"
    site_copy.parent.mkdir(parents=True, exist_ok=True)
    site_copy.write_text(blob, encoding="utf-8")
    print(f"  wrote {site_copy.relative_to(REPO_ROOT)}")


def write_llm_txt(entries: list[dict], generated_at: str) -> None:
    lines = [
        "# awesomeskills — a trust-first index of public agent/Claude skills",
        f"# Generated {generated_at}. Human + agent readable. Full data: /index.json",
        "# health = real activity 0-100 (NOT stars) | security rating | zh = Chinese coverage",
        "# Entries are skill-level (one per SKILL.md); repo-level rows are awesome-lists/hubs.",
        "",
    ]
    for e in entries:
        t = e["trust"]
        zh = "zh" if t["zh"] else "en"
        flag = "" if not e.get("frontmatter") else ("" if e["frontmatter"]["valid"] else " [frontmatter:invalid]")
        lines.append(f"- {e['name']} ({e['id']}) — health {t['health']}, security {t['security']}, {zh}{flag}")
        if e["summary"]:
            lines.append(f"  {e['summary']}")
        lines.append(f"  {e['source_url']}")
    LLM_OUT.parent.mkdir(parents=True, exist_ok=True)
    LLM_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {LLM_OUT.relative_to(REPO_ROOT)}")


def main() -> int:
    now = datetime.now(timezone.utc)
    generated_at = now.replace(microsecond=0).isoformat()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("  (no GITHUB_TOKEN — unauthenticated rate limit)", file=sys.stderr)

    sources = load_sources(SOURCES)
    print(f"building skill-level index from {len(sources)} sources...")

    entries: list[dict] = []
    repo_count = skill_level = repo_level = 0
    for src in sources:
        if not is_public_github_id(src["id"]):
            print(f"  SKIP {src['id']}: not a public github.com owner/repo (internal refs forbidden)", file=sys.stderr)
            continue
        repo = fetch_repo(src["id"], token)
        if repo is None:
            continue
        repo_count += 1
        got = build_skill_entries(src, repo, now, token)
        entries.extend(got)
        if got and got[0].get("level") == "skill":
            skill_level += len(got)
            print(f"  ok {src['id']}: {len(got)} skill(s)")
        else:
            repo_level += 1
            print(f"  ok {src['id']}: repo-level (no SKILL.md)")

    if not entries:
        print("no entries produced — aborting", file=sys.stderr)
        return 1

    write_index(entries, generated_at)
    write_llm_txt(entries, generated_at)
    print(f"done: {len(entries)} entries from {repo_count} repos "
          f"({skill_level} skill-level, {repo_level} repo-level fallbacks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
