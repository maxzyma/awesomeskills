#!/usr/bin/env python3
"""Validate an agent enrichment batch and optionally apply it atomically to the cache."""

from __future__ import annotations

import argparse
from pathlib import Path

from enrichment_store import (
    BASE, CACHE, EnrichmentError, apply_candidate, read_json,
    validate_manifest_binding, write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        base = read_json(args.base)
        candidate = read_json(args.candidate)
        from enrichment_store import validate_candidate
        validate_candidate(candidate, base)
        if args.manifest:
            validate_manifest_binding(candidate, read_json(args.manifest))
        if args.apply:
            cache = read_json(args.cache)
            updated = apply_candidate(cache, candidate)
            write_json(args.cache, updated)
    except (OSError, ValueError, EnrichmentError) as exc:
        print(f"invalid enrichment: {exc}")
        return 1
    print(f"valid enrichment: {len(candidate['entries'])} entries" + ("; cache updated" if args.apply else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
