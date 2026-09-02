"""Mining an aggregator for the repositories behind it.

The aggregator was removed as a source because its entries describe someone else's skill:
health measured the aggregator's own commit rate, `source_url` credited the aggregator
rather than the author, and one entry carried health 88 while the author's repo had already
404'd. What it is still good for is leads -- its records name the upstream repo, author,
crawl-time stars and a license class. These tests hold the filters to what they claim.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from mine_sources import candidates, known_sources  # noqa: E402


def record(**overrides) -> dict:
    base = {
        "name": "a-skill", "repo": "owner/repo", "author": "owner", "stars": 1000,
        "license": "MIT", "distribution": "compatible", "category": "development",
    }
    return {**base, **overrides}


# --- the filters must drop what they say they drop ----------------------------------------

def test_a_restricted_record_is_not_a_candidate():
    """The aggregator marks 61% of its own records restricted, with an explicit
    "verify upstream permission before reuse". Those are not leads."""
    assert candidates([record(distribution="restricted")], set(), 300) == []


def test_a_record_with_no_license_class_is_not_a_candidate():
    assert candidates([record(distribution=None)], set(), 300) == []


def test_a_repo_below_the_star_floor_is_not_a_candidate():
    """Roughly 0.8% of upstream repos clear 500 stars; without a floor the output is
    ninety thousand rows of noise."""
    assert candidates([record(stars=10)], set(), 300) == []


def test_a_repo_already_indexed_is_not_a_candidate():
    """Mining should surface what is missing, not restate sources.toml."""
    assert candidates([record(repo="Owner/Repo")], {"owner/repo"}, 300) == []


def test_a_record_naming_no_upstream_repo_is_skipped():
    assert candidates([record(repo=None)], set(), 300) == []


# --- and keep what it says it keeps -------------------------------------------------------

def test_a_permissive_well_starred_new_repo_is_a_candidate():
    out = candidates([record()], set(), 300)
    assert [row["repo"] for row in out] == ["owner/repo"]
    assert out[0]["license_at_crawl"] == "MIT"


def test_records_are_grouped_by_repo_not_listed_per_skill():
    """The unit of adoption is a repository, and how many skills it carries is itself a
    signal about whether it is worth adding."""
    out = candidates([record(name="one"), record(name="two")], set(), 300)
    assert len(out) == 1
    assert out[0]["skill_count"] == 2
    assert sorted(out[0]["skills"]) == ["one", "two"]


def test_candidates_are_ranked_by_stars():
    out = candidates(
        [record(repo="a/low", stars=400), record(repo="a/high", stars=9000)], set(), 300,
    )
    assert [row["repo"] for row in out] == ["a/high", "a/low"]


def test_categories_are_collected_and_sorted():
    out = candidates(
        [record(category="data"), record(category="agent"), record(category="data")], set(), 300,
    )
    assert out[0]["categories"] == ["agent", "data"]


def test_the_skill_sample_is_bounded():
    """A repo with hundreds of skills must not paste all their names into the candidate."""
    out = candidates([record(name=f"s{i}") for i in range(50)], set(), 300)
    assert out[0]["skill_count"] == 50
    assert len(out[0]["skills"]) == 10


# --- the source list it dedupes against is the real one -----------------------------------

def test_known_sources_reads_the_repositorys_own_source_list():
    known = known_sources(Path(__file__).resolve().parent.parent / "registry" / "sources.toml")
    assert "anthropics/skills" in known
    assert all(name == name.lower() for name in known)


def test_the_aggregator_is_no_longer_a_source():
    """It is a discovery feed now. Leaving it in sources.toml would reintroduce entries
    whose trust signals describe the wrong repository."""
    known = known_sources(Path(__file__).resolve().parent.parent / "registry" / "sources.toml")
    assert "majiayu000/claude-skill-registry" not in known


def test_a_missing_source_list_degrades_to_no_dedupe_rather_than_crashing(tmp_path):
    assert known_sources(tmp_path / "absent.toml") == set()
