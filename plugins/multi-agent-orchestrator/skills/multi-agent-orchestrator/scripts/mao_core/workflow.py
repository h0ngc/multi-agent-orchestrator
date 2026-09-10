from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import secrets
from typing import Callable, Mapping, Sequence

from .config import Config
from .errors import MaoError
from .providers.base import ModelIdentity, ProviderAdapter
from .packet import validate_packet
from .review import (
    CriticJob,
    CriticOutcome,
    Decision,
    Finding,
    ReviewResult,
    deduplicate_findings,
    finding_fingerprint,
    parse_review,
)
from .state import RunState, StateStore, _bind_state, _state_origin
from .transports.base import InvocationRequest, TransportAdapter


TRANSITIONS = {
    "IMPLEMENTED": {"LOCAL_VERIFIED"},
    "LOCAL_VERIFIED": {"REVIEW_ROUND_1"},
    "REVIEW_ROUND_1": {"TRIAGED"},
    "TRIAGED": {"PATCHED", "FINAL_DECISION"},
    "PATCHED": {"LOCAL_REVERIFIED"},
    "LOCAL_REVERIFIED": {"REVIEW_ROUND_2", "FINAL_DECISION"},
    "REVIEW_ROUND_2": {"FINAL_DECISION"},
    "FINAL_DECISION": {"DONE"},
}
_VERDICTS = frozenset({"accepted", "rejected", "needs-proof"})
_METADATA_FIELDS = frozenset(
    {
        "format_version",
        "request_digest",
        "local_verifications",
        "rounds",
        "expected_reviewers",
        "round_complete",
        "review_leases",
        "decisions",
        "review_gaps",
    }
)


@dataclass(frozen=True)
class FinalResult:
    status: str
    findings: tuple[Finding, ...]
    review_gaps: tuple[str, ...]


def _state_error(message: str, **details: object) -> MaoError:
    return MaoError("STATE_TRANSITION_INVALID", message, details)


def _request_digest(request: str) -> str:
    if not isinstance(request, str) or not request or "\x00" in request:
        raise _state_error("Work request must be a non-empty text value")
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _sync_state(target: RunState, source: RunState) -> None:
    target.run_id = source.run_id
    target.request_digest = source.request_digest
    target.packet_digest = source.packet_digest
    target.phase = source.phase
    target.round_number = source.round_number
    target.total_calls = source.total_calls
    target.critics = source.critics
    target.status = source.status
    _bind_state(target, source.run_id, source.request_digest)


def _finding(value: Mapping[str, object]) -> Finding:
    return Finding(
        severity=str(value["severity"]),
        category=str(value["category"]),
        file=str(value["file"]),
        line=value["line"] if isinstance(value["line"], int) else None,
        evidence=str(value["evidence"]),
        reason=str(value["reason"]),
        suggested_fix=str(value["suggested_fix"]),
        confidence=float(value["confidence"]),
        needs_context=tuple(str(item) for item in value["needs_context"]),
    )


def _review_record(critic: str, outcome: ReviewResult | CriticOutcome) -> dict:
    if isinstance(outcome, CriticOutcome):
        parsed = None
        if outcome.status == "completed" and outcome.review is not None:
            try:
                parsed = parse_review(
                    json.loads(json.dumps(asdict(outcome.review), allow_nan=False))
                )
            except (MaoError, TypeError, ValueError):
                parsed = None
        if parsed is None:
            error = outcome.error if isinstance(outcome.error, dict) else {
                "error": {
                    "code": "INVALID_RESULT",
                    "message": "Critic review was incomplete or invalid",
                    "details": {},
                }
            }
            return {
                "critic": outcome.critic,
                "call_number": outcome.call_number,
                "status": "failed",
                "review": None,
                "error": error,
            }
        return {
            "critic": outcome.critic,
            "call_number": outcome.call_number,
            "status": "completed",
            "review": json.loads(json.dumps(asdict(parsed), allow_nan=False)),
            "error": None,
        }
    if not isinstance(outcome, ReviewResult):
        raise _state_error("Review outcome has unsupported type")
    try:
        parsed = parse_review(json.loads(json.dumps(asdict(outcome), allow_nan=False)))
    except (MaoError, TypeError, ValueError):
        raise _state_error("Review outcome is incomplete or invalid") from None
    return {
        "critic": critic,
        "call_number": 0,
        "status": "completed",
        "review": json.loads(json.dumps(asdict(parsed), allow_nan=False)),
        "error": None,
    }


def select_reviewers(
    primary: ModelIdentity, available: Sequence[ModelIdentity]
) -> list[ModelIdentity]:
    if not isinstance(primary, ModelIdentity):
        raise _state_error("Primary model identity is invalid")
    selected: list[ModelIdentity] = []
    vendors: set[str] = set()
    for identity in available:
        if not isinstance(identity, ModelIdentity) or not identity.verified:
            continue
        if identity.vendor == primary.vendor or identity.vendor in vendors:
            continue
        selected.append(identity)
        vendors.add(identity.vendor)
        if len(selected) == 2:
            break
    return selected


