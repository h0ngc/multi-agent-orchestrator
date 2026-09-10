from __future__ import annotations

from typing import Sequence

from mao_core.providers.base import ModelCandidate, ProviderAdapter


def discover_all(
    adapters: Sequence[ProviderAdapter],
) -> dict[str, list[ModelCandidate]]:
    return {adapter.name: adapter.list_models() for adapter in adapters}


def classify_vendor(provider: str, resolved_model: str) -> str:
    value = resolved_model.lower()
    if value.startswith(("gpt-", "o1", "o3", "o4", "codex-")):
        return "openai"
    if "claude" in value:
        return "anthropic"
    if "gemini" in value:
        return "google"
    return provider
