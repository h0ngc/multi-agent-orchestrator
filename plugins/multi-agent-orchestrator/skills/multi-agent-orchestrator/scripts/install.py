#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Sequence

from mao_core.config import initialize_installation
from mao_core.errors import MaoError


SKILL_NAME = "multi-agent-orchestrator"
MANIFEST_NAME = ".mao-install-manifest.json"
HOST_DESTINATIONS = {
    "codex": Path(".agents/skills") / SKILL_NAME,
    "claude": Path(".claude/skills") / SKILL_NAME,
}
SUPPORTED_HOSTS = frozenset({*HOST_DESTINATIONS, "antigravity"})


@dataclass(frozen=True)
class InstallReport:
    installed_hosts: tuple[str, ...]
    antigravity_registration_required: bool
    antigravity_command: list[str]
    managed_files: tuple[str, ...]


def _install_error(message: str, **details: object) -> MaoError:
    return MaoError("INSTALL_INVALID", message, details)


def _canonical_skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _canonical_plugin_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _normalized_hosts(hosts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(hosts, (str, bytes)):
        raise _install_error("Hosts must be a sequence of host names")
    values: list[str] = []
    for host in hosts:
        if not isinstance(host, str) or host not in SUPPORTED_HOSTS:
            raise _install_error("Host is unsupported", host=host)
        if host not in values:
            values.append(host)
    if not values:
        raise _install_error("At least one host is required")
    return tuple(values)


def _safe_relative(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _install_error("Managed file path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise _install_error("Managed file path is invalid")
    if pure.as_posix() == MANIFEST_NAME:
        raise _install_error("Manifest cannot manage itself")
    return Path(*pure.parts)


def _source_files(root: Path) -> dict[Path, bytes]:
    try:
        source = root.expanduser().resolve(strict=True)
    except OSError:
        raise _install_error("Canonical skill source is unavailable") from None
    if not source.is_dir():
        raise _install_error("Canonical skill source is not a directory")
    files: dict[Path, bytes] = {}
    try:
        entries = sorted(source.rglob("*"), key=lambda item: item.as_posix())
        for entry in entries:
            relative = entry.relative_to(source)
            if entry.is_symlink():
                raise _install_error("Canonical skill source contains a symlink")
            if "__pycache__" in relative.parts or entry.suffix in {".pyc", ".pyo"}:
                continue
            if entry.is_file():
                safe = _safe_relative(relative.as_posix())
                files[safe] = entry.read_bytes()
            elif not entry.is_dir():
                raise _install_error("Canonical skill source contains unsupported entry")
    except MaoError:
        raise
    except (OSError, UnicodeError):
        raise _install_error("Canonical skill source could not be read") from None
    if Path("SKILL.md") not in files:
        raise _install_error("Canonical skill source is missing SKILL.md")
    return files


def _source_digest(files: dict[Path, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, payload in sorted(files.items(), key=lambda item: item[0].as_posix()):
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _manifest_payload(files: dict[Path, bytes]) -> dict:
    return {
        "format_version": 1,
        "source_digest": _source_digest(files),
        "managed_files": sorted(path.as_posix() for path in files),
    }


def _read_manifest(destination: Path) -> dict | None:
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    if manifest_path.is_symlink():
        raise _install_error("Installation manifest cannot be a symlink")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _install_error("Installation manifest is invalid") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"format_version", "source_digest", "managed_files"}
        or value["format_version"] != 1
        or not isinstance(value["source_digest"], str)
        or len(value["source_digest"]) != 64
        or not isinstance(value["managed_files"], list)
    ):
        raise _install_error("Installation manifest is invalid")
    managed = [_safe_relative(item) for item in value["managed_files"]]
    if len(managed) != len(set(managed)):
        raise _install_error("Installation manifest has duplicate paths")
    value["managed_files"] = managed
    return value


def _reject_destination_symlinks(project: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(project)
    except ValueError:
        raise _install_error("Installation destination escapes project") from None
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise _install_error("Installation destination contains a symlink")
    try:
        if not destination.resolve(strict=False).is_relative_to(project):
            raise _install_error("Installation destination escapes project")
    except (OSError, RuntimeError):
        raise _install_error("Installation destination could not be resolved") from None
    if destination.is_symlink():
        raise _install_error("Installation destination cannot be a symlink")
    if not destination.exists():
        return
    try:
        for entry in destination.rglob("*"):
            if entry.is_symlink():
                raise _install_error("Installation destination contains a symlink")
    except OSError:
        raise _install_error("Installation destination could not be inspected") from None


def _validate_destination(
    project: Path,
    destination: Path,
    manifest: dict | None,
    incoming: dict[Path, bytes] | None,
) -> None:
    _reject_destination_symlinks(project, destination)
    if incoming is None or not destination.exists():
        return
    managed = set(manifest["managed_files"]) if manifest is not None else set()
    for relative in incoming:
        target = destination / relative
        if target.exists() and relative not in managed:
            raise _install_error(
                "Installation would overwrite an unmanaged file",
                path=relative.as_posix(),
            )


def _validate_runtime_targets(project: Path) -> None:
    runtime = project / ".multi-agent-orchestrator"
    ignore = project / ".gitignore"
    if ignore.is_symlink():
        raise _install_error("Project gitignore cannot be a symlink")
    if ignore.exists() and not ignore.is_file():
        raise _install_error("Project gitignore must be a regular file")
    if runtime.is_symlink():
        raise _install_error("Project runtime cannot be a symlink")
    try:
        if not runtime.resolve(strict=False).is_relative_to(project):
            raise _install_error("Project runtime escapes project")
        if runtime.exists():
            if not runtime.is_dir():
                raise _install_error("Project runtime must be a directory")
            for entry in runtime.rglob("*"):
                if entry.is_symlink():
                    raise _install_error("Project runtime contains a symlink")
    except MaoError:
        raise
    except (OSError, RuntimeError):
        raise _install_error("Project runtime could not be inspected") from None


def _remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _replace_tree(
    destination: Path,
    manifest: dict | None,
    incoming: dict[Path, bytes] | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}.install-", dir=destination.parent)
    )
    staged = temporary_root / destination.name
    backup = destination.parent / f".{destination.name}.backup-{os.getpid()}-{id(staged)}"
    moved_old = False
    committed = False
    try:
        if destination.exists():
            shutil.copytree(destination, staged)
        else:
            staged.mkdir()
        if manifest is not None:
            for relative in manifest["managed_files"]:
                target = staged / relative
                if target.exists():
                    if not target.is_file():
                        raise _install_error("Managed path is not a regular file")
                    target.unlink()
                    _remove_empty_parents(target, staged)
        old_manifest = staged / MANIFEST_NAME
        if old_manifest.exists():
            old_manifest.unlink()
        if incoming is not None:
            for relative, payload in incoming.items():
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            (staged / MANIFEST_NAME).write_text(
                json.dumps(_manifest_payload(incoming), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        if incoming is None and not any(staged.iterdir()):
            staged.rmdir()
            committed = True
        else:
            os.replace(staged, destination)
            committed = True
        if moved_old and committed:
            shutil.rmtree(backup, ignore_errors=True)
            moved_old = False
    except Exception:
        if moved_old and not destination.exists() and backup.exists():
            try:
                os.replace(backup, destination)
                moved_old = False
            except OSError:
                raise _install_error(
                    "Installation failed and original backup could not be restored",
                    backup=str(backup),
                ) from None
        raise
    finally:
        if committed and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(temporary_root, ignore_errors=True)


def install_project(project: Path, hosts: Sequence[str]) -> InstallReport:
    selected = _normalized_hosts(hosts)
    project_root = Path(project).expanduser().resolve()
    files = _source_files(_canonical_skill_root())
    prepared: dict[str, tuple[Path, dict | None]] = {}
    for host in selected:
        if host not in HOST_DESTINATIONS:
            continue
        destination = project_root / HOST_DESTINATIONS[host]
        manifest = _read_manifest(destination)
        _validate_destination(project_root, destination, manifest, files)
        prepared[host] = (destination, manifest)

    _validate_runtime_targets(project_root)
    initialize_installation(project_root)
    installed: list[str] = []
    managed: list[str] = []
    for host in selected:
        if host not in prepared:
            continue
        destination, manifest = prepared[host]
        _reject_destination_symlinks(project_root, destination)
        _replace_tree(destination, manifest, files)
        installed.append(host)
        managed.extend(
            (HOST_DESTINATIONS[host] / relative).as_posix() for relative in files
        )

    antigravity = "antigravity" in selected
    command = (
        ["agy", "plugin", "install", str(_canonical_plugin_root().resolve())]
        if antigravity
        else []
    )
    return InstallReport(tuple(installed), antigravity, command, tuple(sorted(managed)))


def uninstall_project(project: Path, hosts: Sequence[str]) -> InstallReport:
    selected = _normalized_hosts(hosts)
    project_root = Path(project).expanduser().resolve()
    prepared: dict[str, tuple[Path, dict]] = {}
    for host in selected:
        if host not in HOST_DESTINATIONS:
            continue
        destination = project_root / HOST_DESTINATIONS[host]
        manifest = _read_manifest(destination)
        if manifest is None:
            continue
        _validate_destination(project_root, destination, manifest, None)
        prepared[host] = (destination, manifest)

    removed_hosts: list[str] = []
    managed: list[str] = []
    for host in selected:
        if host not in prepared:
            continue
        destination, manifest = prepared[host]
        _reject_destination_symlinks(project_root, destination)
        _replace_tree(destination, manifest, None)
        removed_hosts.append(host)
        managed.extend(
            (HOST_DESTINATIONS[host] / relative).as_posix()
            for relative in manifest["managed_files"]
        )

    antigravity = "antigravity" in selected
    command = ["agy", "plugin", "uninstall", SKILL_NAME] if antigravity else []
    return InstallReport(
        tuple(removed_hosts), antigravity, command, tuple(sorted(managed))
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="install.py")
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument(
        "--host",
        action="append",
        dest="hosts",
        choices=sorted(SUPPORTED_HOSTS),
        required=True,
    )
    parser.add_argument("--register-antigravity", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = (
            install_project(args.project, args.hosts)
            if args.action == "install"
            else uninstall_project(args.project, args.hosts)
        )
        if args.register_antigravity:
            if not report.antigravity_registration_required:
                raise _install_error("Antigravity host was not selected")
            completed = subprocess.run(report.antigravity_command, shell=False, check=False)
            if completed.returncode != 0:
                raise _install_error(
                    "Antigravity registration command failed",
                    returncode=completed.returncode,
                )
        print(json.dumps(asdict(report), sort_keys=True))
        return 0
    except MaoError as error:
        print(json.dumps(error.as_dict(), sort_keys=True), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