def _validate_metadata(value: object, expected_digest: str) -> dict:
    if not isinstance(value, dict) or set(value) != _METADATA_FIELDS:
        raise _state_error("Workflow evidence has invalid fields")
    if value["format_version"] != 1 or value["request_digest"] != expected_digest:
        raise _state_error("Workflow evidence identity is invalid")
    verifications = value["local_verifications"]
    if not isinstance(verifications, list):
        raise _state_error("Local verification evidence is invalid")
    for item in verifications:
        if (
            not isinstance(item, dict)
            or set(item) != {"phase", "passed", "output", "new_review_surface"}
            or item["phase"] not in {"IMPLEMENTED", "PATCHED"}
            or type(item["passed"]) is not bool
            or not isinstance(item["output"], str)
            or type(item["new_review_surface"]) is not bool
        ):
            raise _state_error("Local verification evidence is invalid")
    rounds = value["rounds"]
    if not isinstance(rounds, dict) or set(rounds) != {"1", "2"}:
        raise _state_error("Review-round evidence is invalid")
    for records in rounds.values():
        if not isinstance(records, list):
            raise _state_error("Review-round evidence is invalid")
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != {"critic", "call_number", "status", "review", "error"}
                or not isinstance(record["critic"], str)
                or not record["critic"]
                or type(record["call_number"]) is not int
                or record["call_number"] < 0
                or record["status"] not in {"completed", "failed"}
            ):
                raise _state_error("Review record is invalid")
            if record["status"] == "completed":
                if not isinstance(record["review"], dict) or record["error"] is not None:
                    raise _state_error("Completed review record is invalid")
                try:
                    parse_review(record["review"])
                except MaoError:
                    raise _state_error("Completed review payload is invalid") from None
            elif record["review"] is not None or not isinstance(record["error"], dict):
                raise _state_error("Failed review record is invalid")
    expected_reviewers = value["expected_reviewers"]
    if (
        not isinstance(expected_reviewers, dict)
        or set(expected_reviewers) != {"1", "2"}
    ):
        raise _state_error("Expected reviewer evidence is invalid")
    for reviewers in expected_reviewers.values():
        if (
            not isinstance(reviewers, list)
            or any(not isinstance(item, str) or not item for item in reviewers)
            or len(reviewers) != len(set(reviewers))
        ):
            raise _state_error("Expected reviewer evidence is invalid")
    round_complete = value["round_complete"]
    if (
        not isinstance(round_complete, dict)
        or set(round_complete) != {"1", "2"}
        or any(type(item) is not bool for item in round_complete.values())
    ):
        raise _state_error("Review completion evidence is invalid")
    review_leases = value["review_leases"]
    if not isinstance(review_leases, dict) or set(review_leases) != {"1", "2"}:
        raise _state_error("Review lease evidence is invalid")
    for leases in review_leases.values():
        if not isinstance(leases, dict):
            raise _state_error("Review lease evidence is invalid")
        for critic, lease in leases.items():
            if (
                not isinstance(critic, str)
                or not critic
                or not isinstance(lease, dict)
                or set(lease) != {"owner", "expires_at"}
                or not isinstance(lease["owner"], str)
                or not lease["owner"]
                or type(lease["expires_at"]) not in {int, float}
                or not math.isfinite(lease["expires_at"])
                or lease["expires_at"] <= 0
            ):
                raise _state_error("Review lease evidence is invalid")
    decisions = value["decisions"]
    if not isinstance(decisions, list):
        raise _state_error("Decision evidence is invalid")
    decision_keys: set[str] = set()
    for item in decisions:
        if (
            not isinstance(item, dict)
            or set(item) != {"fingerprint", "verdict", "rationale"}
            or not isinstance(item["fingerprint"], str)
            or not item["fingerprint"]
            or item["fingerprint"] in decision_keys
            or item["verdict"] not in _VERDICTS
            or not isinstance(item["rationale"], str)
            or not item["rationale"]
        ):
            raise _state_error("Decision evidence is invalid")
        decision_keys.add(item["fingerprint"])
    gaps = value["review_gaps"]
    if (
        not isinstance(gaps, list)
        or any(not isinstance(item, str) or not item for item in gaps)
        or len(gaps) != len(set(gaps))
    ):
        raise _state_error("Review-gap evidence is invalid")
    return value


