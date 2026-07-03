#!/usr/bin/env bash
# Hugging Face Space container entrypoint (Phase 6, Layer 3).
#
# The Space runs a **local** tiny model, so unlike the Gemini image this entrypoint
# has to stand up an inference server before serving:
#   1. start `ollama serve` and wait for it to be healthy,
#   2. resolve the tiny model to pull (hardware-aware, ADR-010) and pull it,
#   3. pre-warm the semantic plan-cache (ADR-009) so the headline demo questions
#      replay deterministically — instant even on the free CPU tier,
#   4. exec uvicorn (PID 1 handoff) on $PORT (HF sets it; 8000 locally).
#
# Everything runs as the runtime user, at the runtime paths, so the model store
# (~/.ollama) and the ChromaDB index (data/chroma) are writable — sidestepping the
# root-owned-path pitfall that a build-time approach would hit.
set -euo pipefail

echo "[entrypoint] ==== receivables-agent HF entrypoint starting ===="

OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
PORT="${PORT:-8000}"
FALLBACK_MODEL="qwen2.5:1.5b"   # tiny but tool-capable (the demo default, ADR-011)

# 1) Start the Ollama server in the background and wait until it answers.
echo "[entrypoint] starting ollama serve…"
ollama serve &
OLLAMA_PID=$!

echo "[entrypoint] waiting for Ollama at ${OLLAMA_BASE_URL}…"
for i in $(seq 1 60); do
  if curl -fsS "${OLLAMA_BASE_URL}/api/tags" >/dev/null 2>&1; then
    echo "[entrypoint] Ollama is up (after ${i}s)."
    break
  fi
  if ! kill -0 "${OLLAMA_PID}" 2>/dev/null; then
    echo "[entrypoint] FATAL: ollama serve exited before becoming healthy." >&2
    exit 1
  fi
  sleep 1
done
if ! curl -fsS "${OLLAMA_BASE_URL}/api/tags" >/dev/null 2>&1; then
  echo "[entrypoint] FATAL: Ollama did not become healthy in 60s." >&2
  exit 1
fi

# 2) Resolve the model to pull.
#    * A concrete OLLAMA_MODEL (the Space default, qwen2.5:1.5b) is used as-is —
#      deterministic footprint/latency for a shared free tier.
#    * OLLAMA_MODEL=auto (or unset) → hardware-aware pick (ADR-010): `--select`
#      emits an `OLLAMA_MODEL=<tag>` line (plus diagnostics like `HAS_GPU=…`), so
#      extract only that line's value; fall back to the tiny floor if empty.
if [ -n "${OLLAMA_MODEL:-}" ] && [ "${OLLAMA_MODEL}" != "auto" ]; then
  MODEL="${OLLAMA_MODEL}"
  echo "[entrypoint] using pinned model: ${MODEL}"
else
  echo "[entrypoint] OLLAMA_MODEL=auto — selecting for this hardware…"
  MODEL="$(python -m src.core.hardware --select 2>/dev/null \
           | grep '^OLLAMA_MODEL=' | head -n1 | cut -d= -f2- | tr -d '[:space:]')"
  if [ -z "${MODEL}" ]; then
    echo "[entrypoint] hardware select returned no model; using floor ${FALLBACK_MODEL}."
    MODEL="${FALLBACK_MODEL}"
  fi
fi

echo "[entrypoint] pulling model: ${MODEL}"
ollama pull "${MODEL}"

# Pin the resolved tag for the app so an `auto` resolution at agent-build time can
# never want a model we didn't just download.
export OLLAMA_MODEL="${MODEL}"
echo "[entrypoint] model ready: ${OLLAMA_MODEL}"

# 3) The plan-cache is BAKED at build time with curated, guard-validated plans
#    (see Dockerfile.hf) — so the demo's one-click questions are instant from the
#    first request, with no slow model-warm on startup. As a cheap safety net (in
#    case the baked cache dir didn't ship), re-seed the CURATED plans in the
#    background: it's model-free and idempotent (upsert by question), so it can't
#    hurt and it heals a missing cache without blocking serving.
(
  echo "[entrypoint] (bg) ensuring curated plan-cache is present…"
  if PYTHONIOENCODING=utf-8 python -m data.seed_plan_cache --curated >/dev/null 2>&1; then
    echo "[entrypoint] (bg) curated plan-cache ready."
  else
    echo "[entrypoint] (bg) WARN: curated seeding failed; cached Qs rely on the baked cache." >&2
  fi
) &

# 4) Serve NOW. exec so uvicorn becomes PID 1 and receives signals directly. Its
#    lifespan builds the agent + policy index once on startup, then `/api/health`
#    is live and the UI answers — seeded questions replay instantly from the baked cache.
echo "[entrypoint] starting serving on :${PORT}"
exec uvicorn src.api.app:app --host 0.0.0.0 --port "${PORT}"
