from __future__ import annotations

import io
import json
from pathlib import Path

import mao_cli as mao_cli_module
from mao_cli import main
from mao_core.config import Config
from mao_core.errors import MaoError
from mao_core.process import ProcessResult
from mao_core.providers.base import ModelCandidate, ModelIdentity
from mao_core.setup import (
    launcher_command,
    load_setup_identities,
    setup_payload,
    write_setup_state,
)


class FakeProvider:
    def __init__(self, name, model, vendor, available=True):
        self.name = name
        self.model = model
        self.vendor = vendor
        self.available = available

    def detect(self):
        if not self.available:
            return {"status": "unavailable", "executable": self.name}
        return {"status": "available", "executable": f"/fake/{self.name}", "version": "1.0"}

    def check_auth(self):
        return {"status": "authenticated"}

    def list_models(self):
        return [ModelCandidate(self.model, self.vendor, f"{self.name} fake", self.name == "antigravity")]

    def validate_model(self, model, _cwd):
        if model != self.model:
            raise MaoError("MODEL_UNAVAILABLE", "bad model", {"provider": self.name})
        return ModelIdentity(self.name, model, model, self.vendor, True)

    def parse_result(self, _result):
        return {"summary": "clean", "findings": [], "usage": {}, "review_complete": True}

    def measure_usage(self, _parsed):
        return {}


class FakeTransport:
    def invoke(self, _provider, _request):
        return ProcessResult("ok", 0, "{}", "", 0.01, False)


class InvalidOrcaTransport:
    def validate_configuration(self, provider, _cwd, _timeout):
        raise MaoError(
            "CONFIG_INVALID",
            "Orca worker cannot prove bypass",
            {"provider": provider},
        )


class MissingValidatorOrcaTransport:
    pass


def config():
    return Config("codex", "gpt-main", "claude-critic", "gemini-critic")


def providers():
    return {
        "codex": FakeProvider("codex", "gpt-main", "openai"),
        "claude": FakeProvider("claude", "claude-critic", "anthropic"),
        "antigravity": FakeProvider("antigravity", "gemini-critic", "google"),
    }


def test_launcher_commands_have_exact_dangerous_flags():
    assert launcher_command("codex", "gpt-main") == [
        "codex", "--model", "gpt-main", "--dangerously-bypass-approvals-and-sandbox"
    ]
    assert launcher_command("claude", "claude-main")[-1] == "--dangerously-skip-permissions"
    assert launcher_command("antigravity", "gemini-main") == [
        "agy", "--model", "gemini-main", "--dangerously-skip-permissions"
    ]


def test_setup_payload_is_truthful_machine_readable_and_probes_exact_models(tmp_path):
    payload = setup_payload(tmp_path, config(), providers(), current_provider="claude", probe=True)
    assert [item["provider"] for item in payload["providers"]] == [
        "codex", "claude", "antigravity"
    ]
    assert all(item["probe"]["verified"] for item in payload["providers"])
    assert payload["providers"][1]["models"][0]["exhaustive"] is False
    assert payload["providers"][2]["models"][0]["exhaustive"] is True
    assert payload["permission_mode_detected"] is False
    assert payload["relaunch_required"] is True
    assert payload["relaunch_command"][-1] == "--dangerously-bypass-approvals-and-sandbox"
    assert payload["questions"][0]["id"] == "primary_provider"


def test_setup_relaunches_when_provider_matches_but_model_differs(tmp_path):
    payload = setup_payload(
        tmp_path,
        config(),
        providers(),
        current_provider="codex",
        current_model="gpt-old",
    )
    assert payload["relaunch_required"] is True
    assert payload["relaunch_command"][2] == "gpt-main"


def test_setup_conservatively_relaunches_when_current_model_is_unknown(tmp_path):
    payload = setup_payload(
        tmp_path,
        config(),
        providers(),
        current_provider="codex",
    )
    assert payload["relaunch_required"] is True
    assert payload["probe_may_consume_quota"] is False


def test_cli_passes_current_model_and_discloses_probe_quota_risk(tmp_path):
    code, stdout, stderr = run_cli(
        [
            "configure", "--current-provider", "codex",
            "--current-model", "gpt-old", "--probe",
        ],
        tmp_path,
    )
    payload = json.loads(stdout)["setup"]
    assert code == 0
    assert stderr == ""
    assert payload["relaunch_required"] is True
    assert payload["probe_may_consume_quota"] is True


