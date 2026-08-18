from __future__ import annotations

import base64
import json
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "processing"))

from submission_action import (  # noqa: E402
    Assessment, GitHubAPI, assess_submission, ensure_draft_pull, parse_submission, report_body,
    report_build_result, source_block,
)


class SubmissionActionTest(unittest.TestCase):
    def test_build_report_preserves_observed_repository_shape(self):
        class FakeAPI:
            def __init__(self):
                self.comment = ""

            def get(self, path):
                if path == "repos/example/new-skills":
                    return {"visibility": "public", "private": False, "default_branch": "main"}
                if "/git/trees/main" in path:
                    return {"truncated": False, "tree": [
                        {"type": "blob", "path": "skills/a/SKILL.md"},
                    ]}
                raise AssertionError(path)

            def request(self, method, path, payload=None, allow_404=False):
                if method == "GET" and "/readme?" in path:
                    return None
                if method == "GET" and "/pulls?" in path:
                    return [{"html_url": "https://github.com/maxzyma/awesomeskills/pull/1"}]
                if method == "GET" and "/comments?" in path:
                    return []
                if method == "POST" and path.endswith("/comments"):
                    self.comment = payload["body"]
                    return {}
                raise AssertionError((method, path))

        event = {
            "issue": {"number": 42, "body": """### GitHub repo (owner/repo or URL)
example/new-skills

### Kind
skill-collection (many SKILL.md)
"""},
            "repository": {
                "full_name": "maxzyma/awesomeskills", "owner": {"login": "maxzyma"},
            },
        }
        api = FakeAPI()
        self.assertEqual(report_build_result(api, event, True), 0)
        self.assertIn("Default branch: `main`", api.comment)
        self.assertIn("Standard `SKILL.md`: 1", api.comment)

    def test_idempotent_github_write_retries_transient_failure(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode()

        unavailable = urllib.error.HTTPError(
            "https://api.github.com/example", 503, "unavailable", {},
            BytesIO(b'{"message":"unavailable"}'),
        )
        with (
            patch(
                "submission_action.urllib.request.urlopen",
                side_effect=[unavailable, Response()],
            ),
            patch("submission_action.time.sleep") as sleep,
        ):
            self.assertEqual(GitHubAPI("token").request("PUT", "/example", {}), {"ok": True})
        sleep.assert_called_once_with(2.0)

    def test_content_addressed_git_post_retries_transient_failure(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"sha":"blob-sha"}'

        unavailable = urllib.error.HTTPError(
            "https://api.github.com/example", 503, "unavailable", {},
            BytesIO(b'{"message":"unavailable"}'),
        )
        with (
            patch(
                "submission_action.urllib.request.urlopen",
                side_effect=[unavailable, Response()],
            ),
            patch("submission_action.time.sleep") as sleep,
        ):
            result = GitHubAPI("token").request("POST", "/repos/o/r/git/blobs", {})
        self.assertEqual(result, {"sha": "blob-sha"})
        sleep.assert_called_once_with(2.0)

    def test_issue_form_is_parsed_as_data_only(self):
        body = """### GitHub repo (owner/repo or URL)
https://github.com/example/skills.git

### Kind
skill-collection (many SKILL.md)

### Why is it worth evaluating?
$(touch /tmp/must-not-run) `rm -rf /`
"""
        self.assertEqual(parse_submission(body), ("example/skills", "skill-collection"))

    def test_invalid_kind_is_rejected(self):
        body = """### GitHub repo (owner/repo or URL)
example/skills
### Kind
skill-collection; run something
"""
        with self.assertRaises(ValueError):
            parse_submission(body)

    def test_assessment_passes_public_standard_skill(self):
        def get(path: str) -> dict:
            if path == "repos/example/skills":
                return {"visibility": "public", "private": False, "default_branch": "main"}
            return {"truncated": False, "tree": [{"type": "blob", "path": "x/SKILL.md"}]}

        result = assess_submission("example/skills", "skill-collection", get, set())
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.standard_skill_md, 1)

    def test_packaged_skill_requires_human_review(self):
        def get(path: str) -> dict:
            if path == "repos/example/skills":
                return {"visibility": "public", "private": False, "default_branch": "main"}
            return {"truncated": False, "tree": [{"type": "blob", "path": "x.skill"}]}

        result = assess_submission("example/skills", "skill-collection", get, set())
        self.assertEqual(result.status, "needs_review")
        self.assertIn("does not unpack", result.reasons[0])

    def test_duplicate_fails_without_source_block(self):
        def get(path: str) -> dict:
            if path == "repos/example/skills":
                return {"visibility": "public", "private": False, "default_branch": "main"}
            return {"truncated": False, "tree": []}

        result = assess_submission("example/skills", "registry", get, {"example/skills"})
        self.assertEqual(result.status, "fail")
        with self.assertRaises(ValueError):
            source_block(result, 12)

    def test_report_never_claims_issue_supplied_trust(self):
        assessment = Assessment(
            "pass", "example/skills", "skill", "main", 1, 0, False, False, False,
            ("passed",),
        )
        body = report_body(assessment, "https://github.com/example/index/pull/1")
        self.assertIn("pipeline computes it", body)
        self.assertIn("never auto-merges", body)

    def test_draft_pr_writes_only_normalized_pointer(self):
        class FakeAPI:
            def __init__(self):
                self.calls = []

            def request(self, method, path, payload=None, allow_404=False):
                self.calls.append((method, path, payload, allow_404))
                if "/git/ref/heads/main" in path:
                    return {"object": {"sha": "base-sha"}}
                if "/git/ref/heads/automation" in path:
                    return None
                if method == "GET" and "/git/commits/base-sha" in path:
                    return {"tree": {"sha": "base-tree"}}
                if method == "GET" and "/contents/registry/sources.toml" in path:
                    return {
                        "sha": "file-sha",
                        "content": base64.b64encode(b'schema_version = "0.1"\n').decode(),
                    }
                if method == "POST" and path.endswith("/git/blobs"):
                    return {"sha": "blob-sha"}
                if method == "POST" and path.endswith("/git/trees"):
                    return {"sha": "tree-sha"}
                if method == "POST" and path.endswith("/git/commits"):
                    return {"sha": "proposal-sha"}
                if method == "GET" and "/pulls?" in path:
                    return []
                if method == "POST" and path.endswith("/pulls"):
                    return {"html_url": "https://github.com/maxzyma/awesomeskills/pull/1"}
                return {}

        event = {
            "issue": {"number": 42},
            "repository": {
                "full_name": "maxzyma/awesomeskills", "default_branch": "main",
                "owner": {"login": "maxzyma"},
            },
        }
        assessment = Assessment(
            "pass", "example/skills", "skill-collection", "main", 2, 0, False,
            False, False, ("passed",),
        )
        api = FakeAPI()
        url = ensure_draft_pull(api, event, assessment)
        self.assertEqual(url, "https://github.com/maxzyma/awesomeskills/pull/1")
        blob = next(call for call in api.calls if call[0] == "POST" and call[1].endswith("/git/blobs"))
        written = blob[2]["content"]
        self.assertIn('id   = "example/skills"', written)
        self.assertIn('kind = "skill-collection"', written)
        self.assertNotIn("passed", written)

    def test_rerun_moves_owned_branch_once_and_reuses_open_pull(self):
        class FakeAPI:
            def __init__(self):
                self.calls = []

            def request(self, method, path, payload=None, allow_404=False):
                self.calls.append((method, path, payload, allow_404))
                if "/git/ref/heads/main" in path:
                    return {"object": {"sha": "new-base"}}
                if "/git/ref/heads/automation" in path:
                    return {"object": {"sha": "old-proposal"}}
                if method == "GET" and "/git/commits/new-base" in path:
                    return {"tree": {"sha": "base-tree"}}
                if method == "GET" and "/contents/registry/sources.toml" in path:
                    return {
                        "sha": "file-sha",
                        "content": base64.b64encode(b'schema_version = "0.1"\n').decode(),
                    }
                if method == "POST" and path.endswith("/git/blobs"):
                    return {"sha": "blob-sha"}
                if method == "POST" and path.endswith("/git/trees"):
                    return {"sha": "tree-sha"}
                if method == "POST" and path.endswith("/git/commits"):
                    return {"sha": "new-proposal"}
                if method == "GET" and "/pulls?" in path:
                    return [{"number": 7, "html_url": "https://github.com/maxzyma/awesomeskills/pull/7"}]
                return {}

        event = {
            "issue": {"number": 42},
            "repository": {
                "full_name": "maxzyma/awesomeskills", "default_branch": "main",
                "owner": {"login": "maxzyma"},
            },
        }
        assessment = Assessment(
            "pass", "example/skills", "skill-collection", "main", 2, 0, False,
            False, False, ("passed",),
        )
        api = FakeAPI()
        self.assertEqual(
            ensure_draft_pull(api, event, assessment),
            "https://github.com/maxzyma/awesomeskills/pull/7",
        )
        reset = next(
            call for call in api.calls
            if call[0] == "PATCH" and "/git/refs/heads/automation" in call[1]
        )
        self.assertEqual(reset[2], {"sha": "new-proposal", "force": True})
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/pulls") for call in api.calls))

    def test_archived_repository_fails_automatic_preflight(self):
        def get(path: str) -> dict:
            if path == "repos/example/skills":
                return {
                    "visibility": "public", "private": False, "default_branch": "main",
                    "archived": True,
                }
            return {"truncated": False, "tree": [{"type": "blob", "path": "SKILL.md"}]}

        result = assess_submission("example/skills", "skill", get, set())
        self.assertEqual(result.status, "fail")
        self.assertTrue(any("archived" in reason for reason in result.reasons))

    def test_deprecated_repository_requires_review(self):
        def get(path: str) -> dict:
            if path == "repos/example/skills":
                return {"visibility": "public", "private": False, "default_branch": "main"}
            return {"truncated": False, "tree": [{"type": "blob", "path": "SKILL.md"}]}

        result = assess_submission(
            "example/skills", "skill", get, set(),
            "This repository has been deprecated. Use the successor instead.",
        )
        self.assertEqual(result.status, "needs_review")
        self.assertTrue(result.deprecated)

    def test_large_collection_requires_explicit_scope(self):
        def get(path: str) -> dict:
            if path == "repos/example/skills":
                return {"visibility": "public", "private": False, "default_branch": "main"}
            return {
                "truncated": False,
                "tree": [
                    {"type": "blob", "path": f"skills/{index}/SKILL.md"}
                    for index in range(16)
                ],
            }

        result = assess_submission("example/skills", "skill-collection", get, set())
        self.assertEqual(result.status, "needs_review")
        self.assertIn("explicit deterministic scope", result.reasons[-1])


if __name__ == "__main__":
    unittest.main()
