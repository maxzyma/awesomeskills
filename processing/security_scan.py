"""Static security scan of a SKILL.md (rule-based, no LLM).

MVP scans SKILL.md text only (frontmatter `allowed-tools` + body). `scripts/` files are
NOT fetched at index time, so this is a first-pass signal, not a full audit — the scope
is recorded in the result. Ratings: pass (no hits) / warn (low-risk) / fail (high-risk).
"""

from __future__ import annotations

import re

# (regex, human label) — high-severity ⇒ fail
HIGH = [
    (r"\brm\s+-rf\s+[~/]", "destructive rm -rf on home/root"),
    (r"curl[^\n|]*\|\s*(sudo\s+)?(ba)?sh", "curl | sh (remote code execution)"),
    (r"wget[^\n|]*\|\s*(ba)?sh", "wget | sh (remote code execution)"),
    (r"base64\s+-d[^\n]*\|\s*(ba)?sh", "base64 decode piped to shell"),
    (r"(cat|cp|scp)\s+[^\n]*(\.ssh/|id_rsa|id_ed25519)", "reads private SSH keys"),
    (r"(printenv|env)\s*\|[^\n]*curl", "pipes environment to curl (exfiltration)"),
    (r"curl[^\n]*\$\{?[A-Z_]*(TOKEN|KEY|SECRET|PASSWORD)", "sends secrets in a request"),
]

# (regex, human label) — low-severity ⇒ warn
LOW = [
    (r"\bsudo\b", "uses sudo"),
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)", "prompt-injection-like phrasing"),
    (r"disregard[^\n]*instructions", "prompt-injection-like phrasing"),
    (r"\bpip\s+install\b|\bnpm\s+i(nstall)?\b|\bcurl\b|\bwget\b", "network / install commands"),
]

_ALLOWED_TOOLS = re.compile(r"^allowed-tools:\s*(.+)$", re.M | re.I)


def scan_skill_md(text: str) -> dict:
    """Return {rating, findings:[{sev,label}], scope}. `pass`/`warn`/`fail`."""
    findings: list[dict] = []
    high_hits = 0
    for pat, label in HIGH:
        if re.search(pat, text, re.I):
            findings.append({"sev": "high", "label": label})
            high_hits += 1
    for pat, label in LOW:
        if re.search(pat, text, re.I):
            findings.append({"sev": "low", "label": label})

    m = _ALLOWED_TOOLS.search(text)
    if m and re.search(r"\bBash\b|\*|\ball\b", m.group(1), re.I):
        findings.append({"sev": "low", "label": "broad allowed-tools: " + m.group(1).strip()[:60]})

    rating = "fail" if high_hits else ("warn" if findings else "pass")
    return {"rating": rating, "findings": findings, "scope": "SKILL.md only (scripts/ not scanned)"}
