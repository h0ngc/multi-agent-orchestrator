from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile

from .errors import MaoError


_SECRET_BASENAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "credentials*",
    "secrets*",
)
_EXCLUDED_DIRECTORIES = frozenset(
    {".git", "node_modules", "bin", "obj", "dist", "build"}
)
_SAFE_ARTIFACT_CHARACTER = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_RUNTIME_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MANAGED_DESTINATION_ENTRIES = frozenset(
    {"prompt.md", "artifacts", "manifest.json"}
)
_MANIFEST_KEYS = frozenset(
    {"format_version", "packet_digest", "prompt_sha256", "artifacts"}
)
_ARTIFACT_MANIFEST_KEYS = frozenset({"path", "sha256"})
_FORMAT_VERSION = 1
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVIEW_PACKET_BYTES = 131_072


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
    round_number: int = 1


@dataclass(frozen=True)
class PacketResult:
    prompt_path: Path
    digest: str
    copied_artifacts: tuple[Path, ...]


@dataclass(frozen=True)
class _TextEntry:
    display_path: str
    content: str | None


@dataclass(frozen=True)
class _Artifact:
    display_path: str
    copied_name: str | None
    content: bytes | None
    sha256: str | None


def _packet_error(message: str, field: str) -> MaoError:
    return MaoError("PACKET_PATH_INVALID", message, {"field": field})


def _validate_review_scope_size(prompt: bytes) -> None:
    actual_bytes = len(prompt)
    if actual_bytes > _MAX_REVIEW_PACKET_BYTES:
        raise MaoError(
            "REVIEW_SCOPE_TOO_LARGE",
            "Review scope exceeds one complete packet; split the change without truncating it",
            {
                "actual_bytes": actual_bytes,
                "maximum_bytes": _MAX_REVIEW_PACKET_BYTES,
            },
        )


def _resolve_project(project: Path) -> Path:
    resolution_failed = False
    try:
        resolved = Path(project).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        resolution_failed = True
        resolved = Path()
    if resolution_failed:
        raise _packet_error("Project directory is unavailable", "project")
    if not resolved.is_dir():
        raise _packet_error("Project must be a directory", "project")
    return resolved


def _canonical_destination(destination: Path) -> Path:
    conversion_failed = False
    try:
        raw = Path(destination)
    except (TypeError, ValueError):
        conversion_failed = True
        raw = Path()
    if conversion_failed:
        raise _packet_error("Destination path is invalid", "destination")
    if raw.is_symlink():
        raise _packet_error("Destination symlinks are not allowed", "destination")
    resolution_failed = False
    try:
        resolved = raw.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolution_failed = True
        resolved = Path()
    if resolution_failed:
        raise _packet_error("Destination path is invalid", "destination")
    return resolved


