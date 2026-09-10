from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from mao_core.errors import MaoError
from mao_core.models import classify_vendor
from mao_core.process import ProcessResult, run_process
from mao_core.providers.base import ModelCandidate, ModelIdentity


_PROBE_PROMPT = "Reply with exact text OK."


class CodexAdapter:
    name = "codex"

    def __init__(
        self,
        executable: str = "codex",
        models_cache: Path | None = None,
        timeout_seconds: int = 300,
    ):
        self.executable = executable
        self.models_cache = models_cache
        self.timeout_seconds = timeout_seconds

    def detect(self) -> dict:
        executable = _resolve_executable(self.executable)
        if executable is None:
            return {"status": "unavailable", "executable": self.executable}
        with tempfile.TemporaryDirectory(prefix="mao-codex-detect-") as directory:
            result = run_process([executable, "--version"], Path(directory), 10)
        version = result.stdout.strip() if result.exit_code == 0 else None
        return {"status": "available", "executable": executable, "version": version}

    def check_auth(self) -> dict:
        with tempfile.TemporaryDirectory(prefix="mao-codex-auth-") as directory:
            result = run_process(
                [self.executable, "login", "status"],
                Path(directory),
                min(self.timeout_seconds, 10),
            )
        status = "authenticated" if result.exit_code == 0 else "unauthenticated"
        return {"status": status}

    def list_models(self) -> list[ModelCandidate]:
        cache = self.models_cache or _default_models_cache()
        if not cache.is_file():
            return []
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MaoError(
                "MODEL_LIST_UNSUPPORTED",
                "Codex model cache could not be read",
                {"provider": self.name, "source": "models_cache.json"},
            ) from error

        fetched_at = payload.get("fetched_at")
        source = "codex models_cache.json"
        if isinstance(fetched_at, str) and fetched_at:
            source = f"{source} fetched_at={fetched_at}"

        models = payload.get("models", [])
        candidates = []
        if isinstance(models, list):
            for item in models:
                requested = item.get("slug") if isinstance(item, dict) else None
                if isinstance(requested, str) and requested:
                    candidates.append(
                        ModelCandidate(
                            requested=requested,
                            vendor=classify_vendor(self.name, requested),
                            source=source,
                            exhaustive=False,
                        )
                    )
        return candidates

    def validate_model(self, model: str, cwd: Path) -> ModelIdentity:
        del cwd
        args = [
            self.executable,
            "exec",
            "--model",
            model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--skip-git-repo-check",
            _PROBE_PROMPT,
        ]
        with tempfile.TemporaryDirectory(prefix="mao-codex-probe-") as directory:
            result = run_process(args, Path(directory), self.timeout_seconds)
        events = _probe_events(result, self.name, model)
        response = _successful_agent_message(events)
        if response is None:
            _raise_model_unavailable(self.name, model)
        event, item = response
        reported = _direct_model(event) or _direct_model(item)
        resolved = reported or model
        self.last_resolution_source = (
            "provider_reported" if reported is not None else "requested_fallback"
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
        canonical = json.loads(schema.read_text(encoding="utf-8"))
        compatible = _codex_compatible_schema(canonical)
        with tempfile.TemporaryDirectory(prefix="mao-codex-schema-") as directory:
            compatible_path = Path(directory) / "review.schema.json"
            compatible_path.write_text(
                json.dumps(compatible, ensure_ascii=False),
                encoding="utf-8",
            )
            args = [
                self.executable,
                "exec",
                "--model",
                model,
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
                "--skip-git-repo-check",
                "--output-schema",
                str(compatible_path),
                packet.read_text(encoding="utf-8"),
            ]
            return run_process(args, cwd, self.timeout_seconds)

    def parse_result(self, result: ProcessResult) -> dict:
        events = _result_events(result, self.name)
        final_event = events[-1]
        native_usage = None
        if final_event.get("type") == "turn.completed" and isinstance(
            final_event.get("usage"), dict
        ):
            native_usage = final_event["usage"]
        for event in reversed(events):
            parsed = _structured_result(event)
            if parsed is not None:
                return _with_native_usage(parsed, native_usage)
        raise MaoError(
            "INVALID_RESULT",
            "Provider returned no structured result",
            {"provider": self.name},
        )

    def measure_usage(self, parsed: dict) -> dict:
        usage = parsed.get("usage", {})
        return usage if isinstance(usage, dict) else {}


def _default_models_cache() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return Path(codex_home) / "models_cache.json" if codex_home else Path.home() / ".codex/models_cache.json"


def _codex_compatible_schema(value: object) -> object:
    if isinstance(value, list):
        return [_codex_compatible_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    one_of = value.get("oneOf")
    if isinstance(one_of, list) and one_of and all(
        isinstance(option, dict) and isinstance(option.get("type"), str)
        for option in one_of
    ):
        return {"type": [option["type"] for option in one_of]}

    transformed = {}
    for key, item in value.items():
        if key in {"$schema", "$id", "minLength", "minimum", "maximum"}:
            continue
        if key == "const":
            transformed["type"] = (
                "boolean" if isinstance(item, bool) else "string"
            )
            transformed["enum"] = [item]
            continue
        transformed[key] = _codex_compatible_schema(item)

    enum = transformed.get("enum")
    if "type" not in transformed and isinstance(enum, list) and enum:
        if all(isinstance(item, str) for item in enum):
            transformed["type"] = "string"

    if transformed.get("type") == "object":
        properties = transformed.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        transformed["properties"] = properties
        transformed["required"] = list(properties)
        transformed["additionalProperties"] = False
    return transformed


def _resolve_executable(executable: str) -> str | None:
    if Path(executable).parent != Path("."):
        return str(Path(executable).resolve()) if Path(executable).is_file() else None
    return shutil.which(executable)


def _probe_events(result: ProcessResult, provider: str, requested: str) -> list[dict]:
    if result.timed_out or result.exit_code != 0:
        _raise_model_unavailable(provider, requested)
    try:
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError:
        _raise_model_unavailable(provider, requested)
    if not rows or not all(isinstance(row, dict) for row in rows):
        _raise_model_unavailable(provider, requested)
    return rows


def _result_events(result: ProcessResult, provider: str) -> list[dict]:
    if result.timed_out:
        raise MaoError("TIMEOUT", "Provider invocation timed out", {"provider": provider})
    if result.exit_code != 0:
        raise MaoError(
            "INVALID_RESULT",
            "Provider invocation failed",
            {"provider": provider, "exit_code": result.exit_code},
        )
    try:
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise MaoError(
            "INVALID_RESULT", "Provider returned invalid JSON", {"provider": provider}
        ) from error
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise MaoError(
            "INVALID_RESULT", "Provider returned no JSON object", {"provider": provider}
        )
    return rows


def _structured_result(payload: dict) -> dict | None:
    for key in ("structured_output", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    item = payload.get("item")
    if isinstance(item, dict) and isinstance(item.get("text"), str):
        try:
            parsed = json.loads(item["text"])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return None


def _with_native_usage(parsed: dict, usage: dict | None) -> dict:
    if usage is None:
        return parsed
    value = dict(parsed)
    value["usage"] = dict(usage)
    return value


def _successful_agent_message(events: list[dict]) -> tuple[dict, dict] | None:
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
            and item["text"].strip() == "OK"
        ):
            return event, item
    return None


def _direct_model(value: dict) -> str | None:
    for key in ("resolved_model", "model", "model_id", "modelId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _raise_model_unavailable(provider: str, requested: str) -> None:
    raise MaoError(
        "MODEL_UNAVAILABLE",
        "Requested model could not be verified",
        {"provider": provider, "requested_model": requested},
    )
