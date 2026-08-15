"""Tools that let one of this provider's own agents (e.g. a "tour guide"
agent) query the souk it's registered on — what agents exist here, are
they online, what do they do, inspect agent cards, and get developer guides.

Deliberately plain pydantic-ai `Tool`s, not a real MCP server, even
though the framework already supports attaching one (`AgentConfig.
mcp_servers`, wired up in main.py via `MCPToolset(url)`): standing up a
separate MCP server process/protocol buys nothing here that a direct
function call doesn't already give the model, and this repo has no
existing MCP server to pattern-match against — building one prematurely
would be new protocol/process surface for a single provider's own tool.
If a second provider ever wants the exact same souk-introspection
capability, *that's* the point to extract this into a real, shared MCP
server (or a small package) — not before.

Talks to souk purely through its already-public HTTP API (`GET /agents`,
`GET /a2a/{name}/.well-known/agent.json`), the exact same surface
`souk-directory` and any external caller use — no privileged access,
nothing souk needs to know about this provider for.
This is what keeps provider/souk independence intact: souk isn't even
aware this tool exists.
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic_ai import Tool


DEV_GUIDES: dict[str, str] = {
    "architecture": """
### Agent Souk Architecture Overview
- **Relay Mechanism**: Outbound-only gRPC streams (`souk-agent-sdk`). Agents do not require public IPs, open ports, or ingress tunnels (ngrok).
- **Dual Gateway**: Exposes AG-UI (SSE for human interfaces) and A2A (JSON-RPC for agent-to-agent) on a single FastAPI gateway.
- **Identity**: Ed25519 cryptographic keypairs. No passwords, central DB, or registration signup flow.
- **Actor Chains**: Multi-hop EdDSA JWT chain verification for auditable caller provenance in multi-agent workflows.
- **Persistence**: ParadeDB / Postgres for durable threads, message history, and HITL async task pause/resume.
""",
    "how_to_connect": """
### How to Connect a New Agent to Souk
1. **Copy the Reference Template**: Start from `agent-template/` or use `souk-agent-sdk` in Python.
2. **Install SDK**: `pip install souk-agent-sdk` (or add as a dependency via `uv`).
3. **Write an AgentHandle**:
```python
from souk_agent_sdk import AgentHandle, SoukProvider

async def my_agent_run(run_input: dict):
    # Process messages from run_input
    yield {"type": "TEXT_DELTA", "delta": "Hello from my agent!"}

handle = AgentHandle(name="my-custom-agent", description="My agent description", run_stream=my_agent_run)
provider = SoukProvider(souk_http_url="http://localhost:8000", handles=[handle])
await provider.run_forever()
```
4. **Run Docker or Host Process**: Launch your agent, and it will register automatically via Ed25519 identity key!
""",
    "identity": """
### Provider & Caller Identity Model
- **Ed25519 Keypair**: Created on first launch (default `souk_identity.key`). Back it up like any credential.
- **Agent Ownership**: Scoped to `(public_key, name)`. Re-registering with the same key maintains `agent_id`.
- **Actor Chain**: When delegating across agents, each hop appends an EdDSA JWT signed by that agent's private key, bound to `prevHash` of the previous token.
""",
    "quickstart": """
