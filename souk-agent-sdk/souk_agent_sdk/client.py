"""A souk provider: one identity, several agents, and the loop that serves
them.

`SoukProvider` is named for what it *is* in souk's own vocabulary — a
provider is one keypair offering a batch of agents, which is exactly what
this object holds. It is a client of souk's API, and it contains a worker
loop, and it routes by agent_id; those are things it does. souk's in-process
port is the same noun with the same method (`souk/providers.py`), so an agent
moving between here and inside souk stays the same shape.

The loop is the whole thing, and it is the one souk runs for a provider
hosted in its own process (souk/worker.py) — this SDK is that loop with a
wire in the middle:

    claim runs (with their input)  ->  run each one  ->  push its events back

Registers a batch of AG-UI-shaped agents over HTTP, then holds one
outbound WebSocket to the gateway's `/ws/provider` (souk never connects to
anyone — that is the architecture, forced by NAT topology). After `hello`,
the *server* drives the claim loop on this worker's behalf and pushes each
claimed run down the socket, input included; this side runs them and
pushes `event`/`finish` frames back. Flow control is the `maxClaim` budget
declared in `hello` — `finish` is the credit, no polling on this side at
all. The frame table is authored in the gateway repo's
docs/server-mode.md; this SDK implements it.

This SDK is a convenience client, not the protocol itself — anything that
speaks those JSON frames over a WebSocket (in any language, a browser
included: they are camelCase text messages for exactly that reason) is an
equally valid souk provider; souk never special-cases this implementation.

Agents are agnostic to pydantic-ai or any other implementation — the only
requirement is `run_stream(RunAgentInput dict) -> AsyncIterator[dict]`
yielding AG-UI event dicts, which is exactly what pydantic-ai's AG-UI
adapter already produces.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets

from souk_agent_sdk.identity import load_or_create_identity, public_key_hex, registration_signing_payload, sign

logger = logging.getLogger("souk_agent_sdk")

RunStream = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]

# How long after the handshake the server may take to answer `hello` with
# `welcome` before this side gives up and reconnects.
WELCOME_TIMEOUT_SECONDS = 10.0


@dataclass
class AgentHandle:
    name: str
    run_stream: RunStream
    description: str = ""
    agent_card_extra: dict[str, Any] = field(default_factory=dict)


class SoukProvider:
    """Satisfies souk's Provider port (`run_stream(agent_id, run_input)`) by
    routing to whichever `AgentHandle` owns that agent_id — so an agent is
    still declared the way AG-UI defines one, and the routing every provider
    serving several agents needs is done once, here, rather than in each
    agent.

    Override `run_stream` to route differently (dynamic agents, a shared
    model pool, a dispatch table of your own); everything else — the
    socket, concurrency, reporting, cancellation — is unchanged by that.
    """

    def __init__(
        self,
        souk_http_url: str,
        agents: list[AgentHandle],
        reconnect_delay: float = 2.0,
        max_concurrent_runs: int | None = None,
        identity_key_path: str = "souk_identity.key",
        ca_cert_path: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        # One URL is the whole address now: registration posts to it, and
        # the work socket is the same listener with the scheme swapped
        # (http -> ws, https -> wss) — see _ws_url. The separate gRPC
        # address this took before the WebSocket transport is gone with
        # the transport.
        self.souk_http_url = souk_http_url.rstrip("/")
        # Optional storefront label for this provider's public_key,
        # shown when souk-directory groups agents by provider — see
        # souk/db.py's providers table. Purely descriptive; unset means
        # "didn't say", not "cleared" (souk leaves any previously-set
        # name alone rather than blanking it — see repo.register_agents).
        self.provider_name = provider_name
        # Path to a CA (or self-signed) cert this souk's TLS certificate
        # should be verified against — leave unset only for plaintext
        # development. Verifying against a specific file here (rather
        # than the system trust store) is what makes this provider
        # actually confirm it's talking to *this* souk and not an
        # impostor on the network; it covers both the registration POST
        # and the wss socket, which ride the same listener and the same
        # certificate.
        self.ca_cert_path = ca_cert_path
        self.agents: dict[str, AgentHandle] = {a.name: a for a in agents}
        # Populated by register() from the {name: agent_id} map souk
        # returns — dispatch is keyed by agent_id, not name, since name is
        # no longer a unique routing key (see souk/db.py's
        # UNIQUE(public_key, name)). Empty until the first successful
        # registration.
        self._handle_by_id: dict[str, AgentHandle] = {}
        self.reconnect_delay = reconnect_delay
        # This provider's identity to any souk it connects to — see
        # souk_agent_sdk.identity. Persisted to disk: restarting this
        # process must keep resolving to the same agent_ids it registered
        # before, which only works if it keeps using the same keypair (see
        # souk_agent_sdk.identity's module docstring).
        self._identity = load_or_create_identity(identity_key_path)
        self._session_token: str | None = None
        # How many runs this provider will hold at once, across all its
        # agents combined — declared to souk as `maxClaim` in hello, and
        # enforced there: the server claims further runs only while
        # in-flight is under it. None means unlimited.
        #
        # If one of your agents delegates (A2A) to another agent *this same
        # provider* hosts, leave room for the whole chain: the delegating
        # run holds a slot while it waits, and the delegated run needs a
        # free one before anything claims it. A provider that recurses into
        # itself should leave this unlimited. Delegating to a different
        # provider is unaffected — it has its own budget.
        self.max_concurrent_runs = max_concurrent_runs

        # Frames waiting to go out. Deliberately *not* per connection: a run
        # is addressed by runId rather than by the socket it arrived on, so
        # events (and the finish) of a run cut short by a dropped connection
        # are still worth sending, and go out on the next one. This is the
        # reconnect-and-finish property the gateway's tests pin.
        self._outbound: asyncio.Queue = asyncio.Queue()
        # Runs currently being executed, by run_id. The worker's own
        # bookkeeping — what to stop when souk asks. The claim budget lives
        # on the server now; nothing here counts toward it except by
        # sending finish frames.
        self._in_flight: dict[str, asyncio.Task] = {}

    @property
    def _ws_url(self) -> str:
        scheme, netloc, path, _query, _fragment = urlsplit(self.souk_http_url)
        ws_scheme = "wss" if scheme == "https" else "ws"
        return urlunsplit((ws_scheme, netloc, path.rstrip("/") + "/ws/provider", "", ""))

    async def register(self) -> None:
        names = [a.name for a in self.agents.values()]
        timestamp = int(time.time())
        payload = registration_signing_payload(names, timestamp)
        async with httpx.AsyncClient(verify=self.ca_cert_path or True) as client:
            resp = await client.post(
                f"{self.souk_http_url}/agents/register",
                json={
                    "public_key": public_key_hex(self._identity),
                    "signature": sign(self._identity, payload),
                    "timestamp": timestamp,
                    "provider_name": self.provider_name,
                    "agents": [
                        {
                            "name": a.name,
                            "description": a.description,
                            "agent_card_extra": a.agent_card_extra,
                        }
                        for a in self.agents.values()
                    ],
                },
            )
            resp.raise_for_status()
        body = resp.json()
        self._session_token = body["session_token"]
        agent_ids: dict[str, str] = body["agent_ids"]
        self._handle_by_id = {
            agent_ids[name]: handle for name, handle in self.agents.items() if name in agent_ids
        }
        logger.info(
            "registered %d agent(s) as provider %s", len(self.agents), public_key_hex(self._identity)
        )

    async def run_forever(self) -> None:
        """Keeps a connection to souk alive indefinitely — reconnecting
        with a fixed delay if the socket ever drops or errors, so a
        transient network blip doesn't permanently stop this provider from
        serving. Re-registers on every (re)connect, not just the first —
        that's also how the bearer token gets refreshed before it expires
        (see souk.identity.SESSION_TOKEN_TTL_SECONDS), without a separate
        renewal mechanism; the gateway closes a socket whose token aged
        out for exactly this loop to pick up.
        """
        while True:
            try:
                await self.register()
                await self._run_connection()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "souk connection lost; reconnecting in %.1fs", self.reconnect_delay
                )
            await asyncio.sleep(self.reconnect_delay)

    async def _run_connection(self) -> None:
        """One socket's lifetime: hello, then serve every frame the server
        pushes until the connection ends. The token rides both tracks —
        the Authorization header so an edge can gate the handshake before
        accepting it, and `hello` because agentIds/maxClaim travel there
        anyway (the gateway requires the two to match when both appear).
        """
        ssl_context: ssl.SSLContext | None = None
        if self._ws_url.startswith("wss") and self.ca_cert_path:
            ssl_context = ssl.create_default_context(cafile=self.ca_cert_path)
        async with websockets.connect(
            self._ws_url,
            additional_headers={"Authorization": f"Bearer {self._session_token}"},
            ssl=ssl_context,
        ) as ws:
            hello: dict[str, Any] = {
                "type": "hello",
                "token": self._session_token,
                "agentIds": list(self._handle_by_id.keys()),
            }
            if self.max_concurrent_runs is not None:
                hello["maxClaim"] = self.max_concurrent_runs
            await ws.send(json.dumps(hello))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), WELCOME_TIMEOUT_SECONDS))
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"expected welcome, got {welcome!r}")

            writer = asyncio.create_task(self._write_loop(ws))
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    kind = frame.get("type")
                    if kind == "run":
                        self._dispatch(frame)
                    elif kind == "cancel":
                        # souk asked; it did not decide. Cancelling the
                        # task delivers CancelledError into run_stream's
                        # *current* await, so an in-flight LLM or tool
                        # call is really interrupted rather than paid for
                        # and discarded. Whatever the run emits between
                        # now and its finish is real output souk persists;
                        # a worker that ignored this and finished normally
                        # would have its run recorded as completed.
                        task = self._in_flight.get(frame.get("runId"))
                        if task is None:
                            # A run that finished while the request was in
                            # flight — or one claimed on a previous socket
                            # of this identity (souk asks every socket the
                            # identity has open; a socket without the run
                            # ignores the frame).
                            logger.debug("cancel for unknown/finished runId=%s", frame.get("runId"))
                        else:
                            logger.info("run %s: souk asked it to stop", frame["runId"])
                            task.cancel()
                    elif kind == "error":
                        # Advisory: the server refused one of our frames
                        # (a straggler for a run the stall sweep already
                        # gave up on, usually). Nothing to retry — there
                        # is no acknowledgement protocol to retry within.
                        logger.warning("souk rejected a frame: %s", frame)
                    else:
                        logger.warning("unexpected frame from souk, ignoring: %s", frame)
            finally:
                writer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await writer
                # This socket is gone, so nothing this provider produces
                # can reach souk until the reconnect. Stop the runs rather
                # than let them keep spending on an LLM whose output is
                # going nowhere; their finish frames queue on the
                # (connection-independent) outbound queue and go out on
                # the next socket, so souk still hears how they ended.
                for task in list(self._in_flight.values()):
                    task.cancel()

    def _dispatch(self, frame: dict[str, Any]) -> None:
        run_id = frame["runId"]
        task = asyncio.create_task(
            self._handle_run(run_id, frame.get("agentId", ""), frame.get("input") or {})
        )
        self._in_flight[run_id] = task
        task.add_done_callback(lambda _task, run_id=run_id: self._in_flight.pop(run_id, None))

    async def _write_loop(self, ws) -> None:
        # Single writer serializing all outbound frames onto the one
        # socket, since concurrent runs each queue writes here rather than
        # writing to it directly. A frame whose send raised is gone (the
        # socket is dead by then, and there is no acknowledgement to retry
        # against); anything still queued behind it survives, in order,
        # and goes out on the next connection.
        while True:
            frame = await self._outbound.get()
            await ws.send(json.dumps(frame))

    def run_stream(self, agent_id: str, run_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """The provider port itself: which of this identity's agents a run is
        for, and its input. Routes to that agent's own `run_stream`, which
        stays exactly the AG-UI shape (`run_input` in, events out) — the
        agent_id belongs to the provider, not to the agent.

        Raises KeyError for an agent_id this provider does not host; the
        caller (`_handle_run`) turns that into a warning and ends the run,
        rather than leaving souk holding one nobody will serve.
        """
        return self._handle_by_id[agent_id].run_stream(run_input)

    async def _handle_run(self, run_id: str, agent_id: str, run_input: dict[str, Any]) -> None:
        """One claimed run: feed the input to the agent, push every event
        back. The input arrived with the claim (the `run` frame carries
        it), so there is nothing to wait for here.
        """
        if agent_id not in self._handle_by_id:
            logger.warning("souk pushed a run for unknown local agent_id '%s'", agent_id)
            return

        outbound = self._outbound
        try:
            async for event in self.run_stream(agent_id, run_input):
                await outbound.put({"type": "event", "runId": run_id, "event": event})
        except asyncio.CancelledError:
            logger.info("run %s: stopped", run_id)
            raise
        except Exception:
            logger.exception("run %s for agent_id '%s' failed", run_id, agent_id)
        finally:
            # finish is the last word on this run — souk sends nothing
            # back for it, and decides the outcome from what it saw.
            #
            # put_nowait, not await put: this also runs while unwinding a
            # cancellation, and an await here would be interrupted before
            # the frame was ever queued — leaving souk holding a run whose
            # stream never ended, until its stall sweep noticed.
            outbound.put_nowait({"type": "finish", "runId": run_id})
