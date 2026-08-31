#!/usr/bin/env python3
"""Emit current skill entries whose content digest has no matching fresh enrichment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from enrichment_store import BASE, CACHE, read_json


def build_manifest(base: dict, cache: dict) -> dict:
    cached = cache.get("entries", {})
    pending = []
    for entry in base.get("skills", []):
        if entry.get("level") != "skill":
            continue
        item = cached.get(entry["id"], {})
        if item.get("status") == "fresh" and item.get("content_sha256") == entry.get("content_sha256"):
            continue
        # Legacy entries predate digest binding, so their provenance is unknown and their
        # status stays `legacy` -- but text we already hold is not worth an agent call.
        # reconcile_cache.py records the digest observed when we stopped re-queuing them;
        # while it still matches, the content has not moved and the entry waits.
        if item.get("status") == "legacy" and item.get("bound_sha256") == entry.get("content_sha256"):
            continue
        pending.append({
            key: entry.get(key)
            for key in ("id", "name", "summary", "source_repo", "source_url", "path", "content_sha256")
        })
    return {
        "schema_version": "0.1",
        "base_generated_at": base.get("generated_at"),
        "pending_count": len(pending),
        "entries": pending,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    manifest = build_manifest(read_json(args.base), read_json(args.cache))
    if args.limit:
        manifest["entries"] = manifest["entries"][:args.limit]
        manifest["selected_count"] = len(manifest["entries"])
    blob = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(blob, encoding="utf-8")
    else:
        print(blob, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
