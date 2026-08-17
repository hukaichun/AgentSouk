from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ag_ui.core import Message
from pydantic import TypeAdapter

from souk_provider_sdk.link import SoukLink

if TYPE_CHECKING:
    from souk_provider_sdk.provider import DeliveredRun
    from souk_provider_sdk.runtime import ProviderRuntime

_MESSAGES = TypeAdapter(list[Message])


class InProcessLink(SoukLink):

    def __init__(self, souk: Any, runtime: "ProviderRuntime") -> None:
        self._souk = souk
        self._runtime = runtime
        runtime.link = self


    @property
    def public_key(self) -> str:
        return self._runtime.public_key

    @property
    def max_concurrent_runs(self) -> int | None:
        return self._runtime.max_concurrent_runs

    async def offer(self, run: "DeliveredRun") -> bool:
        return await self._runtime.deliver(run)

    def cancel(self, run_id: str) -> None:
        self._runtime.cancel(run_id)


    async def report_event(self, run_id: str, event: Any) -> None:
        self._souk.report_event(run_id, event, claimed_by=self.public_key)

    async def finish_run(self, run_id: str) -> None:
        self._souk.finish_run(run_id, claimed_by=self.public_key)

    async def thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[Message]:
        raw = await self._souk.get_thread_messages(thread_id)
        messages = _MESSAGES.validate_python(raw)
        return messages[-limit:] if limit is not None else messages