def test_setup_payload_explains_unavailable_provider_without_auth_or_probe(tmp_path):
    values = providers()
    values["claude"] = FakeProvider("claude", "claude-critic", "anthropic", False)
    payload = setup_payload(tmp_path, config(), values, probe=True)
    claude = next(item for item in payload["providers"] if item["provider"] == "claude")
    assert claude["status"] == "unavailable"
    assert claude["reason"]["code"] == "CLI_NOT_FOUND"
    assert "auth" not in claude
    assert "probe" not in claude


def test_verified_setup_identities_round_trip_to_machine_state(tmp_path):
    payload = setup_payload(tmp_path, config(), providers(), probe=True)
    write_setup_state(tmp_path, payload)
    identities = load_setup_identities(tmp_path)
    assert [(item.provider, item.vendor) for item in identities] == [
        ("codex", "openai"),
        ("claude", "anthropic"),
        ("antigravity", "google"),
    ]


def test_setup_state_rejects_duplicate_verified_provider(tmp_path):
    payload = setup_payload(tmp_path, config(), providers(), probe=True)
    payload["providers"].append(dict(payload["providers"][0]))
    write_setup_state(tmp_path, payload)
    try:
        load_setup_identities(tmp_path)
    except MaoError as error:
        assert error.code == "STATE_TRANSITION_INVALID"
    else:
        raise AssertionError("duplicate provider accepted")


def run_cli(arguments, tmp_path, provider_values=None, transport_values=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        ["--project", str(tmp_path), *arguments],
        providers=provider_values or providers(),
        transports=transport_values,
        stdout=stdout,
        stderr=stderr,
        environ={},
    )
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_cli_exposes_all_required_commands_and_json_stdout(tmp_path):
    exit_code, stdout, stderr = run_cli(
        ["configure", "--set", "MAO_TRANSPORT=direct"], tmp_path
    )
    payload = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert payload["command"] == "configure"
    assert payload["applied"] is True
    assert (tmp_path / ".multi-agent-orchestrator/.env").exists()
    assert "/.multi-agent-orchestrator/" in (tmp_path / ".gitignore").read_text()


def test_prepare_records_request_but_does_not_claim_primary_implementation(tmp_path):
    exit_code, stdout, _ = run_cli(
        ["prepare", "--run-id", "run-1", "--request", "build feature"], tmp_path
    )
    payload = json.loads(stdout)
    assert exit_code == 0
    assert payload["phase"] == "CREATED"
    assert payload["primary_action_required"] == "implement_and_verify_locally"
    request = tmp_path / ".multi-agent-orchestrator/runs/run-1/request.md"
    assert request.read_text(encoding="utf-8") == "build feature"


