from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from mao_core.errors import MaoError
from mao_core.models import classify_vendor
from mao_core.process import ProcessResult, run_process
from mao_core.providers.base import ModelCandidate, ModelIdentity


_PROBE_PROMPT = "Reply with exact text OK."


class ClaudeAdapter:
    name = "claude"

    def __init__(self, executable: str = "claude", timeout_seconds: int = 300):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def detect(self) -> dict:
        executable = _resolve_executable(self.executable)
        if executable is None:
            return {"status": "unavailable", "executable": self.executable}
        with tempfile.TemporaryDirectory(prefix="mao-claude-detect-") as directory:
            result = run_process([executable, "--version"], Path(directory), 10)
        version = result.stdout.strip() if result.exit_code == 0 else None
        return {"status": "available", "executable": executable, "version": version}

    def check_auth(self) -> dict:
        with tempfile.TemporaryDirectory(prefix="mao-claude-auth-") as directory:
            result = run_process(
                [self.executable, "auth", "status"],
                Path(directory),
                min(self.timeout_seconds, 10),
            )
        status = "authenticated" if result.exit_code == 0 else "unauthenticated"
        return {"status": status}

    def list_models(self) -> list[ModelCandidate]:
        aliases = []
        with tempfile.TemporaryDirectory(prefix="mao-claude-models-") as directory:
            result = run_process(
                [self.executable, "--help"], Path(directory), min(self.timeout_seconds, 10)
            )
        if result.exit_code == 0:
            aliases = _aliases_from_help(result.stdout)

        candidates = [
            ModelCandidate(
                requested=alias,
                vendor=classify_vendor(self.name, alias),
                source="claude --help",
                exhaustive=False,
            )
            for alias in aliases
        ]
        seen = set(aliases)
        configured = os.environ.get("MAO_CLAUDE_CANDIDATES", "")
        for value in configured.split(","):
            requested = value.strip()
            if not requested or requested in seen:
                continue
            seen.add(requested)
            candidates.append(
                ModelCandidate(
                    requested=requested,
                    vendor=classify_vendor(self.name, requested),
                    source="MAO_CLAUDE_CANDIDATES",
                    exhaustive=False,
                )
            )
        return candidates

    def validate_model(self, model: str, cwd: Path) -> ModelIdentity:
        del cwd
        args = [
            self.executable,
            "-p",
            _PROBE_PROMPT,
            "--model",
            model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]
        with tempfile.TemporaryDirectory(prefix="mao-claude-probe-") as directory:
            result = run_process(args, Path(directory), self.timeout_seconds)
        payload = _load_probe(result, self.name, model)
        if payload.get("result") != "OK":
            _raise_model_unavailable(self.name, model)
        model_usage = payload.get("modelUsage")
        if not isinstance(model_usage, dict) or not model_usage:
            _raise_model_unavailable(self.name, model)
        resolved = next(iter(model_usage))
        self.last_resolution_source = "provider_reported"
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
            "-p",
            packet.read_text(encoding="utf-8"),
            "--model",
            model,
            "--output-format",
            "json",
            "--json-schema",
            _schema_for_cli(schema),
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


def _aliases_from_help(help_text: str) -> list[str]:
    option = re.search(
        r"--model <model>(.*?)(?=\n\s{2}(?:-|Commands:)|\Z)",
        help_text,
        flags=re.DOTALL,
    )
    if option is None:
        return []
    alias_text = option.group(1).split("or a model's full name", 1)[0]
    return list(dict.fromkeys(re.findall(r"['\"]([A-Za-z0-9_.-]+)['\"]", alias_text)))


def _with_native_usage(parsed: dict, usage: object) -> dict:
    if not isinstance(usage, dict):
        return parsed
    value = dict(parsed)
    value["usage"] = dict(usage)
    return value


def _schema_for_cli(schema: Path) -> str:
    payload = json.loads(schema.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MaoError(
            "CONFIG_INVALID",
            "Review schema must be a JSON object",
            {"provider": "claude"},
        )
    compatible = dict(payload)
    compatible.pop("$schema", None)
    return json.dumps(compatible)


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
