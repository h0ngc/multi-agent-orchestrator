import os
from pathlib import Path
import signal
import sys
import time

import pytest

from mao_core.process import run_process


def _process_group_exists(process_group_id):
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_process_group_exit(process_group_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _process_exists(process_id):
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_exit(process_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while _process_exists(process_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def test_process_uses_argument_array_without_shell(tmp_path):
    result = run_process(
        [str(Path("tests/fakes/echo-agent").resolve()), "a;touch", "never"],
        tmp_path,
        2,
    )

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["a;touch", "never"]
    assert not (tmp_path / "touch").exists()


def test_timeout_returns_typed_result(tmp_path):
    result = run_process(
        ["python3", "-c", "import time; time.sleep(3)"],
        tmp_path,
        1,
    )

    assert result.status == "timeout"
    assert result.timed_out is True


def test_timeout_kills_child_when_parent_exits_and_child_closes_pipes(tmp_path):
    child_code = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "ready_fd = int(sys.argv[1]); "
        "os.write(ready_fd, b'1'); "
        "os.close(ready_fd); "
        "time.sleep(30)"
    )
    parent_code = f"""
import os
import subprocess
import sys
import time

read_fd, write_fd = os.pipe()
child = subprocess.Popen(
    [sys.executable, "-c", {child_code!r}, str(write_fd)],
    pass_fds=(write_fd,),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
os.close(write_fd)
os.read(read_fd, 1)
os.close(read_fd)
print(os.getpid(), child.pid, flush=True)
time.sleep(30)
"""
    process_group_id = None

    try:
        result = run_process([sys.executable, "-c", parent_code], tmp_path, 1)
        process_group_id, child_pid = map(int, result.stdout.split())

        assert result.status == "timeout"
        assert _wait_for_process_group_exit(process_group_id, 0.5)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process_group_id is not None and _process_group_exists(process_group_id):
            os.killpg(process_group_id, signal.SIGKILL)
            _wait_for_process_group_exit(process_group_id, 1)


def test_absolute_deadline_bounds_sigterm_resistant_child_teardown(tmp_path):
    child_code = """
import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
time.sleep(30)
"""
    child_pid = None
    started_at = time.monotonic()
    absolute_deadline = started_at + 1.0

    try:
        result = run_process(
            [sys.executable, "-c", child_code],
            tmp_path,
            0.1,
            absolute_deadline=absolute_deadline,
        )
        elapsed = time.monotonic() - started_at
        child_pid = int(result.stdout.strip())

        assert result.status == "timeout"
        assert result.timed_out is True
        assert elapsed < 1.75
        assert _wait_for_process_exit(child_pid, 0.5)
    finally:
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
            _wait_for_process_exit(child_pid, 1)
