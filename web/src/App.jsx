import { useEffect, useRef, useState } from "react";
import { streamChat } from "./api.js";

// Fallback labels if a bare tool name ever reaches the steps list; normally the
// server streams full "step" narration text (ADR-014, 8.5) that renders as-is.
const TOOL_LABELS = {
  query_ledger: "Querying the ledger",
  search_policy: "Reading the collections policy",
};

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
  // Live progress for the in-flight turn, streamed from /api/chat/stream:
  //   cached  — a plan-cache hit (fast); steps  — tool events as they happen;
  //   elapsed — seconds ticking so a slow tiny-model answer never looks frozen.
  const [progress, setProgress] = useState(null); // {cached, steps: [name], elapsed}
  const scrollRef = useRef(null);

  // Auto-scroll to the newest content — but ONLY if the user is already near the
  // bottom. If they've scrolled up to read while the agent is thinking, don't yank
  // them back down on every progress tick.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTo(0, el.scrollHeight);
  }, [messages, busy, progress]);

  // Tick the elapsed-seconds counter while a turn is in flight.
  useEffect(() => {
    if (!busy) return;
    const started = Date.now();
    const id = setInterval(
      () => setProgress((p) => (p ? { ...p, elapsed: Math.round((Date.now() - started) / 1000) } : p)),
      1000,
    );
    return () => clearInterval(id);
  }, [busy]);

  async function ask(text) {
    const question = text.trim();
    if (!question || busy) return;
    setError(null);
    setInput("");

    // History sent to the API is the prior conversation (content only).
    const history = messages.map(({ role, content }) => ({ role, content }));
    const next = [...messages, { role: "user", content: question }];
    setMessages(next);
    setBusy(true);
    setProgress({ cached: false, steps: [], elapsed: 0 });

    let answered = false;
    try {
      await streamChat(question, history, (ev) => {
        if (ev.type === "cached") {
          setProgress((p) => ({ ...p, cached: true }));
        } else if (ev.type === "tool") {
          // Kept for tool tags; the human "step" narration below drives the list.
        } else if (ev.type === "step") {
          // A live "what I'm doing / what I found" line (ADR-014, 8.5). Coalesce
          // repeats so a looping tiny model doesn't spam the same line.
          setProgress((p) =>
            p.steps[p.steps.length - 1] === ev.text
              ? p
              : { ...p, steps: [...p.steps, ev.text] }
          );
        } else if (ev.type === "answer") {
          answered = true;
          setMessages([...next, { role: "assistant", content: ev.reply, tools: ev.tools_used }]);
        } else if (ev.type === "error") {
          throw new Error(ev.message);
        }
      });
      if (!answered) throw new Error("The agent did not return an answer.");
    } catch (e) {
      setError(e.message);
      setMessages(next); // drop the optimistic turn's pending reply
    } finally {
      setBusy(false);
      setProgress(null);
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
              <button key={s} className="chip" onClick={() => ask(s)}>
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

        {busy && progress && (
          <div className="bubble assistant thinking">
            <div className="progress-head">
              {progress.cached ? "Replaying a cached plan…" : "Thinking…"}
              <span className="elapsed">{progress.elapsed}s</span>
            </div>
            {progress.steps.length > 0 && (
              <ul className="steps">
                {progress.steps.map((text, i) => (
                  <li key={i}>{TOOL_LABELS[text] || text}</li>
                ))}
              </ul>
            )}
            {!progress.cached && progress.elapsed >= 6 && (
              <div className="progress-hint">
                Running a tiny model on a free CPU — this can take up to a minute.
                It’s working, not stuck.
              </div>
            )}
          </div>
        )}
        {error && <div className="error">{error}</div>}
      </div>

      {/* Persistent one-click bar: the instant (cached) questions stay reachable
          after the first answer, not just on an empty chat. */}
      {messages.length > 0 && (
        <div className="quick-bar" aria-label="Instant example questions">
          <span className="quick-label">Instant:</span>
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              className="quick-chip"
              disabled={busy}
              title={s}
              onClick={() => ask(s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}

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
