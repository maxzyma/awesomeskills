"""Tests for cache reconciliation.

The load-bearing property: binding a legacy entry must not turn it into a claim we cannot
support. Its text was written against an unrecorded revision, so the entry stays `legacy`
and the observed digest goes in a separate field. Writing it to `content_sha256` would make
merge_index report it as `fresh` -- an assertion that the text was assessed against the
current content, which is exactly what nobody knows.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))

from detect_enrichment_changes import build_manifest  # noqa: E402
from merge_index import merge  # noqa: E402
from reconcile_cache import BIND_FIELD, reconcile  # noqa: E402


def base_with(*skills) -> dict:
    return {"generated_at": "t", "repos": {}, "skills": list(skills)}


def skill(skill_id, digest) -> dict:
    return {"id": skill_id, "level": "skill", "content_sha256": digest,
            "name": skill_id, "summary": "s", "source_repo": "o/r",
            "source_url": "u", "path": "p", "trust": {}}


def cache_with(**entries) -> dict:
    return {"schema_version": "0.1", "entries": dict(entries)}


def legacy(grounding="g") -> dict:
    return {"status": "legacy", "content_sha256": None, "grounding": grounding}


def fresh(digest, grounding="g") -> dict:
    return {"status": "fresh", "content_sha256": digest, "grounding": grounding}


# --- binding must not fabricate provenance ----------------------------------------------

def test_binding_keeps_the_entry_legacy():
    out, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    assert out["entries"]["a"]["status"] == "legacy"


def test_binding_does_not_write_content_sha256():
    """content_sha256 means 'assessed against this'. Setting it would make merge_index
    report the entry as fresh."""
    out, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    assert out["entries"]["a"]["content_sha256"] is None
    assert out["entries"]["a"][BIND_FIELD] == "d1"


def test_merge_still_reports_a_bound_entry_as_legacy():
    bound, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    merged = merge(base_with(skill("a", "d1")), bound)
    assert merged["skills"][0]["enrichment_status"] == "legacy"
    assert merged["skills"][0]["grounding"] == "g"


# --- binding takes the entry out of the queue, until the content moves -------------------

def test_bound_legacy_leaves_the_pending_queue():
    bound, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    assert build_manifest(base_with(skill("a", "d1")), bound)["pending_count"] == 0


def test_unbound_legacy_is_still_queued():
    assert build_manifest(base_with(skill("a", "d1")), cache_with(a=legacy()))["pending_count"] == 1


def test_changed_content_puts_a_bound_entry_back_in_the_queue():
    """Once the digest moves we do know the content changed, so re-assessment is earned."""
    bound, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    assert build_manifest(base_with(skill("a", "d2")), bound)["pending_count"] == 1


def test_binding_is_idempotent():
    once, first = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    twice, second = reconcile(base_with(skill("a", "d1")), once)
    assert once == twice
    assert (first["legacy_bound"], second["legacy_bound"]) == (1, 0)


def test_rebinding_after_a_content_change_is_counted_separately():
    once, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    _, report = reconcile(base_with(skill("a", "d2")), once)
    assert (report["legacy_bound"], report["legacy_rebound"]) == (0, 1)


# --- orphans -----------------------------------------------------------------------------

def test_orphaned_entries_are_pruned():
    out, report = reconcile(base_with(skill("a", "d1")), cache_with(a=fresh("d1"), gone=fresh("d9")))
    assert set(out["entries"]) == {"a"}
    assert report["orphans_pruned"] == 1


def test_fresh_entries_are_left_alone():
    cache = cache_with(a=fresh("d1"))
    out, report = reconcile(base_with(skill("a", "d1")), cache)
    assert out["entries"] == cache["entries"]
    assert report["legacy_bound"] == 0


def test_input_cache_is_not_mutated():
    cache = cache_with(a=legacy())
    reconcile(base_with(skill("a", "d1")), cache)
    assert BIND_FIELD not in cache["entries"]["a"]


def test_entry_without_a_base_digest_is_not_bound():
    out, report = reconcile(base_with(skill("a", None)), cache_with(a=legacy()))
    assert BIND_FIELD not in out["entries"]["a"]
    assert report["legacy_bound"] == 0


# --- requeueing legacy after the leakage audit --------------------------------------------

def test_unbind_returns_legacy_to_the_queue():
    """Measured 2026-09-01 across all 249 enriched entries: the current pipeline produced
    no purpose containing a specific noun absent from its evidence, the legacy set produced
    four. Binding froze that in place."""
    bound, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy()))
    unbound, report = reconcile(base_with(skill("a", "d1")), bound, unbind_legacy=True)

    assert BIND_FIELD not in unbound["entries"]["a"]
    assert report["legacy_unbound"] == 1
    assert build_manifest(base_with(skill("a", "d1")), unbound)["pending_count"] == 1


def test_unbind_preserves_the_text_being_served():
    """Requeued entries keep serving their existing text until a replacement lands; the
    queue is 138 deep, so removing them from the site meanwhile would be worse."""
    bound, _ = reconcile(base_with(skill("a", "d1")), cache_with(a=legacy("existing")))
    unbound, _ = reconcile(base_with(skill("a", "d1")), bound, unbind_legacy=True)
    merged = merge(base_with(skill("a", "d1")), unbound)

    assert merged["skills"][0]["grounding"] == "existing"
    assert merged["skills"][0]["enrichment_status"] == "legacy"


def test_unbind_leaves_fresh_entries_untouched():
    cache = cache_with(a=fresh("d1"))
    unbound, report = reconcile(base_with(skill("a", "d1")), cache, unbind_legacy=True)
    assert unbound["entries"] == cache["entries"]
    assert report["legacy_unbound"] == 0
