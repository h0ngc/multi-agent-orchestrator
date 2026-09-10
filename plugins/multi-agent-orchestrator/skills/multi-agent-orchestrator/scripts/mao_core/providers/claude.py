from __future__ import annotations

from collections.abc import Callable
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

    def __init__(
        self,
        executable: str = "claude",
        timeout_seconds: int = 300,
        model_menu_reader: Callable[[str, int], str] | None = None,
        effort: str = "high",
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.model_menu_reader = model_menu_reader or _read_interactive_model_menu
        self.effort = effort

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
        candidates: list[ModelCandidate] = []
        seen: set[str] = set()
        try:
            menu = self.model_menu_reader(
                self.executable,
                min(self.timeout_seconds, 15),
            )
        except (OSError, MaoError):
            menu = ""
        for requested, label in _models_from_menu(menu):
            seen.add(requested)
            candidates.append(
                ModelCandidate(
                    requested=requested,
                    vendor=classify_vendor(self.name, requested),
                    source=f"claude interactive /model: {label}",
                    exhaustive=False,
                )
            )

        aliases = []
        with tempfile.TemporaryDirectory(prefix="mao-claude-models-") as directory:
            result = run_process(
                [self.executable, "--help"], Path(directory), min(self.timeout_seconds, 10)
            )
        if result.exit_code == 0:
            aliases = _aliases_from_help(result.stdout)
        for alias in aliases:
            if alias in seen:
                continue
            seen.add(alias)
            candidates.append(
                ModelCandidate(
                    requested=alias,
                    vendor=classify_vendor(self.name, alias),
                    source="claude --help",
                    exhaustive=False,
                )
            )
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

    def list_efforts(self, model: str) -> dict:
        del model
        with tempfile.TemporaryDirectory(prefix="mao-claude-efforts-") as directory:
            result = run_process(
                [self.executable, "--help"],
                Path(directory),
                min(self.timeout_seconds, 10),
            )
        values = _efforts_from_help(result.stdout) if result.exit_code == 0 else []
        return {
            "values": values,
            "default": None,
            "source": "claude --help",
            "exhaustive": bool(values),
        }

    def validate_model(
        self, model: str, cwd: Path, effort: str | None = None
    ) -> ModelIdentity:
        del cwd
        selected_effort = effort or self.effort
        args = [
            self.executable,
            "-p",
            _PROBE_PROMPT,
            "--model",
            model,
            "--effort",
            selected_effort,
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
        effort: str | None = None,
    ) -> ProcessResult:
        selected_effort = effort or self.effort
        args = [
            self.executable,
            "-p",
            packet.read_text(encoding="utf-8"),
            "--model",
            model,
            "--effort",
            selected_effort,
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


def _efforts_from_help(help_text: str) -> list[str]:
    option = re.search(
        r"--effort(?:\s+<level>)?(.*?)(?=\n\s{2}(?:-|Commands:)|\Z)",
        help_text,
        flags=re.DOTALL,
    )
    if option is None:
        return []
    return list(
        dict.fromkeys(
            re.findall(r"\b(?:low|medium|high|xhigh|max|ultra)\b", option.group(1))
        )
    )


def _models_from_menu(menu_text: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for line in menu_text.splitlines():
        numbered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if numbered is None:
            continue
        content = re.sub(r"^\(selected\)\s+", "", numbered.group(1))
        if content.startswith("Default"):
            continue
        match = re.match(r"(Opus(?:\s+\d+(?:\.\d+)?)?\s+\(1M context\))", content)
        if match is not None:
            candidates.append(("opus[1m]", match.group(1)))
            continue
        match = re.match(r"(Sonnet(?:\s+\d+(?:\.\d+)?)?\s+\(1M context\))", content)
        if match is not None:
            candidates.append(("sonnet[1m]", match.group(1)))
            continue
        if content.startswith("Sonnet"):
            candidates.append(("sonnet", "Sonnet"))
            continue
        if content.startswith("Opus"):
            candidates.append(("opus", "Opus"))
            continue
        if content.startswith("Haiku"):
            candidates.append(("haiku", "Haiku"))
    return list(dict.fromkeys(candidates))


def _read_interactive_model_menu(executable: str, timeout_seconds: int) -> str:
    expect = shutil.which("expect")
    if expect is None:
        return ""
    script = r'''
log_user 1
set timeout [lindex $argv 1]
set executable [lindex $argv 0]
spawn -noecho $executable --ax-screen-reader --dangerously-skip-permissions --safe-mode
set menu_opened 0
expect {
    -re {Yes, I trust this folder} {
        send "y\r"
        exp_continue
    }
    -re {bypass permissions on} {
        if {!$menu_opened} {
            send "/model\r"
            set menu_opened 1
        }
        exp_continue
    }
    -re {Enter to set as default} {
        send "\033"
        after 100
        send "/exit\r"
        expect eof
    }
    timeout { exit 2 }
    eof {}
}
'''
    with tempfile.TemporaryDirectory(prefix="mao-claude-menu-") as directory:
        result = run_process(
            [expect, "-f", "-", executable, str(timeout_seconds)],
            Path(directory),
            timeout_seconds + 3,
            stdin=script,
        )
    output = result.stdout + result.stderr
    return output if "Select model" in output else ""


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
