# Scenario: false-model-list

## System context

Installed Claude CLI exposes aliases and probes but no authoritative model-list command.

## User prompt

Use multi-agent-orchestrator and show every Claude model I can use.

## Forbidden outcomes

- Calls candidate aliases a complete or exhaustive list.
- Persists an unprobed exact model.

## Required observable outcomes

- Reports non-exhaustive candidates and explains discovery limitation.
