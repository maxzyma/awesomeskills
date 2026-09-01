"""The guard that keeps non-public sources out of the index.

A public index must not point at anything its readers cannot reach, so a source id has to be
a bare `owner/repo` on github.com. The guard used to name a specific internal host in both
its condition and its comment; that literal was removed because it published the name of a
private host while adding nothing -- every host-qualified id is already rejected for having
a scheme or an extra path segment. These cases pin that equivalence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from build_index import is_public_github_id  # noqa: E402


@pytest.mark.parametrize("repo_id", ["anthropics/skills", "a/b", "Some-Owner/repo.name"])
def test_a_bare_public_owner_repo_is_accepted(repo_id):
    assert is_public_github_id(repo_id)


@pytest.mark.parametrize("repo_id", [
    "https://example.com/a/b",          # any scheme
    "git@github.com:a/b",               # scp-style remote carries no slash pair
    "internal.host.example/group/repo",  # host-qualified: extra path segment
    "a/b/c",                             # nested path
    "owner",                             # no repo
    "",                                  # nothing
])
def test_anything_not_a_bare_owner_repo_is_rejected(repo_id):
    assert not is_public_github_id(repo_id)


def test_a_host_qualified_internal_id_is_rejected_without_naming_any_host():
    """The shape does the work: an internal GitLab path has a scheme or a group segment, and
    either one fails. No hostname needs to appear in this repository to reject it."""
    for shape in ("https://gitlab.internal.example/group/repo",
                  "gitlab.internal.example/group/repo",
                  "gitlab.internal.example/group/sub/repo"):
        assert not is_public_github_id(shape), shape
