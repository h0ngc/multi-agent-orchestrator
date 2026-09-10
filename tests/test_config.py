import json
from pathlib import Path

import pytest

from mao_core.config import (
    Config,
    find_project_root,
    initialize_project,
    load_config,
    write_config,
)
from mao_core.errors import MaoError


DEFAULT_ENV = """MAO_PRIMARY_PROVIDER=codex
MAO_CODEX_MODEL=gpt-6-astra
MAO_CLAUDE_MODEL=claude-opus-4-6
MAO_ANTIGRAVITY_MODEL=gemini-3.1-pro-high
MAO_TRANSPORT=direct
MAO_TRANSPORT_FALLBACK=
MAO_EXECUTION_PROFILE=yolo
MAO_MAX_REVIEW_ROUNDS=2
MAO_MAX_CALLS_PER_CRITIC=2
MAO_MAX_TOTAL_CRITIC_CALLS=4
MAO_MAX_TRANSPORT_ATTEMPTS=2
MAO_TIMEOUT_SECONDS=300
"""


def write_runtime_env(root: Path, content: str) -> Path:
    runtime_dir = root / ".multi-agent-orchestrator"
    runtime_dir.mkdir()
    env_path = runtime_dir / ".env"
    env_path.write_text(content, encoding="utf-8")
    return env_path


def assert_config_invalid(root: Path) -> None:
    with pytest.raises(MaoError) as caught:
        load_config(root, {})
    assert caught.value.code == "CONFIG_INVALID"


def test_initialize_project_creates_local_runtime_files_and_idempotent_ignore(
    tmp_path,
):
    (tmp_path / ".git").mkdir()
    root_env = tmp_path / ".env"
    root_env.write_text("DO_NOT_TOUCH=secret\n", encoding="utf-8")

    env_path, state_path = initialize_project(tmp_path)
    state_path.write_text('{"keep": true}\n', encoding="utf-8")
    initialize_project(tmp_path)

    assert env_path == tmp_path / ".multi-agent-orchestrator/.env"
    assert env_path.read_text(encoding="utf-8") == DEFAULT_ENV
    assert state_path == tmp_path / ".multi-agent-orchestrator/state.json"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"keep": True}
    assert (tmp_path / ".multi-agent-orchestrator/runs").is_dir()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").count(
        "/.multi-agent-orchestrator/"
    ) == 1
    assert root_env.read_text(encoding="utf-8") == "DO_NOT_TOUCH=secret\n"


@pytest.mark.parametrize(
    ("initial", "expected"),
    [
        (b"build/\r\n", b"build/\r\n/.multi-agent-orchestrator/\r\n"),
        (b"build/", b"build/\n/.multi-agent-orchestrator/"),
    ],
)
def test_initialize_project_preserves_gitignore_line_endings_and_terminal_newline(
    tmp_path, initial, expected
):
    (tmp_path / ".gitignore").write_bytes(initial)

    initialize_project(tmp_path)

    assert (tmp_path / ".gitignore").read_bytes() == expected


