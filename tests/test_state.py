from dataclasses import fields
import json
import os

import pytest

from mao_core.budget import BudgetGuard
from mao_core.errors import MaoError
from mao_core.state import CriticState, RunState, StateStore


def test_state_types_keep_exact_contract_field_order():
    assert [field.name for field in fields(CriticState)] == [
        "provider",
        "model_requested",
        "model_resolved",
        "calls",
        "rounds",
    ]
    assert [field.name for field in fields(RunState)] == [
        "run_id",
        "request_digest",
        "packet_digest",
        "phase",
        "round_number",
        "total_calls",
        "critics",
        "status",
    ]


def test_create_persists_a_loadable_initial_state(tmp_path):
    store = StateStore(tmp_path)

    created = store.create("run-1", request_digest="abc")
    loaded = store.load("run-1")

    assert loaded == created
    assert loaded.total_calls == 0
    assert loaded.critics["claude"].provider == "claude"
    assert loaded.critics["agy"].rounds == {}


def test_save_atomic_fsyncs_sibling_temp_before_replace(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.phase = "LOCAL_VERIFIED"
    target = tmp_path / "runs/run-1/state.json"
    real_fsync = os.fsync
    real_replace = os.replace
    events = []

    def recording_fsync(file_descriptor):
        events.append("fsync")
        real_fsync(file_descriptor)

    def recording_replace(source, destination):
        source = type(target)(source)
        destination = type(target)(destination)
        assert source.parent == target.parent
        assert destination == target
        assert json.loads(source.read_text(encoding="utf-8"))["phase"] == (
            "LOCAL_VERIFIED"
        )
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr("mao_core.state.os.fsync", recording_fsync)
    monkeypatch.setattr("mao_core.state.os.replace", recording_replace)

    store.save_atomic(state)

    assert events == ["fsync", "replace"]
    assert store.load("run-1").phase == "LOCAL_VERIFIED"
    assert list(target.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        json.dumps({"run_id": "run-1"}),
        json.dumps(
            {
                "run_id": "run-1",
                "request_digest": "abc",
                "packet_digest": "",
                "phase": "CREATED",
                "round_number": 0,
                "total_calls": 0,
                "critics": {"claude": {"provider": "claude"}},
                "status": "running",
            }
        ),
    ],
)
def test_load_rejects_malformed_or_incomplete_state(tmp_path, payload):
    state_path = tmp_path / "runs/run-1/state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(payload, encoding="utf-8")

    with pytest.raises(MaoError) as caught:
        StateStore(tmp_path).load("run-1")

    assert caught.value.code == "STATE_TRANSITION_INVALID"


def test_resume_rejects_request_digest_mismatch(tmp_path):
    store = StateStore(tmp_path)
    store.create("run-1", request_digest="abc")

    with pytest.raises(MaoError) as caught:
        store.resume("run-1", request_digest="changed")

    assert caught.value.code == "STATE_TRANSITION_INVALID"


