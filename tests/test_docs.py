from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
LICENSE = ROOT / "LICENSE"
NOTICE = ROOT / "NOTICE"
ACCEPTANCE = ROOT / "docs/ACCEPTANCE.md"
REAL_SMOKE = ROOT / "tests/integration/test_real_cli_smoke.py"


def test_readme_discloses_dangerous_defaults():
    text = README.read_text(encoding="utf-8")
    for flag in [
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
    ]:
        assert flag in text
    assert "not an OS security boundary" in text


def test_readme_documents_local_runtime_path_and_ignore_rule():
    text = README.read_text(encoding="utf-8")
    assert ".multi-agent-orchestrator/.env" in text
    assert "/.multi-agent-orchestrator/" in text
    assert "MAO_MAX_REVIEW_ROUNDS=2" in text
    assert "MAO_MAX_CALLS_PER_CRITIC=2" in text
    assert "MAO_MAX_TOTAL_CRITIC_CALLS=4" in text


def test_readme_documents_local_effort_discovery_and_settings():
    text = README.read_text(encoding="utf-8")
    assert "MAO_CODEX_EFFORT=" in text
    assert "MAO_CLAUDE_EFFORT=" in text
    assert "MAO_ANTIGRAVITY_EFFORT=" in text
    assert "No browser or web search is used" in text


def test_readme_has_required_sections_in_order_and_no_fake_remote():
    text = README.read_text(encoding="utf-8")
    headings = [
        "## Purpose and explicit-only behavior",
        "## Security warning",
        "## Marketplace installation",
        "## Project-local installation",
        "## First-run setup",
        "## Invocation",
        "## Project-local environment",
        "## Loop and usage limits",
        "## Optional Orca and tmux transports",
        "## Antigravity registration limitation",
        "## Troubleshooting",
        "## Uninstall",
        "## Development and tests",
        "## License and attribution",
    ]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "github.com/example" not in text.lower()
    assert "github.com/your" not in text.lower()


def test_notice_and_license_are_release_ready():
    notice = NOTICE.read_text(encoding="utf-8")
    license_text = LICENSE.read_text(encoding="utf-8")
    assert "multi-agent-starter" in notice
    assert "netwaif" in notice
    assert "MIT" in notice
    assert "architectural reference" in notice
    assert "Copyright (c) 2026 chanhong" in license_text
    assert "MIT License" in license_text


def test_acceptance_matrix_has_exact_columns_and_honest_offline_status():
    text = ACCEPTANCE.read_text(encoding="utf-8")
    assert (
        "Host | CLI version | Primary model | Critic models | Transport | Install | "
        "Configure | Round 1 | Round 2 gate | Resume | Result"
    ) in text
    assert "Not run" in text
    assert "MAO_RUN_REAL_CLI_TESTS=1" in text


def test_real_smoke_is_explicitly_opt_in():
    text = REAL_SMOKE.read_text(encoding="utf-8")
    assert 'os.environ.get("MAO_RUN_REAL_CLI_TESTS") != "1"' in text
    assert "pytest.mark.integration" in text