def test_find_project_root_walks_up_from_a_file(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src/package"
    nested.mkdir(parents=True)
    source = nested / "module.py"
    source.touch()

    assert find_project_root(source) == tmp_path


def test_find_project_root_rejects_path_outside_project(tmp_path):
    with pytest.raises(MaoError) as caught:
        find_project_root(tmp_path)

    assert caught.value.code == "CONFIG_INVALID"


def test_environment_overrides_file(tmp_path):
    write_runtime_env(
        tmp_path,
        "MAO_TRANSPORT=orca\nMAO_MAX_REVIEW_ROUNDS=1\n",
    )

    config = load_config(
        tmp_path,
        {"MAO_TRANSPORT": "direct", "MAO_MAX_REVIEW_ROUNDS": "2"},
    )

    assert config.transport == "direct"
    assert config.max_review_rounds == 2


def test_load_config_accepts_comments_blank_lines_and_basic_quotes(tmp_path):
    write_runtime_env(
        tmp_path,
        "# retained comment\n\n"
        "UNKNOWN_SETTING=retained\n"
        "MAO_PRIMARY_PROVIDER='claude'\n"
        'MAO_CLAUDE_MODEL="claude-fable-5-1"\n'
        "MAO_TRANSPORT='tmux'\n",
    )

    config = load_config(tmp_path, {})

    assert config.primary_provider == "claude"
    assert config.claude_model == "claude-fable-5-1"
    assert config.transport == "tmux"


@pytest.mark.parametrize(
    "content",
    [
        "export MAO_TRANSPORT=direct\n",
        "MAO_TRANSPORT=${TRANSPORT}\n",
        "MAO_TRANSPORT=$TRANSPORT\n",
        "MAO_TRANSPORT=dir\x00ect\n",
        "1INVALID=value\n",
        "MAO_TRANSPORT\n",
        "MAO_TRANSPORT=direct\nMAO_TRANSPORT=orca\n",
        "MAO_TRANSPORT='direct\n",
    ],
)
def test_load_config_rejects_unsafe_or_malformed_dotenv(tmp_path, content):
    write_runtime_env(tmp_path, content)
    assert_config_invalid(tmp_path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAO_PRIMARY_PROVIDER", "other"),
        ("MAO_TRANSPORT", "ssh"),
        ("MAO_TRANSPORT_FALLBACK", "ssh"),
        ("MAO_EXECUTION_PROFILE", "safe"),
        ("MAO_MAX_REVIEW_ROUNDS", "3"),
        ("MAO_MAX_CALLS_PER_CRITIC", "3"),
        ("MAO_MAX_TOTAL_CRITIC_CALLS", "5"),
        ("MAO_MAX_TRANSPORT_ATTEMPTS", "3"),
        ("MAO_TIMEOUT_SECONDS", "0"),
    ],
)
def test_load_config_rejects_invalid_choices_and_limits(tmp_path, key, value):
    write_runtime_env(tmp_path, f"{key}={value}\n")
    assert_config_invalid(tmp_path)


def test_config_is_frozen():
    config = Config(
        primary_provider="codex",
        codex_model="gpt-5.6-sol",
        claude_model="claude-fable-5-1",
        antigravity_model="gemini-3.1-pro-high",
    )

    with pytest.raises(AttributeError):
        config.transport = "orca"


def test_write_config_updates_known_keys_and_preserves_unknown_lines(tmp_path):
    env_path = write_runtime_env(
        tmp_path,
        "# user note\n"
        "UNKNOWN_SETTING=keep-me\n"
        "MAO_TRANSPORT=direct\n"
        "\n",
    )

    result = write_config(
        tmp_path,
        {"MAO_TRANSPORT": "orca", "MAO_MAX_REVIEW_ROUNDS": "1"},
    )

    assert result == env_path
    assert env_path.read_text(encoding="utf-8") == (
        "# user note\n"
        "UNKNOWN_SETTING=keep-me\n"
        "MAO_TRANSPORT=orca\n"
        "\n"
        "MAO_MAX_REVIEW_ROUNDS=1\n"
    )
    assert load_config(tmp_path, {}).transport == "orca"


def test_write_config_rejects_unknown_or_sensitive_keys_without_mutation(tmp_path):
    env_path = write_runtime_env(tmp_path, "# keep exactly\n")

    with pytest.raises(MaoError) as caught:
        write_config(tmp_path, {"MAO_API_KEY": "secret"})

    assert caught.value.code == "CONFIG_INVALID"
    assert env_path.read_text(encoding="utf-8") == "# keep exactly\n"


@pytest.mark.parametrize("line_break", ["\n", "\r"])
def test_write_config_rejects_multiline_values_without_mutation(
    tmp_path, line_break
):
    original = b"# keep exactly\nMAO_CODEX_MODEL=safe-model\n"
    env_path = write_runtime_env(tmp_path, original.decode("utf-8"))
    injected = f"model{line_break}OPENAI_API_KEY=secret"

    with pytest.raises(MaoError) as caught:
        write_config(tmp_path, {"MAO_CODEX_MODEL": injected})

    assert caught.value.code == "CONFIG_INVALID"
    assert env_path.read_bytes() == original


@pytest.mark.parametrize("line_break", ["\n", "\r"])
def test_load_config_rejects_multiline_environment_values(tmp_path, line_break):
    injected = f"model{line_break}OPENAI_API_KEY=secret"

    with pytest.raises(MaoError) as caught:
        load_config(tmp_path, {"MAO_CODEX_MODEL": injected})

    assert caught.value.code == "CONFIG_INVALID"
