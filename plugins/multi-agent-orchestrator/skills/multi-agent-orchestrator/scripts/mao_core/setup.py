from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

from .config import Config
from .errors import MaoError
from .providers.base import ProviderAdapter
from .providers.base import ModelIdentity
from .transports.base import TransportAdapter


_EXECUTABLES = {
    "codex": "codex",
    "claude": "claude",
    "antigravity": "agy",
}
_FLAGS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "claude": "--dangerously-skip-permissions",
    "antigravity": "--dangerously-skip-permissions",
}


def launcher_command(provider: str, model: str) -> list[str]:
    if provider not in _EXECUTABLES:
        raise MaoError("CONFIG_INVALID", "Unsupported primary provider", {"provider": provider})
    if (
        not isinstance(model, str)
        or not model
        or any(ord(character) < 32 for character in model)
    ):
        raise MaoError("CONFIG_INVALID", "Primary model is invalid", {"provider": provider})
    return [_EXECUTABLES[provider], "--model", model, _FLAGS[provider]]


def _selected_model(config: Config, provider: str) -> str:
    return {
        "codex": config.codex_model,
        "claude": config.claude_model,
        "antigravity": config.antigravity_model,
    }[provider]


def _typed_error(error: Exception, fallback: str) -> dict:
    if isinstance(error, MaoError):
        return {"code": error.code, "message": error.message, "details": error.details}
    return {"code": fallback, "message": "Provider inspection failed", "details": {}}


def _provider_report(
    project: Path,
    config: Config,
    provider: str,
    adapter: ProviderAdapter | None,
    probe: bool,
) -> dict:
    selected = _selected_model(config, provider)
    if adapter is None:
        return {
            "provider": provider,
            "selected_model": selected,
            "status": "unavailable",
            "reason": {"code": "CLI_NOT_FOUND", "message": "Provider adapter is unavailable", "details": {}},
        }
    try:
        detected = adapter.detect()
    except Exception as error:
        return {
            "provider": provider,
            "selected_model": selected,
            "status": "unavailable",
            "reason": _typed_error(error, "CLI_NOT_FOUND"),
        }
    if not isinstance(detected, dict) or detected.get("status") != "available":
        return {
            "provider": provider,
            "selected_model": selected,
            "status": "unavailable",
            "detection": detected if isinstance(detected, dict) else {},
            "reason": {
                "code": "CLI_NOT_FOUND",
                "message": "Provider CLI is unavailable",
                "details": {"provider": provider},
            },
        }

    report = {
        "provider": provider,
        "selected_model": selected,
        "status": "available",
        "detection": detected,
    }
    try:
        report["auth"] = adapter.check_auth()
    except Exception as error:
        report["auth"] = {"status": "unknown", "reason": _typed_error(error, "AUTH_REQUIRED")}
    try:
        candidates = adapter.list_models()
        report["models"] = [asdict(item) for item in candidates]
        report["discovery"] = {
            "sources": list(dict.fromkeys(item.source for item in candidates)),
            "exhaustive": bool(candidates) and all(item.exhaustive for item in candidates),
        }
    except Exception as error:
        report["models"] = []
        report["discovery"] = {
            "sources": [],
            "exhaustive": False,
            "reason": _typed_error(error, "MODEL_LIST_UNSUPPORTED"),
        }
    if probe:
        try:
            identity = adapter.validate_model(selected, project)
            report["probe"] = asdict(identity)
            report["probe"]["resolution_source"] = getattr(
                adapter, "last_resolution_source", "unspecified"
            )
        except Exception as error:
            report["probe"] = {
                "verified": False,
                "reason": _typed_error(error, "MODEL_UNAVAILABLE"),
            }
    return report


