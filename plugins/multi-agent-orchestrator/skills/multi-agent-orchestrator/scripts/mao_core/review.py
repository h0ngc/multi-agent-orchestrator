from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from .budget import BudgetGuard
from .errors import MaoError
from .providers.base import ProviderAdapter
from .transports.base import InvocationRequest, TransportAdapter


_SEVERITIES = ("critical", "major", "minor", "note")
_SEVERITY_RANK = {value: len(_SEVERITIES) - index for index, value in enumerate(_SEVERITIES)}
_CATEGORIES = frozenset(
    {
        "correctness",
        "requirements",
        "regression",
        "security",
        "maintainability",
        "visual",
        "accessibility",
        "other",
    }
)
_TOP_FIELDS = frozenset({"summary", "findings", "usage", "review_complete"})
_FINDING_FIELDS = frozenset(
    {
        "severity",
        "category",
        "file",
        "line",
        "evidence",
        "reason",
        "suggested_fix",
        "confidence",
        "needs_context",
    }
)
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_REDACT_ASSIGNMENT = re.compile(
    r'''(?im)(["']?(?:token|secret|password|api[_-]?key|authorization)["']?'''
    r'''\s*[:=]\s*["']?)(?:bearer\s+)?([^"'\s,}]+)'''
)
_REDACT_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_KEYS = frozenset(
    {"token", "secret", "password", "api_key", "api-key", "authorization"}
)
_FINGERPRINT_EXCLUDED = frozenset(
    {
        ".git",
        ".multi-agent-orchestrator",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "bin",
        "obj",
        "dist",
        "build",
    }
)


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    file: str
    line: int | None
    evidence: str
    reason: str
    suggested_fix: str
    confidence: float
    needs_context: tuple[str, ...]


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    findings: tuple[Finding, ...]
    usage: dict
    review_complete: bool


@dataclass(frozen=True)
class Decision:
    fingerprint: str
    verdict: str
    rationale: str


@dataclass(frozen=True)
class CriticJob:
    critic: str
    round_number: int
    provider: ProviderAdapter
    transport: TransportAdapter
    request: InvocationRequest


@dataclass(frozen=True)
class CriticOutcome:
    critic: str
    call_number: int
    status: str
    review: ReviewResult | None
    error: dict | None


@dataclass(frozen=True)
class DeduplicatedFinding:
    fingerprint: str
    finding: Finding
    providers: tuple[str, ...]


def _invalid(message: str, field: str) -> MaoError:
    return MaoError("INVALID_RESULT", message, {"field": field})


def _required_string(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise _invalid("Review field must be a string", field)
    if "\x00" in value or "\r" in value:
        raise _invalid("Review string contains a control character", field)
    return value


def _safe_relative_path(value: object, field: str) -> str:
    raw = _required_string(value, field)
    if any(ord(character) < 32 for character in raw):
        raise _invalid("Review path must be a single line", field)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid("Review path must be UTF-8 encodable", field) from None
    windows = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or windows.drive or windows.root or ".." in path.parts:
        raise _invalid("Review path must be project-relative", field)
    parts = tuple(part for part in path.parts if part != ".")
    if not parts:
        raise _invalid("Review path must name a file", field)
    return PurePosixPath(*parts).as_posix()


def _strict_json(value: object, field: str, seen: set[int] | None = None) -> Any:
    if value is None or type(value) in {str, bool, int}:
        if isinstance(value, str) and ("\x00" in value or "\r" in value):
            raise _invalid("Usage contains an invalid string", field)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid("Usage contains a non-finite number", field)
        return value
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        raise _invalid("Usage contains a cycle", field)
    if isinstance(value, dict):
        seen.add(identity)
        result: dict[str, Any] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str) or not key or "\x00" in key:
                    raise _invalid("Usage keys must be strings", field)
                result[key] = _strict_json(item, field, seen)
        finally:
            seen.remove(identity)
        return result
    if isinstance(value, (list, tuple)):
        seen.add(identity)
        try:
            return [_strict_json(item, field, seen) for item in value]
        finally:
            seen.remove(identity)
    raise _invalid("Usage must contain JSON values", field)


def _context_item(value: object, field: str) -> str:
    item = _required_string(value, field)
    if any(ord(character) < 32 for character in item) or len(item) > 500:
        raise _invalid("Context request must be concise and single-line", field)
    if item.endswith("?"):
        if (
            ".." in item
            or "/" in item
            or "\\" in item
            or not any(character.isspace() for character in item)
        ):
            raise _invalid("Context question is invalid", field)
        return item
    return _safe_relative_path(item, field)


