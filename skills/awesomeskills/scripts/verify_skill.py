#!/usr/bin/env python3
"""Verify a skill's files against the digests the index recorded, before anything is used.

  python3 verify_skill.py --id "owner/repo/skill-dir" [--index-url ...] [--json]

Fetches every file in the index entry's manifest at the pinned commit and compares SHA-256.
This is the check the finder skill promises; it is a real check, not advice.

It refuses rather than passes when it cannot actually prove anything:
  - the entry is pinned to a branch instead of a commit (the revision can have moved)
  - the entry carries no digest manifest (repo-level fallback entries have no skill bundle)
  - the manifest is known to be partial (files_complete is false)

A refusal is not a pass. Exit code is 0 only for "verified".

Standard library only. Honors HTTPS_PROXY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from index_client import fetch_bytes, load_verify_record, raw_file_url, resolve_index_url

VERIFIED, REFUSED, MISMATCH = "verified", "refused", "mismatch"
MAX_FETCH_WORKERS = 4  # raw.githubusercontent resets connections above this


def refusal_reason(entry: dict, allow_incomplete: bool) -> str | None:
    """Why this entry cannot be verified, or None if it can."""
    if entry.get("source_ref_kind") != "commit":
        return (
            f"entry is pinned to a {entry.get('source_ref_kind') or 'missing'} ref "
            f"({entry.get('source_ref')!r}); a moving ref cannot be verified against a digest"
        )
    if not entry.get("files"):
        return "entry carries no digest manifest (repo-level entry has no skill bundle)"
    if not entry.get("files_complete") and not allow_incomplete:
        return (
            "digest manifest is known to be partial (files_complete is false); "
            "re-run with --allow-incomplete to check only the files that were recorded"
        )
    return None


def check_one(repo: str, ref: str, row: dict) -> dict:
    """Fetch one recorded file at the pinned ref and compare its digest."""
    path, expected = row.get("path", ""), row.get("sha256", "")
    try:
        # Hash raw bytes: a bundle can contain binaries, and for text files the UTF-8
        # encoding the build hashed is the same byte sequence.
        actual = hashlib.sha256(fetch_bytes(raw_file_url(repo, ref, path))).hexdigest()
    except Exception as error:  # noqa: BLE001 — an unfetchable file is a failed check
        return {"path": path, "problem": "fetch failed", "detail": str(error)}
    if actual == expected:
        return {"path": path, "role": row.get("role")}
    return {"path": path, "problem": "digest mismatch", "expected": expected, "actual": actual}


def check_files(repo: str, ref: str, files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fetch every recorded file at the pinned ref. Returns (matched, problems).

    Fetched concurrently: a bundle can run past a hundred files, and checking them one
    round-trip at a time makes verification slow enough that it gets skipped.
    """
    workers = min(MAX_FETCH_WORKERS, max(1, len(files)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda row: check_one(repo, ref, row), files))
    return (
        [row for row in results if "problem" not in row],
        [row for row in results if "problem" in row],
    )


def verify(entry: dict, allow_incomplete: bool) -> dict:
    reason = refusal_reason(entry, allow_incomplete)
    if reason:
        return {"verdict": REFUSED, "reason": reason, "checked": 0}

    matched, problems = check_files(entry["source_repo"], entry["source_ref"], entry["files"])
    return {
        "verdict": VERIFIED if not problems else MISMATCH,
        "reason": None if not problems else f"{len(problems)} file(s) failed the check",
        "checked": len(matched) + len(problems),
        "matched": [row["path"] for row in matched],
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="skill id exactly as it appears in the index")
    parser.add_argument("--index-url", default=None)
    parser.add_argument(
        "--allow-incomplete", action="store_true",
        help="check the recorded files even when the manifest is known to be partial",
    )
    args = parser.parse_args()

    url = resolve_index_url(args.index_url)
    try:
        # One skill's manifest, not the whole index: the full artifact is ~366 KB gzipped
        # and a record is a few hundred bytes.
        entry, used = load_verify_record(url, args.id)
    except Exception as error:  # noqa: BLE001 — surface any resolution failure to the agent
        print(json.dumps({"error": f"failed to load index from {url}: {error}"}), file=sys.stderr)
        return 2

    if entry is None:
        print(json.dumps({"error": f"no index entry with id {args.id!r}"}), file=sys.stderr)
        return 2

    result = verify(entry, args.allow_incomplete)
    print(json.dumps({
        "id": args.id,
        "index_source": used,
        "source_repo": entry.get("source_repo"),
        "source_ref": entry.get("source_ref"),
        "source_ref_kind": entry.get("source_ref_kind"),
        "security": entry.get("security"),
        "security_findings": entry.get("security_findings") or [],
        **result,
    }, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == VERIFIED else 1


if __name__ == "__main__":
    raise SystemExit(main())
