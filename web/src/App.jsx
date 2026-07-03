import { useEffect, useRef, useState } from "react";
import { sendChat } from "./api.js";

// These MUST mirror the plan-cache seed set (data/seed_plan_cache.py) so every
// one-click suggestion is a cache hit that replays in ~3s — instant on the free
// CPU Space, instead of a multi-minute tiny-model cold path. Reword either list
// and you reintroduce a slow first impression (the cache uses a 0.90 similarity
// threshold, so a paraphrase misses). Keep the two in lockstep.
const SUGGESTIONS = [
  "Who are the top 10 customers by overdue balance?",
  "What is our current DSO?",
  "Show me the total overdue amount by aging bucket.",
  "What does our policy say about credit holds?",
];

export default function App() {
  const [messages, setMessages] = useState([]); // {role, content, tools?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Whether the in-flight question is a one-click suggestion. Suggestions are
  // plan-cache hits (fast, ~3s); a typed question runs the live tiny model on a
  // free CPU and can take a while — the "Thinking…" copy reflects that.
  const [cachedAsk, setCachedAsk] = useState(false);
  const scrollRef = useRef(null);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, busy]);

  async function ask(text, fromSuggestion = false) {
    const question = text.trim();
    if (!question || busy) return;
    setError(null);
    setInput("");
    setCachedAsk(fromSuggestion || SUGGESTIONS.includes(question));

    // History sent to the API is the prior conversation (content only).
    const history = messages.map(({ role, content }) => ({ role, content }));
    const next = [...messages, { role: "user", content: question }];
    setMessages(next);
    setBusy(true);

    try {
      const { reply, tools_used } = await sendChat(question, history);
      setMessages([...next, { role: "assistant", content: reply, tools: tools_used }]);
    } catch (e) {
      setError(e.message);
      setMessages(next); // drop the optimistic turn's pending reply
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>receivables-agent</h1>
        <p>
          Ask about overdue invoices, aging, DSO and the collections policy. The
          agent answers from a synthetic ledger (guarded SQL) and a policy doc
          (RAG).
        </p>
      </header>

      <div className="chat" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="chip" onClick={() => ask(s, true)}>
                {s}
              </button>
            ))}
            <p className="suggestions-hint">
              These answer instantly (pre-cached plans, re-run live). Typing your
              own runs a tiny model on a free CPU — it works, just give it a moment.
            </p>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            <div className="content">{m.content}</div>
            {m.tools?.length > 0 && (
              <div className="tools">
                {m.tools.map((t) => (
                  <span key={t} className="tool-tag">
                    {t}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}

        {busy && (
          <div className="bubble assistant thinking">
            {cachedAsk
              ? "Thinking…"
              : "Thinking… (a typed question runs the tiny model on a free CPU — this can take up to a minute)"}
          </div>
        )}
        {error && <div className="error">{error}</div>}
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          ask(input);
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about the receivables ledger…"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