def _parse_finding(value: object, index: int) -> Finding:
    field = f"findings[{index}]"
    if not isinstance(value, dict) or set(value) != _FINDING_FIELDS:
        raise _invalid("Finding has invalid fields", field)
    severity = value["severity"]
    category = value["category"]
    if severity not in _SEVERITIES:
        raise _invalid("Finding severity is invalid", f"{field}.severity")
    if category not in _CATEGORIES:
        raise _invalid("Finding category is invalid", f"{field}.category")
    line = value["line"]
    if line is not None and (type(line) is not int or line < 1):
        raise _invalid("Finding line must be positive or null", f"{field}.line")
    confidence = value["confidence"]
    if type(confidence) not in {int, float} or not math.isfinite(confidence):
        raise _invalid("Finding confidence must be finite", f"{field}.confidence")
    if confidence < 0 or confidence > 1:
        raise _invalid("Finding confidence is outside range", f"{field}.confidence")
    raw_context = value["needs_context"]
    if not isinstance(raw_context, list):
        raise _invalid("Finding context requests must be a list", f"{field}.needs_context")
    return Finding(
        severity=severity,
        category=category,
        file=_safe_relative_path(value["file"], f"{field}.file"),
        line=line,
        evidence=_required_string(value["evidence"], f"{field}.evidence"),
        reason=_required_string(value["reason"], f"{field}.reason"),
        suggested_fix=_required_string(
            value["suggested_fix"], f"{field}.suggested_fix"
        ),
        confidence=float(confidence),
        needs_context=tuple(
            _context_item(item, f"{field}.needs_context") for item in raw_context
        ),
    )


def parse_review(value: object) -> ReviewResult:
    if not isinstance(value, dict) or set(value) != _TOP_FIELDS:
        raise _invalid("Review result has invalid fields", "review")
    findings = value["findings"]
    if not isinstance(findings, list):
        raise _invalid("Review findings must be a list", "findings")
    if value["review_complete"] is not True:
        raise _invalid("Review completion flag must be true", "review_complete")
    usage = _strict_json(value["usage"], "usage")
    if not isinstance(usage, dict):
        raise _invalid("Review usage must be an object", "usage")
    return ReviewResult(
        summary=_required_string(value["summary"], "summary", empty=True),
        findings=tuple(_parse_finding(item, index) for index, item in enumerate(findings)),
        usage=usage,
        review_complete=value["review_complete"],
    )


def finding_fingerprint(finding: Finding) -> str:
    path = _safe_relative_path(finding.file, "finding.file")
    evidence = " ".join(finding.evidence.casefold().split())
    line_region = None if finding.line is None else finding.line // 5
    encoded = json.dumps(
        [path, line_region, finding.category.casefold(), evidence],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deduplicate_findings(
    attributed: Sequence[tuple[str, Finding]],
) -> tuple[DeduplicatedFinding, ...]:
    order: list[str] = []
    grouped: dict[str, tuple[Finding, list[str]]] = {}
    for provider, finding in attributed:
        if not isinstance(provider, str) or not provider:
            raise _invalid("Finding provider is invalid", "provider")
        fingerprint = finding_fingerprint(finding)
        if fingerprint not in grouped:
            order.append(fingerprint)
            grouped[fingerprint] = (finding, [provider])
            continue
        selected, providers = grouped[fingerprint]
        if provider not in providers:
            providers.append(provider)
        if _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[selected.severity]:
            selected = finding
        grouped[fingerprint] = (selected, providers)
    return tuple(
        DeduplicatedFinding(key, grouped[key][0], tuple(grouped[key][1]))
        for key in order
    )


def _hash_file(path: Path, digest: Any) -> None:
    if path.is_symlink():
        digest.update(b"L")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return
    digest.update(b"F")
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)


def _tree_fingerprint(project: Path) -> bytes:
    digest = hashlib.sha256()
    try:
        for root, directories, files in os.walk(project, topdown=True, followlinks=False):
            root_path = Path(root)
            kept_directories: list[str] = []
            for name in sorted(directories):
                if name in _FINGERPRINT_EXCLUDED:
                    continue
                path = root_path / name
                if path.is_symlink():
                    relative = path.relative_to(project).as_posix().encode("utf-8")
                    digest.update(len(relative).to_bytes(8, "big"))
                    digest.update(relative)
                    _hash_file(path, digest)
                else:
                    kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(files):
                if name in _FINGERPRINT_EXCLUDED:
                    continue
                path = root_path / name
                relative = path.relative_to(project).as_posix().encode("utf-8")
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                _hash_file(path, digest)
    except (OSError, UnicodeError, ValueError):
        raise MaoError(
            "CRITIC_MUTATED_WORKSPACE",
            "Workspace fingerprint could not be captured",
            {},
        ) from None
    return digest.digest()


