import json
import os
from pathlib import Path

import pytest

import mao_core.providers.antigravity as antigravity_module
import mao_core.providers.codex as codex_module
from mao_core.errors import MaoError
from mao_core.models import classify_vendor
from mao_core.process import ProcessResult
from mao_core.providers.antigravity import AntigravityAdapter
from mao_core.providers.claude import ClaudeAdapter
from mao_core.providers.codex import CodexAdapter


EXPECTED_REVIEW = {
    "summary": "No issues found.",
    "findings": [],
    "usage": {},
    "review_complete": True,
}
FAKES = Path(__file__).parent / "fakes"


def test_codex_review_command_contains_exact_yolo_flag(fake_paths, packet, schema):
    adapter = CodexAdapter(executable=fake_paths.codex)

    result = adapter.invoke("gpt-test", packet, schema, packet.parent)

    assert result.exit_code == 0
    args = fake_paths.last_args("codex")
    assert args[:7] == [
        "exec",
        "--model",
        "gpt-test",
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "--skip-git-repo-check",
        "--output-schema",
    ]
    assert args[8:] == ["Review this minimal packet."]


def test_codex_review_uses_compatible_copy_without_changing_canonical_schema(
    monkeypatch, packet, tmp_path
):
    schema = tmp_path / "review.schema.json"
    canonical = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "line": {
                "oneOf": [
                    {"type": "integer", "minimum": 1},
                    {"type": "null"},
                ]
            },
            "review_complete": {"const": True},
            "usage": {"type": "object"},
        },
        "required": ["line", "review_complete", "usage"],
        "additionalProperties": False,
    }
    schema.write_text(json.dumps(canonical), encoding="utf-8")
    captured = {}

    def capture(args, cwd, timeout_seconds):
        compatible_path = Path(args[args.index("--output-schema") + 1])
        captured["schema"] = json.loads(compatible_path.read_text(encoding="utf-8"))
        return _process_result("{}")

    monkeypatch.setattr(codex_module, "run_process", capture)
    adapter = CodexAdapter()

    adapter.invoke("gpt-test", packet, schema, packet.parent)

    compatible = captured["schema"]
    assert compatible["properties"]["line"] == {"type": ["integer", "null"]}
    assert compatible["properties"]["review_complete"] == {
        "type": "boolean",
        "enum": [True],
    }
    assert compatible["properties"]["usage"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert json.loads(schema.read_text(encoding="utf-8")) == canonical


def test_codex_parses_structured_review_before_final_jsonl_event(
    fake_paths, packet, schema
):
    adapter = CodexAdapter(executable=fake_paths.codex)

    parsed = adapter.parse_result(
        adapter.invoke("gpt-test", packet, schema, packet.parent)
    )

    assert parsed == EXPECTED_REVIEW


def test_claude_review_command_contains_exact_skip_permissions_flag(
    fake_paths, packet, schema
):
    adapter = ClaudeAdapter(executable=fake_paths.claude)

    result = adapter.invoke("claude-test", packet, schema, packet.parent)

    assert result.exit_code == 0
    assert fake_paths.last_args("claude") == [
        "-p",
        "Review this minimal packet.",
        "--model",
        "claude-test",
        "--output-format",
        "json",
        "--json-schema",
        '{"type": "object"}',
        "--dangerously-skip-permissions",
    ]


def test_claude_review_removes_unsupported_schema_dialect_declaration(
    fake_paths, packet, tmp_path
):
    schema = tmp_path / "review.schema.json"
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }
        ),
        encoding="utf-8",
    )
    adapter = ClaudeAdapter(executable=fake_paths.claude)

    result = adapter.invoke("claude-test", packet, schema, packet.parent)

    assert result.exit_code == 0
    args = fake_paths.last_args("claude")
    passed_schema = json.loads(args[args.index("--json-schema") + 1])
    assert passed_schema == {"type": "object"}


def test_agy_review_command_contains_exact_skip_permissions_flag(
    fake_paths, packet, schema
):
    adapter = AntigravityAdapter(executable=fake_paths.agy)

    result = adapter.invoke("gemini-test", packet, schema, packet.parent)

    assert result.exit_code == 0
    assert fake_paths.last_args("agy") == [
        "--print",
        "Review this minimal packet.",
        "--model",
        "gemini-test",
        "--output-format",
        "json",
        "--json-schema",
        str(schema),
        "--dangerously-skip-permissions",
    ]


