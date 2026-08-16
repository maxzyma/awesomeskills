#!/usr/bin/env python3
"""Validation and storage helpers for optional, agent-produced enrichment.

Trust scores never come from this store. The deterministic base index remains authoritative.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "registry" / "base-index.json"
CACHE = ROOT / "registry" / "enrichment-cache.json"


class EnrichmentError(ValueError):
    pass


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(blob, encoding="utf-8")
    os.replace(temporary, path)


def _nonempty_string(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EnrichmentError(f"{field} must be a non-empty string")


def _validate_function_side(value: dict, field: str) -> None:
    if not isinstance(value, dict):
        raise EnrichmentError(f"{field} must be an object")
    allowed = {"purpose", "scenarios", "io", "dependencies", "boundary"}
    if set(value) != allowed:
        raise EnrichmentError(f"{field} fields must be exactly {sorted(allowed)}")
    for name in ("purpose", "io", "boundary"):
        _nonempty_string(value.get(name), f"{field}.{name}")
    for name in ("scenarios", "dependencies"):
        items = value.get(name)
        if not isinstance(items, list) or (name == "scenarios" and not items):
            raise EnrichmentError(f"{field}.{name} must be a list" + (" with at least one item" if name == "scenarios" else ""))
        for index, item in enumerate(items):
            _nonempty_string(item, f"{field}.{name}[{index}]")


def validate_candidate(candidate: dict, base: dict) -> None:
    if not isinstance(candidate, dict):
        raise EnrichmentError("candidate must be an object")
    allowed = {"schema_version", "generated_at", "agent", "model", "entries", "repos"}
    unknown = set(candidate) - allowed
    if unknown:
        raise EnrichmentError(f"unknown top-level fields: {sorted(unknown)}")
    if candidate.get("schema_version") != "0.1":
        raise EnrichmentError("schema_version must be 0.1")
    for field in ("generated_at", "agent", "model"):
        _nonempty_string(candidate.get(field), field)

    base_entries = {entry["id"]: entry for entry in base.get("skills", [])}
    seen: set[str] = set()
    entries = candidate.get("entries")
    if not isinstance(entries, list):
        raise EnrichmentError("entries must be an array")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise EnrichmentError(f"entries[{index}] must be an object")
        expected = {"id", "content_sha256", "function", "scope"}
        if set(entry) != expected:
            raise EnrichmentError(f"entries[{index}] fields must be exactly {sorted(expected)}")
        entry_id = entry.get("id")
        if entry_id in seen:
            raise EnrichmentError(f"duplicate entry id: {entry_id}")
        seen.add(entry_id)
        if entry_id not in base_entries or base_entries[entry_id].get("level") != "skill":
            raise EnrichmentError(f"entry is not a current skill: {entry_id}")
        digest = entry.get("content_sha256")
        if digest != base_entries[entry_id].get("content_sha256"):
            raise EnrichmentError(f"content digest mismatch for {entry_id}")
        _nonempty_string(entry.get("scope"), f"entries[{index}].scope")
        function = entry.get("function")
        if not isinstance(function, dict) or set(function) != {"en", "zh"}:
            raise EnrichmentError(f"entries[{index}].function must contain exactly en and zh")
        _validate_function_side(function["en"], f"entries[{index}].function.en")
        _validate_function_side(function["zh"], f"entries[{index}].function.zh")

    repos = candidate.get("repos", [])
    if not isinstance(repos, list):
        raise EnrichmentError("repos must be an array")
    base_repos = set(base.get("repos", {}))
    repo_seen: set[str] = set()
    for index, repo in enumerate(repos):
        if not isinstance(repo, dict) or set(repo) != {"id", "community"}:
            raise EnrichmentError(f"repos[{index}] must contain exactly id and community")
        repo_id = repo.get("id")
        if repo_id in repo_seen or repo_id not in base_repos:
            raise EnrichmentError(f"unknown or duplicate repo id: {repo_id}")
        repo_seen.add(repo_id)
        if not isinstance(repo.get("community"), dict):
            raise EnrichmentError(f"repos[{index}].community must be an object")


def validate_manifest_binding(candidate: dict, manifest: dict) -> None:
    """Require one exact enrichment result for every selected manifest entry."""
    expected = {
        (entry.get("id"), entry.get("content_sha256"))
        for entry in manifest.get("entries", [])
    }
    actual = {
        (entry.get("id"), entry.get("content_sha256"))
        for entry in candidate.get("entries", [])
    }
    if not expected:
        raise EnrichmentError("manifest has no selected entries")
    if actual != expected or len(candidate.get("entries", [])) != len(expected):
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise EnrichmentError(
            f"candidate does not exactly cover manifest; missing={missing}, unexpected={unexpected}"
        )


def apply_candidate(cache: dict, candidate: dict) -> dict:
    updated = deepcopy(cache)
    updated.setdefault("entries", {})
    updated.setdefault("repos", {})
    for entry in candidate["entries"]:
        updated["entries"][entry["id"]] = {
            "content_sha256": entry["content_sha256"],
            "status": "fresh",
            "generated_at": candidate["generated_at"],
            "agent": candidate["agent"],
            "model": candidate["model"],
            "grounding": {
                "function": entry["function"],
                "scope": entry["scope"],
                "model": candidate["model"],
                "community_repo": entry["id"].split("/", 2)[0] + "/" + entry["id"].split("/", 2)[1],
            },
        }
    for repo in candidate.get("repos", []):
        previous = updated["repos"].get(repo["id"], {})
        previous.update({
            "community": repo["community"],
            "generated_at": candidate["generated_at"],
            "agent": candidate["agent"],
            "model": candidate["model"],
        })
        updated["repos"][repo["id"]] = previous
    updated["schema_version"] = "0.1"
    updated["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return updated
