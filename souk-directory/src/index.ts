import { AgentRosterEntry, escapeHtml, fetchAgents, getSoukUrl, linkWithSouk, renderSoukBar } from "./app.js";

let allAgents: AgentRosterEntry[] = [];

function render(agents: AgentRosterEntry[]): void {
  const container = document.getElementById("agents")!;
  const soukUrl = getSoukUrl();
  if (agents.length === 0) {
    container.innerHTML = `<p class="empty-state">No agents match.</p>`;
    return;
  }
  container.innerHTML = agents
    .map((agent) => {
      const href = linkWithSouk(`agent.html?id=${encodeURIComponent(agent.agent_id)}`, soukUrl);
      const statusClass = agent.online ? "online" : "offline";
      const statusLabel = agent.online ? "online" : "offline";
      return `
        <a class="card" href="${href}">
          <div class="card-header">
            <span class="status-dot ${statusClass}" title="${statusLabel}"></span>
            <span class="card-name">${escapeHtml(agent.name)}</span>
          </div>
          <div class="card-meta">
            ${statusLabel} · agent_id ${escapeHtml(agent.agent_id)} · joined ${new Date(
        agent.joined_at
      ).toLocaleDateString()}
          </div>
          <div class="card-desc">${escapeHtml(agent.description || "(no description)")}</div>
        </a>
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