@pytest.mark.parametrize(
    ("adapter_factory", "model"),
    [
        (lambda paths: CodexAdapter(executable=paths.codex), "gpt-test"),
        (lambda paths: ClaudeAdapter(executable=paths.claude), "claude-test"),
        (lambda paths: AntigravityAdapter(executable=paths.agy), "gemini-test"),
    ],
)
def test_review_fakes_emit_complete_valid_fixture(
    fake_paths, packet, schema, adapter_factory, model
):
    adapter = adapter_factory(fake_paths)

    parsed = adapter.parse_result(adapter.invoke(model, packet, schema, packet.parent))

    assert parsed == EXPECTED_REVIEW


def test_review_valid_fixture_has_complete_expected_envelope():
    fixture = Path(__file__).parent / "fixtures/review-valid.json"

    assert json.loads(fixture.read_text(encoding="utf-8")) == EXPECTED_REVIEW


def test_codex_result_preserves_usage_from_final_turn_event():
    review = json.dumps(EXPECTED_REVIEW)
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": review},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 11,
                        "cached_input_tokens": 2,
                        "output_tokens": 3,
                    },
                }
            ),
        ]
    )
    adapter = CodexAdapter()

    parsed = adapter.parse_result(_process_result(stdout))

    assert parsed == {
        **EXPECTED_REVIEW,
        "usage": {
            "input_tokens": 11,
            "cached_input_tokens": 2,
            "output_tokens": 3,
        },
    }
    assert adapter.measure_usage(parsed) == parsed["usage"]


def test_claude_result_preserves_native_top_level_usage():
    usage = {
        "input_tokens": 7,
        "cache_read_input_tokens": 4,
        "output_tokens": 2,
    }
    stdout = json.dumps(
        {
            "type": "result",
            "structured_output": EXPECTED_REVIEW,
            "usage": usage,
            "modelUsage": {"claude-sonnet-4-6": {"costUSD": 0.01}},
        }
    )
    adapter = ClaudeAdapter()

    parsed = adapter.parse_result(_process_result(stdout))

    assert parsed == {**EXPECTED_REVIEW, "usage": usage}
    assert adapter.measure_usage(parsed) == usage


def test_agy_result_preserves_native_top_level_usage():
    usage = {"input_tokens": 5, "output_tokens": 2}
    stdout = json.dumps(
        {
            "type": "result",
            "result": json.dumps(EXPECTED_REVIEW),
            "usage": usage,
        }
    )
    adapter = AntigravityAdapter()

    parsed = adapter.parse_result(_process_result(stdout))

    assert parsed == {**EXPECTED_REVIEW, "usage": usage}
    assert adapter.measure_usage(parsed) == usage


def test_agy_1_2_result_parses_response_field_and_native_usage():
    usage = {"input_tokens": 5, "output_tokens": 2, "thinking_tokens": 1}
    stdout = json.dumps(
        {
            "conversation_id": "test-conversation",
            "status": "SUCCESS",
            "response": json.dumps(EXPECTED_REVIEW),
            "usage": usage,
        }
    )
    adapter = AntigravityAdapter()

    parsed = adapter.parse_result(_process_result(stdout))

    assert parsed == {**EXPECTED_REVIEW, "usage": usage}
    assert adapter.measure_usage(parsed) == usage


def test_agy_1_2_probe_accepts_success_response_with_requested_fallback(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        antigravity_module,
        "run_process",
        lambda *args, **kwargs: _process_result(
            json.dumps(
                {
                    "conversation_id": "test-conversation",
                    "status": "SUCCESS",
                    "response": "OK.\n",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
        ),
    )
    adapter = AntigravityAdapter()

    identity = adapter.validate_model("gemini-3.1-pro-high", tmp_path)

    assert identity.requested == "gemini-3.1-pro-high"
    assert identity.resolved == "gemini-3.1-pro-high"
    assert identity.verified is True
    assert adapter.last_resolution_source == "requested_fallback"


@pytest.mark.parametrize(
    ("adapter", "stdout"),
    [
        (
            CodexAdapter(),
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps(EXPECTED_REVIEW),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "metadata": {"usage": {"guessed": 99}},
                        }
                    ),
                ]
            ),
        ),
        (
            ClaudeAdapter(),
            json.dumps(
                {
                    "type": "result",
                    "structured_output": EXPECTED_REVIEW,
                    "metadata": {"usage": {"guessed": 99}},
                }
            ),
        ),
        (
            AntigravityAdapter(),
            json.dumps(
                {
                    "type": "result",
                    "result": json.dumps(EXPECTED_REVIEW),
                    "metadata": {"usage": {"guessed": 99}},
                }
            ),
        ),
    ],
)
def test_result_parsers_do_not_guess_usage_from_nested_metadata(adapter, stdout):
    parsed = adapter.parse_result(_process_result(stdout))

    assert parsed["usage"] == {}


