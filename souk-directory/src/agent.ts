import DOMPurify from "dompurify";
import { marked } from "marked";
import { AgentRosterEntry, escapeHtml, fetchAgents, linkWithSouk, renderSoukBar, streamSse } from "./app.js";

interface ThreadTreeNode {
  thread_id: string;
  agent_id: string;
  children: ThreadTreeNode[];
}

marked.setOptions({ breaks: true, gfm: true });

// Agent-produced prose (assistant replies, sub-agent replies, reasoning
// text, tool results) is near-always markdown — the models are prompted
// (and generally trained) to format that way. Rendered through DOMPurify
// rather than trusted raw: this text ultimately originates from whatever
// model a possibly-untrusted third-party provider is running, so treat it
// like any other untrusted HTML source before it hits innerHTML.
function renderMarkdown(el: HTMLElement, raw: string): void {
  el.innerHTML = DOMPurify.sanitize(marked.parse(raw, { async: false }) as string);
  el.classList.add("markdown");
}

const params = new URLSearchParams(window.location.search);
const agentId = params.get("id");
let threadId: string | null = null;
let selfName = "assistant";
let currentAssistantEl: HTMLElement | null = null;
let currentAssistantRaw = "";
let typingEl: HTMLElement | null = null;
// agent_id -> display name, from the roster fetched once at page load —
// used to label the call-chain route with names instead of raw agent_ids.
const agentNameById = new Map<string, string>();
// Keyed by the sub-agent's A2A task id — one accumulating bubble per
// delegated call, so a streamed multi-hop response reads like the main
// assistant's own streaming text instead of one raw JSON blob per event.
const subAgentEls = new Map<string, HTMLElement>();
const subAgentRaw = new Map<string, string>();
// Keyed by toolCallId — one placeholder bubble per in-flight tool call,
// filled in with the tool's result once it comes back. Surfacing this is
// what keeps a multi-second tool round trip from reading as unexplained
// silence (see the souk<->provider latency work this pairs with).
const toolCallEls = new Map<string, HTMLElement>();
// Keyed by the reasoning message_id AG-UI's REASONING_* events share
// across REASONING_START/MESSAGE_CONTENT/ENCRYPTED_VALUE/END for one
// "thought". Not every model exposes readable reasoning text — some only
// ever send an opaque REASONING_ENCRYPTED_VALUE — so the bubble falls
// back to a plain "thinking" placeholder when no content ever arrives.
const reasoningEls = new Map<string, { el: HTMLElement; raw: string }>();

