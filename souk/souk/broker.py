"""In-memory hand-off between the HTTP gateway and the gRPC relay.

souk runs as a single process holding both the HTTP server and the gRPC
server on one event loop, so the live relay path (queue a run -> SDK polls
and discovers it -> SDK opens RunSession -> events stream back to the
waiting HTTP caller) is implemented with plain asyncio primitives rather
than round-tripping through Postgres. Postgres (souk/db.py) is the durable
record for anything that needs to survive a restart or be queried after
the fact (roster, thread history, run status, run_events) — it is not on
the live event-relay hot path.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

# Sentinel put on a run's output queue to signal the stream has ended.
END_OF_STREAM = object()


@dataclass
class RunState:
    run_id: str
    agent_id: str
    thread_id: str
    input_json: dict[str, Any]
    protocol: str  # "ag-ui" | "a2a"
    output_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    seq: int = 0
    # Set by grpc_server._relay_event when it sees a souk.pause CUSTOM
    # event (see souk/pause.py) — read by _finish_run to decide whether
    # this run ended paused ('input-required') rather than 'completed'.
    pause_payload: dict[str, Any] | None = None


class RunBroker:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._pending_by_agent: dict[str, deque[str]] = defaultdict(deque)

    def enqueue_run(
        self,
        run_id: str,
        agent_id: str,
        thread_id: str,
        input_json: dict[str, Any],
        protocol: str,
    ) -> RunState:
        state = RunState(
            run_id=run_id,
            agent_id=agent_id,
            thread_id=thread_id,
            input_json=input_json,
            protocol=protocol,
        )
        self._runs[run_id] = state
        self._pending_by_agent[agent_id].append(run_id)
        return state

    def poll(self, agent_ids: list[str], max_claim: int | None = None) -> list[RunState]:
        """Returns (and removes from the pending queue) up to `max_claim`
        runs across `agent_ids`, round-robining between agents so a cap
        doesn't starve later ids in the list.

        `max_claim=None` means the caller never reported a capacity at
        all — drains everything currently queued, unlimited (the old
        all-at-once behavior). `max_claim=0` is different: it means the
        caller explicitly reported "no spare capacity right now", so
        nothing is claimed — distinct from None, not a stand-in for it.
        """
        if max_claim is not None and max_claim <= 0:
            return []

        found: list[RunState] = []
        queues = [self._pending_by_agent.get(agent_id) for agent_id in agent_ids]
        while any(queues):
            if max_claim is not None and len(found) >= max_claim:
                break
            progressed = False
            for queue in queues:
                if not queue:
                    continue
                if max_claim is not None and len(found) >= max_claim:
                    break
                run_id = queue.popleft()
                progressed = True
                state = self._runs.get(run_id)
                if state is not None:
                    found.append(state)
            if not progressed:
                break
        return found

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def next_seq(self, run_id: str) -> int | None:
        # None means "this run is already forgotten" — a legitimate race,
        # not a bug: a run gets forgotten (see forget()) as soon as its
        # HTTP/SSE consumer finishes reading, but the agent side streams
        # frames independently and a straggler can still be in flight
        # (e.g. two concurrent runs sharing one AgentSession connection,
        # see souk_agent_sdk.client._write_loop). Callers must treat None
        # as "drop this frame", matching close_run's existing
        # already-gone-is-fine handling below, rather than crash.
        state = self._runs.get(run_id)
        if state is None:
            return None
        state.seq += 1
        return state.seq

    async def deliver_event(self, run_id: str, event_json: dict[str, Any]) -> None:
        # Split from next_seq() deliberately: callers must persist the
        # event durably *before* calling this — this is what actually
        # makes it visible to the live SSE caller, and there's no undoing
        # that once it's happened.
        state = self._runs.get(run_id)
        if state is None:
            return
        await state.output_queue.put(event_json)

    async def close_run(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        await state.output_queue.put(END_OF_STREAM)

    def forget(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


broker = RunBroker()
