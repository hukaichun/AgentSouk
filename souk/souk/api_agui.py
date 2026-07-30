import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from souk import repo
from souk.broker import END_OF_STREAM, broker
from souk.db import get_session
from souk.models import RunAgentInput

router = APIRouter()


@router.post("/agui/{agent_name}")
async def run_agent(
    agent_name: str, body: RunAgentInput, session: AsyncSession = Depends(get_session)
) -> EventSourceResponse:
    agent = await repo.get_agent(session, agent_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_name}' is not registered")

    if body.thread_id is not None:
        thread = await repo.get_thread(session, body.thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread '{body.thread_id}' not found")

    thread_id = await repo.ensure_thread(session, agent_name, body.thread_id)

    created = await repo.create_run(
        session, thread_id, agent_name, "ag-ui", body.model_dump(mode="json")
    )
    run_id = created["run_id"]

    input_json = body.model_dump(mode="json")
    input_json["thread_id"] = thread_id
    input_json["run_id"] = run_id

    await repo.append_thread_messages(session, thread_id, run_id, body.messages)
    await session.commit()

    state = broker.enqueue_run(run_id, agent_name, thread_id, input_json, "ag-ui")

    async def event_stream():
        try:
            while True:
                item = await state.output_queue.get()
                if item is END_OF_STREAM:
                    break
                yield {"event": "message", "data": json.dumps(item)}
        finally:
            broker.forget(run_id)

    return EventSourceResponse(
        event_stream(),
        headers={"X-Souk-Thread-Id": thread_id, "X-Souk-Run-Id": run_id},
    )
