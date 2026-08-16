#!/usr/bin/env python3
"""Attach digest-verified SKILL.md evidence to a private enrichment batch manifest."""

from __future__ import annotations

import argparse
import hashlib
import os
from copy import deepcopy
from pathlib import Path

from build_index import fetch_raw
from enrichment_store import BASE, read_json, write_json


def materialize(base: dict, manifest: dict, token: str | None, fetcher=fetch_raw) -> dict:
    if manifest.get("base_generated_at") != base.get("generated_at"):
        raise ValueError("manifest does not belong to the current base index")
    current = {entry["id"]: entry for entry in base.get("skills", [])}
    output = deepcopy(manifest)
    for selected in output.get("entries", []):
        entry = current.get(selected.get("id"))
        if not entry or entry.get("level") != "skill":
            raise ValueError(f"unknown current skill: {selected.get('id')}")
        if selected.get("content_sha256") != entry.get("content_sha256"):
            raise ValueError(f"manifest digest mismatch: {entry['id']}")
        source_ref = entry.get("source_ref")
        path = entry.get("path")
        if not source_ref or not path:
            raise ValueError(f"missing source ref or path: {entry['id']}")
        content = fetcher(entry["source_repo"], source_ref, path, token)
        if content is None:
            raise ValueError(f"could not fetch evidence: {entry['id']}")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != entry["content_sha256"]:
            raise ValueError(f"source changed after base build: {entry['id']}")
        selected["evidence"] = {
            "media_type": "text/markdown",
            "source_url": entry["source_url"],
            "content_sha256": digest,
            "content": content,
        }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    destination = args.out or args.manifest
    try:
        result = materialize(
            read_json(args.base), read_json(args.manifest), os.environ.get("GITHUB_TOKEN")
        )
        write_json(destination, result)
    except (OSError, ValueError) as exc:
        print(f"could not materialize evidence: {exc}")
        return 1
    print(f"materialized {len(result.get('entries', []))} digest-verified evidence files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
