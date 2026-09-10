from pathlib import Path
import json
import sys

import pytest

from helpers import FakePaths

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins/multi-agent-orchestrator"
SKILL_ROOT = ROOT / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: invokes installed external CLIs")


@pytest.fixture
def fake_paths(tmp_path, monkeypatch):
    return FakePaths.create(tmp_path, monkeypatch)


@pytest.fixture
def packet(tmp_path):
    path = tmp_path / "packet.md"
    path.write_text("Review this minimal packet.", encoding="utf-8")
    return path


@pytest.fixture
def schema(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    return path
