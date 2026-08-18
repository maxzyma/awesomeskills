#!/usr/bin/env python3
"""Build the awesomeskills static index from registry/sources.toml.

Offline batch job — NO server. For each curated source repo it:
  - queries the GitHub API for real activity signals -> heuristic health (repo-level)
  - finds every SKILL.md in the repo tree and parses it into a SKILL-LEVEL entry
    (name/description/validation), so the index granularity is the *skill*, not the repo
  - repos with no SKILL.md (awesome-lists, hubs) fall back to a single repo-level entry

Emits one deterministic artifact:
  - registry/base-index.json     health / security / frontmatter / content digests

Run merge_index.py afterwards to combine optional agent enrichment into the public index.

Standard library only. Honors HTTPS_PROXY. Optional GITHUB_TOKEN raises rate limit.
"""

from __future__ import annotations

import argparse
import http.client
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from skill_parser import parse_skill_md
from security_scan import scan_skill_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = REPO_ROOT / "registry" / "sources.toml"
BASE_OUT = REPO_ROOT / "registry" / "base-index.json"

API = "https://api.github.com/repos/"
GRAPHQL_API = "https://api.github.com/graphql"
RAW = "https://raw.githubusercontent.com/"
SCHEMA_VERSION = "0.3"  # 0.3 separates deterministic base data from enrichment
MAX_SKILLS_PER_REPO = 15  # cap per repo; truncation is logged, never silent
MAX_EXECUTABLE_FILES_PER_SKILL = 50
_EXECUTABLE_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".ps1", ".rb", ".pl", ".php", ".go", ".rs",
}
_LICENSE_NAMES = {"license", "license.md", "license.txt", "copying", "copying.md"}

