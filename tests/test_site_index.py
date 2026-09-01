"""Tests for the display-only projection the browser fetches.

It is published as a list that blocks the first render, plus one small file per skill and per
repo, fetched only when a row is expanded. The risk runs one way -- dropping a field the page
renders degrades the UI silently, printing "?" instead of a number -- so the field lists are
asserted here, and the partition itself is checked against the real index rather than a
fixture, because a fixture cannot notice a field the builder started emitting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "processing"))

from site_index import (  # noqa: E402
    detail_files, detail_path, detail_skill, list_skill, repo_detail_path, slim_index,
)


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


def test_every_published_filename_is_unique():
    """The flattening is only injective while no id contains the separator; a collision
    would silently serve one skill's grounding under another's name."""
    data = _real_index()
    paths = [p for p in detail_files(data)]
    assert len(paths) == len(set(paths))
    assert all(".." not in p for p in paths)


def test_the_list_carries_health_factors_because_every_row_tooltips_them():
    """The health badge shows the score breakdown on hover, on every visible row. Once the
    detail half is only fetched on expand, deferring these would mean a hover shows nothing
    on a page nobody expands."""
    factors = list_skill(full_entry())["trust"]["health_factors"]
    for key in ("recency_days", "stars", "open_issues", "forks", "watchers", "archived"):
        assert key in factors, key
    assert factors["parts"]["maintenance"] == 25


def test_the_list_defers_what_only_the_panel_shows():
    """Only the grounding prose. Everything a badge or tooltip reads stays in the list, so
    an unexpanded row never needs a second request."""
    row = list_skill(full_entry())
    for key in ("io", "boundary", "dependencies", "scenarios"):
        assert key not in row["grounding"]["function"]["en"], key


# --- the detail half must carry the rest --------------------------------------------------

def test_the_detail_carries_the_panel_fields():
    detail = detail_skill(full_entry())
    for key in ("io", "boundary", "dependencies", "scenarios"):
        assert key in detail["grounding"]["function"]["en"], key
    assert detail["frontmatter"] == {"issues": [], "headings": 3, "code_blocks": 1}


def test_the_security_tooltip_fields_stay_in_the_list():
    """The security badge tooltips its findings on every visible row. In a per-file layout
    that would be one request per row just to hover, so they ride in the list."""
    trust = list_skill(full_entry())["trust"]
    assert trust["security_findings"][0]["label"] == "x"
    assert trust["security_scope"] == "SKILL.md + executable text files"


def test_there_is_one_file_per_skill():
    files = detail_files({"skills": [full_entry(), full_entry(id="o/r/t")]})
    assert set(files) == {"detail/skill/o__r__s.json", "detail/skill/o__r__t.json"}


def test_repos_are_split_the_same_way():
    """The grade pill paints on every row; the community write-up is panel-only, and shared
    by every skill in the repo rather than copied into each of their files."""
    repos = {"o/r": {"overall_grade": "A", "overall_score": 91,
                     "community": {"en": {}}, "external": {"hn": {}}}}
    data = {"generated_at": "t", "repos": repos, "skills": []}
    assert slim_index(data)["repos"]["o/r"] == {
        "overall_grade": "A", "overall_score": 91, "detail": "detail/repo/o__r.json"}
    assert set(detail_files(data)["detail/repo/o__r.json"]) == {"community", "external"}


def test_skill_and_repo_namespaces_cannot_collide():
    """A two-segment skill id flattens to `owner__repo`, which is exactly what a repo file
    would be called. Seven published entries have ids that short."""
    assert detail_path("o/r") != repo_detail_path("o/r")


def test_the_list_names_the_file_so_the_rule_is_not_written_twice():
    """The browser follows this path rather than rebuilding it from the id, so the flattening
    rule exists in one language instead of two."""
    assert list_skill(full_entry())["detail"] == "detail/skill/o__r__s.json"


def test_an_entry_with_no_assessment_names_no_file():
    """Pointing at a file that was never written spends a round trip to learn nothing."""
    bare = {"id": "o/r/s", "name": "s", "trust": {"health": 1}}
    assert "detail" not in list_skill(bare)
    assert detail_files({"skills": [bare]}) == {}


# --- verification data must not leak into either display artifact ------------------------

def test_verification_fields_are_dropped_from_both_halves():
    """These are the payload the browser was downloading for nothing; the digest manifest
    alone was the single largest field."""
    row, detail = list_skill(full_entry()), detail_skill(full_entry())
    for key in ("files", "files_complete", "content_sha256", "source_ref",
                "source_ref_kind", "source_branch"):
        assert key not in row, key
        assert key not in detail, key
    # The detail half carries no trust block at all; the list's is field-limited.
    assert "trust" not in detail
    for key in ("collection_coverage", "license_path", "security_complete",
                "executable_files_discovered", "executable_files_scanned"):
        assert key not in row["trust"], key


def test_slim_index_labels_itself_as_display_only():
    """A slim copy that reads like the real index is a trap: it has no digests, so nothing
    should ever try to verify against it."""
    out = slim_index({"generated_at": "t", "repos": {}, "skills": [full_entry()]})
    assert out["display_only"] is True
    assert out["full_index"] == "index.json"


def test_every_entry_is_projected():
    out = slim_index({"generated_at": "t", "repos": {}, "skills": [full_entry(), full_entry(id="o/r/t")]})
    assert [row["id"] for row in out["skills"]] == ["o/r/s", "o/r/t"]


def test_an_entry_without_grounding_or_frontmatter_projects_cleanly():
    bare = {"id": "o/r/s", "name": "s", "trust": {"health": 1, "security": "unrated"}}
    assert "grounding" not in list_skill(bare)
    assert "frontmatter" not in list_skill(bare)
    assert detail_skill(bare) == {}


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
    path = ROOT / "site" / "public" / "index.json"
    if not path.exists():
        pytest.skip("site/public/index.json not built")
    return json.loads(path.read_text(encoding="utf-8"))


def test_against_the_real_index_the_halves_agree_with_the_source():
    """Run over every published entry, not a fixture: a fixture cannot notice a field the
    builder started emitting, and the projection is hand-enumerated."""
    data = _real_index()
    for entry in data["skills"]:
        source = leaves(entry)
        projected = {**leaves(list_skill(entry)), **leaves(detail_skill(entry))}
        # `.detail` and `.verify` are pointers this projection invents; neither has a
        # counterpart upstream.
        projected.pop(".detail", None)
        projected.pop(".verify", None)
        for path, value in projected.items():
            assert path in source, f"{entry['id']}: projected {path} is not in the index"
            assert source[path] == value, f"{entry['id']}: {path} disagrees with the index"


def test_against_the_real_index_the_halves_do_not_overlap():
    for entry in _real_index()["skills"]:
        overlap = set(leaves(list_skill(entry))) & set(leaves(detail_skill(entry)))
        assert overlap == set(), f"{entry['id']}: {overlap}"


# --- the published tree must not accumulate files nobody points at ------------------------

def test_a_file_for_a_departed_skill_is_deleted(tmp_path):
    """Without pruning, a skill removed from the index keeps serving its old grounding
    forever, and the reproducibility check cannot tell a correct tree from one carrying
    leftovers -- a stale file matches nothing, so nothing reports it."""
    sys.path.insert(0, str(ROOT / "processing"))
    from merge_index import _write_tree

    directory = tmp_path / "detail"
    _write_tree(directory, detail_files({"skills": [full_entry(), full_entry(id="o/r/t")]}))
    assert (directory / "skill" / "o__r__t.json").exists()

    _write_tree(directory, detail_files({"skills": [full_entry()]}))
    assert (directory / "skill" / "o__r__s.json").exists()
    assert not (directory / "skill" / "o__r__t.json").exists()


def test_pruning_reaches_into_every_namespace(tmp_path):
    """The tree has a subdirectory per namespace, so a top-level-only sweep would leave
    every stale file untouched."""
    from merge_index import _write_tree

    directory = tmp_path / "detail"
    repos = {"o/r": {"community": {"en": {}}}}
    _write_tree(directory, detail_files({"skills": [full_entry()], "repos": repos}))
    for namespace, name in (("skill", "junk__x.json"), ("repo", "junk.json")):
        (directory / namespace / name).write_text("{}", encoding="utf-8")

    _write_tree(directory, detail_files({"skills": [full_entry()], "repos": repos}))
    assert not (directory / "skill" / "junk__x.json").exists()
    assert not (directory / "repo" / "junk.json").exists()
    assert (directory / "repo" / "o__r.json").exists()


# --- verification data is published per skill too -----------------------------------------

def test_a_skill_verification_record_carries_what_a_check_needs():
    from site_index import verify_files, verify_path
    record = verify_files({"skills": [full_entry()]})[verify_path("o/r/s")]
    for key in ("source_repo", "source_ref", "source_ref_kind", "files", "files_complete"):
        assert key in record, key


def test_the_verification_record_carries_the_security_rating():
    """Matching digests answers "are these the bytes we assessed", which alone is a dangerous
    half-answer: a skill can verify exactly and still be the one whose installer runs
    `rm -rf $HOME`. An agent calling verify_skill directly would otherwise never see it."""
    from site_index import verify_files, verify_path
    record = verify_files({"skills": [full_entry()]})[verify_path("o/r/s")]
    assert record["security"] == "warn"
    assert record["security_findings"] == ["x"]


def test_verification_records_are_not_display_material():
    """They live outside detail/ because they are not something the page renders."""
    from site_index import verify_path
    assert not verify_path("o/r/s").startswith("detail/")


def test_the_list_does_not_name_the_verification_record():
    """Unlike `detail`, which the browser follows and no test can hold to the rule here, the
    verify path is read only by Python that a test does pin. Publishing it would put a
    verification pointer inside the artifact marked as carrying no verification data."""
    assert "verify" not in list_skill(full_entry())


def test_the_list_holds_no_digests_so_nothing_can_verify_against_it():
    """The finder searches the list; only that. `display_only` is what says so."""
    row = list_skill(full_entry())
    for key in ("files", "content_sha256", "source_ref"):
        assert key not in row, key
    assert slim_index({"skills": [full_entry()], "repos": {}})["display_only"] is True


def test_the_client_derives_the_same_verification_path_as_the_builder():
    """The flattening rule exists twice: the finder skill installs on its own, with no
    access to processing/. Nothing but this test keeps the two from drifting apart."""
    sys.path.insert(0, str(ROOT / "skills" / "awesomeskills" / "scripts"))
    import index_client

    from site_index import verify_path
    for skill_id in ("o/r/s", "o/r", "a/b/.claude/skills/x", "a/b/c/d/e/f/g/h/i"):
        assert f"verify/{index_client.flat_name(skill_id)}" == verify_path(skill_id), skill_id


def test_every_published_verification_filename_is_unique():
    from site_index import verify_files
    paths = list(verify_files(_real_index()))
    assert len(paths) == len(set(paths))
