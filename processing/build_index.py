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

from health import compute_health
from skill_parser import parse_skill_md
from security_scan import scan_skill_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES = REPO_ROOT / "registry" / "sources.toml"
BASE_OUT = REPO_ROOT / "registry" / "base-index.json"

API = "https://api.github.com/repos/"
GRAPHQL_API = "https://api.github.com/graphql"
RAW = "https://raw.githubusercontent.com/"
# 0.3 separated deterministic base data from enrichment.
# 0.4 pins source_ref to a commit SHA and records a per-file digest manifest, so the
#     verification the finder skill promises can actually be carried out.
SCHEMA_VERSION = "0.4"
# Cap per repo; truncation is logged, never silent. Raised 15 -> 30 on 2026-08-31: this
# makes the mid-size collections complete instead of arbitrarily sampled, while keeping
# registry/index.json small enough for the finder skill to fetch it on every invocation
# (the artifact is pulled per call, see product-definition.md section 5). The mega-
# collections (thousands of SKILL.md) stay truncated at any sane cap; sharding is the
# real fix and is still open.
MAX_SKILLS_PER_REPO = 30
MAX_CONTRIBUTOR_PAGE = 100  # single page; maintainer count saturates here, which is fine
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


class BuildError(RuntimeError):
    """A source could not be assessed completely; never publish a partial build."""


def is_public_github_id(repo_id: str) -> bool:
    """Guard: only public github.com owner/repo ids allowed (no internal internal-host refs)."""
    if "://" in repo_id or "internal-host" in repo_id or repo_id.count("/") != 1:
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


def fetch_commit_sha(repo_id: str, branch: str, token: str | None) -> str | None:
    """Resolve a branch to the commit it currently points at.

    Everything downstream -- file fetches, digests, source_url -- is then pinned to that
    commit. Recording a branch name instead makes the digests unverifiable in practice:
    by the time anyone checks, the branch has moved and there is no way to ask for the
    revision the digest was taken from.
    """
    head = _get(f"{API}{repo_id}/commits/{urllib.parse.quote(branch, safe='')}", token)
    return head.get("sha") if isinstance(head, dict) else None


def fetch_skill_paths(repo_id: str, branch: str, token: str | None) -> list[str] | None:
    tree = _get(f"{API}{repo_id}/git/trees/{branch}?recursive=1", token)
    if tree and not tree.get("truncated"):
        candidates = [i for i in tree.get("tree", []) if i.get("path", "").endswith("SKILL.md")]
        real = [i for i in candidates if _is_real_blob(i)]
        skipped = len(candidates) - len(real)
        if skipped:
            print(f"  … {repo_id}: skipping {skipped} symlinked SKILL.md (alias, not a skill)", file=sys.stderr)
        return sorted(item["path"] for item in real)

    # Either GitHub truncated the recursive tree, or the response never arrived. Both mean
    # the same thing here -- no whole-tree listing -- and both are answered by walking
    # subtrees, whose per-directory responses are small enough to survive a link that could
    # not carry the full one. A 10.6 MB tree failing mid-download used to abort the entire
    # build over a single repository.
    reason = "truncated" if tree else "unavailable"
    print(f"  … {repo_id}: recursive tree {reason}; walking subtrees", file=sys.stderr)
    root = _get(f"{API}{repo_id}/git/trees/{branch}", token)
    if not root:
        raise BuildError(f"failed to fetch root Git tree for {repo_id}")
    found: list[str] = []

    def walk_node(node: dict, prefix: str = "") -> None:
        for item in sorted(node.get("tree", []), key=lambda row: row.get("path", "")):
            if len(found) > MAX_SKILLS_PER_REPO:
                return
            path = f"{prefix}/{item['path']}" if prefix else item["path"]
            if _is_real_blob(item) and path.endswith("SKILL.md") and _eligible_skill_path(path):
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
            if _is_real_blob(item) and path.endswith("SKILL.md") and _eligible_skill_path(path):
                found.append(path)

    walk_node(root)
    return found


def _subtree_blobs(
    repo_id: str, ref: str, directory: str, token: str | None, recursive: bool,
) -> tuple[list[dict], bool]:
    """Blobs of one directory, re-prefixed back to repo-root-relative paths.

    The Git trees endpoint accepts a path-qualified tree-ish (`<ref>:<dir>`) and returns
    paths relative to that directory, so they have to be prefixed to line up with the rest
    of the inventory.
    """
    suffix = "?recursive=1" if recursive else ""
    quoted = f"{urllib.parse.quote(ref, safe='')}:{urllib.parse.quote(directory, safe='/')}"
    node = _get(f"{API}{repo_id}/git/trees/{quoted}{suffix}", token)
    if not node:
        return [], False
    prefix = directory.rstrip("/") + "/"
    blobs = [
        {**item, "path": prefix + item.get("path", "")}
        for item in node.get("tree", [])
        if item.get("type") == "blob"
    ]
    return blobs, not bool(node.get("truncated"))


def _ancestor_dirs(directory: str) -> list[str]:
    parts = [part for part in directory.split("/") if part and part != "."]
    return ["/".join(parts[:depth]) for depth in range(1, len(parts))]


