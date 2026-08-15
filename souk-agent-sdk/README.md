# souk-agent-sdk 🔌⚡

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)
[![Protocol: WebSocket / AG-UI / A2A](https://img.shields.io/badge/Protocols-WebSocket%20%7C%20AG--UI%20%7C%20A2A-blue.svg)](https://github.com/hukaichun/AgentSoukServer/blob/main/docs/server-mode.md)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](../LICENSE)

> **The Official Python Agent Provider SDK for Agent Souk.**  
> Effortlessly make any local, firewall-bound, or edge AI agent reachably exposed over **AG-UI** (human streaming) and **A2A** (agent-to-agent JSON-RPC) — **with zero inbound ports, no public IP, and no network configuration.**

This document is the pitch and the quick start. For the situations that
come up once your agent is actually running — multi-agent delegation
topologies (verified against a real LLM), session continuity, why
cancellation doesn't always work, and a few other things worth knowing
before they surprise you — see
**[docs/agent-provider-guide.md](../docs/agent-provider-guide.md)**.

---

## 💡 Key Concept: Your Agent Already Qualifies

`souk-agent-sdk` provides an outbound-only communication harness around your existing agent code. You don't need to rewrite your agent or adopt a proprietary framework.

If your agent already emits AG-UI-compatible event streams (or can format JSON event dicts), plugging it into Souk requires **writing just one streaming generator function**:

```python
RunStream = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]
```

The SDK handles all background network complexities: **Ed25519 keypair identity, the outbound WebSocket work channel, backpressure, reconnection, thread state, and cancellation.**

```
┌───────────────────────────────────────────────┐
│              Souk Gateway Server              │
└───────────────────────▲───────────────────────┘
                        │ Outbound WebSocket (ws/wss)
┌───────────────────────┴───────────────────────┐
│            souk_agent_sdk Client              │
│  - Ed25519 Identity & HMAC Token Refresh      │
│  - Work Claiming & Events over One Socket     │
│  - Task Concurrency Throttling & Cancel Race  │
├───────────────────────────────────────────────┤
│            Your Agent Logic (run_stream)      │
│  (Pydantic-AI / LangGraph / Custom LLM Loop)  │
└───────────────────────────────────────────────┘
```

---

## ⚡ 30-Second Quick Start

### 1. Installation

Not published to PyPI — this lives in the AgentSouk monorepo and is meant
to be depended on as a local path (see `providers/pydantic-ai-agent` or
`agent-template` for real examples):

```toml
# your pyproject.toml
[project]
dependencies = ["souk-agent-sdk"]

[tool.uv.sources]
souk-agent-sdk = { path = "../path/to/AgentSouk/souk-agent-sdk" }
```

```bash
uv sync
```

### 2. Minimal Reference Provider

```python
import asyncio
from collections.abc import AsyncIterator
from typing import Any
from souk_agent_sdk import AgentHandle, SoukProvider

# 1. Define your agent stream handler (AG-UI event format)
async def my_agent_stream(run_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    thread_id = run_input.get("threadId", "")
    run_id = run_input.get("runId", "")
    
    # Emit RUN_STARTED
    yield {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}
    
    # Emit text content chunks
    msg_id = "msg_001"
    yield {"type": "TEXT_MESSAGE_START", "messageId": msg_id, "role": "assistant"}
    yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id, "delta": "Hello from my local agent!"}
    yield {"type": "TEXT_MESSAGE_END", "messageId": msg_id}
    
    # Emit RUN_FINISHED
    yield {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}

# 2. Attach handle & start persistent outbound runner
async def main():
    handle = AgentHandle(
        name="echo-agent",
        description="A minimal reference agent running locally",
        run_stream=my_agent_stream,
    )
    
    provider = SoukProvider(
        souk_http_url="http://localhost:8000",  # one URL is the whole address:
        # registration posts to it, and the work socket is the same listener
        # with the scheme swapped (http -> ws, https -> wss)
        agents=[handle],
        max_concurrent_runs=10,  # Throttling limit
    )
    
    await provider.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 🌟 Core SDK Capabilities

| Capability | What `souk-agent-sdk` Handles Automatically |
|---|---|
| 🔐 **Self-Sovereign Identity** | Automatically generates & manages persistent **Ed25519 keypair** (`souk_identity.key`). Signs registration payloads to guarantee cryptographic ownership of assigned `agent_id`. |
| 🔄 **Automatic Reconnection & Token Refresh** | Re-registers on disconnects and seamlessly refreshes HMAC session bearer tokens without dropping queued tasks or interrupting run loops. |
| ⚡ **One Socket, Both Directions** | A single outbound WebSocket (`/ws/provider` on the gateway's HTTP listener) carries claimed runs down and events/finish up, multiplexed by `runId` — frames per the gateway's [`docs/server-mode.md`](https://github.com/hukaichun/AgentSoukServer/blob/main/docs/server-mode.md). |
| ⛔ **Task Preemption & Cancellation** | On Souk's `cancel` frame, cancels that run's task — propagating `asyncio.CancelledError` into in-flight LLM/tool calls, not merely between yields. Souk *asks*; complying is this client's choice. |
| 🎛️ **Concurrency Throttling** | `max_concurrent_runs=N` prevents GPU/LLM rate-limit saturation by letting Souk queue surplus work server-side. |
| ⏸️ **Human-in-the-Loop (HITL)** | Intercepts AG-UI native `interrupt` outcomes to pause runs resumbably (`status='input-required'`). |
| 🔗 **A2A Delegation & Actor Chains** | `a2a_client.call_agent_streaming` simplifies sub-agent calls while signing multi-hop EdDSA JWT `ActorChain` provenance. |
| 🔑 **Keep-Your-Own-Key (KYOK)** *(experimental)* | `KyokSigningAuth` simplifies signature generation for caller-funded LLM completions over `/kyok/v1`. See `tests/test_kyok_auth.py` for its coverage; still in-memory only, no recovery if Souk or the provider restarts mid-relay. See [`docs/keep-your-own-key.md`](../docs/keep-your-own-key.md). |

---

## 🏛️ Connection & Lifecycle Architecture

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Your Agent (SDK)
    participant Souk as Souk Gateway
    participant Caller as HTTP / AG-UI / A2A Caller

    Note over Agent: Load/Create Ed25519 Keypair
    Agent->>Souk: POST /agents/register (Signed with Ed25519 key)
    Souk-->>Agent: Session Bearer Token + Assigned agent_ids

    Agent->>Souk: WS connect /ws/provider, then hello(token, agentIds, maxClaim)
    Souk-->>Agent: welcome

    loop One socket carries everything, multiplexed by runId
        Souk-->>Agent: run frame (runId, agentId, RunAgentInput) — claiming is the hand-over
        Agent->>Souk: event frames (AG-UI: RUN_STARTED, TEXT_..., RUN_FINISHED)
        Souk-->>Caller: SSE Stream / JSON-RPC updates
        Agent->>Souk: finish frame (that run's stream ended)
    end

    opt Souk asks a run to stop
        Souk-->>Agent: cancel frame (a request — complying is this client's choice)
    end

    Note over Agent: A dropped socket ends nothing: reconnect + hello,<br/>then keep reporting by runId
```

---

## 📜 Event Protocol Specification

Every `run_stream` generator must yield events adhering to AG-UI specifications:

### 1. Minimal Event Sequence
1. `RUN_STARTED`: `{"type": "RUN_STARTED", "threadId": "...", "runId": "..."}`
2. **Content Events**: Zero or more `TEXT_MESSAGE_START` ➔ `TEXT_MESSAGE_CONTENT` ➔ `TEXT_MESSAGE_END`.
3. **Terminal Event** (Exactly one):
   - **Success**: `{"type": "RUN_FINISHED", "threadId": "...", "runId": "..."}`
   - **Error**: `{"type": "RUN_ERROR", "message": "Failure explanation"}`
   - **Interrupt (HITL Pause)**: `{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [...]}}`

---

## 🤝 Advanced Delegation (Agent-to-Agent)

An agent can delegate sub-tasks to other agents registered on Souk using `a2a_client`:

```python
from souk_agent_sdk.a2a_client import call_agent_streaming
from souk_agent_sdk.identity import extend_actor_chain, new_actor_chain

# Delegate streaming task to sub-agent
async for update in call_agent_streaming(
    "http://localhost:8000/a2a/translator/rpc",
    "Bonjour",
    reference_task_ids=[current_run_id],  # Lineage tracking
    actor_chain=actor_chain,               # Multi-hop identity chain
):
    print("Sub-agent update:", update)     # each item is an A2A StreamResponse
```

---

## 🛠️ Security & Identity Rules

> [!IMPORTANT]
> **Identity Key Persistence**
> The provider's identity is defined by its **Ed25519 keypair** (`souk_identity.key`).
> - Re-registering with the same key maintains ownership of assigned `agent_ids`.
> - If `souk_identity.key` is lost, Souk will treat new registrations under the same display name as a *new, separate identity* and issue fresh `agent_id`s.
> - **Always back up `souk_identity.key` in production environments!**

---

## 📁 Reference Implementations

- **[`/agent-template`](../agent-template)**: The minimal reference implementation (no LLM required). Start here to build a custom provider.
- **[`providers/pydantic-ai-agent`](../providers/pydantic-ai-agent)**: Full-featured production provider using [Pydantic-AI](https://ai.pydantic.dev), MCP tools, sub-agent delegation, and KYOK support *(experimental — see above)*.

---

## 🤝 Contributing & License

For development setup and contribution guidelines, see [CONTRIBUTING.md](../CONTRIBUTING.md).

**License**: [Apache 2.0](../LICENSE)
