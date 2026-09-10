import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_codex_plugin_manifest_names_canonical_skill():
    data = load("plugins/multi-agent-orchestrator/.codex-plugin/plugin.json")
    assert data["name"] == "multi-agent-orchestrator"
    assert data["version"] == "0.1.3"
    assert data["skills"] == "./skills/"
    assert data["license"] == "MIT"
    assert data["repository"] == "https://github.com/h0ngc/multi-agent-orchestrator"
    assert data["homepage"] == "https://github.com/h0ngc/multi-agent-orchestrator#readme"


def test_marketplaces_point_to_nested_plugin():
    codex = load(".agents/plugins/marketplace.json")
    claude = load(".claude-plugin/marketplace.json")
    assert codex["plugins"][0]["source"]["path"] == "./plugins/multi-agent-orchestrator"
    assert claude["plugins"][0]["source"] == "./plugins/multi-agent-orchestrator"
