"""Configuration, loaded from the environment (and an optional `.env`).

Secrets hygiene (see CLAUDE.md): nothing is hardcoded — every value comes from
an environment variable, with safe, offline-friendly defaults for local dev.

Provider order is the one knob that flips between environments:

* **dev**    — Ollama primary (zero cost, no network), Gemini fallback.
* **deploy** — set ``PRIMARY_PROVIDER=gemini`` (a cloud model) and the order
  inverts without a code change.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["ollama", "gemini"]


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM providers (dual provider with active fallback) ---
    primary_provider: Provider = "ollama"
    # Fallback is tried only if it has credentials; None disables it.
    fallback_provider: Provider | None = "gemini"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.0-flash"

    # --- Data ---
    ledger_path: str = "data/ledger.duckdb"
    # Hard cap on rows returned by the SQL tool (the guard enforces it).
    max_rows: int = Field(default=200, ge=1, le=10_000)

    # --- RAG over the collections policy ---
    policy_path: str = "data/collections_policy.md"
    chroma_path: str = "data/chroma"
    policy_collection: str = "collections_policy"
    # How many policy chunks `search_policy` returns.
    search_k: int = Field(default=4, ge=1, le=20)

    # --- Semantic plan-cache (Phase 6, ADR-009) ---
    # Skip the LLM for a semantically-similar past question by re-running its
    # cached *plan* (validated tool calls) live. Caches reasoning, never answers.
    plan_cache_enabled: bool = True
    plan_cache_collection: str = "plan_cache"
    # Cosine similarity a question must reach to reuse a cached plan. High by
    # design: a miss simply falls through to the LLM, so precision beats recall.
    plan_cache_threshold: float = Field(default=0.90, ge=0.0, le=1.0)

    # --- API auth (used from Phase 4) ---
    app_api_key: str = "change-me"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