def fetch_tree_inventory(
    repo_id: str, ref: str, token: str | None, skill_dirs: list[str] | None = None,
) -> tuple[list[dict], bool, bool]:
    """Blob inventory for the skills being indexed.

    Returns (blobs, bundle_complete, repo_tree_complete). The two completeness flags mean
    different things and must not be conflated:

      bundle_complete    -- every file belonging to the selected skill directories is
                            accounted for. This is what the security scan and the digest
                            manifest depend on.
      repo_tree_complete -- the whole repository tree was enumerated. Only this can back a
                            claim about how many SKILL.md the repo contains in total.

    GitHub truncates the recursive tree of very large repos. Previously that ended the
    attempt, and every skill in such a repo was marked unverifiable -- even though the
    skills themselves are small and their files are perfectly reachable. Walking the
    selected directories as subtrees gets the bundle in full without enumerating a
    repository that may hold hundreds of thousands of files.
    """
    tree = _get(f"{API}{repo_id}/git/trees/{urllib.parse.quote(ref, safe='')}?recursive=1", token)
    if tree and not tree.get("truncated"):
        blobs = [item for item in tree.get("tree", []) if item.get("type") == "blob"]
        return blobs, True, True

    # As in fetch_skill_paths: a tree that is truncated and a tree that failed to arrive are
    # the same situation, and the scoped walk handles both. Either way the whole tree was
    # not seen, so repo_tree_complete stays false.
    targets = sorted({d for d in (skill_dirs or []) if d and d != "."})
    reason = "truncated" if tree else "unavailable"
    print(
        f"  … {repo_id}: tree inventory {reason}; walking {len(targets)} skill dir(s) as subtrees",
        file=sys.stderr,
    )

    # Root level, non-recursive: enough for a repo-root LICENSE without pulling the world.
    root = _get(f"{API}{repo_id}/git/trees/{urllib.parse.quote(ref, safe='')}", token)
    if not root:
        raise BuildError(f"failed to fetch root Git tree for {repo_id}")
    by_path = {
        item["path"]: item for item in root.get("tree", []) if item.get("type") == "blob"
    }
    bundle_complete = bool(targets)

    # Intermediate directories, non-recursive: the license lookup walks up from the skill
    # directory, so it needs the files sitting directly in each ancestor.
    for ancestor in sorted({a for target in targets for a in _ancestor_dirs(target)}):
        blobs, ok = _subtree_blobs(repo_id, ref, ancestor, token, recursive=False)
        by_path.update({item["path"]: item for item in blobs})
        bundle_complete = bundle_complete and ok

    for target in targets:
        blobs, ok = _subtree_blobs(repo_id, ref, target, token, recursive=True)
        by_path.update({item["path"]: item for item in blobs})
        bundle_complete = bundle_complete and ok

    return list(by_path.values()), bundle_complete, False


SYMLINK_MODE = "120000"


def _is_real_blob(item: dict) -> bool:
    """A regular file, not a symlink.

    Git stores a symlink as a blob whose content is the target path. GitHub's GraphQL
    Blob.text hands back that path string, while the REST Contents endpoint follows the
    link and returns the target's content -- so a symlinked SKILL.md was being indexed as
    a 64-byte path: empty summary, invalid frontmatter, and a security rating of `pass`
    awarded to content nothing had read. A symlinked skill is an alias anyway; the target
    is indexed under its own path.
    """
    return item.get("type") == "blob" and item.get("mode") != SYMLINK_MODE


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


