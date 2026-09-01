"""Tests for the scoped tree inventory.

GitHub truncates the recursive tree of very large repositories. The inventory used to give
up at that point, which marked every skill in such a repo unverifiable even though the
skills themselves are small and fully reachable. These tests pin the subtree walk and,
just as importantly, the distinction between the two completeness flags -- conflating them
would let a scoped walk back a claim about the whole repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

import build_index  # noqa: E402
import github_api  # noqa: E402

# The per-repo cap belongs to the caller now, so tests state their own.
LIMIT = 30

TOKEN = "t"
REF = "c" * 40


def blob(path, mode="100644"):
    return {"path": path, "type": "blob", "mode": mode}


def tree(path):
    return {"path": path, "type": "tree"}


def tree_url(directory: str | None = None, recursive: bool = False) -> str:
    """The exact URL fetch_tree_inventory builds, so routes cannot match each other."""
    target = REF if directory is None else f"{REF}:{directory}"
    return f"{github_api.API}o/r/git/trees/{target}" + ("?recursive=1" if recursive else "")


def fake_get(routes: dict):
    """Route exact GitHub tree URLs to canned payloads; anything unrouted returns None.

    Matching is exact rather than by substring: the root tree URL is a prefix of every
    subtree URL, so a substring route would answer requests meant for a subtree and quietly
    turn a missing-subtree test into a passing one.
    """
    def get(url, token, accept=None, raw=False):
        return routes.get(url)
    return get


def test_untruncated_tree_takes_the_whole_repo(monkeypatch):
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": False, "tree": [blob("a/SKILL.md"), tree("a"), blob("LICENSE")]},
    }))
    blobs, bundle, repo_tree = github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
    assert {b["path"] for b in blobs} == {"a/SKILL.md", "LICENSE"}
    assert (bundle, repo_tree) == (True, True)


def test_truncated_tree_walks_the_selected_skill_dirs(monkeypatch):
    """The bundle is recoverable even when the repository tree is not."""
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": True, "tree": []},
        tree_url("plugins/demo", recursive=True): {
            "truncated": False,
            "tree": [blob("SKILL.md"), blob("scripts/run.py"), tree("scripts")],
        },
        tree_url("plugins"): {"truncated": False, "tree": [blob("NOTICE")]},
        tree_url(): {"truncated": False, "tree": [blob("LICENSE"), tree("plugins")]},
    }))
    blobs, bundle, repo_tree = github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["plugins/demo"])
    paths = {b["path"] for b in blobs}

    assert "plugins/demo/SKILL.md" in paths, "subtree paths must be re-prefixed to repo root"
    assert "plugins/demo/scripts/run.py" in paths
    assert "LICENSE" in paths, "root level is needed for the license walk-up"
    assert "plugins/NOTICE" in paths, "intermediate dirs are needed for the license walk-up"
    assert bundle is True
    assert repo_tree is False, "a scoped walk must never claim the whole tree was seen"


def test_scoped_walk_does_not_claim_repo_wide_counts(monkeypatch):
    """repo_tree_complete gates discovered_skill_count. If the scoped walk set it, a repo
    whose SKILL.md enumeration stopped early would report that partial count as the total."""
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": True, "tree": []},
        tree_url(): {"truncated": False, "tree": []},
        tree_url("a", recursive=True): {"truncated": False, "tree": [blob("SKILL.md")]},
    }))
    _, _, repo_tree = github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
    assert repo_tree is False


def test_failed_subtree_marks_the_bundle_incomplete(monkeypatch):
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": True, "tree": []},
        tree_url(): {"truncated": False, "tree": []},
        # the subtree for "a" is unrouted -> None
    }))
    _, bundle, _ = github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
    assert bundle is False


def test_truncated_subtree_marks_the_bundle_incomplete(monkeypatch):
    """A subtree can itself be too large. Partial is not complete."""
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": True, "tree": []},
        tree_url(): {"truncated": False, "tree": []},
        tree_url("a", recursive=True): {"truncated": True, "tree": [blob("SKILL.md")]},
    }))
    _, bundle, _ = github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
    assert bundle is False


def test_no_skill_dirs_cannot_be_complete(monkeypatch):
    """A repo-level entry in a truncated repo has nothing scoped to walk, so nothing was
    proven about its files."""
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": True, "tree": []},
        tree_url(): {"truncated": False, "tree": [blob("README.md")]},
    }))
    _, bundle, repo_tree = github_api.fetch_tree_inventory("o/r", REF, TOKEN, [])
    assert (bundle, repo_tree) == (False, False)


def test_unfetchable_root_tree_fails_the_build(monkeypatch):
    monkeypatch.setattr(github_api, "_get", fake_get({}))
    try:
        github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
    except github_api.BuildError:
        return
    raise AssertionError("an unfetchable tree must fail the build, not publish a partial one")


def test_ancestor_dirs_enumerates_every_level():
    assert github_api._ancestor_dirs("a/b/c") == ["a", "a/b"]
    assert github_api._ancestor_dirs("a") == []
    assert github_api._ancestor_dirs(".") == []


# --- symlinks ----------------------------------------------------------------------------

def symlink(path):
    return {"path": path, "type": "blob", "mode": github_api.SYMLINK_MODE}


def test_symlinked_skill_md_is_not_indexed(monkeypatch):
    """Git stores a symlink as a blob holding the target path. GraphQL hands back that path
    string while REST follows the link, so a symlinked SKILL.md was indexed as a 64-byte
    path: empty summary, invalid frontmatter, and a security rating of `pass` awarded to
    content nothing had read."""
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": False, "tree": [
            blob("real/SKILL.md"),
            symlink("alias/SKILL.md"),
        ]},
    }))
    assert github_api.fetch_skill_paths("o/r", REF, TOKEN, LIMIT) == ["real/SKILL.md"]


def test_symlinked_skill_md_is_skipped_in_the_subtree_walk(monkeypatch):
    """fetch_skill_paths descends by tree object SHA, so the symlink filter has to hold on
    that path too, not only on the flat recursive listing."""
    subtree_sha = "e" * 40
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(recursive=True): {"truncated": True, "tree": []},
        tree_url(): {"truncated": False, "tree": [{**tree("a"), "sha": subtree_sha}]},
        f"{github_api.API}o/r/git/trees/{subtree_sha}?recursive=1": {
            "truncated": False, "tree": [blob("SKILL.md"), symlink("link/SKILL.md")],
        },
    }))
    found = github_api.fetch_skill_paths("o/r", REF, TOKEN, LIMIT)
    assert found == ["a/SKILL.md"], found


def test_symlinked_script_is_not_treated_as_executable_text():
    """Same failure mode one level down: the digest would cover the link target's path
    rather than the code that runs."""
    assert build_index._is_executable_text(blob("s/scripts/run.py", "100755"), "s")
    assert not build_index._is_executable_text(symlink("s/scripts/run.py"), "s")


def test_regular_blob_is_still_a_real_blob():
    assert github_api._is_real_blob(blob("a.py"))
    assert github_api._is_real_blob(blob("a.py", "100755"))
    assert not github_api._is_real_blob(symlink("a.py"))
    assert not github_api._is_real_blob(tree("a"))


# --- an unfetchable recursive tree is not a dead end --------------------------------------

def test_skill_paths_fall_back_when_the_recursive_tree_never_arrives(monkeypatch):
    """A 10.6 MB tree failing mid-download aborted an entire build over one repository. The
    per-directory responses the subtree walk uses are small enough to survive a link that
    could not carry the whole listing."""
    subtree_sha = "f" * 40
    monkeypatch.setattr(github_api, "_get", fake_get({
        # the recursive URL is unrouted -> None, as if the fetch had failed
        tree_url(): {"truncated": False, "tree": [{**tree("a"), "sha": subtree_sha}]},
        f"{github_api.API}o/r/git/trees/{subtree_sha}?recursive=1": {
            "truncated": False, "tree": [blob("SKILL.md")],
        },
    }))
    assert github_api.fetch_skill_paths("o/r", REF, TOKEN, LIMIT) == ["a/SKILL.md"]


def test_inventory_falls_back_when_the_recursive_tree_never_arrives(monkeypatch):
    monkeypatch.setattr(github_api, "_get", fake_get({
        tree_url(): {"truncated": False, "tree": [blob("LICENSE")]},
        tree_url("a", recursive=True): {"truncated": False, "tree": [blob("SKILL.md")]},
    }))
    blobs, bundle, repo_tree = github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
    assert {b["path"] for b in blobs} == {"LICENSE", "a/SKILL.md"}
    assert bundle is True
    assert repo_tree is False, "a fetch that failed saw no more of the tree than a truncated one"


def test_a_total_outage_still_fails_the_build(monkeypatch):
    """The fallback covers an oversized response, not an unreachable API. If even the small
    root listing cannot be had, there is nothing to walk."""
    monkeypatch.setattr(github_api, "_get", fake_get({}))
    with pytest.raises(github_api.BuildError):
        github_api.fetch_skill_paths("o/r", REF, TOKEN, LIMIT)
    with pytest.raises(github_api.BuildError):
        github_api.fetch_tree_inventory("o/r", REF, TOKEN, ["a"])
