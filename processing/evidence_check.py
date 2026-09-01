#!/usr/bin/env python3
"""Detect claims an enrichment makes that its evidence does not support.

The failure mode this catches, measured 2026-09-01 across all 249 enriched entries: a
source saying only "Automate Abuselpdb tasks via Rube MCP" -- misspelling included, with no
statement of what the service does -- was enriched as "Automate AbuseIPDB threat
intelligence queries and abuse reporting workflows". The model recognised the product
through the typo and filled in its function from training data.

Every such claim was true of the real product. That is what makes it dangerous: the text
reads as better informed than the source, so reading it back against the source is the only
way to notice. The enrichment policy already forbids it -- "must not infer facts beyond the
declared evidence scope" -- but nothing enforced it.

The detector is deliberately narrow. It only looks for *specific* terms -- proper nouns,
identifiers, versions -- that appear in the enrichment and nowhere in the evidence. Prose
embellishment that introduces no new name is invisible to it. A clean result therefore means
"no unsupported specifics found", never "verified".

Measured on the corpus that motivated it: 0 findings across 111 entries from the current
pipeline, 4 across 138 legacy entries.
"""

from __future__ import annotations

import re

# A term worth checking: long enough to be meaningful, and shaped like a name rather than a
# word -- an internal capital, a digit, a dot, or an underscore.
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{3,}")
_PREFIXES = ("non-", "un-", "multi-", "pre-", "re-", "sub-", "anti-")

# Words that are shaped like identifiers but are ordinary vocabulary in this domain, and so
# are expected to appear in an enrichment whether or not the source spells them out.
_COMMON = frozenset({
    "this", "that", "with", "from", "when", "which", "their", "using", "provides",
    "including", "skill", "skills", "user", "users", "files", "file", "data", "code",
    "tool", "tools", "the", "and", "for", "into", "such", "read-only", "read-write",
    "step-by-step", "end-to-end", "built-in", "command-line", "up-to-date", "well-formed",
})


def _strip_edges(term: str) -> str:
    """Drop trailing sentence punctuation swept up by the token pattern.

    Without this, "counts." and "summary." are read as dotted identifiers and reported
    against every source that spells them without the full stop -- 237 of 249 entries
    flagged on the first calibration run, essentially all of them this.
    """
    return term.strip(".-_")


def _dotted_identifier(term: str) -> bool:
    """A dot only makes a term specific when something follows it: run.py, not sentence."""
    return bool(re.search(r"\.[A-Za-z0-9]", term))


def _looks_specific(term: str) -> bool:
    return (
        any(character.isupper() for character in term[1:])
        or any(character.isdigit() for character in term)
        or _dotted_identifier(term)
        or "_" in term
    )


def _depluralise(term: str) -> str:
    """APIs -> API, URLs -> URL. An acronym's plural is the same term."""
    if len(term) > 2 and term.endswith("s") and term[:-1].isupper():
        return term[:-1]
    return term


def _variants(term: str) -> set[str]:
    """Spellings that should count as the same term.

    Without this the detector reports "AI-agent" against a source saying "AI agent", and
    "non-CLI" against one saying "CLI" -- three of seven raw findings in the first run were
    this kind of noise.
    """
    base = _depluralise(term).lower()
    for prefix in _PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return {
        base,
        base.replace("-", " "),
        base.replace("-", ""),
        base.replace(".", " "),
        base.replace("_", " "),
        base.replace("_", ""),
    }


def unsupported_terms(text: str, evidence: str) -> list[str]:
    """Specific terms in `text` that appear nowhere in `evidence`, in order of appearance."""
    if not text or not evidence:
        return []
    haystack = evidence.lower()
    found: list[str] = []
    for raw in _TERM.findall(text):
        term = _strip_edges(raw)
        if len(term) < 4 or term.lower() in _COMMON or not _looks_specific(term):
            continue
        if term in found or _supported(term, haystack):
            continue
        found.append(term)
    return found


def _supported(term: str, haystack: str) -> bool:
    """Whether the evidence backs this term, directly or through its parts.

    Compound coinages are assembled from the source's own words -- "Chinese-English",
    "ANSI-to-HTML", "MCP-compatible". Judging them whole reports the coinage rather than any
    claim; judging the parts asks the question that matters, which is whether the enrichment
    introduced a name the source never used.

    Snake_case reads as descriptive phrasing rather than a product name ("file_system",
    "http_client"), so its parts are treated the same way.
    """
    if any(variant in haystack for variant in _variants(term)):
        return True
    parts = [part for part in re.split(r"[-_]", term) if len(part) >= 3]
    if len(parts) < 2:
        return False
    return all(
        not _looks_specific(part) or any(v in haystack for v in _variants(part))
        for part in parts
    )


def _prose(side: dict) -> list[tuple[str, str]]:
    """The fields making factual claims about the skill, as (field, text) pairs.

    `boundary` is included: a limit stated in terms the evidence never uses is as much an
    invention as a capability, and the corpus shows boundaries naming real technologies the
    source never mentions.
    """
    pairs: list[tuple[str, str]] = []
    for field in ("purpose", "io", "dependencies", "boundary"):
        value = side.get(field)
        if isinstance(value, str):
            pairs.append((field, value))
        elif isinstance(value, list):
            pairs.extend((field, item) for item in value if isinstance(item, str))
    for index, item in enumerate(side.get("scenarios") or []):
        if isinstance(item, str):
            pairs.append((f"scenarios[{index}]", item))
    return pairs


def check_entry(function: dict, evidence: str) -> list[dict]:
    """Findings for one enrichment entry across both languages."""
    findings: list[dict] = []
    for language in ("en", "zh"):
        side = function.get(language) or {}
        for field, text in _prose(side):
            terms = unsupported_terms(text, evidence)
            if terms:
                findings.append({"lang": language, "field": field, "terms": terms, "text": text})
    return findings


def check_candidate(candidate: dict, manifest: dict) -> list[dict]:
    """Findings across a whole batch, keyed by skill id.

    The manifest carries the exact evidence bytes the enricher was given, so this needs no
    network and cannot check against content different from what the agent saw.
    """
    evidence_by_id = {
        entry.get("id"): (entry.get("evidence") or {}).get("content", "")
        for entry in manifest.get("entries", [])
    }
    results: list[dict] = []
    for entry in candidate.get("entries", []):
        evidence = evidence_by_id.get(entry.get("id"), "")
        findings = check_entry(entry.get("function") or {}, evidence)
        if findings:
            results.append({"id": entry.get("id"), "findings": findings})
    return results
