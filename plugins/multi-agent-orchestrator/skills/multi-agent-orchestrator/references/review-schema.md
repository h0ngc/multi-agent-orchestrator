# Packet and review contract

Create packet-input JSON with exactly:

```json
{
  "acceptance_conditions": ["..."],
  "instructions": ["AGENTS.md"],
  "diff": "unified diff",
  "changed_files": ["relative/path"],
  "related_files": ["relative/path"],
  "test_outputs": ["command and result"],
  "visual_artifacts": ["relative/screenshot.png"]
}
```

Paths are project-relative. Exclude credentials, `.env*`, keys, `.git`, dependencies, build output, and unrelated data. Controller filters diff paths, copies safe visual artifacts with hashes, and rejects traversal/symlinks. For visual work include rendered screenshots or output when available so Antigravity/visual-capable critic can assess hierarchy, consistency, accessibility, interaction, and regressions.

Critics return strict JSON matching `scripts/review.schema.json`: summary, findings, usage, and `review_complete`. Each finding includes severity, category, file, line, evidence, reason, suggested fix, confidence, and safe `needs_context`. Free-form or malformed output is failed attempt, not accepted review.

If context is missing, critic asks for concise question or safe relative path. Primary decides whether disclosure is safe. Supplying context may gate round 2; it never overrides secret exclusions.
