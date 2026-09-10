# Multi-Agent Orchestrator Design

**Date:** 2026-09-09
**Status:** Approved
**Target:** Public GitHub repository and marketplace-distributed plugin/skill

## 1. Purpose

Build one explicitly invoked, cross-vendor orchestration skill. Current host session remains primary implementer and final judge. After primary completes requested changes and local verification, independent models from other vendors review the resulting diff and relevant artifacts. Primary evaluates findings, applies only justified fixes, reruns verification, and performs at most one additional review round.

The system must work without Orca. Direct CLI subprocesses are default transport when Orca is not selected. Orca and tmux remain optional transports behind the same provider contract.

## 2. Goals

- Support Codex, Claude Code, and Antigravity as possible primary hosts.
- Use models from different vendors for independent review.
- Include visual artifacts for Antigravity review when work is visual or multimodal.
- Discover account-accessible models during project setup and record exact requested and resolved model names.
- Explain why any CLI, account, session, or model cannot be accessed.
- Run child CLIs without approval prompts using each CLI's dangerous bypass flag.
- Keep project-specific configuration and run records inside the target project.
- Prevent automatic invocation and unnecessary review spend.
- Enforce finite review and retry budgets in deterministic code.
- Support marketplace installation and project-local installation from a GitHub clone.

## 3. Non-Goals

- Automatic review after every ordinary coding task.
- Critic voting or majority-rule code changes.
- Allowing critics to edit authoritative project files.
- Hiding unavailable model-list capabilities behind guessed model names.
- Estimating subscription quota when a provider does not expose it.
- Automatically committing, pushing, opening pull requests, or deploying.
- Requiring Orca, tmux, MCP, or an external usage-coach service.

## 4. Invocation Contract

The skill is explicit-only. Codex metadata sets:

```yaml
policy:
  allow_implicit_invocation: false
```

Equivalent host-specific registration must avoid automatic hooks and automatic post-edit execution.

Supported user operations:

```text
multi-agent-orchestrator <work request>
multi-agent-orchestrator configure
multi-agent-orchestrator status
multi-agent-orchestrator resume
```

Host UI syntax may differ (`$skill-name` or `/skill-name`), but operation semantics stay identical.

When invoked with a work request:

1. Current host session is primary.
2. Primary implements requested change using native host tools.
3. Primary runs relevant local verification.
4. Controller builds isolated review packets.
5. Eligible critics review independently.
6. Primary classifies each finding.
7. Primary applies accepted fixes and verifies again.
8. A second review round runs only when its gate is satisfied.
9. Primary makes final decision and reports coverage or review gaps.

## 5. High-Level Architecture

```text
Explicit skill invocation
        |
        v
Project configuration gate
        |
        v
Primary native implementation and local verification
        |
        v
Review packet builder
        |
        +-----------> Claude/Codex critic adapter
        |
        +-----------> Antigravity/Gemini critic adapter
                           |
                           v
                    Structured findings
                           |
                           v
                  Primary triage and fixes
                           |
                           v
                 Conditional second review
                           |
                           v
                    Final primary decision
```

Primary session is not spawned by controller during ordinary operation. Controller owns configuration, model discovery, packet construction, critic invocation, usage accounting, loop state, and validation. Primary owns project edits and final judgment.

## 6. Repository and Plugin Layout

```text
multi-agent-orchestrator/
├── .agents/plugins/marketplace.json
├── .claude-plugin/marketplace.json
├── plugins/
│   └── multi-agent-orchestrator/
│       ├── .codex-plugin/plugin.json
│       ├── .claude-plugin/plugin.json
│       └── skills/
│           └── multi-agent-orchestrator/
│               ├── SKILL.md
│               ├── agents/openai.yaml
│               ├── scripts/
│               │   ├── configure.py
│               │   ├── discover_models.py
│               │   ├── build_packet.py
│               │   ├── invoke_critics.py
│               │   ├── loop_guard.py
│               │   ├── install.py
│               │   └── lib/
│               │       ├── config.py
│               │       ├── errors.py
│               │       ├── providers/
│               │       └── transports/
│               └── references/
│                   ├── workflow.md
│                   ├── provider-contracts.md
│                   ├── review-schema.md
│                   └── installation.md
├── tests/
├── docs/
├── README.md
├── LICENSE
└── NOTICE
```

