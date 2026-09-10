from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shlex
import time
from typing import Any

from mao_core.errors import MaoError
from mao_core.process import ProcessResult, run_process
from mao_core.providers.base import ProviderAdapter

from .base import InvocationRequest


_SETTLED_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "canceled", "stopped", "timed_out"}
)
_SUCCESS_STATES = frozenset({"succeeded"})
_UNATTENDED_FLAGS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "claude": "--dangerously-skip-permissions",
}
_CLEANUP_RESERVE_SECONDS = 0.25


class OrcaTransport:
    def __init__(self, executable: str = "orca", poll_interval_seconds: float = 0.1):
        self.executable = executable
        self.poll_interval_seconds = poll_interval_seconds

    def invoke(
        self,
        provider: ProviderAdapter,
        request: InvocationRequest,
    ) -> ProcessResult:
        _validate_request_provider(provider, request)
        started_at = time.monotonic()
        overall_deadline = started_at + request.timeout_seconds
        main_deadline = overall_deadline - _CLEANUP_RESERVE_SECONDS
        dispatch_id = None
        primary_result = None
        primary_error = None

        try:
            self._status(request, main_deadline, overall_deadline)
            self._validate_configuration(
                request.provider,
                request.cwd,
                main_deadline,
                overall_deadline,
                timeout_is_configuration_error=False,
            )

            run_payload = self._json_command(
                [
                    "orchestration",
                    "run-create",
                    "--objective",
                    f"Independent {request.provider} review for {request.run_id}",
                    "--json",
                ],
                request,
                main_deadline,
                overall_deadline,
            )
            run_id = _required_identifier(run_payload, "run_id", "runId")
            task_payload = self._json_command(
                [
                    "orchestration",
                    "task-create",
                    "--run",
                    run_id,
                    "--task-title",
                    f"{request.provider} critic",
                    "--spec",
                    _task_spec(request),
                    "--json",
                ],
                request,
                main_deadline,
                overall_deadline,
            )
            task_id = _required_identifier(task_payload, "task_id", "taskId")

            terminal_handle = None
            if request.provider == "antigravity":
                terminal_payload = self._json_command(
                    [
                        "terminal",
                        "create",
                        "--worktree",
                        "current",
                        "--command",
                        _antigravity_terminal_command(provider, request.model),
                        "--json",
                    ],
                    request,
                    main_deadline,
                    overall_deadline,
                )
                terminal_handle = _required_identifier(
                    terminal_payload, "handle", "terminal_handle", "terminalHandle"
                )

            start_args = [
                "orchestration",
                "worker-start",
                "--task",
                task_id,
            ]
            if terminal_handle is None:
                start_args.extend(
                    ["--agent", request.provider, "--model", request.model]
                )
            else:
                start_args.extend(["--terminal", terminal_handle])
            worker_timeout_ms = _remaining_timeout_ms(main_deadline)
            start_args.extend(
                [
                    "--worktree",
                    "current",
                    "--timeout-ms",
                    str(worker_timeout_ms),
                    "--run",
                    run_id,
                    "--json",
                ]
            )
            start_payload = self._json_command(
                start_args,
                request,
                main_deadline,
                overall_deadline,
            )
            dispatch_id = _required_identifier(
                start_payload, "dispatch_id", "dispatchId"
            )

            state = self._wait_for_settled(
                dispatch_id,
                request,
                main_deadline,
                overall_deadline,
            )
            if state is None:
                primary_result = _timeout_result(
                    started_at, "Orca worker did not settle before timeout"
                )
            else:
                read_payload = self._json_command(
                    [
                        "orchestration",
                        "worker-read",
                        "--dispatch",
                        dispatch_id,
                        "--source",
                        "auto",
                        "--limit",
                        "5000",
                        "--json",
                    ],
                    request,
                    main_deadline,
                    overall_deadline,
                )
                output = _worker_output(read_payload)
                succeeded = state in _SUCCESS_STATES
                timed_out = state == "timed_out"
                primary_result = ProcessResult(
                    status=(
                        "timeout" if timed_out else ("ok" if succeeded else "error")
                    ),
                    exit_code=None if timed_out else (0 if succeeded else 1),
                    stdout=output,
                    stderr="" if succeeded else f"Orca worker settled as {state}",
                    duration_seconds=time.monotonic() - started_at,
                    timed_out=timed_out,
                )
        except MaoError as error:
            if error.code == "TIMEOUT":
                primary_result = _timeout_result(started_at, error.message)
            else:
                primary_error = error
        except BaseException:
            if dispatch_id is not None:
                self._release(dispatch_id, request, overall_deadline)
            raise

        cleanup_error = None
        if dispatch_id is not None:
            cleanup_error = self._release(
                dispatch_id,
                request,
                overall_deadline,
            )

        if primary_error is not None:
            if cleanup_error is not None:
                raise _attach_cleanup_to_error(primary_error, cleanup_error)
            raise primary_error
        if primary_result is None:
            raise MaoError(
                "INVALID_RESULT",
                "Orca invocation produced no primary result",
                {},
            )
        if cleanup_error is not None:
            if primary_result.status == "ok":
                raise cleanup_error
            return _attach_cleanup_to_result(primary_result, cleanup_error)
        return primary_result

    def _status(
        self,
        request: InvocationRequest,
        deadline: float,
        overall_deadline: float,
    ) -> None:
        result = self._run_before_deadline(
            [self.executable, "status", "--json"],
            request.cwd,
            deadline,
            "Orca status deadline expired",
            overall_deadline,
        )
        if result.timed_out:
            raise MaoError(
                "TIMEOUT",
                "Orca status command timed out",
                {"provider": request.provider},
            )
        if result.exit_code != 0:
            raise MaoError(
                "SESSION_START_FAILED",
                "Orca runtime status check failed",
                {"provider": request.provider, "exit_code": result.exit_code},
            )
        _load_json_object(result, "Orca status returned invalid JSON")

    def validate_configuration(
        self,
        provider: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> None:
        if timeout_seconds < 1:
            raise MaoError(
                "CONFIG_INVALID",
                "Orca preflight timeout must be positive",
                {"timeout_seconds": timeout_seconds},
            )
        deadline = time.monotonic() + timeout_seconds
        self._validate_configuration(
            provider,
            cwd,
            deadline,
            deadline,
            timeout_is_configuration_error=True,
        )

    def _validate_configuration(
        self,
        provider: str,
        cwd: Path,
        deadline: float,
        overall_deadline: float,
        timeout_is_configuration_error: bool,
    ) -> None:
        if provider == "antigravity":
            return
        if provider not in _UNATTENDED_FLAGS:
            raise MaoError(
                "CONFIG_INVALID",
                "Unsupported Orca provider",
                {"provider": provider},
            )
        required_flag = _UNATTENDED_FLAGS[provider]
        try:
            result = self._run_before_deadline(
                [self.executable, "orchestration", "worker-start", "--help"],
                cwd,
                deadline,
                "Orca unattended preflight deadline expired",
                overall_deadline,
            )
        except MaoError as error:
            if error.code != "TIMEOUT" or not timeout_is_configuration_error:
                raise
            result = None
        if (
            result is None
            or result.timed_out
            or result.exit_code != 0
            or required_flag not in result.stdout
        ):
            if result is not None and result.timed_out and not timeout_is_configuration_error:
                raise MaoError(
                    "TIMEOUT",
                    "Orca unattended preflight timed out",
                    {"provider": provider},
                )
            raise MaoError(
                "CONFIG_INVALID",
                "Orca managed launch cannot prove unattended permission behavior",
                {
                    "provider": provider,
                    "required_flag": required_flag,
                    "reason": "installed worker-start help does not guarantee the bypass flag",
                },
            )

    def _wait_for_settled(
        self,
        dispatch_id: str,
        request: InvocationRequest,
        deadline: float,
        overall_deadline: float,
    ) -> str | None:
        while True:
            if time.monotonic() >= deadline:
                return None
            payload = self._json_command(
                [
                    "orchestration",
                    "worker-show",
                    "--dispatch",
                    dispatch_id,
                    "--json",
                ],
                request,
                deadline,
                overall_deadline,
            )
            agent_wait = _find_value(payload, "agentWait", "agent_wait")
            if agent_wait is not None:
                raise MaoError(
                    "SESSION_START_FAILED",
                    "Orca worker requires interactive permission input",
                    {"provider": request.provider, "dispatch_id": dispatch_id},
                )
            state = _find_value(payload, "state", "status")
            if isinstance(state, str) and state.lower() in _SETTLED_STATES:
                return state.lower()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _json_command(
        self,
        args: list[str],
        request: InvocationRequest,
        deadline: float,
        overall_deadline: float,
    ) -> dict[str, Any]:
        result = self._run_before_deadline(
            [self.executable, *args],
            request.cwd,
            deadline,
            "Orca lifecycle deadline expired",
            overall_deadline,
        )
        if result.timed_out:
            raise MaoError(
                "TIMEOUT",
                "Orca lifecycle command timed out",
                {"command": " ".join(args[:2])},
            )
        if result.exit_code != 0:
            raise MaoError(
                "SESSION_START_FAILED",
                "Orca lifecycle command failed",
                {"command": " ".join(args[:2]), "exit_code": result.exit_code},
            )
        return _load_json_object(result, "Orca lifecycle returned invalid JSON")

    def _run_before_deadline(
        self,
        args: list[str],
        cwd: Path,
        deadline: float,
        timeout_message: str,
        overall_deadline: float,
    ) -> ProcessResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MaoError("TIMEOUT", timeout_message, {})
        return run_process(
            args,
            cwd,
            remaining,
            absolute_deadline=overall_deadline,
        )

    def _release(
        self,
        dispatch_id: str,
        request: InvocationRequest,
        overall_deadline: float,
    ) -> MaoError | None:
        try:
            self._json_command(
                [
                    "orchestration",
                    "worker-release",
                    "--dispatch",
                    dispatch_id,
                    "--json",
                ],
                request,
                overall_deadline,
                overall_deadline,
            )
        except MaoError as error:
            return error
        return None


def _validate_request_provider(
    provider: ProviderAdapter, request: InvocationRequest
) -> None:
    name = getattr(provider, "name", None)
    if name != request.provider or name not in {"codex", "claude", "antigravity"}:
        raise MaoError(
            "CONFIG_INVALID",
            "Transport provider does not match invocation request",
            {"adapter_provider": name, "request_provider": request.provider},
        )
    if request.timeout_seconds < 1:
        raise MaoError(
            "CONFIG_INVALID",
            "Transport timeout must be positive",
            {"timeout_seconds": request.timeout_seconds},
        )


def _timeout_result(started_at: float, message: str) -> ProcessResult:
    return ProcessResult(
        status="timeout",
        exit_code=None,
        stdout="",
        stderr=message,
        duration_seconds=time.monotonic() - started_at,
        timed_out=True,
    )


def _remaining_timeout_ms(deadline: float) -> int:
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms < 1:
        raise MaoError("TIMEOUT", "Orca worker-start deadline expired", {})
    return remaining_ms


def _cleanup_summary(error: MaoError) -> str:
    return f"Cleanup failed ({error.code}): {error.message}"


def _attach_cleanup_to_result(
    result: ProcessResult, cleanup_error: MaoError
) -> ProcessResult:
    summary = _cleanup_summary(cleanup_error)
    stderr = f"{result.stderr}\n{summary}" if result.stderr else summary
    return replace(result, stderr=stderr)


def _attach_cleanup_to_error(
    primary_error: MaoError, cleanup_error: MaoError
) -> MaoError:
    details = dict(primary_error.details)
    details["cleanup_failure"] = {
        "code": cleanup_error.code,
        "message": cleanup_error.message,
    }
    return MaoError(primary_error.code, primary_error.message, details)


def _task_spec(request: InvocationRequest) -> str:
    return (
        f"Review packet at {request.packet}. Return JSON only matching schema at "
        f"{request.schema}. Do not modify the worktree."
    )


def _antigravity_terminal_command(provider: ProviderAdapter, model: str) -> str:
    executable = str(getattr(provider, "executable", "agy"))
    if Path(executable).name != "agy" or _contains_control(executable, model):
        raise MaoError(
            "CONFIG_INVALID",
            "Antigravity terminal command contains a non-allowlisted argument",
            {"provider": "antigravity"},
        )
    return shlex.join(
        [executable, "--model", model, "--dangerously-skip-permissions"]
    )


def _contains_control(*values: str) -> bool:
    return any("\x00" in value or "\n" in value or "\r" in value for value in values)


def _load_json_object(result: ProcessResult, message: str) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MaoError("INVALID_RESULT", message, {}) from error
    if not isinstance(payload, dict):
        raise MaoError("INVALID_RESULT", message, {})
    return payload


def _find_value(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate is not None:
                return candidate
        for candidate in value.values():
            found = _find_value(candidate, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_value(candidate, *keys)
            if found is not None:
                return found
    return None


def _required_identifier(payload: dict[str, Any], *keys: str) -> str:
    value = _find_value(payload, *keys)
    if not isinstance(value, str) or not value:
        raise MaoError(
            "INVALID_RESULT",
            "Orca lifecycle response omitted required identifier",
            {"expected": list(keys)},
        )
    return value


def _worker_output(payload: dict[str, Any]) -> str:
    value = _find_value(payload, "output", "text", "content", "transcript")
    if isinstance(value, str):
        return value
    rows = _find_value(payload, "rows")
    if isinstance(rows, list):
        parts = []
        for row in rows:
            if isinstance(row, str):
                parts.append(row)
            elif isinstance(row, dict):
                text = _find_value(row, "text", "content", "output")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    raise MaoError(
        "INVALID_RESULT",
        "Orca worker-read returned no output",
        {},
    )
