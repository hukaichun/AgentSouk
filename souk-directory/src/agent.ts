import { AgentRosterEntry, escapeHtml, fetchAgents, linkWithSouk, renderSoukBar, streamSse } from "./app.js";

const params = new URLSearchParams(window.location.search);
const agentId = params.get("id");
let threadId: string | null = null;
let currentAssistantEl: HTMLElement | null = null;
// Keyed by the sub-agent's A2A task id — one accumulating bubble per
// delegated call, so a streamed multi-hop response reads like the main
// assistant's own streaming text instead of one raw JSON blob per event.
const subAgentEls = new Map<string, HTMLElement>();

function appendMessage(role: string, text: string, cssClass?: string): HTMLElement {
  const log = document.getElementById("chat-log")!;
  const div = document.createElement("div");
  div.className = `msg ${cssClass || ""}`.trim();
  div.innerHTML = `<div class="role">${escapeHtml(role)}</div><div class="text"></div>`;
  const textEl = div.querySelector(".text") as HTMLElement;
  textEl.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return textEl;
}

async function loadAgentInfo(soukUrl: string): Promise<AgentRosterEntry | null> {
  const backLink = document.getElementById("back-link") as HTMLAnchorElement;
  backLink.href = linkWithSouk("index.html", soukUrl);

  const agents = await fetchAgents(soukUrl);
  const agent = agents.find((a) => a.agent_id === agentId);
  if (!agent) {
    document.getElementById("agent-title")!.textContent = "Agent not found";
    document.getElementById("agent-meta")!.textContent =
      "It may be offline past the staleness window, or de-listed.";
    (document.getElementById("chat-form") as HTMLElement).style.display = "none";
    return null;
  }
  document.getElementById("agent-title")!.textContent = agent.name;
  document.getElementById("agent-meta")!.textContent =
    `${agent.online ? "online" : "offline"} · agent_id ${agent.agent_id}` +
    (agent.description ? ` · ${agent.description}` : "");
  if (!agent.online) {
    document.getElementById("offline-banner")!.innerHTML =
      `<div class="offline-banner">This agent is currently offline — your message will wait ` +
      `briefly for it to come back, then fail if it doesn't.</div>`;
  }
  return agent;
}

function handleAguiEvent(event: any): void {
  if (event.type === "RUN_ERROR") {
    appendMessage("error", event.message || "The agent failed to respond.", "error");
    return;
  }
  if (event.type === "TEXT_MESSAGE_CONTENT" || event.type === "TEXT_MESSAGE_CHUNK") {
    const delta = event.delta || event.content || "";
    if (!delta) return;
    if (!currentAssistantEl) {
      currentAssistantEl = appendMessage("assistant", "", "");
    }
    currentAssistantEl.textContent += delta;
    return;
  }
  if (event.type === "RUN_FINISHED") {
    currentAssistantEl = null;
    return;
  }
  if (event.type === "CUSTOM" && event.name === "sub_agent_progress") {
    // Surfaces multi-hop delegation live, when the provider forwards it
    // (see providers/pydantic-ai-agent's sub_agent_tool.py) — souk itself
    // doesn't guarantee this for every provider, see the project plan's
    // A9 notes.
    //
    // The raw payload is an A2A task update (status ticks, an internal
    // agui_event passthrough, then streamed artifact text parts) — most
    // of that is plumbing, not something a human should have to read.
    // Render one accumulating bubble per sub-agent call instead of a raw
    // JSON dump per event.
    const value = event.value || {};
    const subAgent = value.sub_agent || "sub-agent";
    const taskId = value.id || subAgent;
    const state = value.status?.state;

    if (state === "completed" || state === "failed") {
      subAgentEls.delete(taskId);
      return;
    }

    const textParts = (value.artifact?.parts || []).filter((p: any) => p.type === "text" && p.text);
    if (textParts.length === 0) {
      // A status-only tick (e.g. the initial "working") — show a
      // placeholder once so the delegation is visible before any text
      // streams in, but don't spam a bubble per tick.
      if (!subAgentEls.has(taskId)) {
        subAgentEls.set(taskId, appendMessage(`↳ ${subAgent}`, "…", "sub"));
      }
      return;
    }

    let el = subAgentEls.get(taskId);
    if (!el || el.textContent === "…") {
      el = el || appendMessage(`↳ ${subAgent}`, "", "sub");
      el.textContent = "";
      subAgentEls.set(taskId, el);
    }
    for (const part of textParts) {
      el.textContent += part.text;
    }
  }
}

async function sendMessage(soukUrl: string, text: string): Promise<void> {
  const sendBtn = document.getElementById("chat-send") as HTMLButtonElement;
  sendBtn.disabled = true;
  appendMessage("you", text, "");
  currentAssistantEl = null;
  subAgentEls.clear();

  // No client-generated `id` here — souk backfills a message id for any
  // AG-UI message that omits one (see souk.agui.fill_message_ids), the
  // same way it already does for A2A-sourced messages. souk is the one
  // party that actually needs ids in its own <category>_<hex> shape
  // (thread history, run events, ...); a plain browser caller shouldn't
  // have to reimplement that scheme just to satisfy AG-UI's schema.
  const body: Record<string, unknown> = {
    messages: [{ role: "user", content: text }],
  };
  if (threadId) {
    body.thread_id = threadId;
  }

  try {
    const resp = await fetch(`${soukUrl}/agui/id/${encodeURIComponent(agentId!)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    threadId = resp.headers.get("X-Souk-Thread-Id") || threadId;

    const contentType = resp.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      // Duplicate-call snapshot branch (an active run already exists on
      // this thread) — no new stream to read, just a state snapshot.
      appendMessage("system", "(already in flight — waiting for it to finish)", "");
      return;
    }
    await streamSse(resp, handleAguiEvent);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    appendMessage("error", `request failed: ${message}`, "error");
  } finally {
    sendBtn.disabled = false;
  }
}

async function init(soukUrl: string): Promise<void> {
  if (!agentId) {
    document.getElementById("agent-title")!.textContent = "No agent id given";
    return;
  }
  await loadAgentInfo(soukUrl);
  document.getElementById("chat-form")!.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = document.getElementById("chat-input") as HTMLInputElement;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendMessage(soukUrl, text);
  });
}

const initial = renderSoukBar(document.getElementById("souk-bar")!, init);
init(initial);
