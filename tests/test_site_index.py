"""Tests for the display-only projection the browser fetches.

It is published as two files: a list that blocks the first render, and a detail half fetched
afterwards that only an expanded row shows. The risk runs one way -- dropping a field the
page renders degrades the UI silently, printing "?" instead of a number -- so the field lists
are asserted here, and the partition itself is checked against the real index rather than a
fixture, because a fixture cannot notice a field the builder started emitting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "processing"))

from site_index import detail_index, detail_skill, list_skill, slim_index  # noqa: E402


def full_entry(**overrides) -> dict:
    entry = {
        "id": "o/r/s", "name": "s", "summary": "does things", "source_repo": "o/r",
        "source_url": "https://github.com/o/r", "kind": "skill", "level": "skill",
        "path": "s/SKILL.md",
        "source_ref": "a" * 40, "source_ref_kind": "commit", "source_branch": "main",
        "content_sha256": "d" * 64,
        "files": [{"path": "s/SKILL.md", "sha256": "d" * 64, "role": "skill"}],
        "files_complete": True,
        "grounding": {"function": {"en": {
            "purpose": "p", "io": "writes files", "boundary": "no network",
            "dependencies": ["Node.js"], "scenarios": ["a scenario"],
        }}},
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


# --- the list half must carry everything needed to paint and search ----------------------

def test_the_list_carries_what_a_row_shows():
    row = list_skill(full_entry())
    for key in ("id", "name", "summary", "source_repo", "source_url", "kind", "level"):
        assert key in row, key
    for key in ("health", "security", "zh"):
        assert key in row["trust"], key


def test_the_list_carries_purpose_because_search_reads_it():
    """The haystack spans both languages, so a row can display a translated purpose and
    still not match a word copied out of it if purpose is deferred."""
    assert list_skill(full_entry())["grounding"]["function"]["en"]["purpose"] == "p"


def test_the_list_carries_frontmatter_validity_because_the_default_sort_gates_on_it():
    """Broken entries sink to the bottom. Deferring `valid` would sort the first paint one
    way and resort it seconds later when the detail arrived."""
    assert list_skill(full_entry())["frontmatter"] == {"valid": True}


def test_enrichment_status_is_in_the_list_because_the_page_discloses_it():
    """A `legacy` assessment predates digest binding, so the page says the revision it was
    written from cannot be confirmed."""
    assert list_skill(full_entry(enrichment_status="legacy"))["enrichment_status"] == "legacy"


def test_the_list_defers_what_only_the_panel_shows():
    row = list_skill(full_entry())
    for key in ("health_factors", "security_findings", "security_scope"):
        assert key not in row["trust"], key
    for key in ("io", "boundary", "dependencies", "scenarios"):
        assert key not in row["grounding"]["function"]["en"], key


# --- the detail half must carry the rest --------------------------------------------------

def test_the_detail_carries_the_panel_fields():
    detail = detail_skill(full_entry())
    for key in ("io", "boundary", "dependencies", "scenarios"):
        assert key in detail["grounding"]["function"]["en"], key
    assert detail["trust"]["health_factors"]["parts"]["maintenance"] == 25
    for key in ("recency_days", "stars", "open_issues", "forks", "watchers", "archived"):
        assert key in detail["trust"]["health_factors"], key
    assert detail["frontmatter"] == {"issues": [], "headings": 3, "code_blocks": 1}


def test_the_detail_is_keyed_by_id():
    out = detail_index({"skills": [full_entry(), full_entry(id="o/r/t")]})
    assert set(out["skills"]) == {"o/r/s", "o/r/t"}


def test_repos_are_split_the_same_way():
    """The grade pill paints on every row; the community write-up is panel-only."""
    repos = {"o/r": {"overall_grade": "A", "overall_score": 91,
                     "community": {"en": {}}, "external": {"hn": {}}}}
    data = {"generated_at": "t", "repos": repos, "skills": []}
    assert slim_index(data)["repos"]["o/r"] == {"overall_grade": "A", "overall_score": 91}
    assert set(detail_index(data)["repos"]["o/r"]) == {"community", "external"}


# --- verification data must not leak into either display artifact ------------------------

def test_verification_fields_are_dropped_from_both_halves():
    """These are the payload the browser was downloading for nothing; the digest manifest
    alone was the single largest field."""
    row, detail = list_skill(full_entry()), detail_skill(full_entry())
    for key in ("files", "files_complete", "content_sha256", "source_ref",
                "source_ref_kind", "source_branch"):
        assert key not in row, key
        assert key not in detail, key
    for key in ("collection_coverage", "license_path", "security_complete",
                "executable_files_discovered", "executable_files_scanned"):
        assert key not in row["trust"], key
        assert key not in detail["trust"], key


def test_slim_index_labels_itself_as_display_only():
    """A slim copy that reads like the real index is a trap: it has no digests, so nothing
    should ever try to verify against it."""
    out = slim_index({"generated_at": "t", "repos": {}, "skills": [full_entry()]})
    assert out["display_only"] is True
    assert out["full_index"] == "index.json"
    assert out["detail_index"] == "site-detail.json"


def test_every_entry_is_projected():
    out = slim_index({"generated_at": "t", "repos": {}, "skills": [full_entry(), full_entry(id="o/r/t")]})
    assert [row["id"] for row in out["skills"]] == ["o/r/s", "o/r/t"]


def test_an_entry_without_grounding_or_frontmatter_projects_cleanly():
    bare = {"id": "o/r/s", "name": "s", "trust": {"health": 1, "security": "unrated"}}
    assert "grounding" not in list_skill(bare)
    assert "frontmatter" not in list_skill(bare)
    assert detail_skill(bare) == {}
    assert detail_index({"skills": [bare]})["skills"] == {}


# --- the split is only sound if the two halves partition the projection ------------------

def leaves(node, prefix="") -> dict:
    """Flatten to dotted paths, so two halves can be compared as sets of values."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            out.update(leaves(value, f"{prefix}.{key}"))
        return out
    return {prefix: json.dumps(node, ensure_ascii=False, sort_keys=True)}


@pytest.mark.parametrize("entry", [full_entry()])
def test_the_halves_never_carry_the_same_field(entry):
    """Overlap is the failure mode that matters: a field present in both can disagree after
    a rebuild, and the client's merge would silently pick the deferred one."""
    assert set(leaves(list_skill(entry))) & set(leaves(detail_skill(entry))) == set()


def _real_index():
    path = ROOT / "registry" / "index.json"
    if not path.exists():
        pytest.skip("registry/index.json not built")
    return json.loads(path.read_text(encoding="utf-8"))


def test_against_the_real_index_the_halves_agree_with_the_source():
    """Run over every published entry, not a fixture: a fixture cannot notice a field the
    builder started emitting, and the projection is hand-enumerated."""
    data = _real_index()
    for entry in data["skills"]:
        source = leaves(entry)
        for path, value in {**leaves(list_skill(entry)), **leaves(detail_skill(entry))}.items():
            assert path in source, f"{entry['id']}: projected {path} is not in the index"
            assert source[path] == value, f"{entry['id']}: {path} disagrees with the index"


def test_against_the_real_index_the_halves_do_not_overlap():
    for entry in _real_index()["skills"]:
        overlap = set(leaves(list_skill(entry))) & set(leaves(detail_skill(entry)))
        assert overlap == set(), f"{entry['id']}: {overlap}"
