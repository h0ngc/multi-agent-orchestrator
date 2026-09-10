from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path

import pytest

import install as installer
from install import InstallReport, install_project, uninstall_project
from mao_core.errors import MaoError


@pytest.fixture
def canonical_skill(tmp_path, monkeypatch):
    source = tmp_path / "canonical/multi-agent-orchestrator"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("canonical\n", encoding="utf-8")
    (source / "scripts/sentinel.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(installer, "_canonical_skill_root", lambda: source)
    return source


def test_install_report_has_exact_frozen_fields():
    assert [item.name for item in fields(InstallReport)] == [
        "installed_hosts",
        "antigravity_registration_required",
        "antigravity_command",
        "managed_files",
    ]
    assert InstallReport.__dataclass_params__.frozen


def test_actual_canonical_skill_installs_and_uninstalls_in_temp_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    installed = install_project(project, ["codex", "claude"])

    assert installed.installed_hosts == ("codex", "claude")
    assert (project / ".agents/skills/multi-agent-orchestrator/SKILL.md").exists()
    assert (project / ".claude/skills/multi-agent-orchestrator/SKILL.md").exists()

    removed = uninstall_project(project, ["codex", "claude"])

    assert removed.installed_hosts == ("codex", "claude")
    assert not (project / ".agents/skills/multi-agent-orchestrator").exists()
    assert not (project / ".claude/skills/multi-agent-orchestrator").exists()


def test_codex_and_claude_local_install_copy_canonical_skill(
    tmp_path, canonical_skill
):
    project = tmp_path / "project"
    project.mkdir()

    report = install_project(project, ["codex", "claude"])

    assert (project / ".agents/skills/multi-agent-orchestrator/SKILL.md").exists()
    assert (project / ".claude/skills/multi-agent-orchestrator/SKILL.md").exists()
    assert report.installed_hosts == ("codex", "claude")
    assert report.antigravity_registration_required is False
    assert report.antigravity_command == []
    assert not (project / ".multi-agent-orchestrator").exists()
    assert ".multi-agent-orchestrator/" in (project / ".gitignore").read_text()


def test_update_removes_only_prior_managed_files_and_preserves_unrelated(
    tmp_path, canonical_skill
):
    project = tmp_path / "project"
    project.mkdir()
    install_project(project, ["codex"])
    destination = project / ".agents/skills/multi-agent-orchestrator"
    (destination / "notes.txt").write_text("mine\n", encoding="utf-8")
    (canonical_skill / "scripts/sentinel.py").unlink()
    (canonical_skill / "scripts/new.py").write_text("VALUE = 2\n", encoding="utf-8")

    install_project(project, ["codex"])

    assert (destination / "notes.txt").read_text() == "mine\n"
    assert not (destination / "scripts/sentinel.py").exists()
    assert (destination / "scripts/new.py").exists()
    manifest = json.loads(
        (destination / ".mao-install-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["source_digest"]
    assert "scripts/new.py" in manifest["managed_files"]


def test_uninstall_removes_only_managed_files(tmp_path, canonical_skill):
    project = tmp_path / "project"
    project.mkdir()
    install_project(project, ["claude"])
    destination = project / ".claude/skills/multi-agent-orchestrator"
    (destination / "notes.txt").write_text("mine\n", encoding="utf-8")

    report = uninstall_project(project, ["claude"])

    assert (destination / "notes.txt").read_text() == "mine\n"
    assert not (destination / "SKILL.md").exists()
    assert not (destination / ".mao-install-manifest.json").exists()
    assert report.installed_hosts == ("claude",)


def test_antigravity_reports_local_plugin_registration_without_running_it(
    tmp_path, canonical_skill
):
    project = tmp_path / "project"
    project.mkdir()

    report = install_project(project, ["antigravity"])

    assert report.installed_hosts == ()
    assert report.antigravity_registration_required is True
    assert report.antigravity_command[:3] == ["agy", "plugin", "install"]
    assert Path(report.antigravity_command[3]).is_absolute()


def test_unknown_host_is_rejected_before_project_mutation(tmp_path, canonical_skill):
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(MaoError):
        install_project(project, ["unknown"])

    assert list(project.iterdir()) == []


def test_invalid_manifest_traversal_is_rejected_without_touching_user_file(
    tmp_path, canonical_skill
):
    project = tmp_path / "project"
    destination = project / ".agents/skills/multi-agent-orchestrator"
    destination.mkdir(parents=True)
    outside = project / "outside.txt"
    outside.write_text("safe\n", encoding="utf-8")
    (destination / ".mao-install-manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "source_digest": "x" * 64,
                "managed_files": ["../../../outside.txt"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MaoError):
        install_project(project, ["codex"])

    assert outside.read_text() == "safe\n"


def test_source_symlink_is_rejected(tmp_path, canonical_skill):
    target = canonical_skill / "real.txt"
    target.write_text("x", encoding="utf-8")
    (canonical_skill / "link.txt").symlink_to(target)
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(MaoError):
        install_project(project, ["codex"])

    assert not (project / ".agents").exists()


def test_destination_ancestor_symlink_cannot_escape_project(
    tmp_path, canonical_skill
):
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / ".agents").symlink_to(external, target_is_directory=True)

    with pytest.raises(MaoError, match="symlink"):
        install_project(project, ["codex"])

    assert list(external.iterdir()) == []
    assert not (project / ".multi-agent-orchestrator").exists()


def test_failed_commit_and_failed_restore_preserve_original_backup(
    tmp_path, canonical_skill, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    install_project(project, ["codex"])
    destination = project / ".agents/skills/multi-agent-orchestrator"
    (destination / "notes.txt").write_text("mine\n", encoding="utf-8")
    real_replace = installer.os.replace
    calls = 0

    def fail_commit_and_restore(source, target):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("simulated rename failure")
        return real_replace(source, target)

    monkeypatch.setattr(installer.os, "replace", fail_commit_and_restore)
    with pytest.raises(MaoError, match="backup"):
        install_project(project, ["codex"])

    backups = list(destination.parent.glob(".multi-agent-orchestrator.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "notes.txt").read_text() == "mine\n"
    assert (backups[0] / "SKILL.md").read_text() == "canonical\n"


def test_runtime_symlink_cannot_escape_project_or_partially_install(
    tmp_path, canonical_skill
):
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / ".multi-agent-orchestrator").symlink_to(
        external, target_is_directory=True
    )

    with pytest.raises(MaoError, match="runtime"):
        install_project(project, ["codex"])

    assert list(external.iterdir()) == []
    assert not (project / ".agents").exists()
    assert not (project / ".gitignore").exists()


def test_gitignore_symlink_is_rejected_before_install(tmp_path, canonical_skill):
    project = tmp_path / "project"
    external = tmp_path / "external.gitignore"
    project.mkdir()
    external.write_text("keep\n", encoding="utf-8")
    (project / ".gitignore").symlink_to(external)

    with pytest.raises(MaoError, match="gitignore"):
        install_project(project, ["claude"])

    assert external.read_text() == "keep\n"
    assert not (project / ".claude").exists()