def test_resume_does_not_repeat_completed_critic(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.critics["claude"].rounds["1"] = "completed"
    store.save_atomic(state)

    resumed = store.resume("run-1", request_digest="abc")

    assert resumed.critics["claude"].rounds["1"] == "completed"


def test_save_atomic_rejects_stale_budget_regression(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    stale = store.load("run-1")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)
    guard.reserve_call("claude", 1)

    with pytest.raises(MaoError) as caught:
        store.save_atomic(stale)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    persisted = store.load("run-1")
    assert persisted.total_calls == 1
    assert persisted.critics["claude"].calls == 1
    assert persisted.critics["claude"].rounds["1"] == "reserved"


def test_save_atomic_rejects_request_digest_mutation(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.request_digest = "mutated"

    with pytest.raises(MaoError) as caught:
        store.save_atomic(state)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert store.load("run-1").request_digest == "abc"


def test_save_atomic_rejects_cross_run_redirection(tmp_path):
    store = StateStore(tmp_path)
    store.create("run-a", request_digest="digest-a")
    store.create("run-b", request_digest="digest-b")
    before_a = store.load("run-a")
    before_b = store.load("run-b")
    redirected = store.load("run-a")
    redirected.phase = "REDIRECTED_FROM_A"
    redirected.run_id = "run-b"
    redirected.request_digest = "digest-b"

    with pytest.raises(MaoError) as caught:
        store.save_atomic(redirected)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert store.load("run-a") == before_a
    assert store.load("run-b") == before_b


def test_save_atomic_rejects_unbound_manual_state(tmp_path):
    store = StateStore(tmp_path)
    original = store.create("run-1", request_digest="abc")
    unbound = RunState(
        run_id="run-1",
        request_digest="abc",
        packet_digest="",
        phase="MANUAL",
        round_number=0,
        total_calls=0,
        critics={
            "claude": CriticState("claude", "", "", 0, {}),
            "agy": CriticState("agy", "", "", 0, {}),
        },
        status="RUNNING",
    )

    with pytest.raises(MaoError) as caught:
        store.save_atomic(unbound)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert store.load("run-1") == original


def test_save_atomic_allows_reserved_to_completed_and_non_budget_updates(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)
    guard.reserve_call("claude", 1)
    state.critics["claude"].rounds["1"] = "completed"
    state.phase = "LOCAL_VERIFIED"
    state.status = "REVIEWING"

    store.save_atomic(state)

    persisted = store.load("run-1")
    assert persisted.total_calls == 1
    assert persisted.critics["claude"].rounds["1"] == "completed"
    assert persisted.phase == "LOCAL_VERIFIED"
    assert persisted.status == "REVIEWING"


def test_save_atomic_rejects_completed_round_downgrade(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    guard = BudgetGuard(2, 2, 4, state=state, store=store)
    guard.reserve_call("claude", 1)
    state.critics["claude"].rounds["1"] = "completed"
    store.save_atomic(state)
    state.critics["claude"].rounds["1"] = "reserved"

    with pytest.raises(MaoError) as caught:
        store.save_atomic(state)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert store.load("run-1").critics["claude"].rounds["1"] == "completed"


@pytest.mark.parametrize(
    "rounds",
    [
        {"3": "reserved"},
        {"01": "completed"},
        {"1": "complete"},
    ],
)
def test_load_rejects_unknown_round_key_or_status(tmp_path, rounds):
    store = StateStore(tmp_path)
    store.create("run-1", request_digest="abc")
    state_path = tmp_path / "runs/run-1/state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["critics"]["claude"]["rounds"] = rounds
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MaoError) as caught:
        store.load("run-1")

    assert caught.value.code == "STATE_TRANSITION_INVALID"


@pytest.mark.parametrize(
    "rounds",
    [
        {"3": "reserved"},
        {"01": "completed"},
        {"1": "complete"},
    ],
)
def test_save_rejects_unknown_round_key_or_status(tmp_path, rounds):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.critics["claude"].rounds = rounds

    with pytest.raises(MaoError) as caught:
        store.save_atomic(state)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    assert store.load("run-1").critics["claude"].rounds == {}


def test_typo_completion_cannot_enable_duplicate_same_round_call(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.total_calls = 1
    state.critics["claude"].calls = 1
    state.critics["claude"].rounds["1"] = "complete"

    with pytest.raises(MaoError) as caught:
        store.save_atomic(state)

    assert caught.value.code == "STATE_TRANSITION_INVALID"
    persisted = store.load("run-1")
    assert persisted.total_calls == 0
    assert persisted.critics["claude"].rounds == {}

    guard = BudgetGuard(2, 2, 4, state=persisted, store=store)
    guard.reserve_call("claude", 1)
    persisted.critics["claude"].rounds["1"] = "completed"
    store.save_atomic(persisted)
    persisted.critics["claude"].rounds["1"] = "complete"
    with pytest.raises(MaoError):
        store.save_atomic(persisted)

    resumed = store.resume("run-1", request_digest="abc")
    resumed_guard = BudgetGuard(2, 2, 4, state=resumed, store=store)
    with pytest.raises(MaoError) as duplicate:
        resumed_guard.reserve_call("claude", 1)
    assert duplicate.value.code == "REVIEW_BUDGET_EXHAUSTED"
    final = store.load("run-1")
    assert final.total_calls == 1
    assert final.critics["claude"].calls == 1
    assert final.critics["claude"].rounds["1"] == "completed"


@pytest.mark.parametrize("run_id", ["../escape", "nested/run", ".", ""])
def test_run_id_cannot_escape_store(tmp_path, run_id):
    with pytest.raises(MaoError) as caught:
        StateStore(tmp_path).create(run_id, request_digest="abc")

    assert caught.value.code == "STATE_TRANSITION_INVALID"
