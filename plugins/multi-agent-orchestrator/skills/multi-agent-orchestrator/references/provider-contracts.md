# Provider and transport contract

First run `configure` without `--probe`, passing known `--current-provider` and `--current-model`. Display installed CLI status, authentication status, candidate models, discovery source, and `exhaustive` value. Claude discovery reads installed CLI native `/model` menu through isolated safe-mode PTY and reports selectors such as `sonnet[1m]`; if `expect` or menu is inaccessible, it falls back to `claude --help`. Both Claude sources remain non-exhaustive. Other candidate aliases are also non-exhaustive unless installed CLI provides authoritative enumeration. Ask one decision at a time: enabled providers, primary provider among them, exact model for each enabled provider, transport. Warn probes launch tiny calls and may consume quota. Then run all explicit selections, including `MAO_ENABLED_PROVIDERS`, with `--probe` and both current-session arguments. Partial initial selection launches no probes. Disabled providers stay diagnostic-only. Probe exact selection before persistence. Persist only after every enabled provider and selected transport validate. If session cannot access provider, report typed reason; never invent model names.

Default unattended launchers:

```text
codex --model <model> --dangerously-bypass-approvals-and-sandbox
claude --model <model> --dangerously-skip-permissions
agy --model <model> --dangerously-skip-permissions
```

Controller cannot detect current host permission mode. When setup reports relaunch required, use/report exact command and never claim current session changed modes.

Transports:

- `direct`: subprocess with argument arrays, no shell.
- `orca`: Orca-managed worker/terminal lifecycle; selected only after unattended-bypass validation.
- `tmux`: local CLI session transport; unavailable when tmux is absent.

No silent transport fallback. Explicit configured fallback consumes another paid critic-call reservation. Reviewer eligibility uses resolved model vendor, excludes primary vendor, and selects at most one reviewer per remaining vendor.
