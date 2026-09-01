"""Tests for the accuracy gate: the evidence pre-filter and the verdict reduction.

Both exist because of one measured failure. Across all 249 enriched entries, a source
saying only "Automate Abuselpdb tasks via Rube MCP" was enriched as "Automate AbuseIPDB
threat intelligence queries and abuse reporting workflows" -- the model recognised the
product through the upstream typo and supplied its function from training data. Every such
claim was true of the real product, which is precisely why reading the text cannot catch it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from apply_verdicts import VerdictError, apply_verdicts, validate_verdict_binding  # noqa: E402
from evidence_check import check_candidate, check_entry, unsupported_terms  # noqa: E402


# --- the detector finds invented specifics ------------------------------------------------

def test_the_case_that_motivated_this():
    evidence = "Automate Abuselpdb tasks via Rube MCP (Composio). Always search tools first."
    text = "Automate AbuseIPDB threat intelligence queries and abuse reporting workflows."
    assert unsupported_terms(text, evidence) == ["AbuseIPDB"]


def test_a_claim_the_evidence_states_is_not_flagged():
    evidence = "Iterate on the Cardputer-Adv MicroPython app bundle after m5-onboard."
    assert unsupported_terms("Iterate on the Cardputer-Adv app bundle.", evidence) == []


# --- and does not flag the noise that would make it unusable ------------------------------

def test_sentence_final_periods_are_not_read_as_identifiers():
    """The first calibration flagged 237 of 249 entries, almost entirely on tokens like
    'counts.' being treated as dotted identifiers."""
    assert unsupported_terms("Reports aggregate counts.", "reports aggregate counts") == []


def test_acronym_plurals_match_their_singular():
    assert unsupported_terms("Queries several APIs.", "the API is documented") == []


def test_compounds_built_from_the_source_are_not_flagged():
    """Coinages like 'Chinese-English' or 'ANSI-to-HTML' assemble the source's own words;
    judging them whole reports the coinage rather than any claim."""
    assert unsupported_terms("Fixes Chinese-English transcripts.", "chinese and english text") == []
    assert unsupported_terms("Performs ANSI-to-HTML conversion.", "converts ansi into html") == []


def test_prefixed_terms_match_their_stem():
    assert unsupported_terms("Supports non-CLI hosts.", "the CLI entry point") == []


def test_ordinary_words_are_never_specific_enough_to_flag():
    assert unsupported_terms("Reads and writes files and data.", "") == []


def test_dotted_identifiers_are_still_caught():
    assert unsupported_terms("Requires Node.js 20.", "a python script") == ["Node.js"]


# --- every claim-bearing field is checked, in both languages -------------------------------

def test_all_prose_fields_are_checked():
    function = {"en": {
        "purpose": "Ordinary text.",
        "io": "Writes PowerPoint decks.",
        "dependencies": ["Node.js"],
        "boundary": "Does not touch UIKit.",
        "scenarios": ["Runs on macOS."],
    }}
    fields = {finding["field"] for finding in check_entry(function, "a plain shell script")}
    assert fields == {"io", "dependencies", "boundary", "scenarios[0]"}


def test_chinese_side_is_checked_too():
    function = {"zh": {"purpose": "调用 AbuseIPDB 接口。", "scenarios": ["查询"]}}
    findings = check_entry(function, "Automate Abuselpdb tasks")
    assert [f["lang"] for f in findings] == ["zh"]


def test_candidate_is_checked_against_the_evidence_the_agent_saw():
    """The manifest carries the exact bytes handed to the enricher, so the check cannot be
    run against content that changed afterwards."""
    manifest = {"entries": [{"id": "a", "evidence": {"content": "Automate Abuselpdb tasks"}}]}
    candidate = {"entries": [{"id": "a", "function": {"en": {"purpose": "Queries AbuseIPDB."}}}]}
    results = check_candidate(candidate, manifest)
    assert results[0]["id"] == "a"
    assert results[0]["findings"][0]["terms"] == ["AbuseIPDB"]


# --- the verdict gate cannot fail open -----------------------------------------------------

def candidate_of(*ids) -> dict:
    return {"entries": [{"id": i, "function": {}, "content_sha256": "d"} for i in ids]}


def manifest_of(*ids) -> dict:
    return {"entries": [{"id": i, "content_sha256": "d"} for i in ids], "pending_count": len(ids)}


def verdicts_of(**rulings) -> dict:
    return {"generated_at": "t", "agent": "a", "model": "m", "verdicts": [
        {"id": i, "supported": bool(ok), "unsupported_claims": [] if ok else ["claimed X"]}
        for i, ok in rulings.items()
    ]}


def test_an_unmentioned_entry_cannot_slip_through():
    """Silence is the quietest way for a gate to fail open, so it is an error."""
    with pytest.raises(VerdictError, match="missing"):
        validate_verdict_binding(candidate_of("a", "b"), verdicts_of(a=True))


def test_verdicts_for_entries_not_in_the_batch_are_rejected():
    with pytest.raises(VerdictError, match="unexpected"):
        validate_verdict_binding(candidate_of("a"), verdicts_of(a=True, b=True))


def test_duplicate_rulings_are_rejected():
    doubled = verdicts_of(a=True)
    doubled["verdicts"].append({"id": "a", "supported": False, "unsupported_claims": ["x"]})
    with pytest.raises(VerdictError, match="duplicate"):
        validate_verdict_binding(candidate_of("a"), doubled)


def test_a_rejection_must_say_what_was_wrong():
    silent = {"generated_at": "t", "agent": "a", "model": "m",
              "verdicts": [{"id": "a", "supported": False, "unsupported_claims": []}]}
    with pytest.raises(VerdictError, match="without a stated claim"):
        apply_verdicts(candidate_of("a"), manifest_of("a"), silent)


# --- reduction keeps the batch honest ------------------------------------------------------

def test_rejected_entries_leave_both_candidate_and_manifest():
    """Reducing only the candidate would break the exact-coverage check that stops an
    enricher from omitting entries."""
    cand, man, report = apply_verdicts(
        candidate_of("a", "b", "c"), manifest_of("a", "b", "c"), verdicts_of(a=True, b=False, c=True),
    )
    assert [e["id"] for e in cand["entries"]] == ["a", "c"]
    assert [e["id"] for e in man["entries"]] == ["a", "c"]
    assert man["pending_count"] == 2
    assert report["rejected_ids"] == ["b"]
    assert report["reasons"]["b"] == ["claimed X"]


def test_the_reduced_pair_still_satisfies_the_manifest_binding():
    from enrichment_store import validate_manifest_binding
    cand, man, _ = apply_verdicts(
        candidate_of("a", "b"), manifest_of("a", "b"), verdicts_of(a=True, b=False),
    )
    validate_manifest_binding(cand, man)


def test_inputs_are_not_mutated():
    candidate, manifest = candidate_of("a", "b"), manifest_of("a", "b")
    apply_verdicts(candidate, manifest, verdicts_of(a=True, b=False))
    assert len(candidate["entries"]) == 2
    assert len(manifest["entries"]) == 2


def test_an_all_clean_batch_passes_through_whole():
    cand, man, report = apply_verdicts(
        candidate_of("a", "b"), manifest_of("a", "b"), verdicts_of(a=True, b=True),
    )
    assert report == {"submitted": 2, "upheld": 2, "rejected": 0, "rejected_ids": [],
                      "reasons": {}, "observations": {}}
    assert len(cand["entries"]) == len(man["entries"]) == 2


# --- observations are recorded, never acted on --------------------------------------------

def test_an_observation_does_not_reject_an_entry():
    """Without somewhere to put a remark, the only way to report anything is to reject. The
    first calibration run lost two entries that way: the verifier noticed their evidence was
    byte-identical to another entry -- true, useful, and nothing to do with claim support."""
    noted = verdicts_of(a=True, b=True)
    noted["verdicts"][0]["observations"] = ["evidence is byte-identical to entry b"]

    cand, man, report = apply_verdicts(candidate_of("a", "b"), manifest_of("a", "b"), noted)
    assert report["rejected"] == 0
    assert len(cand["entries"]) == 2
    assert report["observations"]["a"] == ["evidence is byte-identical to entry b"]


def test_a_rejection_can_also_carry_observations():
    noted = verdicts_of(a=False)
    noted["verdicts"][0]["observations"] = ["source is one line long"]
    _, _, report = apply_verdicts(candidate_of("a", "b"), manifest_of("a", "b"),
                                  {**noted, "verdicts": noted["verdicts"] + [
                                      {"id": "b", "supported": True, "unsupported_claims": []}]})
    assert report["rejected_ids"] == ["a"]
    assert report["observations"]["a"] == ["source is one line long"]


def test_entries_without_observations_are_absent_from_the_report():
    _, _, report = apply_verdicts(candidate_of("a"), manifest_of("a"), verdicts_of(a=True))
    assert report["observations"] == {}