One canonical skill implementation lives under plugin root. Installers copy or register this canonical source; manually maintained duplicate implementations are forbidden.

## 7. Project-Local Configuration

Installation detects target project root and creates only:

```text
target-project/
└── .gitignore
```

Installer appends this exact root-relative ignore rule if no equivalent rule exists:

```gitignore
/.multi-agent-orchestrator/
```

Existing `.gitignore` content, line endings, and terminal newline are preserved. Repeated setup is idempotent. Existing root `.env` files are never read, modified, or reused as orchestrator configuration.

Initial discovery and partial selection create no runtime files. After user explicitly selects enabled providers, primary, exact enabled-provider models, and transport, successful probes create `.multi-agent-orchestrator/.env` plus verified state. User-editable settings include:

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

Runtime environment variables with `MAO_` prefix override file values. Configuration contains no credentials. Provider authentication stays in provider-owned credential stores.

No mandatory `python-dotenv` dependency is introduced. A strict standard-library parser supports only documented `KEY=VALUE`, comments, blank lines, and basic quoting. Unknown keys are preserved when configuration is updated. Generated verification state, discovered models, errors, and timestamps live in `state.json` because they are structured and machine-owned.

## 8. Setup and Model Discovery

Setup performs these stages:

1. Detect host and target project.
2. Find `codex`, `claude`, `agy`, optional `orca`, and optional `tmux`.
3. Record CLI versions.
4. Check authentication or run an isolated no-edit probe when no auth-status command exists.
5. Discover model candidates from provider-supported commands or provider-owned cache/API surfaces.
6. Show discovery source and confidence for every candidate.
7. Ask user to select exact primary and critic models.
8. Probe selected models in an isolated temporary directory.
9. Record requested model, resolved model, version, and verification timestamp.
10. Ask whether this project uses Orca; otherwise select direct transport. tmux remains advanced optional choice.

Provider behavior:

- **Codex:** Prefer app-server model data or provider-owned model cache. Probe selected exact model.
- **Claude Code:** Read the installed CLI's native `/model` menu through an isolated safe-mode PTY when available, then fall back to help aliases. Validate selections through tiny JSON-output probes. Never label either candidate set as exhaustive because neither is an authoritative machine-readable provider list.
- **Antigravity:** Use `agy models`, then probe selected exact model.

Model records include:

```json
{
  "requested_model": "fable",
  "resolved_model": "claude-opus-4-6",
  "cli_version": "2.x",
  "discovery_source": "probe",
  "verified": true,
  "verified_at": "2026-09-09T00:00:00Z"
}
```

Current primary session cannot be silently changed from inside a skill. If selected primary host/model differs from current session, setup writes configuration and returns an exact relaunch command. A generated launcher starts new primary sessions with configured model and bypass flag. Existing sessions retain permissions with which host launched them.

## 9. Dangerous No-Approval Execution Profile

Default profile is `yolo`:

```text
codex  --dangerously-bypass-approvals-and-sandbox
claude --dangerously-skip-permissions
agy    --dangerously-skip-permissions
```

All child CLI invocations use these flags by default. Runtime does not display per-call approval prompts. README and first setup output disclose that these flags disable normal permission protection.

These flags do not grant controller authority to commit, push, deploy, contact third parties, or expand task scope. Existing host-session permissions cannot be retroactively changed. Critics receive dangerous process permissions but are run against isolated packets and are instructed to return structured review only.

## 10. Provider Adapter Contract

Each provider implements:

