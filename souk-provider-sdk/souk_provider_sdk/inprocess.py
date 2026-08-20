from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ag_ui.core import Message
from pydantic import TypeAdapter

from souk_provider_sdk.identity import WrongSouk, souk_connect_payload, verify_signature
from souk_provider_sdk.link import SoukLink

if TYPE_CHECKING:
    from souk_provider_sdk.provider import DeliveredRun
    from souk_provider_sdk.runtime import ProviderRuntime

_MESSAGES = TypeAdapter(list[Message])


class InProcessLink(SoukLink):
    """A `SoukLink` connecting a `ProviderRuntime` directly to an in-process souk instance, with no transport in between."""

    def __init__(
        self, souk: Any, runtime: "ProviderRuntime", souk_public_key: str | None = None
    ) -> None:
        self._souk = souk
        self._runtime = runtime
        self._souk_public_key = souk_public_key or getattr(souk, "identity_public_key", None)
        runtime.link = self


    @property
    def public_key(self) -> str:
        return self._runtime.public_key

    def confirm_connect(self, souk_nonce: str, provider_nonce: str, answer: str | None) -> None:
        """Verify souk's answering signature against the pinned souk key, raising `WrongSouk` on a miss.

        The pin defaults to the identity the souk object itself claims; pass
        `souk_public_key` to demand a specific one. A souk with no identity
        answers nothing, which only a pinning link treats as a failure."""
        if self._souk_public_key is None:
            return
        if answer is None or not verify_signature(
            self._souk_public_key, answer, souk_connect_payload(souk_nonce, provider_nonce)
        ):
            raise WrongSouk(
                f"the souk answering this link-open did not prove '{self._souk_public_key}'"
            )

    @property
    def max_concurrent_runs(self) -> int | None:
        return self._runtime.max_concurrent_runs

    def sign_connect(
        self, souk_public_key: str, souk_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        """Sign the link-open proof, bound to the pinned souk key — refusing, before any
        signature leaves, a souk claiming a different key than the pin."""
        if self._souk_public_key is not None and souk_public_key != self._souk_public_key:
            raise WrongSouk(
                f"this link is pinned to '{self._souk_public_key}', "
                f"not the souk claiming '{souk_public_key}'"
            )
        return self._runtime.identity.sign_connect(
            souk_public_key, souk_nonce, provider_nonce, names
        )

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
