from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ag_ui.core import Message, RunAgentInput

from souk_provider_sdk.provider import DeliveredRun


class SoukLink(ABC):


    @property
    @abstractmethod
    def public_key(self) -> str:
        pass

    @property
    @abstractmethod
    def max_concurrent_runs(self) -> int | None:
        pass

    async def deliver(self, run: Any) -> bool:
        return await self.offer(
            DeliveredRun(
                run_id=run.run_id,
                agent_name=run.agent.name,
                run_input=RunAgentInput.model_validate(run.run_input),
                thread_id=run.thread_id,
            )
        )

    @abstractmethod
    async def offer(self, run: DeliveredRun) -> bool:
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
