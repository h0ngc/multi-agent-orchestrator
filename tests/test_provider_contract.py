from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import get_type_hints

import pytest

from mao_core.process import ProcessResult
from mao_core.providers.base import ModelCandidate, ModelIdentity, ProviderAdapter


def test_provider_model_types_are_frozen_and_keep_contract_field_order():
    candidate = ModelCandidate("gpt-test", "openai", "cache", True)
    identity = ModelIdentity("codex", "gpt-test", "gpt-resolved", "openai", True)

    assert [field.name for field in fields(candidate)] == [
        "requested",
        "vendor",
        "source",
        "exhaustive",
    ]
    assert [field.name for field in fields(identity)] == [
        "provider",
        "requested",
        "resolved",
        "vendor",
        "verified",
    ]
    with pytest.raises(FrozenInstanceError):
        candidate.requested = "changed"
    with pytest.raises(FrozenInstanceError):
        identity.resolved = "changed"


def test_provider_adapter_exposes_exact_typed_method_contract():
    expected = {
        "detect": {"return": dict},
        "check_auth": {"return": dict},
        "list_models": {"return": list[ModelCandidate]},
        "validate_model": {
            "model": str,
            "cwd": Path,
            "return": ModelIdentity,
        },
        "invoke": {
            "model": str,
            "packet": Path,
            "schema": Path,
            "cwd": Path,
            "return": ProcessResult,
        },
        "parse_result": {"result": ProcessResult, "return": dict},
        "measure_usage": {"parsed": dict, "return": dict},
    }

    assert get_type_hints(ProviderAdapter) == {"name": str}
    assert getattr(ProviderAdapter, "_is_protocol", False) is True
    for method_name, annotations in expected.items():
        assert get_type_hints(getattr(ProviderAdapter, method_name)) == annotations
