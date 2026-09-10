from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from .errors import MaoError


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CRITIC_FIELDS = {
    "provider",
    "model_requested",
    "model_resolved",
    "calls",
    "rounds",
}
_RUN_FIELDS = {
    "run_id",
    "request_digest",
    "packet_digest",
    "phase",
    "round_number",
    "total_calls",
    "critics",
    "status",
}
_DEFAULT_CRITICS = ("claude", "agy")
_ROUND_KEYS = frozenset({"1", "2"})
_ROUND_STATUSES = frozenset({"reserved", "completed"})
_STATE_ORIGIN_ATTRIBUTE = "_mao_state_origin"


@dataclass
class CriticState:
    provider: str
    model_requested: str
    model_resolved: str
    calls: int
    rounds: dict[str, str]


@dataclass
class RunState:
    run_id: str
    request_digest: str
    packet_digest: str
    phase: str
    round_number: int
    total_calls: int
    critics: dict[str, CriticState]
    status: str


def _state_error(message: str, **details: object) -> MaoError:
    return MaoError("STATE_TRANSITION_INVALID", message, details)


def _is_int(value: object) -> bool:
    return type(value) is int


def _require_string(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _state_error("Run state field must be a string", field=field)
    return value


def _parse_critic(name: str, value: object) -> CriticState:
    if not isinstance(value, dict) or set(value) != _CRITIC_FIELDS:
        raise _state_error("Critic state has invalid fields", critic=name)

    provider = _require_string(value["provider"], f"critics.{name}.provider")
    model_requested = _require_string(
        value["model_requested"],
        f"critics.{name}.model_requested",
        allow_empty=True,
    )
    model_resolved = _require_string(
        value["model_resolved"],
        f"critics.{name}.model_resolved",
        allow_empty=True,
    )
    calls = value["calls"]
    if not _is_int(calls) or calls < 0:
        raise _state_error("Critic call count must be non-negative", critic=name)

    raw_rounds = value["rounds"]
    if not isinstance(raw_rounds, dict):
        raise _state_error("Critic rounds must be an object", critic=name)
    rounds: dict[str, str] = {}
    for round_key, status in raw_rounds.items():
        if (
            not isinstance(round_key, str)
            or round_key not in _ROUND_KEYS
            or not isinstance(status, str)
            or status not in _ROUND_STATUSES
        ):
            raise _state_error("Critic round entry is invalid", critic=name)
        rounds[round_key] = status

    return CriticState(
        provider=provider,
        model_requested=model_requested,
        model_resolved=model_resolved,
        calls=calls,
        rounds=rounds,
    )


def _parse_state(value: object, expected_run_id: str | None = None) -> RunState:
    if not isinstance(value, dict) or set(value) != _RUN_FIELDS:
        raise _state_error("Run state has invalid fields")

    run_id = _validate_run_id(value["run_id"])
    if expected_run_id is not None and run_id != expected_run_id:
        raise _state_error(
            "Run state identity does not match its path",
            expected=expected_run_id,
            actual=run_id,
        )
    request_digest = _require_string(value["request_digest"], "request_digest")
    packet_digest = _require_string(
        value["packet_digest"], "packet_digest", allow_empty=True
    )
    phase = _require_string(value["phase"], "phase")
    status = _require_string(value["status"], "status")

    round_number = value["round_number"]
    total_calls = value["total_calls"]
    if not _is_int(round_number) or round_number < 0:
        raise _state_error("Round number must be non-negative")
    if not _is_int(total_calls) or total_calls < 0:
        raise _state_error("Total call count must be non-negative")

    raw_critics = value["critics"]
    if not isinstance(raw_critics, dict):
        raise _state_error("Critics must be an object")
    critics: dict[str, CriticState] = {}
    for name, critic_value in raw_critics.items():
        if not isinstance(name, str) or not name:
            raise _state_error("Critic name must be a non-empty string")
        critics[name] = _parse_critic(name, critic_value)

    if total_calls != sum(critic.calls for critic in critics.values()):
        raise _state_error("Total call count does not match critic counters")

    return RunState(
        run_id=run_id,
        request_digest=request_digest,
        packet_digest=packet_digest,
        phase=phase,
        round_number=round_number,
        total_calls=total_calls,
        critics=critics,
        status=status,
    )


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str) or not _RUN_ID_PATTERN.fullmatch(value):
        raise _state_error("Run ID is invalid", run_id=value)
    return value


def _bind_state(state: RunState, run_id: str, request_digest: str) -> RunState:
    setattr(state, _STATE_ORIGIN_ATTRIBUTE, (run_id, request_digest))
    return state