def _process_result(stdout: str) -> ProcessResult:
    return ProcessResult(
        status="ok",
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
    )


@pytest.mark.parametrize("executable_name", ["codex", "claude", "agy"])
def test_fake_executables_use_portable_python_shebang(executable_name):
    executable = FAKES / executable_name

    assert executable.read_text(encoding="utf-8").splitlines()[0] == (
        "#!/usr/bin/env python3"
    )
    assert os.access(executable, os.X_OK)


def test_codex_auth_check_uses_read_only_status_command(fake_paths):
    adapter = CodexAdapter(executable=fake_paths.codex)

    status = adapter.check_auth()

    assert status == {"status": "authenticated"}
    assert fake_paths.last_args("codex") == ["login", "status"]


def test_claude_auth_check_uses_read_only_status_command(fake_paths):
    adapter = ClaudeAdapter(executable=fake_paths.claude)

    status = adapter.check_auth()

    assert status == {"status": "authenticated"}
    assert fake_paths.last_args("claude") == ["auth", "status"]


def test_agy_auth_check_is_unknown_without_guessing_or_running_model(fake_paths):
    adapter = AntigravityAdapter(executable=fake_paths.agy)

    status = adapter.check_auth()

    assert status["status"] == "unknown"
    assert fake_paths.rows("agy") == []


def test_validate_model_keeps_requested_and_resolved_identity_in_isolated_cwd(
    fake_paths, tmp_path
):
    adapter = ClaudeAdapter(executable=fake_paths.claude)

    identity = adapter.validate_model("sonnet", tmp_path)

    row = fake_paths.rows("claude")[-1]
    assert identity.requested == "sonnet"
    assert identity.resolved == "claude-sonnet-4-6"
    assert identity.vendor == "anthropic"
    assert identity.verified is True
    assert adapter.last_resolution_source == "provider_reported"
    assert row["cwd"] != str(tmp_path)
    assert str(tmp_path) not in row["args"]
    assert "Reply with exact text OK." in row["args"]
    assert not Path(row["cwd"]).exists()


def test_codex_validate_model_scans_jsonl_and_keeps_resolved_alias(
    fake_paths, tmp_path
):
    adapter = CodexAdapter(executable=fake_paths.codex)

    identity = adapter.validate_model("gpt-alias", tmp_path)

    assert identity.requested == "gpt-alias"
    assert identity.resolved == "gpt-resolved"
    assert identity.vendor == "openai"
    assert identity.verified is True


def test_codex_validate_model_rejects_ok_found_only_in_unrelated_metadata(
    fake_paths, tmp_path
):
    adapter = CodexAdapter(executable=fake_paths.codex)

    with pytest.raises(MaoError) as raised:
        adapter.validate_model("metadata-ok-only", tmp_path)

    assert raised.value.code == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize(
    ("adapter_factory", "provider"),
    [
        (lambda paths: CodexAdapter(executable=paths.codex), "codex"),
        (lambda paths: ClaudeAdapter(executable=paths.claude), "claude"),
        (lambda paths: AntigravityAdapter(executable=paths.agy), "antigravity"),
    ],
)
def test_validate_model_maps_failed_fake_probe_to_model_unavailable(
    fake_paths, tmp_path, adapter_factory, provider
):
    adapter = adapter_factory(fake_paths)

    with pytest.raises(MaoError) as raised:
        adapter.validate_model("unavailable-model", tmp_path)

    assert raised.value.code == "MODEL_UNAVAILABLE"
    assert raised.value.details == {
        "provider": provider,
        "requested_model": "unavailable-model",
    }


@pytest.mark.parametrize(
    ("provider", "resolved", "expected"),
    [
        ("codex", "gpt-6-astra", "openai"),
        ("codex", "o3", "openai"),
        ("antigravity", "claude-sonnet-4-6", "anthropic"),
        ("antigravity", "gemini-3.1-pro-high", "google"),
        ("local", "custom-model", "local"),
    ],
)
def test_classify_vendor_uses_resolved_model(provider, resolved, expected):
    assert classify_vendor(provider, resolved) == expected
