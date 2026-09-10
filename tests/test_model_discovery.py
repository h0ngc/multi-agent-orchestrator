from pathlib import Path

from mao_core.models import discover_all
from mao_core.providers.antigravity import AntigravityAdapter
from mao_core.providers.claude import ClaudeAdapter
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
