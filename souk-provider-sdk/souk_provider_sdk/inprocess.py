"""souk in this process: the shortest `SoukConnection` there is.

One of several, and deliberately named like the others — `InProcessProvider`
here, `SocketProvider` where a gateway carries the same four calls over a
WebSocket. In-process is a transport, not a special case, and nothing it gets
is a shortcut a remote provider does not: it registers, it proves its
identity, and souk offers it work the same way.

What it adds over the base is the report direction, which the base cannot
carry: it holds the `ProviderRuntime`, so it wires the runtime's two output
callbacks straight to souk. Over a wire those become frames written by
whatever is on the provider's side of the socket, and the connection souk
talks to has no runtime at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from souk_provider_sdk.connection import SoukConnection

if TYPE_CHECKING:
    from souk_provider_sdk.provider import DeliveredRun
    from souk_provider_sdk.runtime import ProviderRuntime


class InProcessProvider(SoukConnection):
    """A `ProviderRuntime` and a souk in one process, joined.

    Constructing one *sets* the runtime's callbacks, so build it before the
    runtime is given work: events queued while `on_event` is still None are
    dropped, silently and by design — a callback belongs to the caller, and
    one bad send must not kill the single consumer every run's ordering
    depends on.

    `souk` is anything with `report_event(run_id, event, claimed_by=...)` and
    `finish_run(run_id, claimed_by=...)`. A `Souk` object satisfies that in
    process; nothing here requires it to be one, and nothing here imports it.
    """

    def __init__(self, souk: Any, runtime: "ProviderRuntime") -> None:
        self._souk = souk
        self._runtime = runtime
        runtime.on_event = self._on_event
        runtime.on_finish = self._on_finish

    # ---- Who, and how much. Both the runtime's to declare; this only carries
    # them across the seam.

    @property
    def public_key(self) -> str:
        return self._runtime.public_key

    @property
    def max_concurrent_runs(self) -> int | None:
        return self._runtime.max_concurrent_runs

    # ---- The transport, such as it is

    async def offer(self, run: "DeliveredRun") -> bool:
        """A function call. There is nothing between the two ends, so the
        runtime's own answer is the ack."""
        return await self._runtime.deliver(run)

    def cancel(self, run_id: str) -> None:
        self._runtime.cancel(run_id)

    # ---- The report direction, which only exists when the runtime is local

    def _on_event(self, run_id: str, event: Any) -> None:
        self._souk.report_event(run_id, event, claimed_by=self.public_key)

    def _on_finish(self, run_id: str) -> None:
        self._souk.finish_run(run_id, claimed_by=self.public_key)
