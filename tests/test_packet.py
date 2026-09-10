from dataclasses import FrozenInstanceError, fields
import hashlib
import json
from pathlib import Path
import traceback

import pytest

import mao_core.packet as packet_module
from mao_core.errors import MaoError
from mao_core.packet import PacketRequest, PacketResult, build_packet


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def packet_request(
    project: Path,
    changed_files=(),
    related_files=(),
    **overrides,
) -> PacketRequest:
    values = {
        "project": project,
        "user_request": "Implement the requested packet builder.",
        "acceptance_conditions": (),
        "instructions": (),
        "diff": (FIXTURE_DIR / "sample-diff.patch").read_text(encoding="utf-8"),
        "changed_files": tuple(changed_files),
        "related_files": tuple(related_files),
        "test_outputs": (),
        "visual_artifacts": (),
        "previous_decisions": (),
    }
    values.update(overrides)
    return PacketRequest(**values)


def assert_packet_path_invalid(request: PacketRequest, destination: Path) -> MaoError:
    with pytest.raises(MaoError) as caught:
        build_packet(request, destination)
    assert caught.value.code == "PACKET_PATH_INVALID"
    return caught.value


def assert_error_is_sealed(error: MaoError, *forbidden: str) -> None:
    surfaces = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.as_dict()),
            "".join(
                traceback.format_exception(
                    type(error),
                    error,
                    error.__traceback__,
                )
            ),
        )
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.details.keys() == {"field"}
    for value in forbidden:
        assert value not in surfaces


def directory_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    snapshot = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "directory", b""))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return tuple(snapshot)


def test_packet_types_have_exact_frozen_fields(tmp_path):
    assert [field.name for field in fields(PacketRequest)] == [
        "project",
        "user_request",
        "acceptance_conditions",
        "instructions",
        "diff",
        "changed_files",
        "related_files",
        "test_outputs",
        "visual_artifacts",
        "previous_decisions",
        "round_number",
    ]
    assert [field.name for field in fields(PacketResult)] == [
        "prompt_path",
        "digest",
        "copied_artifacts",
    ]

    request = packet_request(tmp_path)
    result = PacketResult(tmp_path / "prompt.md", "digest", ())
    with pytest.raises(FrozenInstanceError):
        request.diff = "replacement"
    with pytest.raises(FrozenInstanceError):
        result.digest = "replacement"


