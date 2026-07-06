import { useEffect, useRef, useState } from "react";
import { streamChat } from "./api.js";
import { initialLocale, persistLocale, translator } from "./i18n.js";
import { applyTheme, initialTheme } from "./theme.js";

// These MUST mirror the plan-cache seed set (data/seed_plan_cache.py) so every
// one-click suggestion is a cache hit that replays in ~3s — instant on the free
// CPU Space, instead of a multi-minute tiny-model cold path. Reword either list
// and you reintroduce a slow first impression (the cache uses a 0.90 similarity
// threshold, so a paraphrase misses). Keep the two in lockstep.
// They stay ENGLISH in every UI locale on purpose: they are cache keys AND the
// text sent verbatim to the (English-corpus) agent — i18n localizes the chrome
// around them, not the question the agent receives (ADR-015 honesty boundary).
const SUGGESTIONS = [
  "Who are the top 10 customers by overdue balance?",
  "What is our current DSO?",
  "Show me the total overdue amount by aging bucket.",
  "Top 5 customers by overdue balance in each aging bucket",
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
  const [theme, setTheme] = useState(initialTheme);
  const [locale, setLocale] = useState(initialLocale);
  const scrollRef = useRef(null);
  const t = translator(locale);

  // Stamp the theme attribute on <html> (and persist) whenever it changes.
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Persist the language choice + reflect it on <html lang> for a11y/SEO.
  useEffect(() => {
    persistLocale(locale);
    document.documentElement.setAttribute("lang", locale);
  }, [locale]);

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
      if (!answered) throw new Error(t("noAnswer"));
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
        <div>
          <h1>{t("title")}</h1>
          <p>{t("intro")}</p>
        </div>
        <div className="controls">
          <button
            className="toggle"
            onClick={() => setTheme((th) => (th === "dark" ? "light" : "dark"))}
            title={theme === "dark" ? t("themeToLight") : t("themeToDark")}
            aria-label={theme === "dark" ? t("themeToLight") : t("themeToDark")}
          >
            {theme === "dark" ? t("themeToLight") : t("themeToDark")}
          </button>
          <button
            className="toggle"
            onClick={() => setLocale((l) => (l === "en" ? "pt-BR" : "en"))}
            title="EN / PT"
            aria-label="Switch language"
          >
            {locale === "en" ? "PT" : "EN"}
          </button>
        </div>
      </header>

      <div className="chat" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="chip" onClick={() => ask(s)}>
                {s}
              </button>
            ))}
            <p className="suggestions-hint">{t("suggestionsHint")}</p>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            <div className="content">{m.content}</div>
            {m.tools?.length > 0 && (
              <div className="tools">
                {m.tools.map((tool) => (
                  <span key={tool} className="tool-tag">
                    {tool}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}

        {busy && progress && (
          <div className="bubble assistant thinking">
            <div className="progress-head">
              {progress.cached ? t("replaying") : t("thinking")}
              <span className="elapsed">{progress.elapsed}s</span>
            </div>
            {progress.steps.length > 0 && (
              <ul className="steps">
                {progress.steps.map((text, i) => (
                  <li key={i}>{t(text)}</li>
                ))}
              </ul>
            )}
            {!progress.cached && progress.elapsed >= 6 && (
              <div className="progress-hint">{t("slowHint")}</div>
            )}
          </div>
        )}
        {error && <div className="error">{error}</div>}
      </div>

      {/* Persistent one-click bar: the instant (cached) questions stay reachable
          after the first answer, not just on an empty chat. */}
      {messages.length > 0 && (
        <div className="quick-bar" aria-label="Instant example questions">
          <span className="quick-label">{t("instantLabel")}</span>
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

      {/* Honesty boundary (ADR-015): the interface is localized; the agent's
          answers are not machine-translated. */}
      <p className="i18n-note">{t("i18nNote")}</p>

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
          placeholder={t("placeholder")}
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          {t("send")}
        </button>
      </form>
    </div>
  );
}