def _validate_project_destination_separation(project: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(project)
    except ValueError:
        relative = None
    if relative is not None:
        parts = relative.parts
        if (
            len(parts) == 5
            and parts[0:2] == (".multi-agent-orchestrator", "runs")
            and parts[3] == "packet"
            and _SAFE_RUNTIME_COMPONENT.fullmatch(parts[2])
            and _SAFE_RUNTIME_COMPONENT.fullmatch(parts[4])
        ):
            return
        if (
            len(parts) == 6
            and parts[0:2] == (".multi-agent-orchestrator", "runs")
            and parts[3] == "packet-revisions"
            and _SAFE_RUNTIME_COMPONENT.fullmatch(parts[2])
            and _SAFE_RUNTIME_COMPONENT.fullmatch(parts[4])
            and _SAFE_RUNTIME_COMPONENT.fullmatch(parts[5])
        ):
            return
    if (
        destination == project
        or relative is not None
        or project.is_relative_to(destination)
    ):
        raise _packet_error(
            "Project and destination directories must be isolated",
            "destination",
        )


def _read_without_error(path: Path) -> tuple[bool, bytes]:
    try:
        return True, path.read_bytes()
    except OSError:
        return False, b""


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite number")


def _parse_manifest(data: bytes) -> dict[str, object] | None:
    decode_failed = False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
        text = ""
    if decode_failed:
        return None

    parse_failed = False
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        parse_failed = True
        parsed = None
    if parse_failed or type(parsed) is not dict:
        return None
    return parsed


def _manifest_inventory_is_valid(manifest: dict[str, object]) -> bool:
    if set(manifest) != _MANIFEST_KEYS:
        return False
    if type(manifest["format_version"]) is not int:
        return False
    if manifest["format_version"] != _FORMAT_VERSION:
        return False
    if not isinstance(manifest["packet_digest"], str) or not _HEX_DIGEST.fullmatch(
        manifest["packet_digest"]
    ):
        return False
    if not isinstance(manifest["prompt_sha256"], str) or not _HEX_DIGEST.fullmatch(
        manifest["prompt_sha256"]
    ):
        return False
    artifacts = manifest["artifacts"]
    if type(artifacts) is not list:
        return False
    seen: set[str] = set()
    for artifact in artifacts:
        if type(artifact) is not dict or set(artifact) != _ARTIFACT_MANIFEST_KEYS:
            return False
        path = artifact["path"]
        digest = artifact["sha256"]
        if not isinstance(path, str) or not isinstance(digest, str):
            return False
        path_parts = PurePosixPath(path).parts
        if (
            not _is_utf8_string(path)
            or len(path_parts) != 2
            or path_parts[0] != "artifacts"
            or path_parts[1] in {"", ".", ".."}
            or _SAFE_ARTIFACT_CHARACTER.search(path_parts[1]) is not None
            or path in seen
            or not _HEX_DIGEST.fullmatch(digest)
        ):
            return False
        seen.add(path)
    return True


def _inspect_existing_destination(destination: Path) -> None:
    if not destination.exists():
        return
    if destination.is_symlink() or not destination.is_dir():
        raise _packet_error("Destination must be a regular directory", "destination")

    inspection_failed = False
    try:
        entries = tuple(destination.iterdir())
    except OSError:
        inspection_failed = True
        entries = ()
    if inspection_failed:
        raise _packet_error("Destination cannot be inspected", "destination")
    if {entry.name for entry in entries} != _MANAGED_DESTINATION_ENTRIES:
        raise _packet_error(
            "Destination is not a valid managed packet",
            "destination",
        )

    prompt_path = destination / "prompt.md"
    artifacts_path = destination / "artifacts"
    manifest_path = destination / "manifest.json"
    if (
        prompt_path.is_symlink()
        or artifacts_path.is_symlink()
        or manifest_path.is_symlink()
        or not prompt_path.is_file()
        or not artifacts_path.is_dir()
        or not manifest_path.is_file()
    ):
        raise _packet_error("Destination contains unsafe aliases", "destination")
    artifact_inspection_failed = False
    try:
        artifact_entries = tuple(artifacts_path.iterdir())
    except OSError:
        artifact_inspection_failed = True
        artifact_entries = ()
    if artifact_inspection_failed:
        raise _packet_error("Destination cannot be inspected", "destination")
    if any(entry.is_symlink() or not entry.is_file() for entry in artifact_entries):
        raise _packet_error("Destination contains unsafe aliases", "destination")

    manifest_read, manifest_bytes = _read_without_error(manifest_path)
    prompt_read, prompt_bytes = _read_without_error(prompt_path)
    manifest = _parse_manifest(manifest_bytes) if manifest_read else None
    if not prompt_read or manifest is None or not _manifest_inventory_is_valid(manifest):
        raise _packet_error("Destination manifest is invalid", "destination")

    artifact_records = manifest["artifacts"]
    expected_names = {record["path"].split("/", 1)[1] for record in artifact_records}
    if {entry.name for entry in artifact_entries} != expected_names:
        raise _packet_error("Destination manifest inventory does not match", "destination")
    if hashlib.sha256(prompt_bytes).hexdigest() != manifest["prompt_sha256"]:
        raise _packet_error("Destination prompt hash does not match", "destination")

    inventory_artifacts: list[dict[str, str]] = []
    for record in artifact_records:
        artifact_path = destination / record["path"]
        artifact_read, artifact_bytes = _read_without_error(artifact_path)
        if not artifact_read or hashlib.sha256(artifact_bytes).hexdigest() != record["sha256"]:
            raise _packet_error("Destination artifact hash does not match", "destination")
        inventory_artifacts.append(
            {"path": record["path"], "sha256": record["sha256"]}
        )
    inventory = {
        "format_version": _FORMAT_VERSION,
        "prompt_sha256": manifest["prompt_sha256"],
        "artifacts": inventory_artifacts,
    }
    if _packet_digest_from_inventory(inventory) != manifest["packet_digest"]:
        raise _packet_error("Destination packet digest does not match", "destination")


def _relative_path(value: object, field: str) -> tuple[str, tuple[str, ...]]:
    conversion_failed = False
    try:
        raw = os.fspath(value)
    except TypeError:
        conversion_failed = True
        raw = ""
    if conversion_failed:
        raise _packet_error("Packet input path must be relative", field)
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _packet_error("Packet input path must be relative", field)
    encoding_failed = False
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        raise _packet_error("Packet input path must be UTF-8", field)

    windows_path = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    if (
        posix_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or ".." in posix_path.parts
    ):
        raise _packet_error("Packet input path must be project-relative", field)

    parts = tuple(part for part in posix_path.parts if part != ".")
    if not parts:
        raise _packet_error("Packet input path must name a file", field)
    return posix_path.as_posix(), parts


def _is_excluded(parts: tuple[str, ...]) -> bool:
    return any(
        part in _EXCLUDED_DIRECTORIES
        or any(fnmatchcase(part, pattern) for pattern in _SECRET_BASENAME_PATTERNS)
        for part in parts
    )


_GIT_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}
_MAX_HUNK_NUMBER = 2_147_483_647
_MAX_HUNK_NUMBER_TEXT = str(_MAX_HUNK_NUMBER)
_HUNK_HEADER = re.compile(
    r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?\Z"
)
_INDEX_METADATA = re.compile(
    r"index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?\Z"
)
_MODE_METADATA = re.compile(
    r"(old mode|new mode|new file mode|deleted file mode) ([0-7]{6})\Z"
)
_SIMILARITY_METADATA = re.compile(
    r"(similarity|dissimilarity) index (?:100|[0-9]{1,2})%\Z"
)
_NO_NEWLINE_MARKER = "\\ No newline at end of file"


