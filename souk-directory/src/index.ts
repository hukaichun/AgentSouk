import {
  AgentRosterEntry,
  escapeHtml,
  fetchAgents,
  getSoukUrl,
  groupByProvider,
  linkWithSouk,
  renderSoukBar,
  shortKey,
} from "./app.js";

let allAgents: AgentRosterEntry[] = [];

function renderAgentCard(agent: AgentRosterEntry, soukUrl: string): string {
  const href = linkWithSouk(`agent.html?id=${encodeURIComponent(agent.agent_id)}`, soukUrl);
  const statusClass = agent.online ? "online" : "offline";
  const statusLabel = agent.online ? "online" : "offline";
  return `
    <a class="card" href="${href}">
      <div class="card-header">
        <span class="card-name">${escapeHtml(agent.name)}</span>
        <span class="status-chip ${statusClass}"><span class="dot"></span>${statusLabel}</span>
      </div>
      <div class="card-meta">
        agent_id ${escapeHtml(agent.agent_id)} · joined ${new Date(agent.joined_at).toLocaleDateString()}
      </div>
      <div class="card-desc">${escapeHtml(agent.description || "(no description)")}</div>
    </a>
  `;
}

function render(agents: AgentRosterEntry[]): void {
  const container = document.getElementById("agents")!;
  const soukUrl = getSoukUrl();
  if (agents.length === 0) {
    container.innerHTML = `<p class="empty-state">No agents match.</p>`;
    return;
  }
  container.innerHTML = groupByProvider(agents)
    .map((group) => {
      const onlineCount = group.agents.filter((a) => a.online).length;
      const title = group.providerName
        ? escapeHtml(group.providerName)
        : `<span class="stall-anon">unnamed provider</span>`;
      return `
        <section class="stall">
          <div class="stall-header">
            <span class="stall-name">${title}</span>
            <span class="stall-key" title="${escapeHtml(group.publicKey)}">${escapeHtml(
        shortKey(group.publicKey)
      )}</span>
            <span class="stall-count">${group.agents.length} agent${group.agents.length === 1 ? "" : "s"} · ${onlineCount} online</span>
          </div>
          <div class="stall-cards">
            ${group.agents.map((agent) => renderAgentCard(agent, soukUrl)).join("")}
          </div>
        </section>
      `;
    })
    .join("");
}

function applyFilter(): void {
  const searchInput = document.getElementById("search") as HTMLInputElement;
  const query = searchInput.value.trim().toLowerCase();
  const filtered = !query
    ? allAgents
    : allAgents.filter(
        (a) =>
          a.name.toLowerCase().includes(query) || (a.description || "").toLowerCase().includes(query)
      );
  render(filtered);
}

async function load(soukUrl: string): Promise<void> {
  const container = document.getElementById("agents")!;
  container.innerHTML = `<p class="empty-state">Loading…</p>`;
  try {
    allAgents = await fetchAgents(soukUrl);
    applyFilter();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    container.innerHTML = `<p class="empty-state">Couldn't reach ${escapeHtml(soukUrl)}: ${escapeHtml(
      message
    )}</p>`;
  }
}

const initial = renderSoukBar(document.getElementById("souk-bar")!, load);
document.getElementById("search")!.addEventListener("input", applyFilter);
load(initial);
