"""How a repo that exceeds its cap gets sampled.

Measured failure: `majiayu000/claude-skill-registry` holds 21,141 SKILL.md across twelve
category directories. Taking the first 30 of a sorted list took 30 from `skills/agent/` --
the alphabetically first -- and zero from the other eleven. That is not a sample of the
repo, it is one corner of it, and it is why `skills/other/eli5/SKILL.md` could never appear
no matter how many entries were indexed.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from build_index import (  # noqa: E402
    MAX_SKILLS_PER_REPO, _selection_group, select_skill_paths, source_limit,
)


def paths_for(categories: dict[str, int]) -> list[str]:
    return [f"skills/{c}/skill{i:04d}/SKILL.md" for c, n in categories.items() for i in range(n)]


def spread(paths: list[str]) -> dict[str, int]:
    return collections.Counter(_selection_group(p) for p in paths)


# --- the sampling has to reach past the first directory ----------------------------------

def test_the_cap_spreads_across_directories_instead_of_taking_the_first():
    """The shape that motivated this: one huge early directory and several after it."""
    chosen = select_skill_paths(paths_for({"agent": 1853, "other": 400, "data": 2318}), 30)
    assert len(chosen) == 30
    assert set(spread(chosen)) == {"skills/agent", "skills/data", "skills/other"}
    assert min(spread(chosen).values()) >= 9  # roughly even, not 30/0/0


def test_a_skill_in_a_late_directory_is_now_reachable():
    """`other` sorts after `agent`; under the old head-of-list rule it never appeared."""
    paths = paths_for({"agent": 1853}) + ["skills/other/eli5/SKILL.md"]
    assert "skills/other/eli5/SKILL.md" in select_skill_paths(paths, 30)


def test_a_repo_under_its_cap_is_taken_whole_and_sorted():
    paths = paths_for({"agent": 5, "other": 5})
    assert select_skill_paths(paths, 30) == sorted(paths)


def test_a_directory_smaller_than_its_share_does_not_hold_back_the_rest():
    """One tiny category must not cap the total below the limit."""
    chosen = select_skill_paths(paths_for({"a": 1, "b": 100}), 30)
    assert len(chosen) == 30


def test_selection_is_deterministic():
    paths = paths_for({"a": 50, "b": 50, "c": 50})
    assert select_skill_paths(paths, 30) == select_skill_paths(list(reversed(paths)), 30)


def test_flat_repos_without_categories_still_work():
    paths = [f"skill{i}/SKILL.md" for i in range(50)]
    assert len(select_skill_paths(paths, 30)) == 30


# --- the cap itself is per source --------------------------------------------------------

def test_a_source_without_a_limit_uses_the_default():
    assert source_limit({"id": "o/r"}) == MAX_SKILLS_PER_REPO


def test_a_source_can_raise_its_own_limit():
    """A vetted 31-entry marketplace losing one entry to a default meant for 6,000-entry
    crawls is the cap solving the wrong problem."""
    assert source_limit({"id": "o/r", "limit": 60}) == 60


def test_a_nonsense_limit_falls_back_to_the_default():
    for bad in ({"limit": 0}, {"limit": -5}, {"limit": "many"}, {"limit": None}):
        assert source_limit({"id": "o/r", **bad}) == MAX_SKILLS_PER_REPO


def test_the_registered_marketplaces_are_taken_whole():
    """Both Anthropic marketplaces hold 31 SKILL.md."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    rows = tomllib.loads((root / "registry" / "sources.toml").read_text())["source"]
    by_id = {r["id"]: r for r in rows}
    assert "anthropics/claude-plugins-community" in by_id
    for name in ("anthropics/claude-plugins-official", "anthropics/claude-plugins-community"):
        assert source_limit(by_id[name]) > 31, name
