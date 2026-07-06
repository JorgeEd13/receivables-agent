// Lightweight i18n — a string dictionary + a tiny hook, no framework (two locales
// don't warrant one). Covers the UI SHELL only. Honesty boundary (ADR-015): the
// agent's answers come from the model + the English policy corpus and are NOT
// translated on the fly; localizing the chrome must not imply localized answers,
// so `i18nNote` states that explicitly, and the seeded example QUESTIONS stay in
// English by design — they are plan-cache keys (a 0.90-similarity cache; a PT-BR
// paraphrase would miss and fall to the slow tiny model). The language toggle
// changes chrome, not what question text is sent to the agent.

export const LOCALES = ["en", "pt-BR"];

const STRINGS = {
  en: {
    langName: "EN",
    title: "receivables-agent",
    intro:
      "Ask about overdue invoices, aging, DSO and the collections policy. The agent answers from a synthetic ledger (guarded SQL) and a policy doc (RAG).",
    suggestionsHint:
      "These answer instantly (pre-cached plans, re-run live). Typing your own runs a tiny model on a free CPU — it works, just give it a moment.",
    instantLabel: "Instant:",
    placeholder: "Ask about the receivables ledger…",
    send: "Send",
    thinking: "Thinking…",
    replaying: "Replaying a cached plan…",
    slowHint:
      "Running a tiny model on a free CPU — this can take up to a minute. It’s working, not stuck.",
    noAnswer: "The agent did not return an answer.",
    themeToLight: "☀ Light",
    themeToDark: "☾ Dark",
    i18nNote:
      "The interface is available in English and Portuguese. The agent itself answers in English — it reads a synthetic ledger and an English policy document, so replies are not machine-translated.",
    // Human "step" fallbacks (server usually streams full narration text).
    query_ledger: "Querying the ledger",
    search_policy: "Reading the collections policy",
  },
  "pt-BR": {
    langName: "PT",
    title: "receivables-agent",
    intro:
      "Pergunte sobre faturas vencidas, aging, DSO e a política de cobrança. O agente responde a partir de um razão sintético (SQL protegido) e de um documento de política (RAG).",
    suggestionsHint:
      "Estas respondem na hora (planos pré-cacheados, reexecutados ao vivo). Digitar a sua própria pergunta roda um modelo minúsculo em CPU gratuita — funciona, só leva um instante.",
    instantLabel: "Na hora:",
    placeholder: "Pergunte sobre o razão de recebíveis…",
    send: "Enviar",
    thinking: "Pensando…",
    replaying: "Reexecutando um plano em cache…",
    slowHint:
      "Rodando um modelo minúsculo em CPU gratuita — isso pode levar até um minuto. Está funcionando, não travou.",
    noAnswer: "O agente não retornou uma resposta.",
    themeToLight: "☀ Claro",
    themeToDark: "☾ Escuro",
    i18nNote:
      "A interface está disponível em inglês e português. O agente responde em inglês — ele lê um razão sintético e um documento de política em inglês, então as respostas não são traduzidas automaticamente.",
    query_ledger: "Consultando o razão",
    search_policy: "Lendo a política de cobrança",
  },
};

const STORE_KEY = "receivables-agent.lang";

// Initial locale: a persisted manual choice wins; else the browser language
// (pt* → PT-BR, everyone else EN), matching the theme's prefers-color-scheme idea.
export function initialLocale() {
  try {
    const saved = localStorage.getItem(STORE_KEY);
    if (saved && LOCALES.includes(saved)) return saved;
  } catch {
    /* localStorage may be unavailable (private mode) — fall through to the browser */
  }
  const nav = (typeof navigator !== "undefined" && navigator.language) || "en";
  return nav.toLowerCase().startsWith("pt") ? "pt-BR" : "en";
}

export function persistLocale(locale) {
  try {
    localStorage.setItem(STORE_KEY, locale);
  } catch {
    /* non-fatal: the choice just won't survive a reload */
  }
}

// A translator bound to a locale; unknown keys fall back to EN then to the key
// itself, so a missing string is visible, never a crash.
export function translator(locale) {
  const dict = STRINGS[locale] || STRINGS.en;
  return (key) => dict[key] ?? STRINGS.en[key] ?? key;
}
