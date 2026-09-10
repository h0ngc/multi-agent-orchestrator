# Release acceptance

Status: direct-provider release candidate. Exact model probes and isolated strict-review smoke passed on installed Codex, Claude Code, and Antigravity CLIs. Remote marketplace publication remains open.

Offline tests use fake executables and deterministic pressure fixtures. Billable smoke evidence below proves current local account/model access and one strict direct review per provider; it does not prove every full primary-host workflow, Orca path, or future model availability.

Host | CLI version | Primary model | Critic models | Transport | Install | Configure | Round 1 | Round 2 gate | Resume | Result
--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---
Codex | 0.153.0 | gpt-6-astra | claude-opus-4-6, gemini-3.1-pro-high | direct | Offline pass | Exact probe pass | Strict smoke pass | Offline pass | Offline pass | Provider smoke pass
Claude Code | 2.1.220 | claude-opus-4-6 | gpt-6-astra, gemini-3.1-pro-high | direct | Offline pass | Exact provider-reported probe pass | Strict smoke pass | Offline pass | Offline pass | Provider smoke pass
Antigravity | 1.2.0 | gemini-3.1-pro-high | gpt-6-astra, claude-opus-4-6 | direct | Registration validator pass | Exact request probe pass | Strict smoke pass | Offline pass | Offline pass | Provider smoke pass
Cross-host | Fake CLIs | Fake exact identities | Fake distinct vendors | direct/orca/tmux | Pass | Pass | Pass | Pass | Pass | 403-test offline gate pass

## Offline evidence

- Explicit invocation static policy check: pass, 5 deterministic repetitions.
- Bounded review static policy check: pass, 5 deterministic repetitions.
- Primary-only write static policy check: pass, 5 deterministic repetitions.
- Honest model discovery static policy check: pass, 5 deterministic repetitions.
- Repetitions check policy markers and deterministic predictions; they are not real-agent behavioral trials.
- Skill validator: pass.
- Real direct provider smoke: pass for all three selected models.
- Codex and Antigravity CLIs accepted exact requested model IDs but did not echo model IDs; evidence records `requested_fallback`. Claude returned exact provider-reported model ID.
- Full primary-host workflow, Orca live workflow, and tmux live workflow: Not run.

## Explicit real-host gate

Set `MAO_RUN_REAL_CLI_TESTS=1` plus exact `MAO_REAL_CODEX_MODEL`, `MAO_REAL_CLAUDE_MODEL`, and `MAO_REAL_ANTIGRAVITY_MODEL`, then run:

```bash
python3 -m pytest -m integration tests/integration/test_real_cli_smoke.py -v
```

Record exact CLI version, requested/resolved model, transport, install/configure result, review result, resume behavior, and limitation in table. Never convert skipped or unavailable provider into pass.
