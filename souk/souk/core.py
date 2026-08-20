from __future__ import annotations

import abc
import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from souk import repo
from souk.agui import build_run_agent_input
from souk.broker import (
    ConnectedProvider,
    FinishStream,
    RelayEvent,
    RunBroker,
    RunSnapshot,
)
from souk.changes import ChangeEvent, LlmRosterChanged, RosterChanged, RunStatusChanged
from souk.config import CoreSettings
from souk.db_schema import DEFAULT_DB_SCHEMA, EXPECTED_SCHEMA_REVISION, quoted_schema
from souk.errors import (
    AgentInUse,
    AgentNotFound,
    InvalidRegistration,
    LlmOfferingInUse,
    LlmProviderNotFound,
)
from souk.handlers import make_handlers
from souk.health import run_health_sweeps_forever
from souk.identity import (
    SoukIdentity,
    agent_deletion_signing_payload,
    SIGNATURE_FRESHNESS_WINDOW_SECONDS,
    is_timestamp_fresh,
    llm_deletion_signing_payload,
    llm_registration_signing_payload,
    provider_connect_signing_payload,
    registration_signing_payload,
    souk_connect_signing_payload,
    verify_signature,
)
from souk.ids import new_id
from souk.kyok import ConnectedLLMProvider, KyokRelay
from souk.models import AgentRecord, AgentRef, AgentSummary, LlmRef, LlmSummary, RunRecord

logger = logging.getLogger("souk.core")


@dataclass
class Registration:

    agents: dict[str, AgentRef]


@dataclass(frozen=True)
class Health:
    """Snapshot of database reachability, schema version, and dispatch state."""

    database: bool
    schema_revision: str | None
    expected_schema_revision: str
    background_running: bool
    dispatching: bool = False
    database_error: str | None = None

    @property
    def schema_current(self) -> bool:
        return self.schema_revision == self.expected_schema_revision

    @property
    def ready(self) -> bool:
        return self.database and self.schema_current and self.dispatching


@dataclass
class RunHandle:
    """Caller-facing reference to a run: its id, thread, and its event stream."""

    run_id: str
    thread_id: str
    is_live: bool
    _broker: RunBroker | None = None
    _events: AsyncIterator[Any] | None = None

    async def events(self) -> AsyncIterator[Any]:
        """Yield the run's AG-UI events; yields nothing if no stream was attached."""
        if self._events is None:
            return
        async for item in self._events:
            yield item

    def cancel(self) -> None:
        if self._broker is not None:
            self._broker.request_cancel(self.run_id)


def _complete_run_agent_input(thread_id: str, run_id: str, run_input: dict[str, Any]) -> dict[str, Any]:
    """Fill in a message id for any message that lacks one, then build an AG-UI run input."""
    messages = [
        m if m.get("id") else {**m, "id": new_id("msg")} for m in run_input.get("messages", [])
    ]
    return build_run_agent_input(
        thread_id,
        run_id,
        messages,
        state=run_input.get("state"),
        tools=run_input.get("tools"),
        context=run_input.get("context"),
        forwarded_props=run_input.get("forwardedProps"),
        resume=run_input.get("resume"),
    )


