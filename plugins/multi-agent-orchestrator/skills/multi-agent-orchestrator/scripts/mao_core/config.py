from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

from .errors import MaoError


RUNTIME_DIRECTORY = ".multi-agent-orchestrator"
IGNORE_RULE = "/.multi-agent-orchestrator/"

DEFAULT_VALUES = {
    "MAO_PRIMARY_PROVIDER": "codex",
    "MAO_ENABLED_PROVIDERS": "codex,claude,antigravity",
    "MAO_CODEX_MODEL": "gpt-6-astra",
    "MAO_CLAUDE_MODEL": "claude-opus-4-6",
    "MAO_ANTIGRAVITY_MODEL": "gemini-3.1-pro-high",
    "MAO_TRANSPORT": "direct",
    "MAO_TRANSPORT_FALLBACK": "",
    "MAO_EXECUTION_PROFILE": "yolo",
    "MAO_MAX_REVIEW_ROUNDS": "2",
    "MAO_MAX_CALLS_PER_CRITIC": "2",
    "MAO_MAX_TOTAL_CRITIC_CALLS": "4",
    "MAO_MAX_TRANSPORT_ATTEMPTS": "2",
    "MAO_TIMEOUT_SECONDS": "300",
}

DEFAULT_ENV_TEXT = "".join(
    f"{key}={value}\n" for key, value in DEFAULT_VALUES.items()
)

_KNOWN_KEYS = frozenset(DEFAULT_VALUES)
_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXPORT_PATTERN = re.compile(r"\s*export(?:\s|\Z)")
_NEWLINE_PATTERN = re.compile(rb"\r\n|\n|\r")


@dataclass(frozen=True)
class Config:
    primary_provider: str
    codex_model: str
    claude_model: str
    antigravity_model: str
    transport: str = "direct"
    transport_fallback: str = ""
    execution_profile: str = "yolo"
    max_review_rounds: int = 2
    max_calls_per_critic: int = 2
    max_total_critic_calls: int = 4
    max_transport_attempts: int = 2
    timeout_seconds: int = 300
    enabled_providers: tuple[str, ...] = ("codex", "claude", "antigravity")


def _config_error(message: str, **details: object) -> MaoError:
    return MaoError("CONFIG_INVALID", message, details)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _line_ending(data: bytes) -> bytes:
    match = _NEWLINE_PATTERN.search(data)
    return match.group(0) if match else b"\n"


def _append_ignore_rule(root: Path) -> None:
    ignore_path = root / ".gitignore"
    existing = ignore_path.read_bytes() if ignore_path.exists() else b""
    rule = IGNORE_RULE.encode("ascii")
    if rule in existing.splitlines():
        return

    newline = _line_ending(existing)
    if not existing:
        updated = rule + newline
    elif existing.endswith((b"\r\n", b"\n", b"\r")):
        updated = existing + rule + newline
    else:
        updated = existing + newline + rule
    _atomic_write(ignore_path, updated)


def find_project_root(start: Path) -> Path:
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise _config_error("Could not find project root", path=str(current))


def initialize_project(root: Path) -> tuple[Path, Path]:
    root = Path(root).expanduser().resolve()
    runtime_path = root / RUNTIME_DIRECTORY
    runtime_path.mkdir(parents=True, exist_ok=True)

    env_path = runtime_path / ".env"
    if not env_path.exists():
        _atomic_write(env_path, DEFAULT_ENV_TEXT.encode("utf-8"))

    state_path = runtime_path / "state.json"
    if not state_path.exists():
        _atomic_write(state_path, b"{}\n")

    (runtime_path / "runs").mkdir(exist_ok=True)
    _append_ignore_rule(root)
    return env_path, state_path


def initialize_installation(root: Path) -> Path:
    """Add project ignore rule without creating runtime configuration."""
    root = Path(root).expanduser().resolve()
    _append_ignore_rule(root)
    return root / ".gitignore"


