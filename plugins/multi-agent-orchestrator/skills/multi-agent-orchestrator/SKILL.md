---
name: multi-agent-orchestrator
description: Use when the user explicitly requests bounded cross-vendor implementation review or explicitly invokes multi-agent-orchestrator for a coding or visual change.
---

# Multi-Agent Orchestrator

Do not invoke implicitly. Ordinary implementation, review, or visual requests do not activate this skill unless user explicitly names it or explicitly requests this bounded cross-vendor workflow.

Keep current session as primary implementer and final judge. Critics never receive authority to edit authoritative project. Use bundled `scripts/mao_cli.py`; do not recreate state, budget, model-selection, packet, or resume rules in prose or ad-hoc scripts.

## Start

1. Find repository root. Run commands with `python3 <skill-root>/scripts/mao_cli.py --project <root>`.
2. If project-local `.multi-agent-orchestrator/.env` or verified setup state is missing, run `configure` without probe and pass known `--current-provider` plus `--current-model`. Relay each CLI/auth result, inaccessible reason, discovery source, exhaustive flag, exact candidates, transport availability, and relaunch command. Ask one decision at a time: enabled providers; primary among them; exact model for each enabled provider; transport. Explain probe quota risk. Then pass every explicit selection, including `MAO_ENABLED_PROVIDERS`, with `--probe`. Partial selection makes no probe. Disabled providers are not probed. Persist only after all enabled exact-model probes and transport validation succeed.
3. Run `prepare --request <request>` once. Primary implements request with native tools and runs relevant local verification.
4. Build strict packet-input JSON from resulting diff, changed files, applicable instructions, test output, safe related context, and visual artifacts. Run `packet --run-id <id> --round 1 --input <json>`. Never include secrets.
5. Run `review --run-id <id> --round 1 --implemented --local-verification-output <summary> --passed`. Critics review independently and read only packet workspace.
6. Primary validates every finding. Record exactly one `accepted`, `rejected`, or `needs-proof` decision with rationale, then run `decide`.
7. Primary alone applies accepted fixes and verifies again. Run round 2 only when controller gate accepts changed output, safely supplied context, or new review surface. Rebuild packet first. Never bypass controller budget error.
8. Run `finalize`. Report local verification, accepted/rejected findings, exact reviewer/model coverage, usage availability, and review gaps.

On interruption, use `status` then `resume`; never restart paid calls from memory.

Read only reference needed now:

- State/triage: [workflow.md](references/workflow.md)
- CLI/model/transport: [provider-contracts.md](references/provider-contracts.md)
- Packet/result/visual contract: [review-schema.md](references/review-schema.md)
- Install/update/uninstall: [installation.md](references/installation.md)
