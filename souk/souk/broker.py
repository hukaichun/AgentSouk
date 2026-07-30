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
    agent_name: str
    thread_id: str
    input_json: dict[str, Any]
    protocol: str  # "ag-ui" | "a2a"
    output_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    seq: int = 0


class RunBroker:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._pending_by_agent: dict[str, deque[str]] = defaultdict(deque)

    def enqueue_run(
        self,
        run_id: str,
        agent_name: str,
        thread_id: str,
        input_json: dict[str, Any],
        protocol: str,
    ) -> RunState:
        state = RunState(
            run_id=run_id,
            agent_name=agent_name,
            thread_id=thread_id,
            input_json=input_json,
            protocol=protocol,
        )
        self._runs[run_id] = state
        self._pending_by_agent[agent_name].append(run_id)
        return state

    def poll(self, agent_names: list[str]) -> list[RunState]:
        found: list[RunState] = []
        for name in agent_names:
            queue = self._pending_by_agent.get(name)
            if not queue:
                continue
            while queue:
                run_id = queue.popleft()
                state = self._runs.get(run_id)
                if state is not None:
                    found.append(state)
        return found

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    async def push_event(self, run_id: str, event_json: dict[str, Any]) -> int:
        state = self._runs[run_id]
        state.seq += 1
        await state.output_queue.put(event_json)
        return state.seq

    async def close_run(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        await state.output_queue.put(END_OF_STREAM)

    def forget(self, run_id: str) -> None:
        self._runs.pop(run_id, None)


broker = RunBroker()
