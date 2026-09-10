from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
import threading

import pytest

from mao_core.config import Config
from mao_core.budget import BudgetGuard
from mao_core.errors import MaoError
from mao_core.providers.base import ModelIdentity
from mao_core.review import CriticOutcome, Decision, Finding, ReviewResult
from mao_core.state import StateStore
from mao_core.workflow import FinalResult, Workflow, select_reviewers


class FakeProvider:
    def __init__(self, name):
        self.name = name


class FakeTransport:
    pass


def config():
    return Config(
        primary_provider="codex",
        codex_model="gpt-main",
        claude_model="claude-critic",
        antigravity_model="gemini-critic",
    )


@pytest.fixture
def workflow(tmp_path):
    return Workflow(
        StateStore(tmp_path / ".multi-agent-orchestrator"),
        config(),
        providers={
            "claude": FakeProvider("claude"),
            "antigravity": FakeProvider("antigravity"),
        },
        transports={"direct": FakeTransport()},
        project=tmp_path,
    )


def clean_review():
    return ReviewResult("clean", (), {}, True)


def finding():
    return Finding(
        "major", "correctness", "src/a.py", 10, "bad", "reason", "fix", 0.9, ()
    )


def identity(provider, model, vendor, verified=True):
    return ModelIdentity(provider, model, model, vendor, verified)


def test_final_result_has_exact_frozen_fields():
    assert [item.name for item in fields(FinalResult)] == [
        "status", "findings", "review_gaps"
    ]


def test_packet_revision_lock_blocks_concurrent_phase_claim(workflow):
    run = workflow.create_run("request", run_id="run-1")
    entered = threading.Event()
    release = threading.Event()
    transitioned = threading.Event()

    def build(_revision_root):
        entered.set()
        assert release.wait(timeout=2)
        return {"claude": "a" * 64}

    packet_thread = threading.Thread(
        target=lambda: workflow.commit_packet_revision(run, 1, build)
    )
    transition_thread = threading.Thread(
        target=lambda: (workflow.mark_implemented(run), transitioned.set())
    )
    packet_thread.start()
    assert entered.wait(timeout=2)
    transition_thread.start()
    assert not transitioned.wait(timeout=0.05)
    release.set()
    packet_thread.join(timeout=2)
    transition_thread.join(timeout=2)

    assert transitioned.is_set()
    assert run.phase == "IMPLEMENTED"


def test_round_two_skipped_when_no_output_changed(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, passed=True, output="PASS")
    workflow.record_round_one(run, reviews=[clean_review()])
    workflow.record_decisions(run, decisions=[])

    assert workflow.prepare_round_two(run, output_changed=False) is None
    assert run.phase == "FINAL_DECISION"
    assert workflow.resume("run-1", "request").phase == "FINAL_DECISION"


def test_round_two_only_selects_affected_reviewer(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    outcome = CriticOutcome(
        "claude", 1, "completed", ReviewResult("found", (finding(),), {}, True), None
    )
    workflow.record_round_one(run, [outcome])
    fingerprint = workflow.finding_fingerprints(run)[0]
    workflow.record_decisions(run, [Decision(fingerprint, "accepted", "valid")])
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "PASS AGAIN")

    jobs = workflow.prepare_round_two(run, output_changed=True)

    assert [job.critic for job in jobs] == ["claude"]
    assert all(job.round_number == 2 for job in jobs)
    assert run.phase == "REVIEW_ROUND_2"


def test_context_request_can_gate_round_two_without_output_change(workflow):
    context_finding = Finding(
        "minor", "requirements", "src/a.py", None, "unclear", "reason", "supply", 0.5,
        ("src/config.py",),
    )
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [CriticOutcome("claude", 1, "completed", ReviewResult("x", (context_finding,), {}, True), None)],
    )
    workflow.record_decisions(
        run,
        [Decision(workflow.finding_fingerprints(run)[0], "needs-proof", "need file")],
    )
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "PACKET CONTEXT VERIFIED")

    jobs = workflow.prepare_round_two(run, supplied_context=True)

    assert [job.critic for job in jobs] == ["claude"]
    assert run.phase == "REVIEW_ROUND_2"


