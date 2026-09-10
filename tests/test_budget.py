import multiprocessing
from pathlib import Path

import pytest

from mao_core.budget import BudgetGuard
from mao_core.config import Config
from mao_core.errors import MaoError
from mao_core.state import StateStore


def _reserve_after_process_barrier(root, critic, barrier):
    store = StateStore(Path(root))
    state = store.load("run-1")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)
    barrier.wait(timeout=10)
    guard.reserve_call(critic, 1)


def assert_budget_exhausted(operation):
    with pytest.raises(MaoError) as caught:
        operation()
    assert caught.value.code == "REVIEW_BUDGET_EXHAUSTED"


def test_fifth_total_call_is_rejected():
    guard = BudgetGuard(max_rounds=2, max_per_critic=2, max_total=4)
    for critic in ["claude", "agy", "claude", "agy"]:
        guard.reserve_call(critic, 1 if guard.total < 2 else 2)

    assert_budget_exhausted(lambda: guard.reserve_call("claude", 2))
    assert guard.total == 4


def test_third_call_for_one_critic_is_rejected():
    guard = BudgetGuard(max_rounds=2, max_per_critic=2, max_total=4)
    guard.reserve_call("claude", 1)
    guard.reserve_call("claude", 2)

    assert_budget_exhausted(lambda: guard.reserve_call("claude", 2))
    assert guard.calls_for("claude") == 2


def test_can_start_round_enforces_configured_round_limit():
    guard = BudgetGuard(max_rounds=1, max_per_critic=2, max_total=4)

    assert guard.can_start_round(1) is True
    assert guard.can_start_round(0) is False
    assert guard.can_start_round(2) is False
    assert_budget_exhausted(lambda: guard.reserve_call("claude", 2))


@pytest.mark.parametrize(
    "limits",
    [
        {"max_rounds": 3, "max_per_critic": 2, "max_total": 4},
        {"max_rounds": 2, "max_per_critic": 3, "max_total": 4},
        {"max_rounds": 2, "max_per_critic": 2, "max_total": 5},
    ],
)
def test_constructor_rejects_limits_above_hard_maxima(limits):
    with pytest.raises(MaoError) as caught:
        BudgetGuard(**limits)

    assert caught.value.code == "CONFIG_INVALID"


def test_from_config_uses_configured_limits():
    config = Config(
        primary_provider="codex",
        codex_model="gpt",
        claude_model="claude",
        antigravity_model="gemini",
        max_review_rounds=1,
        max_calls_per_critic=1,
        max_total_critic_calls=1,
    )

    guard = BudgetGuard.from_config(config)
    guard.reserve_call("claude", 1)

    assert_budget_exhausted(lambda: guard.reserve_call("agy", 1))


def test_reservation_is_persisted_before_caller_can_launch(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)

    guard.reserve_call("claude", 1)

    persisted = store.load("run-1")
    assert persisted.total_calls == 1
    assert persisted.critics["claude"].calls == 1
    assert persisted.critics["claude"].rounds["1"] == "reserved"


def test_stale_guards_reload_state_before_reserving(tmp_path):
    store = StateStore(tmp_path)
    store.create("run-1", request_digest="abc")
    left_state = store.load("run-1")
    right_state = store.load("run-1")
    left = BudgetGuard(2, 2, 4, state=left_state, store=store)
    right = BudgetGuard(2, 2, 4, state=right_state, store=store)

    left.reserve_call("claude", 1)
    right.reserve_call("agy", 1)

    persisted = store.load("run-1")
    assert persisted.total_calls == 2
    assert persisted.critics["claude"].calls == 1
    assert persisted.critics["agy"].calls == 1
    assert right.total == 2
    assert right_state == persisted


def test_persistent_can_start_round_refreshes_total_cap(tmp_path):
    store = StateStore(tmp_path)
    store.create("run-1", request_digest="abc")
    active = BudgetGuard(2, 2, 1, state=store.load("run-1"), store=store)
    stale_state = store.load("run-1")
    stale = BudgetGuard(2, 2, 1, state=stale_state, store=store)
    active.reserve_call("claude", 1)

    assert stale.can_start_round(1) is False
    assert stale.total == 1
    assert stale_state == store.load("run-1")


def test_persistent_guard_uses_immutable_run_identity(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)
    state.run_id = "mutated-run"
    state.request_digest = "mutated-digest"

    guard.reserve_call("claude", 1)

    persisted = store.load("run-1")
    assert persisted.total_calls == 1
    assert persisted.request_digest == "abc"
    assert state == persisted


def test_concurrent_process_reservations_are_serialized(tmp_path):
    store = StateStore(tmp_path)
    store.create("run-1", request_digest="abc")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(3)
    processes = [
        context.Process(
            target=_reserve_after_process_barrier,
            args=(str(tmp_path), critic, barrier),
        )
        for critic in ("claude", "agy")
    ]

    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        for process in processes:
            process.join(timeout=10)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    persisted = store.load("run-1")
    assert persisted.total_calls == 2
    assert persisted.critics["claude"].calls == 1
    assert persisted.critics["agy"].calls == 1


def test_resumed_counters_prevent_duplicate_paid_call(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    first = BudgetGuard(2, 1, 4, state=state, store=store)
    first.reserve_call("claude", 1)

    resumed = store.resume("run-1", request_digest="abc")
    second = BudgetGuard(2, 1, 4, state=resumed, store=store)

    assert_budget_exhausted(lambda: second.reserve_call("claude", 1))
    assert store.load("run-1").total_calls == 1


def test_completed_critic_is_not_repeated_with_budget_remaining(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.critics["claude"].calls = 1
    state.critics["claude"].rounds["1"] = "completed"
    state.total_calls = 1
    store.save_atomic(state)
    guard = BudgetGuard(2, 2, 4, state=state, store=store)

    assert_budget_exhausted(lambda: guard.reserve_call("claude", 1))
    assert store.load("run-1").total_calls == 1


def test_nonpersistent_completion_requires_matching_reservation():
    guard = BudgetGuard(2, 2, 4)
    guard.reserve_call("claude", 1)

    assert_budget_exhausted(lambda: guard.complete_call("claude", 2))
    guard.complete_call("claude", 1)
    assert_budget_exhausted(lambda: guard.reserve_call("claude", 1))


def test_failed_persistence_does_not_leave_in_memory_reservation(
    tmp_path, monkeypatch
):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)

    def fail_replace(_source, _destination):
        raise OSError("disk full")

    monkeypatch.setattr("mao_core.state.os.replace", fail_replace)

    with pytest.raises(OSError, match="disk full"):
        guard.reserve_call("claude", 1)

    assert guard.total == 0
    assert state.critics["claude"].calls == 0
    assert state.critics["claude"].rounds == {}


def test_limit_mutation_cannot_bypass_hard_caps():
    guard = BudgetGuard(2, 2, 4)

    for name in ("max_rounds", "max_per_critic", "max_total"):
        with pytest.raises(AttributeError):
            setattr(guard, name, 99)

    guard.__dict__.update(
        _max_rounds=99,
        _max_per_critic=99,
        _max_total=99,
    )

    assert guard.can_start_round(3) is False
    assert_budget_exhausted(lambda: guard.reserve_call("claude", 3))
    guard.reserve_call("claude", 1)
    guard.reserve_call("claude", 2)
    assert_budget_exhausted(lambda: guard.reserve_call("claude", 2))
    guard.reserve_call("agy", 1)
    guard.reserve_call("agy", 2)
    assert_budget_exhausted(lambda: guard.reserve_call("codex", 2))
    assert guard.total == 4
