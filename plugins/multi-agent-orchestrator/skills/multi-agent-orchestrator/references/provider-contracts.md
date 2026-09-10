# Provider and transport contract

First run `configure` without `--probe`, passing known `--current-provider` and `--current-model`. Display installed CLI status, authentication status, candidate models, discovery source, and `exhaustive` value. Claude discovery reads the installed CLI's native `/model` menu through an isolated safe-mode PTY and reports selectors such as `sonnet[1m]`; if `expect` or that menu is inaccessible, it falls back to `claude --help`. Both Claude sources remain non-exhaustive. Other candidate aliases are also non-exhaustive unless installed CLI provides authoritative enumeration. Ask user to choose exact models, warn that probes launch tiny calls and may consume quota, then run selected `--set` values with `--probe` and both current-session arguments. Probe exact selection before persistence. If session cannot access provider, report typed reason; never invent model names.

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
