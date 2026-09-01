---
name: awesomeskills
description: Find a trustworthy public skill when you need a capability you don't currently have installed. Queries the awesomeskills static index (health/security/Chinese-coverage signals) and returns vetted candidates with install guidance. Use when the user asks for a capability, tool, or workflow that no currently-loaded skill covers, explicitly asks to find/discover/search for a skill, or says "use awesomeskills".
---

# awesomeskills

A thin client over the **awesomeskills** static index. It does not host or execute anything —
it fetches a published catalogue of *vetted* public skills, filters by trust signals, and hands
back candidates plus install guidance. Execution/sandboxing is the host agent's responsibility
(this skill only surfaces trust signals).

## When to use

- The user wants a capability that no currently-loaded skill provides.
- The user says "find a skill for X", "is there a skill that…", "discover/search skills".

## How it works

1. Resolve the index URL:
   - `--index-url` if given, else
   - `AWESOMESKILLS_INDEX_URL` env var if set, else
   - the hosted default; the local repo copy is used only if that fetch fails.
2. Filter by the user's need; rank by `trust.health` (real activity, **not** stars).
3. Present top candidates with `health`, `security`, `zh`, `purpose`, and `source_url`.
   `purpose_source` says whose words the purpose is: `assessed` is ours, `upstream` is the
   author's own description, which we have not evaluated. Relay that distinction.
   `purpose_zh` is present when a Chinese assessment exists — prefer it when the user
   is writing in Chinese.
   An `assessment_caveat`, when present, must be relayed too.
4. Before the user acts on a candidate, run `verify_skill.py` on it (see §Verify).
5. On install: prefer the source repo's own documented method (Agent Skills are portable —
   usually `git clone` then copy the skill dir into `.claude/skills/`, or `claude plugin install`).
   Pull the **pinned `source_ref`** that `verify_skill.py` reports, not the branch tip, so the
   digests apply.

## Run

```bash
python3 scripts/find_skill.py --query "<capability the user needs>"
# optional: --zh-only, --min-health 70, --limit 5, --security pass
```

The script prints ranked candidates as JSON — the fields needed to choose, not whole index
entries. Relay them to the user with the trust signals visible, and only proceed to install
with the user's confirmation.

`withheld_by_security_gate` in the output lists candidates that matched the query but were
withheld. If it is non-empty, say so — do not present a filtered list as if it were complete.

## Verify

```bash
python3 scripts/verify_skill.py --id "<candidate id from find_skill.py>"
```

Fetches every file in the entry's digest manifest at the pinned commit and compares SHA-256.
Exit code is 0 only for `verified`.

- `refused` is **not** a pass. It means the check could not be performed — the entry is
  pinned to a branch rather than a commit, has no manifest, or has a manifest known to be
  partial. Report the reason; do not describe the skill as verified.
- `mismatch` means the upstream files no longer match what was assessed. Treat the trust
  signals in the index as no longer applying to that content.
- `verified` answers only "these are the bytes we assessed". The output repeats `security`
  and `security_findings` for that reason: a skill can verify exactly and still be one the
  scan flagged. Check both before telling the user it is safe to install.

## Security (responsibility boundary)

- awesomeskills provides **trust signals**, not a sandbox. A `security` rating of `unrated`
  means *not yet assessed* — say so; do not imply it is safe.
- Skills rated `fail` by the static scan are withheld by default. `--security fail` returns
  them; the rating exists because the scan found things like `rm -rf` against `$HOME` in a
  skill's scripts. Only widen the gate on an explicit request, and say what the finding was.
- The manifest covers SKILL.md **and** the skill's executable files. Verifying only the
  prompt file would check the least dangerous part of the bundle.
- Third-party skills are code + prompts. Let the user review before enabling.