class _Roster(abc.ABC):
    """One live roster of served names, stated once for both vocabularies.

    The agent roster and the LLM-offering roster share these semantics:
    registration is signed and fresh, and re-registering a subset withdraws
    the omitted names from live serving; attaching requires prior
    registration, touches, and announces; detaching is a silent no-op when
    nothing is served. The steps live here because two hand-kept copies
    drifted twice — a member-by-member fix first, then a probe catching the
    withdraw step missing on the LLM side — and a copy of a base can't
    drop a step.
    """

    party: str
    served: str

    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    @abc.abstractmethod
    def signing_payload(self, names: list[str], timestamp: int) -> bytes: ...

    @abc.abstractmethod
    async def registered_names(self, session: AsyncSession, public_key: str) -> set[str]: ...

    @abc.abstractmethod
    async def touch(self, session: AsyncSession, public_key: str, names: list[str]) -> None: ...

    @abc.abstractmethod
    def ref(self, public_key: str, name: str) -> Any: ...

    @abc.abstractmethod
    def served_by(self, public_key: str) -> list[Any]: ...

    @abc.abstractmethod
    def write_live(self, mapping: dict[Any, Any]) -> None: ...

    @abc.abstractmethod
    def withdraw(self, refs: list[Any]) -> None: ...

    @abc.abstractmethod
    def not_found(self, message: str) -> Exception: ...

    @abc.abstractmethod
    def changed(self) -> ChangeEvent: ...

    async def register(
        self,
        public_key: str,
        signature: str,
        timestamp: int,
        names: list[str],
        store: Callable[[AsyncSession], Any],
    ) -> Any:
        """Verify the signed registration, run `store`, withdraw omitted live names, announce."""
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("registration timestamp too far from souk's clock")
        if not verify_signature(public_key, signature, self.signing_payload(names, timestamp)):
            raise InvalidRegistration(f"invalid {self.party} registration signature")
        async with self._souk.session() as session:
            registered = await store(session)
        withdrawn = [r for r in self.served_by(public_key) if r.name not in registered]
        if withdrawn:
            self.withdraw(withdrawn)
        self._souk._notify_change(self.changed())
        return registered

    async def attach(
        self,
        connection: Any,
        names: list[str],
        *,
        challenge: str | None = None,
        provider_nonce: str | None = None,
        proof: str | None = None,
    ) -> str | None:
        """Connect `connection` as the live server for its already-registered `names`.

        Attaching is where runs change hands, so it authenticates like
        everything else: `proof` is a signature over
        `provider_connect_signing_payload(this souk's public key, challenge,
        provider_nonce, names)` where `challenge` came from
        `Souk.issue_connect_challenge` — souk chose the freshness, so a
        recording is worthless, and the payload names this souk as the
        recipient, so a proof coaxed out by one souk cannot be relayed to
        attach at another. A connection that
        can sign (it exposes `sign_connect`, as the in-process links do) is
        challenged and verified automatically; in-process is not trusted
        either. A connection that offers a proof has it verified; one that
        offers none is rejected. There is deliberately no way to switch
        this off — one handshake everywhere is what lets a provider in any
        language implement it once against the published vectors.

        souk answers in kind: the return value is its own signature over
        `souk_connect_signing_payload(challenge, provider_nonce)` — the
        proof a provider checks against the souk key it pinned — or None
        if this souk has no identity configured and so cannot prove
        itself. A transport relays the answer to the far side; a
        connection exposing `confirm_connect` (as the in-process links do)
        is handed it before the attach is committed, so a provider that
        pins can refuse the wrong souk by raising there.
        """
        if not names:
            raise ValueError(
                f"{self.party} '{connection.public_key}' attached with no {self.served} — "
                "there would be nothing to serve"
            )
        signer = getattr(connection, "sign_connect", None)
        if proof is None and callable(signer):
            challenge = self._souk.issue_connect_challenge()
            provider_nonce = secrets.token_hex(16)
            proof = signer(
                self._souk.identity_public_key or "", challenge, provider_nonce, names
            )
        if proof is not None:
            if challenge is None or not self._souk._consume_connect_challenge(challenge):
                raise InvalidRegistration(
                    f"connect proof for {self.party} '{connection.public_key}' does not "
                    "answer a live challenge souk issued"
                )
            payload = provider_connect_signing_payload(
                self._souk.identity_public_key or "", challenge, provider_nonce or "", names
            )
            if not verify_signature(connection.public_key, proof, payload):
                raise InvalidRegistration(
                    f"invalid connect proof for {self.party} '{connection.public_key}'"
                )
        else:
            raise InvalidRegistration(
                f"{self.party} '{connection.public_key}' attached without a connect proof — "
                "sign the challenge from issue_connect_challenge, or expose sign_connect"
            )
        async with self._souk.session() as session:
            registered = await self.registered_names(session, connection.public_key)
        unknown = sorted(set(names) - registered)
        if unknown:
            raise self.not_found(
                f"{self.party} '{connection.public_key}' has not registered {unknown} — "
                "register before attaching, in-process or not"
            )
        answer = (
            self._souk.sign(souk_connect_signing_payload(challenge, provider_nonce or ""))
            if self._souk.identity is not None
            else None
        )
        confirm = getattr(connection, "confirm_connect", None)
        if callable(confirm):
            confirm(challenge, provider_nonce or "", answer)
        self.write_live({self.ref(connection.public_key, n): connection for n in names})
        async with self._souk.session() as session:
            await self.touch(session, connection.public_key, names)
            await session.commit()
        self._souk._notify_change(self.changed())
        return answer

    @abc.abstractmethod
    def live(self, ref: Any) -> Any: ...

    def detach(self, public_key: str, connection: Any = None) -> None:
        """Take everything served by `public_key` offline; a no-op (no change event) if nothing is.

        souk holds one connection per role: a re-attach under the same key
        replaces the old connection, and replicas are the provider's own
        concern behind its single connection. `connection` is how a caller
        cleaning up a *replaced* connection avoids taking the replacement
        down with it: when given, only names whose current connection is
        that object (by identity) are withdrawn.
        """
        attached = self.served_by(public_key)
        if connection is not None:
            attached = [r for r in attached if self.live(r) is connection]
        if not attached:
            return
        self.withdraw(attached)
        self._souk._notify_change(self.changed())