function appendEntry(tag: string, text: string, cssClass: string): HTMLElement {
  const log = document.getElementById("chat-log")!;
  const div = document.createElement("div");
  div.className = `entry ${cssClass}`;
  div.innerHTML = `<span class="tag">${escapeHtml(tag)}</span><div class="bubble"></div>`;
  const bubble = div.querySelector(".bubble") as HTMLElement;
  bubble.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

function showTyping(): void {
  if (typingEl) return;
  const bubble = appendEntry(selfName, "", "assistant");
  bubble.innerHTML = `<span class="typing"><span></span><span></span><span></span></span>`;
  typingEl = bubble.parentElement as HTMLElement;
}

function clearTyping(): void {
  typingEl?.remove();
  typingEl = null;
}

async function loadAgentInfo(soukUrl: string): Promise<AgentRosterEntry | null> {
  const backLink = document.getElementById("back-link") as HTMLAnchorElement;
  backLink.href = linkWithSouk("index.html", soukUrl);

  const agents = await fetchAgents(soukUrl);
  for (const a of agents) {
    agentNameById.set(a.agent_id, a.name);
  }
  const agent = agents.find((a) => a.agent_id === agentId);
  if (!agent) {
    document.getElementById("agent-title")!.textContent = "Agent not found";
    document.getElementById("agent-desc")!.textContent =
      "It may be offline past the staleness window, or de-listed.";
    (document.getElementById("chat-form") as HTMLElement).style.display = "none";
    return null;
  }
  selfName = agent.name;
  document.getElementById("agent-title")!.textContent = agent.name;
  document.getElementById("status-chip")!.innerHTML = `
    <span class="status-chip ${agent.online ? "online" : "offline"}">
      <span class="dot"></span>${agent.online ? "online" : "offline"}
    </span>
  `;
  document.getElementById("agent-id")!.textContent = agent.agent_id;
  document.getElementById("agent-desc")!.textContent = agent.description || "";
  if (!agent.online) {
    document.getElementById("offline-banner")!.innerHTML =
      `<div class="offline-banner">Currently offline — your message will wait ` +
      `briefly for it to come back, then fail if it doesn't.</div>`;
  }
  return agent;
}

function handleAguiEvent(event: any): void {
  if (event.type === "RUN_STARTED") {
    // The standard, in-band place to learn the resolved thread_id — souk
    // substitutes its own real one if the id sent above wasn't
    // recognized (see souk-no-forced-protocol-deviation); no custom
    // response header for this anymore.
    if (event.threadId) threadId = event.threadId;
    return;
  }
  if (event.type === "RUN_ERROR") {
    clearTyping();
    appendEntry("error", event.message || "The agent failed to respond.", "error");
    return;
  }
  if (event.type === "TEXT_MESSAGE_CONTENT" || event.type === "TEXT_MESSAGE_CHUNK") {
    const delta = event.delta || event.content || "";
    if (!delta) return;
    clearTyping();
    if (!currentAssistantEl) {
      currentAssistantEl = appendEntry(selfName, "", "assistant");
      currentAssistantRaw = "";
    }
    currentAssistantRaw += delta;
    renderMarkdown(currentAssistantEl, currentAssistantRaw);
    return;
  }
  if (event.type === "TOOL_CALL_START") {
    clearTyping();
    const el = appendEntry(`tool · ${event.toolCallName || "?"}`, "…", "tool");
    toolCallEls.set(event.toolCallId, el);
    return;
  }
  if (event.type === "TOOL_CALL_RESULT") {
    const el = toolCallEls.get(event.toolCallId);
    toolCallEls.delete(event.toolCallId);
    if (!el) return;
    const content = typeof event.content === "string" ? event.content : JSON.stringify(event.content);
    renderMarkdown(el, content || "(no result)");
    return;
  }
  if (event.type === "REASONING_START") {
    clearTyping();
    const el = appendEntry("thinking", "…", "reasoning");
    reasoningEls.set(event.messageId, { el, raw: "" });
    return;
  }
  if (event.type === "REASONING_MESSAGE_CONTENT") {
    const entry = reasoningEls.get(event.messageId);
    if (!entry) return;
    entry.raw += event.delta || "";
    renderMarkdown(entry.el, entry.raw);
    return;
  }
  if (event.type === "REASONING_ENCRYPTED_VALUE") {
    // entityId is the same message_id REASONING_START used — see
    // pydantic_ai.ui.ag_ui's _thinking_0_13.py. Some models never expose
    // readable reasoning text, only this opaque blob; if nothing rendered
    // yet, at least say a thought happened rather than leaving "…" stuck.
    const entry = reasoningEls.get(event.entityId);
    if (entry && !entry.raw) {
      entry.el.textContent = "(reasoning hidden by the model)";
    }
    return;
  }
  if (event.type === "REASONING_END") {
    reasoningEls.delete(event.messageId);
    return;
  }
  if (event.type === "RUN_FINISHED") {
    currentAssistantEl = null;
    currentAssistantRaw = "";
    toolCallEls.clear();
    reasoningEls.clear();
    clearTyping();
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
    clearTyping();
    const value = event.value || {};
    const subAgent = value.sub_agent || "sub-agent";
    // A2A v1.0 wraps every streamed item in a StreamResponse whose single
    // key says what it is, rather than a bare update carrying a
    // discriminator field.
    const statusUpdate = value.statusUpdate || {};
    const artifactUpdate = value.artifactUpdate || {};
    const taskId = statusUpdate.taskId || artifactUpdate.taskId || subAgent;
    const state = statusUpdate.status?.state;

    if (state === "TASK_STATE_COMPLETED" || state === "TASK_STATE_FAILED") {
      subAgentEls.delete(taskId);
      subAgentRaw.delete(taskId);
      return;
    }

    // `Part` is a oneof, so a text part is just `{text}` — nothing else to
    // match on.
    const textParts = (artifactUpdate.artifact?.parts || []).filter((p: any) => p.text);
    if (textParts.length === 0) {
      // A status-only tick (e.g. the initial "working") — show a
      // placeholder once so the delegation is visible before any text
      // streams in, but don't spam a bubble per tick.
      if (!subAgentEls.has(taskId)) {
        subAgentEls.set(taskId, appendEntry(subAgent, "…", "sub"));
      }
      return;
    }

    let el = subAgentEls.get(taskId);
    if (!el || !subAgentRaw.get(taskId)) {
      el = el || appendEntry(subAgent, "", "sub");
      subAgentEls.set(taskId, el);
      subAgentRaw.set(taskId, "");
    }
    for (const part of textParts) {
      subAgentRaw.set(taskId, (subAgentRaw.get(taskId) || "") + part.text);
    }
    renderMarkdown(el, subAgentRaw.get(taskId) || "");
  }
}

// Flattens the (usually linear) delegation tree into an ordered list of
// stops for the route strip. A real fan-out (one call spawning several
// sibling sub-calls) still renders as a single depth-first route rather
// than branching visually — every delegation chain seen in practice so
// far is linear, and a strip is the right shape for that; a true tree
// widget would be overkill for the common case it'd rarely serve.
function flattenRoute(node: ThreadTreeNode): string[] {
  const stops = [node.agent_id];
  for (const child of node.children) {
    stops.push(...flattenRoute(child));
  }
  return stops;
}

async function renderCallChain(soukUrl: string, forThreadId: string): Promise<void> {
  const container = document.getElementById("call-chain")!;
  try {
    const resp = await fetch(`${soukUrl}/threads/${encodeURIComponent(forThreadId)}/tree`);
    if (!resp.ok) return;
    const root: ThreadTreeNode = await resp.json();
    // Only worth showing once there's an actual delegation — a plain
    // agent with no sub-calls would otherwise render a pointless
    // single-stop route on every reply.
    if (root.children.length === 0) {
      container.innerHTML = "";
      container.classList.remove("has-chain");
      return;
    }
    const stops = flattenRoute(root);
    const stopHtml = stops
      .map((id, i) => {
        const name = escapeHtml(agentNameById.get(id) || id);
        const link = i > 0 ? `<span class="link"></span>` : "";
        return `${link}<span class="stop ${i === 0 ? "root" : ""}"><span class="dot"></span>${name}</span>`;
      })
      .join("");
    container.classList.add("has-chain");
    container.innerHTML = `<div class="chain-label">call chain for this reply</div><div class="route">${stopHtml}</div>`;
  } catch {
    // Best-effort UI sugar — a failed fetch here shouldn't disrupt the
    // chat itself, which already succeeded by the time this runs.
  }
}

async function sendMessage(soukUrl: string, text: string): Promise<void> {
  const sendBtn = document.getElementById("chat-send") as HTMLButtonElement;
  sendBtn.disabled = true;
  appendEntry("you", text, "user");
  currentAssistantEl = null;
  currentAssistantRaw = "";
  subAgentEls.clear();
  subAgentRaw.clear();
  toolCallEls.clear();
  reasoningEls.clear();
  document.getElementById("call-chain")!.innerHTML = "";
  document.getElementById("call-chain")!.classList.remove("has-chain");
  showTyping();

  // POST /threads is optional now (souk mints one automatically for an
  // unrecognized threadId — see souk-no-forced-protocol-deviation), but
  // the directory still calls it explicitly here so it can show the
  // thread_id before the first message is even sent. The message itself
  // still needs an `id` — real AG-UI's own Message schema requires one,
  // no default — but souk discards whatever value this is and assigns
  // its own real one regardless (see
  // repo.append_thread_messages), so any locally-unique placeholder
  // satisfies the schema without meaning anything beyond that.
  try {
    if (!threadId) {
      const created = await fetch(`${soukUrl}/threads/id/${encodeURIComponent(agentId!)}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      threadId = (await created.json()).thread_id;
    }

    const body: Record<string, unknown> = {
      threadId,
      runId: crypto.randomUUID(),
      state: null,
      messages: [{ id: crypto.randomUUID(), role: "user", content: text }],
      tools: [],
      context: [],
      forwardedProps: null,
    };

    const resp = await fetch(`${soukUrl}/agui/id/${encodeURIComponent(agentId!)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const contentType = resp.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      // Duplicate-call snapshot branch (an active run already exists on
      // this thread) — no new stream to read, just a state snapshot.
      // Its own top-level `thread_id` field is the resolved one (see
      // repo.get_thread_snapshot) — no header needed here either.
      const snapshot = await resp.json();
      threadId = snapshot.thread_id || threadId;
      clearTyping();
      appendEntry("system", "(already in flight — waiting for it to finish)", "system");
      return;
    }
    await streamSse(resp, handleAguiEvent);
    if (threadId) {
      await renderCallChain(soukUrl, threadId);
    }
  } catch (err) {
    clearTyping();
    const message = err instanceof Error ? err.message : String(err);
    appendEntry("error", `request failed: ${message}`, "error");
  } finally {
    clearTyping();
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
