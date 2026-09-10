# Installation contract

From cloned repository, run canonical installer with Python 3.10+:

```bash
python3 plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py install --project /absolute/project --host codex --host claude
```

Codex target: `.agents/skills/multi-agent-orchestrator/`.
Claude target: `.claude/skills/multi-agent-orchestrator/`.
Runtime configuration: `.multi-agent-orchestrator/.env`, ignored by project `.gitignore`.

Antigravity selection reports `agy plugin install <absolute-local-plugin-path>` but does not mutate user registry. Execute registration only with explicit request:

```bash
python3 plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py install --project /absolute/project --host antigravity --register-antigravity
```

Managed update reruns `install`. It replaces only prior manifest-listed files and preserves unrelated files. Managed uninstall:

```bash
python3 plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py uninstall --project /absolute/project --host codex --host claude
```

Antigravity uninstall registration is also external user-registry mutation and requires explicit `--register-antigravity` with `uninstall --host antigravity`.
