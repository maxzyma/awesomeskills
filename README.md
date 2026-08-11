# awesomeskills

An AI-ready, agent-native discovery layer for public agent/Claude skills.

**Trust over coverage** — we don't index the most skills, we index the *most trustworthy* ones.
Every listed skill carries health, security, and language-coverage signals so an **agent** can
discover, evaluate, and pull a skill on its own — not just a human browsing a list.

- Public site: [awesomeskills.io](https://awesomeskills.io) (planned)
- Human skill browser + submission requests
- `/llm.txt` and an MCP server for agent-native discovery

> Status: **early / private**. See [`docs/product-definition.md`](docs/product-definition.md)
> for the product definition and MVP scope. This is a monorepo.

## Layout

| Dir | Role |
|-----|------|
| `registry/`   | SSoT: curated source list + schema + generated `index.json` |
| `processing/` | AI-ready assessment kernel (health / security / language grounding) |
| `mcp/`        | Agent-readable MCP server (query + pull) — the core distribution |
| `site/`       | awesomeskills.io site, human browser, submission; `site/public/llm.txt` is **generated** |
| `docs/`       | Product definition, semantics, MCP schema, MVP boundary |
