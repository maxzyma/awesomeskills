#!/usr/bin/env python3
"""Project the full index down to what the browser actually renders.

The full `index.json` is the machine-readable artifact: agents and verify_skill.py need the
pinned refs and the per-file digest manifest. A browser needs none of that, but it was
downloading all of it before the first row could appear -- the digest manifest alone is the
single largest field in the payload.

This projection is display-only. Anything a verifier depends on is deliberately absent, so
the slim file can never be mistaken for a source of verification truth.
"""

from __future__ import annotations

# Kept because the site renders them. Everything not listed here is dropped.
SKILL_FIELDS = (
    "id", "name", "summary", "source_repo", "source_url", "kind", "level", "grounding",
)
TRUST_FIELDS = (
    "health", "security", "zh", "health_factors", "security_findings", "security_scope",
)
# Every counter the grounding panel prints, plus the score breakdown.
HEALTH_FACTOR_FIELDS = (
    "recency_days", "stars", "open_issues", "forks", "watchers", "age_days", "archived",
    "maintainers", "issues_per_maintainer", "parts",
)
FRONTMATTER_FIELDS = ("valid", "issues", "headings", "code_blocks")


def slim_skill(entry: dict) -> dict:
    trust = entry.get("trust") or {}
    factors = trust.get("health_factors") or {}
    slim_trust = {key: trust[key] for key in TRUST_FIELDS if key in trust}
    if factors:
        slim_trust["health_factors"] = {
            key: factors[key] for key in HEALTH_FACTOR_FIELDS if key in factors
        }
    row = {key: entry[key] for key in SKILL_FIELDS if key in entry}
    row["trust"] = slim_trust
    if entry.get("frontmatter"):
        row["frontmatter"] = {
            key: entry["frontmatter"][key]
            for key in FRONTMATTER_FIELDS if key in entry["frontmatter"]
        }
    return row


def slim_index(data: dict) -> dict:
    """Display-only projection of the full index."""
    return {
        "generated_at": data.get("generated_at"),
        "display_only": True,
        "full_index": "index.json",
        "repos": data.get("repos", {}),
        "skills": [slim_skill(entry) for entry in data.get("skills", [])],
    }