def _state_origin(state: RunState) -> tuple[str, str]:
    origin = getattr(state, _STATE_ORIGIN_ATTRIBUTE, None)
    if (
        not isinstance(origin, tuple)
        or len(origin) != 2
        or not all(isinstance(item, str) and item for item in origin)
    ):
        raise _state_error("Run state has no trusted origin")
    return origin


def _validate_state_update(current: RunState, proposed: RunState) -> None:
    if proposed.request_digest != current.request_digest:
        raise _state_error("Request digest cannot change", run_id=current.run_id)
    if proposed.total_calls < current.total_calls:
        raise _state_error("Total call count cannot decrease", run_id=current.run_id)

    for name, current_critic in current.critics.items():
        proposed_critic = proposed.critics.get(name)
        proposed_calls = proposed_critic.calls if proposed_critic else 0
        if proposed_calls < current_critic.calls:
            raise _state_error(
                "Critic call count cannot decrease",
                run_id=current.run_id,
                critic=name,
            )
        for round_key, current_status in current_critic.rounds.items():
            if current_status not in {"reserved", "completed"}:
                continue
            proposed_status = (
                proposed_critic.rounds.get(round_key) if proposed_critic else None
            )
            allowed = (
                {"reserved", "completed"}
                if current_status == "reserved"
                else {"completed"}
            )
            if proposed_status not in allowed:
                raise _state_error(
                    "Critic round status cannot be removed or downgraded",
                    run_id=current.run_id,
                    critic=name,
                    round_number=round_key,
                )


class StateStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.runs_directory = self.root / "runs"

    def create(self, run_id: str, request_digest: str) -> RunState:
        run_id = _validate_run_id(run_id)
        request_digest = _require_string(request_digest, "request_digest")
        state = RunState(
            run_id=run_id,
            request_digest=request_digest,
            packet_digest="",
            phase="CREATED",
            round_number=0,
            total_calls=0,
            critics={
                provider: CriticState(provider, "", "", 0, {})
                for provider in _DEFAULT_CRITICS
            },
            status="RUNNING",
        )
        with self.run_lock(run_id):
            if self._state_path(run_id).exists():
                raise _state_error("Run already exists", run_id=run_id)
            self._save_atomic_unlocked(state, run_id)
        return _bind_state(state, run_id, request_digest)

    def load(self, run_id: str) -> RunState:
        run_id = _validate_run_id(run_id)
        state_path = self._state_path(run_id)
        try:
            raw_state: Any = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise _state_error(
                "Run state could not be loaded", run_id=run_id
            ) from error
        state = _parse_state(raw_state, expected_run_id=run_id)
        return _bind_state(state, run_id, state.request_digest)

    def save_atomic(self, state: RunState) -> None:
        origin_run_id, origin_request_digest = _state_origin(state)
        if (
            state.run_id != origin_run_id
            or state.request_digest != origin_request_digest
        ):
            raise _state_error("Run state diverged from its trusted origin")
        proposed = _parse_state(asdict(state), expected_run_id=origin_run_id)
        with self.run_lock(origin_run_id):
            current = self.resume(origin_run_id, origin_request_digest)
            _validate_state_update(current, proposed)
            self._save_atomic_unlocked(proposed, origin_run_id)

    def update(
        self,
        run_id: str,
        request_digest: str,
        operation: Callable[[RunState], None],
    ) -> RunState:
        with self.run_lock(run_id):
            current = self.resume(run_id, request_digest)
            original = _parse_state(asdict(current), expected_run_id=run_id)
            operation(current)
            proposed = _parse_state(asdict(current), expected_run_id=run_id)
            _validate_state_update(original, proposed)
            self._save_atomic_unlocked(proposed, run_id)
            return _bind_state(proposed, run_id, request_digest)

    def _save_atomic_unlocked(self, state: RunState, run_id: str) -> None:
        validated = _parse_state(asdict(state), expected_run_id=run_id)
        state_path = self._state_path(run_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(asdict(validated), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=state_path.parent,
                prefix=f".{state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, state_path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def resume(self, run_id: str, request_digest: str) -> RunState:
        state = self.load(run_id)
        if state.request_digest != request_digest:
            raise _state_error(
                "Request digest does not match persisted run",
                run_id=run_id,
            )
        return state

    @contextmanager
    def run_lock(self, run_id: str) -> Iterator[None]:
        state_path = self._state_path(run_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = state_path.with_name("state.lock")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _state_path(self, run_id: str) -> Path:
        return self.runs_directory / _validate_run_id(run_id) / "state.json"
