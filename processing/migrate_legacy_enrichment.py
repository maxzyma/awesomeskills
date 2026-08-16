#!/usr/bin/env python3
"""One-time migration from an embedded v0.2 index to the detached enrichment cache."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from enrichment_store import CACHE, read_json, write_json


def migrate(old: dict) -> dict:
    cache = {
        "schema_version": "0.1",
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entries": {},
        "repos": {},
    }
    for entry in old.get("skills", []):
        grounding = entry.get("grounding")
        function = grounding.get("function", {}) if isinstance(grounding, dict) else {}
        if not grounding or not isinstance(function, dict) or function.get("error"):
            continue
        cache["entries"][entry["id"]] = {
            "content_sha256": None,
            "status": "legacy",
            "generated_at": old.get("generated_at"),
            "agent": "legacy-enrich-grounding",
            "model": grounding.get("model", "unknown"),
            "grounding": grounding,
        }
    for repo_id, repo in old.get("repos", {}).items():
        optional = {}
        community = repo.get("community")
        if isinstance(community, dict) and not community.get("error"):
            optional["community"] = community
        if isinstance(repo.get("external"), dict):
            optional["external"] = repo["external"]
        if optional:
            optional.update({"generated_at": old.get("generated_at"), "agent": "legacy-enrichment", "model": "legacy"})
            cache["repos"][repo_id] = optional
    return cache


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("legacy_index", type=Path)
    parser.add_argument("--out", type=Path, default=CACHE)
    args = parser.parse_args()
    cache = migrate(read_json(args.legacy_index))
    write_json(args.out, cache)
    print(f"migrated {len(cache['entries'])} entry enrichments and {len(cache['repos'])} repo enrichments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
