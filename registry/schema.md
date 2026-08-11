# registry schema (v0.1)

Two artifacts, one source of truth.

## Source of truth: `sources.toml`

Hand-curated seed list of public skill repos to index. Machine-readable, reviewed by humans.

```toml
schema_version = "0.1"

[[source]]
id     = "owner/repo"          # GitHub owner/repo, also the index id
kind   = "skill-collection"    # skill | skill-collection | awesome-list | plugin-marketplace | registry
note   = "why it's a seed"     # short human note
```

## Generated: `index.json`

Produced by `processing/build_index.py`. **Do not hand-edit.** See fields in
`docs/product-definition.md` §6. `trust.health` is a heuristic v0 (recency + maintenance,
deliberately *not* star-driven); `trust.security` starts at `unrated` and is filled in later;
`trust.zh` is detected from repo language/description/keywords.

## Generated: `../site/public/llm.txt`

Lightweight agent-facing site map. Generated alongside `index.json`. **Do not hand-edit.**
