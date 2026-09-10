from pathlib import Path


OPENAI_YAML = (
    Path(__file__).resolve().parents[1]
    / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/agents/openai.yaml"
)


def test_openai_policy_is_explicit_only():
    text = OPENAI_YAML.read_text(encoding="utf-8")
    assert 'display_name: "Multi-Agent Orchestrator"' in text
    assert 'short_description: "Cross-vendor implementation and bounded review"' in text
    assert "policy:\n  allow_implicit_invocation: false\n" in text
    assert "$multi-agent-orchestrator" in text