def _diff_error() -> MaoError:
    return _packet_error("Diff must be an unambiguous unified Git diff", "diff")


def _decode_git_quoted_path(value: str) -> str:
    decoded = bytearray()
    index = 0
    valid = True
    while index < len(value) and valid:
        character = value[index]
        if character != "\\":
            try:
                decoded.extend(character.encode("utf-8"))
            except UnicodeEncodeError:
                valid = False
            index += 1
            continue
        index += 1
        if index >= len(value):
            valid = False
            break
        escaped = value[index]
        if escaped in _GIT_ESCAPES:
            decoded.append(_GIT_ESCAPES[escaped])
            index += 1
            continue
        if escaped in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            octal_value = int(value[index:end], 8)
            if octal_value > 255:
                valid = False
                break
            decoded.append(octal_value)
            index = end
            continue
        valid = False
    if not valid:
        raise _diff_error()
    decode_failed = False
    try:
        result = bytes(decoded).decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
        result = ""
    if decode_failed:
        raise _diff_error()
    return result


def _git_token(value: str, start: int) -> tuple[str, int]:
    if start >= len(value):
        raise _diff_error()
    if value[start] != '"':
        end = start
        while end < len(value) and not value[end].isspace():
            end += 1
        if end == start:
            raise _diff_error()
        return value[start:end], end

    index = start + 1
    escaped = False
    while index < len(value):
        character = value[index]
        if character == '"' and not escaped:
            return _decode_git_quoted_path(value[start + 1 : index]), index + 1
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
        index += 1
    raise _diff_error()


def _git_tokens(value: str, count: int) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    for token_index in range(count):
        if token_index:
            separator_start = index
            while index < len(value) and value[index].isspace():
                index += 1
            if separator_start == index:
                raise _diff_error()
        token, index = _git_token(value, index)
        tokens.append(token)
    if value[index:].strip():
        raise _diff_error()
    return tuple(tokens)


def _git_binary_tokens(value: str) -> tuple[str, str]:
    first, index = _git_token(value, 0)
    if not value.startswith(" and ", index):
        raise _diff_error()
    second_start = index + len(" and ")
    second, index = _git_token(value, second_start)
    if value[index:] != " differ":
        raise _diff_error()
    return first, second


def _git_path(token: str, prefix: str | None) -> tuple[str, tuple[str, ...]] | None:
    if token == "/dev/null":
        return None
    if prefix is not None:
        expected = f"{prefix}/"
        if not token.startswith(expected):
            raise _diff_error()
        token = token[len(expected) :]
    return _relative_path(token, "diff")


def _same_path(
    observed: tuple[str, tuple[str, ...]] | None,
    expected: tuple[str, tuple[str, ...]],
) -> bool:
    return observed is None or observed[0] == expected[0]


def _hunk_number(token: str | None, default: int | None = None) -> int:
    if token is None:
        if default is None:
            raise _diff_error()
        return default
    if len(token) > len(_MAX_HUNK_NUMBER_TEXT) or (
        len(token) == len(_MAX_HUNK_NUMBER_TEXT)
        and token > _MAX_HUNK_NUMBER_TEXT
    ):
        raise _diff_error()
    return int(token)


