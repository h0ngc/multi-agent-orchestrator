# Multi-Agent Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build explicitly invoked cross-vendor orchestration plugin whose current primary session implements work, independent vendor critics review it, and deterministic code enforces setup, isolation, usage accounting, resume, and two-round limits.

**Architecture:** One canonical skill ships inside dual Codex/Claude marketplace plugin. Standard-library Python controller manages project-local configuration, provider and transport adapters, review packets, structured critic results, and persistent finite-state runs. Current host remains primary and only authoritative writer.

**Tech Stack:** Python 3.10+ standard library at runtime, pytest 8 for development, JSON/JSON Schema assets, Codex CLI, Claude Code CLI, Antigravity agy CLI, optional Orca CLI, optional tmux.

**Spec:** docs/superpowers/specs/2026-09-09-multi-agent-orchestrator-design.md

## Global Constraints

- Skill invocation is explicit-only; no automatic hooks or post-edit review.
- Current host session is primary implementer and final judge.
- Child Codex uses --dangerously-bypass-approvals-and-sandbox.
- Child Claude uses --dangerously-skip-permissions.
- Child agy uses --dangerously-skip-permissions.
- Existing primary-session permission mode cannot be changed from inside skill.
- Runtime dependencies use Python 3.10+ standard library only; python-dotenv is not required.
- Project settings live under .multi-agent-orchestrator/ and root .gitignore contains /.multi-agent-orchestrator/.
- Credentials remain in provider-owned stores and never enter config, packets, state, or logs.
- Maximum review rounds is 2, maximum calls per critic is 2, and maximum total critic calls is 4.
- Every launched model process, including retry and schema-repair attempts, consumes critic-call budget.
- Primary alone edits authoritative project files.
- Direct transport works without Orca or tmux.
- Reviewer diversity is based on actual model vendor, not CLI host.
- Account quota is never guessed when provider does not expose it.
- Initial verified platforms are macOS and Linux; native Windows support is not claimed before acceptance passes.
- Do not commit, push, create PR, deploy, or contact external systems unless user separately requests it.
- Existing repository rule replaces plan commit steps with local checkpoints. If user later authorizes commits, use feature branch and never push directly to master.

## Planned File Map

