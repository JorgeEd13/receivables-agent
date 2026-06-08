import { useEffect, useRef, useState } from "react";
import { sendChat } from "./api.js";

const SUGGESTIONS = [
  "Which 5 customers have the most overdue money?",
  "What is our current DSO?",
  "Which overdue accounts should go on credit hold, and what's the rule?",
];

export default function App() {
  const [messages, setMessages] = useState([]); // {role, content, tools?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, busy]);

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
              <button key={s} className="chip" onClick={() => ask(s)}>
                {s}
              </button>
            ))}
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

        {busy && <div className="bubble assistant thinking">Thinking…</div>}
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
