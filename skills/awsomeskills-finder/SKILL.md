---
name: awsomeskills-finder
description: Find a trustworthy public skill when you need a capability you don't currently have installed. Queries the awsomeskills static index (health/security/Chinese-coverage signals) and returns vetted candidates with install guidance. Use when the user asks for a capability, tool, or workflow that no currently-loaded skill covers, or explicitly asks to find/discover/search for a skill.
---

# awsomeskills-finder

A thin client over the **awsomeskills** static index. It does not host or execute anything —
it fetches a static `index.json` of *vetted* public skills, filters by trust signals, and hands
back candidates plus install guidance. Execution/sandboxing is the host agent's responsibility
(this skill only surfaces trust signals).

## When to use

- The user wants a capability that no currently-loaded skill provides.
- The user says "find a skill for X", "is there a skill that…", "discover/search skills".

## How it works

1. Resolve the index URL:
   - `AWSOMESKILLS_INDEX_URL` env var if set, else
   - the hosted default (set once the index is published), else
   - the local repo copy `registry/index.json` (for local testing).
2. Filter by the user's need; rank by `trust.health` (real activity, **not** stars).
3. Present top candidates with `health`, `security`, `zh`, summary, and `source_url`.
4. On install: prefer the source repo's own documented method (Agent Skills are portable —
   usually `git clone` then copy the skill dir into `.claude/skills/`, or `claude plugin install`).
   **Verify file digests against the index before trusting a pulled skill** (see §Security).

## Run

```bash
python3 scripts/find_skill.py --query "<capability the user needs>"
# optional: --zh-only, --min-health 70, --limit 5
```

The script prints ranked candidates as JSON. Relay them to the user with the trust signals
visible, and only proceed to install with the user's confirmation.

## Security (responsibility boundary)

- awsomeskills provides **trust signals**, not a sandbox. A `security` rating of `unrated`
  means *not yet assessed* — say so; do not imply it is safe.
- Third-party skills are code + prompts. After pulling, check the fetched files' SHA-256
  against the index entry when digests are present, and let the user review before enabling.
