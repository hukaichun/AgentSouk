"""souk in this process: the shortest `SoukLink` there is.

One of several, and deliberately named like the others — `InProcessLink`
here, and whatever a socket client calls itself where a wire is involved.
In-process is a transport, not a special case: nothing it gets is a shortcut
a remote provider does not get, and it registers, proves its identity and is
offered work the same way.

Both directions are here because both directions are this object's, which is
the whole reason `SoukLink` covers both. Downward it converts and calls;
upward it calls souk directly. Neither half needs a queue, an ack or a
correlation id — that is what a transport is for, and there is no transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ag_ui.core import Message
from pydantic import TypeAdapter

from souk_provider_sdk.link import SoukLink

if TYPE_CHECKING:
    from souk_provider_sdk.provider import DeliveredRun
    from souk_provider_sdk.runtime import ProviderRuntime

# Built once: a TypeAdapter compiles its validator at construction, and every
# call to `InProcessLink.thread_messages` would otherwise pay that cost again
# for the same type.
_MESSAGES = TypeAdapter(list[Message])


class InProcessLink(SoukLink):
    """A `ProviderRuntime` and a souk in one process, joined.

    Constructing one *attaches it to the runtime*, so build it before the
    runtime is given work: events produced while the runtime has no link are
    dropped, silently and by design — reporting belongs to the caller, and one
    bad send must not kill the single consumer every run's ordering depends
    on.

    `souk` is anything with `report_event(run_id, event, claimed_by=...)` and
    `finish_run(run_id, claimed_by=...)`. A `Souk` object satisfies that in
    process; nothing here requires it to be one, and nothing here imports it.
    """

    def __init__(self, souk: Any, runtime: "ProviderRuntime") -> None:
        self._souk = souk
        self._runtime = runtime
        runtime.link = self

    # ---- souk → provider

    @property
    def public_key(self) -> str:
        return self._runtime.public_key

    @property
    def max_concurrent_runs(self) -> int | None:
        """Passed straight through. Capacity is the provider's to declare;
        this only carries it across the seam."""
        return self._runtime.max_concurrent_runs

    async def offer(self, run: "DeliveredRun") -> bool:
        """A function call. There is nothing between the two ends, so the
        runtime's own answer is the ack."""
        return await self._runtime.deliver(run)

    def cancel(self, run_id: str) -> None:
        self._runtime.cancel(run_id)

    # ---- provider → souk
    #
    # `claimed_by` is added here rather than carried by the runtime: which
    # identity holds a run is a fact about this link, and the runtime that
    # produced the event has no reason to know souk's argument names.
    #
    # Neither of these awaits anything, and both are still `async`: souk's
    # own calls are synchronous by its own design (a provider must not wait
    # on its persistence — the run's pipeline does that on its own task), but
    # the *interface* is where a socket transport needs to await its write.

    async def report_event(self, run_id: str, event: Any) -> None:
        self._souk.report_event(run_id, event, claimed_by=self.public_key)

    async def finish_run(self, run_id: str) -> None:
        self._souk.finish_run(run_id, claimed_by=self.public_key)

    async def thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[Message]:
        """souk's own read, validated and sliced here.

        `limit` is applied in this process because souk's query has no bound
        of its own. Over a wire the slicing has to happen on souk's side —
        the whole point of the parameter is to keep it out of the response
        frame — so a socket link sends `limit` rather than trimming what it
        receives.

        `souk.get_thread_messages` is a bare read of stored JSON with no
        shape guarantee of its own (see `repo.get_thread_messages`) — this is
        the one place that JSON is asked to actually be an AG-UI message
        before a provider ever sees it, the same check a socket link would
        get for free by decoding onto this type off the wire.
        """
        raw = await self._souk.get_thread_messages(thread_id)
        messages = _MESSAGES.validate_python(raw)
        return messages[-limit:] if limit is not None else messages