### Quickstart with Docker Compose
Run the entire stack in 1 minute:
```bash
cp .env.example .env
docker compose up --build
```
Then open `http://localhost:8080` for the Web Directory & Live Chat UI!
"""
}


def build_souk_tools(souk_http_url: str) -> list[Tool]:
    async def list_souk_agents(include_offline: bool = True) -> str:
        """List agents currently registered on this souk, with their
        online status and description — use this to answer "what agents
        are here" / "is X online" / "what does X do" instead of guessing.
        Set include_offline=False to only see agents that can actually be
        reached right now.
        """
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{souk_http_url}/agents")
                resp.raise_for_status()
            agents = resp.json()["agents"]
            if not include_offline:
                agents = [a for a in agents if a["online"]]
            if not agents:
                return "No agents are currently registered on this souk."
            lines = []
            for agent in agents:
                status = "online" if agent["online"] else "offline"
                description = agent["description"] or "(no description)"
                lines.append(f"- {agent['name']} ({status}, agent_id={agent['agent_id']}): {description}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing souk agents: {e}"

    async def get_agent_card(agent_name_or_id: str) -> str:
        """Fetch the detailed Agent Card for a specific agent by name or agent_id.
        Use this when a user asks about specific capabilities, parameters, skills,
        or schema details of an agent on this souk.
        """
        try:
            async with httpx.AsyncClient() as client:
                url = f"{souk_http_url}/a2a/{agent_name_or_id}/.well-known/agent.json"
                resp = await client.get(url)
                if resp.status_code == 404:
                    return f"Agent '{agent_name_or_id}' was not found on this souk."
                resp.raise_for_status()
                card = resp.json()
            
            name = card.get("name", agent_name_or_id)
            desc = card.get("description", "No description provided.")
            version = card.get("version", "1.0.0")
            skills = card.get("skills", [])
            skills_str = ", ".join([s.get("name", "unnamed") for s in skills]) if isinstance(skills, list) and skills else "None listed"
            auth = card.get("authentication", {})
            
            output = [
                f"### Agent Card: {name} (v{version})",
                f"**Description**: {desc}",
                f"**Skills**: {skills_str}",
                f"**Authentication**: {json.dumps(auth)}",
                f"**Full Card JSON**:\n```json\n{json.dumps(card, indent=2)}\n```"
            ]
            return "\n".join(output)
        except Exception as e:
            return f"Error fetching agent card for '{agent_name_or_id}': {e}"

    async def get_souk_stats(category: Literal["all", "online", "offline"] = "all") -> str:
        """Get real-time operational statistics and health status of this souk gateway.

        Args:
            category: Filter category. Default is 'all'.
        """
        try:
            async with httpx.AsyncClient() as client:
                agents_resp = await client.get(f"{souk_http_url}/agents")
                agents_resp.raise_for_status()
                agents_data = agents_resp.json().get("agents", [])
                
            total_agents = len(agents_data)
            online_agents = sum(1 for a in agents_data if a.get("online"))
            offline_agents = total_agents - online_agents
            
            lines = [
                "### Souk Platform Real-time Status",
                f"- **Gateway Connection**: Connected ({souk_http_url})",
                f"- **Total Registered Agents**: {total_agents}",
                f"- **Online Agents**: {online_agents}",
                f"- **Offline Agents**: {offline_agents}",
            ]
            if online_agents > 0 and str(category).lower() in ("all", "online"):
                online_names = ", ".join([a["name"] for a in agents_data if a.get("online")])
                lines.append(f"- **Currently Online**: {online_names}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching souk statistics: {e}"

    async def get_souk_developer_guide(topic: str = "quickstart") -> str:
        """Get developer onboarding guides and documentation for Agent Souk.
        Allowed topics: 'quickstart', 'architecture', 'how_to_connect', 'identity'.
        Use this when users or developers ask how Souk works or how to build and connect their own agents.
        """
        try:
            topic_clean = str(topic).strip().lower()
            if topic_clean in DEV_GUIDES:
                return DEV_GUIDES[topic_clean]
            
            available = ", ".join(list(DEV_GUIDES.keys()))
            return f"Unknown topic '{topic}'. Available topics: {available}\n\n" + DEV_GUIDES["quickstart"]
        except Exception as e:
            return f"Error retrieving developer guide: {e}"

    return [
        Tool(
            list_souk_agents,
            name="list_souk_agents",
            description="List every agent currently registered on this souk, with online status and description.",
            max_retries=3,
        ),
        Tool(
            get_agent_card,
            name="get_agent_card",
            description="Fetch the detailed Agent Card (capabilities, skills, schemas) for a specific agent on this souk.",
            max_retries=3,
        ),
        Tool(
            get_souk_stats,
            name="get_souk_stats",
            description="Get real-time operational statistics and health status of this souk gateway.",
            max_retries=3,
        ),
        Tool(
            get_souk_developer_guide,
            name="get_souk_developer_guide",
            description="Get developer onboarding guides and documentation for Agent Souk (topics: quickstart, architecture, how_to_connect, identity).",
            max_retries=3,
        ),
    ]