def _parse_value(raw_value: str, key: str, line_number: int | None) -> str:
    if "\n" in raw_value or "\r" in raw_value:
        raise _config_error(
            "Line breaks are not allowed in configuration values",
            key=key,
            line=line_number,
        )
    value = raw_value.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise _config_error(
                "Unterminated quoted configuration value",
                key=key,
                line=line_number,
            )
        value = value[1:-1]
        if quote in value:
            raise _config_error(
                "Escaped quotes are not supported",
                key=key,
                line=line_number,
            )
    elif "'" in value or '"' in value:
        raise _config_error(
            "Malformed quoted configuration value",
            key=key,
            line=line_number,
        )

    if "\x00" in value:
        raise _config_error(
            "NUL is not allowed in configuration",
            key=key,
            line=line_number,
        )
    if "$" in value:
        raise _config_error(
            "Interpolation is not allowed in configuration",
            key=key,
            line=line_number,
        )
    return value


def _parse_env_text(text: str) -> dict[str, str]:
    if "\x00" in text:
        raise _config_error("NUL is not allowed in configuration")

    parsed: dict[str, str] = {}
    seen_known: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or raw_line.lstrip().startswith("#"):
            continue
        if _EXPORT_PATTERN.match(raw_line):
            raise _config_error("Export syntax is not supported", line=line_number)
        if "=" not in raw_line:
            raise _config_error("Expected KEY=VALUE", line=line_number)

        raw_key, raw_value = raw_line.split("=", 1)
        key = raw_key.strip()
        if not _NAME_PATTERN.fullmatch(key):
            raise _config_error("Malformed configuration name", line=line_number)
        if key in seen_known:
            raise _config_error(
                "Duplicate known configuration key",
                key=key,
                line=line_number,
            )
        if key in _KNOWN_KEYS:
            seen_known.add(key)
        parsed[key] = _parse_value(raw_value, key, line_number)
    return parsed


def _read_env_file(path: Path) -> tuple[str, dict[str, str]]:
    if not path.exists():
        return "", {}
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise _config_error("Configuration must be UTF-8") from error
    return text, _parse_env_text(text)


def _positive_int(
    values: Mapping[str, str], key: str, maximum: int | None = None
) -> int:
    try:
        result = int(values[key], 10)
    except (TypeError, ValueError) as error:
        raise _config_error("Configuration limit must be an integer", key=key) from error
    if result < 1:
        raise _config_error("Configuration limit must be positive", key=key)
    if maximum is not None and result > maximum:
        raise _config_error("Configuration limit exceeds hard maximum", key=key)
    return result


def _build_config(values: Mapping[str, str]) -> Config:
    provider = values["MAO_PRIMARY_PROVIDER"]
    if provider not in {"codex", "claude", "antigravity"}:
        raise _config_error("Unsupported primary provider", key="MAO_PRIMARY_PROVIDER")

    raw_enabled = values["MAO_ENABLED_PROVIDERS"]
    enabled = tuple(item.strip() for item in raw_enabled.split(","))
    supported_providers = {"codex", "claude", "antigravity"}
    if (
        not enabled
        or any(not item or item not in supported_providers for item in enabled)
        or len(set(enabled)) != len(enabled)
    ):
        raise _config_error(
            "Enabled providers must be a unique comma-separated supported list",
            key="MAO_ENABLED_PROVIDERS",
        )
    if provider not in enabled:
        raise _config_error(
            "Primary provider must be enabled",
            key="MAO_ENABLED_PROVIDERS",
        )

    transport = values["MAO_TRANSPORT"]
    transports = {"direct", "orca", "tmux"}
    if transport not in transports:
        raise _config_error("Unsupported transport", key="MAO_TRANSPORT")

    fallback = values["MAO_TRANSPORT_FALLBACK"]
    if fallback and fallback not in transports:
        raise _config_error(
            "Unsupported fallback transport",
            key="MAO_TRANSPORT_FALLBACK",
        )

    execution_profile = values["MAO_EXECUTION_PROFILE"]
    if execution_profile != "yolo":
        raise _config_error(
            "Unsupported execution profile",
            key="MAO_EXECUTION_PROFILE",
        )

    return Config(
        primary_provider=provider,
        codex_model=values["MAO_CODEX_MODEL"],
        claude_model=values["MAO_CLAUDE_MODEL"],
        antigravity_model=values["MAO_ANTIGRAVITY_MODEL"],
        transport=transport,
        transport_fallback=fallback,
        execution_profile=execution_profile,
        max_review_rounds=_positive_int(values, "MAO_MAX_REVIEW_ROUNDS", 2),
        max_calls_per_critic=_positive_int(values, "MAO_MAX_CALLS_PER_CRITIC", 2),
        max_total_critic_calls=_positive_int(
            values, "MAO_MAX_TOTAL_CRITIC_CALLS", 4
        ),
        max_transport_attempts=_positive_int(
            values, "MAO_MAX_TRANSPORT_ATTEMPTS", 2
        ),
        timeout_seconds=_positive_int(values, "MAO_TIMEOUT_SECONDS"),
        enabled_providers=enabled,
    )


