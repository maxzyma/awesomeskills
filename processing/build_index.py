#!/usr/bin/env python3
"""Build the awesomeskills static index from registry/sources.toml.

Offline batch job — NO server. For each curated source repo it:
  - queries the GitHub API for real activity signals -> heuristic health (repo-level)
  - finds every SKILL.md in the repo tree and parses it into a SKILL-LEVEL entry
    (name/description/validation), so the index granularity is the *skill*, not the repo
  - repos with no SKILL.md (awesome-lists, hubs) fall back to a single repo-level entry

Emits one deterministic artifact:
  - registry/base-index.json     health / security / frontmatter / content digests

Every call that leaves the machine lives in github_api.py; this module decides what the
results mean.

Run merge_index.py afterwards to combine optional agent enrichment into the public index.

Standard library only. Network access, proxy handling and rate limits are github_api's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from health import compute_health
from skill_parser import parse_skill_md
from security_scan import scan_skill_bundle
from github_api import (
    BuildError,
    _eligible_skill_path,
    _is_real_blob,
    fetch_bundle_files,
    fetch_commit_sha,
    fetch_repo,
    fetch_repo_capacity,
    fetch_skill_contents,
    fetch_skill_paths,
    fetch_tree_inventory,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = REPO_ROOT / "registry" / "sources.toml"
BASE_OUT = REPO_ROOT / "registry" / "base-index.json"

# 0.3 separated deterministic base data from enrichment.
# 0.4 pins source_ref to a commit SHA and records a per-file digest manifest, so the
#     verification the finder skill promises can actually be carried out.
SCHEMA_VERSION = "0.4"
# Cap per repo; truncation is logged, never silent. Raised 15 -> 30 on 2026-08-31: this
# makes the mid-size collections complete instead of arbitrarily sampled, while keeping
# index.json small enough for the finder skill to fetch it on every invocation
# (the artifact is pulled per call, see product-definition.md section 5). The mega-
# collections (thousands of SKILL.md) stay truncated at any sane cap; sharding is the
# real fix and is still open.
MAX_SKILLS_PER_REPO = 30
# Per-skill cap on executable files scanned and digested. Raised 50 -> 200 on 2026-08-31.
# The distribution is extremely skewed: median 0 executable files per skill, p90 = 4, and
# only 7 of 408 skills exceeded 50. Since the cost is only paid by skills that actually
# have the files, a cap well above the observed maximum (134) is close to free -- it took
# the whole index from 671 to 1354 digested files -- while still bounding a pathological
# repo. Skills over the cap are marked incomplete and refused by verify_skill.py.
MAX_EXECUTABLE_FILES_PER_SKILL = 200
_EXECUTABLE_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".ps1", ".rb", ".pl", ".php", ".go", ".rs",
}
_LICENSE_NAMES = {"license", "license.md", "license.txt", "copying", "copying.md"}

_GITHUB_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def is_public_github_id(repo_id: str) -> bool:
    """Guard: only a bare public github.com `owner/repo` is accepted.

    A host-qualified id -- any scheme, or any extra path segment -- is rejected, which is
    what keeps a non-GitHub or otherwise unreachable source out of the index.
    """
    if "://" in repo_id or repo_id.count("/") != 1:
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



# ---------- signals ----------

def _has_chinese(*texts: str | None) -> bool:
    for t in texts:
        if t and any("一" <= ch <= "鿿" for ch in t):
            return True
    return False


# ---------- entry builders ----------

def _file_manifest(skill_path: str, skill_text: str, bundle: list[dict]) -> list[dict]:
    """Path + SHA-256 for every bundle file this build resolved, SKILL.md first.

    Binary files carry the digest of their bytes and a role that says they were never
    text-scanned. Text files digest the UTF-8 encoding of their content, which is the same
    bytes -- so a verifier can hash raw bytes uniformly regardless of role.
    """
    rows = [{
        "path": skill_path,
        "sha256": hashlib.sha256(skill_text.encode("utf-8")).hexdigest(),
        "role": "skill",
    }]
    rows.extend(
        {
            "path": row["path"],
            "sha256": row["sha256"],
            "role": "executable" if row["kind"] == "text" else "binary-unscanned",
        }
        for row in sorted(bundle, key=lambda item: item["path"])
        if row["kind"] in {"text", "binary"}
    )
    return rows


def _skill_dir(path: str) -> str:
    d = path[: -len("SKILL.md")].rstrip("/")
    return d or "."


def _is_executable_text(item: dict, skill_dir: str) -> bool:
    if not _is_real_blob(item):
        return False  # a symlinked script would be digested as its target path, not its code
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


def build_skill_entries(
    src: dict, repo: dict, now: datetime, token: str | None, capacity: dict | None = None,
) -> list[dict]:
    repo_id = src["id"]
    health, factors = compute_health(repo, now, capacity)
    branch = repo.get("default_branch") or "main"
    commit_sha = fetch_commit_sha(repo_id, branch, token)
    if commit_sha is None:
        print(
            f"  … {repo_id}: could not resolve {branch} to a commit; entries stay pinned to "
            "the branch and are marked unverifiable",
            file=sys.stderr,
        )
    # Read the whole repo at one immutable revision, so digests and content cannot drift
    # apart mid-build.
    ref = commit_sha or branch
    ref_kind = "commit" if commit_sha else "branch"

    paths = fetch_skill_paths(repo_id, ref, token, MAX_SKILLS_PER_REPO)
    if paths is None:
        raise BuildError(f"failed to fetch Git tree for {repo_id}")
    # skip placeholder/non-skill SKILL.md (e.g. a README/ dir, or template/example dirs)
    paths = [path for path in paths if _eligible_skill_path(path)]
    discovered_count = len(paths)
    selection_truncated = discovered_count > MAX_SKILLS_PER_REPO
    if selection_truncated:
        print(f"  … {repo_id}: {len(paths)} SKILL.md found, capping at {MAX_SKILLS_PER_REPO}", file=sys.stderr)
        paths = paths[:MAX_SKILLS_PER_REPO]
    skill_dirs = [_skill_dir(candidate) for candidate in paths]
    inventory, bundle_complete, repo_tree_complete = fetch_tree_inventory(
        repo_id, ref, token, skill_dirs,
    )
    blob_paths = {item.get("path", "") for item in inventory}
    # Only a full tree walk can back a count of what the repo holds. The scoped walk gets
    # every file of the selected skills, which is a different claim.
    coverage = {
        "discovered_skill_count": discovered_count if repo_tree_complete else None,
        "selected_skill_count": len(paths),
        "omitted_skill_count": max(0, discovered_count - len(paths)) if repo_tree_complete else None,
        "selection_limit": MAX_SKILLS_PER_REPO,
        "complete": repo_tree_complete and not selection_truncated,
        "reason": "complete" if repo_tree_complete and not selection_truncated else (
            "selection limit" if repo_tree_complete else "GitHub tree inventory truncated"
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
            "source_ref": ref,
            "source_ref_kind": ref_kind,
            "source_branch": branch,
            # No SKILL.md means there is no skill bundle to digest. Say so explicitly rather
            # than omitting the field, so a verifier refuses instead of silently passing.
            "files": [],
            "files_complete": False,
            "trust": {
                "health": health, "health_factors": factors, "security": "unrated",
                "security_scope": "no standard SKILL.md", "security_complete": False,
                "license": "unknown", "collection_coverage": coverage, "zh": _has_chinese(desc),
            },
            "frontmatter": None,
        }]

    entries: list[dict] = []
    texts = fetch_skill_contents(repo_id, ref, paths, token)
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
        bundle = fetch_bundle_files(repo_id, ref, selected_executable_paths, token)
        executable_files = {
            row["path"]: row["text"] for row in bundle if row["kind"] == "text"
        }
        binary_files = [row["path"] for row in bundle if row["kind"] == "binary"]
        unresolved = [row["path"] for row in bundle if row["kind"] == "failed"]
        # Digests can cover a binary; the text scan cannot read one. Keeping the two facts
        # apart is what lets a skill shipping a vendored tarball stay verifiable while its
        # rating still says the tarball was never scanned.
        files_resolved = not unresolved and len(executable_paths) <= MAX_EXECUTABLE_FILES_PER_SKILL
        security_complete = bundle_complete and files_resolved and not binary_files
        scan = scan_skill_bundle(
            text, executable_files, complete=security_complete, binary_files=binary_files,
        )
        license_path = _license_path(blob_paths, skill_dir) if bundle_complete else None
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
            "source_ref": ref,
            "source_ref_kind": ref_kind,   # "commit" = digests are verifiable; "branch" = not
            "source_branch": branch,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            # Digest manifest for the whole bundle, not just SKILL.md. The executable files
            # are the part that actually runs, so verifying only the prompt file would check
            # the least dangerous thing in the skill.
            "files": _file_manifest(path, text, bundle),
            "files_complete": bundle_complete and files_resolved,
            "trust": {
                "health": health,  # inherited from repo
                "health_factors": factors,
                "security": scan["rating"],
                "security_findings": scan["findings"],
                "security_scope": scan.get("scope"),
                "security_complete": scan.get("complete", False),
                "executable_files_discovered": len(executable_paths) if bundle_complete else None,
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
        capacity = fetch_repo_capacity(src["id"], token)
        if capacity["maintainers"] is None or capacity["open_issues"] is None:
            print(
                f"  … {src['id']}: partial maintenance capacity "
                f"(maintainers={capacity['maintainers']}, open_issues={capacity['open_issues']}); "
                "health records the degraded basis",
                file=sys.stderr,
            )
        try:
            got = build_skill_entries(src, repo, now, token, capacity)
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
