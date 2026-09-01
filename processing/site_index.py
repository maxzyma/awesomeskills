#!/usr/bin/env python3
"""Project the full index down to what the browser renders, split by when it is needed.

The full `index.json` is the machine-readable artifact: agents and verify_skill.py need the
pinned refs and the per-file digest manifest. A browser needs none of that, so a display-only
projection is published separately. Anything a verifier depends on is deliberately absent
from it, so the slim files can never be mistaken for a source of verification truth.

That projection is split again, because the two halves are needed at different moments.
Measured against the live site, the payload arrives at roughly 64 KB/s, so bytes convert
almost directly into seconds before the first row appears:

    one blocking file   269 KB gz   ~4.2s to first paint
    list                113 KB gz   ~1.8s to first paint
    detail              188 KB gz   fetched afterwards, blocking nothing

The list carries what every row paints and what search reads. The detail carries what only
the expanded grounding panel shows -- scenarios, boundaries, dependencies, health factor
counters, security findings. Most visitors expand nothing, and no visitor expands 414 rows.

The split is only sound while the union of the two is exactly the old single projection;
`tests/test_site_index.py` asserts that against the real index rather than a fixture.
"""

from __future__ import annotations

DETAIL_FILE = "site-detail.json"

# --- what a row paints, and what search reads --------------------------------------------
LIST_FIELDS = (
    "id", "name", "summary", "source_repo", "source_url", "kind", "level",
    # Kept because the page discloses it: a `legacy` assessment predates digest binding,
    # so the revision it was written from is unrecorded.
    "enrichment_status",
)
LIST_TRUST_FIELDS = ("health", "security", "zh")
# `purpose` alone, because the row shows it in place of the upstream summary and the search
# haystack spans both languages. The rest of the assessment is panel-only.
LIST_FUNCTION_FIELDS = ("purpose",)
# `valid` alone: the default sort gates broken entries to the bottom, so it has to be known
# before the first render. Its issue list is panel-only.
LIST_FRONTMATTER_FIELDS = ("valid",)
LIST_REPO_FIELDS = ("overall_grade", "overall_score")

# --- what only the expanded panel shows ---------------------------------------------------
DETAIL_TRUST_FIELDS = ("health_factors", "security_findings", "security_scope")
DETAIL_FUNCTION_FIELDS = ("io", "boundary", "dependencies", "scenarios")
DETAIL_FRONTMATTER_FIELDS = ("issues", "headings", "code_blocks")
DETAIL_REPO_FIELDS = ("community", "external")

# Every counter the grounding panel prints, plus the score breakdown.
HEALTH_FACTOR_FIELDS = (
    "recency_days", "stars", "open_issues", "forks", "watchers", "age_days", "archived",
    "maintainers", "issues_per_maintainer", "parts",
)

LANGUAGES = ("en", "zh")


def _pick(source: dict | None, fields) -> dict:
    source = source or {}
    return {key: source[key] for key in fields if key in source}


def _function_sides(entry: dict) -> dict:
    return ((entry.get("grounding") or {}).get("function")) or {}


def _split_function(entry: dict, fields) -> dict:
    """The per-language assessment, restricted to `fields`, dropping empty sides."""
    sides = {
        language: _pick(_function_sides(entry).get(language), fields)
        for language in LANGUAGES
    }
    kept = {language: side for language, side in sides.items() if side}
    return {"function": kept} if kept else {}


def list_skill(entry: dict) -> dict:
    row = _pick(entry, LIST_FIELDS)
    row["trust"] = _pick(entry.get("trust"), LIST_TRUST_FIELDS)
    grounding = _split_function(entry, LIST_FUNCTION_FIELDS)
    if grounding:
        row["grounding"] = grounding
    if entry.get("frontmatter"):
        frontmatter = _pick(entry["frontmatter"], LIST_FRONTMATTER_FIELDS)
        if frontmatter:
            row["frontmatter"] = frontmatter
    return row


def detail_skill(entry: dict) -> dict:
    trust = _pick(entry.get("trust"), DETAIL_TRUST_FIELDS)
    if "health_factors" in trust:
        trust["health_factors"] = _pick(trust["health_factors"], HEALTH_FACTOR_FIELDS)
    detail: dict = {}
    if trust:
        detail["trust"] = trust
    grounding = _split_function(entry, DETAIL_FUNCTION_FIELDS)
    if grounding:
        detail["grounding"] = grounding
    if entry.get("frontmatter"):
        frontmatter = _pick(entry["frontmatter"], DETAIL_FRONTMATTER_FIELDS)
        if frontmatter:
            detail["frontmatter"] = frontmatter
    return detail


def slim_index(data: dict) -> dict:
    """The blocking payload: everything needed to paint and search the list."""
    return {
        "generated_at": data.get("generated_at"),
        "display_only": True,
        "full_index": "index.json",
        "detail_index": DETAIL_FILE,
        "repos": {
            name: _pick(repo, LIST_REPO_FIELDS)
            for name, repo in (data.get("repos") or {}).items()
        },
        "skills": [list_skill(entry) for entry in data.get("skills", [])],
    }


def detail_index(data: dict) -> dict:
    """The deferred payload, keyed by id so the client can merge it without re-sorting."""
    skills = {}
    for entry in data.get("skills", []):
        detail = detail_skill(entry)
        if detail:
            skills[entry["id"]] = detail
    return {
        "generated_at": data.get("generated_at"),
        "display_only": True,
        "repos": {
            name: _pick(repo, DETAIL_REPO_FIELDS)
            for name, repo in (data.get("repos") or {}).items()
        },
        "skills": skills,
    }
