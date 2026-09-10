from __future__ import annotations

from pathlib import Path
import re
import shlex
import time

from mao_core.errors import MaoError
from mao_core.process import ProcessResult, run_process
from mao_core.providers.base import ProviderAdapter

from .base import InvocationRequest


_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_EXECUTABLE_NAMES = {
    "codex": "codex",
    "claude": "claude",
    "antigravity": "agy",
}
_CLEANUP_RESERVE_SECONDS = 0.25


class TmuxTransport:
    def __init__(self, executable: str = "tmux", poll_interval_seconds: float = 0.1):
        self.executable = executable
        self.poll_interval_seconds = poll_interval_seconds

    def invoke(
        self,
        provider: ProviderAdapter,
        request: InvocationRequest,
    ) -> ProcessResult:
        started_at = time.monotonic()
        command = _provider_command(provider, request)
        session_name = _session_name(request)
        paths = _artifact_paths(request.packet.parent, session_name)
        overall_deadline = started_at + request.timeout_seconds
        main_deadline = overall_deadline - _CLEANUP_RESERVE_SECONDS

        existing = _run_before_deadline(
            [self.executable, "has-session", "-t", session_name],
            request.cwd,
            main_deadline,
            overall_deadline,
        )
        if existing is None or existing.timed_out:
            return _timeout_result(paths, started_at, "tmux preflight timed out")
        if existing.exit_code == 0:
            raise MaoError(
                "SESSION_START_FAILED",
                "Refusing to replace a pre-existing tmux session",
                {"session": session_name},
            )
        if existing.exit_code not in {1}:
            raise MaoError(
                "SESSION_START_FAILED",
                "tmux session collision check failed",
                {"session": session_name, "exit_code": existing.exit_code},
            )

        _write_launcher(paths, command)
        started = _run_before_deadline(
            [
                self.executable,
                "new-session",
                "-d",
                "-s",
                session_name,
                shlex.join([str(paths["launcher"])]),
            ],
            request.cwd,
            main_deadline,
            overall_deadline,
        )
        if started is None:
            return _timeout_result(paths, started_at, "tmux start deadline expired")
        if started.timed_out:
            self._kill_session_best_effort(
                session_name, request, overall_deadline
            )
            return _timeout_result(paths, started_at, "tmux start timed out")
        if started.exit_code != 0:
            raise MaoError(
                "SESSION_START_FAILED",
                "tmux session could not be started",
                {"session": session_name, "exit_code": started.exit_code},
            )

        while True:
            active = _run_before_deadline(
                [self.executable, "has-session", "-t", session_name],
                request.cwd,
                main_deadline,
                overall_deadline,
            )
            if active is None or active.timed_out:
                self._kill_session_best_effort(
                    session_name, request, overall_deadline
                )
                return _timeout_result(paths, started_at, "tmux polling timed out")
            if active.exit_code == 1:
                return _read_result(paths, started_at)
            if active.exit_code != 0:
                self._kill_session_best_effort(
                    session_name, request, overall_deadline
                )
                raise MaoError(
                    "SESSION_START_FAILED",
                    "tmux session status check failed",
                    {"session": session_name, "exit_code": active.exit_code},
                )
            remaining = main_deadline - time.monotonic()
            if remaining <= 0:
                self._kill_session_best_effort(
                    session_name, request, overall_deadline
                )
                return _timeout_result(paths, started_at, "tmux polling deadline expired")
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _kill_session_best_effort(
        self,
        session_name: str,
        request: InvocationRequest,
        overall_deadline: float,
    ) -> None:
        try:
            _run_before_deadline(
                [self.executable, "kill-session", "-t", session_name],
                request.cwd,
                overall_deadline,
                overall_deadline,
            )
        except MaoError:
            pass


def _session_name(request: InvocationRequest) -> str:
    if request.timeout_seconds < 1:
        raise MaoError(
            "CONFIG_INVALID",
            "Transport timeout must be positive",
            {"timeout_seconds": request.timeout_seconds},
        )
    if not _NAME_PATTERN.fullmatch(request.run_id):
        raise MaoError(
            "CONFIG_INVALID",
            "Run id cannot form a safe tmux session name",
            {"run_id": request.run_id},
        )
    if request.provider not in _EXECUTABLE_NAMES:
        raise MaoError(
            "CONFIG_INVALID",
            "Unsupported tmux provider",
            {"provider": request.provider},
        )
    return f"mao-{request.run_id}-{request.provider}"


