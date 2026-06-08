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