class _AgentRoster(_Roster):

    party = "provider"
    served = "agent names"

    def signing_payload(self, names: list[str], timestamp: int) -> bytes:
        return registration_signing_payload(names, timestamp)

    async def registered_names(self, session: AsyncSession, public_key: str) -> set[str]:
        return await repo.get_agent_names_for_provider(session, public_key)

    async def touch(self, session: AsyncSession, public_key: str, names: list[str]) -> None:
        await repo.touch_agents(session, public_key, names)

    def ref(self, public_key: str, name: str) -> AgentRef:
        return AgentRef(provider_key=public_key, name=name)

    def served_by(self, public_key: str) -> list[AgentRef]:
        return self._souk.broker.agents_served_by(public_key)

    def live(self, ref: AgentRef) -> Any:
        return self._souk.broker.serving(ref)

    def write_live(self, mapping: dict[Any, Any]) -> None:
        self._souk.broker.register_provider(mapping)

    def withdraw(self, refs: list[Any]) -> None:
        self._souk.broker.unregister_provider(refs)

    def not_found(self, message: str) -> Exception:
        return AgentNotFound(message)

    def changed(self) -> ChangeEvent:
        return RosterChanged()


class _LlmRoster(_Roster):

    party = "LLM provider"
    served = "model names"

    def signing_payload(self, names: list[str], timestamp: int) -> bytes:
        return llm_registration_signing_payload(names, timestamp)

    async def registered_names(self, session: AsyncSession, public_key: str) -> set[str]:
        return await repo.get_llm_names_for_key(session, public_key)

    async def touch(self, session: AsyncSession, public_key: str, names: list[str]) -> None:
        await repo.touch_llm_providers(session, public_key, names)

    def ref(self, public_key: str, name: str) -> LlmRef:
        return LlmRef(provider_key=public_key, name=name)

    def served_by(self, public_key: str) -> list[LlmRef]:
        return self._souk.kyok_relay.served_by(public_key)

    def live(self, ref: LlmRef) -> Any:
        return self._souk.kyok_relay.serving(ref)

    def write_live(self, mapping: dict[Any, Any]) -> None:
        self._souk.kyok_relay.attach(mapping)

    def withdraw(self, refs: list[Any]) -> None:
        self._souk.kyok_relay.withdraw(refs)

    def not_found(self, message: str) -> Exception:
        return LlmProviderNotFound(message)

    def changed(self) -> ChangeEvent:
        return LlmRosterChanged()


