from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ag_ui.core import Message
from pydantic import TypeAdapter

from funduq_provider_sdk.identity import WrongFunduq, funduq_connect_payload, verify_signature
from funduq_provider_sdk.link import FunduqLink

if TYPE_CHECKING:
    from funduq_provider_sdk.provider import DeliveredRun
    from funduq_provider_sdk.runtime import ProviderRuntime

_MESSAGES = TypeAdapter(list[Message])


class InProcessLink(FunduqLink):
    """A `FunduqLink` connecting a `ProviderRuntime` directly to an in-process funduq instance, with no transport in between."""

    def __init__(
        self, funduq: Any, runtime: "ProviderRuntime", funduq_public_key: str | None = None
    ) -> None:
        self._funduq = funduq
        self._runtime = runtime
        self._funduq_public_key = funduq_public_key or getattr(funduq, "identity_public_key", None)
        runtime.link = self


    @property
    def public_key(self) -> str:
        return self._runtime.public_key

    def confirm_connect(self, funduq_nonce: str, provider_nonce: str, answer: str | None) -> None:
        """Verify funduq's answering signature against the pinned funduq key, raising `WrongFunduq` on a miss.

        The pin defaults to the identity the funduq object itself claims; pass
        `funduq_public_key` to demand a specific one. A funduq with no identity
        answers nothing, which only a pinning link treats as a failure."""
        if self._funduq_public_key is None:
            return
        if answer is None or not verify_signature(
            self._funduq_public_key, answer, funduq_connect_payload(funduq_nonce, provider_nonce)
        ):
            raise WrongFunduq(
                f"the funduq answering this link-open did not prove '{self._funduq_public_key}'"
            )

    @property
    def max_concurrent_runs(self) -> int | None:
        return self._runtime.max_concurrent_runs

    def sign_connect(
        self, funduq_public_key: str, funduq_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        """Sign the link-open proof, bound to the pinned funduq key — refusing, before any
        signature leaves, a funduq claiming a different key than the pin."""
        if self._funduq_public_key is not None and funduq_public_key != self._funduq_public_key:
            raise WrongFunduq(
                f"this link is pinned to '{self._funduq_public_key}', "
                f"not the funduq claiming '{funduq_public_key}'"
            )
        return self._runtime.identity.sign_connect(
            funduq_public_key, funduq_nonce, provider_nonce, names
        )

    async def offer(self, run: "DeliveredRun") -> bool:
        return await self._runtime.deliver(run)

    def cancel(self, run_id: str) -> None:
        self._runtime.cancel(run_id)


    async def report_event(self, run_id: str, event: Any) -> None:
        self._funduq.report_event(run_id, event, claimed_by=self.public_key)

    async def finish_run(self, run_id: str) -> None:
        self._funduq.finish_run(run_id, claimed_by=self.public_key)

    async def thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[Message]:
        raw = await self._funduq.get_thread_messages(thread_id)
        messages = _MESSAGES.validate_python(raw)
        return messages[-limit:] if limit is not None else messages
