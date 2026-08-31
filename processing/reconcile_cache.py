#!/usr/bin/env python3
"""Reconcile the enrichment cache against the current base index.

Two one-time-then-idempotent operations:

**Bind legacy entries.** Entries carried over from the pre-digest enrichment run have
`content_sha256: null` -- their text exists but nobody recorded which revision it was
written from. `detect_enrichment_changes.py` therefore queued all of them forever, and
regenerating them costs agent time to reproduce text we already have.

Binding records the digest observed *at binding time* in a separate field, `bound_sha256`.
It is deliberately not written to `content_sha256`, and the status stays `legacy`: we are
not claiming this text was assessed against the current content, only that the content has
not moved since we stopped re-queuing it. If upstream changes, the digest stops matching
and the entry re-enters the queue -- at which point re-assessment is warranted, because
now we actually know the content changed.

**Prune orphans.** Cache entries whose id no longer exists in the base index. Skill ids
change when a repo is restructured, so these accumulate and can never be served.

  python3 reconcile_cache.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from enrichment_store import BASE, CACHE, read_json

BIND_FIELD = "bound_sha256"


def current_skill_digests(base: dict) -> dict[str, str]:
    return {
        entry["id"]: entry.get("content_sha256")
        for entry in base.get("skills", [])
        if entry.get("level") == "skill"
    }


def reconcile(base: dict, cache: dict) -> tuple[dict, dict]:
    """Return the new cache and a report. The input cache is not mutated."""
    digests = current_skill_digests(base)
    entries = cache.get("entries", {})

    kept: dict[str, dict] = {}
    bound = rebound = orphaned = 0
    for skill_id, item in entries.items():
        if skill_id not in digests:
            orphaned += 1
            continue
        if item.get("status") != "legacy":
            kept[skill_id] = item
            continue
        digest = digests[skill_id]
        if not digest:
            kept[skill_id] = item
            continue
        if item.get(BIND_FIELD) == digest:
            kept[skill_id] = item
            continue
        rebound += 1 if item.get(BIND_FIELD) else 0
        bound += 0 if item.get(BIND_FIELD) else 1
        kept[skill_id] = {**item, BIND_FIELD: digest}

    report = {
        "entries_before": len(entries),
        "entries_after": len(kept),
        "legacy_bound": bound,
        "legacy_rebound": rebound,
        "orphans_pruned": orphaned,
    }
    return {**cache, "entries": kept}, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cache = read_json(args.cache)
    updated, report = reconcile(read_json(args.base), cache)
    print(json.dumps(report, indent=2))
    if args.dry_run:
        print("dry run; cache not written")
        return 0

    blob = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
    temporary = args.cache.with_suffix(args.cache.suffix + ".tmp")
    temporary.write_text(blob, encoding="utf-8")
    os.replace(temporary, args.cache)
    print(f"wrote {args.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
