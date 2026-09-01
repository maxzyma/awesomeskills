#!/usr/bin/env python3
"""Merge deterministic base data with optional enrichment and write public artifacts."""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path

from enrichment_store import BASE, CACHE, ROOT, read_json
from site_index import DETAIL_DIR, detail_files, slim_index

INDEX = ROOT / "registry" / "index.json"
SITE = ROOT / "site" / "public" / "index.json"
SITE_SLIM = ROOT / "site" / "public" / "site-index.json"
SITE_DETAIL_DIR = ROOT / "site" / "public" / DETAIL_DIR
LLM = ROOT / "site" / "public" / "llm.txt"


def merge(base: dict, cache: dict) -> dict:
    output = deepcopy(base)
    cached_entries = cache.get("entries", {})
    fresh = legacy = stale = pending = 0
    for entry in output.get("skills", []):
        if entry.get("level") != "skill":
            continue
        item = cached_entries.get(entry["id"])
        if not item:
            pending += 1
            entry["enrichment_status"] = "pending"
            continue
        digest = item.get("content_sha256")
        if item.get("status") == "fresh" and digest == entry.get("content_sha256"):
            fresh += 1
            entry["grounding"] = item.get("grounding")
            entry["enrichment_status"] = "fresh"
        elif item.get("status") == "legacy" and not digest:
            legacy += 1
            entry["grounding"] = item.get("grounding")
            entry["enrichment_status"] = "legacy"
        else:
            stale += 1
            entry["enrichment_status"] = "stale"

    cached_repos = cache.get("repos", {})
    for repo_id, repo in output.get("repos", {}).items():
        optional = cached_repos.get(repo_id, {})
        for key in ("community", "external"):
            if key in optional:
                repo[key] = optional[key]
    output["enrichment_coverage"] = {
        "fresh": fresh,
        "legacy": legacy,
        "stale": stale,
        "pending": pending,
        "total_skills": fresh + legacy + stale + pending,
    }
    return output


def llm_text(data: dict) -> str:
    lines = [
        "# awesomeskills — a trust-first index of public agent/Claude skills",
        f"# Generated {data['generated_at']}. Human + agent readable. Full data: /index.json",
        "# health/security/repo grade are deterministic; optional summaries are agent enrichment",
        "",
    ]
    for entry in data["skills"]:
        trust = entry["trust"]
        zh = "zh" if trust["zh"] else "en"
        flag = "" if not entry.get("frontmatter") else ("" if entry["frontmatter"]["valid"] else " [frontmatter:invalid]")
        lines.append(
            f"- {entry['name']} ({entry['id']}) — health {trust['health']}, "
            f"security {trust['security']}, {zh}{flag}"
        )
        if entry.get("summary"):
            lines.append(f"  {entry['summary']}")
        lines.append(f"  {entry['source_url']}")
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: dict) -> str:
    """Write atomically and return the blob, so the caller can report its size."""
    blob = json.dumps(payload, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(blob, encoding="utf-8")
    os.replace(temporary, path)
    return blob


def _write_detail_dir(directory: Path, files: dict[str, dict]) -> int:
    """Write every per-skill file and delete any that no longer belongs.

    Pruning is the point of writing the directory as a whole. A skill that leaves the index
    would otherwise keep serving its stale grounding forever, and the reproducibility check
    could not tell a correct tree from one carrying leftovers.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = set()
    for path, payload in files.items():
        target = directory.parent / path
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(payload, ensure_ascii=False) + "\n"
        # Skip untouched files so a rebuild does not restate 400 unchanged blobs to git.
        if not target.exists() or target.read_text(encoding="utf-8") != blob:
            target.write_text(blob, encoding="utf-8")
        written.add(target.resolve())
    # rglob, not iterdir: the tree has a subdirectory per namespace, and a top-level-only
    # sweep would leave every stale file in them untouched.
    for stale in directory.rglob("*"):
        if stale.is_file() and stale.resolve() not in written:
            stale.unlink()
    return len(written)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--site", type=Path, default=SITE)
    parser.add_argument("--site-slim", type=Path, default=SITE_SLIM)
    parser.add_argument("--site-detail-dir", type=Path, default=SITE_DETAIL_DIR)
    parser.add_argument("--llm", type=Path, default=LLM)
    args = parser.parse_args()
    data = merge(read_json(args.base), read_json(args.cache))
    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for path in (args.index, args.site):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(blob, encoding="utf-8")
        os.replace(temporary, path)
    # Display-only projection the browser fetches, split into the part that blocks the first
    # render and the part only an expanded row needs. The full artifact stays published at
    # index.json for agents and verify_skill.py.
    slim_blob = _write_json(args.site_slim, slim_index(data))
    detail_count = _write_detail_dir(args.site_detail_dir, detail_files(data))

    args.llm.parent.mkdir(parents=True, exist_ok=True)
    llm_temporary = args.llm.with_suffix(args.llm.suffix + ".tmp")
    llm_temporary.write_text(llm_text(data), encoding="utf-8")
    os.replace(llm_temporary, args.llm)
    coverage = data["enrichment_coverage"]
    print(f"merged {len(data['skills'])} entries; enrichment {coverage}")
    print(f"  site list: {len(slim_blob)/1024:.0f} KB (blocks first paint)"
          f" · {detail_count} deferred files"
          f" · full {len(blob)/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
