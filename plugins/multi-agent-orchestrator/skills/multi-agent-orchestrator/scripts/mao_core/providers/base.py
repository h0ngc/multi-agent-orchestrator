from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from mao_core.process import ProcessResult


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
    effort: str = ""


class ProviderAdapter(Protocol):
    name: str

    def detect(self) -> dict: ...

    def check_auth(self) -> dict: ...

    def list_models(self) -> list[ModelCandidate]: ...

    def list_efforts(self, model: str) -> dict: ...

    def validate_model(
        self, model: str, cwd: Path, effort: Optional[str] = None
    ) -> ModelIdentity: ...

    def invoke(
        self,
        model: str,
        packet: Path,
        schema: Path,
        cwd: Path,
        effort: Optional[str] = None,
    ) -> ProcessResult: ...

    def parse_result(self, result: ProcessResult) -> dict: ...

    def measure_usage(self, parsed: dict) -> dict: ...
