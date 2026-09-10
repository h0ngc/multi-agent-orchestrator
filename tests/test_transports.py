from __future__ import annotations

from dataclasses import fields, replace
import json
import os
from pathlib import Path
import shlex
import time

import pytest

import mao_core.transports.orca as orca_module
import mao_core.transports.tmux as tmux_module
from mao_core.errors import MaoError
from mao_core.process import ProcessResult
from mao_core.providers.codex import CodexAdapter
from mao_core.transports.base import InvocationRequest
from mao_core.transports.direct import DirectTransport
from mao_core.transports.orca import OrcaTransport
from mao_core.transports.tmux import TmuxTransport


FAKES = Path(__file__).parent / "fakes"


def request_for(tmp_path: Path) -> InvocationRequest:
    packet = tmp_path / "packet.md"
    packet.write_text("Review this minimal packet.", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    return InvocationRequest(
        provider="codex",
        model="test-model",
        packet=packet,
        schema=schema,
        cwd=tmp_path,
        timeout_seconds=2,
        run_id="run-1",
        effort="high",
    )


class FakeProvider:
    name = "codex"

    def __init__(self) -> None:
        self.invocations = 0

    def invoke(
        self,
        model: str,
        packet: Path,
        schema: Path,
        cwd: Path,
        effort: str,
    ) -> ProcessResult:
        self.invocations += 1
        assert effort == "high"
        return ProcessResult(
            status="ok",
            exit_code=0,
            stdout="{}",
            stderr="",
            duration_seconds=0,
            timed_out=False,
        )


class FakeOrcaHarness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = str(FAKES / "orca")
        self.log = tmp_path / "orca.log"
        self.log.touch()
        monkeypatch.setenv("MAO_FAKE_ORCA_LOG", str(self.log))
        monkeypatch.setenv("MAO_FAKE_FIXTURES", str(Path(__file__).parent / "fixtures"))
        self.provider = FakeProvider()
        self.agy_provider = FakeProvider()
        self.agy_provider.name = "antigravity"
        self.agy_provider.executable = "agy"

    @property
    def rows(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    @property
    def commands(self) -> list[str]:
        return [row["command"] for row in self.rows]

    def args_for(self, command: str) -> list[str]:
        return [row["args"] for row in self.rows if row["command"] == command][-1]


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def invocation_request(tmp_path: Path) -> InvocationRequest:
    return request_for(tmp_path)


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeOrcaHarness:
    return FakeOrcaHarness(tmp_path, monkeypatch)


@pytest.fixture
def fake_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    log = tmp_path / "tmux.log"
    log.touch()
    state = tmp_path / "tmux-state"
    state.mkdir()
    monkeypatch.setenv("MAO_FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("MAO_FAKE_TMUX_STATE", str(state))
    return str(FAKES / "tmux"), log


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_invocation_request_has_exact_frozen_contract(invocation_request):
    assert [field.name for field in fields(invocation_request)] == [
        "provider",
        "model",
        "packet",
        "schema",
        "cwd",
        "timeout_seconds",
        "run_id",
        "effort",
    ]
    with pytest.raises(AttributeError):
        invocation_request.run_id = "changed"


def test_direct_calls_provider_once(provider, invocation_request):
    result = DirectTransport().invoke(provider, invocation_request)

    assert result.status == "ok"
    assert provider.invocations == 1


def test_orca_uses_run_task_worker_lifecycle(fake_orca, invocation_request):
    result = OrcaTransport(fake_orca.path).invoke(
        fake_orca.provider, invocation_request
    )

    assert result.status == "ok"
    assert json.loads(result.stdout)["review_complete"] is True
    assert fake_orca.commands == [
        "status",
        "orchestration run-create",
        "orchestration task-create",
        "orchestration worker-start",
        "orchestration worker-show",
        "orchestration worker-read",
        "orchestration worker-release",
    ]
    assert fake_orca.args_for("status") == ["status", "--json"]
    start_args = fake_orca.args_for("orchestration worker-start")
    timeout_index = start_args.index("--timeout-ms") + 1
    timeout_ms = int(start_args[timeout_index])
    start_args[timeout_index] = "<remaining-ms>"
    assert start_args == [
        "orchestration",
        "worker-start",
        "--task",
        "task-1",
        "--agent",
        "codex",
        "--model",
        "test-model",
        "--effort",
        "high",
        "--worktree",
        "current",
        "--timeout-ms",
        "<remaining-ms>",
        "--run",
        "orca-run-1",
        "--json",
    ]
    assert 0 < timeout_ms <= 1750


def test_orca_antigravity_adopts_exact_yolo_terminal(fake_orca, invocation_request):
    invocation_request = replace(invocation_request, provider="antigravity")

    OrcaTransport(fake_orca.path).invoke(fake_orca.agy_provider, invocation_request)

    create_args = fake_orca.args_for("terminal create")
    assert create_args == [
        "terminal",
        "create",
        "--worktree",
        "current",
        "--command",
        shlex.join([
            "agy", "--model", "test-model", "--effort", "high",
            "--dangerously-skip-permissions",
        ]),
        "--json",
    ]
    start_args = fake_orca.args_for("orchestration worker-start")
    assert "--terminal" in start_args
    assert start_args[start_args.index("--terminal") + 1] == "terminal-1"
    assert "--agent" not in start_args


def test_orca_status_failure_maps_to_session_start_failed(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_STATUS_FAILURE", "1")

    with pytest.raises(MaoError) as error:
        OrcaTransport(fake_orca.path).invoke(fake_orca.provider, invocation_request)

    assert error.value.code == "SESSION_START_FAILED"
    assert fake_orca.commands == ["status"]


def test_orca_rejects_unproved_managed_agent_launch(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_PROVE_UNATTENDED", "0")

    with pytest.raises(MaoError) as error:
        OrcaTransport(fake_orca.path).invoke(fake_orca.provider, invocation_request)

    assert error.value.code == "CONFIG_INVALID"
    assert error.value.details["provider"] == "codex"
    assert "unattended" in error.value.message.lower()
    assert fake_orca.commands == ["status"]


def test_orca_configuration_preflight_rejects_unproved_managed_agent(
    fake_orca, tmp_path, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_PROVE_UNATTENDED", "0")

    with pytest.raises(MaoError) as error:
        OrcaTransport(fake_orca.path).validate_configuration(
            "claude", tmp_path, timeout_seconds=2
        )

    assert error.value.code == "CONFIG_INVALID"
    assert error.value.details == {
        "provider": "claude",
        "required_flag": "--dangerously-skip-permissions",
        "reason": "installed worker-start help does not guarantee the bypass flag",
    }
    assert fake_orca.commands == []


def test_orca_agent_wait_is_failure_and_releases_worker(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_AGENT_WAIT", "1")

    with pytest.raises(MaoError) as error:
        OrcaTransport(fake_orca.path).invoke(fake_orca.provider, invocation_request)

    assert error.value.code == "SESSION_START_FAILED"
    assert fake_orca.commands[-1] == "orchestration worker-release"
    assert fake_orca.commands.count("orchestration worker-release") == 1


def test_orca_settled_timed_out_maps_to_timeout_process_result(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_WORKER_STATE", "timed_out")

    result = OrcaTransport(fake_orca.path).invoke(
        fake_orca.provider, invocation_request
    )

    assert result.status == "timeout"
    assert result.exit_code is None
    assert result.timed_out is True
    assert fake_orca.commands[-2:] == [
        "orchestration worker-read",
        "orchestration worker-release",
    ]


def test_orca_uses_one_overall_deadline_across_delayed_commands(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_DELAY_SECONDS", "0.35")
    invocation_request = replace(invocation_request, timeout_seconds=1)
    started_at = time.monotonic()

    result = OrcaTransport(fake_orca.path, poll_interval_seconds=0.01).invoke(
        fake_orca.provider, invocation_request
    )

    elapsed = time.monotonic() - started_at
    assert result.status == "timeout"
    assert result.timed_out is True
    assert elapsed < 1.5
    assert "orchestration worker-start" not in fake_orca.commands


def test_orca_worker_timeout_uses_remaining_budget_after_setup(
    fake_orca, invocation_request, monkeypatch
):
    process_calls = []
    real_run_process = orca_module.run_process

    def recording_run_process(
        args, cwd, timeout_seconds, stdin=None, *, absolute_deadline=None
    ):
        process_calls.append((list(args), timeout_seconds, absolute_deadline))
        if absolute_deadline is None:
            return real_run_process(args, cwd, timeout_seconds, stdin)
        return real_run_process(
            args,
            cwd,
            timeout_seconds,
            stdin,
            absolute_deadline=absolute_deadline,
        )

    monkeypatch.setattr(orca_module, "run_process", recording_run_process)
    monkeypatch.setenv(
        "MAO_FAKE_ORCA_DELAYS_JSON",
        json.dumps(
            {
                "status": 0.1,
                "preflight": 0.1,
                "orchestration run-create": 0.1,
                "orchestration task-create": 0.1,
                "orchestration worker-start": 0.1,
                "orchestration worker-show": 0.1,
                "orchestration worker-read": 0.1,
                "orchestration worker-release": 0.1,
            }
        ),
    )

    result = OrcaTransport(fake_orca.path, poll_interval_seconds=0.01).invoke(
        fake_orca.provider, invocation_request
    )

    timeout_ms = int(
        fake_orca.args_for("orchestration worker-start")[
            fake_orca.args_for("orchestration worker-start").index("--timeout-ms") + 1
        ]
    )
    assert result.status == "ok"
    assert 0 < timeout_ms < 1600
    assert fake_orca.commands[-4:] == [
        "orchestration worker-start",
        "orchestration worker-show",
        "orchestration worker-read",
        "orchestration worker-release",
    ]
    absolute_deadlines = [call[2] for call in process_calls]
    assert all(deadline is not None for deadline in absolute_deadlines)
    assert len(set(absolute_deadlines)) == 1
    main_timeouts = [call[1] for call in process_calls[:-1]]
    assert all(later < earlier for earlier, later in zip(main_timeouts, main_timeouts[1:]))


def test_orca_release_failure_after_success_surfaces_typed_failure(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_RELEASE_FAILURE", "1")

    with pytest.raises(MaoError) as error:
        OrcaTransport(fake_orca.path).invoke(
            fake_orca.provider, invocation_request
        )

    assert error.value.code == "SESSION_START_FAILED"
    assert error.value.details["command"] == "orchestration worker-release"
    assert fake_orca.commands[-2:] == [
        "orchestration worker-read",
        "orchestration worker-release",
    ]


def test_orca_primary_timeout_attaches_cleanup_failure_within_overall_deadline(
    fake_orca, invocation_request, monkeypatch
):
    monkeypatch.setenv("MAO_FAKE_ORCA_WORKER_STATE", "running")
    monkeypatch.setenv(
        "MAO_FAKE_ORCA_DELAYS_JSON",
        json.dumps(
            {
                "orchestration worker-show": 2,
                "orchestration worker-release": 2,
            }
        ),
    )
    invocation_request = replace(invocation_request, timeout_seconds=1)
    started_at = time.monotonic()

    result = OrcaTransport(fake_orca.path, poll_interval_seconds=0.01).invoke(
        fake_orca.provider, invocation_request
    )

    elapsed = time.monotonic() - started_at
    assert result.status == "timeout"
    assert result.timed_out is True
    assert "cleanup failed" in result.stderr.lower()
    assert elapsed < 1.15
    assert fake_orca.commands[-2:] == [
        "orchestration worker-show",
        "orchestration worker-release",
    ]


def test_tmux_missing_returns_cli_not_found(tmp_path):
    with pytest.raises(MaoError) as error:
        TmuxTransport("/missing/tmux").invoke(FakeProvider(), request_for(tmp_path))

    assert error.value.code == "CLI_NOT_FOUND"


def test_tmux_runs_exact_named_one_shot_and_returns_process_result(
    fake_tmux, fake_paths, invocation_request
):
    executable, log = fake_tmux
    adapter = CodexAdapter(executable=fake_paths.codex)

    result = TmuxTransport(executable, poll_interval_seconds=0.01).invoke(
        adapter, invocation_request
    )

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert json.loads(result.stdout.splitlines()[1])["item"]["type"] == "agent_message"
    rows = _rows(log)
    assert rows[0]["args"] == ["has-session", "-t", "mao-run-1-codex"]
    assert rows[1]["args"][:5] == [
        "new-session",
        "-d",
        "-s",
        "mao-run-1-codex",
        shlex.join(
            [
                str(
                    invocation_request.packet.parent
                    / ".mao-run-1-codex.launcher.sh"
                )
            ]
        ),
    ]
    assert rows[2]["args"] == ["has-session", "-t", "mao-run-1-codex"]
    launcher = invocation_request.packet.parent / ".mao-run-1-codex.launcher.sh"
    assert launcher.stat().st_mode & 0o777 == 0o700


def test_tmux_shlex_quotes_model_as_one_argument_without_shell_execution(
    fake_tmux, fake_paths, invocation_request
):
    executable, _ = fake_tmux
    marker = invocation_request.cwd / "injected"
    unsafe_looking_model = f"model; touch {marker}"
    invocation_request = replace(invocation_request, model=unsafe_looking_model)
    adapter = CodexAdapter(executable=fake_paths.codex)

    TmuxTransport(executable, poll_interval_seconds=0.01).invoke(
        adapter, invocation_request
    )

    assert not marker.exists()
    assert fake_paths.last_args("codex")[2] == unsafe_looking_model


def test_tmux_quotes_launcher_for_tmux_shell_parsing(
    fake_tmux, fake_paths, tmp_path
):
    executable, _ = fake_tmux
    packet_directory = tmp_path / "packet dir; touch injected; #"
    packet_directory.mkdir()
    invocation_request = request_for(packet_directory)
    adapter = CodexAdapter(executable=fake_paths.codex)

    result = TmuxTransport(executable, poll_interval_seconds=0.01).invoke(
        adapter, invocation_request
    )

    assert result.status == "ok"
    assert not (packet_directory / "injected").exists()


def test_tmux_rejects_non_allowlisted_provider_executable_before_start(
    fake_tmux, invocation_request
):
    executable, log = fake_tmux
    provider = FakeProvider()
    provider.executable = "/bin/sh"

    with pytest.raises(MaoError) as error:
        TmuxTransport(executable).invoke(provider, invocation_request)

    assert error.value.code == "CONFIG_INVALID"
    assert _rows(log) == []


def test_tmux_rejects_line_break_in_model_before_start(
    fake_tmux, invocation_request
):
    executable, log = fake_tmux
    provider = FakeProvider()
    provider.executable = "codex"
    invocation_request = replace(invocation_request, model="model\nfragment")

    with pytest.raises(MaoError) as error:
        TmuxTransport(executable).invoke(provider, invocation_request)

    assert error.value.code == "CONFIG_INVALID"
    assert _rows(log) == []


def test_tmux_rejects_preexisting_exact_session_without_killing_it(
    fake_tmux, invocation_request, monkeypatch
):
    executable, log = fake_tmux
    monkeypatch.setenv("MAO_FAKE_TMUX_PREEXISTING", "mao-run-1-codex")
    provider = FakeProvider()
    provider.executable = "codex"

    with pytest.raises(MaoError) as error:
        TmuxTransport(executable).invoke(provider, invocation_request)

    assert error.value.code == "SESSION_START_FAILED"
    assert _rows(log) == [{"args": ["has-session", "-t", "mao-run-1-codex"]}]


def test_tmux_timeout_polling_is_bounded_and_kills_only_generated_session(
    fake_tmux, invocation_request, monkeypatch
):
    executable, log = fake_tmux
    monkeypatch.setenv("MAO_FAKE_TMUX_STICKY", "1")
    provider = FakeProvider()
    provider.executable = "codex"
    invocation_request = replace(invocation_request, timeout_seconds=1)

    result = TmuxTransport(executable, poll_interval_seconds=0.01).invoke(
        provider, invocation_request
    )

    assert result.status == "timeout"
    assert result.timed_out is True
    rows = _rows(log)
    assert rows[-1] == {"args": ["kill-session", "-t", "mao-run-1-codex"]}
    assert all(
        row["args"][-1] == "mao-run-1-codex"
        for row in rows
        if row["args"][0] in {"has-session", "kill-session"}
    )


def test_tmux_uses_one_overall_deadline_across_delayed_commands(
    fake_tmux, invocation_request, monkeypatch
):
    executable, log = fake_tmux
    process_calls = []
    real_run_process = tmux_module.run_process

    def recording_run_process(
        args, cwd, timeout_seconds, stdin=None, *, absolute_deadline=None
    ):
        process_calls.append((list(args), timeout_seconds, absolute_deadline))
        if absolute_deadline is None:
            return real_run_process(args, cwd, timeout_seconds, stdin)
        return real_run_process(
            args,
            cwd,
            timeout_seconds,
            stdin,
            absolute_deadline=absolute_deadline,
        )

    monkeypatch.setattr(tmux_module, "run_process", recording_run_process)
    monkeypatch.setenv(
        "MAO_FAKE_TMUX_DELAYS_JSON",
        json.dumps({"new-session": 2.0}),
    )
    provider = FakeProvider()
    provider.executable = "codex"
    invocation_request = replace(invocation_request, timeout_seconds=1)
    started_at = time.monotonic()

    result = TmuxTransport(executable, poll_interval_seconds=0.01).invoke(
        provider, invocation_request
    )

    elapsed = time.monotonic() - started_at
    assert result.status == "timeout"
    assert result.timed_out is True
    assert elapsed < 1.75
    assert [row["args"][0] for row in _rows(log)] == [
        "has-session",
        "new-session",
        "kill-session",
    ]
    absolute_deadlines = [call[2] for call in process_calls]
    assert all(deadline is not None for deadline in absolute_deadlines)
    assert len(set(absolute_deadlines)) == 1
    assert process_calls[1][1] < process_calls[0][1]
    assert process_calls[2][1] < process_calls[1][1]


def test_transport_fakes_are_portable_executables():
    for name in ("orca", "tmux"):
        path = FAKES / name
        assert path.read_text(encoding="utf-8").splitlines()[0] == (
            "#!/usr/bin/env python3"
        )
        assert os.access(path, os.X_OK)
