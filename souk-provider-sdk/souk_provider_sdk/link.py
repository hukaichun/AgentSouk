from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ag_ui.core import Message, RunAgentInput
from pydantic import ValidationError

from souk_provider_sdk.provider import DeliveredRun, Refusal


class SoukLink(ABC):
    """A transport connecting a provider to souk; subclasses must implement every abstract member below
    (a subclass missing one, e.g. `max_concurrent_runs`, fails to construct with a TypeError).

    A link that crosses a process boundary must authenticate its open
    against a challenge the verifier chose — sign
    `provider_connect_payload` (and check souk's `souk_connect_payload`
    answer against the souk key you pinned). A self-chosen timestamp is not
    a challenge; a signature over one is replayable for its whole freshness
    window. `InProcessLink` skips this only because there is no boundary to
    cross.
    """

    @property
    @abstractmethod
    def public_key(self) -> str:
        pass

    @property
    @abstractmethod
    def max_concurrent_runs(self) -> int | None:
        pass

    async def deliver(self, run: Any) -> bool | Refusal:
        """Translates souk's internal claimed-run object into a `DeliveredRun` and hands it to `offer`.

        An input that doesn't validate as `RunAgentInput` is a permanent
        refusal, not a transient decline — re-offering the same bytes can
        never succeed."""
        try:
            delivered = DeliveredRun.from_claimed(run)
        except ValidationError as e:
            return Refusal(f"input does not validate as RunAgentInput: {e}")
        return await self.offer(delivered)

    @abstractmethod
    async def offer(self, run: DeliveredRun) -> bool | Refusal:
        """Accept (`True`), decline transiently (`False` — full right now), or refuse permanently (`Refusal`)."""
        pass

    @abstractmethod
    def cancel(self, run_id: str) -> None:
        pass


    @abstractmethod
    async def report_event(self, run_id: str, event: Any) -> None:
        pass

    @abstractmethod
    async def finish_run(self, run_id: str) -> None:
        pass

    @abstractmethod
    async def thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[Message]:
        pass