def post_graphql(query: str, variables: dict, token: str, label: str) -> dict | None:
    """POST one GraphQL query, retrying transient failures. Returns `data`, or None."""
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
                print(f"  ! GraphQL {label}: {payload['errors']}", file=sys.stderr)
                return None
            return payload.get("data") or {}
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
                print(f"  ! GraphQL {label}: HTTP {error.code}; retrying in {delay:g}s", file=sys.stderr)
                time.sleep(delay)
                continue
            print(f"  ! GraphQL {label}: HTTP {error.code}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
            if attempt == 4:
                print(f"  ! GraphQL {label}: {error} (gave up)", file=sys.stderr)
                return None
            time.sleep(min(6.0, 1.5 * (attempt + 1)))
    return None


def fetch_repo_capacity(repo_id: str, token: str | None) -> dict:
    """Signals the plain repo endpoint cannot supply, for the maintenance score.

    Two gaps are being closed here. `open_issues_count` bundles open pull requests, so it
    overstates backlog for submission-driven lists; GraphQL reports the two separately.
    And the repo payload says nothing about how many people actually maintain the thing,
    which is the only sensible denominator for a backlog. Every field degrades to None
    independently -- compute_health labels which basis it ended up using.
    """
    maintainers = top_commits = None
    contributors = _get(
        f"{API}{repo_id}/contributors?per_page={MAX_CONTRIBUTOR_PAGE}&anon=0", token
    )
    if isinstance(contributors, list):
        humans = [row for row in contributors if row.get("type") != "Bot"]
        maintainers = len(humans)
        top_commits = max((row.get("contributions") or 0 for row in humans), default=0)

    open_issues = open_prs = None
    if token:
        owner, name = repo_id.split("/", 1)
        data = post_graphql(
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
            "issues(states:OPEN){totalCount} pullRequests(states:OPEN){totalCount}}}",
            {"owner": owner, "name": name},
            token,
            f"issue counts for {repo_id}",
        )
        repository = (data or {}).get("repository") or {}
        if repository:
            open_issues = (repository.get("issues") or {}).get("totalCount")
            open_prs = (repository.get("pullRequests") or {}).get("totalCount")

    return {
        "maintainers": maintainers,
        "top_contributor_commits": top_commits,
        "open_issues": open_issues,
        "open_prs": open_prs,
    }


def fetch_blob_bytes(repo_id: str, ref: str, path: str, token: str | None) -> bytes | None:
    """Raw bytes of one file. Used for blobs GraphQL will not return as text."""
    url = (
        f"{API}{repo_id}/contents/{urllib.parse.quote(path, safe='/')}"
        f"?ref={urllib.parse.quote(ref, safe='')}"
    )
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github.raw+json")
    request.add_header("User-Agent", "awesomeskills-build-index")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            print(f"  ! {url}: HTTP {error.code}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
            if attempt == 2:
                print(f"  ! {url}: {error} (gave up)", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_bundle_files(
    repo_id: str, ref: str, paths: list[str], token: str | None,
) -> list[dict]:
    """Resolve each bundle file to text (scannable) or to a byte digest (binary).

    A skill can ship a binary next to its scripts -- a vendored tarball, for instance.
    GraphQL returns no text for those, which previously made them indistinguishable from a
    failed fetch, so the whole entry was marked incomplete and refused. A binary cannot be
    text-scanned, but it can and should be digested: it is part of what gets installed.
    """
    records = fetch_blob_records(repo_id, ref, paths, token)
    resolved: list[dict] = []
    for path, record in zip(paths, records):
        if record.get("text") is not None:
            resolved.append({
                "path": path, "kind": "text", "text": record["text"],
                "sha256": hashlib.sha256(record["text"].encode("utf-8")).hexdigest(),
            })
            continue
        if not record.get("binary"):
            resolved.append({"path": path, "kind": "failed", "text": None, "sha256": None})
            continue
        payload = fetch_blob_bytes(repo_id, ref, path, token)
        resolved.append({
            "path": path,
            "kind": "binary" if payload is not None else "failed",
            "text": None,
            "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
        })
    return resolved


def fetch_blob_records(
    repo_id: str, branch: str, paths: list[str], token: str | None,
) -> list[dict]:
    """Fetch selected files in one authenticated GraphQL request.

    Each record is {"text": str|None, "binary": bool}. Separating "no text because the blob
    is binary" from "no text because the fetch failed" is what lets a binary asset be
    digested instead of sinking the whole entry.

    The REST Contents endpoint is retained as the no-token fallback, but using it once per
    file makes a full build prone to GitHub's secondary rate limit.
    """
    if not token or not paths:
        return [
            {"text": fetch_raw(repo_id, branch, path, token), "binary": False}
            for path in paths
        ]

    owner, name = repo_id.split("/", 1)
    definitions = ["$owner:String!", "$name:String!"]
    selections: list[str] = []
    variables: dict[str, str] = {"owner": owner, "name": name}
    for index, path in enumerate(paths):
        variable = f"expression{index}"
        definitions.append(f"${variable}:String!")
        selections.append(
            f"file{index}:object(expression:${variable})"
            "{... on Blob{text isTruncated isBinary}}"
        )
        variables[variable] = f"{branch}:{path}"
    query = (
        "query(" + ",".join(definitions) + ")"
        + "{repository(owner:$owner,name:$name){" + "".join(selections) + "}}"
    )
    data = post_graphql(query, variables, token, f"blob fetch for {repo_id}")
    if data is None:
        return [{"text": None, "binary": False} for _ in paths]
    repository = data.get("repository") or {}

    records: list[dict] = []
    for index, path in enumerate(paths):
        blob = repository.get(f"file{index}") or {}
        # GraphQL caps Blob.text at ~512 KB and reports it via isTruncated. Digesting or
        # scanning the truncated prefix would silently describe part of a file as if it
        # were the whole one, so fall back to the Contents endpoint for the full bytes.
        if blob.get("isTruncated"):
            print(f"  … {repo_id}:{path}: GraphQL blob truncated; refetching in full", file=sys.stderr)
            records.append({"text": fetch_raw(repo_id, branch, path, token), "binary": False})
        else:
            records.append({"text": blob.get("text"), "binary": bool(blob.get("isBinary"))})
    return records


def fetch_skill_contents(
    repo_id: str, branch: str, paths: list[str], token: str | None,
) -> list[str | None]:
    """Text of each path, or None. Thin view over fetch_blob_records for SKILL.md itself."""
    return [record["text"] for record in fetch_blob_records(repo_id, branch, paths, token)]


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

    paths = fetch_skill_paths(repo_id, ref, token)
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
