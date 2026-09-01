# awesomeskills

An AI-ready, agent-native discovery layer for public agent/Claude skills.

**Trust over coverage** — we don't index the most skills, we index the *most trustworthy* ones.
Every listed skill carries health, security, and language-coverage signals so an **agent** can
discover, evaluate, and pull a skill on its own — not just a human browsing a list.

- Live site: **[awesomeskills.io](https://awesomeskills.io)** — human browser
- Machine-readable: [`/index.json`](https://awesomeskills.io/index.json) · [`/llm.txt`](https://awesomeskills.io/llm.txt)

## Use with your agent

Let your agent discover & pull vetted skills on demand. Pick one:

**Claude Code · plugin (recommended)** — one-click, updatable
```
/plugin marketplace add maxzyma/awesomeskills
/plugin install awesomeskills@awesomeskills
```

**Claude Code · install manually**
```
git clone https://github.com/maxzyma/awesomeskills
# all projects (recommended for a finder):
cp -r awesomeskills/skills/awesomeskills ~/.claude/skills/
# or this project only:
cp -r awesomeskills/skills/awesomeskills .claude/skills/
```
`~/.claude/skills/` is personal (available in every project); `.claude/skills/` is scoped to
one project. Then ask your agent for a capability it doesn't have; the `awesomeskills` skill
queries the index, ranks by trust, and returns vetted candidates with install guidance.

**Any other agent** — it's a standard Agent Skill ([agentskills.io](https://agentskills.io)).
Install it the way your agent expects; most read `.agents/skills/` (project) or
`~/.agents/skills/` (global). See your agent's own skills docs for the exact location — we
don't maintain a per-agent directory table (it goes stale; the authoritative source is each agent).

**Any LLM** — point it at `https://awesomeskills.io/llm.txt` (navigation) or `/index.json` (full data).

> awesomeskills is an **evaluator, not an executor** — it surfaces trust signals; sandboxing a
> pulled skill is the host agent's responsibility.

## Submit a skill

Know a good public skill we're missing?
**[Open a submission request](https://github.com/maxzyma/awesomeskills/issues/new?template=submit-skill.yml)** —
we review for real activity, security, and language coverage before indexing.
The request is only a public repository pointer; all trust signals are recomputed by our pipeline.
Passing submissions receive an automated draft PR. The private conversational review gate binds a
maintainer decision to the exact PR head SHA before it records a GitHub review and attempts merge;
the submission workflow itself never auto-merges.
Archived repositories fail preflight; explicitly deprecated or oversized collections require a
maintainer-defined scope before a PR. Collection coverage, executable-file scan completeness, and
license discovery are published rather than implied.
Maintainer workflow: [`docs/submission-workflow.md`](docs/submission-workflow.md).

## Layout

Split by who writes a file: curated input, code, or generated output.

| Dir | Role |
|-----|------|
| `registry/`   | inputs and contracts — curated `sources.toml`, schemas, and the digest-bound caches the build reads back |
| `processing/` | AI-ready assessment kernel — builds the skill-level index (health / zh / frontmatter) |
| `skills/awesomeskills/` | the finder skill (thin client over the static index) |
| `ops/`        | Cloudflare DNS automation (`cf_dns.py`) — token-based, creds never in repo |
| `site/public/`| everything published to awesomeskills.io, generated: `index.json`, `site-index.json`, `detail/`, `verify/`, `llm.txt` |
| `.claude-plugin/` | marketplace manifest for one-click Claude Code install |
| `docs/`       | product definition, policy, and external reference material |

Generated files live in exactly one place. `index.json` was previously committed under both
`registry/` and `site/public/`, byte-identical, so every rebuild wrote the same 2 MB change
twice; the finder now reads the published endpoint instead of a path inside the repo.

> Status: **early**. Distribution is a finder skill + static index (no server); MCP is a
> possible future. See [`docs/product-definition.md`](docs/product-definition.md).

### Documents

| File | What it answers |
|------|-----------------|
| [`docs/product-definition.md`](docs/product-definition.md) | what the product is, and the open questions still to settle |
| [`docs/enrichment-policy.md`](docs/enrichment-policy.md) | what an agent may write, and what binds it to evidence |
| [`docs/submission-workflow.md`](docs/submission-workflow.md) | how a submitted skill reaches the index |
| [`docs/mvp-plan.md`](docs/mvp-plan.md) | the delivery checklist this was built against |
| [`docs/article-pivot-fit.md`](docs/article-pivot-fit.md) | assessment of reuse from the article-pivot kernel |
| [`docs/herdr-distribution-reference.md`](docs/herdr-distribution-reference.md) | **external reference input**, not a decision — someone else's distribution write-up, kept for comparison |

## Build artifacts

The publish path is deliberately split so model availability cannot block trust assessment:

```bash
GITHUB_TOKEN=... python3 processing/build_index.py
python3 processing/detect_enrichment_changes.py
python3 processing/merge_index.py
```

`base-index.json` and repo grades are deterministic. Optional agent summaries live in a
digest-bound cache and never affect ranking. See [`docs/enrichment-policy.md`](docs/enrichment-policy.md).

## License

[Apache-2.0](LICENSE).

The index describes public repositories; each indexed skill remains under its own
repository's license, and the entry links to the source so that license is one click away.
