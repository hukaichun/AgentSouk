"""A message sent while a run is active is delivered alongside it — never dropped.

The A2A door used to answer a mid-run message with the in-flight task and
silently discard the message. These tests pin what replaced that: every
utterance becomes its own run, offered to the provider in arrival order —
funduq imposes no turn-taking; sequencing a conversation is the provider's
own decision. Two special lanes ride on explicit declarations: a message
whose `taskId` names the thread's paused `input-required` task resumes that
task (the reply lane), and a run declaring `addressedRunId` (the
interjection extension) asks to join a turn in flight — intent the caller
states, never inferred from the target's state, judged by the agent alone.
"""

from __future__ import annotations

import asyncio

import pytest

from funduq import repo
from funduq.protocols.a2a import A2AAdapter


async def _rpc(funduq, agent, method: str, params: dict):
    return await A2AAdapter(funduq).handle_rpc(
        agent, {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    )


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.01)


class GateAgent:
    """Holds every run open until `release` is set, recording what it was handed."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.runs: list = []

    async def run_stream(self, agent_name: str, run_input):
        self.runs.append(run_input)
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        await self.release.wait()
        yield {"type": "RUN_FINISHED", **ids}


class AskingAgent:
    """Pauses its first round on an interrupt; any later round completes."""

    def __init__(self) -> None:
        self.rounds: list = []

    async def run_stream(self, agent_name: str, run_input):
        self.rounds.append(run_input)
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        if len(self.rounds) == 1:
            yield {
                "type": "RUN_FINISHED",
                **ids,
                "outcome": {
                    "type": "interrupt",
                    "interrupts": [{"id": "int_1", "reason": "question", "message": "which?"}],
                },
            }
        else:
            yield {"type": "RUN_FINISHED", **ids}


async def test_a_message_sent_mid_run_is_queued_not_dropped(funduq, serve):
    provider = GateAgent()
    served = await serve(provider, "busy")
    agent = served.agents["busy"]

    first = asyncio.create_task(
        _rpc(funduq, agent, "SendMessage", {"message": _message("start working")})
    )
    await _until(lambda: len(provider.runs) == 1)
    thread_id = provider.runs[0].thread_id

    second = asyncio.create_task(
        _rpc(
            funduq,
            agent,
            "SendMessage",
            {"message": {**_message("one more thing"), "contextId": thread_id}},
        )
    )

    # No turn-taking: the second utterance reaches the provider while the
    # first is still open. It is its own run — not merged — and what to do
    # with the overlap is the provider's decision.
    await _until(lambda: len(provider.runs) == 2)

    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second_result["result"]["id"] != first_result["result"]["id"]
    assert second_result["result"]["contextId"] == thread_id
    assert [r.thread_id for r in provider.runs] == [thread_id, thread_id]


async def test_a_reply_addressed_to_the_paused_task_resumes_it(funduq, serve):
    provider = AskingAgent()
    served = await serve(provider, "asker")
    agent = served.agents["asker"]

    first = await _rpc(funduq, agent, "SendMessage", {"message": _message("do the thing")})
    task_id = first["result"]["id"]
    assert first["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    second = await _rpc(
        funduq,
        agent,
        "SendMessage",
        {"message": {**_message("the answer"), "taskId": task_id}},
    )

    assert second["result"]["id"] == task_id, "a reply resumes the task, not a new one"
    assert second["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(provider.rounds) == 2
    assert provider.rounds[1].run_id == provider.rounds[0].run_id


async def test_an_unaddressed_message_does_not_resume_the_paused_task(funduq, serve):
    provider = AskingAgent()
    served = await serve(provider, "patient")
    agent = served.agents["patient"]

    first = await _rpc(funduq, agent, "SendMessage", {"message": _message("do the thing")})
    task_id = first["result"]["id"]
    thread_id = first["result"]["contextId"]

    unaddressed = asyncio.create_task(
        _rpc(
            funduq,
            agent,
            "SendMessage",
            {"message": {**_message("also, unrelated"), "contextId": thread_id}},
        )
    )

    # The unaddressed message does not resume the paused task — it is its
    # own run, delivered and answered while the question stays open.
    third = await unaddressed
    assert third["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert third["result"]["id"] != task_id

    async with funduq.session() as session:
        paused = await repo.get_run(session, task_id)
    assert paused.status == "input-required", "only an addressed reply may resume the question"

    reply = await _rpc(
        funduq, agent, "SendMessage", {"message": {**_message("the answer"), "taskId": task_id}}
    )
    assert reply["result"]["id"] == task_id
    assert reply["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(provider.rounds) == 3


async def test_reopen_run_refuses_a_run_that_is_not_in_the_expected_status(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "r"}])
    agent = registered["r"]
    thread_id = await repo.create_thread(session, agent)
    created = await repo.create_run(session, thread_id, agent, "ag-ui", {})
    await session.commit()

    reopened = await repo.reopen_run(
        session, created["run_id"], {}, expected_status="input-required"
    )

    assert reopened is False
    stored = await repo.get_run(session, created["run_id"])
    assert stored.status == "queued"


async def test_two_agui_runs_on_one_thread_flow_side_by_side(funduq, serve):
    from ag_ui.core import RunAgentInput, UserMessage

    from funduq.protocols.agui import AGUIAdapter

    def _body(thread_id: str, text: str) -> RunAgentInput:
        return RunAgentInput(
            thread_id=thread_id,
            run_id="ignored",
            state={},
            messages=[UserMessage(id="m1", role="user", content=text)],
            tools=[],
            context=[],
            forwarded_props={},
        )

    async def _drain(stream) -> list[dict]:
        return [event async for event in stream.events]

    provider = GateAgent()
    served = await serve(provider, "sse")
    agent = served.agents["sse"]
    adapter = AGUIAdapter(funduq)

    first = await adapter.run(agent, _body("t-sse", "start"))
    first_events = asyncio.create_task(_drain(first))
    await _until(lambda: len(provider.runs) == 1)

    second = await adapter.run(agent, _body(first.thread_id, "one more"))
    second_events = asyncio.create_task(_drain(second))
    # No turn-taking: the second run reaches the provider while the first is
    # still open; both streams are live.
    await _until(lambda: len(provider.runs) == 2)

    provider.release.set()
    assert {e["type"] for e in await first_events} >= {"RUN_STARTED", "RUN_FINISHED"}
    assert {e["type"] for e in await second_events} >= {"RUN_STARTED", "RUN_FINISHED"}
    assert [r.thread_id for r in provider.runs] == [first.thread_id, first.thread_id]


@pytest.fixture
async def tight(settings):
    """A funduq whose per-thread pending buffer holds exactly one run."""
    from funduq.config import CoreSettings
    from funduq.core import Funduq
    from funduq_provider_sdk import InProcessLink, ProviderIdentity, ProviderRuntime

    funduq = Funduq(
        CoreSettings(
            database_url=settings.database_url,
            token_signing_secret=settings.token_signing_secret,
            thread_queue_limit=1,
        )
    )
    await funduq.start()
    runtimes = []

    async def _serve(provider, name):
        identity = ProviderIdentity.generate()
        signature, timestamp = identity.sign_registration([name])
        registration = await funduq.register_agents(
            identity.public_key, signature, timestamp, [{"name": name}]
        )
        # One run at a time: with the provider's capacity full, further
        # utterances stay in funduq's own buffer — which is what these tests
        # bound. (funduq itself imposes no turn-taking.)
        runtime = ProviderRuntime(identity, provider, max_concurrent_runs=1)
        runtimes.append(runtime)
        runtime.start()
        await funduq.attach_provider(InProcessLink(funduq, runtime), [name])
        return registration.agents[name]

    funduq.serve_one = _serve
    try:
        yield funduq
    finally:
        for runtime in runtimes:
            await runtime.aclose(cancel_in_flight=True)
        await funduq.aclose()


async def test_a_full_thread_buffer_refuses_the_next_message_loudly(tight):
    from funduq.errors import ThreadQueueFull

    provider = GateAgent()
    agent = await tight.serve_one(provider, "guarded")

    first = asyncio.create_task(
        _rpc(tight, agent, "SendMessage", {"message": _message("start")})
    )
    await _until(lambda: len(provider.runs) == 1)
    thread_id = provider.runs[0].thread_id

    async def _first_is_running() -> bool:
        # The claim's status write is asynchronous; until it lands the first
        # run still counts as pending and would itself fill the limit-1 buffer.
        async with tight.session() as session:
            stored = await repo.get_run(session, provider.runs[0].run_id)
        return stored.status == "running"

    await _until(_first_is_running)

    second = asyncio.create_task(
        _rpc(tight, agent, "SendMessage", {"message": {**_message("waits"), "contextId": thread_id}})
    )

    async def _buffer_full() -> bool:
        async with tight.session() as session:
            return await repo.count_queued_runs_for_thread(session, thread_id) == 1

    await _until(_buffer_full)
    with pytest.raises(ThreadQueueFull, match="not accepted"):
        await A2AAdapter(tight).send_task(
            agent, _message("one too many"), context_id=thread_id
        )

    provider.release.set()
    results = await asyncio.gather(first, second)
    assert all(r["result"]["status"]["state"] == "TASK_STATE_COMPLETED" for r in results)


class AskThenHold:
    """Pauses its first round on a question; later rounds hold open until `release`."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.rounds: list = []

    async def run_stream(self, agent_name: str, run_input):
        self.rounds.append(run_input)
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        if len(self.rounds) == 1:
            yield {
                "type": "RUN_FINISHED",
                **ids,
                "outcome": {
                    "type": "interrupt",
                    "interrupts": [{"id": "int_1", "reason": "question", "message": "which?"}],
                },
            }
        else:
            await self.release.wait()
            yield {"type": "RUN_FINISHED", **ids}


