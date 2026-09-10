from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile

import pytest

from mao_core.providers.antigravity import AntigravityAdapter
from mao_core.providers.claude import ClaudeAdapter
from mao_core.providers.codex import CodexAdapter
from mao_core.budget import BudgetGuard
from mao_core.review import CriticJob, dispatch_parallel
from mao_core.transports import DirectTransport
from mao_core.transports.base import InvocationRequest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MAO_RUN_REAL_CLI_TESTS") != "1",
        reason="billable real CLI smoke requires explicit opt-in",
    ),
]


def _project_fingerprint(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file()
        and path.relative_to(project).parts[0] != ".multi-agent-orchestrator"
    }


@pytest.mark.parametrize(
    ("provider_name", "model_env", "adapter_type", "dangerous_flag"),
    [
        ("codex", "MAO_REAL_CODEX_MODEL", CodexAdapter, "--dangerously-bypass-approvals-and-sandbox"),
        ("claude", "MAO_REAL_CLAUDE_MODEL", ClaudeAdapter, "--dangerously-skip-permissions"),
        ("antigravity", "MAO_REAL_ANTIGRAVITY_MODEL", AntigravityAdapter, "--dangerously-skip-permissions"),
    ],
)
def test_real_provider_returns_strict_review_without_project_mutation(
    tmp_path, provider_name, model_env, adapter_type, dangerous_flag, record_property
):
    model = os.environ.get(model_env)
    assert model, f"opt-in real smoke requires {model_env}"
    project = tmp_path / "authoritative-project"
    project.mkdir()
    sentinel = project / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    before = _project_fingerprint(project)
    schema = (
        Path(__file__).resolve().parents[2]
        / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/review.schema.json"
    )
    with tempfile.TemporaryDirectory(prefix=f"mao-real-{provider_name}-") as directory:
        workspace = Path(directory)
        packet = workspace / "prompt.md"
        packet.write_text(
            "Return one JSON review with summary='smoke', no findings, empty usage, review_complete=true.",
            encoding="utf-8",
        )
        adapter = adapter_type(timeout_seconds=300)
        detected = adapter.detect()
        assert detected["status"] == "available"
        assert detected["version"]
        identity = adapter.validate_model(model, workspace)
        assert identity.requested == model
        assert identity.resolved
        resolution_source = getattr(adapter, "last_resolution_source", "unknown")
        assert resolution_source in {"provider_reported", "requested_fallback"}
        if resolution_source == "requested_fallback":
            assert identity.resolved == identity.requested
        audit = {
            "provider": provider_name,
            "cli_version": detected["version"],
            "requested_model": identity.requested,
            "resolved_model": identity.resolved,
            "resolution_source": resolution_source,
        }
        print(json.dumps(audit, sort_keys=True), flush=True)
        record_property("real_cli_identity", json.dumps(audit, sort_keys=True))
        request = InvocationRequest(
            provider=provider_name,
            model=model,
            packet=packet,
            schema=schema,
            cwd=workspace,
            timeout_seconds=300,
            run_id=f"real-{provider_name}",
        )
        outcome = dispatch_parallel(
            [CriticJob(provider_name, 1, adapter, DirectTransport(), request)],
            BudgetGuard(2, 2, 4),
            project,
        )[0]
        assert outcome.status == "completed", outcome.error
        assert outcome.review is not None
        assert outcome.review.review_complete is True
        source = Path(adapter_type.__module__.replace(".", "/") + ".py")
        adapter_text = (
            Path(__file__).resolve().parents[2] / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts" / source
        ).read_text(encoding="utf-8")
        assert dangerous_flag in adapter_text
    assert _project_fingerprint(project) == before
