#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence, TextIO

from mao_core.budget import BudgetGuard
from mao_core.config import initialize_project, load_config, preview_config, write_config
from mao_core.errors import MaoError
from mao_core.packet import PacketRequest, build_packet
from mao_core.providers.antigravity import AntigravityAdapter
from mao_core.providers.base import ProviderAdapter
from mao_core.providers.claude import ClaudeAdapter
from mao_core.providers.codex import CodexAdapter
from mao_core.review import Decision, dispatch_parallel
from mao_core.setup import load_setup_identities, setup_payload, write_setup_state
from mao_core.state import StateStore
from mao_core.transports import DirectTransport, OrcaTransport, TmuxTransport
from mao_core.transports.base import TransportAdapter
from mao_core.workflow import Workflow, select_reviewers


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise MaoError("CONFIG_INVALID", "Invalid command arguments", {})


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="mao")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    configure = commands.add_parser("configure")
    configure.add_argument("--set", dest="settings", action="append", default=[])
    configure.add_argument("--current-provider", choices=("codex", "claude", "antigravity"))
    configure.add_argument("--current-model")
    configure.add_argument("--probe", action="store_true")

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--request", required=True)
    prepare.add_argument("--run-id")

    packet = commands.add_parser("packet")
    packet.add_argument("--run-id", required=True)
    packet.add_argument("--round", type=int, choices=(1, 2), required=True)
    packet.add_argument("--critic", action="append", default=[])
    packet.add_argument("--input", type=Path, required=True)

    review = commands.add_parser("review")
    review.add_argument("--run-id", required=True)
    review.add_argument("--round", type=int, choices=(1, 2), default=1)
    review.add_argument("--implemented", action="store_true")
    review.add_argument("--patched", action="store_true")
    review.add_argument("--local-verification-output")
    review.add_argument("--passed", action="store_true")
    review.add_argument("--critic", action="append", default=[])
    review.add_argument("--output-changed", action="store_true")
    review.add_argument("--supplied-context", action="store_true")
    review.add_argument("--new-review-surface", action="store_true")

    decide = commands.add_parser("decide")
    decide.add_argument("--run-id", required=True)
    decide.add_argument("--decisions", type=Path)

    for name in ("status", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)

    resume = commands.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--request", required=True)
    return parser


def _default_providers(timeout_seconds: int) -> dict[str, ProviderAdapter]:
    return {
        "codex": CodexAdapter(timeout_seconds=timeout_seconds),
        "claude": ClaudeAdapter(timeout_seconds=timeout_seconds),
        "antigravity": AntigravityAdapter(timeout_seconds=timeout_seconds),
    }


def _default_transports() -> dict[str, TransportAdapter]:
    return {
        "direct": DirectTransport(),
        "orca": OrcaTransport(),
        "tmux": TmuxTransport(),
    }