def _git_metadata(project: Path) -> bytes:
    digest = hashlib.sha256()
    for command in (
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "diff", "--binary", "--no-ext-diff"],
        ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
    ):
        try:
            result = subprocess.run(
                command,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return b"NON_GIT"
        if result.returncode != 0:
            return b"NON_GIT"
        digest.update(result.stdout)
    return digest.digest()


def capture_workspace_fingerprint(project: Path) -> str:
    try:
        root = Path(project).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise MaoError(
            "CRITIC_MUTATED_WORKSPACE", "Workspace is unavailable", {}
        ) from None
    if not root.is_dir():
        raise MaoError(
            "CRITIC_MUTATED_WORKSPACE", "Workspace must be a directory", {}
        )
    digest = hashlib.sha256()
    digest.update(b"MAO_WORKSPACE_V1\0")
    digest.update(_git_metadata(root))
    digest.update(_tree_fingerprint(root))
    return digest.hexdigest()


def _redact(value: str) -> str:
    assigned = _REDACT_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}<redacted>", value
    )
    return _REDACT_BEARER.sub("Bearer <redacted>", assigned)


def _sanitize_error_value(value: object, key: str | None = None) -> object:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, str):
        return _redact(value)
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else "<invalid>"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_error_value(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_error_value(item) for item in value]
    return "<redacted>"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    _atomic_write(path, payload)


def _error_payload(error: Exception) -> dict:
    if isinstance(error, MaoError):
        payload = _sanitize_error_value(error.as_dict())
        if isinstance(payload, dict):
            return payload
    return MaoError(
        "SESSION_START_FAILED", "Critic invocation failed", {}
    ).as_dict()


def _review_dict(review: ReviewResult) -> dict:
    return asdict(review)


def _attempt_directory(project: Path, job: CriticJob, call_number: int) -> Path:
    return (
        project
        / ".multi-agent-orchestrator"
        / "runs"
        / job.request.run_id
        / f"round-{job.round_number}"
        / job.critic
        / f"attempt-{call_number}"
    )


def _validate_job(job: CriticJob, project: Path) -> None:
    if not _SAFE_NAME.fullmatch(job.critic):
        raise _invalid("Critic name is invalid", "critic")
    if type(job.round_number) is not int or job.round_number not in {1, 2}:
        raise _invalid("Critic round is invalid", "round_number")
    if not _SAFE_NAME.fullmatch(job.request.run_id):
        raise _invalid("Run identity is invalid", "run_id")
    if job.request.provider != job.provider.name:
        raise _invalid("Provider identity does not match request", "provider")
    if not isinstance(job.request.model, str) or not job.request.model:
        raise _invalid("Critic model is invalid", "model")
    if (
        type(job.request.timeout_seconds) not in {int, float}
        or not math.isfinite(job.request.timeout_seconds)
        or job.request.timeout_seconds <= 0
    ):
        raise _invalid("Critic timeout is invalid", "timeout_seconds")
    try:
        request_cwd = Path(job.request.cwd).resolve(strict=True)
        root = project.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise _invalid("Critic workspace is unavailable", "cwd") from None
    if request_cwd == root:
        raise _invalid("Critic workspace must be isolated from project", "cwd")
    try:
        relative_cwd = request_cwd.relative_to(root)
    except ValueError:
        relative_cwd = None
    if (
        relative_cwd is not None
        and (
            not relative_cwd.parts
            or relative_cwd.parts[0] != ".multi-agent-orchestrator"
        )
    ):
        raise _invalid("Critic workspace overlaps authoritative project", "cwd")
    for field, source in (("packet", job.request.packet), ("schema", job.request.schema)):
        try:
            candidate = Path(source)
            resolved = candidate.resolve(strict=True)
            if candidate.is_symlink() or not resolved.is_file():
                raise OSError
            if field == "packet":
                resolved.relative_to(request_cwd)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise _invalid("Critic request input is unavailable", field) from None