def _provider_command(
    provider: ProviderAdapter, request: InvocationRequest
) -> list[str]:
    provider_name = getattr(provider, "name", None)
    if provider_name != request.provider or provider_name not in _EXECUTABLE_NAMES:
        raise MaoError(
            "CONFIG_INVALID",
            "Transport provider does not match invocation request",
            {"adapter_provider": provider_name, "request_provider": request.provider},
        )
    executable = str(getattr(provider, "executable", _EXECUTABLE_NAMES[provider_name]))
    if Path(executable).name != _EXECUTABLE_NAMES[provider_name]:
        raise MaoError(
            "CONFIG_INVALID",
            "Provider executable is not allowlisted for tmux",
            {"provider": provider_name, "executable": executable},
        )

    packet_text = _read_utf8(request.packet, "packet")
    schema_text = _read_utf8(request.schema, "schema")
    _validate_scalar_arguments(
        executable,
        request.model,
        request.effort,
        str(request.packet),
        str(request.schema),
        str(request.cwd),
    )
    _validate_payload_arguments(packet_text, schema_text)
    if provider_name == "codex":
        return [
            executable,
            "exec",
            "--model",
            request.model,
            "-c",
            f'model_reasoning_effort="{request.effort}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--skip-git-repo-check",
            "--output-schema",
            str(request.schema),
            packet_text,
        ]
    if provider_name == "claude":
        return [
            executable,
            "-p",
            packet_text,
            "--model",
            request.model,
            "--effort",
            request.effort,
            "--output-format",
            "json",
            "--json-schema",
            schema_text,
            "--dangerously-skip-permissions",
        ]
    return [
        executable,
        "--print",
        packet_text,
        "--model",
        request.model,
        "--effort",
        request.effort,
        "--output-format",
        "json",
        "--json-schema",
        str(request.schema),
        "--dangerously-skip-permissions",
    ]


def _read_utf8(path: Path, kind: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MaoError(
            "PACKET_PATH_INVALID",
            f"Review {kind} could not be read",
            {"path": str(path)},
        ) from error


def _validate_scalar_arguments(*arguments: str) -> None:
    for argument in arguments:
        if any(character in argument for character in ("\x00", "\n", "\r")):
            raise MaoError(
                "CONFIG_INVALID",
                "Provider command scalar argument contains a control character",
                {},
            )


def _validate_payload_arguments(*arguments: str) -> None:
    if any("\x00" in argument for argument in arguments):
        raise MaoError(
            "CONFIG_INVALID",
            "Provider command payload contains NUL",
            {},
        )


def _artifact_paths(directory: Path, session_name: str) -> dict[str, Path]:
    prefix = directory / f".{session_name}"
    return {
        "launcher": Path(f"{prefix}.launcher.sh"),
        "stdout": Path(f"{prefix}.stdout"),
        "stderr": Path(f"{prefix}.stderr"),
        "exit": Path(f"{prefix}.exit"),
    }


def _write_launcher(paths: dict[str, Path], command: list[str]) -> None:
    launcher = "\n".join(
        [
            "#!/bin/sh",
            f"{shlex.join(command)} > {shlex.quote(str(paths['stdout']))} "
            f"2> {shlex.quote(str(paths['stderr']))}",
            "mao_exit_code=$?",
            f"printf '%s\\n' \"$mao_exit_code\" > {shlex.quote(str(paths['exit']))}",
            "exit \"$mao_exit_code\"",
            "",
        ]
    )
    paths["launcher"].write_text(launcher, encoding="utf-8")
    paths["launcher"].chmod(0o700)


def _run_before_deadline(
    args: list[str],
    cwd: Path,
    deadline: float,
    overall_deadline: float,
) -> ProcessResult | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return run_process(
        args,
        cwd,
        remaining,
        absolute_deadline=overall_deadline,
    )


def _timeout_result(
    paths: dict[str, Path], started_at: float, message: str
) -> ProcessResult:
    stderr = _read_optional(paths["stderr"])
    if not stderr:
        stderr = message
    return ProcessResult(
        status="timeout",
        exit_code=None,
        stdout=_read_optional(paths["stdout"]),
        stderr=stderr,
        duration_seconds=time.monotonic() - started_at,
        timed_out=True,
    )


def _read_result(paths: dict[str, Path], started_at: float) -> ProcessResult:
    try:
        exit_code = int(paths["exit"].read_text(encoding="ascii").strip())
        stdout = paths["stdout"].read_text(encoding="utf-8", errors="replace")
        stderr = paths["stderr"].read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as error:
        raise MaoError(
            "INVALID_RESULT",
            "tmux launcher did not produce complete result artifacts",
            {"launcher": str(paths["launcher"])},
        ) from error
    return ProcessResult(
        status="ok" if exit_code == 0 else "error",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started_at,
        timed_out=False,
    )


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