def _parse_hunk(lines: list[str], start: int) -> int:
    header = lines[start].rstrip("\r\n")
    match = _HUNK_HEADER.fullmatch(header)
    if match is None:
        raise _diff_error()
    _hunk_number(match.group(1))
    old_expected = _hunk_number(match.group(2), 1)
    _hunk_number(match.group(3))
    new_expected = _hunk_number(match.group(4), 1)
    if old_expected == 0 and new_expected == 0:
        raise _diff_error()
    old_seen = 0
    new_seen = 0
    index = start + 1
    previous_was_hunk_line = False

    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line == _NO_NEWLINE_MARKER:
            if not previous_was_hunk_line:
                raise _diff_error()
            previous_was_hunk_line = False
            index += 1
            continue
        if old_seen == old_expected and new_seen == new_expected:
            return index
        if not line or line[0] not in {" ", "+", "-"}:
            raise _diff_error()
        if line[0] == " ":
            old_seen += 1
            new_seen += 1
        elif line[0] == "-":
            old_seen += 1
        else:
            new_seen += 1
        if old_seen > old_expected or new_seen > new_expected:
            raise _diff_error()
        previous_was_hunk_line = True
        index += 1

    if old_seen != old_expected or new_seen != new_expected:
        raise _diff_error()
    return index


