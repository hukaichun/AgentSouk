"""Tools that let one of this provider's own agents (e.g. a "tour guide"
agent) query the souk it's registered on — what agents exist here, are
they online, what do they do.

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

Talks to souk purely through its already-public HTTP API (`GET /agents`),
the exact same surface `souk-directory` and any external caller use — no
privileged access, nothing souk needs to know about this provider for.
This is what keeps provider/souk independence intact: souk isn't even
aware this tool exists.
"""

from __future__ import annotations

import httpx
from pydantic_ai import Tool


def build_souk_tools(souk_http_url: str) -> list[Tool]:
    async def list_souk_agents(include_offline: bool = True) -> str:
        """List agents currently registered on this souk, with their
        online status and description — use this to answer "what agents
        are here" / "is X online" / "what does X do" instead of guessing.
        Set include_offline=False to only see agents that can actually be
        reached right now.
        """
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

    return [
        Tool(
            list_souk_agents,
            name="list_souk_agents",
            description="List every agent currently registered on this souk, with online status and description.",
        )
    ]
