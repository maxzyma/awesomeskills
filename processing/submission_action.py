#!/usr/bin/env python3
"""Process a structured submission issue and maintain its draft source-pointer PR.

Issue and repository content are untrusted data. This program never executes either one and only
writes a normalized owner/repo, an enumerated kind, and a fixed factual note to sources.toml.
"""

from __future__ import annotations

import base64
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from validate_submission import existing_ids, normalize_repo


MARKER = "<!-- awesomeskills-submission-bot -->"
ALLOWED_KINDS = {
    "skill (single SKILL.md)": "skill",
    "skill-collection (many SKILL.md)": "skill-collection",
    "awesome-list": "awesome-list",
    "plugin-marketplace": "plugin-marketplace",
    "registry": "registry",
}
REPO_LABEL = "GitHub repo (owner/repo or URL)"
KIND_LABEL = "Kind"


class ActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Assessment:
    status: str
    repo_id: str
    kind: str
    default_branch: str | None
    standard_skill_md: int
    packaged_dot_skill: int
    tree_truncated: bool
    reasons: tuple[str, ...]


def parse_issue_form(body: str) -> dict[str, str]:
    """Parse GitHub Issue Forms' rendered `### label` sections as plain data."""
    fields: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^### ([^\r\n]+)\r?\n", body or ""))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end():end].strip()
        fields[match.group(1).strip()] = value
    return fields


def parse_submission(body: str) -> tuple[str, str]:
    fields = parse_issue_form(body)
    repo_id = normalize_repo(fields.get(REPO_LABEL, ""))
    raw_kind = fields.get(KIND_LABEL, "")
    if raw_kind not in ALLOWED_KINDS:
        raise ValueError("Kind must be one of the Issue Form options")
    return repo_id, ALLOWED_KINDS[raw_kind]


def assess_submission(
    repo_id: str,
    kind: str,
    github_get: Callable[[str], dict],
    listed: set[str],
) -> Assessment:
    repo = github_get(f"repos/{repo_id}")
    branch = repo.get("default_branch") or "main"
    tree = github_get(
        f"repos/{repo_id}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1"
    )
    paths = [item.get("path", "") for item in tree.get("tree", []) if item.get("type") == "blob"]
    skills = sorted(path for path in paths if path.endswith("SKILL.md"))
    packages = sorted(path for path in paths if path.endswith(".skill"))
    reasons: list[str] = []
    status = "pass"
    public = repo.get("visibility") == "public" and not repo.get("private", False)
    if not public:
        status = "fail"
        reasons.append("repository is not publicly visible")
    if repo_id.lower() in listed:
        status = "fail"
        reasons.append("repository is already listed")
    if tree.get("truncated") and status != "fail":
        status = "needs_review"
        reasons.append("GitHub returned a truncated tree; manual shape review is required")
    if kind in {"skill", "skill-collection"} and not skills and status != "fail":
        status = "needs_review"
        if packages:
            reasons.append("only packaged .skill files were found; the builder does not unpack them")
        else:
            reasons.append("no standard SKILL.md was found on the default branch")
    if not reasons:
        reasons.append("public pointer and repository shape passed automatic preflight")
    return Assessment(
        status=status,
        repo_id=repo_id,
        kind=kind,
        default_branch=branch,
        standard_skill_md=len(skills),
        packaged_dot_skill=len(packages),
        tree_truncated=bool(tree.get("truncated")),
        reasons=tuple(reasons),
    )


def source_block(assessment: Assessment, issue_number: int) -> str:
    if assessment.status != "pass":
        raise ValueError("only passing assessments can become source pointers")
    return (
        f"\n\n# Community submission #{issue_number}; trust signals are pipeline-computed.\n"
        "[[source]]\n"
        f'id   = "{assessment.repo_id}"\n'
        f'kind = "{assessment.kind}"\n'
        f'note = "Community submission via issue #{issue_number}; trust signals are pipeline-computed"\n'
    )


def report_body(assessment: Assessment, pr_url: str | None = None) -> str:
    icon = {"pass": "✅", "needs_review": "⚠️", "fail": "❌"}[assessment.status]
    lines = [
        MARKER,
        f"{icon} Automatic submission preflight: **{assessment.status}**",
        "",
        f"- Repository: `{assessment.repo_id}`",
        f"- Kind: `{assessment.kind}`",
        f"- Default branch: `{assessment.default_branch or 'unknown'}`",
        f"- Standard `SKILL.md`: {assessment.standard_skill_md}",
        f"- Packaged `.skill`: {assessment.packaged_dot_skill}",
        f"- Tree truncated: `{str(assessment.tree_truncated).lower()}`",
        "- Trust score: not accepted from this issue; the pipeline computes it",
        "",
        "Result:",
    ]
    lines.extend(f"- {reason}" for reason in assessment.reasons)
    if pr_url:
        lines.extend(["", f"Draft pointer PR: {pr_url}"])
    elif assessment.status == "needs_review":
        lines.extend([
            "", "A maintainer must resolve the repository-shape question before a draft PR is created.",
        ])
    elif assessment.status == "fail":
        lines.extend(["", "No source-pointer PR was created."])
    lines.extend([
        "", "Merging a draft PR still requires maintainer review; this automation never auto-merges.",
    ])
    return "\n".join(lines)


