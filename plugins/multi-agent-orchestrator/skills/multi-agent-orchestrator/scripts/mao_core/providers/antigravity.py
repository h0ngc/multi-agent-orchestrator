from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tempfile

from mao_core.errors import MaoError
from mao_core.models import classify_vendor
from mao_core.process import ProcessResult, run_process
from mao_core.providers.base import ModelCandidate, ModelIdentity


_PROBE_PROMPT = "Reply with exact text OK."


class AntigravityAdapter:
    name = "antigravity"

    def __init__(self, executable: str = "agy", timeout_seconds: int = 300):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def detect(self) -> dict:
        executable = _resolve_executable(self.executable)
        if executable is None:
            return {"status": "unavailable", "executable": self.executable}
        with tempfile.TemporaryDirectory(prefix="mao-agy-detect-") as directory:
            result = run_process([executable, "--version"], Path(directory), 10)
        version = result.stdout.strip() if result.exit_code == 0 else None
        return {"status": "available", "executable": executable, "version": version}

    def check_auth(self) -> dict:
        return {
            "status": "unknown",
            "reason": "Antigravity exposes no non-billable auth check used by this adapter",
        }

    def list_models(self) -> list[ModelCandidate]:
        with tempfile.TemporaryDirectory(prefix="mao-agy-models-") as directory:
            result = run_process(
                [self.executable, "models"], Path(directory), min(self.timeout_seconds, 30)
            )
        if result.timed_out or result.exit_code != 0:
            raise MaoError(
                "MODEL_LIST_UNSUPPORTED",
                "Antigravity model list could not be read",
                {"provider": self.name, "source": "agy models"},
            )
        return [
            ModelCandidate(
                requested=model,
                vendor=classify_vendor(self.name, model),
                source="agy models",
                exhaustive=True,
            )
            for model in _models_from_output(result.stdout)
        ]

    def validate_model(self, model: str, cwd: Path) -> ModelIdentity:
        del cwd
        args = [
            self.executable,
            "--print",
            _PROBE_PROMPT,
            "--model",
            model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]
        with tempfile.TemporaryDirectory(prefix="mao-agy-probe-") as directory:
            result = run_process(args, Path(directory), self.timeout_seconds)
        payload = _load_probe(result, self.name, model)
        probe_text = payload.get("result")
        if not isinstance(probe_text, str):
            probe_text = payload.get("response")
        if not isinstance(probe_text, str) or probe_text.strip() not in {"OK", "OK."}:
            _raise_model_unavailable(self.name, model)
        resolved = payload.get("model")
        reported = isinstance(resolved, str) and bool(resolved)
        if not reported:
            resolved = model
        self.last_resolution_source = (
            "provider_reported" if reported else "requested_fallback"
        )
        return ModelIdentity(
            provider=self.name,
            requested=model,
            resolved=resolved,
            vendor=classify_vendor(self.name, resolved),
            verified=True,
        )

    def invoke(
        self,
        model: str,
        packet: Path,
        schema: Path,
        cwd: Path,
    ) -> ProcessResult:
        args = [
            self.executable,
            "--print",
            packet.read_text(encoding="utf-8"),
            "--model",
            model,
            "--output-format",
            "json",
            "--json-schema",
            str(schema),
            "--dangerously-skip-permissions",
        ]
        return run_process(args, cwd, self.timeout_seconds)

    def parse_result(self, result: ProcessResult) -> dict:
        payload = _load_result(result, self.name)
        parsed = None
        structured = payload.get("structured_output")
        if isinstance(structured, dict):
            parsed = structured
        if parsed is None:
            value = payload.get("result")
            if value is None:
                value = payload.get("response")
            if isinstance(value, dict):
                parsed = value
            elif isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict):
                    parsed = value
        if parsed is None:
            parsed = payload
        return _with_native_usage(parsed, payload.get("usage"))

    def measure_usage(self, parsed: dict) -> dict:
        usage = parsed.get("usage", {})
        return usage if isinstance(usage, dict) else {}


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _models_from_output(stdout: str) -> list[str]:
    models: list[str] = []
    for row in stdout.splitlines():
        model = row.split("\t", 1)[0].strip()
        if _MODEL_ID.fullmatch(model) and model not in models:
            models.append(model)
    return models


def _with_native_usage(parsed: dict, usage: object) -> dict:
    if not isinstance(usage, dict):
        return parsed
    value = dict(parsed)
    value["usage"] = dict(usage)
    return value


def _resolve_executable(executable: str) -> str | None:
    if Path(executable).parent != Path("."):
        return str(Path(executable).resolve()) if Path(executable).is_file() else None
    return shutil.which(executable)


def _load_probe(result: ProcessResult, provider: str, requested: str) -> dict:
    if result.timed_out or result.exit_code != 0:
        _raise_model_unavailable(provider, requested)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        _raise_model_unavailable(provider, requested)
    if not isinstance(payload, dict):
        _raise_model_unavailable(provider, requested)
    return payload


def _load_result(result: ProcessResult, provider: str) -> dict:
    if result.timed_out:
        raise MaoError("TIMEOUT", "Provider invocation timed out", {"provider": provider})
    if result.exit_code != 0:
        raise MaoError(
            "INVALID_RESULT",
            "Provider invocation failed",
            {"provider": provider, "exit_code": result.exit_code},
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MaoError(
            "INVALID_RESULT", "Provider returned invalid JSON", {"provider": provider}
        ) from error
    if not isinstance(payload, dict):
        raise MaoError(
            "INVALID_RESULT", "Provider returned no JSON object", {"provider": provider}
        )
    return payload


def _raise_model_unavailable(provider: str, requested: str) -> None:
    raise MaoError(
        "MODEL_UNAVAILABLE",
        "Requested model could not be verified",
        {"provider": provider, "requested_model": requested},
    )
