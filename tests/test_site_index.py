"""Tests for the display-only projection the browser fetches.

The risk here runs one way: dropping a field the page renders degrades the UI silently,
showing "?" instead of a number. Field lists are enumerated by hand, so these tests assert
what the site reads today; the standing check is the differential render (build both
artifacts, render every entry from each, diff the HTML), which is what actually proves the
projection is lossless for display.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from site_index import slim_index, slim_skill  # noqa: E402


def full_entry(**overrides) -> dict:
    entry = {
        "id": "o/r/s", "name": "s", "summary": "does things", "source_repo": "o/r",
        "source_url": "https://github.com/o/r", "kind": "skill", "level": "skill",
        "path": "s/SKILL.md",
        "source_ref": "a" * 40, "source_ref_kind": "commit", "source_branch": "main",
        "content_sha256": "d" * 64,
        "files": [{"path": "s/SKILL.md", "sha256": "d" * 64, "role": "skill"}],
        "files_complete": True,
        "grounding": {"function": {"en": {"purpose": "p"}}},
        "enrichment_status": "fresh",
        "trust": {
            "health": 80, "security": "warn", "zh": True,
            "security_findings": [{"sev": "low", "label": "x", "path": "p"}],
            "security_scope": "SKILL.md + executable text files",
            "security_complete": True,
            "license": "known", "license_path": "LICENSE",
            "executable_files_discovered": 2, "executable_files_scanned": 2,
            "collection_coverage": {"complete": True, "reason": "complete"},
            "health_factors": {
                "recency_days": 1, "stars": 10, "open_issues": 2, "forks": 3,
                "watchers": 4, "age_days": 400, "archived": False, "maintainers": 5,
                "issues_per_maintainer": 0.4,
                "parts": {"recency": 40, "maintenance": 25, "engagement": 8, "maturity": 15},
            },
        },
        "frontmatter": {"valid": True, "issues": [], "headings": 3, "code_blocks": 1},
    }
    return {**entry, **overrides}


# --- every value the grounding panel prints must survive ---------------------------------

def test_health_counters_the_panel_prints_are_kept():
    """The Health section prints stars, open issues, forks and watchers by name. Dropping
    any of them renders a '?' rather than failing loudly."""
    factors = slim_skill(full_entry())["trust"]["health_factors"]
    for key in ("recency_days", "stars", "open_issues", "forks", "watchers", "archived"):
        assert key in factors, key
    assert factors["parts"]["maintenance"] == 25


def test_frontmatter_counts_the_panel_prints_are_kept():
    """The Coverage section prints heading and code-block counts."""
    frontmatter = slim_skill(full_entry())["frontmatter"]
    assert frontmatter["headings"] == 3
    assert frontmatter["code_blocks"] == 1


def test_display_fields_survive():
    row = slim_skill(full_entry())
    for key in ("id", "name", "summary", "source_repo", "source_url", "kind", "level", "grounding"):
        assert key in row, key
    for key in ("health", "security", "zh", "security_findings", "security_scope"):
        assert key in row["trust"], key


# --- verification data must not leak into the display artifact ---------------------------

def test_verification_fields_are_dropped():
    """These are the payload the browser was downloading for nothing; the manifest alone was
    the single largest field."""
    row = slim_skill(full_entry())
    for key in ("files", "files_complete", "content_sha256", "source_ref",
                "source_ref_kind", "source_branch", "enrichment_status"):
        assert key not in row, key
    for key in ("collection_coverage", "license_path", "security_complete",
                "executable_files_discovered", "executable_files_scanned"):
        assert key not in row["trust"], key


def test_slim_index_labels_itself_as_display_only():
    """A slim copy that reads like the real index is a trap: it has no digests, so nothing
    should ever try to verify against it."""
    out = slim_index({"generated_at": "t", "repos": {}, "skills": [full_entry()]})
    assert out["display_only"] is True
    assert out["full_index"] == "index.json"


def test_repos_block_passes_through_for_the_community_layer():
    repos = {"o/r": {"overall_grade": "A", "overall_score": 91, "community": {"en": {}}}}
    out = slim_index({"generated_at": "t", "repos": repos, "skills": []})
    assert out["repos"] == repos


def test_entry_without_frontmatter_stays_without_one():
    row = slim_skill(full_entry(frontmatter=None))
    assert "frontmatter" not in row


def test_every_entry_is_projected():
    out = slim_index({"generated_at": "t", "repos": {}, "skills": [full_entry(), full_entry(id="o/r/t")]})
    assert [row["id"] for row in out["skills"]] == ["o/r/s", "o/r/t"]
