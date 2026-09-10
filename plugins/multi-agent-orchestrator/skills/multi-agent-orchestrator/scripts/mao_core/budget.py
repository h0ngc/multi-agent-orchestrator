from __future__ import annotations

from .config import Config
from .errors import MaoError
from .state import CriticState, RunState, StateStore, _bind_state, _state_origin


HARD_MAX_ROUNDS = 2
HARD_MAX_CALLS_PER_CRITIC = 2
HARD_MAX_TOTAL_CALLS = 4


def _config_error(message: str, **details: object) -> MaoError:
    return MaoError("CONFIG_INVALID", message, details)


def _budget_error(message: str, **details: object) -> MaoError:
    return MaoError("REVIEW_BUDGET_EXHAUSTED", message, details)


def _validate_limit(name: str, value: int, hard_maximum: int) -> int:
    if type(value) is not int or value < 1 or value > hard_maximum:
        raise _config_error(
            "Review budget limit is outside its hard bounds",
            limit=name,
            value=value,
            hard_maximum=hard_maximum,
        )
    return value


class BudgetGuard:
    def __init__(
        self,
        max_rounds: int,
        max_per_critic: int,
        max_total: int,
        *,
        state: RunState | None = None,
        store: StateStore | None = None,
    ):
        self._max_rounds = _validate_limit(
            "max_rounds", max_rounds, HARD_MAX_ROUNDS
        )
        self._max_per_critic = _validate_limit(
            "max_per_critic", max_per_critic, HARD_MAX_CALLS_PER_CRITIC
        )
        self._max_total = _validate_limit(
            "max_total", max_total, HARD_MAX_TOTAL_CALLS
        )
        if (state is None) != (store is None):
            raise _config_error("Persistent budget requires state and store together")

        self._state = state
        self._store = store
        if state is not None:
            self._expected_run_id, self._expected_request_digest = _state_origin(
                state
            )
        else:
            self._expected_run_id = None
            self._expected_request_digest = None
        self._total = state.total_calls if state is not None else 0
        self._calls = (
            {name: critic.calls for name, critic in state.critics.items()}
            if state is not None
            else {}
        )
        self._completed_rounds: set[tuple[str, int]] = set()
        self._reserved_rounds: set[tuple[str, int]] = set()

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        state: RunState | None = None,
        store: StateStore | None = None,
    ) -> BudgetGuard:
        return cls(
            config.max_review_rounds,
            config.max_calls_per_critic,
            config.max_total_critic_calls,
            state=state,
            store=store,
        )

    @property
    def total(self) -> int:
        return self._total

    @property
    def max_rounds(self) -> int:
        return min(self._max_rounds, HARD_MAX_ROUNDS)

    @property
    def max_per_critic(self) -> int:
        return min(self._max_per_critic, HARD_MAX_CALLS_PER_CRITIC)

    @property
    def max_total(self) -> int:
        return min(self._max_total, HARD_MAX_TOTAL_CALLS)

    def calls_for(self, critic: str) -> int:
        return self._calls.get(critic, 0)

    def can_start_round(self, round_number: int) -> bool:
        if self._state is not None and self._store is not None:
            assert self._expected_run_id is not None
            assert self._expected_request_digest is not None
            with self._store.run_lock(self._expected_run_id):
                current = self._store.resume(
                    self._expected_run_id,
                    self._expected_request_digest,
                )
                self._synchronize(current)
                return self._can_start_round_current(round_number)
        return self._can_start_round_current(round_number)

    def _can_start_round_current(self, round_number: int) -> bool:
        return (
            type(round_number) is int
            and 1 <= round_number <= self.max_rounds
            and self._total < self.max_total
        )

    def reserve_call(self, critic: str, round_number: int) -> None:
        """Persist one paid-call reservation before caller starts its process."""
        if not isinstance(critic, str) or not critic:
            raise _budget_error("Critic name is invalid")
        if self._state is not None and self._store is not None:
            self._reserve_persistent(critic, round_number)
            return

        self._validate_reservation(critic, round_number)
        self._total += 1
        self._calls[critic] = self.calls_for(critic) + 1
        self._reserved_rounds.add((critic, round_number))

    def complete_call(self, critic: str, round_number: int) -> None:
        """Mark an already-reserved critic round complete without charging again."""
        if not isinstance(critic, str) or not critic:
            raise _budget_error("Critic name is invalid")
        if type(round_number) is not int or not 1 <= round_number <= self.max_rounds:
            raise _budget_error(
                "Review round is invalid",
                critic=critic,
                round_number=round_number,
            )
        if self._state is not None and self._store is not None:
            self._complete_persistent(critic, round_number)
            return
        if (critic, round_number) not in self._reserved_rounds:
            raise _budget_error(
                "Critic round was not reserved",
                critic=critic,
                round_number=round_number,
            )
        self._reserved_rounds.remove((critic, round_number))
        self._completed_rounds.add((critic, round_number))

    def _validate_reservation(self, critic: str, round_number: int) -> None:
        if not self._can_start_round_current(round_number):
            raise _budget_error(
                "Review round or total call budget is exhausted",
                critic=critic,
                round_number=round_number,
            )
        if self.calls_for(critic) >= self.max_per_critic:
            raise _budget_error(
                "Per-critic call budget is exhausted",
                critic=critic,
            )
        if self._completed(critic, round_number):
            raise _budget_error(
                "Critic already completed this round",
                critic=critic,
                round_number=round_number,
            )

    def _completed(self, critic: str, round_number: int) -> bool:
        if (critic, round_number) in self._completed_rounds:
            return True
        if self._state is None or critic not in self._state.critics:
            return False
        return (
            self._state.critics[critic].rounds.get(str(round_number))
            == "completed"
        )

    def _reserve_persistent(self, critic: str, round_number: int) -> None:
        assert self._state is not None
        assert self._store is not None
        assert self._expected_run_id is not None
        assert self._expected_request_digest is not None

        def reserve(current: RunState) -> None:
            self._synchronize(current)
            self._validate_reservation(critic, round_number)
            critic_state = current.critics.setdefault(
                critic,
                CriticState(critic, "", "", 0, {}),
            )
            current.total_calls += 1
            critic_state.calls += 1
            critic_state.rounds[str(round_number)] = "reserved"

        current = self._store.update(
            self._expected_run_id,
            self._expected_request_digest,
            reserve,
        )
        self._synchronize(current)

    def _complete_persistent(self, critic: str, round_number: int) -> None:
        assert self._state is not None
        assert self._store is not None
        assert self._expected_run_id is not None
        assert self._expected_request_digest is not None

        def complete(current: RunState) -> None:
            self._synchronize(current)
            critic_state = current.critics.get(critic)
            if (
                critic_state is None
                or critic_state.rounds.get(str(round_number)) != "reserved"
            ):
                raise _budget_error(
                    "Critic round was not reserved",
                    critic=critic,
                    round_number=round_number,
                )
            critic_state.rounds[str(round_number)] = "completed"

        current = self._store.update(
            self._expected_run_id,
            self._expected_request_digest,
            complete,
        )
        self._synchronize(current)

    def _synchronize(self, current: RunState) -> None:
        assert self._state is not None
        self._state.run_id = current.run_id
        self._state.request_digest = current.request_digest
        self._state.packet_digest = current.packet_digest
        self._state.phase = current.phase
        self._state.round_number = current.round_number
        self._state.total_calls = current.total_calls
        self._state.critics = {
            name: CriticState(
                critic.provider,
                critic.model_requested,
                critic.model_resolved,
                critic.calls,
                dict(critic.rounds),
            )
            for name, critic in current.critics.items()
        }
        self._state.status = current.status
        assert self._expected_run_id is not None
        assert self._expected_request_digest is not None
        _bind_state(
            self._state,
            self._expected_run_id,
            self._expected_request_digest,
        )
        self._total = current.total_calls
        self._calls = {
            name: critic.calls for name, critic in current.critics.items()
        }
