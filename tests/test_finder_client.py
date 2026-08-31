"""Tests for the finder skill's client scripts: the security gate and digest verification.

These cover the two guarantees the skill makes to a calling agent -- that a skill flagged
as destructive is not handed over as a candidate, and that "verified" means digests were
actually compared rather than assumed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "awesomeskills" / "scripts"))

import index_client  # noqa: E402
import verify_skill  # noqa: E402
from find_skill import (  # noqa: E402
    DEFAULT_MAX_SECURITY,
    match,
    passes_gate,
    security_rank,
)


def entry(skill_id="o/r/s", security="pass", **extra) -> dict:
    return {"id": skill_id, "name": "s", "summary": "", "kind": "skill",
            "trust": {"security": security, "health": 80}, **extra}


# --- security gate ---------------------------------------------------------------------

def test_fail_rated_skill_is_withheld_by_default():
    """A scan rating of `fail` flags things like rm -rf on $HOME. Matching the query is not
    a reason to surface one."""
    assert not passes_gate(entry(security="fail"), DEFAULT_MAX_SECURITY)


def test_pass_and_warn_survive_the_default_gate():
    assert passes_gate(entry(security="pass"), DEFAULT_MAX_SECURITY)
    assert passes_gate(entry(security="warn"), DEFAULT_MAX_SECURITY)


def test_unrated_is_ranked_riskier_than_pass():
    """`unrated` means not yet assessed; it must never sort as safe as an actual pass."""
    assert security_rank(entry(security="unrated")) > security_rank(entry(security="pass"))


def test_gate_can_be_widened_explicitly():
    assert passes_gate(entry(security="fail"), "fail")


def test_unknown_rating_is_treated_as_worst_case():
    assert not passes_gate(entry(security="something-new"), DEFAULT_MAX_SECURITY)


def test_query_requires_every_term():
    assert match(entry(skill_id="o/r/pdf-tools"), "pdf tools")
    assert not match(entry(skill_id="o/r/pdf-tools"), "pdf browser")


# --- verification refuses rather than passing -------------------------------------------

def pinned(files, complete=True, ref="a" * 40, kind="commit") -> dict:
    return {"id": "o/r/s", "source_repo": "o/r", "source_ref": ref,
            "source_ref_kind": kind, "files": files, "files_complete": complete}


def test_branch_pinned_entry_is_refused():
    """A digest taken from a moving branch cannot be re-checked later."""
    result = verify_skill.verify(pinned([{"path": "SKILL.md", "sha256": "x"}], ref="main", kind="branch"), False)
    assert result["verdict"] == verify_skill.REFUSED
    assert "moving ref" in result["reason"]


def test_entry_without_manifest_is_refused():
    result = verify_skill.verify(pinned([]), False)
    assert result["verdict"] == verify_skill.REFUSED
    assert result["checked"] == 0


def test_partial_manifest_is_refused_unless_explicitly_allowed():
    incomplete = pinned([{"path": "SKILL.md", "sha256": "x"}], complete=False)
    assert verify_skill.verify(incomplete, False)["verdict"] == verify_skill.REFUSED
    assert verify_skill.refusal_reason(incomplete, True) is None


def test_refusal_is_not_success():
    """Exit status must distinguish 'could not check' from 'checked and clean'."""
    assert verify_skill.REFUSED != verify_skill.VERIFIED


# --- verification actually compares digests ---------------------------------------------

@pytest.fixture
def fake_remote(monkeypatch):
    files: dict[str, str] = {}

    def fetch(url, timeout=30):
        for path, body in files.items():
            if url.endswith(path):
                return body.encode("utf-8")
        raise OSError(f"404 {url}")

    monkeypatch.setattr(verify_skill, "fetch_bytes", fetch)
    return files


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_matching_digests_verify(fake_remote):
    fake_remote["SKILL.md"] = "hello"
    fake_remote["scripts/run.py"] = "print(1)"
    result = verify_skill.verify(pinned([
        {"path": "SKILL.md", "sha256": digest("hello"), "role": "skill"},
        {"path": "scripts/run.py", "sha256": digest("print(1)"), "role": "executable"},
    ]), False)
    assert result["verdict"] == verify_skill.VERIFIED
    assert result["checked"] == 2


def test_tampered_executable_is_caught(fake_remote):
    """The executable file is the part that runs; verifying only SKILL.md would miss this."""
    fake_remote["SKILL.md"] = "hello"
    fake_remote["scripts/run.py"] = "import os; os.system('rm -rf ~')"
    result = verify_skill.verify(pinned([
        {"path": "SKILL.md", "sha256": digest("hello"), "role": "skill"},
        {"path": "scripts/run.py", "sha256": digest("print(1)"), "role": "executable"},
    ]), False)
    assert result["verdict"] == verify_skill.MISMATCH
    assert [p["path"] for p in result["problems"]] == ["scripts/run.py"]


def test_unfetchable_file_fails_rather_than_passes(fake_remote):
    fake_remote["SKILL.md"] = "hello"
    result = verify_skill.verify(pinned([
        {"path": "SKILL.md", "sha256": digest("hello")},
        {"path": "scripts/gone.py", "sha256": digest("x")},
    ]), False)
    assert result["verdict"] == verify_skill.MISMATCH
    assert result["problems"][0]["problem"] == "fetch failed"


# --- index resolution bugs ---------------------------------------------------------------

def test_local_fallback_is_reachable_when_the_network_fails(monkeypatch, tmp_path):
    """The old resolution chain put the local copy behind a non-empty constant, so `or`
    short-circuited and any network failure was fatal."""
    local = tmp_path / "index.json"
    local.write_text('{"skills": [], "generated_at": "x"}', encoding="utf-8")
    monkeypatch.setattr(index_client, "LOCAL_FALLBACK", local)
    monkeypatch.setattr(index_client, "fetch_text", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))

    payload, used = index_client.load_index("https://example.invalid/index.json")
    assert payload["skills"] == []
    assert "offline fallback" in used


def test_corrected_env_var_is_honoured(monkeypatch):
    monkeypatch.setenv(index_client.ENV_VAR, "https://example.test/a.json")
    assert index_client.resolve_index_url(None) == "https://example.test/a.json"


def test_legacy_misspelled_env_var_still_works(monkeypatch):
    monkeypatch.delenv(index_client.ENV_VAR, raising=False)
    monkeypatch.setenv(index_client.LEGACY_ENV_VAR, "https://example.test/legacy.json")
    assert index_client.resolve_index_url(None) == "https://example.test/legacy.json"


def test_cli_url_beats_environment(monkeypatch):
    monkeypatch.setenv(index_client.ENV_VAR, "https://example.test/env.json")
    assert index_client.resolve_index_url("./local.json") == "./local.json"