~~~text
.agents/plugins/marketplace.json                         Codex marketplace catalog
.claude-plugin/marketplace.json                         Claude marketplace catalog
plugins/multi-agent-orchestrator/.codex-plugin/plugin.json
plugins/multi-agent-orchestrator/.claude-plugin/plugin.json
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/SKILL.md
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/agents/openai.yaml
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/workflow.md
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/provider-contracts.md
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/review-schema.md
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/installation.md
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_cli.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/config.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/errors.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/process.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/models.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/providers/*.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/transports/*.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/state.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/budget.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/packet.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/review.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/workflow.py
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/review.schema.json
plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py
tests/fakes/{codex,claude,agy,orca,tmux}
tests/fixtures/*
tests/skill/scenarios/*
tests/test_*.py
pyproject.toml
README.md
LICENSE
NOTICE
~~~

---

### Task 1: Repository Foundation and Valid Plugin Manifests

**Files:**
- Create: pyproject.toml
- Create: tests/conftest.py
- Create: tests/test_manifests.py
- Create: .agents/plugins/marketplace.json
- Create: .claude-plugin/marketplace.json
- Create: plugins/multi-agent-orchestrator/.codex-plugin/plugin.json
- Create: plugins/multi-agent-orchestrator/.claude-plugin/plugin.json
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/__init__.py

**Interfaces:**
- Produces: PLUGIN_ROOT fixture as pathlib.Path.
- Produces: valid plugin name multi-agent-orchestrator and version 0.1.0.
- Consumes: no application interfaces.

- [ ] **Step 1: Initialize local Git repository without a commit**

Run:

~~~bash
cd /Users/chanhong/Desktop/hcg_coding/multi-agent-orchestrator
git init -b main
~~~

Expected: empty Git repository on main; no commit exists.

- [ ] **Step 2: Write failing manifest tests**

Create tests/conftest.py:

~~~python
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

def pytest_configure(config):
    config.addinivalue_line("markers", "integration: invokes installed external CLIs")
~~~

Create tests/test_manifests.py:

~~~python
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))

def test_codex_plugin_manifest_names_canonical_skill():
    data = load("plugins/multi-agent-orchestrator/.codex-plugin/plugin.json")
    assert data["name"] == "multi-agent-orchestrator"
    assert data["version"] == "0.1.0"
    assert data["skills"] == "./skills/"
    assert data["license"] == "MIT"

def test_marketplaces_point_to_nested_plugin():
    codex = load(".agents/plugins/marketplace.json")
    claude = load(".claude-plugin/marketplace.json")
    assert codex["plugins"][0]["source"]["path"] == "./plugins/multi-agent-orchestrator"
    assert claude["plugins"][0]["source"] == "./plugins/multi-agent-orchestrator"
~~~

- [ ] **Step 3: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_manifests.py -v
~~~

Expected: FAIL with FileNotFoundError for missing manifests.

- [ ] **Step 4: Create minimal manifests and package foundation**

Use exact Codex catalog policy:

~~~json
{
  "name": "multi-agent-orchestrator",
  "interface": {"displayName": "Multi-Agent Orchestrator"},
  "plugins": [{
    "name": "multi-agent-orchestrator",
    "source": {"source": "local", "path": "./plugins/multi-agent-orchestrator"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Coding"
  }]
}
~~~

Codex plugin manifest must include name, version, description, author.name, license, keywords, skills, and interface fields. Omit homepage and repository until real GitHub remote exists. Claude manifest contains name, version, description, author.name.

Create pyproject.toml:

~~~toml
[project]
name = "multi-agent-orchestrator"
version = "0.1.0"
requires-python = ">=3.10"

[project.optional-dependencies]
dev = ["pytest>=8,<9"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: invokes installed external CLIs"]
~~~

- [ ] **Step 5: Run manifest tests and validators**

Run:

~~~bash
python3 -m pytest tests/test_manifests.py -v
python3 /Users/chanhong/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/multi-agent-orchestrator
git diff --check
git status --short
~~~

Expected: tests PASS, validator PASS, no whitespace errors, only intended uncommitted files.

---

### Task 2: Project-Local Environment Configuration

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/config.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/errors.py
- Create: tests/test_config.py
- Create: tests/test_errors.py

**Interfaces:**
- Produces: Config dataclass.
- Produces: find_project_root(start: Path) -> Path.
- Produces: initialize_project(root: Path) -> tuple[Path, Path].
- Produces: load_config(root: Path, environ: Mapping[str, str]) -> Config.
- Produces: write_config(root: Path, values: Mapping[str, str]) -> Path.
- Produces: MaoError(code: str, message: str, details: dict).
- Produces: ERROR_CODES: frozenset[str].

- [ ] **Step 1: Write failing configuration tests**

~~~python
from pathlib import Path
from mao_core.config import find_project_root, initialize_project, load_config

def test_initialize_project_creates_local_env_and_idempotent_ignore(tmp_path):
    (tmp_path / ".git").mkdir()
    env_path, state_path = initialize_project(tmp_path)
    initialize_project(tmp_path)
    assert env_path == tmp_path / ".multi-agent-orchestrator/.env"
    assert state_path == tmp_path / ".multi-agent-orchestrator/state.json"
    assert (tmp_path / ".gitignore").read_text().count(
        "/.multi-agent-orchestrator/"
    ) == 1

def test_environment_overrides_file(tmp_path):
    (tmp_path / ".multi-agent-orchestrator").mkdir()
    (tmp_path / ".multi-agent-orchestrator/.env").write_text(
        "MAO_TRANSPORT=orca\nMAO_MAX_REVIEW_ROUNDS=2\n"
    )
    config = load_config(tmp_path, {"MAO_TRANSPORT": "direct"})
    assert config.transport == "direct"
    assert config.max_review_rounds == 2
~~~

Create tests/test_errors.py:

~~~python
from mao_core.errors import ERROR_CODES

def test_stable_error_codes_cover_public_contract():
    assert {
        "CLI_NOT_FOUND", "AUTH_REQUIRED", "AUTH_EXPIRED",
        "MODEL_LIST_UNSUPPORTED", "MODEL_UNAVAILABLE",
        "SESSION_START_FAILED", "QUOTA_EXHAUSTED", "NETWORK_ERROR",
        "TIMEOUT", "INVALID_RESULT", "CRITIC_MUTATED_WORKSPACE",
        "REVIEW_BUDGET_EXHAUSTED", "USAGE_UNAVAILABLE",
        "CONFIG_INVALID", "PACKET_PATH_INVALID",
        "STATE_TRANSITION_INVALID",
    } <= ERROR_CODES
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_config.py tests/test_errors.py -v
~~~

Expected: FAIL with ModuleNotFoundError for mao_core.config.

- [ ] **Step 3: Implement strict parser and initialization**

Implement:

~~~python
@dataclass(frozen=True)
class Config:
    primary_provider: str
    codex_model: str
    claude_model: str
    antigravity_model: str
    transport: str = "direct"
    transport_fallback: str = ""
    execution_profile: str = "yolo"
    max_review_rounds: int = 2
    max_calls_per_critic: int = 2
    max_total_critic_calls: int = 4
    max_transport_attempts: int = 2
    timeout_seconds: int = 300
~~~

Parser rules:

- Accept blank lines, comments, KEY=VALUE, single quotes, and double quotes.
- Reject export syntax, interpolation, duplicate known keys, malformed names, and embedded NUL.
- Preserve unknown keys and comments when wizard updates known keys.
- Validate provider in codex, claude, antigravity.
- Validate transport in direct, orca, tmux.
- Validate execution profile equals yolo for version 0.1.0.
- Reject limits above design maxima as CONFIG_INVALID.

initialize_project writes empty JSON object atomically to state.json and appends ignore rule while preserving existing line endings.

errors.py defines ERROR_CODES as immutable public contract. MaoError serializes through:

~~~python
def as_dict(self):
    return {
        "error": {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
    }
~~~

- [ ] **Step 4: Run focused and full tests**

Run:

~~~bash
python3 -m pytest tests/test_config.py tests/test_errors.py -v
python3 -m pytest -q
git diff --check
~~~

Expected: PASS.

---

### Task 3: Safe Process Envelope and Provider Base Contract

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/process.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/providers/__init__.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/providers/base.py
- Create: tests/fakes/echo-agent
- Create: tests/test_process.py
- Create: tests/test_provider_contract.py

**Interfaces:**
- Produces: ProcessResult(status, exit_code, stdout, stderr, duration_seconds, timed_out).
- Produces: run_process(args: Sequence[str], cwd: Path, timeout_seconds: int, stdin: str | None = None) -> ProcessResult.
- Produces: ProviderAdapter Protocol with detect, check_auth, list_models, validate_model, invoke, parse_result, measure_usage.
- Consumes: MaoError.

- [ ] **Step 1: Write failing process tests**

~~~python
from pathlib import Path
from mao_core.process import run_process

def test_process_uses_argument_array_without_shell(tmp_path):
    result = run_process(
        [str(Path("tests/fakes/echo-agent").resolve()), "a;touch", "never"],
        tmp_path,
        2,
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["a;touch", "never"]
    assert not (tmp_path / "touch").exists()

def test_timeout_returns_typed_result(tmp_path):
    result = run_process(
        ["python3", "-c", "import time; time.sleep(3)"],
        tmp_path,
        1,
    )
    assert result.status == "timeout"
    assert result.timed_out is True
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_process.py tests/test_provider_contract.py -v
~~~

Expected: FAIL because process and provider modules do not exist.

- [ ] **Step 3: Implement subprocess runner and protocol**

run_process must call subprocess.Popen with shell=False, text=True, start_new_session=True, and bounded communicate timeout. On timeout terminate process group, wait five seconds, then kill. Redact credential-like strings before persistence, while raw stdout remains in memory only until provider parser finishes.

Provider types:

~~~python
@dataclass(frozen=True)
class ModelCandidate:
    requested: str
    vendor: str
    source: str
    exhaustive: bool

@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    requested: str
    resolved: str
    vendor: str
    verified: bool

class ProviderAdapter(Protocol):
    name: str
    def detect(self) -> dict: ...
    def check_auth(self) -> dict: ...
    def list_models(self) -> list[ModelCandidate]: ...
    def validate_model(self, model: str, cwd: Path) -> ModelIdentity: ...
    def invoke(self, model: str, packet: Path, schema: Path, cwd: Path) -> ProcessResult: ...
    def parse_result(self, result: ProcessResult) -> dict: ...
    def measure_usage(self, parsed: dict) -> dict: ...
~~~

- [ ] **Step 4: Verify GREEN**

Run:

~~~bash
python3 -m pytest tests/test_process.py tests/test_provider_contract.py -v
python3 -m pytest -q
~~~

Expected: PASS.

---

### Task 4: Codex, Claude, and Antigravity Provider Adapters

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/providers/codex.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/providers/claude.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/providers/antigravity.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/models.py
- Create: tests/fakes/codex
- Create: tests/fakes/claude
- Create: tests/fakes/agy
- Create: tests/fixtures/codex-models-cache.json
- Create: tests/fixtures/claude-probe.json
- Create: tests/fixtures/agy-models.txt
- Create: tests/helpers.py
- Modify: tests/conftest.py
- Create: tests/test_providers.py
- Create: tests/test_model_discovery.py

**Interfaces:**
- Produces: CodexAdapter, ClaudeAdapter, AntigravityAdapter.
- Produces: discover_all(adapters: Sequence[ProviderAdapter]) -> dict[str, list[ModelCandidate]].
- Produces: classify_vendor(provider: str, resolved_model: str) -> str.
- Consumes: run_process, ModelCandidate, ModelIdentity, ProcessResult.

- [ ] **Step 1: Write failing command and discovery tests**

Create tests/helpers.py with FakePaths dataclass. Its create(tmp_path, monkeypatch)
method writes three executable Python fakes, sets MAO_FAKE_LOG, and exposes:

~~~python
@dataclass
class FakePaths:
    codex: str
    claude: str
    agy: str
    log: Path

    def last_args(self, executable_name: str) -> list[str]:
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if row["executable"] == executable_name][-1]["args"]
~~~

Each fake appends executable basename and sys.argv[1:] to MAO_FAKE_LOG. agy
models prints the two-line fixture, Claude probe prints claude-probe.json, and
all review calls print review-valid.json. Add fake_paths, packet, and schema
fixtures to tests/conftest.py; packet contains a minimal review prompt and
schema contains an object schema.

~~~python
def test_codex_review_command_contains_exact_yolo_flag(fake_paths, packet, schema):
    adapter = CodexAdapter(executable=fake_paths.codex)
    adapter.invoke("gpt-test", packet, schema, packet.parent)
    args = fake_paths.last_args("codex")
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--model" in args and "gpt-test" in args

def test_claude_candidate_list_is_marked_non_exhaustive(fake_paths):
    adapter = ClaudeAdapter(executable=fake_paths.claude)
    candidates = adapter.list_models()
    assert candidates
    assert all(candidate.exhaustive is False for candidate in candidates)

def test_agy_models_are_machine_discovered(fake_paths):
    adapter = AntigravityAdapter(executable=fake_paths.agy)
    candidates = adapter.list_models()
    assert [candidate.requested for candidate in candidates] == [
        "gemini-3.1-pro-high", "claude-sonnet-4-6"
    ]
    assert all(candidate.source == "agy models" for candidate in candidates)
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_providers.py tests/test_model_discovery.py -v
~~~

Expected: FAIL because concrete adapters do not exist.

- [ ] **Step 3: Implement exact command builders**

Codex invocation argument array:

~~~python
[
    executable, "exec",
    "--model", model,
    "--dangerously-bypass-approvals-and-sandbox",
    "--json",
    "--skip-git-repo-check",
    "--output-schema", str(schema),
    packet.read_text(encoding="utf-8"),
]
~~~

Claude invocation argument array:

~~~python
[
    executable, "-p", packet.read_text(encoding="utf-8"),
    "--model", model,
    "--output-format", "json",
    "--json-schema", schema.read_text(encoding="utf-8"),
    "--dangerously-skip-permissions",
]
~~~

Antigravity invocation argument array:

~~~python
[
    executable,
    "--print", packet.read_text(encoding="utf-8"),
    "--model", model,
    "--output-format", "json",
    "--json-schema", str(schema),
    "--dangerously-skip-permissions",
]
~~~

Before locking these arrays, run installed CLI help and adjust only flags proven by that version. Tests remain authoritative for chosen version contract.

- [ ] **Step 4: Implement honest model discovery and probes**

Codex reads provider-owned models_cache.json when present and marks cache source plus fetch timestamp. Claude obtains aliases from installed help and configured MAO_CLAUDE_CANDIDATES, marks list non-exhaustive, then resolves selected candidate through JSON probe modelUsage keys. Antigravity parses agy models output and marks it exhaustive for that command response.

validate_model runs in tempfile.TemporaryDirectory and asks for exact text OK with no project path. If response names a different model, persist both names. Unknown or unauthorized model raises MODEL_UNAVAILABLE.

classify_vendor rules:

~~~python
def classify_vendor(provider, resolved_model):
    value = resolved_model.lower()
    if value.startswith(("gpt-", "o1", "o3", "o4", "codex-")):
        return "openai"
    if "claude" in value:
        return "anthropic"
    if "gemini" in value:
        return "google"
    return provider
~~~

- [ ] **Step 5: Verify adapters**

Run:

~~~bash
python3 -m pytest tests/test_providers.py tests/test_model_discovery.py -v
python3 -m pytest -q
git diff --check
~~~

Expected: PASS and exact yolo flags visible in fake CLI argument logs.

---

### Task 5: Direct, Orca, and tmux Transport Adapters

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/transports/__init__.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/transports/base.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/transports/direct.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/transports/orca.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/transports/tmux.py
- Create: tests/fakes/orca
- Create: tests/fakes/tmux
- Create: tests/test_transports.py

**Interfaces:**
- Produces: TransportAdapter Protocol invoke(request: InvocationRequest) -> ProcessResult.
- Produces: InvocationRequest(provider, model, packet, schema, cwd, timeout_seconds, run_id).
- Produces: DirectTransport, OrcaTransport, TmuxTransport.
- Consumes: ProviderAdapter and run_process.

- [ ] **Step 1: Write failing transport contract tests**

Define InvocationRequest exactly:

~~~python
@dataclass(frozen=True)
class InvocationRequest:
    provider: str
    model: str
    packet: Path
    schema: Path
    cwd: Path
    timeout_seconds: int
    run_id: str
~~~

In tests/test_transports.py define request_for(tmp_path) to create packet.md
and schema.json, then return InvocationRequest with model test-model, timeout
2, and run id run-1. Define FakeProvider with invocations counter and invoke
returning ProcessResult(status="ok", exit_code=0, stdout="{}", stderr="",
duration_seconds=0, timed_out=False). Define provider and request pytest
fixtures from these helpers. Define FakeOrcaHarness around tests/fakes/orca;
the fake appends normalized command names to a JSON-lines log and returns
fixed run_id, task_id, dispatch_id, settled state, review output, and release
result for each lifecycle command.

~~~python
def test_direct_calls_provider_once(provider, request):
    result = DirectTransport().invoke(provider, request)
    assert result.status == "ok"
    assert provider.invocations == 1

def test_orca_uses_run_task_worker_lifecycle(fake_orca, request):
    OrcaTransport(fake_orca.path).invoke(fake_orca.provider, request)
    assert fake_orca.commands == [
        "status", "orchestration run-create", "orchestration task-create",
        "orchestration worker-start", "orchestration worker-show",
        "orchestration worker-read", "orchestration worker-release"
    ]

def test_orca_antigravity_adopts_yolo_terminal(fake_orca, request):
    request = replace(request, provider="antigravity")
    OrcaTransport(fake_orca.path).invoke(fake_orca.agy_provider, request)
    create_args = fake_orca.args_for("terminal create")
    assert "agy" in create_args[-1]
    assert "--dangerously-skip-permissions" in create_args[-1]
    start_args = fake_orca.args_for("orchestration worker-start")
    assert "--terminal" in start_args

def test_tmux_missing_returns_cli_not_found(tmp_path):
    with pytest.raises(MaoError) as error:
        TmuxTransport("/missing/tmux").invoke(FakeProvider(), request_for(tmp_path))
    assert error.value.code == "CLI_NOT_FOUND"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_transports.py -v
~~~

Expected: FAIL because transport modules do not exist.

- [ ] **Step 3: Implement direct transport**

DirectTransport delegates exactly once to provider.invoke. It creates no shell, tmux, Orca run, or fallback.

- [ ] **Step 4: Implement Orca supervised worker lifecycle**

Sequence:

1. orca status --json.
2. orca orchestration run-create --objective <review objective> --json.
3. orca orchestration task-create --run <run_id> --task-title <critic name> --spec <packet path and JSON-only contract> --json.
4. For Codex or Claude, call orchestration worker-start with --agent, --model, --worktree current, and --timeout-ms.
5. For Antigravity, create one exact Orca terminal running agy with selected model and --dangerously-skip-permissions, then call orchestration worker-start with --terminal <handle> and --worktree current so task is dispatched into adopted terminal.
6. Poll worker-show --dispatch <dispatch_id> --json until settled or timeout.
7. worker-read --dispatch <dispatch_id> --source auto --limit 5000 --json.
8. worker-release --dispatch <dispatch_id> --json in finally block.

Orca status failure raises SESSION_START_FAILED. Agent-wait observation is
treated as failure because runtime is unattended. Orca-managed Codex and Claude
launches must pass setup probe proving no permission prompt before configuration
can select Orca transport. If installed Orca version cannot prove bypass launch,
configuration rejects Orca for that provider and explains exact reason.
Antigravity terminal command is built from allowlisted arguments with shlex.join
because orca terminal create accepts a command string; no user-provided shell
fragment is accepted.

- [ ] **Step 5: Implement tmux one-shot lifecycle**

Write provider command to packet-local launcher file with mode 0700, using shlex.join only after executable allowlist and argument validation. Start:

~~~text
tmux new-session -d -s mao-<run>-<critic> <launcher-path>
~~~

Launcher writes stdout, stderr, and exit code to packet-local files. Poll has-session at bounded intervals, capture files after exit, and kill only exact generated session name during cleanup. Reject any pre-existing session with same name.

- [ ] **Step 6: Verify transports**

Run:

~~~bash
python3 -m pytest tests/test_transports.py -v
python3 -m pytest -q
~~~

Expected: PASS. Real tmux test remains skipped when tmux is absent.

---

### Task 6: Persistent Run State and Hard Budget Guard

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/state.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/budget.py
- Create: tests/test_state.py
- Create: tests/test_budget.py

**Interfaces:**
- Produces: RunState and CriticState dataclasses.
- Produces: StateStore.create, load, save_atomic, resume.
- Produces: BudgetGuard.reserve_call(critic: str, round_number: int) -> None.
- Produces: BudgetGuard.can_start_round(round_number: int) -> bool.
- Consumes: Config limits and MaoError.

- [ ] **Step 1: Write failing state and budget tests**

~~~python
def test_fifth_total_call_is_rejected():
    guard = BudgetGuard(max_rounds=2, max_per_critic=2, max_total=4)
    for critic in ["claude", "agy", "claude", "agy"]:
        guard.reserve_call(critic, 1 if guard.total < 2 else 2)
    with pytest.raises(MaoError) as error:
        guard.reserve_call("claude", 2)
    assert error.value.code == "REVIEW_BUDGET_EXHAUSTED"

def test_resume_does_not_repeat_completed_critic(tmp_path):
    store = StateStore(tmp_path)
    state = store.create("run-1", request_digest="abc")
    state.critics["claude"].rounds["1"] = "completed"
    store.save_atomic(state)
    resumed = store.resume("run-1", request_digest="abc")
    assert resumed.critics["claude"].rounds["1"] == "completed"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_state.py tests/test_budget.py -v
~~~

Expected: FAIL because state and budget modules do not exist.

- [ ] **Step 3: Implement atomic state and counters**

RunState fields:

~~~python
@dataclass
class CriticState:
    provider: str
    model_requested: str
    model_resolved: str
    calls: int
    rounds: dict[str, str]

@dataclass
class RunState:
    run_id: str
    request_digest: str
    packet_digest: str
    phase: str
    round_number: int
    total_calls: int
    critics: dict[str, CriticState]
    status: str
~~~

save_atomic writes JSON to sibling temporary file, fsyncs, then os.replace. reserve_call increments persistent counter before launching process so crash cannot create an uncounted paid call. A failed launch after process creation remains consumed. A missing executable discovered before process creation does not consume model-call budget.

- [ ] **Step 4: Verify state and budget**

Run:

~~~bash
python3 -m pytest tests/test_state.py tests/test_budget.py -v
python3 -m pytest -q
~~~

Expected: PASS.

---

### Task 7: Isolated Review Packet Builder

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/packet.py
- Create: tests/fixtures/sample-diff.patch
- Create: tests/test_packet.py

**Interfaces:**
- Produces: PacketRequest(user_request, instructions, diff, changed_files, related_files, tests, visual_artifacts).
- Produces: build_packet(request: PacketRequest, destination: Path) -> PacketResult.
- Produces: PacketResult(prompt_path, digest, copied_artifacts).
- Consumes: project-relative paths only.

- [ ] **Step 1: Write failing packet tests**

Define production types:

~~~python
@dataclass(frozen=True)
class PacketRequest:
    project: Path
    user_request: str
    acceptance_conditions: tuple[str, ...]
    instructions: tuple[Path, ...]
    diff: str
    changed_files: tuple[str, ...]
    related_files: tuple[str, ...]
    test_outputs: tuple[str, ...]
    visual_artifacts: tuple[str, ...]
    previous_decisions: tuple[dict, ...] = ()

@dataclass(frozen=True)
class PacketResult:
    prompt_path: Path
    digest: str
    copied_artifacts: tuple[Path, ...]
~~~

tests/test_packet.py defines packet_request(project=tmp_path, changed_files=(),
related_files=()) returning PacketRequest with fixed request, empty optional
tuples, and diff from tests/fixtures/sample-diff.patch.

~~~python
def test_packet_contains_required_context_and_excludes_secret(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "changed.py").write_text("VALUE = 2\n")
    (project / ".env").write_text("TOKEN=secret\n")
    request = packet_request(
        project=project,
        changed_files=["changed.py"],
        related_files=[".env"],
    )
    result = build_packet(request, tmp_path / "packet")
    prompt = result.prompt_path.read_text()
    assert "VALUE = 2" in prompt
    assert "TOKEN=secret" not in prompt
    assert ".env: SECRET_PATH_EXCLUDED" in prompt

def test_packet_rejects_path_escape(tmp_path):
    with pytest.raises(MaoError) as error:
        build_packet(packet_request(related_files=["../outside"]), tmp_path / "packet")
    assert error.value.code == "PACKET_PATH_INVALID"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_packet.py -v
~~~

Expected: FAIL because packet builder does not exist.

- [ ] **Step 3: Implement deterministic packet format**

Packet prompt section order:

1. Role and JSON-only output contract.
2. Original user request.
3. Acceptance conditions.
4. Applicable repository instructions.
5. Diff.
6. Changed files.
7. Related files.
8. Test commands and outputs.
9. Visual artifact index.
10. Previous decisions for round two.

Exclude basename and path patterns: .env, .env.*, *.pem, *.key, credentials*, secrets*, .git/**, node_modules/**, bin/**, obj/**, dist/**, build/**. Symlinks resolving outside project root are rejected. Packet copies visual artifacts into isolated destination and records SHA-256.

- [ ] **Step 4: Verify packet behavior**

Run:

~~~bash
python3 -m pytest tests/test_packet.py -v
python3 -m pytest -q
~~~

Expected: PASS.

---

### Task 8: Structured Review Parsing, Deduplication, and Parallel Dispatch

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/review.schema.json
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/review.py
- Create: tests/fixtures/review-valid.json
- Create: tests/fixtures/review-invalid.json
- Create: tests/test_review.py
- Create: tests/test_dispatch.py

**Interfaces:**
- Produces: Finding, ReviewResult, Decision dataclasses.
- Produces: parse_review(value: object) -> ReviewResult.
- Produces: finding_fingerprint(finding: Finding) -> str.
- Produces: capture_workspace_fingerprint(project: Path) -> str.
- Produces: dispatch_parallel(jobs: Sequence[CriticJob], guard: BudgetGuard, project: Path) -> list[CriticOutcome].
- Consumes: provider and transport adapters, StateStore, BudgetGuard.

- [ ] **Step 1: Write failing schema and dispatch tests**

Define production types:

~~~python
@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    file: str
    line: int | None
    evidence: str
    reason: str
    suggested_fix: str
    confidence: float
    needs_context: tuple[str, ...]

@dataclass(frozen=True)
class ReviewResult:
    summary: str
    findings: tuple[Finding, ...]
    usage: dict
    review_complete: bool

@dataclass(frozen=True)
class Decision:
    fingerprint: str
    verdict: str
    rationale: str

@dataclass(frozen=True)
class CriticJob:
    critic: str
    round_number: int
    provider: ProviderAdapter
    transport: TransportAdapter
    request: InvocationRequest

@dataclass(frozen=True)
class CriticOutcome:
    critic: str
    call_number: int
    status: str
    review: ReviewResult | None
    error: dict | None
~~~

tests/test_review.py defines finding(file, line, evidence) with severity major,
category correctness, fixed reason/fix/confidence, and empty needs_context.
tests/test_dispatch.py defines FakeTransport returning review-valid.json and
creates fake_jobs as two CriticJob instances for claude and agy. Its
mutating_job fixture uses FakeTransport that writes mutation.txt under supplied
project root before returning otherwise valid review.

~~~python
def test_invalid_review_missing_evidence_is_rejected():
    value = {
        "summary": "x",
        "findings": [{
            "severity": "major", "category": "correctness",
            "file": "a.py", "line": 2,
            "reason": "wrong", "suggested_fix": "fix",
            "confidence": 0.9, "needs_context": []
        }],
        "usage": {}, "review_complete": True
    }
    with pytest.raises(MaoError) as error:
        parse_review(value)
    assert error.value.code == "INVALID_RESULT"

def test_parallel_dispatch_reserves_before_starting(fake_jobs, tmp_path):
    outcomes = dispatch_parallel(fake_jobs, BudgetGuard(2, 2, 4), tmp_path)
    assert {item.critic for item in outcomes} == {"claude", "agy"}
    assert all(item.call_number == 1 for item in outcomes)

def test_equivalent_findings_share_fingerprint():
    left = finding(file="src/a.py", line=10, evidence="null dereference")
    right = finding(file="./src/a.py", line=11, evidence="Null dereference")
    assert finding_fingerprint(left) == finding_fingerprint(right)

def test_project_mutation_rejects_critic_result(mutating_job, tmp_path):
    outcomes = dispatch_parallel(
        [mutating_job], BudgetGuard(2, 2, 4), tmp_path
    )
    assert outcomes[0].error["code"] == "CRITIC_MUTATED_WORKSPACE"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_review.py tests/test_dispatch.py -v
~~~

Expected: FAIL because review module and schema do not exist.

- [ ] **Step 3: Implement strict result validation**

Allowed severity values are critical, major, minor, note. Allowed category
values are correctness, requirements, regression, security, maintainability,
visual, accessibility, and other. line is positive integer or null. confidence
is number from 0 through 1. needs_context is list of safe project-relative
paths or concise questions. Unknown top-level and finding fields are rejected
in version 0.1.0.

Fingerprint normalizes path, groups line into five-line region, lowercases category/evidence, collapses whitespace, and hashes normalized tuple. Deduplication preserves all provider attributions and highest severity.

- [ ] **Step 4: Implement bounded parallel dispatch**

Use concurrent.futures.ThreadPoolExecutor with maximum two workers. Reserve and
persist budget before submitting each launched model process. Retry and explicit
fallback create another reservation and never exceed configured per-critic,
transport-attempt, or total limits. Each job writes provider envelope, stdout,
stderr, parsed review, and usage to its round directory. Exceptions become typed
CriticOutcome and do not cancel valid sibling result.

capture_workspace_fingerprint records git status --porcelain=v1 -z, diff hashes,
and hashes of untracked files selected into review scope. Capture before dispatch
and after all critics settle. A changed fingerprint marks responsible concurrent
critic outcomes CRITIC_MUTATED_WORKSPACE and rejects their review. For non-Git
projects, hash selected authoritative files before and after. This is detection,
not an OS sandbox.

- [ ] **Step 5: Verify review engine**

Run:

~~~bash
python3 -m pytest tests/test_review.py tests/test_dispatch.py -v
python3 -m pytest -q
~~~

Expected: PASS.

---

### Task 9: Workflow State Machine and CLI Commands

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/workflow.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_core/setup.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/mao_cli.py
- Create: tests/test_workflow.py
- Create: tests/test_cli.py

**Interfaces:**
- Produces: Workflow.create_run, mark_implemented, record_local_verification, record_round_one, record_decisions, mark_patched, prepare_round_two, record_round_two, finalize, resume.
- Produces: FinalResult(status: str, findings: tuple[Finding, ...], review_gaps: tuple[str, ...]).
- Produces: select_reviewers(primary: ModelIdentity, available: Sequence[ModelIdentity]) -> list[ModelIdentity].
- Produces: CLI subcommands configure, prepare, review, decide, status, resume, finalize.
- Consumes: Config, provider registry, transport registry, packet builder, dispatch, StateStore.

- [ ] **Step 1: Write failing state-machine tests**

tests/test_workflow.py defines clean_review as ReviewResult with no findings,
empty usage, and review_complete true. Define reviewed_run_with_accepted_finding
by creating run, moving through round one, and recording one accepted Decision
for provider claude. Define run_with_failed_reviews by creating run, moving
through local verification, and recording typed failed CriticOutcome values for
the supplied provider names. workflow fixture uses temporary StateStore, fake
provider registry, direct fake transport, and Config limits 2/2/4.
Define identity(provider, model, vendor) in same test file to return
ModelIdentity(provider=provider, requested=model, resolved=model,
vendor=vendor, verified=True).

~~~python
def test_round_two_skipped_when_no_output_changed(workflow):
    run = workflow.create_run("request")
    workflow.mark_implemented(run)
    workflow.record_local_verification(run, passed=True, output="PASS")
    workflow.record_round_one(run, reviews=[clean_review()])
    workflow.record_decisions(run, decisions=[])
    assert workflow.prepare_round_two(run, output_changed=False) is None
    assert run.phase == "FINAL_DECISION"

def test_round_two_only_selects_affected_reviewer(workflow):
    run = reviewed_run_with_accepted_finding(workflow, provider="claude")
    jobs = workflow.prepare_round_two(run, output_changed=True)
    assert [job.critic for job in jobs] == ["claude"]

def test_two_failed_critics_yield_review_gap(workflow):
    run = run_with_failed_reviews(workflow, ["claude", "agy"])
    result = workflow.finalize(run)
    assert result.status == "COMPLETED_WITH_REVIEW_GAP"

def test_reviewer_selection_uses_actual_vendor():
    primary = identity("agy", "claude-opus-4-6-thinking", "anthropic")
    available = [
        identity("claude", "claude-fable-5-1", "anthropic"),
        identity("codex", "gpt-5.6-sol", "openai"),
        identity("agy", "gemini-3.1-pro-high", "google"),
    ]
    selected = select_reviewers(primary, available)
    assert [item.vendor for item in selected] == ["openai", "google"]
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_workflow.py tests/test_cli.py -v
~~~

Expected: FAIL because workflow and CLI do not exist.

- [ ] **Step 3: Implement phases and transition table**

Allowed transitions:

~~~python
TRANSITIONS = {
    "IMPLEMENTED": {"LOCAL_VERIFIED"},
    "LOCAL_VERIFIED": {"REVIEW_ROUND_1"},
    "REVIEW_ROUND_1": {"TRIAGED"},
    "TRIAGED": {"PATCHED", "FINAL_DECISION"},
    "PATCHED": {"LOCAL_REVERIFIED"},
    "LOCAL_REVERIFIED": {"REVIEW_ROUND_2", "FINAL_DECISION"},
    "REVIEW_ROUND_2": {"FINAL_DECISION"},
    "FINAL_DECISION": {"DONE"},
}
~~~

Every transition persists atomically. Illegal transition raises STATE_TRANSITION_INVALID. Round two gate requires output_changed, supplied needs_context, or new review surface from local verification.

select_reviewers removes primary vendor, groups remaining verified identities
by vendor, then selects at most one identity per vendor in configured provider
priority. Fewer than two distinct external vendors is recorded as review gap;
same-vendor substitution is forbidden.

- [ ] **Step 4: Implement setup conversation payload**

configure command emits machine-readable setup questions and accepts repeated --set KEY=VALUE for host skill to relay user choices. It reports detected CLIs, auth state, candidate models, discovery source, exhaustive flag, model probes, transport status, and exact relaunch command when primary differs.

The CLI must never claim it can identify current host permission mode. It prints configured launcher command for guaranteed yolo primary:

~~~text
codex --model <model> --dangerously-bypass-approvals-and-sandbox
claude --model <model> --dangerously-skip-permissions
agy --model <model> --dangerously-skip-permissions
~~~

- [ ] **Step 5: Implement CLI with JSON output**

mao_cli.py uses argparse, returns JSON on stdout, diagnostics on stderr, and stable exit codes. Work request itself remains in primary session; prepare command records request and emits packet requirements. review dispatches critics only after primary marks local verification passed.

- [ ] **Step 6: Verify workflow and CLI**

Run:

~~~bash
python3 -m pytest tests/test_workflow.py tests/test_cli.py -v
python3 -m pytest -q
~~~

Expected: PASS.

---

### Task 10: Project-Local Installer and Host Registration

**Files:**
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/scripts/install.py
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/agents/openai.yaml
- Create: tests/test_installer.py
- Create: tests/test_openai_yaml.py

**Interfaces:**
- Produces: install_project(project: Path, hosts: Sequence[str]) -> InstallReport.
- Produces: uninstall_project(project: Path, hosts: Sequence[str]) -> InstallReport.
- Produces: InstallReport(installed_hosts: tuple[str, ...], antigravity_registration_required: bool, antigravity_command: list[str], managed_files: tuple[str, ...]).
- Consumes: canonical skill root and initialize_project.

- [ ] **Step 1: Write failing installer tests**

tests/test_openai_yaml.py defines OPENAI_YAML as repository-root-relative path
to skills/multi-agent-orchestrator/agents/openai.yaml. tests/test_installer.py
imports install_project and uses canonical test skill fixture containing
SKILL.md and scripts/sentinel.py.

~~~python
def test_codex_and_claude_local_install_copy_canonical_skill(tmp_path):
    report = install_project(tmp_path, ["codex", "claude"])
    assert (tmp_path / ".agents/skills/multi-agent-orchestrator/SKILL.md").exists()
    assert (tmp_path / ".claude/skills/multi-agent-orchestrator/SKILL.md").exists()
    assert report.antigravity_registration_required is False

def test_antigravity_reports_local_plugin_registration(tmp_path):
    report = install_project(tmp_path, ["antigravity"])
    assert report.antigravity_registration_required is True
    assert report.antigravity_command[0:3] == ["agy", "plugin", "install"]

def test_openai_policy_is_explicit_only():
    text = OPENAI_YAML.read_text(encoding="utf-8")
    assert "policy:\n  allow_implicit_invocation: false\n" in text
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_installer.py tests/test_openai_yaml.py -v
~~~

Expected: FAIL because installer and metadata do not exist.

- [ ] **Step 3: Implement idempotent installer**

Installer resolves canonical source from its own file location, copies skill tree into selected host-local directories using temporary destination plus atomic rename, and writes installation manifest with source digest. Existing unrelated files are preserved. Updating a managed installation replaces only files listed in prior installation manifest. Uninstall removes only managed files and empty managed directories.

Antigravity report emits:

~~~text
agy plugin install <absolute-local-plugin-path>
~~~

Do not execute Antigravity registration during project-local copy unless caller passes --register-antigravity. This is host registry mutation distinct from project files.

- [ ] **Step 4: Create explicit-only Codex metadata**

~~~yaml
interface:
  display_name: "Multi-Agent Orchestrator"
  short_description: "Cross-vendor implementation and bounded review"
  default_prompt: "Use $multi-agent-orchestrator to implement this request and run bounded cross-vendor review."
policy:
  allow_implicit_invocation: false
~~~

- [ ] **Step 5: Verify installer**

Run:

~~~bash
python3 -m pytest tests/test_installer.py tests/test_openai_yaml.py -v
python3 -m pytest -q
~~~

Expected: PASS.

---

### Task 11: Skill Pressure Tests and Minimal Skill Instructions

**Files:**
- Create: tests/skill/scenarios/implicit-invocation.md
- Create: tests/skill/scenarios/unbounded-review.md
- Create: tests/skill/scenarios/critic-edits.md
- Create: tests/skill/scenarios/false-model-list.md
- Create: tests/skill/run_pressure.py
- Create: tests/skill/baseline-results.json
- Create: tests/skill/with-skill-results.json
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/SKILL.md
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/workflow.md
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/provider-contracts.md
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/review-schema.md
- Create: plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator/references/installation.md
- Create: tests/test_skill_static.py

**Interfaces:**
- Produces: host instructions that call mao_cli.py rather than reimplementing deterministic rules.
- Produces: pressure-test evidence for explicit invocation, bounded calls, primary-only writes, and honest model discovery.
- Consumes: completed controller CLI and approved design.

- [ ] **Step 1: Write pressure scenarios before SKILL.md**

Each scenario contains system context, user prompt, forbidden outcomes, and required observable outcomes. Combined pressures:

- User asks ordinary code change but never names skill; agent must not start critic loop.
- User demands repeated review until perfect; agent must stop after hard cap.
- Critic suggests editing project directly to save time; primary must reject critic write path.
- Claude CLI cannot enumerate all models; agent must report non-exhaustive candidates rather than claim full list.

- [ ] **Step 2: Run baseline without skill and record RED evidence**

Run each scenario in fresh isolated context with no new skill loaded:

~~~bash
python3 tests/skill/run_pressure.py --phase baseline --repetitions 5
~~~

Expected: at least one forbidden behavior appears in control runs. Store exact outputs and scoring in baseline-results.json. If a scenario never fails, remove that guidance requirement instead of inventing rules for it.

- [ ] **Step 3: Write static tests for skill discovery and routing**

~~~python
def test_skill_description_is_trigger_only():
    frontmatter = load_frontmatter(SKILL)
    assert frontmatter["name"] == "multi-agent-orchestrator"
    assert frontmatter["description"].startswith("Use when")
    assert "round" not in frontmatter["description"].lower()

def test_skill_routes_mechanics_to_controller():
    body = SKILL.read_text()
    assert "mao_cli.py" in body
    assert "Do not invoke implicitly" in body
    assert len(body.split()) < 500
~~~

- [ ] **Step 4: Run static test and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_skill_static.py -v
~~~

Expected: FAIL because SKILL.md does not exist.

- [ ] **Step 5: Write minimal SKILL.md and focused references**

Frontmatter:

~~~yaml
---
name: multi-agent-orchestrator
description: Use when the user explicitly requests bounded cross-vendor implementation review or explicitly invokes multi-agent-orchestrator for a coding or visual change.
---
~~~

SKILL.md must:

- State explicit invocation requirement.
- Detect project configuration and route configure when missing.
- Keep current session as primary.
- Require primary implementation and local verification before review.
- Invoke controller for packet, review, status, resume, and finalization.
- Require primary to record accepted/rejected/needs-proof decisions.
- Stop at controller budget boundary.
- Read only relevant reference for current operation.

workflow.md owns state machine and primary decision responsibilities. provider-contracts.md owns CLI and transport behavior. review-schema.md owns packet/result contract. installation.md owns marketplace, project-local, and Antigravity registration paths.

- [ ] **Step 6: Run pressure tests with skill**

Run:

~~~bash
python3 tests/skill/run_pressure.py --phase with-skill --repetitions 5
python3 -m pytest tests/test_skill_static.py -v
~~~

Expected: all required behaviors pass, forbidden outcomes absent, and outputs converge. Record with-skill-results.json. If new rationalization appears, make smallest instruction change and rerun both affected scenario and no-guidance control.

- [ ] **Step 7: Validate skill**

Run:

~~~bash
python3 /Users/chanhong/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator
python3 -m pytest -q
~~~

Expected: validator PASS and test suite PASS.

---

### Task 12: Documentation, Licensing, and Release Acceptance

**Files:**
- Create: README.md
- Create: LICENSE
- Create: NOTICE
- Create: docs/ACCEPTANCE.md
- Create: tests/test_docs.py
- Create: tests/integration/test_real_cli_smoke.py

**Interfaces:**
- Produces: complete installation, configuration, invocation, risk, troubleshooting, and uninstall documentation.
- Produces: acceptance matrix recording tested host, CLI version, model, transport, and result.
- Consumes: all implemented commands and manifests.

- [ ] **Step 1: Write failing documentation tests**

~~~python
def test_readme_discloses_dangerous_defaults():
    text = README.read_text()
    for flag in [
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
    ]:
        assert flag in text

def test_readme_documents_local_runtime_path_and_ignore_rule():
    text = README.read_text()
    assert ".multi-agent-orchestrator/.env" in text
    assert "/.multi-agent-orchestrator/" in text

def test_notice_attributes_reference_project():
    assert "multi-agent-starter" in NOTICE.read_text()
    assert "MIT" in NOTICE.read_text()
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
python3 -m pytest tests/test_docs.py -v
~~~

Expected: FAIL because documentation files do not exist.

- [ ] **Step 3: Write README and acceptance guide**

README section order:

1. Purpose and explicit-only behavior.
2. Security warning for yolo defaults.
3. Marketplace installation.
4. GitHub clone and project-local installation from current checkout.
5. First-run model and transport wizard.
6. Invocation examples.
7. Project-local .env reference.
8. Loop and usage limits.
9. Orca and tmux optional behavior.
10. Antigravity local registration limitation.
11. Error code troubleshooting.
12. Uninstall.
13. Development tests.
14. License and attribution.

Do not include a fake GitHub URL. Add remote-specific clone URL only after repository remote exists.

docs/ACCEPTANCE.md matrix columns:

~~~text
Host | CLI version | Primary model | Critic models | Transport | Install | Configure | Round 1 | Round 2 gate | Resume | Result
~~~

- [ ] **Step 4: Add MIT license and attribution**

LICENSE contains current year and project owner name chanhong. NOTICE states multi-agent-starter by netwaif was used as architectural reference under MIT and includes upstream repository name without claiming copied code. If implementation copies upstream code, add original copyright notice verbatim before release.

- [ ] **Step 5: Add opt-in real CLI smoke tests**

Integration test skips unless MAO_RUN_REAL_CLI_TESTS=1. It uses tempfile project, tiny prompt, configured selected models, direct transport, and one critic call per provider. It asserts resolved model, JSON validity, exact dangerous flag path, and no authoritative project mutation. Never run this test as part of default pytest.

- [ ] **Step 6: Run full offline release gate**

Run:

~~~bash
python3 -m pytest -m "not integration" -q
python3 /Users/chanhong/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/multi-agent-orchestrator/skills/multi-agent-orchestrator
python3 /Users/chanhong/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/multi-agent-orchestrator
agy plugin validate plugins/multi-agent-orchestrator
git diff --check
git status --short
~~~

Expected: all tests and available validators PASS. agy validation failure must be resolved or documented as exact host limitation; it cannot be hidden.

- [ ] **Step 7: Run billable integration only when explicitly selected**

Run:

~~~bash
MAO_RUN_REAL_CLI_TESTS=1 python3 -m pytest -m integration tests/integration/test_real_cli_smoke.py -v
~~~

Expected: selected local accounts return valid tiny responses, exact requested/resolved models are recorded, and no project mutation occurs.

- [ ] **Step 8: Final completion verification**

Run:

~~~bash
rg -n "TBD|TODO|FIXME|PLACEHOLDER" . -g '!docs/superpowers/plans/**'
python3 -m pytest -m "not integration" -q
git diff --check
git status --short
~~~

Expected: no unfinished placeholders, offline suite PASS, no whitespace errors, no unexpected files.

Do not commit or push. Report files, test evidence, real-integration status, known limitations, and exact next command. If user separately requests commit, create a feature branch before committing and never push directly to master.
