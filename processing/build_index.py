#!/usr/bin/env python3
"""Build the awsomeskills static index from registry/sources.toml.

Offline batch job — NO server. Reads the curated seed list, queries the GitHub
API for real activity signals, computes a heuristic (deliberately non-star-driven)
health score, detects Chinese-language coverage, and emits two static artifacts:

  - registry/index.json          machine-readable index (see docs §6)
  - site/public/llm.txt          lightweight agent-facing site map

Standard library only. Honors HTTPS_PROXY. Optional GITHUB_TOKEN raises rate limit.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = REPO_ROOT / "registry" / "sources.toml"
INDEX_OUT = REPO_ROOT / "registry" / "index.json"
LLM_OUT = REPO_ROOT / "site" / "public" / "llm.txt"

API = "https://api.github.com/repos/"
SCHEMA_VERSION = "0.1"


# ---------- sources.toml parsing (tomllib, with a tiny fallback) ----------

def load_sources(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # py3.11+
        return tomllib.loads(text).get("source", [])
    except ModuleNotFoundError:
        return _parse_sources_fallback(text)


def _parse_sources_fallback(text: str) -> list[dict]:
    """Minimal parser for the fixed [[source]] key = "value" shape only."""
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


# ---------- GitHub API ----------

def fetch_repo(repo_id: str, token: str | None) -> dict | None:
    req = urllib.request.Request(API + repo_id)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "awsomeskills-build-index")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"  ! {repo_id}: HTTP {e.code}", file=sys.stderr)
            return None  # a real HTTP status (404/403) won't change on retry
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 2:
                print(f"  ! {repo_id}: {e} (gave up after 3 tries)", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))  # transient proxy blip — back off and retry
    return None


# ---------- signal computation ----------

def _has_chinese(*texts: str | None) -> bool:
    for t in texts:
        if t and any("一" <= ch <= "鿿" for ch in t):
            return True
    return False


def compute_health(repo: dict, now: datetime) -> tuple[int, dict]:
    """Heuristic v0: activity-driven, NOT star-driven (anti-vanity).

    Recency of last push dominates; archived repos are heavily penalized.
    Stars and open issues are recorded as factors but do not drive the score.
    """
    pushed_at = repo.get("pushed_at")
    recency_days = None
    if pushed_at:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        recency_days = (now - dt).days

    if recency_days is None:
        base = 0.0
    else:
        # ~0 by one year stale; full marks when fresh.
        base = max(0.0, min(100.0, 100.0 - recency_days * 0.3))

    if repo.get("archived"):
        base *= 0.3

    factors = {
        "recency_days": recency_days,
        "stars": repo.get("stargazers_count"),
        "open_issues": repo.get("open_issues_count"),
        "archived": bool(repo.get("archived")),
    }
    return round(base), factors


def build_entry(src: dict, repo: dict, now: datetime) -> dict:
    health, factors = compute_health(repo, now)
    desc = repo.get("description")
    return {
        "id": src["id"],
        "name": repo.get("name") or src["id"].split("/")[-1],
        "summary": desc or "",
        "source_url": repo.get("html_url") or f"https://github.com/{src['id']}",
        "kind": src.get("kind", "skill"),
        "trust": {
            "health": health,
            "health_factors": factors,
            "security": "unrated",  # honest default; real scan is M2
            "zh": _has_chinese(desc, repo.get("language")),
        },
    }


# ---------- artifacts ----------

def write_index(entries: list[dict], generated_at: str) -> None:
    entries.sort(key=lambda e: e["trust"]["health"], reverse=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "skills": entries,
    }
    blob = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    INDEX_OUT.write_text(blob, encoding="utf-8")
    print(f"  wrote {INDEX_OUT.relative_to(REPO_ROOT)} ({len(entries)} skills)")
    # Also drop a copy into the deployable static site root so the browser can fetch ./index.json.
    site_copy = LLM_OUT.parent / "index.json"
    site_copy.parent.mkdir(parents=True, exist_ok=True)
    site_copy.write_text(blob, encoding="utf-8")
    print(f"  wrote {site_copy.relative_to(REPO_ROOT)}")


def write_llm_txt(entries: list[dict], generated_at: str) -> None:
    lines = [
        "# awsomeskills — a trust-first index of public agent/Claude skills",
        f"# Generated {generated_at}. Human + agent readable. Full data: /index.json",
        "# health = real activity 0-100 (NOT stars) | security rating | zh = Chinese coverage",
        "",
    ]
    for e in entries:
        t = e["trust"]
        zh = "zh" if t["zh"] else "en"
        lines.append(f"- {e['name']} ({e['id']}) — health {t['health']}, security {t['security']}, {zh}")
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
        print("  (no GITHUB_TOKEN — using unauthenticated rate limit)", file=sys.stderr)

    sources = load_sources(SOURCES)
    print(f"building index from {len(sources)} sources...")

    entries: list[dict] = []
    for src in sources:
        repo = fetch_repo(src["id"], token)
        if repo is None:
            continue
        entries.append(build_entry(src, repo, now))
        h = entries[-1]["trust"]["health"]
        print(f"  ok {src['id']}: health {h}")

    if not entries:
        print("no entries produced — aborting", file=sys.stderr)
        return 1

    write_index(entries, generated_at)
    write_llm_txt(entries, generated_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
