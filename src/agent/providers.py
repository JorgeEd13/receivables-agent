"""LLM providers with active fallback.

Two interchangeable chat models — a local one (Ollama) and a cloud one
(Gemini). The primary/fallback order is config-driven (``Settings``), so the
same code runs Ollama-first in dev and Gemini-first on a cloud Space by flipping
``PRIMARY_PROVIDER``.

The provider SDKs are imported lazily so that, e.g., a dev box without the
Gemini SDK (or without a key) can still build an Ollama-only agent. The fallback
is wired with LangChain's ``with_fallbacks``: if the primary raises at call time
(Ollama down, Gemini quota), the next provider is tried transparently.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from src.core.config import Provider, Settings


def has_credentials(provider: Provider, settings: Settings) -> bool:
    """Whether `provider` is usable with the current configuration."""
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    return True  # Ollama needs only a reachable base URL (checked at call time)


def build_chat_model(provider: Provider, settings: Settings) -> BaseChatModel:
    """Construct a chat model for `provider` (no network call at construction)."""
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=0,
        )
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=0,
        )
    raise ValueError(f"Unknown provider: {provider!r}")
