# Release acceptance

Status: published direct-provider release candidate. Exact model probes and isolated strict-review smoke passed on installed Codex, Claude Code, and Antigravity CLIs. Public GitHub repository plus Codex and Claude remote marketplace flows are live. Antigravity uses verified local plugin registration because CLI 1.2.0 cannot consume either remote marketplace format.

Offline tests use fake executables and deterministic pressure fixtures. Billable smoke evidence below proves current local account/model access and one strict direct review per provider; it does not prove every full primary-host workflow, Orca path, or future model availability.

Host | CLI version | Primary model | Critic models | Transport | Install | Configure | Round 1 | Round 2 gate | Resume | Result
--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---
Codex | 0.153.0 | gpt-6-astra | claude-opus-4-6, gemini-3.1-pro-high | direct | Offline pass | Exact probe pass | Strict smoke pass | Offline pass | Offline pass | Provider smoke pass
Claude Code | 2.1.220 | claude-opus-4-6 | gpt-6-astra, gemini-3.1-pro-high | direct | Offline pass | Exact provider-reported probe pass | Strict smoke pass | Offline pass | Offline pass | Provider smoke pass
Antigravity | 1.2.0 | gemini-3.1-pro-high | gpt-6-astra, claude-opus-4-6 | direct | Local plugin registration pass | Exact request probe pass | Strict smoke pass | Offline pass | Offline pass | Provider smoke pass
Cross-host | Fake CLIs | Fake exact identities | Fake distinct vendors | direct/orca/tmux | Pass | Pass | Pass | Pass | Pass | 405-test offline gate pass

## Offline evidence

- Explicit invocation static policy check: pass, 5 deterministic repetitions.
- Bounded review static policy check: pass, 5 deterministic repetitions.
- Primary-only write static policy check: pass, 5 deterministic repetitions.
- Honest model discovery static policy check: pass, 5 deterministic repetitions.
- Repetitions check policy markers and deterministic predictions; they are not real-agent behavioral trials.
- Skill validator: pass.
- Real direct provider smoke: pass for all three selected models.
- Codex and Antigravity CLIs accepted exact requested model IDs but did not echo model IDs; evidence records `requested_fallback`. Claude returned exact provider-reported model ID.
- Fresh project-local setup passed with Codex `gpt-5.6-sol`, Claude selector `sonnet[1m]` resolved as `claude-sonnet-5[1m]`, Antigravity `gemini-3.8-flash-medium`, and direct transport.
- Claude CLI 2.1.220 native `/model` menu was read without a model call and exposed `sonnet[1m]` as `Sonnet 5 (1M context)`; fallback help aliases remain non-exhaustive.
- Full primary-host workflow, Orca live workflow, and tmux live workflow: Not run.

## Publication evidence

- Public repository: `https://github.com/h0ngc/multi-agent-orchestrator`.
- Published branch: `feature/initial-build`; no direct `master` or `main` push.
- Codex CLI 0.153.0: remote marketplace add, plugin install, and plugin remove passed.
- Claude Code 2.1.220: remote marketplace add, project-local plugin install, and plugin uninstall passed.
- Antigravity CLI 1.2.0: remote marketplace reference failed with `unknown marketplace`; Claude import found no compatible extension. Absolute local plugin registration passed and remains installed.

## Explicit real-host gate

Set `MAO_RUN_REAL_CLI_TESTS=1` plus exact `MAO_REAL_CODEX_MODEL`, `MAO_REAL_CLAUDE_MODEL`, and `MAO_REAL_ANTIGRAVITY_MODEL`, then run:

```bash
python3 -m pytest -m integration tests/integration/test_real_cli_smoke.py -v
```

Record exact CLI version, requested/resolved model, transport, install/configure result, review result, resume behavior, and limitation in table. Never convert skipped or unavailable provider into pass.