async def test_answering_the_paused_question_is_never_refused_by_the_buffer(tight):
    provider = AskThenHold()
    agent = await tight.serve_one(provider, "asks")

    first = await _rpc(tight, agent, "SendMessage", {"message": _message("go")})
    task_id = first["result"]["id"]
    thread_id = first["result"]["contextId"]
    assert first["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    # A filler takes the provider's one slot and holds it; a second filler
    # then fills the thread's whole pending buffer (limit 1).
    filler = asyncio.create_task(
        _rpc(tight, agent, "SendMessage", {"message": {**_message("filler"), "contextId": thread_id}})
    )
    await _until(lambda: len(provider.rounds) == 2)
    filler_2 = asyncio.create_task(
        _rpc(tight, agent, "SendMessage", {"message": {**_message("more filler"), "contextId": thread_id}})
    )

    async def _buffer_full() -> bool:
        async with tight.session() as session:
            return await repo.count_queued_runs_for_thread(session, thread_id) == 1

    await _until(_buffer_full)

    # The reply lane never competes for buffer room: answering the question
    # is how the thread drains.
    reply = asyncio.create_task(
        _rpc(tight, agent, "SendMessage", {"message": {**_message("the answer"), "taskId": task_id}})
    )
    async def _reopened() -> bool:
        async with tight.session() as session:
            stored = await repo.get_run(session, task_id)
        return stored.status != "input-required"

    await _until(_reopened)

    provider.release.set()
    reply_result, filler_result, filler_2_result = await asyncio.gather(reply, filler, filler_2)
    assert reply_result["result"]["id"] == task_id
    assert reply_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert filler_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert filler_2_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_a_declared_interjection_reaches_the_agent_while_the_turn_is_open(funduq, serve):
    """The interjection extension: the caller *declares* the intent to join a
    turn in flight (never inferred from the target's state), funduq relays it
    as `forwardedProps.addressedRunId`, and the agent — sole holder of the
    live truth — judges what to do with it."""
    from funduq.props import ADDRESSED_RUN_METADATA_KEY

    provider = GateAgent()
    served = await serve(provider, "interject")
    agent = served.agents["interject"]

    first = asyncio.create_task(
        _rpc(funduq, agent, "SendMessage", {"message": _message("start")})
    )
    await _until(lambda: len(provider.runs) == 1)
    first_run_id = provider.runs[0].run_id
    thread_id = provider.runs[0].thread_id

    second = asyncio.create_task(
        _rpc(
            funduq,
            agent,
            "SendMessage",
            {
                "message": {
                    **_message("actually, in metric units"),
                    "contextId": thread_id,
                    "metadata": {ADDRESSED_RUN_METADATA_KEY: first_run_id},
                }
            },
        )
    )

    # The declared interjection reaches the agent while its target is open,
    # wearing the caller's intent.
    await _until(lambda: len(provider.runs) == 2)
    assert provider.runs[1].forwarded_props == {"addressedRunId": first_run_id}

    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second_result["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert second_result["result"]["id"] != first_run_id, "an interjection is still its own run"


async def test_a_task_id_naming_a_running_task_declares_nothing(funduq, serve):
    """taskId's only defined meaning at this door is the reply lane. Naming a
    running task starts an ordinary next run on its thread — no interjection
    is inferred, because intent is the caller's to declare, not funduq's to
    guess from the target's state."""
    provider = GateAgent()
    served = await serve(provider, "literal")
    agent = served.agents["literal"]

    first = asyncio.create_task(
        _rpc(funduq, agent, "SendMessage", {"message": _message("start")})
    )
    await _until(lambda: len(provider.runs) == 1)
    first_run_id = provider.runs[0].run_id

    second = asyncio.create_task(
        _rpc(
            funduq,
            agent,
            "SendMessage",
            {"message": {**_message("and another thing"), "taskId": first_run_id}},
        )
    )
    await _until(lambda: len(provider.runs) == 2)
    assert not (provider.runs[1].forwarded_props or {}), (
        "no declaration, no interjection — the run arrives unmarked"
    )
    assert provider.runs[1].thread_id == provider.runs[0].thread_id

    provider.release.set()
    results = await asyncio.gather(first, second)
    assert {r["result"]["status"]["state"] for r in results} == {"TASK_STATE_COMPLETED"}


async def test_addressing_the_paused_task_needs_no_answer_funduq_relays_anything(funduq, serve):
    provider = AskingAgent()
    served = await serve(provider, "overruled")
    agent = served.agents["overruled"]

    first = await _rpc(funduq, agent, "SendMessage", {"message": _message("book the flight")})
    task_id = first["result"]["id"]
    assert first["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    overrule = await _rpc(
        funduq,
        agent,
        "SendMessage",
        {"message": {**_message("forget the passport, book the train instead"), "taskId": task_id}},
    )

    assert overrule["result"]["id"] == task_id, "a non-answer resumes the task just the same"
    assert overrule["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    resumed_input = provider.rounds[1]
    contents = [m.content for m in resumed_input.messages if getattr(m, "content", None)]
    assert any("book the train instead" in c for c in contents), (
        "the provider receives the utterance verbatim and judges it itself"
    )
