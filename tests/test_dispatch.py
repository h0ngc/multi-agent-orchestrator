from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import threading
import time

import pytest

from mao_core.budget import BudgetGuard
from mao_core.errors import MaoError
from mao_core.process import ProcessResult
from mao_core.review import (
    CriticJob,
    capture_workspace_fingerprint,
    dispatch_parallel,
)
from mao_core.state import StateStore
from mao_core.transports.base import InvocationRequest


VALID_REVIEW = json.loads(
    (Path(__file__).parent / "fixtures/review-valid.json").read_text()
)


class FakeProvider:
    def __init__(self, name, *, available=True, parsed=None):
        self.name = name
        self.available = available
        self.parsed = parsed or VALID_REVIEW
        self.detect_calls = 0

    def detect(self):
        self.detect_calls += 1
        return {
            "status": "available" if self.available else "unavailable",
            "executable": f"/fake/{self.name}",
        }

    def parse_result(self, result):
        if result.exit_code != 0:
            raise MaoError("INVALID_RESULT", "provider failed", {})
        return dict(self.parsed)

    def measure_usage(self, parsed):
        return {"input_tokens": 3, "output_tokens": 2}


class FakeTransport:
    def __init__(self, action=None):
        self.action = action
        self.calls = 0

    def invoke(self, provider, request):
        self.calls += 1
        if self.action is not None:
            self.action(provider, request)
        return ProcessResult(
            status="ok",
            exit_code=0,
            stdout=json.dumps(provider.parsed),
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )


def job(tmp_path, critic="claude", provider=None, transport=None, round_number=1):
    provider = provider or FakeProvider(critic)
    transport = transport or FakeTransport()
    packet_directory = (
        tmp_path / ".multi-agent-orchestrator/runs/run-1/packet" / critic
    )
    packet_directory.mkdir(parents=True, exist_ok=True)
    packet = packet_directory / "packet.md"
    schema = packet_directory / "schema.json"
    packet.write_text("packet", encoding="utf-8")
    schema.write_text("{}", encoding="utf-8")
    request = InvocationRequest(
        provider=provider.name,
        model=f"{provider.name}-model",
        packet=packet,
        schema=schema,
        cwd=packet_directory,
        timeout_seconds=10,
        run_id="run-1",
    )
    return CriticJob(critic, round_number, provider, transport, request)


def test_missing_executable_is_reported_before_budget_reservation(tmp_path):
    provider = FakeProvider("claude", available=False)
    transport = FakeTransport()
    guard = BudgetGuard(2, 2, 4)
    outcome = dispatch_parallel(
        [job(tmp_path, provider=provider, transport=transport)], guard, tmp_path
    )[0]
    assert outcome.error["error"]["code"] == "CLI_NOT_FOUND"
    assert outcome.call_number == 0
    assert guard.total == 0
    assert transport.calls == 0


def test_authoritative_project_cwd_is_rejected_before_budget_reservation(tmp_path):
    critic_job = job(tmp_path)
    unsafe_request = InvocationRequest(
        provider=critic_job.request.provider,
        model=critic_job.request.model,
        packet=critic_job.request.packet,
        schema=critic_job.request.schema,
        cwd=tmp_path,
        timeout_seconds=critic_job.request.timeout_seconds,
        run_id=critic_job.request.run_id,
    )
    critic_job = type(critic_job)(
        critic_job.critic,
        critic_job.round_number,
        critic_job.provider,
        critic_job.transport,
        unsafe_request,
    )
    guard = BudgetGuard(2, 2, 4)
    outcome = dispatch_parallel([critic_job], guard, tmp_path)[0]
    assert outcome.status == "failed"
    assert outcome.error["error"]["code"] == "INVALID_RESULT"
    assert guard.total == 0


def test_parallel_dispatch_reserves_before_starting(tmp_path):
    guard = BudgetGuard(2, 2, 4)

    def assert_reserved(provider, _request):
        assert guard.calls_for(provider.name) == 1

    jobs = [
        job(tmp_path, "claude", transport=FakeTransport(assert_reserved)),
        job(tmp_path, "agy", transport=FakeTransport(assert_reserved)),
    ]
    outcomes = dispatch_parallel(jobs, guard, tmp_path)
    assert {item.critic for item in outcomes} == {"claude", "agy"}
    assert all(item.call_number == 1 for item in outcomes)
    assert all(item.status == "completed" for item in outcomes)
    assert guard.total == 2


