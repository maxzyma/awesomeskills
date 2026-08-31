"""Deterministic static scan of a skill instruction and its executable text files."""

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
# Note: plain curl/pip/npm are NOT flagged — they're near-ubiquitous in skills and produced
# a high false-positive rate. Only genuinely suspicious low-severity patterns remain here.
LOW = [
    (r"\bsudo\b", "uses sudo"),
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)", "prompt-injection-like phrasing"),
    (r"disregard[^\n]*instructions", "prompt-injection-like phrasing"),
    (r"chmod\s+\+x[^\n]*&&[^\n]*\./", "downloads then executes a script"),
]

_ALLOWED_TOOLS = re.compile(r"^allowed-tools:\s*(.+)$", re.M | re.I)


def _scan_text(text: str, path: str) -> list[dict]:
    findings: list[dict] = []
    for pat, label in HIGH:
        if re.search(pat, text, re.I):
            findings.append({"sev": "high", "label": label, "path": path})
    for pat, label in LOW:
        if re.search(pat, text, re.I):
            findings.append({"sev": "low", "label": label, "path": path})

    return findings


def scan_skill_bundle(
    skill_text: str,
    executable_files: dict[str, str],
    complete: bool = True,
    binary_files: list[str] | None = None,
) -> dict:
    """Scan SKILL.md and fetched executable text; incomplete scans can never pass.

    `binary_files` are bundle files that ship with the skill but cannot be read as text --
    a vendored tarball, for instance. They are digested elsewhere, but nothing here has
    inspected them, so each is disclosed by path and the rating can never come out `pass`.
    """
    findings = _scan_text(skill_text, "SKILL.md")

    m = _ALLOWED_TOOLS.search(skill_text)
    if m and re.search(r"\bBash\b|\*|\ball\b", m.group(1), re.I):
        findings.append({
            "sev": "low", "label": "broad allowed-tools: " + m.group(1).strip()[:60],
            "path": "SKILL.md",
        })

    for path, text in sorted(executable_files.items()):
        findings.extend(_scan_text(text, path))

    for path in sorted(binary_files or []):
        findings.append({
            "sev": "low", "label": "binary bundle file, not text-scanned", "path": path,
        })

    if not complete:
        findings.append({
            "sev": "low", "label": "executable-file scan incomplete", "path": "skill bundle",
        })

    rating = "fail" if any(item["sev"] == "high" for item in findings) else ("warn" if findings else "pass")
    return {
        "rating": rating,
        "findings": findings,
        "scope": "SKILL.md + executable text files",
        "complete": complete,
        "executable_files_scanned": len(executable_files),
    }


def scan_skill_md(text: str) -> dict:
    """Backward-compatible instruction-only scan; it deliberately cannot return pass."""
    return scan_skill_bundle(text, {}, complete=False)