def _preflight(job: CriticJob, project: Path) -> dict:
    _validate_job(job, project)
    detected = job.provider.detect()
    if not isinstance(detected, dict) or detected.get("status") != "available":
        raise MaoError(
            "CLI_NOT_FOUND",
            "Critic provider executable is unavailable",
            {"provider": job.provider.name},
        )
    return detected


def _invoke_job(job: CriticJob, call_number: int, project: Path) -> CriticOutcome:
    attempt = _attempt_directory(project, job, call_number)
    try:
        _write_json(
            attempt / "envelope.json",
            {
                "call_number": call_number,
                "critic": job.critic,
                "model": job.request.model,
                "provider": job.provider.name,
                "round_number": job.round_number,
                "run_id": job.request.run_id,
            },
        )
        process = job.transport.invoke(job.provider, job.request)
        _atomic_write(attempt / "stdout.txt", _redact(process.stdout).encode("utf-8"))
        _atomic_write(attempt / "stderr.txt", _redact(process.stderr).encode("utf-8"))
        parsed = job.provider.parse_result(process)
        review = parse_review(parsed)
        usage = _strict_json(job.provider.measure_usage(parsed), "usage")
        if not isinstance(usage, dict):
            raise _invalid("Provider usage must be an object", "usage")
        _write_json(attempt / "review.json", _review_dict(review))
        _write_json(attempt / "usage.json", usage)
        return CriticOutcome(job.critic, call_number, "completed", review, None)
    except Exception as error:
        payload = _error_payload(error)
        try:
            _write_json(attempt / "error.json", payload)
        except Exception:
            pass
        return CriticOutcome(job.critic, call_number, "failed", None, payload)


def _failed_outcome(
    job: CriticJob, error: Exception, call_number: int = 0
) -> CriticOutcome:
    return CriticOutcome(
        job.critic, call_number, "failed", None, _error_payload(error)
    )


def dispatch_parallel(
    jobs: Sequence[CriticJob],
    guard: BudgetGuard,
    project: Path,
    *,
    complete_call: Callable[[CriticJob], None] | None = None,
) -> list[CriticOutcome]:
    root = Path(project).resolve(strict=True)
    baseline = capture_workspace_fingerprint(root)
    outcomes: list[CriticOutcome | None] = [None] * len(jobs)
    submitted: list[tuple[int, CriticJob, int, Future[CriticOutcome]]] = []

    with ThreadPoolExecutor(max_workers=max(1, min(2, len(jobs)))) as executor:
        for index, job in enumerate(jobs):
            try:
                _preflight(job, root)
                guard.reserve_call(job.critic, job.round_number)
                call_number = guard.calls_for(job.critic)
                future = executor.submit(_invoke_job, job, call_number, root)
                submitted.append((index, job, call_number, future))
            except Exception as error:
                outcomes[index] = _failed_outcome(job, error)
        for index, _job, call_number, future in submitted:
            try:
                outcomes[index] = future.result()
            except Exception as error:
                outcomes[index] = _failed_outcome(jobs[index], error, call_number)

    try:
        changed = capture_workspace_fingerprint(root) != baseline
    except MaoError:
        changed = True
    if changed:
        mutation = MaoError(
            "CRITIC_MUTATED_WORKSPACE",
            "Critic execution changed authoritative workspace",
            {},
        ).as_dict()
        for index, job, call_number, _future in submitted:
            outcomes[index] = CriticOutcome(job.critic, call_number, "failed", None, mutation)
            try:
                _write_json(
                    _attempt_directory(root, job, call_number) / "error.json",
                    mutation,
                )
            except Exception:
                pass
    else:
        for index, job, _call_number, _future in submitted:
            outcome = outcomes[index]
            if outcome is not None and outcome.status == "completed":
                try:
                    if complete_call is None:
                        guard.complete_call(job.critic, job.round_number)
                    else:
                        complete_call(job)
                except Exception as error:
                    payload = _error_payload(error)
                    outcomes[index] = CriticOutcome(
                        job.critic,
                        outcome.call_number,
                        "failed",
                        None,
                        payload,
                    )
                    try:
                        _write_json(
                            _attempt_directory(root, job, outcome.call_number)
                            / "error.json",
                            payload,
                        )
                    except Exception:
                        pass

    return [
        outcome
        if outcome is not None
        else CriticOutcome(jobs[index].critic, 0, "failed", None, _error_payload(RuntimeError()))
        for index, outcome in enumerate(outcomes)
    ]