def test_packet_command_builds_managed_critic_packet_from_strict_input(tmp_path):
    code, _stdout, _stderr = run_cli(
        [
            "configure",
            "--set", "MAO_CODEX_MODEL=gpt-main",
            "--set", "MAO_CLAUDE_MODEL=claude-critic",
            "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
            "--probe",
        ],
        tmp_path,
    )
    assert code == 0
    run_cli(["prepare", "--run-id", "run-1", "--request", "review work"], tmp_path)
    source = tmp_path / "src.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    packet_input = tmp_path / "packet-input.json"
    packet_input.write_text(
        json.dumps(
            {
                "acceptance_conditions": ["value changed"],
                "instructions": [],
                "diff": "diff --git a/src.py b/src.py\n--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
                "changed_files": ["src.py"],
                "related_files": [],
                "test_outputs": ["pytest: PASS"],
                "visual_artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    code, stdout, stderr = run_cli(
        [
            "packet", "--run-id", "run-1", "--round", "1",
            "--critic", "claude", "--input", str(packet_input),
        ],
        tmp_path,
    )

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["command"] == "packet"
    assert payload["critics"] == ["claude"]
    prompt = Path(payload["packets"][0]["prompt"])
    assert "review work" in prompt.read_text(encoding="utf-8")


def test_failed_second_critic_packet_build_preserves_active_revision(
    tmp_path, monkeypatch
):
    run_cli(
        [
            "configure",
            "--set", "MAO_CODEX_MODEL=gpt-main",
            "--set", "MAO_CLAUDE_MODEL=claude-critic",
            "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
            "--probe",
        ],
        tmp_path,
    )
    run_cli(["prepare", "--run-id", "run-1", "--request", "work"], tmp_path)
    (tmp_path / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    packet_input = tmp_path / "packet-input.json"
    packet_input.write_text(
        json.dumps(
            {
                "acceptance_conditions": [],
                "instructions": [],
                "diff": "",
                "changed_files": ["src.py"],
                "related_files": [],
                "test_outputs": ["PASS"],
                "visual_artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    code, stdout, _stderr = run_cli(
        ["packet", "--run-id", "run-1", "--round", "1", "--input", str(packet_input)],
        tmp_path,
    )
    assert code == 0
    original = json.loads(stdout)
    old_files = {
        item["critic"]: Path(item["prompt"]).read_bytes()
        for item in original["packets"]
    }
    state_path = tmp_path / ".multi-agent-orchestrator/runs/run-1/state.json"
    old_state = state_path.read_bytes()
    real_build = mao_cli_module.build_packet
    calls = 0

    def fail_second(request, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MaoError("PACKET_PATH_INVALID", "simulated second failure", {})
        return real_build(request, destination)

    monkeypatch.setattr(mao_cli_module, "build_packet", fail_second)
    code, stdout, stderr = run_cli(
        ["packet", "--run-id", "run-1", "--round", "1", "--input", str(packet_input)],
        tmp_path,
    )

    assert code != 0
    assert stdout == ""
    assert json.loads(stderr)["error"]["code"] == "PACKET_PATH_INVALID"
    assert state_path.read_bytes() == old_state
    assert all(
        Path(item["prompt"]).read_bytes() == old_files[item["critic"]]
        for item in original["packets"]
    )
    assert run_cli(["resume", "--run-id", "run-1", "--request", "work"], tmp_path)[0] == 0


def test_review_is_blocked_until_primary_reports_local_verification(tmp_path):
    run_cli(["prepare", "--run-id", "run-1", "--request", "work"], tmp_path)
    exit_code, stdout, stderr = run_cli(["review", "--run-id", "run-1"], tmp_path)
    assert stdout == ""
    assert exit_code != 0
    assert json.loads(stderr)["error"]["code"] == "STATE_TRANSITION_INVALID"


def test_status_resume_decide_finalize_commands_return_json(tmp_path):
    run_cli(["prepare", "--run-id", "run-1", "--request", "work"], tmp_path)
    for command in (
        ["status", "--run-id", "run-1"],
        ["resume", "--run-id", "run-1", "--request", "work"],
    ):
        code, stdout, _ = run_cli(command, tmp_path)
        assert code == 0
        assert json.loads(stdout)["run_id"] == "run-1"

    for command in ("decide", "finalize"):
        code, stdout, stderr = run_cli([command, "--run-id", "run-1"], tmp_path)
        assert code != 0
        assert stdout == ""
        assert json.loads(stderr)["error"]["code"] == "STATE_TRANSITION_INVALID"


def test_cli_rejects_malformed_set_with_typed_json_error(tmp_path):
    code, stdout, stderr = run_cli(["configure", "--set", "bad"], tmp_path)
    assert code != 0
    assert stdout == ""
    assert json.loads(stderr)["error"]["code"] == "CONFIG_INVALID"


def test_cli_argument_errors_are_typed_json(tmp_path):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["--project", str(tmp_path), "unknown-command"],
        providers=providers(),
        stdout=stdout,
        stderr=stderr,
        environ={},
    )
    assert code != 0
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["error"]["code"] == "CONFIG_INVALID"


def test_failed_exact_model_probe_does_not_persist_update(tmp_path):
    code, stdout, _stderr = run_cli(
        ["configure", "--set", "MAO_CODEX_MODEL=not-available", "--probe"],
        tmp_path,
    )
    assert code == 0
    assert json.loads(stdout)["applied"] is False
    env_text = (tmp_path / ".multi-agent-orchestrator/.env").read_text()
    assert "MAO_CODEX_MODEL=gpt-6-astra" in env_text
    assert "not-available" not in env_text


def test_invalid_orca_preflight_does_not_persist_transport(tmp_path):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            "--project", str(tmp_path), "configure",
            "--set", "MAO_TRANSPORT=orca",
        ],
        providers=providers(),
        transports={"direct": FakeTransport(), "orca": InvalidOrcaTransport()},
        stdout=stdout,
        stderr=stderr,
        environ={},
    )
    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert stderr.getvalue() == ""
    assert payload["applied"] is False
    selected = next(item for item in payload["setup"]["transports"] if item["selected"])
    assert selected["status"] == "invalid"
    env_text = (tmp_path / ".multi-agent-orchestrator/.env").read_text()
    assert "MAO_TRANSPORT=direct" in env_text


def test_orca_without_bypass_validator_does_not_persist_transport(tmp_path):
    stdout = io.StringIO()
    code = main(
        ["--project", str(tmp_path), "configure", "--set", "MAO_TRANSPORT=orca"],
        providers=providers(),
        transports={"direct": FakeTransport(), "orca": MissingValidatorOrcaTransport()},
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert payload["applied"] is False
    selected = next(item for item in payload["setup"]["transports"] if item["selected"])
    assert selected["status"] == "invalid"
    assert "MAO_TRANSPORT=direct" in (
        tmp_path / ".multi-agent-orchestrator/.env"
    ).read_text()


def test_failed_combined_config_keeps_prior_env_and_verified_identities(tmp_path):
    initial = [
        "configure",
        "--set", "MAO_CODEX_MODEL=gpt-main",
        "--set", "MAO_CLAUDE_MODEL=claude-critic",
        "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
        "--probe",
    ]
    assert run_cli(initial, tmp_path)[0] == 0
    env_path = tmp_path / ".multi-agent-orchestrator/.env"
    state_path = tmp_path / ".multi-agent-orchestrator/state.json"
    old_env = env_path.read_bytes()
    old_state = state_path.read_bytes()
    changed_providers = providers()
    changed_providers["claude"] = FakeProvider("claude", "claude-new", "anthropic")
    stdout = io.StringIO()
    code = main(
        [
            "--project", str(tmp_path), "configure",
            "--set", "MAO_CLAUDE_MODEL=claude-new",
            "--set", "MAO_TRANSPORT=orca",
            "--probe",
        ],
        providers=changed_providers,
        transports={"direct": FakeTransport(), "orca": InvalidOrcaTransport()},
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert json.loads(stdout.getvalue())["applied"] is False
    assert env_path.read_bytes() == old_env
    assert state_path.read_bytes() == old_state


def test_primary_provider_change_requires_successful_target_primary_probe(tmp_path):
    run_cli(
        [
            "configure",
            "--set", "MAO_CODEX_MODEL=gpt-main",
            "--set", "MAO_CLAUDE_MODEL=claude-critic",
            "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
            "--probe",
        ],
        tmp_path,
    )
    env_path = tmp_path / ".multi-agent-orchestrator/.env"
    before = env_path.read_bytes()
    values = providers()
    values["claude"] = FakeProvider(
        "claude", "claude-critic", "anthropic", available=False
    )

    code, stdout, stderr = run_cli(
        ["configure", "--set", "MAO_PRIMARY_PROVIDER=claude", "--probe"],
        tmp_path,
        provider_values=values,
    )

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)["applied"] is False
    assert env_path.read_bytes() == before


def test_cli_successful_review_does_not_report_primary_as_review_gap(tmp_path):
    code, _stdout, _stderr = run_cli(
        [
            "configure",
            "--set", "MAO_CODEX_MODEL=gpt-main",
            "--set", "MAO_CLAUDE_MODEL=claude-critic",
            "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
            "--probe",
        ],
        tmp_path,
    )
    assert code == 0
    run_cli(["prepare", "--run-id", "run-1", "--request", "work"], tmp_path)
    (tmp_path / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    packet_input = tmp_path / "packet-input.json"
    packet_input.write_text(
        json.dumps(
            {
                "acceptance_conditions": [],
                "instructions": [],
                "diff": "",
                "changed_files": ["src.py"],
                "related_files": [],
                "test_outputs": ["PASS"],
                "visual_artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    packet_code, packet_stdout, _packet_stderr = run_cli(
        [
            "packet", "--run-id", "run-1", "--round", "1",
            "--critic", "claude", "--input", str(packet_input),
        ],
        tmp_path,
    )
    assert packet_code == 0
    packet_payload = json.loads(packet_stdout)

    code, stdout, stderr = run_cli(
        [
            "review", "--run-id", "run-1", "--implemented",
            "--local-verification-output", "PASS", "--passed",
            "--critic", "claude",
        ],
        tmp_path,
        transport_values={"direct": FakeTransport()},
    )
    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)["phase"] == "REVIEW_ROUND_1"

    prompt = Path(packet_payload["packets"][0]["prompt"])
    before = prompt.read_bytes()
    packet_input.write_text(
        packet_input.read_text(encoding="utf-8").replace("PASS", "CHANGED"),
        encoding="utf-8",
    )
    packet_code, packet_stdout, packet_stderr = run_cli(
        [
            "packet", "--run-id", "run-1", "--round", "1",
            "--critic", "claude", "--input", str(packet_input),
        ],
        tmp_path,
    )
    assert packet_code != 0
    assert packet_stdout == ""
    assert json.loads(packet_stderr)["error"]["code"] == "STATE_TRANSITION_INVALID"
    assert prompt.read_bytes() == before

    code, _stdout, _stderr = run_cli(["decide", "--run-id", "run-1"], tmp_path)
    assert code == 0
    code, stdout, _stderr = run_cli(["finalize", "--run-id", "run-1"], tmp_path)
    assert code == 0
    result = json.loads(stdout)["result"]
    assert result["status"] == "COMPLETED_WITH_REVIEW_GAP"
    assert result["review_gaps"] == ["antigravity"]
    assert "codex" not in result["review_gaps"]


def test_cli_requires_managed_packet_before_review(tmp_path):
    run_cli(
        [
            "configure",
            "--set", "MAO_CODEX_MODEL=gpt-main",
            "--set", "MAO_CLAUDE_MODEL=claude-critic",
            "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
            "--probe",
        ],
        tmp_path,
    )
    run_cli(["prepare", "--run-id", "run-1", "--request", "work"], tmp_path)
    code, stdout, stderr = run_cli(
        [
            "review", "--run-id", "run-1", "--implemented",
            "--local-verification-output", "PASS", "--passed", "--critic", "claude",
        ],
        tmp_path,
        transport_values={"direct": FakeTransport()},
    )
    assert code != 0
    assert stdout == ""
    assert json.loads(stderr)["error"]["code"] == "PACKET_PATH_INVALID"


def test_cli_rejects_tampered_managed_packet_before_review_dispatch(tmp_path):
    run_cli(
        [
            "configure",
            "--set", "MAO_CODEX_MODEL=gpt-main",
            "--set", "MAO_CLAUDE_MODEL=claude-critic",
            "--set", "MAO_ANTIGRAVITY_MODEL=gemini-critic",
            "--probe",
        ],
        tmp_path,
    )
    run_cli(["prepare", "--run-id", "run-1", "--request", "work"], tmp_path)
    (tmp_path / "src.py").write_text("VALUE = 2\n", encoding="utf-8")
    packet_input = tmp_path / "packet-input.json"
    packet_input.write_text(
        json.dumps(
            {
                "acceptance_conditions": [],
                "instructions": [],
                "diff": "",
                "changed_files": ["src.py"],
                "related_files": [],
                "test_outputs": ["PASS"],
                "visual_artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    packet_code, packet_stdout, _packet_stderr = run_cli(
        [
            "packet", "--run-id", "run-1", "--critic", "claude",
            "--round", "1", "--input", str(packet_input),
        ],
        tmp_path,
    )
    assert packet_code == 0
    prompt = Path(json.loads(packet_stdout)["packets"][0]["prompt"])
    prompt.write_text(prompt.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    resume_code, resume_stdout, resume_stderr = run_cli(
        ["resume", "--run-id", "run-1", "--request", "work"], tmp_path
    )
    assert resume_code != 0
    assert resume_stdout == ""
    assert json.loads(resume_stderr)["error"]["code"] == "PACKET_PATH_INVALID"

    code, stdout, stderr = run_cli(
        [
            "review", "--run-id", "run-1", "--implemented",
            "--local-verification-output", "PASS", "--passed", "--critic", "claude",
        ],
        tmp_path,
        transport_values={"direct": FakeTransport()},
    )

    assert code != 0
    assert stdout == ""
    assert json.loads(stderr)["error"]["code"] == "PACKET_PATH_INVALID"