```text
detect()          -> executable and version
check_auth()      -> authenticated, unavailable, or unknown
list_models()     -> candidates plus discovery source
validate_model()  -> requested and resolved identity
invoke()          -> structured process envelope
parse_result()    -> validated review response
measure_usage()   -> reported tokens/cost or unavailable
```

Shared process envelope:

```json
{
  "status": "ok",
  "provider": "claude",
  "model_requested": "fable",
  "model_resolved": "claude-opus-4-6",
  "attempt": 1,
  "duration_seconds": 21,
  "usage": {},
  "stdout_path": "round-1/claude.stdout",
  "stderr_path": "round-1/claude.stderr"
}
```

Provider-specific command construction stays inside adapter. Orchestrator never concatenates shell command strings from configuration; it passes argument arrays to subprocess APIs.

## 11. Transport Adapter Contract

Supported transports:

- `direct`: one-shot CLI subprocess; default when Orca is not selected.
- `orca`: structured Orca thread/task/session coordination when valid Orca context exists.
- `tmux`: persistent terminal session for users requiring reattachment.

Transport selection does not change provider result schema, loop limits, or reviewer role. Selected transport gets at most configured attempts. Failure does not silently switch transport. Fallback runs only when `MAO_TRANSPORT_FALLBACK` explicitly names one and remaining critic-call budget permits it.

Orca is optional. Direct transport provides complete core behavior. tmux is optional and unavailable on platforms without tmux.

## 12. Review Packet

Critics do not receive unrestricted write access to authoritative project tree as normal input. Controller creates an isolated packet containing:

- Original user request and acceptance conditions.
- Applicable repository instructions.
- Base and resulting diff.
- Full changed-file contents.
- Relevant supporting files selected by primary.
- Local test commands and outputs.
- Screenshots, rendered HTML, images, or sampled video frames for visual work.
- Round number and previous accepted/rejected decisions when applicable.

Packet excludes known secret files, credentials, `.git`, build caches, dependency trees, and unrelated project data. If critic needs missing context, it returns specific `needs_context` paths or questions. Primary may add that context in round two if safe and relevant.

## 13. Review Result Schema

Each critic returns JSON matching a bundled schema:

```json
{
  "summary": "",
  "findings": [
    {
      "severity": "critical|major|minor|note",
      "category": "correctness|requirements|regression|security|maintainability|visual|accessibility|other",
      "file": "relative/path",
      "line": 1,
      "evidence": "",
      "reason": "",
      "suggested_fix": "",
      "confidence": 0.0,
      "needs_context": []
    }
  ],
  "usage": {},
  "review_complete": true
}
```

Malformed output is one failed attempt. Repair/retry consumes both transport attempt and critic-call budget. Controller never invents missing findings or converts free-form prose into accepted changes without primary review.

## 14. Reviewer Selection and Diversity

Reviewer selection uses actual model vendor, not CLI host name:

- OpenAI primary -> Anthropic reviewer plus Google reviewer.
- Anthropic primary -> OpenAI reviewer plus Google reviewer.
- Google primary -> OpenAI reviewer plus Anthropic reviewer.

If Antigravity exposes an Anthropic or OpenAI model, it is classified by model vendor. Setup warns when configured reviewers duplicate primary vendor or each other. Runtime prefers two distinct external vendors. Missing providers produce explicit review gaps rather than substitution with the primary vendor.

Reviewer focus differs without changing result schema:

- General critic: correctness, requirements, regression risk, tests, security, maintainability, and missed edge cases.
- Visual/multimodal critic: hierarchy, consistency, accessibility, interaction, screenshots, rendered output, image/video artifacts, and visual regressions. For nonvisual work it performs general review.

## 15. State Machine and Review Budget

```text
IMPLEMENTED
  -> LOCAL_VERIFIED
  -> REVIEW_ROUND_1
  -> TRIAGED
  -> PATCHED
  -> LOCAL_REVERIFIED
  -> REVIEW_ROUND_2
  -> FINAL_DECISION
  -> DONE
```

