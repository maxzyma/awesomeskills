#!/usr/bin/env python3
"""Mine an aggregator registry for the upstream repositories behind it.

An aggregator cannot be a source. Its entries carry someone else's skill, so every trust
signal attaches to the wrong repository: health measures the aggregator's own commit rate,
`source_url` credits the aggregator rather than the author, and one sampled entry scored
health 88 while the author's repo had already 404'd. Indexing an aggregator therefore
produces confidently-rated entries about repositories nobody has assessed.

What an aggregator is good for is leads. `majiayu000/claude-skill-registry` publishes
161,277 records across 256 sharded JSON files, and each record names the upstream repo,
its author, its star count at crawl time, and a license classification. That is a candidate
list, not an index -- so this script produces candidates for review and nothing else.

    python3 processing/mine_sources.py --out registry/source-candidates.json

Three filters, each for a stated reason:

  * `distribution == "compatible"`. The aggregator marks 61% of its own records `restricted`
    with "verify upstream permission before reuse". Those are not candidates.
  * a star floor. Only about 0.8% of upstream repos clear even 500 stars, so without a floor
    the output is ninety thousand rows of noise.
  * not already a source. Mining should surface what is missing, not restate sources.toml.

The star counts and licenses are crawl-time snapshots and go stale -- the 404'd repo above
still carried its old star count. So this writes candidates, and build_index re-measures
everything live when one is actually adopted. Nothing here is a trust signal.

Standard library only. Honors HTTPS_PROXY.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from enrichment_store import ROOT

RAW = "https://raw.githubusercontent.com/majiayu000/claude-skill-registry/main"
MANIFEST = f"{RAW}/registry-manifest.json"
SOURCES = ROOT / "registry" / "sources.toml"
DEFAULT_OUT = ROOT / "registry" / "source-candidates.json"

# The aggregator's own license classification. Anything else it labels restricted or leaves
# unknown, and says so in a `permission_note` on the record.
USABLE_DISTRIBUTION = "compatible"
DEFAULT_MIN_STARS = 300


def _fetch(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "awesomeskills-mine"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_shard(path: str) -> list[dict]:
    """One shard's records. Prefers the gzipped copy: 80 KB against 350 KB."""
    try:
        payload = gzip.decompress(_fetch(f"{RAW}/{path}.gz"))
    except (urllib.error.HTTPError, OSError):
        payload = _fetch(f"{RAW}/{path}")
    return json.loads(payload).get("skills", [])


def known_sources(path: Path) -> set[str]:
    """Ids already in sources.toml, lowercased, so mining does not restate them."""
    try:
        import tomllib
        rows = tomllib.loads(path.read_text(encoding="utf-8")).get("source", [])
    except (ModuleNotFoundError, FileNotFoundError):
        return set()
    return {row["id"].lower() for row in rows if row.get("id")}


def candidates(records: list[dict], known: set[str], min_stars: int) -> list[dict]:
    """Upstream repos worth a human look, most-starred first.

    Grouped by repo rather than listed per skill: the unit of adoption is a repository, and
    how many skills it carries is itself a signal about whether it is worth adding.
    """
    by_repo: dict[str, dict] = {}
    for record in records:
        repo = record.get("repo")
        if not repo or repo.lower() in known:
            continue
        if record.get("distribution") != USABLE_DISTRIBUTION:
            continue
        stars = record.get("stars") or 0
        if stars < min_stars:
            continue
        row = by_repo.setdefault(repo, {
            "repo": repo,
            "author": record.get("author"),
            "stars_at_crawl": stars,
            "license_at_crawl": record.get("license"),
            "skill_count": 0,
            "skills": [],
            "categories": set(),
        })
        row["skill_count"] += 1
        if len(row["skills"]) < 10:
            row["skills"].append(record.get("name"))
        if record.get("category"):
            row["categories"].add(record["category"])
    ranked = sorted(by_repo.values(), key=lambda row: (-row["stars_at_crawl"], row["repo"]))
    for row in ranked:
        row["categories"] = sorted(row["categories"])
    return ranked


def _rejection_counts(records: list[dict], known: set[str], min_stars: int) -> dict:
    """Why records were dropped. A filter that cannot say what it removed is not reviewable."""
    counts = collections.Counter()
    for record in records:
        if not record.get("repo"):
            counts["no upstream repo named"] += 1
        elif record["repo"].lower() in known:
            counts["already a source"] += 1
        elif record.get("distribution") != USABLE_DISTRIBUTION:
            counts[f"distribution={record.get('distribution') or 'unknown'}"] += 1
        elif (record.get("stars") or 0) < min_stars:
            counts[f"under {min_stars} stars"] += 1
        else:
            counts["kept"] += 1
    return dict(counts.most_common())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-stars", type=int, default=DEFAULT_MIN_STARS)
    parser.add_argument(
        "--shards", type=int, default=0,
        help="only read the first N shards (a sample, for a quick look)",
    )
    args = parser.parse_args()

    try:
        manifest = json.loads(_fetch(MANIFEST))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        print(f"failed to fetch the registry manifest: {error}", file=sys.stderr)
        return 1

    shards = manifest.get("shards", [])
    if args.shards:
        shards = shards[: args.shards]
    print(f"reading {len(shards)} of {manifest.get('shard_count')} shards "
          f"({manifest.get('total_count')} records total)...", file=sys.stderr)

    records: list[dict] = []
    failed: list[str] = []
    for index, shard in enumerate(shards, 1):
        try:
            records.extend(fetch_shard(shard["path"]))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            failed.append(f"{shard['path']}: {error}")
        if index % 32 == 0:
            print(f"  {index}/{len(shards)} shards, {len(records)} records", file=sys.stderr)

    if failed:
        # Reported rather than swallowed: a partial read means a partial candidate list, and
        # the reader has to know the difference.
        print(f"  {len(failed)} shard(s) unreadable; candidate list is partial", file=sys.stderr)
        for line in failed[:5]:
            print(f"    {line}", file=sys.stderr)

    known = known_sources(SOURCES)
    ranked = candidates(records, known, args.min_stars)
    payload = {
        "generated_from": "majiayu000/claude-skill-registry",
        "records_read": len(records),
        "shards_read": len(shards) - len(failed),
        "shards_total": manifest.get("shard_count"),
        "complete": not failed,
        "min_stars": args.min_stars,
        "distribution_filter": USABLE_DISTRIBUTION,
        "note": (
            "Candidates for human review, not index entries. Star counts and licenses are "
            "the aggregator's crawl-time snapshots and go stale; build_index re-measures "
            "everything live when a candidate is adopted."
        ),
        "rejected": _rejection_counts(records, known, args.min_stars),
        "candidate_count": len(ranked),
        "candidates": ranked,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n{len(ranked)} candidate repo(s) from {len(records)} records -> {args.out}")
    for row in ranked[:15]:
        print(f"  {row['stars_at_crawl']:>7}★  {row['skill_count']:>3} skill(s)  "
              f"{row['repo']}  [{row['license_at_crawl']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