def _transport_reports(
    config: Config,
    project: Path,
    transports: Mapping[str, TransportAdapter] | None,
) -> list[dict]:
    reports = [{"transport": "direct", "status": "available"}]
    for transport, executable in (("orca", "orca"), ("tmux", "tmux")):
        resolved = shutil.which(executable)
        reports.append(
            {
                "transport": transport,
                "status": "available" if resolved else "unavailable",
                "executable": resolved or executable,
                "selected": config.transport == transport,
                **(
                    {}
                    if resolved
                    else {
                        "reason": {
                            "code": "CLI_NOT_FOUND",
                            "message": f"Optional {transport} transport is unavailable",
                        }
                    }
                ),
            }
        )
    reports[0]["selected"] = config.transport == "direct"
    if config.transport != "direct" and transports is not None:
        selected = next(item for item in reports if item["transport"] == config.transport)
        adapter = transports.get(config.transport)
        validator = getattr(adapter, "validate_configuration", None)
        if adapter is None:
            selected["status"] = "unavailable"
            selected["reason"] = {
                "code": "CLI_NOT_FOUND",
                "message": "Selected transport adapter is unavailable",
            }
        elif callable(validator):
            try:
                for provider in ("codex", "claude", "antigravity"):
                    if provider != config.primary_provider:
                        validator(provider, project, min(config.timeout_seconds, 30))
                selected["status"] = "available"
                selected["executable"] = str(
                    getattr(adapter, "executable", config.transport)
                )
                selected.pop("reason", None)
            except Exception as error:
                selected["status"] = "invalid"
                selected["reason"] = _typed_error(error, "CONFIG_INVALID")
        elif config.transport == "orca":
            selected["status"] = "invalid"
            selected["reason"] = {
                "code": "CONFIG_INVALID",
                "message": "Selected Orca transport cannot prove unattended bypass",
                "details": {},
            }
    return reports


def setup_payload(
    project: Path,
    config: Config,
    providers: Mapping[str, ProviderAdapter],
    *,
    current_provider: str | None = None,
    current_model: str | None = None,
    probe: bool = False,
    transports: Mapping[str, TransportAdapter] | None = None,
) -> dict:
    root = Path(project).resolve()
    provider_reports = [
        _provider_report(root, config, name, providers.get(name), probe)
        for name in ("codex", "claude", "antigravity")
    ]
    models_by_provider = {
        item["provider"]: [model["requested"] for model in item.get("models", [])]
        for item in provider_reports
    }
    selected_primary_model = _selected_model(config, config.primary_provider)
    relaunch = current_provider is not None and (
        current_provider != config.primary_provider
        or current_model != selected_primary_model
    )
    return {
        "providers": provider_reports,
        "transports": _transport_reports(config, root, transports),
        "questions": [
            {
                "id": "primary_provider",
                "prompt": "Select primary provider",
                "options": [item["provider"] for item in provider_reports if item["status"] == "available"],
            },
            {
                "id": "models",
                "prompt": "Select exact model for each provider",
                "options": models_by_provider,
            },
            {
                "id": "transport",
                "prompt": "Select direct, Orca, or tmux transport",
                "options": ["direct", "orca", "tmux"],
            },
        ],
        "execution_profile": config.execution_profile,
        "dangerous_bypass_disclosed": True,
        "permission_mode_detected": False,
        "probe_may_consume_quota": bool(probe),
        "permission_mode_reason": "Existing host-session permission mode cannot be inspected or changed by this skill",
        "relaunch_required": relaunch,
        "relaunch_command": (
            launcher_command(config.primary_provider, selected_primary_model)
            if relaunch
            else []
        ),
    }


def write_setup_state(project: Path, payload: dict) -> Path:
    path = Path(project).resolve() / ".multi-agent-orchestrator/state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps({"format_version": 1, "setup": payload}, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".state.json.", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def load_setup_identities(project: Path) -> list[ModelIdentity]:
    path = Path(project).resolve() / ".multi-agent-orchestrator/state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        reports = value["setup"]["providers"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise MaoError(
            "STATE_TRANSITION_INVALID",
            "Verified setup state is unavailable; run configure --probe",
            {},
        ) from None
    identities: list[ModelIdentity] = []
    seen_providers: set[str] = set()
    if not isinstance(reports, list):
        raise MaoError("STATE_TRANSITION_INVALID", "Verified setup state is invalid", {})
    for report in reports:
        probe = report.get("probe") if isinstance(report, dict) else None
        if not isinstance(probe, dict) or probe.get("verified") is not True:
            continue
        try:
            identity = ModelIdentity(
                provider=probe["provider"],
                requested=probe["requested"],
                resolved=probe["resolved"],
                vendor=probe["vendor"],
                verified=True,
            )
        except (KeyError, TypeError):
            raise MaoError("STATE_TRANSITION_INVALID", "Verified model identity is invalid", {}) from None
        if not all(
            isinstance(item, str) and item
            for item in (
                identity.provider,
                identity.requested,
                identity.resolved,
                identity.vendor,
            )
        ):
            raise MaoError("STATE_TRANSITION_INVALID", "Verified model identity is invalid", {})
        if (
            identity.provider in seen_providers
            or report.get("provider") != identity.provider
            or report.get("selected_model") != identity.requested
        ):
            raise MaoError("STATE_TRANSITION_INVALID", "Verified model identity is invalid", {})
        seen_providers.add(identity.provider)
        identities.append(identity)
    return identities
