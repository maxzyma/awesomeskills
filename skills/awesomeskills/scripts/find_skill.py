#!/usr/bin/env python3
"""Thin client: query the awesomeskills static index for trustworthy skills.

No server. Reads the published catalogue (hosted URL or local repo copy), filters by the
requested capability, ranks by real-activity health, prints ranked candidates.

It reads the list rather than the full index -- a tenth the bytes, and everything a search
needs -- and returns a shape built for deciding rather than the raw index entry.

  python3 find_skill.py --query "pdf" [--zh-only] [--min-health 70] [--limit 5]

Skills whose static scan rated `fail` are withheld by default: the scan flags things like
`rm -rf` against $HOME, and a capability match is not a reason to put one in front of an
agent. `--security` widens or narrows that gate, and withheld candidates are always
reported as a count so the omission is visible rather than silent.

Index resolution: --index-url > $AWESOMESKILLS_INDEX_URL > hosted default, with the local
repo copy as an offline fallback.
Standard library only. Honors HTTPS_PROXY for remote URLs.
"""

from __future__ import annotations

import argparse
import json
import sys

from index_client import flat_name, load_catalog, resolve_index_url

# Ordered least to most dangerous. `unrated` means not yet assessed, which is not a pass.
SECURITY_ORDER = ["pass", "unrated", "warn", "fail"]
DEFAULT_MAX_SECURITY = "warn"
# The withheld list exists so a filtered result is never passed off as complete. That duty is
# discharged by the count; the ids are a convenience, and an uncapped one grows with the
# index -- `--security pass` already withholds 101 of 414, which is context spent on ids no
# agent acts on.
MAX_WITHHELD_IDS = 10


def purpose_in(entry: dict, language: str) -> str:
    side = ((entry.get("grounding") or {}).get("function") or {}).get(language) or {}
    return side.get("purpose") or ""


def match(entry: dict, query: str) -> bool:
    if not query:
        return True
    # Both languages, and our assessed purpose as well as the author's own summary: a skill
    # described only in Chinese was unfindable by the words its own description used.
    hay = " ".join((
        entry.get("id", ""), entry.get("name", ""), entry.get("summary", ""),
        entry.get("kind", ""), purpose_in(entry, "en"), purpose_in(entry, "zh"),
    )).lower()
    return all(term in hay for term in query.lower().split())


def present(entry: dict, repos: dict) -> dict:
    """What an agent needs to choose, and nothing it will not read.

    The whole index entry used to be returned. A single entry can carry a 19 KB digest
    manifest, so a default five-result search could put ~100k characters of file hashes into
    the caller's context -- data no agent reads while deciding, and that verify_skill fetches
    for itself anyway.
    """
    trust = entry.get("trust") or {}
    findings = [f.get("label") for f in (trust.get("security_findings") or []) if f.get("label")]
    grade = (repos.get(entry.get("source_repo")) or {}).get("overall_grade")
    english, chinese = purpose_in(entry, "en"), purpose_in(entry, "zh")
    assessed = english or chinese
    out = {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "source_repo": entry.get("source_repo"),
        "source_url": entry.get("source_url"),
        # Our assessment when we have one, the author's own words when we do not, and always
        # which of the two this is.
        "purpose": assessed or entry.get("summary"),
        "purpose_source": "assessed" if assessed else "upstream",
        "trust": {
            "health": trust.get("health"),
            "security": trust.get("security"),
            "zh": trust.get("zh"),
            "repo_grade": grade,
        },
        # The check to run before using it. Derived rather than published, because the
        # list is the artifact that carries no verification data by design.
        "verify": f"verify/{flat_name(entry.get('id', ''))}",
    }
    # Both languages when we have both: the search matches either, so a query in Chinese
    # could otherwise return a row whose Chinese assessment is never shown.
    if english and chinese:
        out["purpose_zh"] = chinese
    if findings:
        out["trust"]["security_findings"] = findings
    if entry.get("enrichment_status") == "legacy":
        out["assessment_caveat"] = (
            "assessed before content digests were recorded; the revision it was written "
            "from cannot be confirmed"
        )
    return out


def security_rank(entry: dict) -> int:
    rating = entry.get("trust", {}).get("security", "unrated")
    return SECURITY_ORDER.index(rating) if rating in SECURITY_ORDER else len(SECURITY_ORDER)


def passes_gate(entry: dict, max_security: str) -> bool:
    return security_rank(entry) <= SECURITY_ORDER.index(max_security)


def summarise_withheld(withheld: list[dict]) -> dict:
    """Report what the gate removed without listing all of it."""
    by_rating: dict[str, int] = {}
    for entry in withheld:
        rating = entry.get("trust", {}).get("security", "unrated")
        by_rating[rating] = by_rating.get(rating, 0) + 1
    out = {"count": len(withheld), "by_rating": by_rating}
    if withheld:
        out["ids"] = [e["id"] for e in withheld[:MAX_WITHHELD_IDS]]
        if len(withheld) > MAX_WITHHELD_IDS:
            out["ids_truncated"] = len(withheld) - MAX_WITHHELD_IDS
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="", help="capability keywords (all must match)")
    ap.add_argument("--index-url", default=None)
    ap.add_argument("--zh-only", action="store_true")
    ap.add_argument("--min-health", type=int, default=0)
    ap.add_argument(
        "--security", choices=SECURITY_ORDER, default=DEFAULT_MAX_SECURITY,
        help=f"worst security rating to return (default: {DEFAULT_MAX_SECURITY}; "
             "'fail' returns everything, including skills flagged as destructive)",
    )
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    url = resolve_index_url(args.index_url)
    try:
        index, used = load_catalog(url)
    except Exception as e:  # noqa: BLE001 — surface any resolution/parse error to the agent
        print(json.dumps({"error": f"failed to load index from {url}: {e}"}), file=sys.stderr)
        return 1

    relevant = [
        e for e in index.get("skills", [])
        if match(e, args.query)
        and e.get("trust", {}).get("health", 0) >= args.min_health
        and (not args.zh_only or e.get("trust", {}).get("zh"))
    ]
    candidates = [e for e in relevant if passes_gate(e, args.security)]
    withheld = [e for e in relevant if not passes_gate(e, args.security)]
    candidates.sort(key=lambda e: e.get("trust", {}).get("health", 0), reverse=True)

    out = {
        "index_source": used,
        "index_generated_at": index.get("generated_at"),
        "query": args.query,
        "security_gate": args.security,
        "count": len(candidates),
        "withheld_by_security_gate": summarise_withheld(withheld),
        "candidates": [present(e, index.get("repos") or {}) for e in candidates[: args.limit]],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