_GITHUB_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class BuildError(RuntimeError):
    """A source could not be assessed completely; never publish a partial build."""


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
    attempts = 5
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8")
                return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            secondary_limit = e.code == 403 and (
                "secondary rate limit" in detail.lower()
                or (e.headers and e.headers.get("X-RateLimit-Remaining") == "0")
                or (e.headers and e.headers.get("Retry-After"))
            )
            retryable = e.code in {408, 429, 500, 502, 503, 504} or secondary_limit
            if retryable and attempt < attempts - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    if retry_after:
                        delay = min(60.0, float(retry_after))
                    elif secondary_limit:
                        delay = min(60.0, 15.0 * (attempt + 1))
                    else:
                        delay = min(6.0, 1.5 * (attempt + 1))
                except ValueError:
                    delay = min(6.0, 1.5 * (attempt + 1))
                print(
                    f"  ! {url}: HTTP {e.code}; retrying in {delay:g}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            print(f"  ! {url}: HTTP {e.code}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as e:
            if attempt == attempts - 1:
                print(f"  ! {url}: {e} (gave up)", file=sys.stderr)
                return None
            time.sleep(min(6.0, 1.5 * (attempt + 1)))
    return None


def fetch_repo(repo_id: str, token: str | None) -> dict | None:
    return _get(API + repo_id, token)


def fetch_skill_paths(repo_id: str, branch: str, token: str | None) -> list[str] | None:
    tree = _get(f"{API}{repo_id}/git/trees/{branch}?recursive=1", token)
    if not tree:
        return None
    if not tree.get("truncated"):
        return sorted(
            item["path"] for item in tree.get("tree", [])
            if item.get("type") == "blob" and item.get("path", "").endswith("SKILL.md")
        )

    # GitHub truncates very large recursive trees. Walk subtrees in path order and stop only
    # after enough eligible paths are known for the public per-repo cap.
    print(f"  … {repo_id}: recursive tree truncated; walking subtrees", file=sys.stderr)
    root = _get(f"{API}{repo_id}/git/trees/{branch}", token)
    if not root:
        raise BuildError(f"failed to fetch root Git tree for {repo_id}")
    found: list[str] = []

    def walk_node(node: dict, prefix: str = "") -> None:
        for item in sorted(node.get("tree", []), key=lambda row: row.get("path", "")):
            if len(found) > MAX_SKILLS_PER_REPO:
                return
            path = f"{prefix}/{item['path']}" if prefix else item["path"]
            if item.get("type") == "blob" and path.endswith("SKILL.md") and _eligible_skill_path(path):
                found.append(path)
            elif item.get("type") == "tree":
                walk_sha(item["sha"], path)

    def walk_sha(sha: str, prefix: str) -> None:
        child = _get(f"{API}{repo_id}/git/trees/{sha}?recursive=1", token)
        if not child:
            raise BuildError(f"failed to fetch subtree {prefix} for {repo_id}")
        if child.get("truncated"):
            child = _get(f"{API}{repo_id}/git/trees/{sha}", token)
            if not child:
                raise BuildError(f"failed to fetch subtree root {prefix} for {repo_id}")
            walk_node(child, prefix)
            return
        for item in sorted(child.get("tree", []), key=lambda row: row.get("path", "")):
            if len(found) > MAX_SKILLS_PER_REPO:
                return
            path = f"{prefix}/{item['path']}"
            if item.get("type") == "blob" and path.endswith("SKILL.md") and _eligible_skill_path(path):
                found.append(path)

    walk_node(root)
    return found


def fetch_tree_inventory(repo_id: str, branch: str, token: str | None) -> tuple[list[dict], bool]:
    """Return the complete recursive blob inventory, or an incomplete marker."""
    tree = _get(f"{API}{repo_id}/git/trees/{branch}?recursive=1", token)
    if not tree:
        raise BuildError(f"failed to fetch Git tree inventory for {repo_id}")
    blobs = [item for item in tree.get("tree", []) if item.get("type") == "blob"]
    return blobs, not bool(tree.get("truncated"))


def _eligible_skill_path(path: str) -> bool:
    return not any(
        segment in ("readme", "template", "example", "examples")
        for segment in path.lower().split("/")
    )


def fetch_raw(repo_id: str, branch: str, path: str, token: str | None) -> str | None:
    encoded_path = urllib.parse.quote(path, safe="/")
    encoded_ref = urllib.parse.quote(branch, safe="")
    return _get(
        f"{API}{repo_id}/contents/{encoded_path}?ref={encoded_ref}",
        token,
        accept="application/vnd.github.raw+json",
        raw=True,
    )


def fetch_skill_contents(
    repo_id: str, branch: str, paths: list[str], token: str | None,
) -> list[str | None]:
    """Fetch a repository's selected skill files with one authenticated GraphQL request.

    The REST Contents endpoint is retained as the no-token fallback, but using it once per
    file makes a full build prone to GitHub's secondary rate limit.
    """
    if not token or not paths:
        return [fetch_raw(repo_id, branch, path, token) for path in paths]

    owner, name = repo_id.split("/", 1)
    definitions = ["$owner:String!", "$name:String!"]
    selections: list[str] = []
    variables: dict[str, str] = {"owner": owner, "name": name}
    for index, path in enumerate(paths):
        variable = f"expression{index}"
        definitions.append(f"${variable}:String!")
        selections.append(
            f"file{index}:object(expression:${variable}){{... on Blob{{text}}}}"
        )
        variables[variable] = f"{branch}:{path}"
    query = (
        "query(" + ",".join(definitions) + ")"
        + "{repository(owner:$owner,name:$name){" + "".join(selections) + "}}"
    )
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(GRAPHQL_API, data=body, method="POST")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "awesomeskills-build-index")

    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("errors"):
                print(f"  ! GraphQL blob fetch for {repo_id}: {payload['errors']}", file=sys.stderr)
                return [None] * len(paths)
            repository = payload.get("data", {}).get("repository") or {}
            return [
                (repository.get(f"file{index}") or {}).get("text")
                for index in range(len(paths))
            ]
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            retryable = error.code in {408, 429, 500, 502, 503, 504} or (
                error.code == 403 and "rate limit" in detail.lower()
            )
            if retryable and attempt < 4:
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = min(60.0, float(retry_after)) if retry_after else min(30.0, 3.0 * (attempt + 1))
                except ValueError:
                    delay = min(30.0, 3.0 * (attempt + 1))
                print(
                    f"  ! GraphQL blob fetch for {repo_id}: HTTP {error.code}; retrying in {delay:g}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            print(f"  ! GraphQL blob fetch for {repo_id}: HTTP {error.code}", file=sys.stderr)
            return [None] * len(paths)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
            if attempt == 4:
                print(f"  ! GraphQL blob fetch for {repo_id}: {error} (gave up)", file=sys.stderr)
                return [None] * len(paths)
            time.sleep(min(6.0, 1.5 * (attempt + 1)))
    return [None] * len(paths)


# ---------- signals ----------

def _has_chinese(*texts: str | None) -> bool:
    for t in texts:
        if t and any("一" <= ch <= "鿿" for ch in t):
            return True
    return False


def compute_health(repo: dict, now: datetime) -> tuple[int, dict]:
    """Multi-factor activity/maintenance score (0-100), deliberately de-emphasizing raw stars.

    recency (40) + maintenance/issue-load (25) + real engagement watch+fork ratio (20)
    + maturity (15); archived heavily penalized. Still heuristic, but spreads across repos
    instead of the old naive-recency version that pinned everything to ~100.
    """
    stars = repo.get("stargazers_count") or 0
    open_issues = repo.get("open_issues_count") or 0
    forks = repo.get("forks_count") or 0
    watchers = repo.get("subscribers_count") or 0

    def _days(key):
        v = repo.get(key)
        return (now - datetime.fromisoformat(v.replace("Z", "+00:00"))).days if v else None

    recency_days = _days("pushed_at")
    age_days = _days("created_at")

    # recency: 40 fresh → ~0 by ~80 days stale (steeper, so "recently touched" isn't near-free)
    recency = 0.0 if recency_days is None else max(0.0, 40.0 - recency_days * 0.5)
    # maintenance: open-issue backlog relative to size; stricter threshold
    backlog = open_issues / max(stars, 30)
    maintenance = 25.0 * max(0.0, 1.0 - min(backlog, 0.10) / 0.10)
    # engagement (anti-vanity): watched + forked vs merely starred; harder to max out
    eng_ratio = (watchers + forks) / max(stars, 30)
    engagement = 20.0 * min(1.0, eng_ratio / 0.25)
    # maturity: real history (>~1yr) that is still recently active
    maturity = 0.0
    if age_days is not None and recency_days is not None:
        maturity = 15.0 * min(1.0, age_days / 365.0) * (1.0 if recency_days < 120 else 0.4)

    score = recency + maintenance + engagement + maturity
    if repo.get("archived"):
        score *= 0.3

    factors = {
        "recency_days": recency_days,
        "stars": stars,
        "open_issues": open_issues,
        "forks": forks,
        "watchers": watchers,
        "age_days": age_days,
        "archived": bool(repo.get("archived")),
        "parts": {"recency": round(recency), "maintenance": round(maintenance),
                  "engagement": round(engagement), "maturity": round(maturity)},
    }
    return round(min(100.0, score)), factors


# ---------- entry builders ----------

def _skill_dir(path: str) -> str:
    d = path[: -len("SKILL.md")].rstrip("/")
    return d or "."


def _is_executable_text(item: dict, skill_dir: str) -> bool:
    path = item.get("path", "")
    prefix = "" if skill_dir == "." else skill_dir + "/"
    if prefix and not path.startswith(prefix):
        return False
    relative = path[len(prefix):] if prefix else path
    if relative == "SKILL.md" or relative.lower() in _LICENSE_NAMES:
        return False
    suffix = Path(relative).suffix.lower()
    return "/scripts/" in f"/{relative.lower()}" or suffix in _EXECUTABLE_SUFFIXES or item.get("mode") == "100755"


def _owning_skill_dir(path: str, skill_dirs: list[str]) -> str | None:
    owners = [
        directory for directory in skill_dirs
        if directory == "." or path.startswith(directory.rstrip("/") + "/")
    ]
    return max(owners, key=lambda directory: 0 if directory == "." else len(directory), default=None)


def _license_path(blob_paths: set[str], skill_dir: str) -> str | None:
    normalized = {path.lower(): path for path in blob_paths}
    current = Path("" if skill_dir == "." else skill_dir)
    candidates: list[str] = []
    while True:
        prefix = "" if str(current) in {"", "."} else str(current).rstrip("/") + "/"
        candidates.extend(prefix + name for name in sorted(_LICENSE_NAMES))
        if str(current) in {"", "."}:
            break
        current = current.parent
    return next((normalized[path.lower()] for path in candidates if path.lower() in normalized), None)


def merge_source_entries(existing: list[dict], repo_id: str, replacement: list[dict]) -> list[dict]:
    """Replace one source while preserving every non-target assessment byte-for-byte."""
    return [row for row in existing if row.get("source_repo") != repo_id] + replacement


def merge_source_summaries(existing: dict[str, dict], repo_id: str, replacement: dict) -> dict:
    return {**{key: value for key, value in existing.items() if key != repo_id}, repo_id: replacement}


def build_skill_entries(src: dict, repo: dict, now: datetime, token: str | None) -> list[dict]:
    repo_id = src["id"]
    health, factors = compute_health(repo, now)
    branch = repo.get("default_branch") or "main"

    paths = fetch_skill_paths(repo_id, branch, token)
    if paths is None:
        raise BuildError(f"failed to fetch Git tree for {repo_id}")
    # skip placeholder/non-skill SKILL.md (e.g. a README/ dir, or template/example dirs)
    paths = [path for path in paths if _eligible_skill_path(path)]
    discovered_count = len(paths)
    selection_truncated = discovered_count > MAX_SKILLS_PER_REPO
    if selection_truncated:
        print(f"  … {repo_id}: {len(paths)} SKILL.md found, capping at {MAX_SKILLS_PER_REPO}", file=sys.stderr)
        paths = paths[:MAX_SKILLS_PER_REPO]
    inventory, inventory_complete = fetch_tree_inventory(repo_id, branch, token)
    blob_paths = {item.get("path", "") for item in inventory}
    coverage = {
        "discovered_skill_count": discovered_count if inventory_complete else None,
        "selected_skill_count": len(paths),
        "omitted_skill_count": max(0, discovered_count - len(paths)) if inventory_complete else None,
        "selection_limit": MAX_SKILLS_PER_REPO,
        "complete": inventory_complete and not selection_truncated,
        "reason": "complete" if inventory_complete and not selection_truncated else (
            "selection limit" if inventory_complete else "GitHub tree inventory truncated"
        ),
    }

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
            "trust": {
                "health": health, "health_factors": factors, "security": "unrated",
                "security_scope": "no standard SKILL.md", "security_complete": False,
                "license": "unknown", "collection_coverage": coverage, "zh": _has_chinese(desc),
            },
            "frontmatter": None,
        }]

    entries: list[dict] = []
    skill_dirs = [_skill_dir(candidate) for candidate in paths]
    texts = fetch_skill_contents(repo_id, branch, paths, token)
    failed_paths = [path for path, text in zip(paths, texts) if text is None]
    if failed_paths:
        raise BuildError(f"failed to fetch {len(failed_paths)} SKILL.md file(s) for {repo_id}")
    for path, text in zip(paths, texts):
        skill_dir = _skill_dir(path)
        assert text is not None
        parsed = parse_skill_md(text)
        executable_paths = sorted(
            item["path"] for item in inventory
            if _is_executable_text(item, skill_dir)
            and _owning_skill_dir(item["path"], skill_dirs) == skill_dir
        )
        selected_executable_paths = executable_paths[:MAX_EXECUTABLE_FILES_PER_SKILL]
        executable_texts = fetch_skill_contents(
            repo_id, branch, selected_executable_paths, token,
        )
        executable_files = {
            file_path: content for file_path, content in zip(selected_executable_paths, executable_texts)
            if content is not None
        }
        security_complete = (
            inventory_complete
            and len(executable_paths) <= MAX_EXECUTABLE_FILES_PER_SKILL
            and len(executable_files) == len(selected_executable_paths)
        )
        scan = scan_skill_bundle(text, executable_files, complete=security_complete)
        license_path = _license_path(blob_paths, skill_dir) if inventory_complete else None
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
            "source_ref": branch,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "trust": {
                "health": health,  # inherited from repo
                "health_factors": factors,
                "security": scan["rating"],
                "security_findings": scan["findings"],
                "security_scope": scan.get("scope"),
                "security_complete": scan.get("complete", False),
                "executable_files_discovered": len(executable_paths) if inventory_complete else None,
                "executable_files_scanned": scan.get("executable_files_scanned", 0),
                "license": "known" if license_path else "unknown",
                "license_path": license_path,
                "collection_coverage": coverage,
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


# ---------- deterministic repo assessment + artifact ----------


def build_repo_summaries(entries: list[dict]) -> dict[str, dict]:
    """Build repo grades from deterministic signals only (never LLM/community output)."""
    repos: dict[str, dict] = {}
    for repo_id in sorted({entry["source_repo"] for entry in entries}):
        rows = [entry for entry in entries if entry["source_repo"] == repo_id]
        health = rows[0]["trust"]["health"]
        security = Counter(row["trust"]["security"] for row in rows)
        licenses = Counter(row["trust"].get("license", "unknown") for row in rows)
        coverage = rows[0]["trust"].get("collection_coverage", {})
        frontmatter = [row for row in rows if row.get("frontmatter")]
        fm_rate = (
            sum(1 for row in frontmatter if row["frontmatter"]["valid"]) / len(frontmatter)
            if frontmatter else None
        )
        total = sum(security.values()) or 1
        score = (
            health
            - security.get("fail", 0) / total * 40
            - security.get("warn", 0) / total * 10
            - licenses.get("unknown", 0) / total * 15
        )
        if fm_rate is not None:
            score = score * 0.85 + fm_rate * 100 * 0.15
        score = max(0, min(100, round(score)))
        grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"
        repos[repo_id] = {
            "health": health,
            "security": dict(security),
            "license": dict(licenses),
            "collection_coverage": coverage,
            "frontmatter_pass_rate": round(fm_rate, 2) if fm_rate is not None else None,
            "skill_count": len(rows),
            "overall_score": score,
            "overall_grade": grade,
            "score_policy": "deterministic-v1",
        }
    return repos


def write_base(entries: list[dict], generated_at: str, repo_summaries: dict | None = None) -> None:
    entries.sort(key=lambda e: (e["trust"]["health"], e["name"]), reverse=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "repos": repo_summaries if repo_summaries is not None else build_repo_summaries(entries),
        "skills": entries,
    }
    blob = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = BASE_OUT.with_suffix(BASE_OUT.suffix + ".tmp")
    temporary.write_text(blob, encoding="utf-8")
    os.replace(temporary, BASE_OUT)
    print(f"  wrote {BASE_OUT.relative_to(REPO_ROOT)} ({len(entries)} entries)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only-source", help="rebuild one source and preserve existing non-target assessments",
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    generated_at = now.replace(microsecond=0).isoformat()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("  (no GITHUB_TOKEN — unauthenticated rate limit)", file=sys.stderr)

    sources = load_sources(SOURCES)
    if args.only_source:
        sources = [src for src in sources if src.get("id", "").lower() == args.only_source.lower()]
        if len(sources) != 1:
            print(f"source not found exactly once: {args.only_source}", file=sys.stderr)
            return 2
    print(f"building skill-level index from {len(sources)} sources...")

    existing_entries: list[dict] = []
    existing_repos: dict[str, dict] = {}
    if args.only_source:
        if not BASE_OUT.is_file():
            print("source-scoped build requires an existing base index", file=sys.stderr)
            return 2
        existing_payload = json.loads(BASE_OUT.read_text(encoding="utf-8"))
        existing_entries = existing_payload.get("skills", [])
        existing_repos = existing_payload.get("repos", {})
    entries: list[dict] = []
    failures: list[str] = []
    repo_count = skill_level = repo_level = 0
    for src in sources:
        if not is_public_github_id(src["id"]):
            print(f"  SKIP {src['id']}: not a public github.com owner/repo (internal refs forbidden)", file=sys.stderr)
            continue
        repo = fetch_repo(src["id"], token)
        if repo is None:
            failures.append(f"failed to fetch repository metadata for {src['id']}")
            continue
        repo_count += 1
        try:
            got = build_skill_entries(src, repo, now, token)
        except BuildError as exc:
            failures.append(str(exc))
            continue
        entries.extend(got)
        if got and got[0].get("level") == "skill":
            skill_level += len(got)
            print(f"  ok {src['id']}: {len(got)} skill(s)")
        else:
            repo_level += 1
            print(f"  ok {src['id']}: repo-level (no SKILL.md)")

    if failures:
        print("build incomplete — base index not updated:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    if not entries or repo_count != len(sources):
        print("no complete source set produced — base index not updated", file=sys.stderr)
        return 1

    repo_summaries = None
    if args.only_source:
        repo_id = sources[0]["id"]
        target_summary = build_repo_summaries(entries)
        repo_summaries = merge_source_summaries(existing_repos, repo_id, target_summary[repo_id])
        entries = merge_source_entries(existing_entries, repo_id, entries)
    write_base(entries, generated_at, repo_summaries)
    print(f"done: {len(entries)} entries from {repo_count} repos "
          f"({skill_level} skill-level, {repo_level} repo-level fallbacks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
