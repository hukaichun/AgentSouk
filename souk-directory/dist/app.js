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
const SOUK_URL_KEY = "souk-directory:soukUrl";
const DEFAULT_SOUK_URL = "http://localhost:8000";
export function getSoukUrl() {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get("souk");
    if (fromQuery) {
        localStorage.setItem(SOUK_URL_KEY, fromQuery);
        return fromQuery.replace(/\/$/, "");
    }
    const stored = localStorage.getItem(SOUK_URL_KEY);
    return (stored || DEFAULT_SOUK_URL).replace(/\/$/, "");
}
export function setSoukUrl(url) {
    const clean = url.trim().replace(/\/$/, "");
    localStorage.setItem(SOUK_URL_KEY, clean);
    return clean;
}
export function linkWithSouk(path, soukUrl) {
    const url = new URL(path, window.location.href);
    url.searchParams.set("souk", soukUrl);
    return url.toString();
}
// Renders the "which souk am I browsing" bar present on every page — the
// souk URL is always resolved (falls back to DEFAULT_SOUK_URL), so
// `onChange` fires once immediately with that value and again whenever
// the user edits it.
export function renderSoukBar(containerEl, onChange) {
    const current = getSoukUrl();
    containerEl.innerHTML = `
    <form id="souk-bar-form" class="souk-bar">
      <label for="souk-bar-input">souk</label>
      <input id="souk-bar-input" type="text" placeholder="${DEFAULT_SOUK_URL}"
             value="${escapeHtml(current)}" />
      <button type="submit">Connect</button>
    </form>
  `;
    const form = containerEl.querySelector("#souk-bar-form");
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        const input = containerEl.querySelector("#souk-bar-input");
        const url = setSoukUrl(input.value);
        onChange(url);
    });
    return current;
}
export function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}
export async function fetchAgents(soukUrl) {
    const resp = await fetch(`${soukUrl}/agents`);
    if (!resp.ok) {
        throw new Error(`GET /agents failed: ${resp.status}`);
    }
    const body = await resp.json();
    return body.agents;
}
// Minimal SSE body parser for a POST'd EventSource-shaped stream (the
// browser's native EventSource can't POST, and souk's /agui/id/{agent_id}
// requires a POST body — see sse_starlette's `event: message\r\ndata: ...\r\n\r\n`
// framing on the souk side — sse_starlette uses CRLF, not bare LF, which
// is why the buffer is normalized below before framing on blank lines).
// Calls `onEvent` with the parsed JSON payload of each event as it arrives.
export async function streamSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done)
            break;
        buffer += decoder.decode(value, { stream: true });
        // Normalized on the whole accumulated buffer, not just the newly
        // decoded chunk — a \r\n pair can straddle two separate reads.
        buffer = buffer.replace(/\r\n/g, "\n");
        let sepIndex;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
            const rawEvent = buffer.slice(0, sepIndex);
            buffer = buffer.slice(sepIndex + 2);
            const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data:"));
            if (dataLine) {
                const payload = dataLine.slice(5).trim();
                try {
                    onEvent(JSON.parse(payload));
                }
                catch (err) {
                    console.error("souk-directory: failed to parse SSE payload", payload, err);
                }
            }
        }
    }
}
