# Multi-Agent Orchestrator

Project-local skill/plugin for one primary coding agent to implement a request, obtain bounded independent reviews from other model vendors, judge those findings, and optionally run one targeted follow-up review.

## Purpose and explicit-only behavior

This skill runs only when user explicitly invokes `$multi-agent-orchestrator` or clearly requests its bounded cross-vendor workflow. Installing it does not add critic calls to ordinary coding tasks.

Current Codex, Claude Code, or Antigravity session remains primary implementer and final judge. Primary alone edits project files. Up to two eligible critics from distinct non-primary model vendors receive read-only review packets and return structured findings.

## Security warning

Default child launchers deliberately disable approval and permission prompts:

```text
codex --model <model> --dangerously-bypass-approvals-and-sandbox
claude --model <model> --dangerously-skip-permissions
agy --model <model> --dangerously-skip-permissions
```

Use only with CLIs, plugins, repositories, and prompts you trust. Packet isolation and before/after mutation detection reduce accidental writes but are not an OS security boundary. A compromised CLI process can still access data available to your account. Secret-path filtering is defense in depth, not a substitute for OS isolation.

The orchestrator never infers permission to commit, push, create PRs, deploy, or contact external people.

## Marketplace installation

Repository marketplace: `https://github.com/h0ngc/multi-agent-orchestrator`.

Codex:

```bash
codex plugin marketplace add h0ngc/multi-agent-orchestrator
codex plugin add multi-agent-orchestrator@multi-agent-orchestrator
```

Claude Code:

```bash
claude plugin marketplace add h0ngc/multi-agent-orchestrator
claude plugin install multi-agent-orchestrator@multi-agent-orchestrator --scope project
```

Repository publication and both remote marketplace flows were verified on 2026-09-10 with Codex CLI 0.153.0 and Claude Code 2.1.220. Marketplace registration makes plugin discoverable; runtime configuration remains project-local.

Codex metadata explicitly sets `allow_implicit_invocation: false`. Claude discovers bundled skill from plugin `skills/` directory.

## Project-local installation

Clone repository, then run Python 3.10+ installer. It copies canonical skill into selected project without global skill installation:

```bash
git clone https://github.com/h0ngc/multi-agent-orchestrator.git
cd multi-agent-orchestrator
```

Install:

```bash
python3 plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py install \
  --project /absolute/path/to/project \
  --host codex --host claude
```

Targets:

- Codex: `.agents/skills/multi-agent-orchestrator/`
- Claude Code: `.claude/skills/multi-agent-orchestrator/`

Rerun same command to update managed files. Manifest-listed files are replaced; unrelated files remain untouched. Unsafe symlinks, traversal, and unmanaged filename collisions fail before install.

Install only copies skill files and adds `/.multi-agent-orchestrator/` to project `.gitignore`. It does not create `.env`, state, or run records before setup succeeds.

## First-run setup

Set controller path from installed skill, then inspect available CLIs, auth, model candidates, probes, transports, and exact relaunch command:

```bash
python3 .agents/skills/multi-agent-orchestrator/scripts/mao_cli.py \
  --project /absolute/path/to/project configure \
  --current-provider codex --current-model <current-exact-model>
```

Candidate lists are marked exhaustive only when installed CLI supplies authoritative enumeration. Claude discovery first opens an isolated safe-mode CLI session and reads its native `/model` menu without making a model call; selectors such as `sonnet[1m]` remain non-exhaustive because the menu is not a machine-readable provider API. If `expect` or the interactive menu is unavailable, discovery falls back to aliases from `claude --help`. Codex cache entries are also non-exhaustive. Inaccessible sessions include exact reason.

Choose in order: enabled providers, primary provider, exact model for each enabled provider, then transport. Initial persistence requires every selection explicitly, including `MAO_ENABLED_PROVIDERS`; disabled providers remain visible for diagnosis but are not probed or used:

```bash
python3 .agents/skills/multi-agent-orchestrator/scripts/mao_cli.py \
  --project /absolute/path/to/project configure \
  --set MAO_PRIMARY_PROVIDER=codex \
  --set MAO_ENABLED_PROVIDERS=codex,claude,antigravity \
  --set MAO_CODEX_MODEL=gpt-model \
  --set MAO_CLAUDE_MODEL=claude-model \
  --set MAO_ANTIGRAVITY_MODEL=gemini-model \
  --set MAO_TRANSPORT=direct \
  --current-provider codex --current-model <current-exact-model> --probe
```

Initial discovery does not launch model probes or create runtime configuration. Partial initial selections return `missing_selections` and also make no model call. Configuration changes without `--probe` are rejected. After user chooses exact models, `--probe` launches tiny calls only for enabled providers and may consume quota. `.env` and verified state persist only when every enabled exact-model probe and selected transport validation succeeds. Controller cannot detect whether current host session already uses dangerous permission mode; follow returned relaunch command when required. If current model is unknown, setup conservatively requests relaunch.

## Invocation

Invoke skill explicitly in host chat:

```text
Use $multi-agent-orchestrator to implement this request and run bounded cross-vendor review: <request>
```

Primary follows controller sequence: `prepare` → native implementation/local tests → strict `packet` → review round 1 → primary `decide` → accepted fixes/local reverification → gated packet/review round 2 → `finalize`. `status` and request-digest-bound `resume` recover interrupted work without repeating completed calls.

Packet input includes acceptance conditions, applicable instruction paths, unified diff, changed/related files, test outputs, and optional visual artifacts. Visual tasks should include screenshots or rendered output when safe. Critics receive only filtered packet context.

## Project-local environment

After successful setup, runtime state lives under `.multi-agent-orchestrator/`; installer adds exact `/.multi-agent-orchestrator/` rule to project `.gitignore`. Local settings live in `.multi-agent-orchestrator/.env`, not global agent configuration.

Defaults:

```dotenv
MAO_PRIMARY_PROVIDER=codex
MAO_ENABLED_PROVIDERS=codex,claude,antigravity
MAO_CODEX_MODEL=gpt-6-astra
MAO_CLAUDE_MODEL=claude-opus-4-6
MAO_ANTIGRAVITY_MODEL=gemini-3.1-pro-high
MAO_TRANSPORT=direct
MAO_TRANSPORT_FALLBACK=
MAO_EXECUTION_PROFILE=yolo
MAO_MAX_REVIEW_ROUNDS=2
MAO_MAX_CALLS_PER_CRITIC=2
MAO_MAX_TOTAL_CRITIC_CALLS=4
MAO_MAX_TRANSPORT_ATTEMPTS=2
MAO_TIMEOUT_SECONDS=300
```

Process environment overrides project `.env`. Limits cannot exceed compiled hard maxima.

## Loop and usage limits

- Maximum review rounds: 2
- Maximum calls per critic: 2
- Maximum critic calls total: 4
- Maximum concurrent critics: 2
- Maximum transport attempts per invocation: 2; each launched model process consumes critic-call budget

Round 2 runs only after accepted-finding output change, safely supplied requested context, or persisted new review surface. No “review until perfect” request overrides limits. Provider-native usage is recorded when available; missing usage is reported, never guessed.

## Optional Orca and tmux transports

`direct` needs neither Orca nor tmux. `orca` coordinates managed workers/terminals and must prove unattended bypass before selection persists. `tmux` uses local sessions and reports unavailable when binary is missing. Transport choice does not change schema or budget.

Fallback is never silent. `MAO_TRANSPORT_FALLBACK` must explicitly name it, and fallback consumes remaining critic-call budget.

## Antigravity registration limitation

Antigravity CLI 1.2.0 does not consume this repository through Codex or Claude marketplace registration. `agy plugin install multi-agent-orchestrator@multi-agent-orchestrator` reports `unknown marketplace`, and `agy plugin import claude` does not discover the Claude marketplace plugin.

Use clone plus local plugin registration instead:

```text
agy plugin install <absolute-local-plugin-path>
```

For this repository, `<absolute-local-plugin-path>` is the absolute path to `plugins/multi-agent-orchestrator`. This registration flow was verified with Antigravity CLI 1.2.0. It mutates Antigravity user plugin registry. Installer does not execute it unless caller explicitly adds `--register-antigravity`. Runtime `.env`, state, packets, and run records remain project-local.

## Troubleshooting

Errors are JSON with stable code and safe details:

- `CLI_NOT_FOUND`, `AUTH_REQUIRED`, `AUTH_EXPIRED`: install/login to named CLI.
- `MODEL_LIST_UNSUPPORTED`: candidate enumeration unavailable; use non-exhaustive candidates plus exact probe.
- `MODEL_UNAVAILABLE`: exact selected model probe failed.
- `SESSION_START_FAILED`, `NETWORK_ERROR`, `TIMEOUT`, `QUOTA_EXHAUSTED`: provider/transport attempt failed; no silent fallback.
- `INVALID_RESULT`: critic output failed strict schema.
- `PACKET_PATH_INVALID`: packet path/content violated safety contract.
- `CRITIC_MUTATED_WORKSPACE`: critic changed authoritative project; result rejected.
- `REVIEW_BUDGET_EXHAUSTED`: hard call/round boundary reached; stop.
- `USAGE_UNAVAILABLE`: provider did not return reliable usage.
- `REVIEW_SCOPE_TOO_LARGE`: complete text packet exceeds 128 KiB. Split change by logical scope; never truncate modified code.
- `CONFIG_INVALID`, `INSTALL_INVALID`, `STATE_TRANSITION_INVALID`: correct local config, installation ownership, or command order.

One critic failure preserves valid sibling review but final result reports coverage gap. Two unavailable critics cannot produce fully verified status.

## Uninstall

Remove only installer-managed Codex/Claude files:

```bash
python3 plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py uninstall \
  --project /absolute/path/to/project --host codex --host claude
```

Unrelated files in skill directories remain. Runtime `.multi-agent-orchestrator/` is intentionally retained because it contains user-selected config and audit records; remove it manually only after preserving anything needed.

Antigravity registry removal is external mutation and runs only with explicit `uninstall --host antigravity --register-antigravity`.

## Development and tests

Offline release gate makes no model calls. Pressure outputs are static policy checks, not real-agent behavioral evidence:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -m "not integration" -q
python3 /Users/chanhong/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator
python3 /Users/chanhong/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  plugins/multi-agent-orchestrator
```

Billable real-host smoke requires explicit opt-in and exact model env values:

```bash
MAO_RUN_REAL_CLI_TESTS=1 \
MAO_REAL_CODEX_MODEL=<exact> \
MAO_REAL_CLAUDE_MODEL=<exact> \
MAO_REAL_ANTIGRAVITY_MODEL=<exact> \
python3 -m pytest -m integration tests/integration/test_real_cli_smoke.py -v -s
```

## License and attribution

MIT licensed. Implementation was written independently. `multi-agent-starter` by netwaif was used as architectural reference under MIT; see `NOTICE`.
