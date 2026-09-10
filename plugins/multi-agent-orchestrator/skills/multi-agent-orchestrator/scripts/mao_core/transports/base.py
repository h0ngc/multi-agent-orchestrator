from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mao_core.process import ProcessResult
from mao_core.providers.base import ProviderAdapter


@dataclass(frozen=True)
class InvocationRequest:
    provider: str
    model: str
    packet: Path
    schema: Path
    cwd: Path
    timeout_seconds: int
    run_id: str


class TransportAdapter(Protocol):
    def invoke(
        self,
        provider: ProviderAdapter,
        request: InvocationRequest,
    ) -> ProcessResult: ...
