#!/usr/bin/env python3
"""The GitHub side of the build: every call that leaves the machine.

Extracted from build_index.py, which had grown past the point where the fetching and the
assessing could be read separately. Nothing here decides anything about a skill -- it
retrieves trees, blobs, repo metadata and maintainer capacity, and reports honestly when
GitHub will not give up the whole answer.

Two behaviours are load-bearing and easy to lose:

  - a truncated or unfetchable recursive tree is not an error, it is a signal to walk
    subtrees, and callers are told which of the two happened;
  - GraphQL silently truncates a blob at about 512 KB, so anything that matters is refetched
    raw rather than digested from a partial body.

Standard library only. Honors HTTPS_PROXY. Optional GITHUB_TOKEN raises the rate limit.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com/repos/"
GRAPHQL_API = "https://api.github.com/graphql"
MAX_CONTRIBUTOR_PAGE = 100  # single page; maintainer count saturates here, which is fine


class BuildError(RuntimeError):
    """A source could not be assessed completely; never publish a partial build.

    Defined here, beside the calls that raise it, and re-exported by build_index because
    that is where it is caught -- and because two classes of this name would make
    `except BuildError` silently miss the one actually raised.
    """


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


def fetch_skill_paths(
    repo_id: str, branch: str, token: str | None, limit: int,
) -> list[str] | None:
    """Every real SKILL.md path. `limit` is the caller's cap, not this layer's."""
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
            if len(found) > limit:
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
            if len(found) > limit:
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

