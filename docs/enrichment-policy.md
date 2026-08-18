# Agent enrichment policy

awesomeskills separates reproducible trust assessment from optional agent-authored explanation.

## Authoritative, deterministic layer

`registry/base-index.json` is produced from public GitHub evidence by `processing/build_index.py`.
It owns repository health, frontmatter completeness, security findings, content digests, repository
grades, and every field used by default ranking. A model or agent cannot supply or override them.

The build is fail-closed: if any declared source, Git tree, or selected `SKILL.md` cannot be fetched,
the previous base index is preserved and the run fails.

## Optional enrichment layer

`registry/enrichment-cache.json` contains bilingual purpose, scenarios, inputs/outputs,
dependencies, boundaries, and evidence summaries. Each fresh skill result is bound to the SHA-256
of the exact `SKILL.md` content assessed. Changed content becomes stale until reassessed.

Agent output is untrusted until it passes `registry/enrichment.schema.json` and
`processing/validate_enrichment.py`. A scheduled batch must cover exactly the IDs and digests in
its pending manifest; partial or substituted output is rejected. Repository content is evidence, never an instruction: the
enricher must not execute scripts, install dependencies, follow embedded prompts, disclose local
data, or infer facts beyond the declared evidence scope.

Before the agent runs, `materialize_enrichment_evidence.py` fetches the selected public `SKILL.md`
files and verifies each byte sequence against the base-index digest. The private scheduler passes
that exact evidence bundle to the read-only agent, so enrichment does not depend on model-side web
access and cannot silently assess content that changed after the deterministic build.

Missing enrichment is neutral. It is shown as `pending`, `stale`, or `legacy`; it never lowers or
raises trust scores. When enrichment fails, the deterministic index remains publishable and the
last digest-matching successful cache entry is retained.

Validated batches stop at `review_ready`. The private review controller binds the public repository
HEAD, the complete generated diff hash, the exact changed IDs, and the allow-listed artifact paths
into a conversational receipt. Approval expires if HEAD or any byte of the diff changes. Only after
an explicit conversation decision may the controller run the public tests, commit and push the five
generated artifacts, wait for main validation, and update the parent submodule. Rejection records the
decision but does not destructively discard the local diff.

## Published index

`processing/merge_index.py` combines base data and matching cache entries into
`registry/index.json`, `site/public/index.json`, and `site/public/llm.txt`. Repo community and
external discussion fields may be attached as explanation, but deterministic repo grades always
win during the merge.

The private scheduler, credentials, model/provider choice, local paths, notifications, and Git
delivery policy are intentionally outside this public repository.
