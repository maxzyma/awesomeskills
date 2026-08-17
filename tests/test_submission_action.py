from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "processing"))

from submission_action import (  # noqa: E402
    Assessment, assess_submission, ensure_draft_pull, parse_submission, report_body, source_block,
)


class SubmissionActionTest(unittest.TestCase):
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
            "pass", "example/skills", "skill", "main", 1, 0, False, ("passed",),
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
                if method == "GET" and "/contents/registry/sources.toml" in path:
                    return {
                        "sha": "file-sha",
                        "content": base64.b64encode(b'schema_version = "0.1"\n').decode(),
                    }
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
            "pass", "example/skills", "skill-collection", "main", 2, 0, False, ("passed",),
        )
        api = FakeAPI()
        url = ensure_draft_pull(api, event, assessment)
        self.assertEqual(url, "https://github.com/maxzyma/awesomeskills/pull/1")
        put = next(call for call in api.calls if call[0] == "PUT")
        written = base64.b64decode(put[2]["content"]).decode()
        self.assertIn('id   = "example/skills"', written)
        self.assertIn('kind = "skill-collection"', written)
        self.assertNotIn("passed", written)


if __name__ == "__main__":
    unittest.main()
