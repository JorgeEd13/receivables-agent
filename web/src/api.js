// Thin client for the agent's /api/chat endpoint.
//
// The API key is baked at build time from VITE_API_KEY (defaults to the same
// "change-me" the server defaults to, so local dev works out of the box). On
// the public demo Space, set VITE_API_KEY and APP_API_KEY to the same secret.
const API_KEY = import.meta.env.VITE_API_KEY || "change-me";

export async function sendChat(message, history) {
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": API_KEY,
    },
    body: JSON.stringify({ message, history }),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`API ${resp.status}: ${detail}`);
  }
  return resp.json(); // { reply, tools_used }
}

// Streaming variant: POST to /api/chat/stream (SSE) and invoke `onEvent` for each
// progress event ({type:"cached"} | {type:"tool",name} | {type:"answer",reply,
// tools_used} | {type:"error",message}). Resolves when the stream ends. Lets the
// UI show the agent *thinking* — important on the free-CPU Space where a tiny
// model is slow.
export async function streamChat(message, history, onEvent) {
  const resp = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
    body: JSON.stringify({ message, history }),
  });
  if (!resp.ok || !resp.body) {
    const detail = resp.body ? await resp.text() : "";
    throw new Error(`API ${resp.status}: ${detail}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; process complete frames.
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const payload = line.slice(6);
      if (payload === "[DONE]") return;
      try {
        onEvent(JSON.parse(payload));
      } catch {
        /* ignore a malformed frame rather than break the stream */
      }
    }
  }
}