class GitHubAPI:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None, allow_404: bool = False):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            "https://api.github.com" + path,
            method=method,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "awesomeskills-submission-action",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        idempotent = method in {"GET", "PUT", "PATCH", "DELETE"}
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if allow_404 and exc.code == 404:
                    return None
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                retryable = idempotent and exc.code in {408, 429, 500, 502, 503, 504}
                if retryable and attempt < 4:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(30.0, float(retry_after)) if retry_after else 2.0 * (attempt + 1)
                    except ValueError:
                        delay = 2.0 * (attempt + 1)
                    print(
                        f"GitHub API {method} {path} returned {exc.code}; retrying in {delay:g}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise ActionError(
                    f"GitHub API {method} {path} returned {exc.code}: {detail}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if idempotent and attempt < 4:
                    time.sleep(min(6.0, 1.5 * (attempt + 1)))
                    continue
                raise ActionError(f"GitHub API {method} {path} failed: {exc}") from exc
        raise ActionError(f"GitHub API {method} {path} exhausted retries")

    def get(self, path: str):
        return self.request("GET", "/" + path.lstrip("/"))


def encoded_repo(full_name: str) -> str:
    owner, name = full_name.split("/", 1)
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def find_pull(api: GitHubAPI, repo_path: str, owner: str, branch: str) -> dict | None:
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}"})
    pulls = api.request("GET", f"/repos/{repo_path}/pulls?{query}")
    return pulls[0] if pulls else None


def close_stale_pull(api: GitHubAPI, event: dict, branch: str) -> None:
    repo_path = encoded_repo(event["repository"]["full_name"])
    owner = event["repository"]["owner"]["login"]
    pull = find_pull(api, repo_path, owner, branch)
    if pull:
        api.request("PATCH", f"/repos/{repo_path}/pulls/{pull['number']}", {"state": "closed"})


def ensure_draft_pull(api: GitHubAPI, event: dict, assessment: Assessment) -> str:
    issue_number = int(event["issue"]["number"])
    repository = event["repository"]
    repo_path = encoded_repo(repository["full_name"])
    owner = repository["owner"]["login"]
    base_branch = repository.get("default_branch") or "main"
    branch = f"automation/submission-{issue_number}"
    base_ref = api.request(
        "GET", f"/repos/{repo_path}/git/ref/heads/{urllib.parse.quote(base_branch, safe='')}"
    )
    branch_path = f"/repos/{repo_path}/git/refs/heads/{urllib.parse.quote(branch, safe='')}"
    existing_ref = api.request("GET", branch_path.replace("refs/", "ref/"), allow_404=True)
    base_sha = base_ref["object"]["sha"]
    base_commit = api.request("GET", f"/repos/{repo_path}/git/commits/{base_sha}")
    quoted_base = urllib.parse.quote(base_branch, safe="")
    contents_path = f"/repos/{repo_path}/contents/registry/sources.toml?ref={quoted_base}"
    current = api.request("GET", contents_path)
    text = base64.b64decode(current["content"]).decode("utf-8").rstrip()
    text += source_block(assessment, issue_number)
    blob = api.request("POST", f"/repos/{repo_path}/git/blobs", {
        "content": text + "\n", "encoding": "utf-8",
    })
    tree = api.request("POST", f"/repos/{repo_path}/git/trees", {
        "base_tree": base_commit["tree"]["sha"],
        "tree": [{
            "path": "registry/sources.toml", "mode": "100644", "type": "blob",
            "sha": blob["sha"],
        }],
    })
    proposal = api.request("POST", f"/repos/{repo_path}/git/commits", {
        "message": f"chore: propose {assessment.repo_id} from issue #{issue_number}",
        "tree": tree["sha"], "parents": [base_sha],
    })
    if existing_ref:
        api.request("PATCH", branch_path, {"sha": proposal["sha"], "force": True})
    else:
        api.request("POST", f"/repos/{repo_path}/git/refs", {
            "ref": f"refs/heads/{branch}", "sha": proposal["sha"],
        })

    pull = find_pull(api, repo_path, owner, branch)
    if not pull:
        pull = api.request("POST", f"/repos/{repo_path}/pulls", {
            "title": f"[submission] Add {assessment.repo_id}",
            "head": branch,
            "base": base_branch,
            "body": (
                f"Automated source-pointer proposal for #{issue_number}.\n\n"
                "This PR does not endorse the source. Trust signals must be produced by the "
                "deterministic pipeline, and a maintainer must review before merge.\n\n"
                f"Closes #{issue_number}"
            ),
            "draft": True,
        })
    return pull["html_url"]


