// Shared helpers for souk-directory — a pure static browser client of a
// souk's public HTTP API (GET /agents, POST /agui/id/{agent_id}, A2A agent
// cards). No backend of its own.
//
// The souk base URL is a runtime parameter, never baked into the build
// (see the project plan: this is the only hook worth building now for
// eventual multi-souk federation, without building any aggregation logic
// that doesn't exist yet) — read from ?souk=, remembered in localStorage,
// editable at any time from the top bar on every page. When neither is
// set, defaults to http://localhost:8000 — this repo's own
// docker-compose.yml maps souk to that port, so the common "just cloned
// this and ran docker compose up" case needs zero manual entry; the
// input box (still pre-filled and editable) is what makes anything else
// possible.

import { EventSourceParserStream } from "eventsource-parser/stream";

const SOUK_URL_KEY = "souk-directory:soukUrl";
const DEFAULT_SOUK_URL = "http://localhost:8000";

export interface AgentRosterEntry {
  agent_id: string;
  name: string;
  description: string;
  skills: unknown[];
  joined_at: string;
  last_seen_at: string;
  online: boolean;
  public_key: string;
  provider_name: string | null;
}

// Provider identity is purely the public_key (see souk/db.py's providers
// table docstring) — provider_name is an optional label on top of it, not
// a replacement. Groups a flat agent list into "who registered these."
export interface ProviderGroup {
  publicKey: string;
  providerName: string | null;
  agents: AgentRosterEntry[];
}

export function shortKey(publicKey: string): string {
  return publicKey.length <= 14 ? publicKey : `${publicKey.slice(0, 8)}…${publicKey.slice(-6)}`;
}

export function groupByProvider(agents: AgentRosterEntry[]): ProviderGroup[] {
  const groups = new Map<string, ProviderGroup>();
  for (const agent of agents) {
    let group = groups.get(agent.public_key);
    if (!group) {
      group = { publicKey: agent.public_key, providerName: agent.provider_name, agents: [] };
      groups.set(agent.public_key, group);
    }
    group.agents.push(agent);
  }
  // Named storefronts first (alphabetically), anonymous ones after,
  // ordered by key so the grouping is at least stable across reloads.
  return [...groups.values()].sort((a, b) => {
    if (!!a.providerName !== !!b.providerName) return a.providerName ? -1 : 1;
    const an = a.providerName || a.publicKey;
    const bn = b.providerName || b.publicKey;
    return an.localeCompare(bn);
  });
}

export function getSoukUrl(): string {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("souk");
  if (fromQuery) {
    localStorage.setItem(SOUK_URL_KEY, fromQuery);
    return fromQuery.replace(/\/$/, "");
  }
  const stored = localStorage.getItem(SOUK_URL_KEY);
  return (stored || DEFAULT_SOUK_URL).replace(/\/$/, "");
}

export function setSoukUrl(url: string): string {
  const clean = url.trim().replace(/\/$/, "");
  localStorage.setItem(SOUK_URL_KEY, clean);
  return clean;
}

export function linkWithSouk(path: string, soukUrl: string): string {
  const url = new URL(path, window.location.href);
  url.searchParams.set("souk", soukUrl);
  return url.toString();
}

// Renders the "which souk am I browsing" bar present on every page — the
// souk URL is always resolved (falls back to DEFAULT_SOUK_URL), so
// `onChange` fires once immediately with that value and again whenever
// the user edits it.
export function renderSoukBar(containerEl: HTMLElement, onChange: (soukUrl: string) => void): string {
  const current = getSoukUrl();
  containerEl.innerHTML = `
    <form id="souk-bar-form" class="souk-bar">
      <label for="souk-bar-input">souk</label>
      <input id="souk-bar-input" type="text" placeholder="${DEFAULT_SOUK_URL}"
             value="${escapeHtml(current)}" />
      <button type="submit">Connect</button>
    </form>
  `;
  const form = containerEl.querySelector<HTMLFormElement>("#souk-bar-form")!;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = containerEl.querySelector<HTMLInputElement>("#souk-bar-input")!;
    const url = setSoukUrl(input.value);
    onChange(url);
  });
  return current;
}

export function escapeHtml(str: string): string {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

export async function fetchAgents(soukUrl: string): Promise<AgentRosterEntry[]> {
  const resp = await fetch(`${soukUrl}/agents`);
  if (!resp.ok) {
    throw new Error(`GET /agents failed: ${resp.status}`);
  }
  const body: { agents: AgentRosterEntry[] } = await resp.json();
  return body.agents;
}

// Parses a POST'd EventSource-shaped stream (the browser's native
// EventSource can't POST, and souk's /agui/id/{agent_id} requires a POST
// body, so we still drive the fetch ourselves) via eventsource-parser
// instead of hand-rolling SSE framing — a hand-rolled version of this
// previously shipped broken (assumed bare `\n\n` between events; sse_
// starlette actually emits `\r\n\r\n`, so it silently parsed zero events
// off a perfectly well-formed stream). SSE framing has enough edge cases
// (CRLF vs LF, multi-line `data:` fields, comment lines, `id:`/`retry:`)
// that it isn't worth re-deriving by hand a second time.
// Calls `onEvent` with the parsed JSON payload of each event as it arrives.
export async function streamSse(response: Response, onEvent: (event: any) => void): Promise<void> {
  const stream = response
    .body!.pipeThrough(new TextDecoderStream())
    .pipeThrough(new EventSourceParserStream());
  for await (const event of stream) {
    try {
      onEvent(JSON.parse(event.data));
    } catch (err) {
      console.error("souk-directory: failed to parse SSE payload", event.data, err);
    }
  }
}
