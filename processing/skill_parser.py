"""Parse a SKILL.md into skill-level metadata + validation.

Zero dependencies — own minimal frontmatter parser (per docs/article-pivot-fit.md,
we build the skill semantic layer ourselves rather than reusing article-pivot, which
has no Markdown/frontmatter entry). Handles the common `--- key: value --- body` shape.
"""

from __future__ import annotations

import re

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---\\nyaml\\n---\\nbody`. Returns (frontmatter dict, body).

    Minimal: single-line `key: value` pairs only (SKILL.md name/description are single-line).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line and not line.lstrip().startswith("#"):
            key, val = line.split(":", 1)
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm, "\n".join(lines[end + 1:])


def parse_skill_md(text: str) -> dict:
    """Parse + validate a SKILL.md. Validation targets the skill's own semantics
    (name kebab-case, description present and reasonable) — not article structure."""
    fm, body = split_frontmatter(text)
    name = fm.get("name") or ""
    desc = fm.get("description") or ""

    issues: list[str] = []
    if not name:
        issues.append("missing frontmatter name")
    elif not _KEBAB.match(name):
        issues.append(f"name not kebab-case: {name!r}")
    if not desc:
        issues.append("missing frontmatter description")
    elif len(desc) < 20:
        issues.append("description too short (<20 chars)")
    elif len(desc) > 1024:
        issues.append("description too long (>1024 chars)")

    headings = sum(1 for line in body.splitlines() if line.lstrip().startswith("#"))
    code_blocks = body.count("```") // 2

    return {
        "name": name,
        "description": desc,
        "frontmatter_valid": not issues,
        "issues": issues,
        "body_headings": headings,
        "body_code_blocks": code_blocks,
    }