def test_dispatch_uses_at_most_two_workers_and_keeps_input_order(tmp_path):
    lock = threading.Lock()
    active = 0
    maximum = 0

    def slow(_provider, _request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1

    jobs = [
        job(tmp_path, name, transport=FakeTransport(slow))
        for name in ("claude", "agy", "codex")
    ]
    outcomes = dispatch_parallel(jobs, BudgetGuard(2, 2, 4), tmp_path)
    assert [item.critic for item in outcomes] == ["claude", "agy", "codex"]
    assert maximum == 2


def test_one_critic_failure_does_not_cancel_valid_sibling(tmp_path):
    def fail(_provider, _request):
        raise MaoError("NETWORK_ERROR", "offline", {})

    outcomes = dispatch_parallel(
        [
            job(tmp_path, "claude", transport=FakeTransport(fail)),
            job(tmp_path, "agy"),
        ],
        BudgetGuard(2, 2, 4),
        tmp_path,
    )
    assert outcomes[0].status == "failed"
    assert outcomes[0].error["error"]["code"] == "NETWORK_ERROR"
    assert outcomes[1].status == "completed"
    assert outcomes[1].review is not None


def test_failure_after_reservation_remains_consumed(tmp_path):
    def fail(_provider, _request):
        raise RuntimeError("boom")

    guard = BudgetGuard(2, 2, 4)
    outcome = dispatch_parallel(
        [job(tmp_path, transport=FakeTransport(fail))], guard, tmp_path
    )[0]
    assert outcome.status == "failed"
    assert outcome.call_number == 1
    assert guard.total == 1


def test_success_marks_persistent_round_completed_and_prevents_repeat(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(project / ".multi-agent-orchestrator")
    state = store.create("run-1", "digest")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)
    transport = FakeTransport()
    critic_job = job(project, transport=transport)

    first = dispatch_parallel([critic_job], guard, project)[0]
    second = dispatch_parallel([critic_job], guard, project)[0]

    assert first.status == "completed"
    assert second.status == "failed"
    assert second.error["error"]["code"] == "REVIEW_BUDGET_EXHAUSTED"
    assert transport.calls == 1
    persisted = store.load("run-1")
    assert persisted.total_calls == 1
    assert persisted.critics["claude"].rounds["1"] == "completed"


@pytest.mark.parametrize("missing", ["packet", "schema"])
def test_missing_request_input_is_rejected_before_reservation(tmp_path, missing):
    guard = BudgetGuard(2, 2, 4)
    critic_job = job(tmp_path)
    target = critic_job.request.packet if missing == "packet" else critic_job.request.schema
    target.unlink()

    outcome = dispatch_parallel([critic_job], guard, tmp_path)[0]

    assert outcome.status == "failed"
    assert guard.total == 0
    assert critic_job.transport.calls == 0


def test_project_mutation_rejects_critic_result(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.py").write_text("VALUE = 1\n", encoding="utf-8")

    def mutate(_provider, _request):
        (project / "mutation.txt").write_text("changed", encoding="utf-8")

    outcome = dispatch_parallel(
        [job(project, transport=FakeTransport(mutate))],
        BudgetGuard(2, 2, 4),
        project,
    )[0]
    assert outcome.status == "failed"
    assert outcome.review is None
    assert outcome.error["error"]["code"] == "CRITIC_MUTATED_WORKSPACE"


def test_runtime_attempt_logs_do_not_trigger_workspace_mutation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    outcome = dispatch_parallel(
        [job(project)], BudgetGuard(2, 2, 4), project
    )[0]
    assert outcome.status == "completed"
    attempt = (
        project / ".multi-agent-orchestrator/runs/run-1/round-1/claude/attempt-1"
    )
    assert json.loads((attempt / "envelope.json").read_text())["model"] == "claude-model"
    assert json.loads((attempt / "review.json").read_text())["review_complete"] is True
    assert json.loads((attempt / "usage.json").read_text())["input_tokens"] == 3
    assert (attempt / "stdout.txt").exists()
    assert (attempt / "stderr.txt").exists()


def test_workspace_fingerprint_changes_for_authoritative_file_not_runtime(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.py"
    source.write_text("one", encoding="utf-8")
    first = capture_workspace_fingerprint(project)
    source.write_text("two", encoding="utf-8")
    second = capture_workspace_fingerprint(project)
    assert first != second
    runtime = project / ".multi-agent-orchestrator/runs/x"
    runtime.mkdir(parents=True)
    (runtime / "log.json").write_text("changed", encoding="utf-8")
    assert capture_workspace_fingerprint(project) == second


def test_workspace_fingerprint_tracks_directory_symlink_identity(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.mkdir()
    second_target.mkdir()
    link = project / "linked-directory"
    link.symlink_to(first_target, target_is_directory=True)
    first = capture_workspace_fingerprint(project)
    link.unlink()
    link.symlink_to(second_target, target_is_directory=True)
    assert capture_workspace_fingerprint(project) != first


def test_post_dispatch_fingerprint_failure_rejects_result(tmp_path, monkeypatch):
    import mao_core.review as review_module

    calls = 0

    def fingerprint(_project):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "before"
        raise MaoError("CRITIC_MUTATED_WORKSPACE", "unreadable", {})

    monkeypatch.setattr(review_module, "capture_workspace_fingerprint", fingerprint)
    outcome = dispatch_parallel(
        [job(tmp_path)], BudgetGuard(2, 2, 4), tmp_path
    )[0]
    assert outcome.status == "failed"
    assert outcome.error["error"]["code"] == "CRITIC_MUTATED_WORKSPACE"


def test_dispatch_rejects_job_identity_and_round_mismatch_without_reserve(tmp_path):
    invalid = job(tmp_path)
    invalid_request = InvocationRequest(
        provider="agy",
        model=invalid.request.model,
        packet=invalid.request.packet,
        schema=invalid.request.schema,
        cwd=invalid.request.cwd,
        timeout_seconds=invalid.request.timeout_seconds,
        run_id=invalid.request.run_id,
    )
    invalid = type(invalid)(
        critic="claude",
        round_number=3,
        provider=invalid.provider,
        transport=invalid.transport,
        request=invalid_request,
    )
    guard = BudgetGuard(2, 2, 4)
    outcome = dispatch_parallel([invalid], guard, tmp_path)[0]
    assert outcome.status == "failed"
    assert guard.total == 0


def test_attempt_envelope_does_not_serialize_provider_or_command_secrets(tmp_path):
    provider = FakeProvider("claude")
    provider.secret_token = "DO_NOT_LOG"
    outcome = dispatch_parallel(
        [job(tmp_path, provider=provider)], BudgetGuard(2, 2, 4), tmp_path
    )[0]
    assert outcome.status == "completed"
    attempt = (
        tmp_path / ".multi-agent-orchestrator/runs/run-1/round-1/claude/attempt-1"
    )
    combined = "".join(
        path.read_text(encoding="utf-8") for path in attempt.iterdir()
    )
    assert "DO_NOT_LOG" not in combined
    assert "dangerously" not in combined


def test_typed_error_details_are_redacted_in_attempt_log(tmp_path):
    def fail(_provider, _request):
        raise MaoError(
            "NETWORK_ERROR",
            "request failed token=DO_NOT_LOG",
            {"authorization": "Bearer DO_NOT_LOG", "token": "DO_NOT_LOG"},
        )

    outcome = dispatch_parallel(
        [job(tmp_path, transport=FakeTransport(fail))],
        BudgetGuard(2, 2, 4),
        tmp_path,
    )[0]
    serialized = json.dumps(outcome.error)
    assert "DO_NOT_LOG" not in serialized


def test_stdout_redacts_bearer_and_quoted_secret_keys(tmp_path):
    transport = FakeTransport()

    def invoke(provider, request):
        transport.calls += 1
        return ProcessResult(
            status="ok",
            exit_code=0,
            stdout=(
                '{"token": "DO_NOT_LOG", '
                '"authorization": "Bearer ALSO_DO_NOT_LOG"}\n'
                "Bearer THIRD_DO_NOT_LOG"
            ),
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )

    transport.invoke = invoke
    outcome = dispatch_parallel(
        [job(tmp_path, transport=transport)], BudgetGuard(2, 2, 4), tmp_path
    )[0]
    assert outcome.status == "completed"
    stdout = (
        tmp_path
        / ".multi-agent-orchestrator/runs/run-1/round-1/claude/attempt-1/stdout.txt"
    ).read_text(encoding="utf-8")
    assert "DO_NOT_LOG" not in stdout


def test_failure_writing_attempt_envelope_preserves_reserved_call_number(
    tmp_path, monkeypatch
):
    import mao_core.review as review_module

    monkeypatch.setattr(
        review_module,
        "_write_json",
        lambda _path, _value: (_ for _ in ()).throw(OSError("disk full")),
    )
    guard = BudgetGuard(2, 2, 4)
    outcome = dispatch_parallel([job(tmp_path)], guard, tmp_path)[0]

    assert outcome.status == "failed"
    assert outcome.call_number == 1
    assert guard.total == 1
