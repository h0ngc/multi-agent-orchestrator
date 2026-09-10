# Provider and transport contract

First run `configure` without `--probe`, passing known `--current-provider`, `--current-model`, and `--current-effort`. Display installed CLI status, authentication status, candidate models and efforts, discovery sources, and each `exhaustive` value. No browser or web search is used for model or effort discovery.

Codex models and per-model efforts come from local `models_cache.json`. Claude models come from installed CLI native `/model` menu through isolated safe-mode PTY; fallback aliases and effort values come from `claude --help`. Antigravity models come from `agy models`; effort values come from `agy --help`. Non-authoritative sources remain non-exhaustive. If local discovery fails, report reason; never invent names.

Ask one decision at a time: enabled providers, primary provider among them, exact model then exact effort for each enabled provider, transport. Warn probes launch tiny calls and may consume quota. Then run all explicit selections, including `MAO_ENABLED_PROVIDERS`, with `--probe` and current-session arguments. Partial initial selection launches no probes. Disabled providers stay diagnostic-only. Probe exact selection before persistence; each probe validates exact model and exact effort together. Persist only after every enabled provider and selected transport validate.

Default unattended launchers:

```text
codex --model <model> -c model_reasoning_effort="<effort>" --dangerously-bypass-approvals-and-sandbox
claude --model <model> --effort <effort> --dangerously-skip-permissions
agy --model <model> --effort <effort> --dangerously-skip-permissions
```

Controller cannot detect current host permission mode. When setup reports relaunch required, use/report exact command and never claim current session changed modes.

Transports:

- `direct`: subprocess with argument arrays, no shell.
- `orca`: Orca-managed worker/terminal lifecycle; selected only after unattended-bypass validation.
- `tmux`: local CLI session transport; unavailable when tmux is absent.

No silent transport fallback. Explicit configured fallback consumes another paid critic-call reservation. Reviewer eligibility uses resolved model vendor, excludes primary vendor, and selects at most one reviewer per remaining vendor.
