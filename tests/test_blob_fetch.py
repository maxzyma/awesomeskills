"""Regression tests for silent content truncation and transient fetch failures.

Both defects here are silent by nature: they produce a plausible-looking digest or a
plausible-looking verification failure, with nothing in the output saying the content was
never fully read. They are pinned because neither is visible from the artifact alone.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "processing"))
sys.path.insert(0, str(ROOT / "skills" / "awesomeskills" / "scripts"))

import build_index  # noqa: E402
import index_client  # noqa: E402
import security_scan  # noqa: E402

TOKEN = "t"


def graphql_returning(blobs: list[dict]):
    def fake(query, variables, token, label):
        return {"repository": {f"file{i}": blob for i, blob in enumerate(blobs)}}
    return fake


def test_truncated_blob_is_refetched_in_full(monkeypatch):
    """GraphQL caps Blob.text around 512 KB and flags it with isTruncated. Digesting the
    prefix would record a hash of part of a file as if it were the whole file -- and the
    security scanner would only ever see that prefix."""
    monkeypatch.setattr(
        build_index, "post_graphql",
        graphql_returning([{"text": "FIRST-512KB-ONLY", "isTruncated": True}]),
    )
    monkeypatch.setattr(build_index, "fetch_raw", lambda *a, **k: "THE-WHOLE-FILE")

    got = build_index.fetch_skill_contents("o/r", "sha", ["big.js"], TOKEN)
    assert got == ["THE-WHOLE-FILE"]


def test_untruncated_blob_is_used_as_is(monkeypatch):
    monkeypatch.setattr(
        build_index, "post_graphql",
        graphql_returning([{"text": "small", "isTruncated": False}]),
    )
    monkeypatch.setattr(build_index, "fetch_raw", lambda *a, **k: pytest.fail("should not refetch"))

    assert build_index.fetch_skill_contents("o/r", "sha", ["small.py"], TOKEN) == ["small"]


def test_failed_refetch_of_truncated_blob_yields_none_not_a_prefix(monkeypatch):
    """If the full content cannot be had, the file counts as unfetched. Recording the
    prefix would be worse than recording nothing."""
    monkeypatch.setattr(
        build_index, "post_graphql",
        graphql_returning([{"text": "prefix", "isTruncated": True}]),
    )
    monkeypatch.setattr(build_index, "fetch_raw", lambda *a, **k: None)

    assert build_index.fetch_skill_contents("o/r", "sha", ["big.js"], TOKEN) == [None]


def test_transient_reset_is_retried(monkeypatch):
    """Concurrent fetches draw occasional connection resets. Without a retry those read as
    'this file does not match', which is a false alarm on a verification path."""
    calls = {"n": 0}

    class Response:
        def read(self):
            return b"ok"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection reset")
        return Response()

    monkeypatch.setattr(index_client.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(index_client.time, "sleep", lambda _: None)

    assert index_client.fetch_text("https://example.test/a", attempts=3) == "ok"
    assert calls["n"] == 3


def test_http_error_is_not_retried(monkeypatch):
    """A 404 is an answer, not a hiccup; retrying it just wastes time."""
    calls = {"n": 0}

    def not_found(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(index_client.urllib.request, "urlopen", not_found)
    with pytest.raises(urllib.error.HTTPError):
        index_client.fetch_text("https://example.test/missing")
    assert calls["n"] == 1


def test_retries_are_exhausted_and_then_raise(monkeypatch):
    monkeypatch.setattr(
        index_client.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    monkeypatch.setattr(index_client.time, "sleep", lambda _: None)
    with pytest.raises(urllib.error.URLError):
        index_client.fetch_text("https://example.test/a", attempts=2)


# --- binary bundle files ----------------------------------------------------------------

def test_binary_blob_is_digested_rather_than_counted_as_a_failure(monkeypatch):
    """GraphQL returns no text for a binary. Treating that as a failed fetch sank the whole
    entry, even though a vendored tarball is exactly the kind of thing worth digesting."""
    monkeypatch.setattr(
        build_index, "post_graphql",
        graphql_returning([{"text": None, "isTruncated": False, "isBinary": True}]),
    )
    monkeypatch.setattr(build_index, "fetch_blob_bytes", lambda *a, **k: b"\x00\x01binary")

    rows = build_index.fetch_bundle_files("o/r", "sha", ["scripts/vendor.tar.gz"], TOKEN)
    assert rows[0]["kind"] == "binary"
    assert rows[0]["text"] is None
    assert rows[0]["sha256"] == hashlib.sha256(b"\x00\x01binary").hexdigest()


def test_binary_whose_bytes_cannot_be_fetched_still_counts_as_failed(monkeypatch):
    monkeypatch.setattr(
        build_index, "post_graphql",
        graphql_returning([{"text": None, "isTruncated": False, "isBinary": True}]),
    )
    monkeypatch.setattr(build_index, "fetch_blob_bytes", lambda *a, **k: None)

    assert build_index.fetch_bundle_files("o/r", "sha", ["a.bin"], TOKEN)[0]["kind"] == "failed"


def test_missing_text_without_binary_flag_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        build_index, "post_graphql",
        graphql_returning([{"text": None, "isTruncated": False, "isBinary": False}]),
    )
    monkeypatch.setattr(build_index, "fetch_blob_bytes", lambda *a, **k: pytest.fail("not binary"))

    assert build_index.fetch_bundle_files("o/r", "sha", ["a.py"], TOKEN)[0]["kind"] == "failed"


def test_manifest_marks_binaries_as_unscanned_and_keeps_their_digest():
    bundle = [
        {"path": "scripts/run.py", "kind": "text", "text": "x", "sha256": "aa"},
        {"path": "scripts/v.tar.gz", "kind": "binary", "text": None, "sha256": "bb"},
        {"path": "scripts/gone.py", "kind": "failed", "text": None, "sha256": None},
    ]
    rows = build_index._file_manifest("SKILL.md", "body", bundle)
    roles = {row["path"]: row["role"] for row in rows}

    assert roles["scripts/v.tar.gz"] == "binary-unscanned"
    assert roles["scripts/run.py"] == "executable"
    assert "scripts/gone.py" not in roles, "a file with no digest must not enter the manifest"


def test_binary_presence_is_disclosed_and_blocks_a_pass():
    scan = security_scan.scan_skill_bundle(
        "harmless", {}, complete=False, binary_files=["scripts/v.tar.gz"],
    )
    labels = {finding["path"]: finding["label"] for finding in scan["findings"]}
    assert "scripts/v.tar.gz" in labels
    assert "not text-scanned" in labels["scripts/v.tar.gz"]
    assert scan["rating"] != "pass"
