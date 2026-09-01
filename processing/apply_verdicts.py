#!/usr/bin/env python3
"""Reduce an enrichment batch to the entries a verification pass upheld.

The enricher writes; a second agent reads the same evidence and rules on whether every
claim is traceable to it. This module applies those rulings -- deterministically, in the
public repo, where the logic is inspectable and tested -- rather than letting the private
orchestrator decide what survives.

Rejected entries are dropped from both the candidate and the manifest. Reducing both keeps
the existing integrity property intact: `validate_manifest_binding` still demands the batch
cover its manifest exactly, so an enricher cannot quietly omit or substitute entries. What
changes is that we asked for less after the verifier spoke, which is our decision and is
recorded.

A rejected entry is not marked bad. It simply stays in the queue and is attempted again,
because a rejection means the claim was not shown, not that the skill is unfit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


class VerdictError(RuntimeError):
    """The verdict batch cannot be trusted to describe this candidate."""


def validate_verdict_binding(candidate: dict, verdicts: dict) -> None:
    """Require exactly one ruling per candidate entry.

    Same reasoning as the manifest binding: a partial verdict set would let entries pass by
    going unmentioned, which is the quietest possible way for the gate to fail open.
    """
    expected = {entry.get("id") for entry in candidate.get("entries", [])}
    actual = [verdict.get("id") for verdict in verdicts.get("verdicts", [])]
    if not expected:
        raise VerdictError("candidate has no entries")
    if len(actual) != len(set(actual)):
        raise VerdictError("verdict batch contains duplicate ids")
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        unexpected = sorted(set(actual) - expected)
        raise VerdictError(
            f"verdicts do not exactly cover the candidate; missing={missing}, unexpected={unexpected}"
        )


def _observations(verdicts: dict) -> dict[str, list[str]]:
    """Remarks that are not rulings. Carried into the report, never into the decision."""
    return {
        verdict["id"]: verdict["observations"]
        for verdict in verdicts.get("verdicts", [])
        if verdict.get("observations")
    }


def _rejection_reasons(verdicts: dict) -> dict[str, list[str]]:
    return {
        verdict["id"]: verdict.get("unsupported_claims") or []
        for verdict in verdicts.get("verdicts", [])
        if not verdict.get("supported")
    }


UNEXPLAINED_ACCEPTANCE = (
    "upheld despite naming a term absent from the evidence, with no observation saying what "
    "entails it; acceptances under the entailment exception must be recorded to be reviewable"
)


def apply_verdicts(
    candidate: dict, manifest: dict, verdicts: dict, flagged_ids: set[str] | None = None,
) -> tuple[dict, dict, dict]:
    """Return (reduced candidate, reduced manifest, report). Inputs are not mutated.

    `flagged_ids` are entries where the deterministic pre-filter found a term present in the
    enrichment and absent from the evidence. Upholding one of those means the verifier
    accepted an addition -- permitted only when the evidence's own subject could not exist
    without it, and required to be recorded. Enforcing that here turns the rule from advice
    into something the batch has to satisfy: an exception nobody has to justify is a rule
    that widens quietly.
    """
    validate_verdict_binding(candidate, verdicts)
    rejected = _rejection_reasons(verdicts)
    observations = _observations(verdicts)

    for skill_id in sorted(flagged_ids or set()):
        if skill_id not in rejected and skill_id not in observations:
            rejected[skill_id] = [UNEXPLAINED_ACCEPTANCE]

    unexplained = sorted(skill_id for skill_id, claims in rejected.items() if not claims)
    if unexplained:
        raise VerdictError(
            f"rejection without a stated claim: {unexplained}; a gate that cannot say what "
            "was wrong cannot be reviewed"
        )

    kept_entries = [e for e in candidate.get("entries", []) if e.get("id") not in rejected]
    kept_ids = {entry.get("id") for entry in kept_entries}
    kept_manifest = [e for e in manifest.get("entries", []) if e.get("id") in kept_ids]

    report = {
        "submitted": len(candidate.get("entries", [])),
        "upheld": len(kept_entries),
        "rejected": len(rejected),
        "rejected_ids": sorted(rejected),
        "reasons": rejected,
        "observations": observations,
    }
    reduced_candidate = {**candidate, "entries": kept_entries}
    reduced_manifest = {
        **manifest, "entries": kept_manifest, "pending_count": len(kept_manifest),
    }
    return reduced_candidate, reduced_manifest, report


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("verdicts", type=Path)
    parser.add_argument("--out-candidate", type=Path, required=True)
    parser.add_argument("--out-manifest", type=Path, required=True)
    parser.add_argument(
        "--flagged", type=Path,
        help="JSON array of ids the deterministic pre-filter flagged; upholding one without "
             "an observation is treated as a rejection",
    )
    args = parser.parse_args()

    try:
        reduced_candidate, reduced_manifest, report = apply_verdicts(
            json.loads(args.candidate.read_text(encoding="utf-8")),
            json.loads(args.manifest.read_text(encoding="utf-8")),
            json.loads(args.verdicts.read_text(encoding="utf-8")),
            set(json.loads(args.flagged.read_text(encoding="utf-8"))) if args.flagged else None,
        )
    except (VerdictError, json.JSONDecodeError) as error:
        print(f"verdict application failed: {error}", file=sys.stderr)
        return 1

    if not reduced_candidate["entries"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("every entry was rejected; nothing to apply", file=sys.stderr)
        return 2

    _write(args.out_candidate, reduced_candidate)
    _write(args.out_manifest, reduced_manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