class Souk:
    """The network-free facade: agent/LLM-provider rosters, threads, runs, and dispatch."""

    def __init__(self, settings: CoreSettings | None = None, broker: RunBroker | None = None) -> None:
        self.settings = settings or CoreSettings()
        self.identity = (
            SoukIdentity.from_hex(self.settings.identity_private_key)
            if self.settings.identity_private_key
            else None
        )
        self.engine = _create_engine(self.settings)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.broker = broker or RunBroker(spawn=self.spawn)
        self.kyok_relay = KyokRelay()
        self.broker.add_forget_listener(self.kyok_relay.discard)
        self._agent_roster = _AgentRoster(self)
        self._llm_roster = _LlmRoster(self)
        self._connect_challenges: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._started = False
        self._change_subscribers: set[Callable[[ChangeEvent], None]] = set()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session


    @property
    def identity_public_key(self) -> str | None:
        return self.identity.public_key if self.identity is not None else None

    def issue_connect_challenge(self) -> str:
        """Mint a single-use nonce a connecting provider must sign over to attach.

        souk chose it, so a recorded proof is worthless; it expires with the
        signature freshness window and is consumed by the attach that answers
        it. A serving layer relays it to the far side of its transport before
        calling attach with the returned proof.
        """
        now = time.time()
        self._connect_challenges = {
            nonce: issued
            for nonce, issued in self._connect_challenges.items()
            if now - issued <= SIGNATURE_FRESHNESS_WINDOW_SECONDS
        }
        nonce = secrets.token_hex(16)
        self._connect_challenges[nonce] = now
        return nonce

    def _consume_connect_challenge(self, nonce: str) -> bool:
        issued = self._connect_challenges.pop(nonce, None)
        return issued is not None and time.time() - issued <= SIGNATURE_FRESHNESS_WINDOW_SECONDS

    def sign(self, payload: bytes) -> str:
        """Sign `payload` with this souk's identity key, or raise if none is configured."""
        if self.identity is None:
            raise RuntimeError(
                "this souk has no identity: set identity_private_key "
                "(SOUK_IDENTITY_PRIVATE_KEY) to a hex-encoded Ed25519 seed"
            )
        return self.identity.sign(payload)


    async def start(self) -> list[str]:
        """Run once: fail any run left queued/running from a prior process and start dispatch.

        A second call is a no-op that returns an empty list, so it cannot reap runs
        queued after the first call. Returns the ids of runs marked failed as orphaned.
        """
        if self._started:
            return []
        self._started = True
        async with self.session() as session:
            orphaned = await repo.fail_orphaned_runs(session)
            for paused in await repo.get_paused_runs(session):
                self.broker.hold_thread(paused["thread_id"], paused["run_id"])
        if orphaned:
            logger.warning(
                "start: marked %d run(s) failed — still queued/running from before this "
                "process, and souk's dispatch state does not survive a restart: %s",
                len(orphaned),
                orphaned,
            )
        self.broker.start()
        self.spawn(run_health_sweeps_forever(self), name="health-sweeps")
        return orphaned

    async def health(self, timeout: float = 2.0) -> Health:
        """Probe the database within `timeout` and report reachability, schema, and dispatch state."""
        revision: str | None = None
        reachable = True
        error: str | None = None
        try:
            async with asyncio.timeout(timeout):
                async with self.session() as session:
                    await session.execute(text("SELECT 1"))
                    revision = await repo.get_schema_revision(session)
        except TimeoutError:
            reachable, error = False, "TimeoutError"
        except Exception as exc:
            reachable, error = False, type(exc).__name__

        return Health(
            database=reachable,
            schema_revision=revision,
            expected_schema_revision=EXPECTED_SCHEMA_REVISION,
            background_running=any(
                t.get_name() == "health-sweeps" and not t.done() for t in self._tasks
            ),
            dispatching=self.broker.is_running,
            database_error=error,
        )


    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Start `coro` as a tracked background task so `aclose` can cancel it later."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        """Stop dispatch, cancel every task spawned via `spawn`, and dispose the engine."""
        self.broker.stop()
        self._started = False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self.engine.dispose()


    async def register_agents(
        self,
        public_key: str,
        signature: str,
        timestamp: int,
        agents: list[dict[str, Any]],
        provider_name: str | None = None,
    ) -> Registration:
        """Verify the signed registration, store the agents, and withdraw any attached agent omitted from this batch.

        Withdrawal is from the broker only: an omitted agent stays registered
        in the database (see `repo.register_agents`) but goes offline until it
        is registered again, so re-register the full roster while attached.

        Raises `InvalidRegistration` if the timestamp is stale or the signature doesn't verify.
        """
        registered = await self._agent_roster.register(
            public_key,
            signature,
            timestamp,
            [a["name"] for a in agents],
            store=lambda session: repo.register_agents(
                session, public_key, agents, provider_name=provider_name
            ),
        )
        return Registration(agents=registered)

    async def delete_agent(
        self, public_key: str, name: str, signature: str, timestamp: int
    ) -> None:
        """Delete an agent record after verifying the signature and that it's safe to remove.

        Raises `AgentNotFound` if unregistered, or `AgentInUse` if a provider is currently
        serving it, it has active runs, or it has any thread/run history (which must be
        removed by taking the agent offline instead, not by deleting the record).
        """
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("deletion timestamp too far from souk's clock")
        if not verify_signature(
            public_key, signature, agent_deletion_signing_payload(name, timestamp)
        ):
            raise InvalidRegistration("invalid deletion signature")

        agent = AgentRef(provider_key=public_key, name=name)
        async with self.session() as session:
            record = await repo.get_agent(session, agent)
            if record is None:
                raise AgentNotFound(f"agent '{agent}' is not registered")
            if self.broker.serving(agent) is not None:
                raise AgentInUse(
                    f"agent '{agent}' has a provider serving it", reason="connected"
                )

            active = await repo.count_runs_for_agent(
                session, agent, repo.ACTIVE_RUN_STATUSES
            )
            if active:
                raise AgentInUse(
                    f"agent '{agent}' has {active} active run(s)", reason="active_run"
                )
            if await repo.count_threads_for_agent(session, agent) or await repo.count_runs_for_agent(
                session, agent
            ):
                raise AgentInUse(
                    f"agent '{agent}' has a conversation behind it and cannot be removed — "
                    "stop offering it instead, and it goes offline and off the roster with "
                    "its record intact",
                    reason="has_history",
                )

            await repo.delete_agent(session, agent)
        self._notify_change(RosterChanged())

    async def delete_llm_offering(
        self, public_key: str, name: str, signature: str, timestamp: int
    ) -> None:
        """Delete an LLM offering's record after verifying the signature and that it's safe to remove — the mirror of `delete_agent`.

        Raises `LlmProviderNotFound` if unregistered, or `LlmOfferingInUse` if a
        provider is currently serving it or a live run is bound to it. Offerings
        carry no conversation history, so there is no `has_history` refusal.
        """
        if not is_timestamp_fresh(timestamp):
            raise InvalidRegistration("deletion timestamp too far from souk's clock")
        if not verify_signature(
            public_key, signature, llm_deletion_signing_payload(name, timestamp)
        ):
            raise InvalidRegistration("invalid deletion signature")

        ref = LlmRef(provider_key=public_key, name=name)
        async with self.session() as session:
            record = await repo.get_llm_provider(session, ref)
            if record is None:
                raise LlmProviderNotFound(f"LLM offering '{ref}' is not registered")
            if self.kyok_relay.serving(ref) is not None:
                raise LlmOfferingInUse(
                    f"LLM offering '{ref}' has a provider serving it", reason="connected"
                )
            bound = self.kyok_relay.bound_runs(ref)
            if bound:
                raise LlmOfferingInUse(
                    f"LLM offering '{ref}' has {bound} live run(s) bound to it",
                    reason="active_run",
                )
            await repo.delete_llm_provider(session, ref)
        self._notify_change(LlmRosterChanged())

    def report_event(self, run_id: str, event: Any, *, claimed_by: str) -> bool:
        """Relay `event` into the run's stream if `claimed_by` holds the run (or can late-claim it).

        Returns False, without relaying, for an unknown run or one held by a different claimant.
        """
        run = self.broker.get(run_id)
        if run is None:
            return False
        if run.claimed_by is None:
            if not self.broker.accept_late_ack(run_id, claimed_by):
                logger.warning(
                    "report_event: '%s' reported for run %s, which nobody holds",
                    claimed_by,
                    run_id,
                )
                return False
        elif run.claimed_by != claimed_by:
            logger.warning(
                "report_event: '%s' reported for run %s, which is held by '%s'",
                claimed_by,
                run_id,
                run.claimed_by,
            )
            return False
        return self.broker.push(run_id, RelayEvent(event))

    def finish_run(self, run_id: str, *, claimed_by: str) -> bool:
        """End the run's stream if `claimed_by` currently holds it; False for an unknown or mismatched run."""
        run = self.broker.get(run_id)
        if run is None:
            return False
        if run.claimed_by != claimed_by:
            logger.warning(
                "finish_run: '%s' tried to end run %s, which is held by '%s'",
                claimed_by,
                run_id,
                run.claimed_by,
            )
            return False
        return self.broker.push(run_id, FinishStream())

    async def attach_provider(
        self,
        provider: ConnectedProvider,
        agent_names: list[str],
        *,
        challenge: str | None = None,
        provider_nonce: str | None = None,
        proof: str | None = None,
    ) -> str | None:
        """Connect `provider` as the live server for its already-registered `agent_names`.

        Raises `ValueError` for an empty list and `AgentNotFound` if any name was never
        registered under this provider's key — attaching does not implicitly register.
        A connection exposing `sign_connect` is challenged and verified automatically;
        a transport passes the `challenge` it relayed (from `issue_connect_challenge`),
        the provider's `provider_nonce`, and the returned `proof`, and relays the
        returned answer — souk's own signature for the provider to check against its
        pinned souk key. See `_Roster.attach`.
        """
        return await self._agent_roster.attach(
            provider, agent_names, challenge=challenge, provider_nonce=provider_nonce, proof=proof
        )

    async def register_llm_providers(
        self,
        public_key: str,
        signature: str,
        timestamp: int,
        names: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, LlmRef]:
        """Verify the signed registration and store `names` as LLM offerings for this key.

        An attached offering omitted from this batch is withdrawn from live
        serving (it stays registered in the database), same as agents.
        """
        return await self._llm_roster.register(
            public_key,
            signature,
            timestamp,
            names,
            store=lambda session: repo.register_llm_providers(
                session, public_key, names, metadata
            ),
        )

    async def attach_llm_provider(
        self,
        link: ConnectedLLMProvider,
        model_names: list[str],
        *,
        challenge: str | None = None,
        provider_nonce: str | None = None,
        proof: str | None = None,
    ) -> str | None:
        """Connect `link` as the live server for its already-registered `model_names`.

        Raises `ValueError` for an empty list and `LlmProviderNotFound` if any name was
        never registered under this key. Connect authentication — souk's answering
        signature included — works exactly as in `attach_provider`.
        """
        return await self._llm_roster.attach(
            link, model_names, challenge=challenge, provider_nonce=provider_nonce, proof=proof
        )

    def detach_llm_provider(self, public_key: str, connection: Any = None) -> None:
        """Remove every model offering served by `public_key`; a no-op (no change event) if none.

        Pass `connection` when cleaning up after a specific link (a closed
        socket): a replaced connection's cleanup then leaves its replacement
        serving. See `_Roster.detach`.
        """
        self._llm_roster.detach(public_key, connection)

    async def detach_provider(self, provider_public_key: str, connection: Any = None) -> None:
        """Take every agent served by `provider_public_key` offline; a no-op if it's serving nothing.

        Pass `connection` when cleaning up after a specific link (a closed
        socket): a replaced connection's cleanup then leaves its replacement
        serving. See `_Roster.detach`.
        """
        self._agent_roster.detach(provider_public_key, connection)


    def on_change(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        self._change_subscribers.add(callback)

        def unsubscribe() -> None:
            self._change_subscribers.discard(callback)

        return unsubscribe

    def _notify_change(self, event: ChangeEvent) -> None:
        for callback in list(self._change_subscribers):
            try:
                callback(event)
            except Exception:
                logger.exception("on_change subscriber raised for %r", event)

    async def mark_run_status(
        self, session: AsyncSession, run_id: str, status: str, metadata: dict[str, Any] | None = None
    ) -> None:
        await repo.mark_run_status(session, run_id, status, metadata=metadata)
        if status in ("completed", "failed", "cancelled"):
            self.broker.release_thread(run_id)
        self._notify_change(RunStatusChanged(run_id=run_id, status=status))

    async def list_agents(self) -> list[AgentSummary]:
        """List registered agents with `online` set to whether a provider is currently serving each."""
        async with self.session() as session:
            stored = await repo.list_agents(
                session,
                stale_hidden_window_seconds=self.settings.stale_hidden_window_seconds,
            )
        return [
            summary.model_copy(update={"online": self.is_serving(
                AgentRef(provider_key=summary.provider_key, name=summary.name)
            )})
            for summary in stored
        ]

    async def list_llm_providers(self) -> list[LlmSummary]:
        """List registered LLM offerings with `online` set to whether a provider is currently serving each — the mirror of `list_agents`."""
        async with self.session() as session:
            stored = await repo.list_llm_providers(
                session,
                stale_hidden_window_seconds=self.settings.stale_hidden_window_seconds,
            )
        return [
            summary.model_copy(update={"online": self.is_serving_llm(
                LlmRef(provider_key=summary.provider_key, name=summary.name)
            )})
            for summary in stored
        ]

    def is_serving(self, agent: AgentRef) -> bool:
        return self.broker.serving(agent) is not None

    def is_serving_llm(self, ref: LlmRef) -> bool:
        return self.kyok_relay.serving(ref) is not None

    async def get_agent(self, agent: AgentRef) -> AgentRecord | None:
        async with self.session() as session:
            return await repo.get_agent(session, agent)

    async def resolve_agent(self, provider: str, name: str) -> AgentRecord | None:
        async with self.session() as session:
            return await repo.resolve_agent(session, provider, name)


    async def create_thread(
        self, agent: AgentRef, parent_thread_id: str | None = None, metadata: dict | None = None
    ) -> str:
        async with self.session() as session:
            thread_id = await repo.create_thread(session, agent, parent_thread_id, metadata)
            await session.commit()
            return thread_id

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        async with self.session() as session:
            return await repo.get_thread(session, thread_id)

    async def get_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        async with self.session() as session:
            return await repo.get_thread_messages(session, thread_id)

    async def get_thread_snapshot(self, thread_id: str) -> dict[str, Any] | None:
        async with self.session() as session:
            return await repo.get_thread_snapshot(session, thread_id)

    async def get_thread_tree(self, thread_id: str) -> dict[str, Any] | None:
        """Return `thread_id` and its descendant threads nested as `children`, or None if it doesn't exist."""
        async with self.session() as session:
            root = await repo.get_thread(session, thread_id)
            if root is None:
                return None

            async def build(node_thread_id: str) -> list[dict[str, Any]]:
                children = await repo.get_thread_children(session, node_thread_id)
                return [
                    {**child, "children": await build(child["thread_id"])} for child in children
                ]

            return {
                "thread_id": thread_id,
                "provider_key": root["provider_key"],
                "agent_name": root["agent_name"],
                "children": await build(thread_id),
            }


    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self.session() as session:
            return await repo.get_run(session, run_id)

    async def get_run_events(self, run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        async with self.session() as session:
            return await repo.get_run_events(session, run_id, since_seq=since_seq)

    def active_runs(self) -> list[str]:
        return self.broker.active_run_ids()

    def enqueue_run(
        self,
        run_id: str,
        agent: AgentRef,
        thread_id: str,
        input_json: dict[str, Any],
        protocol: str,
        seq: int = 0,
    ) -> RunSnapshot:
        return self.broker.enqueue_run(
            run_id, agent, thread_id, input_json, protocol, make_handlers(self), seq=seq
        )

    async def start_run(
        self,
        agent: AgentRef,
        run_input: dict[str, Any],
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunHandle:
        """Create (or reuse) a thread, create a queued run on it, and enqueue it for dispatch.

        Returns a live `RunHandle` subscribed to the run's event stream.
        """
        async with self.session() as session:
            resolved_thread_id = await repo.ensure_thread(
                session, agent, thread_id, metadata=metadata, create_if_missing=True
            )
            created = await repo.create_run(
                session, resolved_thread_id, agent, "ag-ui", run_input, metadata
            )
            run_id = created["run_id"]

        self.enqueue_run(
            run_id,
            agent,
            resolved_thread_id,
            _complete_run_agent_input(resolved_thread_id, run_id, run_input),
            "ag-ui",
        )
        return RunHandle(
            run_id=run_id,
            thread_id=resolved_thread_id,
            is_live=True,
            _broker=self.broker,
            _events=self.broker.subscribe(run_id),
        )

    async def resume_run(self, run_id: str, run_input: dict[str, Any], metadata: dict | None = None) -> RunHandle:
        """Reopen an existing run under its same `run_id` and re-enqueue it with new input.

        Raises `LookupError` if `run_id` doesn't exist. The returned handle's event stream
        continues appending from the run's last stored event sequence.
        """
        async with self.session() as session:
            stored = await repo.get_run(session, run_id)
            if stored is None:
                raise LookupError(f"no such run: {run_id}")
            await repo.reopen_run(session, run_id, run_input, metadata)
            starting_seq = await repo.get_last_event_seq(session, run_id)

        self.enqueue_run(
            run_id,
            AgentRef(provider_key=stored.provider_key, name=stored.agent_name),
            stored.thread_id,
            _complete_run_agent_input(stored.thread_id, run_id, run_input),
            stored.protocol or "ag-ui",
            seq=starting_seq,
        )
        return RunHandle(
            run_id=run_id,
            thread_id=stored.thread_id,
            is_live=True,
            _broker=self.broker,
            _events=self.broker.subscribe(run_id),
        )

    def cancel_run(self, run_id: str) -> bool:
        return self.broker.request_cancel(run_id)


def _create_engine(settings: CoreSettings):
    is_sqlite = make_url(settings.database_url).get_backend_name() == "sqlite"

    connect_args = (
        {"options": f"-c search_path={quoted_schema(settings.db_schema)},public"}
        if not is_sqlite and settings.db_schema != DEFAULT_DB_SCHEMA
        else {}
    )

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=not is_sqlite,
        connect_args=connect_args,
    )

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine
