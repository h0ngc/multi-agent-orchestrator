from __future__ import annotations

from mao_core.process import ProcessResult
from mao_core.providers.base import ProviderAdapter

from .base import InvocationRequest


class DirectTransport:
    def invoke(
        self,
        provider: ProviderAdapter,
        request: InvocationRequest,
    ) -> ProcessResult:
        return provider.invoke(
            request.model,
            request.packet,
            request.schema,
            request.cwd,
        )