def _settings(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise MaoError("CONFIG_INVALID", "Expected --set KEY=VALUE", {})
        key, value = item.split("=", 1)
        if not key:
            raise MaoError("CONFIG_INVALID", "Expected --set KEY=VALUE", {})
        result[key] = value
    return result


def _json_ready(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def _load_decisions(path: Path | None) -> list[Decision]:
    if path is None:
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MaoError("STATE_TRANSITION_INVALID", "Decision file could not be loaded", {}) from None
    if not isinstance(value, list):
        raise MaoError("STATE_TRANSITION_INVALID", "Decisions must be a JSON list", {})
    try:
        return [Decision(item["fingerprint"], item["verdict"], item["rationale"]) for item in value]
    except (KeyError, TypeError):
        raise MaoError("STATE_TRANSITION_INVALID", "Decision record is invalid", {}) from None


_PACKET_INPUT_FIELDS = {
    "acceptance_conditions",
    "instructions",
    "diff",
    "changed_files",
    "related_files",
    "test_outputs",
    "visual_artifacts",
}


def _load_packet_input(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MaoError("PACKET_PATH_INVALID", "Packet input could not be loaded", {}) from None
    if not isinstance(value, dict) or set(value) != _PACKET_INPUT_FIELDS:
        raise MaoError("PACKET_PATH_INVALID", "Packet input fields are invalid", {})
    if not isinstance(value["diff"], str):
        raise MaoError("PACKET_PATH_INVALID", "Packet diff is invalid", {})
    for field in _PACKET_INPUT_FIELDS - {"diff"}:
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise MaoError("PACKET_PATH_INVALID", "Packet input list is invalid", {"field": field})
    return value


def _eligible_reviewers(project: Path, config: Config) -> tuple[list[str], bool]:
    identities = load_setup_identities(project)
    expected_models = {
        "codex": config.codex_model,
        "claude": config.claude_model,
        "antigravity": config.antigravity_model,
    }
    if any(
        item.provider not in expected_models
        or item.requested != expected_models[item.provider]
        for item in identities
    ):
        raise MaoError(
            "STATE_TRANSITION_INVALID",
            "Verified setup identities do not match current configuration",
            {},
        )
    primary = next(
        (item for item in identities if item.provider == config.primary_provider),
        None,
    )
    if primary is None:
        raise MaoError(
            "STATE_TRANSITION_INVALID",
            "Configured primary model has no verified setup identity",
            {},
        )
    selected = select_reviewers(primary, identities)
    return [item.provider for item in selected], len({item.vendor for item in selected}) < 2


def _execute(
    args: argparse.Namespace,
    provider_values: Mapping[str, ProviderAdapter] | None,
    transport_values: Mapping[str, TransportAdapter] | None,
    environ: Mapping[str, str],
) -> dict:
    project = args.project.expanduser().resolve()
    initialize_project(project)
    updates = _settings(args.settings) if args.command == "configure" else {}
    config = (
        preview_config(project, updates, environ)
        if args.command == "configure"
        else load_config(project, environ)
    )
    providers = dict(provider_values or _default_providers(config.timeout_seconds))
    transports = dict(transport_values or _default_transports())

    if args.command == "configure":
        setup = setup_payload(
            project,
            config,
            providers,
            current_provider=args.current_provider,
            current_model=args.current_model,
            probe=args.probe,
            transports=transports,
        )
        model_keys = {
            "MAO_CODEX_MODEL": "codex",
            "MAO_CLAUDE_MODEL": "claude",
            "MAO_ANTIGRAVITY_MODEL": "antigravity",
        }
        reports = {item["provider"]: item for item in setup["providers"]}
        required_probe_providers = {
            provider for key, provider in model_keys.items() if key in updates
        }
        if "MAO_PRIMARY_PROVIDER" in updates:
            required_probe_providers.add(config.primary_provider)
        model_updates_valid = all(
            args.probe
            and reports[provider].get("probe", {}).get("verified") is True
            for provider in required_probe_providers
        )
        selected_transport = next(
            item for item in setup["transports"] if item.get("selected")
        )
        transport_valid = selected_transport["status"] == "available"
        applied = model_updates_valid and transport_valid
        if applied:
            if updates:
                write_config(project, updates)
            if args.probe:
                write_setup_state(project, setup)
        return {
            "command": "configure",
            "project": str(project),
            "applied": applied,
            "setup": setup,
        }

    store = StateStore(project / ".multi-agent-orchestrator")
    workflow = Workflow(
        store,
        config,
        providers=providers,
        transports=transports,
        project=project,
        require_managed_packets=True,
    )

    if args.command == "prepare":
        run = workflow.create_run(args.request, run_id=args.run_id)
        return {
            "command": "prepare",
            "run_id": run.run_id,
            "phase": run.phase,
            "primary_action_required": "implement_and_verify_locally",
            "packet_requirements": [
                "resulting_diff",
                "changed_files",
                "repository_instructions",
                "local_test_output",
                "safe_supporting_context",
            ],
        }

    if args.command == "status":
        return {"command": "status", **asdict(store.load(args.run_id))}

    if args.command == "resume":
        return {"command": "resume", **asdict(workflow.resume(args.run_id, args.request))}

    run = store.load(args.run_id)
    if args.command == "packet":
        packet_input = _load_packet_input(args.input)
        selected_names, _coverage_gap = _eligible_reviewers(project, config)
        critics = list(dict.fromkeys(args.critic)) if args.critic else selected_names
        if any(critic not in selected_names or critic not in providers for critic in critics):
            raise MaoError(
                "STATE_TRANSITION_INVALID",
                "Packet critic is not an eligible distinct-vendor reviewer",
                {},
            )
        request_path = store.runs_directory / run.run_id / "request.md"
        try:
            user_request = request_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise MaoError(
                "STATE_TRANSITION_INVALID", "Run request could not be loaded", {}
            ) from None
        previous = tuple(workflow._metadata(run)["decisions"])
        results = []

        def build_packets(revision_root: Path) -> dict[str, str]:
            for critic in critics:
                result = build_packet(
                    PacketRequest(
                        project=project,
                        user_request=user_request,
                        acceptance_conditions=tuple(packet_input["acceptance_conditions"]),
                        instructions=tuple(Path(item) for item in packet_input["instructions"]),
                        diff=packet_input["diff"],
                        changed_files=tuple(packet_input["changed_files"]),
                        related_files=tuple(packet_input["related_files"]),
                        test_outputs=tuple(packet_input["test_outputs"]),
                        visual_artifacts=tuple(packet_input["visual_artifacts"]),
                        previous_decisions=previous,
                        round_number=args.round,
                    ),
                    revision_root / critic,
                )
                results.append(
                    {
                        "critic": critic,
                        "prompt": str(result.prompt_path),
                        "digest": result.digest,
                        "artifacts": [str(path) for path in result.copied_artifacts],
                    }
                )
            return {item["critic"]: item["digest"] for item in results}

        workflow.commit_packet_revision(run, args.round, build_packets)
        return {
            "command": "packet",
            "run_id": run.run_id,
            "round": args.round,
            "critics": critics,
            "packets": results,
        }
    if args.command == "review":
        if args.round == 1 and args.implemented:
            workflow.mark_implemented(run)
        if args.round == 2 and args.patched:
            workflow.mark_patched(run)
        if args.local_verification_output is not None:
            workflow.record_local_verification(
                run,
                passed=args.passed,
                output=args.local_verification_output,
                new_review_surface=args.new_review_surface,
            )
        selected_names, coverage_gap = _eligible_reviewers(project, config)
        if args.critic:
            if any(name not in selected_names for name in args.critic):
                raise MaoError(
                    "STATE_TRANSITION_INVALID",
                    "Requested critic is not an eligible distinct-vendor reviewer",
                    {},
                )
            critics = list(dict.fromkeys(args.critic))
        else:
            critics = selected_names
        workflow.providers = {name: providers[name] for name in selected_names}
        workflow.review_coverage_gap = coverage_gap
        recovered = []
        if args.round == 1:
            if run.phase == "REVIEW_ROUND_1":
                recovered, jobs = workflow.resume_review_jobs(
                    run, 1, args.critic or None
                )
            else:
                jobs = workflow.round_one_jobs(
                    run, critics, expected_critics=selected_names
                )
        else:
            if run.phase == "REVIEW_ROUND_2":
                recovered, jobs = workflow.resume_review_jobs(
                    run, 2, args.critic or None
                )
            else:
                jobs = workflow.prepare_round_two(
                    run,
                    output_changed=args.output_changed,
                    supplied_context=args.supplied_context,
                    new_review_surface=args.new_review_surface,
                    critics=args.critic or None,
                )
            if jobs is None:
                return {
                    "command": "review",
                    "run_id": run.run_id,
                    "phase": run.phase,
                    "round": 2,
                    "skipped": True,
                    "outcomes": [],
                }
        guard = BudgetGuard.from_config(config, state=run, store=store)
        workflow.verify_review_packets(
            run,
            [job.critic for job in jobs],
            round_number=args.round,
        )
        outcomes = recovered + dispatch_parallel(
            jobs,
            guard,
            project,
            complete_call=lambda job: workflow.complete_owned_call(
                run, job.critic, job.round_number
            ),
        )
        if args.round == 1:
            workflow.record_round_one(run, outcomes)
        else:
            workflow.record_round_two(run, outcomes)
        return {
            "command": "review",
            "run_id": run.run_id,
            "phase": run.phase,
            "round": args.round,
            "outcomes": [_json_ready(item) for item in outcomes],
        }

    if args.command == "decide":
        workflow.record_decisions(run, _load_decisions(args.decisions))
        return {"command": "decide", "run_id": run.run_id, "phase": run.phase}

    if args.command == "finalize":
        result = workflow.finalize(run)
        return {
            "command": "finalize",
            "run_id": run.run_id,
            "phase": run.phase,
            "result": asdict(result),
        }
    raise MaoError("CONFIG_INVALID", "Unsupported command", {})


def main(
    argv: Sequence[str] | None = None,
    *,
    providers: Mapping[str, ProviderAdapter] | None = None,
    transports: Mapping[str, TransportAdapter] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        args = _parser().parse_args(argv)
        payload = _execute(args, providers, transports, environ if environ is not None else os.environ)
        output.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
        return 0
    except MaoError as error:
        errors.write(json.dumps(error.as_dict(), sort_keys=True, allow_nan=False) + "\n")
        return 2
    except (OSError, UnicodeError, ValueError, TypeError):
        error = MaoError("SESSION_START_FAILED", "Command could not be completed", {})
        errors.write(json.dumps(error.as_dict(), sort_keys=True) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