def load_config(root: Path, environ: Mapping[str, str]) -> Config:
    env_path = Path(root).expanduser().resolve() / RUNTIME_DIRECTORY / ".env"
    _, file_values = _read_env_file(env_path)
    values = dict(DEFAULT_VALUES)
    values.update({key: value for key, value in file_values.items() if key in _KNOWN_KEYS})
    for key in _KNOWN_KEYS:
        if key in environ:
            values[key] = _parse_value(environ[key], key, None)
    return _build_config(values)


def preview_config(
    root: Path,
    updates: Mapping[str, str],
    environ: Mapping[str, str],
) -> Config:
    """Validate project-local updates without mutating configuration."""
    env_path = Path(root).expanduser().resolve() / RUNTIME_DIRECTORY / ".env"
    _, file_values = _read_env_file(env_path)
    values = dict(DEFAULT_VALUES)
    values.update({key: value for key, value in file_values.items() if key in _KNOWN_KEYS})
    for key, raw_value in updates.items():
        if key not in _KNOWN_KEYS:
            raise _config_error("Unknown configuration key", key=key)
        values[key] = _parse_value(raw_value, key, None)
    for key in _KNOWN_KEYS:
        if key in environ:
            values[key] = _parse_value(environ[key], key, None)
    return _build_config(values)


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def write_config(root: Path, values: Mapping[str, str]) -> Path:
    root = Path(root).expanduser().resolve()
    env_path = root / RUNTIME_DIRECTORY / ".env"
    original_text, original_values = _read_env_file(env_path)

    updates: dict[str, str] = {}
    for key, raw_value in values.items():
        if key not in _KNOWN_KEYS:
            raise _config_error("Unknown configuration key", key=key)
        updates[key] = _parse_value(raw_value, key, None)

    merged = dict(DEFAULT_VALUES)
    merged.update({key: value for key, value in original_values.items() if key in _KNOWN_KEYS})
    merged.update(updates)
    _build_config(merged)

    if not env_path.exists():
        original_text = DEFAULT_ENV_TEXT

    lines = original_text.splitlines(keepends=True)
    remaining = dict(updates)
    rewritten: list[str] = []
    for line in lines:
        content, ending = _split_line_ending(line)
        if "=" in content and not content.lstrip().startswith("#"):
            key = content.split("=", 1)[0].strip()
            if key in remaining:
                rewritten.append(f"{key}={remaining.pop(key)}{ending}")
                continue
        rewritten.append(line)

    output = "".join(rewritten)
    if remaining:
        newline_bytes = _line_ending(original_text.encode("utf-8"))
        newline = newline_bytes.decode("ascii")
        had_terminal_newline = output.endswith(("\r\n", "\n", "\r"))
        if output and not had_terminal_newline:
            output += newline
        output += newline.join(f"{key}={value}" for key, value in remaining.items())
        if had_terminal_newline or not original_text:
            output += newline

    _atomic_write(env_path, output.encode("utf-8"))
    return env_path
