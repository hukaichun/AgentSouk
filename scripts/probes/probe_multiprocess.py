"""What two funduq processes behind a load balancer actually do, today.

The baseline the horizontal-scaling work is measured against (see
design/broker-horizontal-scaling.md, kept in git history at d78d063 rather
than in the tree). Everything here is expected to *fail* on
current code — that is the point. Each scenario prints what happened and what
the design says should happen instead, so the same script becomes the pass/fail
check as each phase lands rather than being thrown away.

    python scripts/probes/probe_multiprocess.py
    FUNDUQ_DATABASE_URL=postgresql+psycopg://… python scripts/probes/probe_multiprocess.py

Two real OS processes, one database, and an LB that round-robins every call
(scripts/probes/cluster.py). An earlier version of this probe ran two `Funduq`
objects in one process, which shares an event loop and therefore proves less
than it appears to.
"""

from __future__ import annotations

import asyncio
import os
import time

from cluster import Cluster
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Only for building a real signed registration — the probe proves nothing if
# it takes a shortcut past the identity funduq checks on every claim.
from funduq.identity import registration_signing_payload

RUN_INPUT = {"messages": [{"id": "m1", "role": "user", "content": "hello"}], "state": {}}


class Findings:
    """Collects one line per scenario so the summary is the probe's own
    output, not something the reader assembles by scrolling."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def record(self, name: str, healthy: bool, detail: str) -> None:
        self.rows.append((name, healthy, detail))
        print(f"  {'OK  ' if healthy else 'BROKEN'} {name}: {detail}")

    def summarize(self) -> int:
        broken = [row for row in self.rows if not row[1]]
        print(f"\n{len(self.rows) - len(broken)}/{len(self.rows)} healthy, {len(broken)} broken")
        for name, _healthy, detail in broken:
            print(f"  BROKEN {name}: {detail}")
        return len(broken)


async def register(cluster: Cluster) -> tuple[str, str, str]:
    """A real signed registration, through whichever node the LB picks."""
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    signature = key.sign(registration_signing_payload(["prober"], timestamp)).hex()
    result = await cluster.call(
        {
            "op": "register",
            "public_key": public_key,
            "signature": signature,
            "timestamp": timestamp,
            "agents": [{"name": "prober", "description": "probe fixture"}],
        }
    )
    return public_key, result["agents"]["prober"], result["session_token"]


async def scenario_cross_node_claim(cluster: Cluster, findings: Findings) -> None:
    """A run enqueued on one node, claimed by a worker whose call lands on the
    other. The base case: with a load balancer in front, whether these are the
    same node is a coin toss.
    """
    print("\n[1] cross-node claim")
    public_key, agent, token = await register(cluster)
    run = await cluster.call({"op": "start_run", **agent, "run_input": RUN_INPUT}, node="a")

    claimed_b = await cluster.call(
        {"op": "claim_work", "token": token, "agent_names": [agent["agent_name"]], "max_claim": 5}, node="b"
    )
    findings.record(
        "a worker on B can claim a run enqueued on A",
        len(claimed_b) == 1,
        f"B claimed {len(claimed_b)} run(s); should be 1 (run {run['run_id'][:16]}… is queued in the database)",
    )

    claimed_a = await cluster.call(
        {"op": "claim_work", "token": token, "agent_names": [agent["agent_name"]], "max_claim": 5}, node="a"
    )
    print(f"       (A claims the same run: {len(claimed_a)} — it is only visible where it was enqueued)")
    return public_key, agent, token, run


async def scenario_new_replica_reaps(cluster: Cluster, findings: Findings) -> None:
    """A new replica boots while another node is mid-run — a rolling deploy,
    an autoscaler, a restarted container. Its startup reconciliation is
    database-wide, so it declares the live run failed; and the node actually
    running it never learns, so it keeps persisting events into a run the
    database says is over.

    The booting node must genuinely be fresh: `Funduq.start()` runs once per
    process by design, so calling it again on a node that already started is a
    no-op and proves nothing. (It quietly made an earlier version of this
    scenario pass.)
    """
    print("\n[2] a new replica boots mid-run")
    public_key, agent, token = await register(cluster)
    run = await cluster.call({"op": "start_run", **agent, "run_input": RUN_INPUT}, node="a")
    claimed = await cluster.call(
        {"op": "claim_work", "token": token, "agent_names": [agent["agent_name"]], "max_claim": 1}, node="a"
    )
    if not claimed:
        findings.record("a booting replica leaves live runs alone", False, "nothing claimed on A")
        return
    # Let A's pipeline record the claim, so this measures the reap and not the
    # claim/status window (see scenario 5, which measures that separately).
    await asyncio.sleep(0.3)

    cluster.spawn("c")
    reaped = await cluster.call({"op": "funduq_start"}, node="c")
    record = await cluster.call({"op": "get_run", "run_id": run["run_id"]}, node="c")
    still_dispatching = run["run_id"] in await cluster.call({"op": "active_runs"}, node="a")

    findings.record(
        "a booting replica leaves another node's live run alone",
        run["run_id"] not in reaped["reaped"],
        f"C reaped {len(reaped['reaped'])} run(s); the run now reads {record['status']!r} "
        f"({record['metadata'].get('failureReason')}) while A is still dispatching it: {still_dispatching}",
    )

    accepted = await cluster.call(
        {
            "op": "report_event",
            "run_id": run["run_id"],
            "event": {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "still here"},
            "claimed_by": public_key,
        },
        node="a",
    )
    await asyncio.sleep(0.4)
    events = await cluster.call({"op": "get_run_events", "run_id": run["run_id"]}, node="c")
    findings.record(
        "nothing lands in a run already declared failed",
        not (record["status"] == "failed" and accepted and events),
        f"the run reads {record['status']!r}, yet A accepted a further event ({accepted}) and "
        f"{len(events)} event(s) are persisted against it — a caller polling the run and one "
        "on its stream get contradictory accounts",
    )


async def scenario_report_to_wrong_node(cluster: Cluster, findings: Findings) -> None:
    """A worker reconnects and its next call lands on the other node. funduq
    answers False and nothing is persisted — and the worker, which pushed and
    moved on, never finds out.
    """
    print("\n[3] event reported to the node that does not hold the run")
    public_key, agent, token = await register(cluster)
    run = await cluster.call({"op": "start_run", **agent, "run_input": RUN_INPUT}, node="a")
    claimed = await cluster.call(
        {"op": "claim_work", "token": token, "agent_names": [agent["agent_name"]], "max_claim": 1}, node="a"
    )
    if not claimed:
        findings.record("an event reported to any node reaches the run", False, "nothing claimed on A")
        return

    accepted = await cluster.call(
        {
            "op": "report_event",
            "run_id": run["run_id"],
            "event": {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": "hi"},
            "claimed_by": public_key,
        },
        node="b",
    )
    await asyncio.sleep(0.3)
    events = await cluster.call({"op": "get_run_events", "run_id": run["run_id"]}, node="a")
    findings.record(
        "an event reported to any node reaches the run",
        bool(accepted) and len(events) > 0,
        f"B answered {accepted} and {len(events)} event(s) persisted — the worker is not told",
    )


async def scenario_cross_node_stream(cluster: Cluster, findings: Findings) -> None:
    """A caller streaming on the node that did not claim the run. This is the
    read half: even when everything else works, the consumer has to receive
    what a worker reported elsewhere.
    """
    print("\n[4] consumer on the node that does not own the run")
    public_key, agent, token = await register(cluster)
    run = await cluster.call({"op": "start_run", **agent, "run_input": RUN_INPUT}, node="a")
    claimed = await cluster.call(
        {"op": "claim_work", "token": token, "agent_names": [agent["agent_name"]], "max_claim": 1}, node="a"
    )
    if not claimed:
        findings.record("a consumer on B sees a run owned by A", False, "nothing claimed on A")
        return

    async def produce() -> None:
        await asyncio.sleep(0.2)
        for delta in ("one", "two"):
            await cluster.call(
                {
                    "op": "report_event",
                    "run_id": run["run_id"],
                    "event": {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": delta},
                    "claimed_by": public_key,
                },
                node="a",
            )
        await cluster.call(
            {"op": "finish_run", "run_id": run["run_id"], "claimed_by": public_key}, node="a"
        )

    on_b, _ = await asyncio.gather(cluster.subscribe(run["run_id"], node="b", timeout=3.0), produce())
    findings.record(
        "a consumer on B sees a run owned by A",
        len(on_b) >= 2,
        f"B's stream yielded {len(on_b)} event(s) for a run producing on A",
    )


async def scenario_owner_dies(cluster: Cluster, findings: Findings) -> None:
    """The node holding a run is SIGKILLed. Nobody finishes the run, and no
    surviving node has any way to tell "its owner is dead" from "its provider
    is quiet" — the only cleanup that keys off a node being gone is the *next*
    boot's reconciliation.

    Killed immediately after the claim on purpose: that also catches the
    window in which a run has been handed to a worker and the database has
    not yet been told (the status write is `_handle_claim`'s, on the run's
    pipeline task, after `claim_work` has already returned). What the row
    says at the moment of death is printed below.
    """
    print("\n[5] the owning node dies")
    _public_key, agent, token = await register(cluster)
    run = await cluster.call({"op": "start_run", **agent, "run_input": RUN_INPUT}, node="a")
    claimed = await cluster.call(
        {"op": "claim_work", "token": token, "agent_names": [agent["agent_name"]], "max_claim": 1}, node="a"
    )
    if not claimed:
        findings.record("a dead node's run reaches a verdict", False, "nothing claimed on A")
        return

    cluster.kill("a")
    at_death = await cluster.call({"op": "get_run", "run_id": run["run_id"]}, node="b")
    print(
        f"       (the row reads {at_death['status']!r} at the moment A dies — "
        "'queued' means the claim never reached the database)"
    )
    await cluster.call({"op": "sweep_once"}, node="b")
    record = await cluster.call({"op": "get_run", "run_id": run["run_id"]}, node="b")
    findings.record(
        "a dead node's run reaches a verdict promptly",
        record["status"] in ("failed", "cancelled"),
        f"A is gone; B's sweep leaves the run at {record['status']!r} — no sweep keys off "
        "a node being dead, only off the provider having gone quiet",
    )


async def main() -> int:
    findings = Findings()
    database_url = os.environ.get("FUNDUQ_DATABASE_URL")
    print(f"two funduq processes, one database ({(database_url or 'sqlite, throwaway file').split('://')[0]})")
    print("every call is round-robined unless a scenario names a node\n")

    async with Cluster(nodes=["a", "b"], database_url=database_url) as cluster:
        await cluster.call({"op": "funduq_start"}, node="a")
        await cluster.call({"op": "funduq_start"}, node="b")
        await scenario_cross_node_claim(cluster, findings)
        await scenario_new_replica_reaps(cluster, findings)
        await scenario_report_to_wrong_node(cluster, findings)
        await scenario_cross_node_stream(cluster, findings)
        await scenario_owner_dies(cluster, findings)

    return findings.summarize()


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(main()) else 0)