Hard limits:

- Maximum review rounds: 2.
- Maximum calls per critic: 2.
- Maximum total critic calls: 4.
- Maximum transport attempts per critic invocation: 2, but every launched model process counts as a critic call.
- Default timeout: 300 seconds.
- No hidden retries.

Round one calls up to two eligible reviewers independently and may run them in parallel. Primary classifies each finding as `accepted`, `rejected`, or `needs-proof` and records rationale.

Round two runs only when at least one condition is true:

- Primary changed code or artifacts because of accepted round-one findings.
- Critic requested specific context that primary supplied.
- Local reverification exposed a new review surface.

Only affected reviewers run in round two. If no accepted finding changes output and no safe context request remains, workflow skips round two. No critic calls occur after round two. Primary resolves remaining questions directly.

Finding deduplication uses stable fingerprint fields: provider-independent normalized file, line region, category, and evidence hash. Duplicate wording does not create another required fix.

## 16. Usage Accounting

Controller records token and cost metadata only when provider returns it. It records `USAGE_UNAVAILABLE` when provider exposes no reliable usage or remaining-quota signal. It never estimates account quota from elapsed time, output length, or undocumented heuristics.

Deterministic call, round, retry, timeout, and packet-size limits protect spend even when quota data is unavailable. Quota exhaustion stops that provider immediately and records degraded review coverage.

## 17. Persistence and Resume

Each run lives under:

```text
.multi-agent-orchestrator/runs/<run-id>/
├── request.md
├── packet/
├── round-1/
├── round-2/
├── decisions.json
├── usage.json
└── state.json
```

State writes are atomic through temporary-file replace. Run identity includes project identity, request digest, and creation timestamp. Packet digest and per-reviewer status prevent repeating completed calls after interruption. Resume rejects changed packet contents unless primary explicitly creates a new revision within same bounded run.

## 18. Error Handling

Stable reason codes:

```text
CLI_NOT_FOUND
AUTH_REQUIRED
AUTH_EXPIRED
MODEL_LIST_UNSUPPORTED
MODEL_UNAVAILABLE
SESSION_START_FAILED
QUOTA_EXHAUSTED
NETWORK_ERROR
TIMEOUT
INVALID_RESULT
CRITIC_MUTATED_WORKSPACE
REVIEW_BUDGET_EXHAUSTED
USAGE_UNAVAILABLE
```

One critic failure does not discard another critic's valid review. Two unavailable critics produce `COMPLETED_WITH_REVIEW_GAP`, not fully verified status. Primary implementation and local tests remain reportable, but final output identifies missing independent coverage.

Controller snapshots authoritative project status before and after critic execution. Any detected critic-originated mutation causes result rejection and `CRITIC_MUTATED_WORKSPACE`. Because dangerous bypass disables OS protections, this check detects accidental workflow violations but is not an operating-system security boundary.

## 19. Installation and Distribution

Distribution supports:

1. Codex marketplace.
2. Claude marketplace.
3. Antigravity plugin installation/import where supported.
4. GitHub clone plus project-local installer.

Project-local installer places host-recognized skill files for Codex and Claude and adds runtime ignore rule, but does not initialize runtime configuration. Successful first-run setup creates project-local config and state. Current Antigravity CLI exposes `agy plugin install <target>` and can register a locally cloned plugin. That registration may live in Antigravity's user plugin registry even though plugin source is local; runtime configuration and run records remain project-local.

README documents host-specific installation commands, exact uninstallation steps, dangerous execution defaults, supported platforms, and Antigravity registration limitation.

Initial platform target is macOS and Linux. Direct transport code uses portable Python subprocess APIs and path handling suitable for later native Windows verification. tmux is disabled when unavailable. Windows support is not claimed until acceptance suite passes there.

## 20. Test Strategy

Skill behavior follows RED-GREEN-REFACTOR:

1. Create pressure scenarios without skill and record baseline failures.
2. Write minimal skill guidance and deterministic scripts addressing observed failures.
3. Run same scenarios with skill.
4. Add only corrections supported by observed loopholes.

Offline tests use fake executables and no paid model calls. Coverage includes:

- CLI detection, version parsing, and authentication status.
- Model-list parsing and requested/resolved identity.
- Honest non-exhaustive Claude candidate behavior.
- Exact dangerous flags for all three CLIs.
- Safe subprocess argument-array construction.
- `.env` parsing, preservation, precedence, and invalid-value errors.
- Project-root detection and idempotent `.gitignore` update.
- State atomicity and interrupted-run resume.
- Packet content and secret exclusion.
- Parallel critic dispatch.
- Hard call, round, retry, and timeout limits.
- Conditional second-round gate.
- Finding fingerprint deduplication.
- Invalid JSON/schema handling.
- Critic mutation detection.
- Stable error codes and degraded completion statuses.
- Plugin and marketplace manifest validation.
- Project-local install, update, and uninstall behavior.

Real CLI integration tests are separate, explicit, and potentially billable. They validate available local accounts with tiny isolated probes and record exact CLI/model versions. Release acceptance requires successful installation and explicit invocation smoke tests on Codex, Claude Code, and Antigravity, or documented host-specific limitation.

## 21. Security Boundaries

- Dangerous bypass is default and clearly disclosed.
- No secrets are written to project configuration or run artifacts.
- Logs redact likely tokens and credentials.
- Review packets exclude conventional secret paths and files.
- Commands use argument arrays and command allowlists.
- Model names and paths are validated before process launch.
- Critics receive isolated packet working directories.
- Primary alone applies authoritative edits.
- No commit, push, PR, deployment, or external communication is inferred from skill invocation.
- Packet isolation and mutation checks reduce accidental edits but do not sandbox a malicious or compromised CLI process running with dangerous bypass.

## 22. Licensing

Repository uses MIT license. New implementation is written independently while using `multi-agent-starter` as architectural reference. If implementation copies substantial code or documentation, original MIT copyright notice and attribution are retained in `NOTICE`. Third-party code is not vendored unless its license and update strategy are documented.

## 23. Acceptance Criteria

Implementation is acceptable when:

1. Skill runs only after explicit invocation.
2. First project invocation creates local `.env`, state, runs directory, and idempotent ignore rule.
3. Setup reports CLI, auth, model discovery, selected model probe, and exact failure reasons.
4. Requested and resolved model identities are persisted.
5. Direct transport works without Orca or tmux.
6. Optional Orca and tmux transports honor same result and budget contracts.
7. Primary performs implementation; critics return review only.
8. Reviewer diversity uses actual model vendor.
9. Visual tasks can include screenshots and other rendered artifacts.
10. Critic calls can never exceed configured hard caps.
11. Second review runs only through documented gate.
12. Interrupted runs resume without duplicating completed model calls.
13. Usage is recorded honestly without quota guessing.
14. All offline tests and manifest validators pass.
15. Real-host smoke tests document exact tested versions and any limitations.
16. No commit, push, PR, or deployment happens without separate user request.

## 24. Approved Decisions

- New standalone repository: `/Users/chanhong/Desktop/hcg_coding/multi-agent-orchestrator`.
- New lightweight plugin and Python controller, not a heavy fork.
- Explicit invocation only.
- Current host session is primary implementer and final judge.
- Project-local `.multi-agent-orchestrator/.env` plus machine-owned JSON state.
- Automatic `.gitignore` entry for local runtime directory.
- Dangerous bypass flags are default for child CLIs.
- Exact selected model probing and honest discovery limitations.
- Direct transport works independently; Orca and tmux are optional.
- Two rounds maximum, two calls per critic maximum, four critic calls maximum.
- Structured critic findings and primary-owned triage.
- Marketplace and GitHub clone/project-local installation.