def test_oversized_review_scope_is_rejected_without_truncation_or_write(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"

    with pytest.raises(MaoError) as caught:
        build_packet(
            packet_request(project, user_request="x" * 200_000),
            destination,
        )

    assert caught.value.code == "REVIEW_SCOPE_TOO_LARGE"
    assert caught.value.details["actual_bytes"] > caught.value.details["maximum_bytes"]
    assert not destination.exists()


def test_packet_contains_ten_ordered_sections_and_json_only_contract(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("Follow local rules.\n", encoding="utf-8")
    (project / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / "related.txt").write_text("Relevant context.\n", encoding="utf-8")
    request = packet_request(
        project,
        changed_files=("changed.py",),
        related_files=("related.txt",),
        acceptance_conditions=("Packet is isolated.",),
        instructions=(Path("AGENTS.md"),),
        test_outputs=("python -m pytest: 12 passed",),
        previous_decisions=({"finding": "F-1", "decision": "accepted"},),
    )

    result = build_packet(request, tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    headings = [
        "# 1. Role and JSON-only output contract",
        "# 2. Original user request",
        "# 3. Acceptance conditions",
        "# 4. Applicable repository instructions",
        "# 5. Diff",
        "# 6. Changed files",
        "# 7. Related files",
        "# 8. Test commands and outputs",
        "# 9. Visual artifact index",
        "# 10. Previous decisions for round two",
    ]
    assert [prompt.index(heading) for heading in headings] == sorted(
        prompt.index(heading) for heading in headings
    )
    assert "Return exactly one JSON object" in prompt
    assert '"review_complete": true' in prompt
    assert '"severity": "critical|major|minor|note"' in prompt
    assert '"category": "correctness|requirements|regression|security|maintainability|visual|accessibility|other"' in prompt
    assert "Implement the requested packet builder." in prompt
    assert "Packet is isolated." in prompt
    assert "Follow local rules." in prompt
    assert "diff --git a/changed.py b/changed.py" in prompt
    assert "VALUE = 2" in prompt
    assert "Relevant context." in prompt
    assert "python -m pytest: 12 passed" in prompt
    assert '"decision": "accepted"' in prompt


def test_packet_contains_required_context_and_excludes_secret(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    request = packet_request(
        project=project,
        changed_files=["changed.py"],
        related_files=[".env"],
    )

    result = build_packet(request, tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert "VALUE = 2" in prompt
    assert "TOKEN=secret" not in prompt
    assert ".env: SECRET_PATH_EXCLUDED" in prompt


@pytest.mark.parametrize(
    "secret_path",
    [
        ".env",
        ".env.local",
        "cert.pem",
        "signing.key",
        "credentials",
        "credentials-prod.json",
        "secrets",
        "secrets.yaml",
        ".git/config",
        "node_modules/pkg/index.js",
        "bin/output.txt",
        "obj/cache.txt",
        "dist/app.js",
        "build/output.txt",
    ],
)
def test_packet_excludes_every_frozen_secret_build_and_dependency_pattern(
    tmp_path, secret_path
):
    project = tmp_path / "project"
    target = project / secret_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"NOT-UTF-8:\xffPRIVATE")

    result = build_packet(
        packet_request(project, related_files=(secret_path,)),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert f"{secret_path}: SECRET_PATH_EXCLUDED" in prompt
    assert "PRIVATE" not in prompt


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside",
        "safe/../../outside",
        "/absolute/path",
        r"C:\absolute\path",
    ],
)
def test_packet_rejects_path_escape_and_absolute_paths(tmp_path, unsafe_path):
    project = tmp_path / "project"
    project.mkdir()

    assert_packet_path_invalid(
        packet_request(project, related_files=(unsafe_path,)),
        tmp_path / "packet",
    )


def test_packet_rejects_symlink_resolving_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("DO_NOT_LEAK\n", encoding="utf-8")
    (project / "linked.txt").symlink_to(outside)

    error = assert_packet_path_invalid(
        packet_request(project, related_files=("linked.txt",)),
        tmp_path / "packet",
    )

    assert "DO_NOT_LEAK" not in json.dumps(error.as_dict())


def test_packet_excludes_in_project_symlink_alias_to_secret(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_bytes(b"TOKEN=private\xff")
    (project / "public-name.txt").symlink_to(project / ".env")

    result = build_packet(
        packet_request(project, related_files=("public-name.txt",)),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert "public-name.txt: SECRET_PATH_EXCLUDED" in prompt
    assert "TOKEN" not in prompt


def test_packet_rejects_directories_without_following_their_trees(tmp_path):
    project = tmp_path / "project"
    source = project / "src"
    source.mkdir(parents=True)
    (source / "hidden.txt").write_text("DO_NOT_LEAK\n", encoding="utf-8")

    error = assert_packet_path_invalid(
        packet_request(project, changed_files=("src",)),
        tmp_path / "packet",
    )

    assert "DO_NOT_LEAK" not in json.dumps(error.as_dict())
    assert not (tmp_path / "packet").exists()


def test_packet_reports_invalid_utf8_as_typed_error_without_content_or_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "sensitive-name.txt").write_bytes(b"TOKEN=private\xff")

    error = assert_packet_path_invalid(
        packet_request(project, changed_files=("sensitive-name.txt",)),
        tmp_path / "packet",
    )
    serialized = json.dumps(error.as_dict())

    assert "UTF-8" in error.message
    assert "TOKEN" not in serialized
    assert "private" not in serialized
    assert "sensitive-name" not in serialized


def test_packet_copies_visual_artifacts_with_collision_safe_names_and_hashes(
    tmp_path,
):
    project = tmp_path / "project"
    first = project / "screens/desktop/result.png"
    second = project / "screens/mobile/result.png"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"desktop-image")
    second.write_bytes(b"mobile-image")

    result = build_packet(
        packet_request(
            project,
            visual_artifacts=(
                "screens/desktop/result.png",
                "screens/mobile/result.png",
            ),
        ),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert len(result.copied_artifacts) == 2
    assert len({path.name for path in result.copied_artifacts}) == 2
    assert [path.read_bytes() for path in result.copied_artifacts] == [
        b"desktop-image",
        b"mobile-image",
    ]
    for source, copied in zip(
        ("screens/desktop/result.png", "screens/mobile/result.png"),
        result.copied_artifacts,
    ):
        expected_hash = hashlib.sha256(copied.read_bytes()).hexdigest()
        assert copied.parent == result.prompt_path.parent / "artifacts"
        assert f"{source} -> artifacts/{copied.name}" in prompt
        assert f"sha256: {expected_hash}" in prompt
    assert "read-only tools to inspect supplied files under ./artifacts" in prompt


def test_packet_excludes_secret_visual_artifact_without_copying_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "credentials.png").write_bytes(b"PRIVATE-IMAGE")

    result = build_packet(
        packet_request(project, visual_artifacts=("credentials.png",)),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert result.copied_artifacts == ()
    assert "credentials.png: SECRET_PATH_EXCLUDED" in prompt
    assert "PRIVATE-IMAGE" not in prompt


def test_equivalent_packet_builds_have_same_prompt_artifacts_and_digest(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "screen.png").write_bytes(b"same-image")
    request = packet_request(project, visual_artifacts=("screen.png",))

    first = build_packet(request, tmp_path / "packet-one")
    second = build_packet(request, tmp_path / "packet-two")

    assert first.digest == second.digest
    assert first.prompt_path.read_bytes() == second.prompt_path.read_bytes()
    assert [path.name for path in first.copied_artifacts] == [
        path.name for path in second.copied_artifacts
    ]
    assert [path.read_bytes() for path in first.copied_artifacts] == [
        path.read_bytes() for path in second.copied_artifacts
    ]


def test_rebuild_existing_destination_removes_stale_artifacts(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "old.png").write_bytes(b"old")
    (project / "new.png").write_bytes(b"new")
    destination = tmp_path / "packet"

    old_result = build_packet(
        packet_request(project, visual_artifacts=("old.png",)), destination
    )
    old_artifact = old_result.copied_artifacts[0]
    new_result = build_packet(
        packet_request(project, visual_artifacts=("new.png",)), destination
    )

    assert not old_artifact.exists()
    assert tuple((destination / "artifacts").iterdir()) == new_result.copied_artifacts
    assert new_result.copied_artifacts[0].read_bytes() == b"new"


def test_packet_rejects_destination_equal_to_project_without_mutation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "changed.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    before = {path.relative_to(project): path.read_bytes() for path in project.iterdir()}

    assert_packet_path_invalid(
        packet_request(project, changed_files=("changed.py",)),
        project,
    )

    after = {path.relative_to(project): path.read_bytes() for path in project.iterdir()}
    assert after == before


def test_packet_rejects_destination_nested_inside_project_without_mutation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "changed.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    destination = project / "review-packet"

    assert_packet_path_invalid(
        packet_request(project, changed_files=("changed.py",)),
        destination,
    )

    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not destination.exists()


def test_packet_allows_exact_managed_runtime_packet_destination(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = (
        project
        / ".multi-agent-orchestrator/runs/run-1/packet/claude"
    )

    result = build_packet(packet_request(project), destination)

    assert result.prompt_path == destination / "prompt.md"
    assert result.prompt_path.exists()


def test_packet_rejects_project_nested_inside_destination_without_mutation(tmp_path):
    destination = tmp_path / "container"
    project = destination / "project"
    project.mkdir(parents=True)
    source = project / "changed.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    marker = destination / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    assert_packet_path_invalid(
        packet_request(project, changed_files=("changed.py",)),
        destination,
    )

    assert source.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert not (destination / "prompt.md").exists()
    assert not (destination / "artifacts").exists()


def test_packet_rejects_destination_symlink_without_writing_through_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    destination = tmp_path / "packet"
    destination.symlink_to(outside, target_is_directory=True)

    assert_packet_path_invalid(packet_request(project), destination)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (outside / "prompt.md").exists()


def test_packet_rejects_artifact_directory_symlink_without_writing_through_it(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    destination.mkdir()
    (destination / "prompt.md").write_text("old prompt", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (destination / "artifacts").symlink_to(outside, target_is_directory=True)

    assert_packet_path_invalid(packet_request(project), destination)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (outside / "prompt.md").exists()


def test_packet_rejects_existing_destination_with_unmanaged_content(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    destination.mkdir()
    unmanaged = destination / "keep.txt"
    unmanaged.write_text("keep", encoding="utf-8")

    assert_packet_path_invalid(packet_request(project), destination)

    assert unmanaged.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("diff", "display_path"),
    [
        (
            "diff --git a/.env b/.env\n"
            "index 1111111..2222222 100644\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            "@@ -1 +1 @@\n"
            "-TOKEN=old-secret\n"
            "+TOKEN=new-secret\n",
            ".env",
        ),
        (
            "diff --git a/.env.local b/.env.local\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/.env.local\n"
            "@@ -0,0 +1 @@\n"
            "+TOKEN=local-secret\n",
            ".env.local",
        ),
        (
            "diff --git a/signing.key b/signing.key\n"
            "deleted file mode 100644\n"
            "--- a/signing.key\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-PRIVATE_KEY=deleted-secret\n",
            "signing.key",
        ),
    ],
)
def test_packet_replaces_entire_secret_diff_block(diff, display_path, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert f"{display_path}: SECRET_PATH_EXCLUDED" in prompt
    assert "diff --git" not in prompt
    assert "TOKEN=" not in prompt
    assert "PRIVATE_KEY=" not in prompt
    assert "index 1111111" not in prompt


@pytest.mark.parametrize(
    ("diff", "secret_path"),
    [
        (
            "diff --git a/public.txt b/.env\n"
            "similarity index 100%\n"
            "rename from public.txt\n"
            "rename to .env\n",
            ".env",
        ),
        (
            "diff --git a/.env b/public.txt\n"
            "similarity index 100%\n"
            "rename from .env\n"
            "rename to public.txt\n",
            ".env",
        ),
    ],
)
def test_packet_excludes_secret_rename_from_either_side(diff, secret_path, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert f"{secret_path}: SECRET_PATH_EXCLUDED" in prompt
    assert "rename from" not in prompt
    assert "rename to" not in prompt
    assert "public.txt" not in prompt


def test_packet_excludes_quoted_git_secret_path_without_leaking_block(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    diff = (
        'diff --git "a/config folder/.env.local" "b/config folder/.env.local"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/config folder/.env.local"\n'
        '+++ "b/config folder/.env.local"\n'
        "@@ -1 +1 @@\n"
        "-TOKEN=quoted-old\n"
        "+TOKEN=quoted-new\n"
    )

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert "config folder/.env.local: SECRET_PATH_EXCLUDED" in prompt
    assert "quoted-old" not in prompt
    assert "quoted-new" not in prompt
    assert "diff --git" not in prompt


def test_packet_excludes_secret_binary_diff_without_leaking_headers(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    diff = (
        "diff --git a/credentials.png b/credentials.png\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "Binary files /dev/null and b/credentials.png differ\n"
    )

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert "credentials.png: SECRET_PATH_EXCLUDED" in prompt
    assert "Binary files" not in prompt
    assert "index 0000000" not in prompt


def test_packet_preserves_complete_non_secret_text_and_binary_diff_blocks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    diff = (
        "diff --git a/source.py b/source.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/source.py\n"
        "+++ b/source.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
        "diff --git a/image.png b/image.png\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "Binary files /dev/null and b/image.png differ\n"
    )

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert diff in prompt


@pytest.mark.parametrize(
    "diff",
    [
        "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n",
        "diff --git a/source.py\n",
        "diff --git a/source.py b/source.py\n",
        'diff --git "a/unclosed b/source.py\n',
        (
            "diff --git a/source.py b/source.py\n"
            "--- /dev/null\n"
            "+++ /dev/null\n"
        ),
        (
            "diff --git a/source.py b/source.py\n"
            "--- a/other.py\n"
            "+++ b/source.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        ),
        (
            "diff --git a/source.py b/renamed.py\n"
            "similarity index 100%\n"
            "rename from other.py\n"
            "rename to renamed.py\n"
        ),
    ],
)
def test_packet_rejects_malformed_or_ambiguous_diff(diff, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"

    assert_packet_path_invalid(packet_request(project, diff=diff), destination)

    assert not destination.exists()


def test_packet_allows_empty_diff(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    result = build_packet(packet_request(project, diff=""), tmp_path / "packet")

    assert result.prompt_path.is_file()


def test_packet_writes_strict_manifest_covering_prompt_and_artifacts(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "screen.png").write_bytes(b"image-bytes")

    result = build_packet(
        packet_request(project, visual_artifacts=("screen.png",)),
        tmp_path / "packet",
    )
    destination = result.prompt_path.parent
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    artifact = result.copied_artifacts[0]

    assert {path.name for path in destination.iterdir()} == {
        "prompt.md",
        "artifacts",
        "manifest.json",
    }
    assert manifest == {
        "format_version": 1,
        "packet_digest": result.digest,
        "prompt_sha256": hashlib.sha256(result.prompt_path.read_bytes()).hexdigest(),
        "artifacts": [
            {
                "path": f"artifacts/{artifact.name}",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ],
    }


def test_packet_rejects_legacy_shape_without_manifest_and_preserves_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    destination.mkdir()
    prompt = destination / "prompt.md"
    prompt.write_text("legacy prompt\n", encoding="utf-8")
    artifacts = destination / "artifacts"
    artifacts.mkdir()
    stale = artifacts / "stale.png"
    stale.write_bytes(b"stale")

    assert_packet_path_invalid(packet_request(project), destination)

    assert prompt.read_text(encoding="utf-8") == "legacy prompt\n"
    assert stale.read_bytes() == b"stale"


@pytest.mark.parametrize("tampered_name", ["prompt.md", "artifact"])
def test_packet_rejects_hash_tampered_managed_destination_without_mutation(
    tmp_path, tampered_name
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "screen.png").write_bytes(b"original-image")
    destination = tmp_path / "packet"
    first = build_packet(
        packet_request(project, visual_artifacts=("screen.png",)), destination
    )
    target = (
        first.prompt_path
        if tampered_name == "prompt.md"
        else first.copied_artifacts[0]
    )
    target.write_bytes(b"tampered-content")
    before = directory_snapshot(destination)

    assert_packet_path_invalid(packet_request(project), destination)

    assert directory_snapshot(destination) == before


def test_packet_rejects_manifest_with_extra_fields_without_mutation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    build_packet(packet_request(project), destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = directory_snapshot(destination)

    assert_packet_path_invalid(packet_request(project), destination)

    assert directory_snapshot(destination) == before


def test_packet_default_and_second_round_are_explicit_and_change_digest(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    first = build_packet(packet_request(project), tmp_path / "round-one")
    second = build_packet(
        packet_request(project, round_number=2), tmp_path / "round-two"
    )

    assert "Review round: 1" in first.prompt_path.read_text(encoding="utf-8")
    assert "Review round: 2" in second.prompt_path.read_text(encoding="utf-8")
    assert first.digest != second.digest


@pytest.mark.parametrize("round_number", [0, 3, True, 1.0, "1"])
def test_packet_rejects_invalid_round_number(round_number, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert_packet_path_invalid(
        packet_request(project, round_number=round_number),
        tmp_path / "packet",
    )


def test_packet_accepts_recursively_strict_previous_decision_json(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    decisions = (
        {
            "finding": "F-1",
            "decision": "accepted",
            "proof": [None, True, 3, 1.25, {"source": "test"}],
        },
    )

    result = build_packet(
        packet_request(project, previous_decisions=decisions, round_number=2),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert '"finding": "F-1"' in prompt
    assert '"proof": [' in prompt


def invalid_previous_decisions():
    cycle = {}
    cycle["self"] = cycle
    return [
        ("not-a-dict",),
        ({1: "non-string-key"},),
        ({"value": object()},),
        ({"value": float("nan")},),
        ({"value": float("inf")},),
        ({"value": float("-inf")},),
        ({"value": "bad-surrogate-\ud800"},),
        ({"bad-key-\ud800": "value"},),
        (cycle,),
    ]


@pytest.mark.parametrize("decisions", invalid_previous_decisions())
def test_packet_rejects_non_strict_previous_decision_json(decisions, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    error = assert_packet_path_invalid(
        packet_request(project, previous_decisions=decisions),
        tmp_path / "packet",
    )

    assert_error_is_sealed(error, "bad-surrogate", "non-string-key")


def test_packet_missing_file_error_exposes_no_sensitive_exception_state(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    missing = "private-folder/private-source.txt"

    error = assert_packet_path_invalid(
        packet_request(project, changed_files=(missing,)),
        tmp_path / "packet",
    )

    assert_error_is_sealed(error, str(project), missing, "private-source")
    assert error.details == {"field": "changed_files"}


def test_packet_read_error_exposes_no_sensitive_exception_state(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "private-source.txt"
    source.write_text("safe source\n", encoding="utf-8")
    request = packet_request(project, changed_files=("private-source.txt",))
    original_read_bytes = Path.read_bytes

    def fail_sensitive_read(path):
        if path == source:
            raise OSError(f"{source}: TOKEN=read-secret")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_sensitive_read)

    error = assert_packet_path_invalid(request, tmp_path / "packet")

    assert_error_is_sealed(error, str(source), "TOKEN=read-secret", "private-source")
    assert error.details == {"field": "changed_files"}


def test_packet_decode_error_exposes_no_sensitive_exception_state(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "private-source.txt"
    source.write_bytes(b"TOKEN=decode-secret\xff")

    error = assert_packet_path_invalid(
        packet_request(project, changed_files=("private-source.txt",)),
        tmp_path / "packet",
    )

    assert_error_is_sealed(error, str(source), "TOKEN=decode-secret", "private-source")
    assert error.details == {"field": "changed_files"}


def assert_no_packet_temporaries(destination: Path) -> None:
    assert not tuple(destination.parent.glob(f".{destination.name}.staging-*"))
    assert not tuple(destination.parent.glob(f".{destination.name}.backup-*"))


def test_staging_write_failure_preserves_original_packet(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    build_packet(packet_request(project), destination)
    before = directory_snapshot(destination)
    original_write_bytes = Path.write_bytes

    def fail_staged_prompt(path, content):
        if path.name == "prompt.md" and ".staging-" in path.parent.name:
            raise OSError("TOKEN=staging-write-secret")
        return original_write_bytes(path, content)

    monkeypatch.setattr(Path, "write_bytes", fail_staged_prompt)

    error = assert_packet_path_invalid(packet_request(project), destination)

    assert directory_snapshot(destination) == before
    assert_no_packet_temporaries(destination)
    assert_error_is_sealed(error, "TOKEN=staging-write-secret")


def test_original_to_backup_rename_failure_preserves_original_packet(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    build_packet(packet_request(project), destination)
    before = directory_snapshot(destination)
    real_replace = packet_module.os.replace

    def fail_original_rename(source, target):
        if Path(source) == destination:
            raise OSError("TOKEN=original-rename-secret")
        return real_replace(source, target)

    monkeypatch.setattr(packet_module.os, "replace", fail_original_rename)

    error = assert_packet_path_invalid(packet_request(project), destination)

    assert directory_snapshot(destination) == before
    assert_no_packet_temporaries(destination)
    assert_error_is_sealed(error, "TOKEN=original-rename-secret")


def test_staging_to_destination_failure_rolls_original_packet_back(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    build_packet(packet_request(project), destination)
    before = directory_snapshot(destination)
    real_replace = packet_module.os.replace

    def fail_staging_commit(source, target):
        if Path(source).name.startswith(".packet.staging-"):
            raise OSError("TOKEN=commit-secret")
        return real_replace(source, target)

    monkeypatch.setattr(packet_module.os, "replace", fail_staging_commit)

    error = assert_packet_path_invalid(packet_request(project), destination)

    assert directory_snapshot(destination) == before
    assert_no_packet_temporaries(destination)
    assert_error_is_sealed(error, "TOKEN=commit-secret")


def test_rollback_failure_leaves_original_packet_recoverable(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    build_packet(packet_request(project), destination)
    before = directory_snapshot(destination)
    real_replace = packet_module.os.replace

    def fail_commit_and_rollback(source, target):
        source_path = Path(source)
        if source_path.name.startswith(".packet.staging-"):
            raise OSError("TOKEN=commit-secret")
        if source_path.name.startswith(".packet.backup-"):
            raise OSError("TOKEN=rollback-secret")
        return real_replace(source, target)

    monkeypatch.setattr(packet_module.os, "replace", fail_commit_and_rollback)

    error = assert_packet_path_invalid(packet_request(project), destination)
    backups = tuple(tmp_path.glob(".packet.backup-*"))

    assert not destination.exists()
    assert len(backups) == 1
    assert directory_snapshot(backups[0]) == before
    assert not tuple(tmp_path.glob(".packet.staging-*"))
    assert_error_is_sealed(error, "TOKEN=commit-secret", "TOKEN=rollback-secret")


def test_post_commit_backup_cleanup_failure_returns_committed_packet(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    build_packet(packet_request(project, user_request="old request"), destination)
    old_snapshot = directory_snapshot(destination)
    real_rmtree = packet_module.shutil.rmtree

    def fail_backup_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".packet.backup-"):
            raise OSError("TOKEN=cleanup-secret")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(packet_module.shutil, "rmtree", fail_backup_cleanup)

    result = build_packet(
        packet_request(project, user_request="new request"), destination
    )
    backups = tuple(tmp_path.glob(".packet.backup-*"))

    assert "new request" in result.prompt_path.read_text(encoding="utf-8")
    assert len(backups) == 1
    assert directory_snapshot(backups[0]) == old_snapshot


@pytest.mark.parametrize(
    "diff",
    [
        (
            "diff --git a/source.py b/source.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/source.py\n"
            "+++ b/source.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            "@@ -1 +1 @@\n"
            "-TOKEN=old-round2-secret\n"
            "+TOKEN=new-round2-secret\n"
        ),
        (
            "diff --git a/image.png b/image.png\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/image.png and b/image.png differ\n"
            "--- a/.env\n"
            "+++ b/.env\n"
            "+TOKEN=binary-round2-secret\n"
        ),
        (
            "diff --git a/source.py b/source.py\n"
            "index 1111111..2222222 100644\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+TOKEN=missing-markers-round2-secret\n"
        ),
        (
            "diff --git a/source.py b/source.py\n"
            "--- /dev/null\n"
            "+++ /dev/null\n"
            "@@ -0,0 +1 @@\n"
            "+TOKEN=null-round2-secret\n"
        ),
        (
            "diff --git a/source.py b/source.py\n"
            "--- a/source.py\n"
            "+++ b/source.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-only-one-old-line\n"
            "+TOKEN=count-round2-secret\n"
        ),
        (
            "diff --git a/source.py b/source.py\n"
            "--- a/source.py\n"
            "+++ b/source.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "rename to .env\n"
            "TOKEN=metadata-round2-secret\n"
        ),
        (
            "diff --git a/source.py b/source.py\n"
            "--- a/source.py\n"
            "+++ b/source.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "\\ No newline at end of file\n"
            "\\ No newline at end of file\n"
        ),
        (
            "diff --git a/image.png b/image.png\n"
            "new file mode 100644\n"
            "index 0000000..1111111\n"
            "GIT binary patch\n"
            "literal 1\n"
            "TOKEN=git-binary-round2-secret\n"
        ),
    ],
)
def test_packet_rejects_full_block_grammar_violations_without_secret_leak(
    diff, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"

    error = assert_packet_path_invalid(packet_request(project, diff=diff), destination)

    assert not destination.exists()
    assert_error_is_sealed(error, "TOKEN=", "round2-secret")


def test_packet_accepts_valid_multiple_hunks_and_no_newline_markers(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    diff = (
        "diff --git a/source.py b/source.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/source.py\n"
        "+++ b/source.py\n"
        "@@ -1,2 +1,2 @@ first\n"
        " context\n"
        "-old\n"
        "+new\n"
        "@@ -10 +10 @@ second\n"
        "-final-old\n"
        "\\ No newline at end of file\n"
        "+final-new\n"
        "\\ No newline at end of file\n"
    )

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")

    assert diff in result.prompt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "diff",
    [
        (
            "diff --git a/source.py b/source.py\n"
            "old mode 100644\n"
            "new mode 100755\n"
        ),
        (
            "diff --git a/old-name.py b/new-name.py\n"
            "similarity index 100%\n"
            "rename from old-name.py\n"
            "rename to new-name.py\n"
        ),
        (
            'diff --git "a/old name.py" "b/new name.py"\n'
            "similarity index 100%\n"
            'copy from "old name.py"\n'
            'copy to "new name.py"\n'
        ),
    ],
)
def test_packet_accepts_valid_metadata_only_blocks(diff, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")

    assert diff in result.prompt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "secret_path",
    [
        ".git",
        "node_modules",
        "bin",
        "obj",
        "dist",
        "build",
        "credentials-prod/token.json",
        "safe/secrets-v1/token.txt",
        "safe/.env.local/token.txt",
        "safe/certificate.pem/token.txt",
    ],
)
def test_packet_excludes_secret_or_generated_component_in_requested_text_path(
    secret_path, tmp_path
):
    project = tmp_path / "project"
    target = project / secret_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"TOKEN=component-round2-secret\xff")

    result = build_packet(
        packet_request(project, related_files=(secret_path,)),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert f"{secret_path}: SECRET_PATH_EXCLUDED" in prompt
    assert "component-round2-secret" not in prompt


@pytest.mark.parametrize(
    "secret_path",
    [
        ".git",
        "credentials-prod/screen.png",
        "safe/secrets-v1/screen.png",
        "safe/.env.local/screen.png",
    ],
)
def test_packet_excludes_secret_component_in_requested_visual_path(
    secret_path, tmp_path
):
    project = tmp_path / "project"
    target = project / secret_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"component-image-round2-secret")

    result = build_packet(
        packet_request(project, visual_artifacts=(secret_path,)),
        tmp_path / "packet",
    )
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert result.copied_artifacts == ()
    assert f"{secret_path}: SECRET_PATH_EXCLUDED" in prompt
    assert "component-image-round2-secret" not in prompt


@pytest.mark.parametrize(
    ("diff", "display_path"),
    [
        (
            "diff --git a/public.txt b/credentials-prod/token.json\n"
            "similarity index 100%\n"
            "rename from public.txt\n"
            "rename to credentials-prod/token.json\n",
            "credentials-prod/token.json",
        ),
        (
            "diff --git a/safe/secrets-v1/token.txt b/public.txt\n"
            "similarity index 100%\n"
            "rename from safe/secrets-v1/token.txt\n"
            "rename to public.txt\n",
            "safe/secrets-v1/token.txt",
        ),
        (
            "diff --git a/.git b/public.txt\n"
            "similarity index 100%\n"
            "rename from .git\n"
            "rename to public.txt\n",
            ".git",
        ),
    ],
)
def test_packet_excludes_secret_component_from_either_diff_path(
    diff, display_path, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")
    prompt = result.prompt_path.read_text(encoding="utf-8")

    assert f"{display_path}: SECRET_PATH_EXCLUDED" in prompt
    assert "diff --git" not in prompt
    assert "public.txt" not in prompt


@pytest.mark.parametrize("unicode_digit", ["١", "१"])
def test_packet_rejects_unicode_hunk_digits_with_sealed_error(
    unicode_digit, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    private_path = "round3-private.py"
    payload = "round3-unicode-secret"
    diff = (
        f"diff --git a/{private_path} b/{private_path}\n"
        f"--- a/{private_path}\n"
        f"+++ b/{private_path}\n"
        f"@@ -{unicode_digit},0 +1,1 @@ {payload}\n"
        "+new\n"
    )

    error = assert_packet_path_invalid(packet_request(project, diff=diff), destination)

    assert not destination.exists()
    assert_error_is_sealed(error, unicode_digit, payload, private_path, str(project))


def test_packet_rejects_oversized_hunk_number_without_raw_value_error(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    private_path = "round3-overflow-private.py"
    payload = "round3-overflow-secret"
    oversized_count = "9" * 5000
    diff = (
        f"diff --git a/{private_path} b/{private_path}\n"
        f"--- a/{private_path}\n"
        f"+++ b/{private_path}\n"
        f"@@ -1,{oversized_count} +1,0 @@ {payload}\n"
    )

    error = assert_packet_path_invalid(packet_request(project, diff=diff), destination)

    assert not destination.exists()
    assert_error_is_sealed(
        error,
        oversized_count[:64],
        payload,
        private_path,
        str(project),
    )


def test_packet_rejects_semantically_empty_hunk_with_sealed_error(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    private_path = "round3-empty-private.py"
    payload = "round3-empty-secret"
    diff = (
        f"diff --git a/{private_path} b/{private_path}\n"
        f"--- a/{private_path}\n"
        f"+++ b/{private_path}\n"
        f"@@ -0,0 +0,0 @@ {payload}\n"
    )

    error = assert_packet_path_invalid(packet_request(project, diff=diff), destination)

    assert not destination.exists()
    assert_error_is_sealed(error, payload, private_path, str(project))


def test_packet_accepts_hunk_number_upper_boundary(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    diff = (
        "diff --git a/source.py b/source.py\n"
        "--- a/source.py\n"
        "+++ b/source.py\n"
        "@@ -2147483647,0 +2147483647,1 @@\n"
        "+new\n"
    )

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")

    assert diff in result.prompt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "hunk_header",
    [
        "@@ -2147483648,0 +1,1 @@",
        "@@ -1,2147483648 +1,0 @@",
        "@@ -1,0 +2147483648,1 @@",
        "@@ -1,0 +1,2147483648 @@",
    ],
)
def test_packet_rejects_each_hunk_number_just_over_boundary(
    hunk_header, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "packet"
    private_path = "round3-boundary-private.py"
    payload = "round3-boundary-secret"
    diff = (
        f"diff --git a/{private_path} b/{private_path}\n"
        f"--- a/{private_path}\n"
        f"+++ b/{private_path}\n"
        f"{hunk_header} {payload}\n"
        "+new\n"
    )

    error = assert_packet_path_invalid(packet_request(project, diff=diff), destination)

    assert not destination.exists()
    assert_error_is_sealed(error, payload, private_path, str(project))


@pytest.mark.parametrize(
    "diff",
    [
        (
            "diff --git a/empty.txt b/empty.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/empty.txt\n"
            "@@ -0,0 +1,1 @@\n"
            "+content\n"
        ),
        (
            "diff --git a/empty.txt b/empty.txt\n"
            "deleted file mode 100644\n"
            "--- a/empty.txt\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-content\n"
        ),
    ],
)
def test_packet_preserves_valid_zero_count_on_one_hunk_side(diff, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    result = build_packet(packet_request(project, diff=diff), tmp_path / "packet")

    assert diff in result.prompt_path.read_text(encoding="utf-8")
