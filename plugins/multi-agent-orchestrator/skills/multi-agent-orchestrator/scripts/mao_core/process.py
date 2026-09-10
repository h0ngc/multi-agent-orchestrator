from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Sequence

from mao_core.errors import MaoError


_TERMINATION_WAIT_SECONDS = 5
_KILL_WAIT_SECONDS = 0.1
_PROCESS_GROUP_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class ProcessResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


def run_process(
    args: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    stdin: str | None = None,
    *,
    absolute_deadline: float | None = None,
) -> ProcessResult:
    """Run one command without a shell and keep captured output in memory."""
    command = list(args)
    if not command:
        raise MaoError(
            "SESSION_START_FAILED",
            "Process command cannot be empty",
            {},
        )

    started_at = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise MaoError(
            "CLI_NOT_FOUND",
            "Process executable not found",
            {"executable": command[0]},
        ) from error
    except OSError as error:
        raise MaoError(
            "SESSION_START_FAILED",
            "Process could not be started",
            {"executable": command[0], "errno": error.errno},
        ) from error

    communicate_timeout = timeout_seconds
    if absolute_deadline is not None:
        communicate_timeout = min(
            timeout_seconds,
            max(0, absolute_deadline - time.monotonic()),
        )
    try:
        stdout, stderr = process.communicate(
            input=stdin,
            timeout=communicate_timeout,
        )
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process_group(process, absolute_deadline)
        return ProcessResult(
            status="timeout",
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started_at,
            timed_out=True,
        )

    return ProcessResult(
        status="ok" if process.returncode == 0 else "error",
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started_at,
        timed_out=False,
    )


def _terminate_process_group(
    process: subprocess.Popen[str],
    absolute_deadline: float | None = None,
) -> tuple[str, str]:
    if absolute_deadline is not None:
        return _terminate_process_group_before_deadline(process, absolute_deadline)

    process_group_id = process.pid
    _signal_process_group(process_group_id, signal.SIGTERM)
    grace_deadline = time.monotonic() + _TERMINATION_WAIT_SECONDS
    output = _communicate_until(process, grace_deadline)

    if _wait_for_process_group_exit(process_group_id, grace_deadline):
        if output is not None:
            return output
        output = _communicate_until(
            process,
            time.monotonic() + _TERMINATION_WAIT_SECONDS,
        )
        if output is not None:
            return output

    _signal_process_group(process_group_id, signal.SIGKILL)
    kill_deadline = time.monotonic() + _TERMINATION_WAIT_SECONDS
    output = _communicate_until(process, kill_deadline)
    group_exited = _wait_for_process_group_exit(process_group_id, kill_deadline)
    if output is not None and group_exited:
        return output

    raise MaoError(
        "TIMEOUT",
        "Process group did not exit after kill",
        {},
    )


def _terminate_process_group_before_deadline(
    process: subprocess.Popen[str],
    absolute_deadline: float,
) -> tuple[str, str]:
    process_group_id = process.pid
    remaining = max(0, absolute_deadline - time.monotonic())
    kill_wait = min(_KILL_WAIT_SECONDS, remaining)
    terminate_deadline = absolute_deadline - kill_wait
    _signal_process_group_before_deadline(process_group_id, signal.SIGTERM)
    output, partial_output = _communicate_with_partial_until(
        process,
        terminate_deadline,
    )

    if output is not None:
        partial_output = output
        if _wait_for_process_group_exit(process_group_id, terminate_deadline):
            return output

    _signal_process_group_before_deadline(process_group_id, signal.SIGKILL)
    output, killed_partial_output = _communicate_with_partial_until(
        process,
        absolute_deadline,
    )
    if output is not None:
        return output
    if killed_partial_output != ("", ""):
        return killed_partial_output
    return partial_output


def _signal_process_group(process_group_id: int, signal_number: int) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        pass


def _signal_process_group_before_deadline(
    process_group_id: int,
    signal_number: int,
) -> None:
    try:
        _signal_process_group(process_group_id, signal_number)
    except PermissionError:
        pass


def _communicate_until(
    process: subprocess.Popen[str],
    deadline: float,
) -> tuple[str, str] | None:
    try:
        return process.communicate(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return None


def _communicate_with_partial_until(
    process: subprocess.Popen[str],
    deadline: float,
) -> tuple[tuple[str, str] | None, tuple[str, str]]:
    try:
        return (
            process.communicate(timeout=max(0, deadline - time.monotonic())),
            ("", ""),
        )
    except subprocess.TimeoutExpired as error:
        return None, (
            _timeout_output_text(error.stdout),
            _timeout_output_text(error.stderr),
        )


def _timeout_output_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def _wait_for_process_group_exit(process_group_id: int, deadline: float) -> bool:
    while _process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PROCESS_GROUP_POLL_SECONDS, remaining))
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
