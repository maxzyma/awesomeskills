"""Regression suite for the health formula.

The fixtures are frozen GitHub snapshots taken 2026-08-31. They are a deliberate
*contrast set*: four repos that trended on the same day, in the same ecosystem, with
maintainer structures that differ by orders of magnitude. A formula that cannot tell
them apart is not measuring maintenance.

Assertions are written as relative orderings wherever possible, so retuning a weight
does not require restating every expected score.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from health import (  # noqa: E402
    MAINTENANCE_POINTS,
    UNKNOWN_MAINTENANCE_CREDIT,
    compute_health,
)

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)

# --- frozen snapshots: (repo payload, measured capacity) -------------------------------

SCIENTIFIC = (
    {
        "stargazers_count": 39472, "forks_count": 3668, "subscribers_count": 178,
        "open_issues_count": 22, "created_at": "2025-10-19T20:54:15Z",
        "pushed_at": "2026-08-29T22:55:14Z", "archived": False,
    },
    # org-backed: 47 contributors, top author 310 commits
    {"maintainers": 47, "open_issues": 7, "open_prs": 15, "top_contributor_commits": 310},
)

ARCHIFY = (
    {
        "stargazers_count": 35150, "forks_count": 2251, "subscribers_count": 127,
        "open_issues_count": 76, "created_at": "2026-04-15T05:27:37Z",
        "pushed_at": "2026-08-30T15:05:12Z", "archived": False,
    },
    # single high-output author: 13 contributors, top author 164 of ~205 commits
    {"maintainers": 13, "open_issues": 38, "open_prs": 38, "top_contributor_commits": 164},
)

LAST30DAYS = (
    {
        "stargazers_count": 60551, "forks_count": 5300, "subscribers_count": 218,
        "open_issues_count": 183, "created_at": "2026-01-23T20:37:37Z",
        "pushed_at": "2026-08-30T18:45:02Z", "archived": False,
    },
    # genuine co-maintainers: 98 humans, top two at 459 and 341 commits
    {"maintainers": 98, "open_issues": 98, "open_prs": 85, "top_contributor_commits": 459},
)

PATENT = (
    {
        "stargazers_count": 5782, "forks_count": 705, "subscribers_count": 16,
        "open_issues_count": 8, "created_at": "2026-04-07T15:44:15Z",
        "pushed_at": "2026-08-30T09:31:46Z", "archived": False,
    },
    # solo project: 2 contributors, second contributor has 1 commit
    {"maintainers": 2, "open_issues": 5, "open_prs": 3, "top_contributor_commits": 30},
)

# A submission-driven awesome-list: most of its "open issues" are actually open PRs.
TRAVISVN = (
    {
        "stargazers_count": 14902, "forks_count": 1919, "subscribers_count": 109,
        "open_issues_count": 778, "created_at": "2025-10-16T20:42:39Z",
        "pushed_at": "2026-04-28T19:30:24Z", "archived": False,
    },
    {"maintainers": 2, "open_issues": 45, "open_prs": 733, "top_contributor_commits": 41},
)

ANTHROPIC_SKILLS = (
    {
        "stargazers_count": 172663, "forks_count": 20510, "subscribers_count": 1113,
        "open_issues_count": 1190, "created_at": "2025-09-22T15:53:31Z",
        "pushed_at": "2026-08-21T17:10:55Z", "archived": False,
    },
    {"maintainers": 15, "open_issues": 332, "open_prs": 858, "top_contributor_commits": 14},
)

CONTRAST_SET = {
    "scientific-agent-skills": SCIENTIFIC,
    "archify": ARCHIFY,
    "last30days-skill": LAST30DAYS,
    "patent-disclosure-skill": PATENT,
}


def score(fixture) -> int:
    repo, capacity = fixture
    return compute_health(repo, NOW, capacity)[0]


def factors(fixture) -> dict:
    repo, capacity = fixture
    return compute_health(repo, NOW, capacity)[1]


# --- the defect this suite exists to prevent ------------------------------------------

def test_maintenance_is_not_dead_weight_for_popular_repos():
    """The old formula divided backlog by stars, so every repo over ~5k stars banked the
    full 25 points. Popularity must never buy maintenance credit."""
    awarded = {name: factors(f)["parts"]["maintenance"] for name, f in CONTRAST_SET.items()}
    assert not all(points == round(MAINTENANCE_POINTS) for points in awarded.values()), (
        f"maintenance is flat across the contrast set: {awarded}"
    )
    assert min(awarded.values()) < round(MAINTENANCE_POINTS) * 0.75


def test_contrast_set_is_not_flat():
    """Four same-day repos with 2 to 98 maintainers must not collapse onto one score."""
    scores = {name: score(f) for name, f in CONTRAST_SET.items()}
    assert max(scores.values()) - min(scores.values()) >= 15, scores


def test_solo_project_scores_below_co_maintained_peer():
    """patent-disclosure-skill (2 contributors) and last30days-skill (98) tied at 81 under
    the star-denominator formula despite differing by ~50x in maintainer capacity."""
    assert score(PATENT) < score(LAST30DAYS)


def test_org_backed_collection_scores_highest_in_contrast_set():
    scores = {name: score(f) for name, f in CONTRAST_SET.items()}
    assert scores["scientific-agent-skills"] == max(scores.values()), scores


# --- issue/PR conflation ---------------------------------------------------------------

def test_open_prs_do_not_count_against_maintenance():
    """REST `open_issues_count` bundles PRs. A list whose 778 "issues" are 733 PRs must be
    scored on the 45 real issues."""
    measured = factors(TRAVISVN)
    assert measured["open_issues"] == 45
    assert measured["issues_per_maintainer"] == pytest.approx(22.5)

    repo, _ = TRAVISVN
    conflated = compute_health(repo, NOW, {"maintainers": 2})[1]
    assert conflated["maintenance_basis"] == "issues+prs-per-maintainer"
    assert conflated["open_issues"] == 778
    assert measured["parts"]["maintenance"] >= conflated["parts"]["maintenance"]


def test_real_backlog_still_loses_the_points():
    """Separating PRs out must not amnesty a genuine backlog: 332 open issues across 15
    maintainers is still a backlog."""
    assert factors(ANTHROPIC_SKILLS)["parts"]["maintenance"] == 0


# --- unknown is a first-class value ----------------------------------------------------

def test_unknown_capacity_is_neutral_and_labelled():
    repo, _ = SCIENTIFIC
    unknown = compute_health(repo, NOW, None)[1]
    assert unknown["maintenance_basis"] == "unknown-capacity"
    assert unknown["issues_per_maintainer"] is None
    assert unknown["parts"]["maintenance"] == round(MAINTENANCE_POINTS * UNKNOWN_MAINTENANCE_CREDIT)


def test_zero_maintainers_is_treated_as_unknown_not_as_worst_case():
    repo, _ = PATENT
    degraded = compute_health(repo, NOW, {"maintainers": 0, "open_issues": 5})[1]
    assert degraded["maintenance_basis"] == "unknown-capacity"
    assert degraded["parts"]["maintenance"] > 0


# --- unchanged behaviour that must survive the refactor --------------------------------

def test_archived_repo_is_heavily_penalised():
    repo, capacity = SCIENTIFIC
    archived = compute_health({**repo, "archived": True}, NOW, capacity)[0]
    assert archived < score(SCIENTIFIC) * 0.4


def test_stale_push_drains_recency():
    repo, capacity = SCIENTIFIC
    stale = compute_health({**repo, "pushed_at": "2026-01-01T00:00:00Z"}, NOW, capacity)[1]
    assert stale["parts"]["recency"] == 0


def test_engagement_resists_vanity_stars():
    """Adding stars alone, with no new watchers or forks, must not raise the score."""
    repo, capacity = PATENT
    inflated = compute_health({**repo, "stargazers_count": repo["stargazers_count"] * 10}, NOW, capacity)
    assert inflated[0] < score(PATENT)


def test_scores_stay_in_range():
    for name, fixture in CONTRAST_SET.items():
        assert 0 <= score(fixture) <= 100, name