def _parse_diff_block(block: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    lines = block.splitlines(keepends=True)
    if len(lines) < 2:
        raise _diff_error()
    header = lines[0].rstrip("\r\n")
    if not header.startswith("diff --git "):
        raise _diff_error()
    old_token, new_token = _git_tokens(header[len("diff --git ") :], 2)
    old_path = _git_path(old_token, "a")
    new_path = _git_path(new_token, "b")
    if old_path is None or new_path is None:
        raise _diff_error()

    observed_paths: list[tuple[str, tuple[str, ...]]] = [old_path, new_path]
    rename_old: tuple[str, tuple[str, ...]] | None = None
    rename_new: tuple[str, tuple[str, ...]] | None = None
    copy_old: tuple[str, tuple[str, ...]] | None = None
    copy_new: tuple[str, tuple[str, ...]] | None = None
    rename_old_seen = False
    rename_new_seen = False
    copy_old_seen = False
    copy_new_seen = False
    metadata_changes: set[str] = set()
    seen_metadata: set[str] = set()
    index = 1

    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line.startswith("--- "):
            if index + 1 >= len(lines):
                raise _diff_error()
            next_line = lines[index + 1].rstrip("\r\n")
            if not next_line.startswith("+++ "):
                raise _diff_error()
            marker_old = _git_path(_git_tokens(line[4:], 1)[0], "a")
            marker_new = _git_path(_git_tokens(next_line[4:], 1)[0], "b")
            if marker_old is None and marker_new is None:
                raise _diff_error()
            if not _same_path(marker_old, old_path) or not _same_path(
                marker_new, new_path
            ):
                raise _diff_error()
            if marker_old is not None:
                observed_paths.append(marker_old)
            if marker_new is not None:
                observed_paths.append(marker_new)
            index += 2
            if index >= len(lines) or not lines[index].startswith("@@ "):
                raise _diff_error()
            while index < len(lines):
                if not lines[index].startswith("@@ "):
                    raise _diff_error()
                index = _parse_hunk(lines, index)
            break

        if line.startswith("@@ ") or line.startswith("+++ "):
            raise _diff_error()
        if line == "GIT binary patch":
            raise _diff_error()
        if line.startswith("Binary files "):
            binary_old_token, binary_new_token = _git_binary_tokens(
                line[len("Binary files ") :]
            )
            binary_old = _git_path(binary_old_token, "a")
            binary_new = _git_path(binary_new_token, "b")
            if binary_old is None and binary_new is None:
                raise _diff_error()
            if not _same_path(binary_old, old_path) or not _same_path(
                binary_new, new_path
            ):
                raise _diff_error()
            if index != len(lines) - 1:
                raise _diff_error()
            if binary_old is not None:
                observed_paths.append(binary_old)
            if binary_new is not None:
                observed_paths.append(binary_new)
            index += 1
            break

        if _INDEX_METADATA.fullmatch(line):
            if "index" in seen_metadata:
                raise _diff_error()
            seen_metadata.add("index")
            index += 1
            continue
        mode_match = _MODE_METADATA.fullmatch(line)
        if mode_match is not None:
            mode_kind = mode_match.group(1)
            if mode_kind in seen_metadata:
                raise _diff_error()
            seen_metadata.add(mode_kind)
            metadata_changes.add(mode_kind)
            index += 1
            continue
        if _SIMILARITY_METADATA.fullmatch(line):
            similarity_kind = line.split(" ", 1)[0]
            if similarity_kind in seen_metadata:
                raise _diff_error()
            seen_metadata.add(similarity_kind)
            index += 1
            continue
        if line.startswith("rename from "):
            if rename_old_seen or copy_old_seen or copy_new_seen:
                raise _diff_error()
            rename_old_seen = True
            rename_old = _git_path(
                _git_tokens(line[len("rename from ") :], 1)[0], None
            )
            if rename_old is None:
                raise _diff_error()
            index += 1
            continue
        if line.startswith("rename to "):
            if rename_new_seen or not rename_old_seen or copy_old_seen or copy_new_seen:
                raise _diff_error()
            rename_new_seen = True
            rename_new = _git_path(
                _git_tokens(line[len("rename to ") :], 1)[0], None
            )
            if rename_new is None:
                raise _diff_error()
            index += 1
            continue
        if line.startswith("copy from "):
            if copy_old_seen or rename_old_seen or rename_new_seen:
                raise _diff_error()
            copy_old_seen = True
            copy_old = _git_path(
                _git_tokens(line[len("copy from ") :], 1)[0], None
            )
            if copy_old is None:
                raise _diff_error()
            index += 1
            continue
        if line.startswith("copy to "):
            if copy_new_seen or not copy_old_seen or rename_old_seen or rename_new_seen:
                raise _diff_error()
            copy_new_seen = True
            copy_new = _git_path(
                _git_tokens(line[len("copy to ") :], 1)[0], None
            )
            if copy_new is None:
                raise _diff_error()
            index += 1
            continue
        raise _diff_error()

    for observed_old, observed_new, old_seen, new_seen in (
        (rename_old, rename_new, rename_old_seen, rename_new_seen),
        (copy_old, copy_new, copy_old_seen, copy_new_seen),
    ):
        if old_seen != new_seen:
            raise _diff_error()
        if observed_old is not None and observed_new is not None:
            if observed_old[0] != old_path[0] or observed_new[0] != new_path[0]:
                raise _diff_error()
            observed_paths.extend((observed_old, observed_new))

    has_mode_change = (
        {"old mode", "new mode"} <= metadata_changes
        or "new file mode" in metadata_changes
        or "deleted file mode" in metadata_changes
    )
    has_path_change = rename_old_seen or copy_old_seen
    has_text_or_binary = any(
        line.startswith("--- ") or line.startswith("Binary files ")
        for line in lines[1:]
    )
    if not has_text_or_binary and not has_mode_change and not has_path_change:
        raise _diff_error()
    if ("old mode" in metadata_changes) != ("new mode" in metadata_changes):
        raise _diff_error()
    if "new file mode" in metadata_changes and "deleted file mode" in metadata_changes:
        raise _diff_error()
    return tuple(observed_paths)


def _filter_diff(value: object) -> str:
    diff = _utf8_text(value, "diff")
    if not diff:
        return ""
    lines = diff.splitlines(keepends=True)
    starts = [
        index for index, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    if not starts or starts[0] != 0:
        raise _diff_error()
    starts.append(len(lines))

    rendered: list[str] = []
    for index in range(len(starts) - 1):
        block = "".join(lines[starts[index] : starts[index + 1]])
        paths = _parse_diff_block(block)
        excluded = [path for path in paths if _is_excluded(path[1])]
        if excluded:
            rendered.append(f"{excluded[-1][0]}: SECRET_PATH_EXCLUDED\n")
        else:
            rendered.append(block)
    return "".join(rendered)


def _resolve_requested_file(
    project: Path,
    parts: tuple[str, ...],
    field: str,
) -> tuple[Path, tuple[str, ...]]:
    resolution_failed = False
    try:
        resolved = project.joinpath(*parts).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        resolution_failed = True
        resolved = project
    if resolution_failed:
        raise _packet_error("Requested packet file is unavailable", field)
    if not resolved.is_relative_to(project):
        raise _packet_error("Requested packet file escapes project", field)
    if not resolved.is_file():
        raise _packet_error("Requested packet path must be a file", field)
    resolved_parts = tuple(resolved.relative_to(project).parts)
    return resolved, resolved_parts


def _read_bytes(path: Path, field: str) -> bytes:
    succeeded, content = _read_without_error(path)
    if not succeeded:
        raise _packet_error("Requested packet file cannot be read", field)
    return content


def _text_entries(
    project: Path,
    values: tuple[object, ...],
    field: str,
) -> tuple[_TextEntry, ...]:
    entries: list[_TextEntry] = []
    for value in values:
        display_path, parts = _relative_path(value, field)
        if _is_excluded(parts):
            entries.append(_TextEntry(display_path, None))
            continue

        resolved, resolved_parts = _resolve_requested_file(project, parts, field)
        if _is_excluded(resolved_parts):
            entries.append(_TextEntry(display_path, None))
            continue
        decode_failed = False
        try:
            content = _read_bytes(resolved, field).decode("utf-8")
        except UnicodeDecodeError:
            decode_failed = True
            content = ""
        if decode_failed:
            raise _packet_error("Requested packet file must be UTF-8", field)
        entries.append(_TextEntry(display_path, content))
    return tuple(entries)


def _artifact_name(index: int, display_path: str, basename: str) -> str:
    safe_basename = _SAFE_ARTIFACT_CHARACTER.sub("_", basename)[:100] or "artifact"
    path_hash = hashlib.sha256(display_path.encode("utf-8")).hexdigest()[:16]
    return f"{index:04d}-{path_hash}-{safe_basename}"


def _artifacts(project: Path, values: tuple[object, ...]) -> tuple[_Artifact, ...]:
    artifacts: list[_Artifact] = []
    copied_index = 0
    for value in values:
        display_path, parts = _relative_path(value, "visual_artifacts")
        if _is_excluded(parts):
            artifacts.append(_Artifact(display_path, None, None, None))
            continue

        resolved, resolved_parts = _resolve_requested_file(
            project, parts, "visual_artifacts"
        )
        if _is_excluded(resolved_parts):
            artifacts.append(_Artifact(display_path, None, None, None))
            continue
        content = _read_bytes(resolved, "visual_artifacts")
        copied_index += 1
        copied_name = _artifact_name(copied_index, display_path, parts[-1])
        artifacts.append(
            _Artifact(
                display_path,
                copied_name,
                content,
                hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(artifacts)


def _utf8_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _packet_error("Packet text must be a string", field)
    encoding_failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        raise _packet_error("Packet text must be UTF-8 encodable", field)
    return value


def _render_list(values: tuple[object, ...], field: str) -> str:
    if not values:
        return "(none)"
    return "\n".join(f"- {_utf8_text(value, field)}" for value in values)


def _render_text_entries(entries: tuple[_TextEntry, ...]) -> str:
    if not entries:
        return "(none)"
    rendered: list[str] = []
    for entry in entries:
        if entry.content is None:
            rendered.append(f"{entry.display_path}: SECRET_PATH_EXCLUDED")
            continue
        content = entry.content
        if content and not content.endswith(("\n", "\r")):
            content += "\n"
        rendered.append(
            f"## File: {entry.display_path}\n\n"
            f"--- BEGIN FILE CONTENT ---\n{content}--- END FILE CONTENT ---"
        )
    return "\n\n".join(rendered)


def _render_test_outputs(values: tuple[object, ...]) -> str:
    if not values:
        return "(none)"
    return "\n\n".join(
        f"## Test output {index}\n\n{_utf8_text(value, 'test_outputs')}"
        for index, value in enumerate(values, start=1)
    )


def _render_artifact_index(artifacts: tuple[_Artifact, ...]) -> str:
    if not artifacts:
        return "(none)"
    lines: list[str] = []
    for artifact in artifacts:
        if artifact.copied_name is None:
            lines.append(f"{artifact.display_path}: SECRET_PATH_EXCLUDED")
        else:
            lines.append(
                f"- {artifact.display_path} -> artifacts/{artifact.copied_name} "
                f"(sha256: {artifact.sha256})"
            )
    return "\n".join(lines)


_JSON_ONLY_CONTRACT = """You are an independent critic. Review only supplied packet content. Do not edit files. You may use read-only tools to inspect supplied files under ./artifacts, but never access paths outside this packet workspace or modify any file. Return exactly one JSON object and no prose or Markdown fences, matching this contract:
{
  "summary": "",
  "findings": [
    {
      "severity": "critical|major|minor|note",
      "category": "correctness|requirements|regression|security|maintainability|visual|accessibility|other",
      "file": "relative/path",
      "line": 1,
      "evidence": "",
      "reason": "",
      "suggested_fix": "",
      "confidence": 0.0,
      "needs_context": []
    }
  ],
  "usage": {},
  "review_complete": true
}"""


def _is_utf8_string(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _is_strict_json(value: object, active: set[int]) -> bool:
    if value is None or type(value) is bool or type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is str:
        return _is_utf8_string(value)
    if type(value) is list:
        identity = id(value)
        if identity in active:
            return False
        active.add(identity)
        valid = all(_is_strict_json(item, active) for item in value)
        active.remove(identity)
        return valid
    if type(value) is dict:
        identity = id(value)
        if identity in active:
            return False
        active.add(identity)
        valid = all(
            _is_utf8_string(key) and _is_strict_json(item, active)
            for key, item in value.items()
        )
        active.remove(identity)
        return valid
    return False


def _validate_previous_decisions(values: object) -> tuple[dict, ...]:
    valid = type(values) is tuple and all(type(item) is dict for item in values)
    if valid:
        recursion_failed = False
        try:
            valid = all(_is_strict_json(item, set()) for item in values)
        except RecursionError:
            recursion_failed = True
        valid = valid and not recursion_failed
    if not valid:
        raise _packet_error(
            "Previous decisions must contain strict JSON objects",
            "previous_decisions",
        )
    return values


def _render_previous_decisions(values: tuple[dict, ...]) -> str:
    values = _validate_previous_decisions(values)
    if not values:
        return "(none)"
    serialization_failed = False
    try:
        rendered = json.dumps(
            list(values),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        serialization_failed = True
        rendered = ""
    if serialization_failed:
        raise _packet_error(
            "Previous decisions must be UTF-8 JSON values",
            "previous_decisions",
        )
    return rendered


def _validate_round_number(value: object) -> int:
    if type(value) is not int or value not in {1, 2}:
        raise _packet_error("Round number must be 1 or 2", "round_number")
    return value


def _render_prompt(
    request: PacketRequest,
    filtered_diff: str,
    instructions: tuple[_TextEntry, ...],
    changed_files: tuple[_TextEntry, ...],
    related_files: tuple[_TextEntry, ...],
    artifacts: tuple[_Artifact, ...],
) -> bytes:
    sections = (
        (
            "# 1. Role and JSON-only output contract",
            f"{_JSON_ONLY_CONTRACT}\n\nReview round: {request.round_number}",
        ),
        (
            "# 2. Original user request",
            _utf8_text(request.user_request, "user_request"),
        ),
        (
            "# 3. Acceptance conditions",
            _render_list(request.acceptance_conditions, "acceptance_conditions"),
        ),
        (
            "# 4. Applicable repository instructions",
            _render_text_entries(instructions),
        ),
        ("# 5. Diff", filtered_diff),
        ("# 6. Changed files", _render_text_entries(changed_files)),
        ("# 7. Related files", _render_text_entries(related_files)),
        (
            "# 8. Test commands and outputs",
            _render_test_outputs(request.test_outputs),
        ),
        ("# 9. Visual artifact index", _render_artifact_index(artifacts)),
        (
            "# 10. Previous decisions for round two",
            _render_previous_decisions(request.previous_decisions),
        ),
    )
    prompt = "\n\n".join(f"{heading}\n\n{body}" for heading, body in sections)
    encoding_failed = False
    try:
        encoded = (prompt + "\n").encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
        encoded = b""
    if encoding_failed:
        raise _packet_error("Packet content must be UTF-8 encodable", "request")
    return encoded


def _packet_inventory(
    prompt: bytes,
    artifacts: tuple[_Artifact, ...],
) -> dict[str, object]:
    return {
        "format_version": _FORMAT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "artifacts": [
            {
                "path": f"artifacts/{artifact.copied_name}",
                "sha256": artifact.sha256,
            }
            for artifact in artifacts
            if artifact.copied_name is not None and artifact.sha256 is not None
        ],
    }


def _packet_digest_from_inventory(inventory: dict[str, object]) -> str:
    canonical = json.dumps(
        inventory,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"MAO_PACKET_V1\x00" + canonical).hexdigest()


def _manifest_bytes(inventory: dict[str, object], digest: str) -> bytes:
    manifest = dict(inventory)
    manifest["packet_digest"] = digest
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _try_make_parent(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _try_make_temporary(parent: Path, prefix: str) -> Path | None:
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    except OSError:
        return None


def _try_make_directory(path: Path) -> bool:
    try:
        path.mkdir()
        return True
    except OSError:
        return False


def _try_write(path: Path, content: bytes) -> bool:
    try:
        path.write_bytes(content)
        return True
    except OSError:
        return False


def _try_replace(source: Path, target: Path) -> bool:
    try:
        os.replace(source, target)
        return True
    except OSError:
        return False


def _try_remove_empty(path: Path) -> bool:
    try:
        path.rmdir()
        return True
    except OSError:
        return False


def _remove_tree_best_effort(path: Path | None) -> bool:
    if path is None or not path.exists():
        return True
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def _matches_staged_packet(
    destination: Path,
    prompt: bytes,
    manifest: bytes,
    artifacts: tuple[_Artifact, ...],
) -> bool:
    if not destination.is_dir() or destination.is_symlink():
        return False
    try:
        entries = tuple(destination.iterdir())
        artifact_entries = tuple((destination / "artifacts").iterdir())
    except OSError:
        return False
    if {entry.name for entry in entries} != _MANAGED_DESTINATION_ENTRIES:
        return False
    if any(entry.is_symlink() or not entry.is_file() for entry in artifact_entries):
        return False
    expected_artifacts = {
        artifact.copied_name: artifact.content
        for artifact in artifacts
        if artifact.copied_name is not None and artifact.content is not None
    }
    if {entry.name for entry in artifact_entries} != set(expected_artifacts):
        return False
    prompt_read, prompt_content = _read_without_error(destination / "prompt.md")
    manifest_read, manifest_content = _read_without_error(destination / "manifest.json")
    if not prompt_read or not manifest_read:
        return False
    if prompt_content != prompt or manifest_content != manifest:
        return False
    return all(
        _read_without_error(destination / "artifacts" / name) == (True, content)
        for name, content in expected_artifacts.items()
    )


def _write_staged_packet(
    destination: Path,
    prompt: bytes,
    manifest: bytes,
    artifacts: tuple[_Artifact, ...],
) -> None:
    if not _try_make_parent(destination.parent):
        raise _packet_error("Packet destination cannot be written", "destination")
    staging = _try_make_temporary(
        destination.parent,
        f".{destination.name}.staging-",
    )
    if staging is None:
        raise _packet_error("Packet destination cannot be written", "destination")

    artifact_directory = staging / "artifacts"
    writes_succeeded = _try_make_directory(artifact_directory)
    writes_succeeded = writes_succeeded and _try_write(staging / "prompt.md", prompt)
    writes_succeeded = writes_succeeded and _try_write(
        staging / "manifest.json", manifest
    )
    for artifact in artifacts:
        if artifact.copied_name is not None and artifact.content is not None:
            writes_succeeded = writes_succeeded and _try_write(
                artifact_directory / artifact.copied_name,
                artifact.content,
            )
    if not writes_succeeded:
        _remove_tree_best_effort(staging)
        raise _packet_error("Packet destination cannot be written", "destination")

    validation_error: MaoError | None = None
    try:
        _inspect_existing_destination(destination)
    except MaoError as error:
        validation_error = error
    if validation_error is not None:
        _remove_tree_best_effort(staging)
        raise validation_error

    backup: Path | None = None
    if destination.exists():
        backup = _try_make_temporary(
            destination.parent,
            f".{destination.name}.backup-",
        )
        if backup is None or not _try_remove_empty(backup):
            _remove_tree_best_effort(staging)
            _remove_tree_best_effort(backup)
            raise _packet_error("Packet destination cannot be written", "destination")
        if not _try_replace(destination, backup):
            _remove_tree_best_effort(staging)
            raise _packet_error("Packet destination cannot be written", "destination")

    if not _try_replace(staging, destination):
        if _matches_staged_packet(destination, prompt, manifest, artifacts):
            _remove_tree_best_effort(backup)
            return
        if backup is not None and _try_replace(backup, destination):
            backup = None
        _remove_tree_best_effort(staging)
        raise _packet_error("Packet destination cannot be written", "destination")

    _remove_tree_best_effort(backup)


def build_packet(request: PacketRequest, destination: Path) -> PacketResult:
    _validate_round_number(request.round_number)
    project = _resolve_project(request.project)
    destination = _canonical_destination(destination)
    _validate_project_destination_separation(project, destination)
    _inspect_existing_destination(destination)

    instructions = _text_entries(project, request.instructions, "instructions")
    changed_files = _text_entries(project, request.changed_files, "changed_files")
    related_files = _text_entries(project, request.related_files, "related_files")
    artifacts = _artifacts(project, request.visual_artifacts)
    filtered_diff = _filter_diff(request.diff)
    prompt = _render_prompt(
        request,
        filtered_diff,
        instructions,
        changed_files,
        related_files,
        artifacts,
    )
    _validate_review_scope_size(prompt)
    inventory = _packet_inventory(prompt, artifacts)
    digest = _packet_digest_from_inventory(inventory)
    manifest = _manifest_bytes(inventory, digest)

    _write_staged_packet(destination, prompt, manifest, artifacts)
    copied_artifacts = tuple(
        destination / "artifacts" / artifact.copied_name
        for artifact in artifacts
        if artifact.copied_name is not None
    )
    return PacketResult(destination / "prompt.md", digest, copied_artifacts)


def validate_packet(destination: Path) -> str:
    canonical = _canonical_destination(destination)
    _inspect_existing_destination(canonical)
    try:
        manifest = _parse_manifest((canonical / "manifest.json").read_bytes())
    except OSError:
        manifest = None
    if manifest is None or not _manifest_inventory_is_valid(manifest):
        raise _packet_error("Packet manifest is invalid", "destination")
    return str(manifest["packet_digest"])
