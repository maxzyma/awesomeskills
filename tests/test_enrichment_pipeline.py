from __future__ import annotations

# ruff: noqa: E402

import sys
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "processing"))

from detect_enrichment_changes import build_manifest
from build_index import _get, fetch_raw, fetch_skill_paths
from enrichment_store import (
    EnrichmentError, apply_candidate, validate_candidate, validate_manifest_binding,
)
from merge_index import merge
from materialize_enrichment_evidence import materialize


def side(language: str) -> dict:
    return {
        "purpose": f"{language} purpose", "scenarios": [f"{language} scenario"],
        "io": f"{language} io", "dependencies": ["none"], "boundary": f"{language} boundary",
    }


class EnrichmentPipelineTest(unittest.TestCase):
    def setUp(self):
        self.base = {
            "schema_version": "0.3", "generated_at": "2026-08-16T00:00:00+00:00",
            "repos": {"o/r": {"overall_score": 80, "overall_grade": "B", "score_policy": "deterministic-v1"}},
            "skills": [{
                "id": "o/r/s", "name": "s", "summary": "summary", "source_repo": "o/r",
                "source_url": "https://github.com/o/r/tree/main/s", "path": "s/SKILL.md",
                "content_sha256": "a" * 64, "level": "skill", "kind": "skill",
                "trust": {"health": 80, "security": "pass", "zh": False},
                "frontmatter": {"valid": True, "issues": [], "headings": 1, "code_blocks": 0},
            }],
        }
        self.candidate = {
            "schema_version": "0.1", "generated_at": "2026-08-16T00:00:00+00:00",
            "agent": "test-agent", "model": "test-model",
            "entries": [{"id": "o/r/s", "content_sha256": "a" * 64,
                         "function": {"en": side("en"), "zh": side("zh")}, "scope": "SKILL.md only"}],
            "repos": [{"id": "o/r", "community": {"en": {"summary": "evidence"}}}],
        }

    def test_valid_candidate_becomes_fresh(self):
        validate_candidate(self.candidate, self.base)
        cache = apply_candidate({"entries": {}, "repos": {}}, self.candidate)
        output = merge(self.base, cache)
        self.assertEqual(output["skills"][0]["enrichment_status"], "fresh")
        self.assertEqual(output["repos"]["o/r"]["overall_score"], 80)
        self.assertIn("community", output["repos"]["o/r"])

    def test_digest_mismatch_is_rejected(self):
        self.candidate["entries"][0]["content_sha256"] = "b" * 64
        with self.assertRaises(EnrichmentError):
            validate_candidate(self.candidate, self.base)

    def test_candidate_must_cover_exact_manifest(self):
        manifest = build_manifest(self.base, {"entries": {}, "repos": {}})
        validate_manifest_binding(self.candidate, manifest)
        self.candidate["entries"] = []
        with self.assertRaises(EnrichmentError):
            validate_manifest_binding(self.candidate, manifest)

    def test_materialized_evidence_must_match_digest(self):
        content = "exact public skill content"
        import hashlib
        self.base["skills"][0].update({
            "source_ref": "main",
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        })
        manifest = build_manifest(self.base, {"entries": {}, "repos": {}})
        output = materialize(
            self.base, manifest, None,
            fetcher=lambda repo, ref, path, token: content,
        )
        self.assertEqual(output["entries"][0]["evidence"]["content"], content)
        with self.assertRaises(ValueError):
            materialize(
                self.base, manifest, None,
                fetcher=lambda repo, ref, path, token: "changed",
            )

    def test_truncated_tree_uses_deterministic_subtree_walk(self):
        def fake_get(url, token):
            if url.endswith("main?recursive=1"):
                return {"truncated": True, "tree": []}
            if url.endswith("git/trees/main"):
                return {"tree": [{"path": "skills", "type": "tree", "sha": "abc"}]}
            if url.endswith("git/trees/abc?recursive=1"):
                return {"truncated": False, "tree": [
                    {"path": "a/SKILL.md", "type": "blob", "sha": "y"},
                    {"path": "README.md", "type": "blob", "sha": "x"},
                ]}
            raise AssertionError(url)

        with patch("build_index._get", side_effect=fake_get):
            self.assertEqual(fetch_skill_paths("o/r", "main", None), ["skills/a/SKILL.md"])

    def test_transient_github_http_error_is_retried(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        transient = urllib.error.HTTPError(
            "https://api.github.com/example", 504, "gateway timeout", {}, None,
        )
        with (
            patch("build_index.urllib.request.urlopen", side_effect=[transient, Response()]),
            patch("build_index.time.sleep") as sleep,
        ):
            self.assertEqual(_get("https://api.github.com/example", None), {"ok": True})
            sleep.assert_called_once()

    def test_raw_skill_fetch_uses_authenticated_contents_api(self):
        with patch("build_index._get", return_value="content") as request:
            self.assertEqual(fetch_raw("o/r", "feature/x", "skills/a b/SKILL.md", "token"), "content")
        request.assert_called_once_with(
            "https://api.github.com/repos/o/r/contents/skills/a%20b/SKILL.md?ref=feature%2Fx",
            "token",
            accept="application/vnd.github.raw+json",
            raw=True,
        )

    def test_secondary_rate_limit_403_is_retried(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        limited = urllib.error.HTTPError(
            "https://api.github.com/example", 403, "forbidden", {},
            BytesIO(b'{"message":"You have exceeded a secondary rate limit."}'),
        )
        with (
            patch("build_index.urllib.request.urlopen", side_effect=[limited, Response()]),
            patch("build_index.time.sleep") as sleep,
        ):
            self.assertEqual(_get("https://api.github.com/example", "token"), {"ok": True})
            sleep.assert_called_once_with(15.0)

    def test_changed_digest_is_pending_and_old_result_is_stale(self):
        cache = apply_candidate({"entries": {}, "repos": {}}, self.candidate)
        self.base["skills"][0]["content_sha256"] = "b" * 64
        self.assertEqual(build_manifest(self.base, cache)["pending_count"], 1)
        output = merge(self.base, cache)
        self.assertEqual(output["skills"][0]["enrichment_status"], "stale")
        self.assertNotIn("grounding", output["skills"][0])

    def test_legacy_result_is_visible_but_pending_for_refresh(self):
        cache = {"entries": {"o/r/s": {"status": "legacy", "content_sha256": None,
                                         "grounding": {"function": {}}}}, "repos": {}}
        self.assertEqual(build_manifest(self.base, cache)["pending_count"], 1)
        self.assertEqual(merge(self.base, cache)["skills"][0]["enrichment_status"], "legacy")


if __name__ == "__main__":
    unittest.main()
