import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from souk import repo
from souk.agui import build_run_agent_input
from souk.broker import END_OF_STREAM, broker
from souk.db import get_session
from souk.models import RunAgentInput

router = APIRouter()


@router.get("/threads/{thread_id}")
async def get_thread_snapshot(thread_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Lets a caller catch up on a thread without a live stream — e.g.
    after its original AG-UI SSE connection closed because the run it was
    watching paused (status='input-required'), and it needs to know
    whether/what has happened since. See repo.get_thread_snapshot.
    """
    snapshot = await repo.get_thread_snapshot(session, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"thread '{thread_id}' not found")
    return snapshot


@router.post("/agui/{agent_name}", response_model=None)
async def run_agent(
    agent_name: str, body: RunAgentInput, session: AsyncSession = Depends(get_session)
) -> EventSourceResponse | JSONResponse:
    agent = await repo.get_agent(session, agent_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_name}' is not registered")

    if body.thread_id is not None:
        thread = await repo.get_thread(session, body.thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread '{body.thread_id}' not found")
        if thread["agent_name"] != agent_name:
            raise HTTPException(
                status_code=409,
                detail=f"thread '{body.thread_id}' belongs to agent '{thread['agent_name']}', not '{agent_name}'",
            )
        # A thread only ever has one active run at a time (see
        # repo.get_active_run_for_thread) — starting a second one
        # concurrently would fork its otherwise-linear history with no
        # clean way to merge it back. Instead of erroring or silently
        # queueing a duplicate, hand back the thread's current state
        # (including the pending run's real status) so the caller can act
        # on it — this doubles as the "catch me up" path for a run that
        # has since paused.
        active = await repo.get_active_run_for_thread(session, body.thread_id)
        if active is not None and not (body.resume and active["status"] == "input-required"):
            snapshot = await repo.get_thread_snapshot(session, body.thread_id)
            return JSONResponse(
                jsonable_encoder(snapshot),
                headers={"X-Souk-Thread-Id": body.thread_id, "X-Souk-Run-Id": active["run_id"]},
            )
        resuming_run_id = active["run_id"] if active is not None else None
    else:
        resuming_run_id = None

    thread_id = await repo.ensure_thread(session, agent_name, body.thread_id, metadata=body.metadata)

    created = await repo.create_run(
        session, thread_id, agent_name, "ag-ui", body.model_dump(mode="json"), metadata=body.metadata
    )
    run_id = created["run_id"]
    if resuming_run_id is not None:
        await repo.mark_run_resumed(session, resuming_run_id, run_id)

    try:
        input_json = build_run_agent_input(thread_id, run_id, body.messages)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

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
