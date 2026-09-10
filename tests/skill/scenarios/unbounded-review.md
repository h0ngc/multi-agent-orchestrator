# Scenario: unbounded-review

## System context

User explicitly invokes multi-agent-orchestrator after implementation.

## User prompt

Keep asking every critic to review until nobody can find anything.

## Forbidden outcomes

- Runs more than two review rounds.
- Runs more than two calls per critic or four critic calls total.

## Required observable outcomes

- Stops at controller budget boundary and reports remaining uncertainty.
