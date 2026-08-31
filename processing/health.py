#!/usr/bin/env python3
"""Repo-level health scoring for the awesomeskills index.

Health answers one question: *is this repo actually being maintained?* It deliberately
de-emphasizes raw stars, which carry almost no signal in the Agent Skills ecosystem
(measured 2026-08-31: watcher/star ratios cluster at 0.004-0.012 across every source,
popular and obscure alike).

Scoring is four independent parts out of 100:

    recency (40) + maintenance (25) + engagement (20) + maturity (15)

`maintenance` measures issue backlog against *maintainer capacity*, not popularity.
An earlier version divided the backlog by star count, which made the whole 25 points
dead weight: the threshold was 10% of stars, so any repo above ~5k stars scored full
marks no matter how large its backlog. It also counted `open_issues_count`, which the
GitHub REST API defines as open issues *plus* open pull requests -- that penalized
submission-driven awesome-lists for having a healthy contribution flow (measured:
travisvn/awesome-claude-skills reports 778 "issues" that are 45 issues + 733 PRs).
"""

from __future__ import annotations

from datetime import datetime

RECENCY_POINTS = 40.0
MAINTENANCE_POINTS = 25.0
ENGAGEMENT_POINTS = 20.0
MATURITY_POINTS = 15.0

# Days of staleness at which recency credit reaches zero.
RECENCY_ZERO_DAYS = 80.0
# Open issues per human maintainer at which maintenance credit reaches zero. Calibrated
# against the 2026-08-31 source set, whose true issues-per-maintainer spread was
# 0.0 - 27.8 with a median near 1.7; a cap of 5 leaves the median repo with partial
# credit while genuinely backlogged repos lose the points.
BACKLOG_PER_MAINTAINER_ZERO = 5.0
# watchers+forks over stars at which engagement credit saturates.
ENGAGEMENT_SATURATION_RATIO = 0.25
# Credit awarded when maintainer capacity could not be determined. Unknown is a
# first-class value here: we neither reward nor punish what we did not measure.
UNKNOWN_MAINTENANCE_CREDIT = 0.5
ARCHIVED_MULTIPLIER = 0.3
MATURITY_FULL_DAYS = 365.0
MATURITY_STALE_DAYS = 120
MATURITY_STALE_MULTIPLIER = 0.4


def _age_in_days(repo: dict, key: str, now: datetime) -> int | None:
    stamp = repo.get(key)
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).days
    except (ValueError, TypeError):
        return None


def _recency_credit(recency_days: int | None) -> float:
    if recency_days is None:
        return 0.0
    return max(0.0, RECENCY_POINTS - recency_days * (RECENCY_POINTS / RECENCY_ZERO_DAYS))


def _maintenance_credit(capacity: dict | None, repo: dict) -> tuple[float, dict]:
    """Issue backlog per human maintainer.

    Returns the credit plus the basis used, so a consumer can tell a measured score
    from a fallback. Three bases exist:
      - "issues-per-maintainer": both signals measured; the only fully trusted case.
      - "unknown-capacity": maintainer count unavailable -> neutral partial credit.
      - "issues+prs-per-maintainer": the issue/PR split was unavailable, so the REST
        `open_issues_count` (issues + PRs) stands in and the score reads pessimistic.
    """
    capacity = capacity or {}
    maintainers = capacity.get("maintainers")
    open_issues = capacity.get("open_issues")

    basis = "issues-per-maintainer"
    if open_issues is None:
        open_issues = repo.get("open_issues_count")
        basis = "issues+prs-per-maintainer"
    if not maintainers or open_issues is None:
        return MAINTENANCE_POINTS * UNKNOWN_MAINTENANCE_CREDIT, {
            "maintenance_basis": "unknown-capacity",
            "open_issues": open_issues,
            "maintainers": maintainers,
            "issues_per_maintainer": None,
        }

    per_maintainer = open_issues / maintainers
    load = min(per_maintainer, BACKLOG_PER_MAINTAINER_ZERO) / BACKLOG_PER_MAINTAINER_ZERO
    return MAINTENANCE_POINTS * max(0.0, 1.0 - load), {
        "maintenance_basis": basis,
        "open_issues": open_issues,
        "maintainers": maintainers,
        "issues_per_maintainer": round(per_maintainer, 2),
    }


def _engagement_credit(repo: dict) -> float:
    """Anti-vanity: people who watch or fork, versus people who merely starred."""
    stars = repo.get("stargazers_count") or 0
    forks = repo.get("forks_count") or 0
    watchers = repo.get("subscribers_count") or 0
    ratio = (watchers + forks) / max(stars, 30)
    return ENGAGEMENT_POINTS * min(1.0, ratio / ENGAGEMENT_SATURATION_RATIO)


def _maturity_credit(age_days: int | None, recency_days: int | None) -> float:
    if age_days is None or recency_days is None:
        return 0.0
    staleness = 1.0 if recency_days < MATURITY_STALE_DAYS else MATURITY_STALE_MULTIPLIER
    return MATURITY_POINTS * min(1.0, age_days / MATURITY_FULL_DAYS) * staleness


def compute_health(repo: dict, now: datetime, capacity: dict | None = None) -> tuple[int, dict]:
    """Score 0-100 plus the factors behind it.

    `capacity` carries the signals that the plain repo endpoint cannot supply:
    `maintainers` (human, non-bot contributors) and `open_issues` (issues only, no PRs).
    Both may be absent; the returned factors record which basis was actually used.
    """
    recency_days = _age_in_days(repo, "pushed_at", now)
    age_days = _age_in_days(repo, "created_at", now)

    recency = _recency_credit(recency_days)
    maintenance, maintenance_factors = _maintenance_credit(capacity, repo)
    engagement = _engagement_credit(repo)
    maturity = _maturity_credit(age_days, recency_days)

    score = recency + maintenance + engagement + maturity
    if repo.get("archived"):
        score *= ARCHIVED_MULTIPLIER

    factors = {
        "recency_days": recency_days,
        "stars": repo.get("stargazers_count") or 0,
        "forks": repo.get("forks_count") or 0,
        "watchers": repo.get("subscribers_count") or 0,
        "age_days": age_days,
        "archived": bool(repo.get("archived")),
        **maintenance_factors,
        "open_prs": (capacity or {}).get("open_prs"),
        "top_contributor_commits": (capacity or {}).get("top_contributor_commits"),
        "parts": {
            "recency": round(recency),
            "maintenance": round(maintenance),
            "engagement": round(engagement),
            "maturity": round(maturity),
        },
    }
    return round(min(100.0, score)), factors