def upsert_issue_comment(api: GitHubAPI, event: dict, body: str) -> None:
    repo_path = encoded_repo(event["repository"]["full_name"])
    issue_number = int(event["issue"]["number"])
    comments = api.request("GET", f"/repos/{repo_path}/issues/{issue_number}/comments?per_page=100")
    previous = next(
        (
            comment for comment in comments
            if comment.get("user", {}).get("login") == "github-actions[bot]"
            and comment.get("body", "").startswith(MARKER)
        ),
        None,
    )
    if previous:
        api.request("PATCH", f"/repos/{repo_path}/issues/comments/{previous['id']}", {"body": body})
    else:
        api.request("POST", f"/repos/{repo_path}/issues/{issue_number}/comments", {"body": body})


def write_action_outputs(**values: str | int | None) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            rendered = "" if value is None else str(value)
            if "\n" in rendered or "\r" in rendered:
                raise ValueError(f"invalid multiline action output: {key}")
            stream.write(f"{key}={rendered}\n")


def report_build_result(api: GitHubAPI, event: dict, succeeded: bool) -> int:
    issue_number = int(event["issue"]["number"])
    branch = f"automation/submission-{issue_number}"
    repo_id, kind = parse_submission(event["issue"].get("body", ""))
    repo_path = encoded_repo(event["repository"]["full_name"])
    owner = event["repository"]["owner"]["login"]
    pull = find_pull(api, repo_path, owner, branch)
    preflight = assess_submission(repo_id, kind, api.get, existing_ids())
    assessment = Assessment(
        status="pass" if succeeded else "needs_review",
        repo_id=repo_id, kind=kind, default_branch=preflight.default_branch,
        standard_skill_md=preflight.standard_skill_md,
        packaged_dot_skill=preflight.packaged_dot_skill,
        tree_truncated=preflight.tree_truncated,
        reasons=(
            "the full deterministic build and generated-artifact verification passed"
            if succeeded
            else "the full deterministic build or generated-artifact verification failed",
        ),
    )
    upsert_issue_comment(api, event, report_body(assessment, pull.get("html_url") if pull else None))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-failure", action="store_true")
    parser.add_argument("--build-success", action="store_true")
    args = parser.parse_args()
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    token = os.environ.get("GITHUB_TOKEN", "")
    if not event_path.is_file() or not token:
        print("GITHUB_EVENT_PATH and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    event = json.loads(event_path.read_text(encoding="utf-8"))
    labels = {label.get("name") for label in event.get("issue", {}).get("labels", [])}
    if "submission" not in labels or "pull_request" in event.get("issue", {}):
        print("not a labeled submission issue; nothing to do")
        return 0
    api = GitHubAPI(token)
    if args.build_failure or args.build_success:
        try:
            return report_build_result(api, event, succeeded=args.build_success)
        except (ActionError, KeyError, TypeError, ValueError, urllib.error.URLError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    issue_number = int(event["issue"]["number"])
    branch = f"automation/submission-{issue_number}"
    try:
        repo_id, kind = parse_submission(event["issue"].get("body", ""))
        assessment = assess_submission(repo_id, kind, api.get, existing_ids())
        pr_url = ensure_draft_pull(api, event, assessment) if assessment.status == "pass" else None
        if assessment.status != "pass":
            close_stale_pull(api, event, branch)
        upsert_issue_comment(api, event, report_body(assessment, pr_url))
        write_action_outputs(status=assessment.status, branch=branch if pr_url else "", repo=repo_id)
        print(json.dumps({"status": assessment.status, "repo": repo_id, "pr": pr_url}))
        return 0
    except (ActionError, KeyError, TypeError, ValueError, urllib.error.URLError) as exc:
        fallback = Assessment(
            status="fail", repo_id="invalid submission", kind="unknown", default_branch=None,
            standard_skill_md=0, packaged_dot_skill=0, tree_truncated=False,
            reasons=("automatic processing failed safely; a maintainer should inspect the workflow log",),
        )
        try:
            close_stale_pull(api, event, branch)
            upsert_issue_comment(api, event, report_body(fallback))
        except (ActionError, KeyError, TypeError, ValueError, urllib.error.URLError):
            pass
        write_action_outputs(status="error", branch="", repo="")
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
