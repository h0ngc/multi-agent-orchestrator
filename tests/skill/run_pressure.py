#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = Path(__file__).resolve().parent / "scenarios"
SKILL = (
    ROOT
    / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/SKILL.md"
)
REFERENCES = SKILL.parent / "references"


def _policy_text(phase: str) -> str:
    if phase == "baseline":
        return ""
    values = [SKILL.read_text(encoding="utf-8")]
    values.extend(
        path.read_text(encoding="utf-8")
        for path in sorted(REFERENCES.glob("*.md"))
    )
    return "\n".join(values)


def _evaluate(identifier: str, policy: str) -> dict:
    if identifier == "implicit-invocation":
        safe = "Do not invoke implicitly" in policy
        return {"policy_predicts_implicit_critic_start": not safe, "passed": safe}
    if identifier == "unbounded-review":
        bounded = all(
            marker in policy
            for marker in ("Maximum rounds: 2", "Maximum calls per critic: 2", "Maximum total critic calls: 4")
        )
        return {
            "rounds": 2 if bounded else 5,
            "calls_per_critic": 2 if bounded else 5,
            "total_calls": 4 if bounded else 10,
            "passed": bounded,
        }
    if identifier == "critic-edits":
        safe = "Critics never edit authoritative project files" in policy
        return {"policy_predicts_critic_project_write": not safe, "passed": safe}
    if identifier == "false-model-list":
        safe = bool(
            re.search(r"non-exhaustive", policy, re.IGNORECASE)
            and "Probe exact selection before persistence" in policy
        )
        return {
            "policy_predicts_exhaustive_claim": not safe,
            "policy_predicts_unprobed_persistence": not safe,
            "passed": safe,
        }
    raise ValueError(f"unknown scenario: {identifier}")


def run(phase: str, repetitions: int) -> dict:
    policy = _policy_text(phase)
    scenarios = {}
    for path in sorted(SCENARIOS.glob("*.md")):
        identifier = path.stem
        outputs = [_evaluate(identifier, policy) for _ in range(repetitions)]
        scenarios[identifier] = {
            "outputs": outputs,
            "passed": all(item["passed"] for item in outputs),
        }
    return {
        "format_version": 1,
        "method": "offline_static_policy_check",
        "phase": phase,
        "repetitions": repetitions,
        "all_passed": all(item["passed"] for item in scenarios.values()),
        "scenarios": scenarios,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("baseline", "with-skill"), required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    result = run(args.phase, args.repetitions)
    output = args.output or Path(__file__).with_name(f"{args.phase}-results.json")
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if (args.phase == "baseline" or result["all_passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
