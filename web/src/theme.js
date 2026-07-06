// Theme state — light/dark, driven by prefers-color-scheme as the ambient signal
// and a manual, persisted override stamped as `data-theme` on <html> (styles.css
// makes the attribute win in both directions). No dependency.

const STORE_KEY = "receivables-agent.theme";

// The effective initial theme: a persisted manual choice wins; else follow the OS.
export function initialTheme() {
  try {
    const saved = localStorage.getItem(STORE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* localStorage unavailable — fall through to the OS preference */
  }
  const prefersDark =
    typeof matchMedia !== "undefined" &&
    matchMedia("(prefers-color-scheme: dark)").matches;
  return prefersDark ? "dark" : "light";
}

// Stamp the attribute so the CSS override applies, and persist the choice.
export function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(STORE_KEY, theme);
  } catch {
    /* non-fatal */
  }
}
