#!/usr/bin/env python3
"""Project the full index down to what the browser renders, split by when it is needed.

The full `index.json` is the machine-readable artifact: agents and verify_skill.py need the
pinned refs and the per-file digest manifest. A browser needs none of that, so a display-only
projection is published separately. Anything a verifier depends on is deliberately absent
from it, so these files can never be mistaken for a source of verification truth.

It is published as one list plus one small file per skill and per repo.

The list carries everything the page shows before anyone clicks: the row, the badges, every
badge tooltip, and the text search reads. Measured against the live site the payload arrives
at roughly 64 KB/s, so bytes convert almost directly into seconds before the first row
appears -- the single combined artifact was 269 KB gz and ~4.2s, the list is ~119 KB.

A skill's grounding prose -- scenarios, boundary, dependencies, io -- goes in its own file,
fetched only when that row is expanded; median 841 bytes gzipped, 408 files. A repo's
community assessment goes in a file of its own, shared by every row from that repo.

Per file rather than one deferred bundle, because of how the two invalidate. Enrichment
rewrites roughly twenty entries a day. A single bundle changes its ETag whenever any one of
them does, so every returning visitor re-downloads the whole thing daily; per skill, only the
rewritten twenty lose their cache and the other several hundred keep it. The bundle does
compress better as one document (147 KB against 271 KB summed), but that total is only paid
by someone who expands all 408 rows, and nobody does.
"""

from __future__ import annotations

DETAIL_DIR = "detail"
# Two namespaces, because a two-segment skill id flattens to `owner__repo` -- exactly what a
# repo file would be called. Seven entries have ids that short.
SKILL_DIR = f"{DETAIL_DIR}/skill"
REPO_DIR = f"{DETAIL_DIR}/repo"

# --- what the page shows before anyone clicks --------------------------------------------
LIST_FIELDS = (
    "id", "name", "summary", "source_repo", "source_url", "kind", "level",
    # Kept because the page discloses it: a `legacy` assessment predates digest binding,
    # so the revision it was written from is unrecorded.
    "enrichment_status",
)
# Every badge tooltips something, and a tooltip that waited on a per-skill fetch would need
# one request per visible row just to hover. So all of it rides in the list: the health
# breakdown costs 5.2 KB gzipped and the security findings 2.9 KB. The effect is that an
# unexpanded row never needs a second request at all.
LIST_TRUST_FIELDS = (
    "health", "security", "zh", "health_factors", "security_findings", "security_scope",
)
# `purpose` alone, because the row shows it in place of the upstream summary and the search
# haystack spans both languages. The rest of the assessment is panel-only.
LIST_FUNCTION_FIELDS = ("purpose",)
# `valid` alone: the default sort gates broken entries to the bottom, so it has to be known
# before the first render. Its issue list is panel-only.
LIST_FRONTMATTER_FIELDS = ("valid",)
LIST_REPO_FIELDS = ("overall_grade", "overall_score")
# The community write-up is per-repo and panel-only. Keeping it in the list would put 10 KB
# gzipped on a payload that carries a cache-buster and is therefore re-fetched every visit;
# copying it into each skill file would repeat one repo's assessment across all its skills.
# So it gets its own file, shared by every row from that repo and cached after one expand.
DETAIL_REPO_FIELDS = ("community", "external")

# Every counter the grounding panel prints, plus the score breakdown.
HEALTH_FACTOR_FIELDS = (
    "recency_days", "stars", "open_issues", "forks", "watchers", "age_days", "archived",
    "maintainers", "issues_per_maintainer", "parts",
)

# --- what only an expanded panel adds -----------------------------------------------------
DETAIL_FUNCTION_FIELDS = ("io", "boundary", "dependencies", "scenarios")
DETAIL_FRONTMATTER_FIELDS = ("issues", "headings", "code_blocks")

LANGUAGES = ("en", "zh")


def detail_path(skill_id: str) -> str:
    """Where a skill's grounding file lives, relative to the site root.

    Flattened rather than mirroring the id, whose segments include names like `.claude`.
    A directory tree of dot-prefixed folders is the kind of thing static hosts quietly drop,
    and the flat form is one directory of plainly named files instead. Ids are already
    restricted to [A-Za-z0-9._/-] and none contains `__`, so the mapping is injective; the
    longest name this produces is 148 characters.

    The list publishes this path per entry rather than leaving the browser to rebuild it,
    so the rule exists once instead of once per language.
    """
    return f"{SKILL_DIR}/{skill_id.replace('/', '__')}.json"


def repo_detail_path(repo_id: str) -> str:
    """Where a repo's community assessment lives, relative to the site root."""
    return f"{REPO_DIR}/{repo_id.replace('/', '__')}.json"


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


def detail_skill(entry: dict) -> dict:
    """The panel-only half. Empty when the entry has nothing an expansion would add."""
    detail: dict = {}
    grounding = _split_function(entry, DETAIL_FUNCTION_FIELDS)
    if grounding:
        detail["grounding"] = grounding
    if entry.get("frontmatter"):
        frontmatter = _pick(entry["frontmatter"], DETAIL_FRONTMATTER_FIELDS)
        if frontmatter:
            detail["frontmatter"] = frontmatter
    return detail


def list_skill(entry: dict) -> dict:
    row = _pick(entry, LIST_FIELDS)
    trust = _pick(entry.get("trust"), LIST_TRUST_FIELDS)
    if "health_factors" in trust:
        trust["health_factors"] = _pick(trust["health_factors"], HEALTH_FACTOR_FIELDS)
    row["trust"] = trust
    grounding = _split_function(entry, LIST_FUNCTION_FIELDS)
    if grounding:
        row["grounding"] = grounding
    if entry.get("frontmatter"):
        frontmatter = _pick(entry["frontmatter"], LIST_FRONTMATTER_FIELDS)
        if frontmatter:
            row["frontmatter"] = frontmatter
    # Named only when a file was actually written. Six entries have no assessment yet, and
    # pointing at their absent files would spend a round trip to learn nothing.
    if detail_skill(entry):
        row["detail"] = detail_path(entry["id"])
    return row


def _list_repo(repo_id: str, repo: dict) -> dict:
    row = _pick(repo, LIST_REPO_FIELDS)
    if _pick(repo, DETAIL_REPO_FIELDS):
        row["detail"] = repo_detail_path(repo_id)
    return row


def slim_index(data: dict) -> dict:
    """The blocking payload: everything shown before a row is expanded."""
    return {
        "generated_at": data.get("generated_at"),
        "display_only": True,
        "full_index": "index.json",
        "repos": {
            name: _list_repo(name, repo)
            for name, repo in (data.get("repos") or {}).items()
        },
        "skills": [list_skill(entry) for entry in data.get("skills", [])],
    }


def detail_files(data: dict) -> dict[str, dict]:
    """Every deferred file -- per skill and per repo -- keyed by path from the site root."""
    files = {}
    for entry in data.get("skills", []):
        detail = detail_skill(entry)
        if detail:
            files[detail_path(entry["id"])] = detail
    for repo_id, repo in (data.get("repos") or {}).items():
        community = _pick(repo, DETAIL_REPO_FIELDS)
        if community:
            files[repo_detail_path(repo_id)] = community
    return files
