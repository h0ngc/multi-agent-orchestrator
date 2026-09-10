from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/SKILL.md"
REFERENCES = SKILL.parent / "references"
SCENARIOS = ROOT / "tests/skill/scenarios"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    header = text.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in header.splitlines())


def test_pressure_scenarios_have_complete_contract_sections():
    paths = sorted(SCENARIOS.glob("*.md"))
    assert [path.stem for path in paths] == [
        "critic-edits",
        "false-model-list",
        "implicit-invocation",
        "unbounded-review",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for heading in (
            "## System context",
            "## User prompt",
            "## Forbidden outcomes",
            "## Required observable outcomes",
        ):
            assert heading in text


def test_skill_description_is_trigger_only():
    frontmatter = _frontmatter(SKILL)
    assert frontmatter["name"] == "multi-agent-orchestrator"
    assert frontmatter["description"].startswith("Use when")
    assert "round" not in frontmatter["description"].lower()


def test_skill_routes_mechanics_to_controller_and_stays_small():
    body = SKILL.read_text(encoding="utf-8")
    assert "mao_cli.py" in body
    assert "Do not invoke implicitly" in body
    assert len(body.split()) < 500


def test_skill_links_only_focused_reference_contracts():
    body = SKILL.read_text(encoding="utf-8")
    expected = {
        "workflow.md",
        "provider-contracts.md",
        "review-schema.md",
        "installation.md",
    }
    assert {path.name for path in REFERENCES.glob("*.md")} == expected
    assert all(name in body for name in expected)


def test_references_preserve_hard_safety_and_truthfulness_rules():
    workflow = (REFERENCES / "workflow.md").read_text(encoding="utf-8")
    providers = (REFERENCES / "provider-contracts.md").read_text(encoding="utf-8")
    assert "Maximum rounds: 2" in workflow
    assert "Maximum calls per critic: 2" in workflow
    assert "Maximum total critic calls: 4" in workflow
    assert "Critics never edit authoritative project files" in workflow
    assert "non-exhaustive" in providers
    assert "Probe exact selection before persistence" in providers
    assert "exact effort" in providers
    assert "No browser or web search" in providers


def test_pressure_evidence_is_labeled_static_not_real_agent_behavior():
    for name in ("baseline-results.json", "with-skill-results.json"):
        result = json.loads((ROOT / "tests/skill" / name).read_text(encoding="utf-8"))
        assert result["method"] == "offline_static_policy_check"
        serialized = json.dumps(result)
        assert "critic_wrote_project" not in serialized
        assert "critic_started" not in serialized