def test_any_round_two_gate_requires_local_reverification(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(run, [clean_review()])
    workflow.record_decisions(run, [])
    with pytest.raises(MaoError) as caught:
        workflow.prepare_round_two(run, new_review_surface=True)
    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert run.phase == "TRIAGED"


def test_two_failed_critics_yield_review_gap(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    failed = [
        CriticOutcome(name, 1, "failed", None, {"error": {"code": "TIMEOUT"}})
        for name in ("claude", "antigravity")
    ]
    workflow.record_round_one(run, failed)

    result = workflow.finalize(run)

    assert result.status == "COMPLETED_WITH_REVIEW_GAP"
    assert result.review_gaps == ("claude", "antigravity")
    assert run.phase == "DONE"


def test_reviewer_selection_uses_actual_vendor():
    primary = identity("antigravity", "claude-opus-4-6-thinking", "anthropic")
    available = [
        identity("claude", "claude-fable-5-1", "anthropic"),
        identity("codex", "gpt-5.6-sol", "openai"),
        identity("antigravity", "gemini-3.1-pro-high", "google"),
    ]
    selected = select_reviewers(primary, available)
    assert [item.vendor for item in selected] == ["openai", "google"]


def test_reviewer_selection_rejects_unverified_and_duplicate_vendor():
    selected = select_reviewers(
        identity("codex", "gpt-main", "openai"),
        [
            identity("agy", "claude-one", "anthropic", False),
            identity("claude", "claude-two", "anthropic"),
            identity("agy", "claude-three", "anthropic"),
            identity("agy", "gemini", "google"),
        ],
    )
    assert [(item.provider, item.vendor) for item in selected] == [
        ("claude", "anthropic"), ("agy", "google")
    ]


def test_illegal_transition_is_typed_and_does_not_change_persisted_state(workflow):
    run = workflow.create_run("request", run_id="run-1")
    with pytest.raises(MaoError) as caught:
        workflow.record_round_one(run, [clean_review()])
    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert workflow.resume("run-1", "request").phase == "CREATED"


def test_failed_local_verification_blocks_review(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, False, "FAIL")
    assert run.phase == "IMPLEMENTED"
    with pytest.raises(MaoError):
        workflow.record_round_one(run, [clean_review()])


def test_decisions_must_cover_every_finding_exactly_once(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [CriticOutcome("claude", 1, "completed", ReviewResult("x", (finding(),), {}, True), None)],
    )
    with pytest.raises(MaoError) as caught:
        workflow.record_decisions(run, [])
    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert run.phase == "REVIEW_ROUND_1"


def test_resume_rejects_changed_request(workflow):
    workflow.create_run("request", run_id="run-1")
    with pytest.raises(MaoError) as caught:
        workflow.resume("run-1", "different")
    assert caught.value.code == "STATE_TRANSITION_INVALID"


def test_finalize_returns_only_accepted_deduplicated_findings(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    duplicate = Finding(
        "minor", "correctness", "src/a.py", 11, "BAD", "other", "fix", 0.8, ()
    )
    workflow.record_round_one(
        run,
        [
            CriticOutcome("claude", 1, "completed", ReviewResult("x", (finding(),), {}, True), None),
            CriticOutcome("antigravity", 1, "completed", ReviewResult("x", (duplicate,), {}, True), None),
        ],
    )
    fingerprint = workflow.finding_fingerprints(run)[0]
    workflow.record_decisions(run, [Decision(fingerprint, "accepted", "valid")])
    workflow.prepare_round_two(run, output_changed=False)
    result = workflow.finalize(run)
    assert result.status == "COMPLETED"
    assert result.findings == (finding(),)
    assert result.review_gaps == ()


def test_stale_round_writer_cannot_overwrite_newer_review_evidence(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    stale = workflow.resume("run-1", "request")
    workflow.record_round_one(run, [clean_review()])
    evidence_path = workflow.store.runs_directory / "run-1/workflow.json"
    expected = evidence_path.read_bytes()

    with pytest.raises(MaoError):
        workflow.record_round_one(
            stale,
            [CriticOutcome("claude", 1, "failed", None, {"error": {"code": "TIMEOUT"}})],
        )

    assert evidence_path.read_bytes() == expected


def test_stale_local_verification_cannot_append_evidence(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    stale = workflow.resume("run-1", "request")
    workflow.record_local_verification(run, True, "PASS")
    evidence_path = workflow.store.runs_directory / "run-1/workflow.json"
    expected = evidence_path.read_bytes()

    with pytest.raises(MaoError):
        workflow.record_local_verification(stale, False, "STALE FAIL")

    assert evidence_path.read_bytes() == expected


def test_round_two_missing_unaffected_reviewer_does_not_create_gap(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [
            CriticOutcome("claude", 1, "completed", ReviewResult("x", (finding(),), {}, True), None),
            CriticOutcome("antigravity", 1, "completed", clean_review(), None),
        ],
    )
    workflow.record_decisions(
        run,
        [Decision(workflow.finding_fingerprints(run)[0], "accepted", "valid")],
    )
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "PASS AGAIN")
    jobs = workflow.prepare_round_two(run, output_changed=True)
    assert [item.critic for item in jobs] == ["claude"]
    workflow.record_round_two(
        run,
        [CriticOutcome("claude", 2, "completed", clean_review(), None)],
    )
    result = workflow.finalize(run)
    assert result.review_gaps == ()


def test_state_save_failure_rolls_back_transition_evidence(workflow, monkeypatch):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    evidence_path = workflow.store.runs_directory / "run-1/workflow.json"
    expected = evidence_path.read_bytes()

    monkeypatch.setattr(
        workflow.store,
        "_save_atomic_unlocked",
        lambda _state, _run_id: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        workflow.record_round_one(run, [clean_review()])

    assert evidence_path.read_bytes() == expected
    assert workflow.store.load("run-1").phase == "LOCAL_VERIFIED"


def test_corrupt_workflow_evidence_fails_with_typed_error(workflow):
    run = workflow.create_run("request", run_id="run-1")
    evidence = workflow.store.runs_directory / "run-1/workflow.json"
    evidence.write_text(
        json.dumps({"request_digest": run.request_digest, "rounds": []}),
        encoding="utf-8",
    )
    with pytest.raises(MaoError) as caught:
        workflow.finding_fingerprints(run)
    assert caught.value.code == "STATE_TRANSITION_INVALID"


def test_round_two_new_finding_requires_decision_and_is_in_final_result(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [
            CriticOutcome("claude", 1, "completed", clean_review(), None),
            CriticOutcome("antigravity", 1, "completed", clean_review(), None),
        ],
    )
    workflow.record_decisions(run, [])
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "NEW SURFACE", new_review_surface=True)
    workflow.prepare_round_two(run, new_review_surface=True)
    workflow.record_round_two(
        run,
        [CriticOutcome("claude", 2, "completed", ReviewResult("new", (finding(),), {}, True), None)],
    )
    assert run.phase == "REVIEW_ROUND_2"
    with pytest.raises(MaoError):
        workflow.finalize(run)
    fingerprint = workflow.finding_fingerprints(run)[0]
    workflow.record_decisions(run, [Decision(fingerprint, "accepted", "valid")])
    result = workflow.finalize(run)
    assert result.findings == (finding(),)


def test_round_two_subset_is_rejected_before_phase_claim(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [
            CriticOutcome("claude", 1, "completed", clean_review(), None),
            CriticOutcome("antigravity", 1, "completed", clean_review(), None),
        ],
    )
    workflow.record_decisions(run, [])
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "NEW", new_review_surface=True)
    with pytest.raises(MaoError):
        workflow.prepare_round_two(
            run,
            new_review_surface=True,
            critics=["claude"],
        )
    assert run.phase == "LOCAL_REVERIFIED"


def test_round_one_jobs_atomically_claim_phase_before_dispatch(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    stale = workflow.resume("run-1", "request")
    jobs = workflow.round_one_jobs(run, ["claude"])
    assert [item.critic for item in jobs] == ["claude"]
    assert run.phase == "REVIEW_ROUND_1"
    with pytest.raises(MaoError):
        workflow.round_one_jobs(stale, ["claude"])


def test_incomplete_completed_review_becomes_review_gap(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [
            CriticOutcome("claude", 1, "completed", ReviewResult("partial", (), {}, False), None),
            CriticOutcome("antigravity", 1, "completed", clean_review(), None),
        ],
    )
    result = workflow.finalize(run)
    assert result.status == "COMPLETED_WITH_REVIEW_GAP"
    assert result.review_gaps == ("claude",)


def test_crash_journal_recovers_evidence_to_authoritative_phase(workflow, monkeypatch):
    class SimulatedCrash(BaseException):
        pass

    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    evidence = workflow.store.runs_directory / "run-1/workflow.json"
    expected = evidence.read_bytes()
    monkeypatch.setattr(
        workflow.store,
        "_save_atomic_unlocked",
        lambda _state, _run_id: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        workflow.record_round_one(run, [clean_review()])
    assert evidence.read_bytes() != expected
    monkeypatch.undo()

    resumed = workflow.resume("run-1", "request")
    assert resumed.phase == "LOCAL_VERIFIED"
    assert evidence.read_bytes() == expected
    assert not (workflow.store.runs_directory / "run-1/workflow-transaction.json").exists()


def test_resume_recovers_completed_critic_without_creating_new_job(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(run, ["claude"])
    guard = BudgetGuard(2, 2, 4, state=run, store=workflow.store)
    guard.reserve_call("claude", 1)
    guard.complete_call("claude", 1)
    attempt = (
        workflow.store.runs_directory
        / "run-1/round-1/claude/attempt-1/review.json"
    )
    attempt.parent.mkdir(parents=True)
    attempt.write_text(
        json.dumps({"summary": "clean", "findings": [], "usage": {}, "review_complete": True}),
        encoding="utf-8",
    )
    resumed = workflow.resume("run-1", "request")
    recovered, jobs = workflow.resume_review_jobs(resumed, 1)
    assert jobs == []
    assert len(recovered) == 1
    assert recovered[0].call_number == 1
    assert recovered[0].status == "completed"


def test_recorded_round_cannot_be_resumed_into_more_jobs(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(run, ["claude"])
    workflow.record_round_one(
        run,
        [CriticOutcome("claude", 1, "completed", clean_review(), None)],
    )
    with pytest.raises(MaoError) as caught:
        workflow.resume_review_jobs(run, 1)
    assert caught.value.code == "STATE_TRANSITION_INVALID"


def test_active_round_one_claim_blocks_decide_and_finalize(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(run, ["claude"])
    with pytest.raises(MaoError):
        workflow.record_decisions(run, [])
    with pytest.raises(MaoError):
        workflow.finalize(run)
    assert run.phase == "REVIEW_ROUND_1"


def test_repeated_round_two_fingerprint_requires_fresh_decision(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(
        run,
        [CriticOutcome("claude", 1, "completed", ReviewResult("x", (finding(),), {}, True), None)],
    )
    fingerprint = workflow.finding_fingerprints(run)[0]
    workflow.record_decisions(run, [Decision(fingerprint, "accepted", "first")])
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "PASS AGAIN")
    workflow.prepare_round_two(run, output_changed=True)
    workflow.record_round_two(
        run,
        [CriticOutcome("claude", 2, "completed", ReviewResult("still", (finding(),), {}, True), None)],
    )
    assert run.phase == "REVIEW_ROUND_2"
    with pytest.raises(MaoError):
        workflow.finalize(run)
    workflow.record_decisions(run, [Decision(fingerprint, "rejected", "fixed enough")])
    assert workflow.finalize(run).findings == ()


def test_persisted_new_review_surface_gates_round_two_without_repeated_flag(workflow):
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.record_round_one(run, [clean_review()])
    workflow.record_decisions(run, [])
    workflow.mark_patched(run)
    workflow.record_local_verification(run, True, "NEW", new_review_surface=True)
    jobs = workflow.prepare_round_two(run)
    assert [item.critic for item in jobs] == ["claude"]


def test_reserved_reviewer_cannot_resume_until_lease_expires(
    workflow, monkeypatch
):
    import mao_core.workflow as workflow_module

    now = 1_800_000_000.0
    monkeypatch.setattr(workflow_module.time, "time", lambda: now)
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(run, ["claude"])
    guard = BudgetGuard(2, 2, 4, state=run, store=workflow.store)
    guard.reserve_call("claude", 1)
    resumed = workflow.resume("run-1", "request")

    with pytest.raises(MaoError) as caught:
        workflow.resume_review_jobs(resumed, 1)
    assert caught.value.code == "STATE_TRANSITION_INVALID"

    monkeypatch.setattr(
        workflow_module.time,
        "time",
        lambda: now + workflow.config.timeout_seconds + 2,
    )
    recovered, jobs = workflow.resume_review_jobs(resumed, 1)
    assert recovered == []
    assert [item.critic for item in jobs] == ["claude"]

    competing = workflow.resume("run-1", "request")
    with pytest.raises(MaoError):
        workflow.resume_review_jobs(competing, 1)


def test_resume_never_launches_expected_reviewer_omitted_from_original_claim(
    workflow, monkeypatch
):
    import mao_core.workflow as workflow_module

    now = 1_800_000_000.0
    monkeypatch.setattr(workflow_module.time, "time", lambda: now)
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(
        run,
        ["claude"],
        expected_critics=["claude", "antigravity"],
    )
    monkeypatch.setattr(
        workflow_module.time,
        "time",
        lambda: now + workflow.config.timeout_seconds + 2,
    )

    resumed = workflow.resume("run-1", "request")
    recovered, jobs = workflow.resume_review_jobs(resumed, 1)

    assert recovered == []
    assert [job.critic for job in jobs] == ["claude"]


def test_expired_lease_takeover_rejects_stale_completion_and_result(
    workflow, monkeypatch
):
    import mao_core.workflow as workflow_module

    now = 1_800_000_000.0
    monkeypatch.setattr(workflow_module.time, "time", lambda: now)
    old_run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(old_run)
    workflow.record_local_verification(old_run, True, "PASS")
    workflow.round_one_jobs(old_run, ["claude"])
    old_guard = BudgetGuard(2, 2, 4, state=old_run, store=workflow.store)
    old_guard.reserve_call("claude", 1)

    monkeypatch.setattr(
        workflow_module.time,
        "time",
        lambda: now + workflow.config.timeout_seconds + 2,
    )
    new_workflow = Workflow(
        workflow.store,
        workflow.config,
        providers=workflow.providers,
        transports=workflow.transports,
        project=workflow.project,
    )
    new_run = new_workflow.resume("run-1", "request")
    recovered, jobs = new_workflow.resume_review_jobs(new_run, 1)
    assert recovered == []
    assert [item.critic for item in jobs] == ["claude"]

    with pytest.raises(MaoError, match="stale"):
        workflow.complete_owned_call(old_run, "claude", 1)
    assert workflow.store.load("run-1").critics["claude"].rounds["1"] == "reserved"

    new_guard = BudgetGuard(2, 2, 4, state=new_run, store=workflow.store)
    new_guard.reserve_call("claude", 1)
    new_workflow.complete_owned_call(new_run, "claude", 1)

    with pytest.raises(MaoError, match="stale"):
        workflow.record_round_one(
            old_run,
            [CriticOutcome("claude", 1, "completed", clean_review(), None)],
        )
    assert not new_workflow._metadata(new_run)["round_complete"]["1"]

    new_workflow.record_round_one(
        new_run,
        [CriticOutcome("claude", 2, "completed", clean_review(), None)],
    )
    assert new_workflow._metadata(new_run)["round_complete"]["1"]


def test_expired_lease_rejects_completion_and_result_before_takeover(
    workflow, monkeypatch
):
    import mao_core.workflow as workflow_module

    now = 1_800_000_000.0
    monkeypatch.setattr(workflow_module.time, "time", lambda: now)
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(run, ["claude"])
    guard = BudgetGuard(2, 2, 4, state=run, store=workflow.store)
    guard.reserve_call("claude", 1)

    monkeypatch.setattr(
        workflow_module.time,
        "time",
        lambda: now + workflow.config.timeout_seconds + 2,
    )
    with pytest.raises(MaoError, match="expired"):
        workflow.complete_owned_call(run, "claude", 1)
    with pytest.raises(MaoError, match="expired"):
        workflow.record_round_one(
            run,
            [CriticOutcome("claude", 1, "completed", clean_review(), None)],
        )
    persisted = workflow.store.load("run-1")
    assert persisted.critics["claude"].rounds["1"] == "reserved"
    assert not workflow._metadata(run)["round_complete"]["1"]


def test_result_can_settle_after_owner_completed_before_lease_expiry(
    workflow, monkeypatch
):
    import mao_core.workflow as workflow_module

    now = 1_800_000_000.0
    monkeypatch.setattr(workflow_module.time, "time", lambda: now)
    run = workflow.create_run("request", run_id="run-1")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, True, "PASS")
    workflow.round_one_jobs(run, ["claude"])
    guard = BudgetGuard(2, 2, 4, state=run, store=workflow.store)
    guard.reserve_call("claude", 1)
    workflow.complete_owned_call(run, "claude", 1)

    monkeypatch.setattr(
        workflow_module.time,
        "time",
        lambda: now + workflow.config.timeout_seconds + 2,
    )
    workflow.record_round_one(
        run,
        [CriticOutcome("claude", 1, "completed", clean_review(), None)],
    )
    assert workflow._metadata(run)["round_complete"]["1"]
