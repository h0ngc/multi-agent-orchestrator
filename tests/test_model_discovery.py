import os
from pathlib import Path
import sys

from mao_core.models import discover_all
from mao_core.providers.antigravity import AntigravityAdapter
from mao_core.providers.claude import ClaudeAdapter, _read_interactive_model_menu
from mao_core.providers.codex import CodexAdapter


FIXTURES = Path(__file__).parent / "fixtures"


def test_codex_models_come_from_provider_cache_with_fetch_timestamp(fake_paths):
    adapter = CodexAdapter(
        executable=fake_paths.codex,
        models_cache=FIXTURES / "codex-models-cache.json",
    )

    candidates = adapter.list_models()

    assert [candidate.requested for candidate in candidates] == [
        "gpt-6-astra",
        "o3",
    ]
    assert all(candidate.exhaustive is False for candidate in candidates)
    assert all("models_cache.json" in candidate.source for candidate in candidates)
    assert all(
        "2026-09-09T03:48:03.790219Z" in candidate.source
        for candidate in candidates
    )


def test_claude_candidate_list_is_marked_non_exhaustive(fake_paths):
    adapter = ClaudeAdapter(executable=fake_paths.claude)

    candidates = adapter.list_models()

    assert [candidate.requested for candidate in candidates] == [
        "fable",
        "opus",
        "sonnet",
    ]
    assert all(candidate.exhaustive is False for candidate in candidates)
    assert all(candidate.source == "claude --help" for candidate in candidates)


def test_claude_prefers_models_read_from_interactive_model_menu(fake_paths):
    menu = """
Select model
1. Default (recommended) Use the default model (currently Opus 5 (1M context))
2. Opus (1M context) Opus 5 with 1M context
3. Sonnet Sonnet 5
4. (selected) Sonnet 5 (1M context) Sonnet 5 for long sessions
5. Haiku Haiku 4.5
Enter to set as default · s to use this session only · Esc to cancel
"""
    adapter = ClaudeAdapter(
        executable=fake_paths.claude,
        model_menu_reader=lambda _executable, _timeout: menu,
    )

    candidates = adapter.list_models()

    assert [candidate.requested for candidate in candidates] == [
        "opus[1m]",
        "sonnet",
        "sonnet[1m]",
        "haiku",
        "fable",
        "opus",
    ]
    assert candidates[2].source == "claude interactive /model: Sonnet 5 (1M context)"
    assert all(candidate.exhaustive is False for candidate in candidates)


def test_claude_interactive_reader_uses_expect_compatible_spawn_syntax(
    tmp_path, monkeypatch
):
    expect = tmp_path / "expect"
    expect.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "script = sys.stdin.read()\n"
        "if 'spawn -noecho -- ' in script:\n"
        "    print('bad flag --', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "if 'send \\\"y\\\\r\\\"' not in script:\n"
        "    print('trust prompt requires explicit y', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "print('Select model\\n4. Sonnet 5 (1M context)')\n",
        encoding="utf-8",
    )
    expect.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    menu = _read_interactive_model_menu("/fake/claude", 1)

    assert "Sonnet 5 (1M context)" in menu


def test_claude_merges_configured_candidates_without_claiming_exhaustiveness(
    fake_paths, monkeypatch
):
    monkeypatch.setenv(
        "MAO_CLAUDE_CANDIDATES", "claude-custom-1, sonnet, claude-custom-2"
    )
    adapter = ClaudeAdapter(executable=fake_paths.claude)

    candidates = adapter.list_models()

    assert [candidate.requested for candidate in candidates] == [
        "fable",
        "opus",
        "sonnet",
        "claude-custom-1",
        "claude-custom-2",
    ]
    configured = candidates[-2:]
    assert all(candidate.source == "MAO_CLAUDE_CANDIDATES" for candidate in configured)
    assert all(candidate.exhaustive is False for candidate in candidates)


def test_agy_models_are_machine_discovered(fake_paths):
    adapter = AntigravityAdapter(executable=fake_paths.agy)

    candidates = adapter.list_models()

    assert [candidate.requested for candidate in candidates] == [
        "gemini-3.1-pro-high",
        "claude-sonnet-4-6",
    ]
    assert all(candidate.source == "agy models" for candidate in candidates)
    assert all(candidate.exhaustive is True for candidate in candidates)


def test_discover_all_preserves_provider_names_and_candidate_lists(fake_paths):
    adapters = [
        ClaudeAdapter(executable=fake_paths.claude),
        AntigravityAdapter(executable=fake_paths.agy),
    ]

    discovered = discover_all(adapters)

    assert list(discovered) == ["claude", "antigravity"]
    assert discovered["claude"][0].requested == "fable"
    assert discovered["antigravity"][0].requested == "gemini-3.1-pro-high"
