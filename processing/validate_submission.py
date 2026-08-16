#!/usr/bin/env python3
"""Read-only preflight for a proposed public GitHub skill source.

This validates only the submission pointer and observable repository shape. It deliberately
does not calculate or accept trust signals; those belong to the full indexing pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "registry" / "sources.toml"
REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo(value: str) -> str:
    value = value.strip().rstrip("/")
    value = re.sub(r"^https?://(?:www\.)?github\.com/", "", value, flags=re.I)
    if value.endswith(".git"):
        value = value[:-4]
    if not REPO_ID.fullmatch(value):
        raise ValueError("expected a public GitHub owner/repo or repository URL")
    return value


def github(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "awesomeskills-submission-validator",
            **(
                {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
                if os.environ.get("GITHUB_TOKEN")
                else {}
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def existing_ids() -> set[str]:
    import tomllib

    return {source["id"].lower() for source in tomllib.loads(SOURCES.read_text()).get("source", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a source pointer before human review")
    parser.add_argument("repo", help="public GitHub owner/repo or repository URL")
    args = parser.parse_args()

    try:
        repo_id = normalize_repo(args.repo)
        repo = github(f"repos/{repo_id}")
        branch = repo.get("default_branch") or "main"
        tree = github(f"repos/{repo_id}/git/trees/{branch}?recursive=1")
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(f"FAIL: GitHub returned HTTP {exc.code}; confirm the repository is public", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"FAIL: could not verify GitHub: {exc}", file=sys.stderr)
        return 2

    paths = [item["path"] for item in tree.get("tree", []) if item.get("type") == "blob"]
    skill_paths = sorted(path for path in paths if path.endswith("SKILL.md"))
    packages = sorted(path for path in paths if path.endswith(".skill"))

    print(f"repo: {repo.get('full_name', repo_id)}")
    print(f"public: {repo.get('visibility') == 'public' and not repo.get('private', False)}")
    print(f"default_branch: {branch}")
    print(f"already_listed: {repo_id.lower() in existing_ids()}")
    print(f"tree_truncated: {bool(tree.get('truncated'))}")
    print(f"standard_skill_md: {len(skill_paths)}")
    print(f"packaged_dot_skill: {len(packages)}")
    for path in skill_paths[:10]:
        print(f"  SKILL.md: {path}")
    if len(skill_paths) > 10:
        print(f"  ... {len(skill_paths) - 10} more")
    if not skill_paths and packages:
        print("NOTE: .skill packages exist, but the current index builder does not unpack them")
    elif not skill_paths:
        print("NOTE: no standard SKILL.md in the default-branch Git tree; review repo-level scope")

    if repo.get("visibility") != "public" or repo.get("private", False) or tree.get("truncated"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
