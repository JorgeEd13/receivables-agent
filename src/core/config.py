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

    # --- API auth (used from Phase 4) ---
    app_api_key: str = "change-me"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
