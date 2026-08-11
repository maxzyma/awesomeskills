# awesomeskills

An AI-ready, agent-native discovery layer for public agent/Claude skills.

**Trust over coverage** — we don't index the most skills, we index the *most trustworthy* ones.
Every listed skill carries health, security, and language-coverage signals so an **agent** can
discover, evaluate, and pull a skill on its own — not just a human browsing a list.

- Live site: **[awesomeskills.io](https://awesomeskills.io)** — human browser
- Machine-readable: [`/index.json`](https://awesomeskills.io/index.json) · [`/llm.txt`](https://awesomeskills.io/llm.txt)

## Use with your agent

Let your agent discover & pull vetted skills on demand. Pick one:

**Claude Code · plugin (one-click)**
```
/plugin marketplace add maxzyma/awesomeskills
/plugin install awesomeskills@awesomeskills
```

**Claude Code · install the skill manually**
```
git clone https://github.com/maxzyma/awesomeskills
cp -r awesomeskills/skills/awesomeskills .claude/skills/
```
Then ask your agent for a capability it doesn't have; the `awesomeskills` skill queries the
index, ranks by trust, and returns vetted candidates with install guidance.

**Any LLM · just read the index**
Point it at `https://awesomeskills.io/llm.txt` (navigation) or `/index.json` (full data).

> awesomeskills is an **evaluator, not an executor** — it surfaces trust signals; sandboxing a
> pulled skill is the host agent's responsibility.

## Submit a skill

Know a good public skill we're missing?
**[Open a submission request](https://github.com/maxzyma/awesomeskills/issues/new?template=submit-skill.yml)** —
we review for real activity, security, and language coverage before indexing.

## Layout

| Dir | Role |
|-----|------|
| `registry/`   | SSoT: curated source list (`sources.toml`) + schema + generated `index.json` |
| `processing/` | AI-ready assessment kernel — builds the skill-level index (health / zh / frontmatter) |
| `skills/awesomeskills/` | the finder skill (thin client over the static index) |
| `ops/`        | Cloudflare DNS automation (`cf_dns.py`) — token-based, creds never in repo |
| `site/`       | awesomeskills.io site + generated `index.json`/`llm.txt` |
| `.claude-plugin/` | marketplace manifest for one-click Claude Code install |
| `docs/`       | product definition, MVP plan, assessments |

> Status: **early**. Distribution is a finder skill + static index (no server); MCP is a
> possible future. See [`docs/product-definition.md`](docs/product-definition.md).