class Workflow:
    def __init__(
        self,
        store: StateStore,
        config: Config,
        *,
        providers: Mapping[str, ProviderAdapter] | None = None,
        transports: Mapping[str, TransportAdapter] | None = None,
        project: Path | None = None,
        review_coverage_gap: bool = False,
        require_managed_packets: bool = False,
    ):
        self.store = store
        self.config = config
        self.providers = dict(providers or {})
        self.transports = dict(transports or {})
        self.project = Path(project or store.root.parent).resolve()
        self.review_coverage_gap = bool(review_coverage_gap)
        self.require_managed_packets = bool(require_managed_packets)
        self._lease_tokens: dict[tuple[str, int, str], str] = {}

    def create_run(self, request: str, *, run_id: str | None = None) -> RunState:
        digest = _request_digest(request)
        identity = run_id or f"run-{time.time_ns():x}-{digest[:8]}"
        state = self.store.create(identity, digest)
        run_directory = self._run_directory(identity)
        try:
            _atomic_text(run_directory / "request.md", request)
            _atomic_json(
                run_directory / "workflow.json",
                {
                    "format_version": 1,
                    "request_digest": digest,
                    "local_verifications": [],
                    "rounds": {"1": [], "2": []},
                    "expected_reviewers": {"1": [], "2": []},
                    "round_complete": {"1": False, "2": False},
                    "review_leases": {"1": {}, "2": {}},
                    "decisions": [],
                    "review_gaps": [],
                },
            )
        except Exception:
            raise _state_error("Run evidence could not be initialized", run_id=identity) from None
        return state

    def mark_implemented(self, run: RunState) -> None:
        self._transition(run, {"CREATED"}, "IMPLEMENTED")

    def record_local_verification(
        self, run: RunState, passed: bool, output: str, *, new_review_surface: bool = False
    ) -> None:
        if type(passed) is not bool or not isinstance(output, str):
            raise _state_error("Local verification record is invalid")
        if run.phase not in {"IMPLEMENTED", "PATCHED"}:
            raise _state_error("Local verification is not allowed in current phase", phase=run.phase)
        source_phase = run.phase

        def record(metadata: dict) -> None:
            metadata["local_verifications"].append(
                {
                    "phase": source_phase,
                    "passed": passed,
                    "output": output,
                    "new_review_surface": bool(new_review_surface),
                }
            )

        if passed:
            target = "LOCAL_VERIFIED" if source_phase == "IMPLEMENTED" else "LOCAL_REVERIFIED"
            self._commit(run, {source_phase}, target, metadata_update=record)
        else:
            self._metadata_only(run, {source_phase}, record)

    def record_round_one(
        self, run: RunState, reviews: Sequence[ReviewResult | CriticOutcome]
    ) -> None:
        if run.phase == "LOCAL_VERIFIED":
            self._commit(
                run,
                {"LOCAL_VERIFIED"},
                "REVIEW_ROUND_1",
                round_number=1,
                metadata_update=lambda metadata: self._record_reviews(
                    metadata, run.run_id, 1, reviews
                ),
            )
            return
        if run.phase == "REVIEW_ROUND_1":
            self._metadata_only(
                run,
                {"REVIEW_ROUND_1"},
                lambda metadata, current: self._record_reviews(
                    metadata, run.run_id, 1, reviews, current
                ),
                with_state=True,
            )
            return
        raise _state_error("Round one requires successful local verification", phase=run.phase)

    def round_one_jobs(
        self,
        run: RunState,
        critics: Sequence[str],
        *,
        expected_critics: Sequence[str] | None = None,
    ) -> list[CriticJob]:
        if run.phase != "LOCAL_VERIFIED":
            raise _state_error("Round one requires successful local verification", phase=run.phase)
        unique: list[str] = []
        for critic in critics:
            if critic not in unique:
                unique.append(critic)
        expected = list(dict.fromkeys(expected_critics or unique))
        if any(critic not in expected for critic in unique):
            raise _state_error("Launched reviewer is outside expected reviewer set")
        self.verify_review_packets(run, unique, round_number=1)
        jobs = [self._job(run, critic, 1) for critic in unique]
        self._commit(
            run,
            {"LOCAL_VERIFIED"},
            "REVIEW_ROUND_1",
            round_number=1,
            metadata_update=lambda metadata: self._claim_reviewers(
                metadata, run.run_id, 1, expected, unique
            ),
        )
        return jobs

    def resume_review_jobs(
        self,
        run: RunState,
        round_number: int,
        critics: Sequence[str] | None = None,
    ) -> tuple[list[CriticOutcome], list[CriticJob]]:
        expected_phase = "REVIEW_ROUND_1" if round_number == 1 else "REVIEW_ROUND_2"
        if run.phase != expected_phase:
            raise _state_error("Review round is not resumable", phase=run.phase)
        origin_run_id, origin_digest = _state_origin(run)
        round_key = str(round_number)
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            current = self.store.resume(origin_run_id, origin_digest)
            if current.phase != expected_phase or run.phase != current.phase:
                raise _state_error("Review resume claim is stale", phase=current.phase)
            metadata = self._load_metadata_unlocked(origin_run_id, origin_digest)
            if metadata["round_complete"][round_key]:
                raise _state_error("Review round results are already recorded")
            expected = list(metadata["expected_reviewers"][round_key])
            if not expected:
                raise _state_error("Review round has no persisted reviewer claim")
            leases = metadata["review_leases"][round_key]
            claimed = [critic for critic in expected if critic in leases]
            if critics is not None and set(dict.fromkeys(critics)) != set(claimed):
                raise _state_error("Resume must include every claimed reviewer")
            self._verify_review_packets_unlocked(
                current, claimed, round_number=round_number
            )
            active: list[str] = []
            pending: list[str] = []
            now = time.time()
            for critic in expected:
                critic_state = current.critics.get(critic)
                if (
                    critic_state is not None
                    and critic_state.rounds.get(round_key) == "completed"
                ):
                    lease = metadata["review_leases"][round_key].get(critic)
                    if isinstance(lease, dict):
                        self._lease_tokens[(run.run_id, round_number, critic)] = lease["owner"]
                    continue
                lease = metadata["review_leases"][round_key].get(critic)
                if lease is None:
                    continue
                if isinstance(lease, dict) and lease["expires_at"] > now:
                    active.append(critic)
                else:
                    pending.append(critic)
            if active:
                raise _state_error(
                    "Review reservation still has an active lease",
                    critics=active,
                )
            for critic in pending:
                lease = self._new_lease()
                metadata["review_leases"][round_key][critic] = lease
                self._lease_tokens[(run.run_id, round_number, critic)] = lease["owner"]
            if pending:
                _validate_metadata(metadata, origin_digest)
                _atomic_json(
                    self._run_directory(origin_run_id) / "workflow.json",
                    metadata,
                )
            _sync_state(run, current)
        recovered: list[CriticOutcome] = []
        jobs: list[CriticJob] = []
        for critic in expected:
            critic_state = run.critics.get(critic)
            if (
                critic_state is not None
                and critic_state.rounds.get(str(round_number)) == "completed"
            ):
                recovered.append(
                    self._load_completed_outcome(
                        run, critic, round_number, critic_state.calls
                    )
                )
            elif critic in metadata["review_leases"][round_key]:
                jobs.append(self._job(run, critic, round_number))
        return recovered, jobs

    def record_decisions(self, run: RunState, decisions: Sequence[Decision]) -> None:
        if run.phase not in {"REVIEW_ROUND_1", "REVIEW_ROUND_2"}:
            raise _state_error("Decisions require completed review round", phase=run.phase)
        decision_round = "1" if run.phase == "REVIEW_ROUND_1" else "2"
        if not self._metadata(run)["round_complete"][decision_round]:
            raise _state_error("Review decisions are blocked while dispatch is active")
        expected = self.finding_fingerprints(run)
        seen: set[str] = set()
        supplied: dict[str, dict] = {}
        for decision in decisions:
            if (
                not isinstance(decision, Decision)
                or decision.fingerprint not in expected
                or decision.fingerprint in seen
                or decision.verdict not in _VERDICTS
                or not decision.rationale
            ):
                raise _state_error("Review decision is invalid")
            seen.add(decision.fingerprint)
            supplied[decision.fingerprint] = asdict(decision)
        metadata = self._metadata(run)
        merged = {
            item["fingerprint"]: item for item in metadata["decisions"]
        }
        merged.update(supplied)
        if set(merged) != set(expected):
            raise _state_error("Every finding requires exactly one decision")
        records = [merged[fingerprint] for fingerprint in expected]
        target = "TRIAGED" if run.phase == "REVIEW_ROUND_1" else "FINAL_DECISION"
        self._commit(
            run,
            {run.phase},
            target,
            metadata_update=lambda metadata: metadata.__setitem__("decisions", records),
        )

    def mark_patched(self, run: RunState) -> None:
        self._transition(run, {"TRIAGED"}, "PATCHED")

    def prepare_round_two(
        self,
        run: RunState,
        *,
        output_changed: bool = False,
        supplied_context: bool = False,
        new_review_surface: bool = False,
        critics: Sequence[str] | None = None,
    ) -> list[CriticJob] | None:
        metadata = self._metadata(run)
        persisted_new_surface = any(
            item["phase"] == "PATCHED"
            and item["passed"]
            and item["new_review_surface"]
            for item in metadata["local_verifications"]
        )
        if new_review_surface and not persisted_new_surface:
            raise _state_error("New review surface must be persisted during local reverification")
        gate = bool(output_changed or supplied_context or persisted_new_surface)
        if not gate:
            if run.phase not in {"TRIAGED", "LOCAL_REVERIFIED"}:
                raise _state_error("Round two decision is not allowed", phase=run.phase)
            self._transition(run, {run.phase}, "FINAL_DECISION")
            return None
        if run.phase != "LOCAL_REVERIFIED":
            raise _state_error("Round two input must be locally reverified before dispatch")

        affected = self._affected_critics(
            run,
            output_changed=output_changed,
            supplied_context=supplied_context,
            new_review_surface=persisted_new_surface,
        )
        if critics is not None:
            requested = list(dict.fromkeys(critics))
            if set(requested) != set(affected):
                raise _state_error("Round two must include every affected reviewer")
        if not affected:
            self._transition(run, {run.phase}, "FINAL_DECISION")
            return None
        self.verify_review_packets(run, affected, round_number=2)
        jobs = [self._job(run, critic, 2) for critic in affected]
        self._commit(
            run,
            {run.phase},
            "REVIEW_ROUND_2",
            round_number=2,
            metadata_update=lambda metadata: self._claim_reviewers(
                metadata, run.run_id, 2, affected, affected
            ),
        )
        return jobs

    def record_round_two(
        self, run: RunState, reviews: Sequence[ReviewResult | CriticOutcome]
    ) -> None:
        if run.phase != "REVIEW_ROUND_2":
            raise _state_error("Round two results are not expected", phase=run.phase)
        self._metadata_only(
            run,
            {"REVIEW_ROUND_2"},
            lambda metadata, current: self._record_reviews(
                metadata, run.run_id, 2, reviews, current
            ),
            with_state=True,
        )
        if not self._unresolved_fingerprints(run):
            self._transition(
                run, {"REVIEW_ROUND_2"}, "FINAL_DECISION", round_number=2
            )

    def finalize(self, run: RunState) -> FinalResult:
        if run.phase == "REVIEW_ROUND_1":
            if not self._metadata(run)["round_complete"]["1"]:
                raise _state_error("Round one dispatch is still active")
            if self.finding_fingerprints(run):
                raise _state_error("Findings require primary decisions before finalization")
            self.record_decisions(run, [])
        if run.phase in {"TRIAGED", "LOCAL_REVERIFIED"}:
            self._transition(run, {run.phase}, "FINAL_DECISION")
        if run.phase == "REVIEW_ROUND_2":
            if not self._metadata(run)["round_complete"]["2"]:
                raise _state_error("Round two dispatch is still active")
            if self._unresolved_fingerprints(run):
                raise _state_error("Round two findings require primary decisions")
            self._transition(run, {"REVIEW_ROUND_2"}, "FINAL_DECISION", round_number=2)
        if run.phase != "FINAL_DECISION":
            raise _state_error("Run is not ready for finalization", phase=run.phase)

        metadata = self._metadata(run)
        accepted = {
            item["fingerprint"]
            for item in metadata["decisions"]
            if item["verdict"] == "accepted"
        }
        findings = tuple(
            item.finding
            for item in self._deduplicated(run)
            if item.fingerprint in accepted
        )
        gaps = tuple(metadata["review_gaps"])
        result = FinalResult(
            "COMPLETED_WITH_REVIEW_GAP" if gaps else "COMPLETED",
            findings,
            gaps,
        )
        self._transition(run, {"FINAL_DECISION"}, "DONE", status=result.status)
        return result

    def resume(self, run_id: str, request: str) -> RunState:
        digest = _request_digest(request)
        with self.store.run_lock(run_id):
            self._recover_unlocked(run_id, digest)
            current = self.store.resume(run_id, digest)
            if current.packet_digest:
                self._verify_review_packets_unlocked(current, ())
            return current

    def finding_fingerprints(self, run: RunState) -> tuple[str, ...]:
        return tuple(item.fingerprint for item in self._deduplicated(run))

    def commit_packet_revision(
        self,
        run: RunState,
        round_number: int,
        build: Callable[[Path], Mapping[str, str]],
    ) -> None:
        if type(round_number) is not int or round_number not in {1, 2}:
            raise _state_error("Packet round is invalid")
        origin_run_id, origin_digest = _state_origin(run)
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            current = self.store.resume(origin_run_id, origin_digest)
            if current.phase != run.phase or current.phase in {
                "REVIEW_ROUND_1",
                "REVIEW_ROUND_2",
                "DONE",
            }:
                raise _state_error("Packet digest update is stale", phase=current.phase)
            revision = secrets.token_hex(16)
            revision_root = (
                self._run_directory(origin_run_id)
                / "packet-revisions"
                / revision
            )
            try:
                packet_digests = build(revision_root)
            except BaseException:
                shutil.rmtree(revision_root, ignore_errors=True)
                raise
            if not isinstance(packet_digests, Mapping) or not packet_digests:
                shutil.rmtree(revision_root, ignore_errors=True)
                raise _state_error("Packet digest set is invalid")
            normalized: dict[str, str] = {}
            for critic, digest in packet_digests.items():
                if (
                    not isinstance(critic, str)
                    or not critic
                    or "/" in critic
                    or "\\" in critic
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    shutil.rmtree(revision_root, ignore_errors=True)
                    raise _state_error("Packet digest set is invalid")
                normalized[critic] = digest
            packet_set = {
                "format_version": 1,
                "round_number": round_number,
                "revision": revision,
                "packets": dict(sorted(normalized.items())),
            }
            set_digest = hashlib.sha256(
                json.dumps(
                    packet_set,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            packet_set_path = (
                self._run_directory(origin_run_id)
                / "packet-sets"
                / f"{set_digest}.json"
            )
            _atomic_json(packet_set_path, packet_set)
            current.packet_digest = set_digest
            try:
                self.store._save_atomic_unlocked(current, origin_run_id)
            except Exception:
                try:
                    packet_set_path.unlink()
                except OSError:
                    pass
                shutil.rmtree(revision_root, ignore_errors=True)
                raise
            _sync_state(run, current)

    def verify_review_packets(
        self,
        run: RunState,
        critics: Sequence[str],
        *,
        round_number: int | None = None,
    ) -> None:
        origin_run_id, origin_digest = _state_origin(run)
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            current = self.store.resume(origin_run_id, origin_digest)
            if current.phase != run.phase:
                raise _state_error("Packet verification is stale", phase=current.phase)
            self._verify_review_packets_unlocked(
                current, critics, round_number=round_number
            )
            _sync_state(run, current)

    def _verify_review_packets_unlocked(
        self,
        run: RunState,
        critics: Sequence[str],
        *,
        round_number: int | None = None,
    ) -> None:
        if not run.packet_digest:
            if self.require_managed_packets:
                raise MaoError(
                    "PACKET_PATH_INVALID",
                    "Managed packet set is required before review",
                    {},
                )
            return
        packet_set_path = (
            self._run_directory(run.run_id)
            / "packet-sets"
            / f"{run.packet_digest}.json"
        )
        try:
            packet_set = json.loads(packet_set_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise MaoError(
                "PACKET_PATH_INVALID", "Packet set could not be loaded", {}
            ) from None
        if (
            not isinstance(packet_set, dict)
            or set(packet_set)
            != {"format_version", "round_number", "revision", "packets"}
            or packet_set["format_version"] != 1
            or packet_set["round_number"] not in {1, 2}
            or not isinstance(packet_set["packets"], dict)
            or not packet_set["packets"]
            or not isinstance(packet_set["revision"], str)
            or len(packet_set["revision"]) != 32
            or any(
                character not in "0123456789abcdef"
                for character in packet_set["revision"]
            )
        ):
            raise MaoError("PACKET_PATH_INVALID", "Packet set is invalid", {})
        canonical = json.dumps(
            packet_set, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != run.packet_digest:
            raise MaoError("PACKET_PATH_INVALID", "Packet set digest is invalid", {})
        if round_number is not None and packet_set["round_number"] != round_number:
            raise MaoError("PACKET_PATH_INVALID", "Packet round does not match", {})
        if any(critic not in packet_set["packets"] for critic in critics):
            raise MaoError("PACKET_PATH_INVALID", "Required critic packet is missing", {})
        for critic, expected_digest in packet_set["packets"].items():
            if not isinstance(critic, str) or not isinstance(expected_digest, str):
                raise MaoError("PACKET_PATH_INVALID", "Packet set is invalid", {})
            digest = validate_packet(
                self._run_directory(run.run_id)
                / "packet-revisions"
                / packet_set["revision"]
                / critic
            )
            if digest != expected_digest:
                raise MaoError(
                    "PACKET_PATH_INVALID",
                    "Packet digest does not match recorded run digest",
                    {"critic": critic},
                )

    def _deduplicated(self, run: RunState):
        metadata = self._metadata(run)
        attributed: list[tuple[str, Finding]] = []
        for round_key in ("1", "2"):
            for record in metadata["rounds"][round_key]:
                review = record.get("review")
                if record.get("status") != "completed" or not isinstance(review, dict):
                    continue
                for value in review.get("findings", []):
                    attributed.append((record["critic"], _finding(value)))
        return deduplicate_findings(attributed)

    def _unresolved_fingerprints(self, run: RunState) -> tuple[str, ...]:
        metadata = self._metadata(run)
        decided = {item["fingerprint"] for item in metadata["decisions"]}
        return tuple(
            fingerprint
            for fingerprint in self.finding_fingerprints(run)
            if fingerprint not in decided
        )

    def _record_reviews(
        self,
        metadata: dict,
        run_id: str,
        round_number: int,
        reviews: Sequence[ReviewResult | CriticOutcome],
        current: RunState | None = None,
    ) -> None:
        if metadata["rounds"][str(round_number)]:
            raise _state_error("Review round evidence is already recorded")
        provider_names = tuple(
            name for name in self.providers if name != self.config.primary_provider
        )
        round_key = str(round_number)
        expected = metadata["expected_reviewers"][round_key]
        was_claimed = bool(expected)
        if not expected:
            expected.extend(provider_names)
        records = [
            _review_record(
                expected[index] if index < len(expected) else f"reviewer-{index + 1}",
                outcome,
            )
            for index, outcome in enumerate(reviews)
        ]
        if not expected:
            expected.extend(record["critic"] for record in records)
        observed_names = [record["critic"] for record in records]
        if len(observed_names) != len(set(observed_names)) or any(
            name not in expected for name in observed_names
        ):
            raise _state_error("Review outcomes do not match claimed reviewers")
        leases = metadata["review_leases"][round_key]
        if was_claimed:
            for critic in observed_names:
                lease = leases.get(critic)
                owner = self._lease_tokens.get((run_id, round_number, critic))
                if not isinstance(lease, dict) or owner != lease["owner"]:
                    raise _state_error(
                        "Review outcome lease owner is stale",
                        critic=critic,
                        round_number=round_number,
                    )
                critic_state = current.critics.get(critic) if current is not None else None
                completed = (
                    critic_state is not None
                    and critic_state.rounds.get(round_key) == "completed"
                )
                if lease["expires_at"] <= time.time() and not completed:
                    raise _state_error(
                        "Review outcome lease is expired",
                        critic=critic,
                        round_number=round_number,
                    )
        metadata["rounds"][round_key] = records
        gaps = list(metadata["review_gaps"])
        observed = {record["critic"] for record in records}
        for critic in expected:
            if critic not in observed and critic not in gaps:
                gaps.append(critic)
        for record in records:
            if record["status"] != "completed" and record["critic"] not in gaps:
                gaps.append(record["critic"])
        if self.review_coverage_gap and "distinct-external-vendor" not in gaps:
            gaps.append("distinct-external-vendor")
        metadata["review_gaps"] = gaps
        if round_number == 2:
            observed_fingerprints = {
                finding_fingerprint(_finding(value))
                for record in records
                if record["status"] == "completed"
                for value in record["review"]["findings"]
            }
            metadata["decisions"] = [
                decision
                for decision in metadata["decisions"]
                if decision["fingerprint"] not in observed_fingerprints
            ]
        metadata["round_complete"][round_key] = True
        metadata["review_leases"][round_key] = {}

    def _new_lease(self) -> dict:
        return {
            "owner": secrets.token_hex(16),
            "expires_at": time.time() + self.config.timeout_seconds,
        }

    def _claim_reviewers(
        self,
        metadata: dict,
        run_id: str,
        round_number: int,
        expected: Sequence[str],
        launched: Sequence[str],
    ) -> None:
        round_key = str(round_number)
        if metadata["expected_reviewers"][round_key] or metadata["review_leases"][round_key]:
            raise _state_error("Review round is already claimed")
        metadata["expected_reviewers"][round_key] = list(expected)
        leases = {critic: self._new_lease() for critic in launched}
        metadata["review_leases"][round_key] = leases
        for critic, lease in leases.items():
            self._lease_tokens[(run_id, round_number, critic)] = lease["owner"]

    def complete_owned_call(
        self,
        run: RunState,
        critic: str,
        round_number: int,
    ) -> None:
        origin_run_id, origin_digest = _state_origin(run)
        owner = self._lease_tokens.get((origin_run_id, round_number, critic))
        if owner is None:
            raise _state_error("Review completion has no lease owner", critic=critic)
        round_key = str(round_number)
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            metadata = self._load_metadata_unlocked(origin_run_id, origin_digest)
            lease = metadata["review_leases"][round_key].get(critic)
            if not isinstance(lease, dict) or lease["owner"] != owner:
                raise _state_error(
                    "Review completion lease owner is stale",
                    critic=critic,
                    round_number=round_number,
                )
            if lease["expires_at"] <= time.time():
                raise _state_error(
                    "Review completion lease is expired",
                    critic=critic,
                    round_number=round_number,
                )
            current = self.store.resume(origin_run_id, origin_digest)
            critic_state = current.critics.get(critic)
            if (
                critic_state is None
                or critic_state.rounds.get(round_key) != "reserved"
            ):
                raise _state_error(
                    "Review completion has no active reservation",
                    critic=critic,
                    round_number=round_number,
                )
            critic_state.rounds[round_key] = "completed"
            self.store._save_atomic_unlocked(current, origin_run_id)
            _sync_state(run, current)

    def _load_completed_outcome(
        self,
        run: RunState,
        critic: str,
        round_number: int,
        call_number: int,
    ) -> CriticOutcome:
        path = (
            self._run_directory(run.run_id)
            / f"round-{round_number}"
            / critic
            / f"attempt-{call_number}"
            / "review.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            review = parse_review(value)
        except (OSError, UnicodeError, json.JSONDecodeError, MaoError):
            error = {
                "error": {
                    "code": "INVALID_RESULT",
                    "message": "Completed critic evidence could not be recovered",
                    "details": {},
                }
            }
            return CriticOutcome(critic, call_number, "failed", None, error)
        return CriticOutcome(critic, call_number, "completed", review, None)

    def _affected_critics(
        self,
        run: RunState,
        *,
        output_changed: bool,
        supplied_context: bool,
        new_review_surface: bool,
    ) -> list[str]:
        metadata = self._metadata(run)
        decisions = {item["fingerprint"]: item["verdict"] for item in metadata["decisions"]}
        selected: list[str] = []
        for item in self._deduplicated(run):
            include = (
                (output_changed and decisions.get(item.fingerprint) == "accepted")
                or (supplied_context and decisions.get(item.fingerprint) == "needs-proof")
            )
            if include:
                for provider in item.providers:
                    if provider in self.providers and provider not in selected:
                        selected.append(provider)
        if new_review_surface:
            for record in metadata["rounds"]["1"]:
                critic = record["critic"]
                if record["status"] == "completed" and critic in self.providers and critic not in selected:
                    selected.append(critic)
        return selected

    def _job(self, run: RunState, critic: str, round_number: int) -> CriticJob:
        provider = self.providers.get(critic)
        transport = self.transports.get(self.config.transport)
        if provider is None or transport is None:
            raise _state_error("Reviewer provider or transport is unavailable", critic=critic)
        if run.packet_digest:
            packet_set_path = (
                self._run_directory(run.run_id)
                / "packet-sets"
                / f"{run.packet_digest}.json"
            )
            try:
                packet_set = json.loads(packet_set_path.read_text(encoding="utf-8"))
                revision = packet_set["revision"]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                raise MaoError(
                    "PACKET_PATH_INVALID", "Packet set could not be loaded", {}
                ) from None
            packet_directory = (
                self._run_directory(run.run_id)
                / "packet-revisions"
                / revision
                / critic
            )
        else:
            packet_directory = self._run_directory(run.run_id) / "packet" / critic
        models = {
            "codex": self.config.codex_model,
            "claude": self.config.claude_model,
            "antigravity": self.config.antigravity_model,
        }
        return CriticJob(
            critic,
            round_number,
            provider,
            transport,
            InvocationRequest(
                provider=provider.name,
                model=models[provider.name],
                packet=packet_directory / "prompt.md",
                schema=Path(__file__).parents[1] / "review.schema.json",
                cwd=packet_directory,
                timeout_seconds=self.config.timeout_seconds,
                run_id=run.run_id,
            ),
        )

    def _transition(
        self,
        run: RunState,
        sources: set[str],
        target: str,
        *,
        round_number: int | None = None,
        status: str | None = None,
    ) -> None:
        self._commit(
            run,
            sources,
            target,
            round_number=round_number,
            status=status,
        )

    def _commit(
        self,
        run: RunState,
        sources: set[str],
        target: str,
        *,
        round_number: int | None = None,
        status: str | None = None,
        metadata_update=None,
    ) -> None:
        origin_run_id, origin_digest = _state_origin(run)
        metadata_path = self._run_directory(origin_run_id) / "workflow.json"
        transaction_path = self._run_directory(origin_run_id) / "workflow-transaction.json"
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            current = self.store.resume(origin_run_id, origin_digest)
            if current.phase not in sources or run.phase != current.phase:
                raise _state_error(
                    "Illegal or stale workflow transition",
                    source=current.phase,
                    target=target,
                )
            if current.phase != "CREATED" and target not in TRANSITIONS.get(current.phase, set()):
                raise _state_error(
                    "Workflow transition is not in transition table",
                    source=current.phase,
                    target=target,
                )
            original_metadata = self._load_metadata_unlocked(origin_run_id, origin_digest)
            metadata = copy.deepcopy(original_metadata)
            if metadata_update is not None:
                metadata_update(metadata)
                _validate_metadata(metadata, origin_digest)
                _atomic_json(
                    transaction_path,
                    {
                        "format_version": 1,
                        "run_id": origin_run_id,
                        "request_digest": origin_digest,
                        "source_phase": current.phase,
                        "target_phase": target,
                        "old_metadata": original_metadata,
                        "new_metadata": metadata,
                    },
                )
                _atomic_json(metadata_path, metadata)
            current.phase = target
            if round_number is not None:
                current.round_number = round_number
            if status is not None:
                current.status = status
            try:
                self.store._save_atomic_unlocked(current, origin_run_id)
            except Exception:
                if metadata_update is not None:
                    _atomic_json(metadata_path, original_metadata)
                    try:
                        transaction_path.unlink()
                    except OSError:
                        pass
                raise
            if metadata_update is not None:
                try:
                    transaction_path.unlink()
                except OSError:
                    pass
            _sync_state(run, current)

    def _metadata_only(
        self,
        run: RunState,
        phases: set[str],
        update,
        *,
        with_state: bool = False,
    ) -> None:
        origin_run_id, origin_digest = _state_origin(run)
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            current = self.store.resume(origin_run_id, origin_digest)
            if current.phase not in phases or run.phase != current.phase:
                raise _state_error(
                    "Workflow evidence update is stale",
                    source=current.phase,
                )
            metadata = self._load_metadata_unlocked(origin_run_id, origin_digest)
            if with_state:
                update(metadata, current)
            else:
                update(metadata)
            _validate_metadata(metadata, origin_digest)
            _atomic_json(self._run_directory(origin_run_id) / "workflow.json", metadata)
            _sync_state(run, current)

    def _run_directory(self, run_id: str) -> Path:
        return self.store.runs_directory / run_id

    def _metadata(self, run: RunState) -> dict:
        origin_run_id, origin_digest = _state_origin(run)
        with self.store.run_lock(origin_run_id):
            self._recover_unlocked(origin_run_id, origin_digest)
            return self._load_metadata_unlocked(origin_run_id, origin_digest)

    def _load_metadata_unlocked(self, run_id: str, request_digest: str) -> dict:
        try:
            value = json.loads(
                (self._run_directory(run_id) / "workflow.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise _state_error("Workflow evidence could not be loaded", run_id=run_id) from None
        return _validate_metadata(value, request_digest)

    def _recover_unlocked(self, run_id: str, request_digest: str) -> None:
        transaction_path = self._run_directory(run_id) / "workflow-transaction.json"
        if not transaction_path.exists():
            return
        try:
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise _state_error("Workflow transaction journal is invalid", run_id=run_id) from None
        if (
            not isinstance(transaction, dict)
            or set(transaction)
            != {
                "format_version",
                "run_id",
                "request_digest",
                "source_phase",
                "target_phase",
                "old_metadata",
                "new_metadata",
            }
            or transaction["format_version"] != 1
            or transaction["run_id"] != run_id
            or transaction["request_digest"] != request_digest
            or not isinstance(transaction["source_phase"], str)
            or not isinstance(transaction["target_phase"], str)
        ):
            raise _state_error("Workflow transaction journal is invalid", run_id=run_id)
        old_metadata = _validate_metadata(transaction["old_metadata"], request_digest)
        new_metadata = _validate_metadata(transaction["new_metadata"], request_digest)
        current = self.store.resume(run_id, request_digest)
        if current.phase == transaction["source_phase"]:
            recovered = old_metadata
        elif current.phase == transaction["target_phase"]:
            recovered = new_metadata
        else:
            raise _state_error(
                "Workflow transaction does not match authoritative phase",
                run_id=run_id,
            )
        _atomic_json(self._run_directory(run_id) / "workflow.json", recovered)
        try:
            transaction_path.unlink()
        except OSError:
            pass

    def _write_metadata(self, run: RunState, metadata: dict) -> None:
        run_id, _digest = _state_origin(run)
        _atomic_json(self._run_directory(run_id) / "workflow.json", metadata)
