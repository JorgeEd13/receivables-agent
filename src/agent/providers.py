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

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from src.core.config import Provider, Settings

logger = logging.getLogger(__name__)

# Sentinel that turns on hardware-aware selection (ADR-010): resolve the local
# model from the detected RAM/VRAM instead of pinning one in config.
AUTO_MODEL = "auto"


def has_credentials(provider: Provider, settings: Settings) -> bool:
    """Whether `provider` is usable with the current configuration."""
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    return True  # Ollama needs only a reachable base URL (checked at call time)


def resolve_ollama_model(settings: Settings) -> str:
    """Resolve `settings.ollama_model`, honouring the ``auto`` sentinel.

    ``auto`` → detect the hardware, ask Ollama which models are downloaded, and
    pick the best that fits (see ``src.core.hardware``). This makes the local
    path scale from a tiny CPU model to a stronger one **from the first run**,
    not just as a README claim. A concrete tag is returned unchanged.

    Falls back to the tiny floor model if detection yields nothing (e.g. Ollama
    is down and no model would fit the reported memory) — a real tag is always
    returned so ``ChatOllama`` construction never fails; the provider fallback
    still covers a genuinely unusable local box at call time.
    """
    if settings.ollama_model != AUTO_MODEL:
        return settings.ollama_model

    from src.core.hardware import detect_hardware, list_downloaded_models, recommend_model

    profile = detect_hardware()
    downloaded = list_downloaded_models(settings.ollama_base_url)
    best, _ = recommend_model(profile, downloaded)
    chosen = best or "qwen2.5:0.5b"
    logger.info(
        "hardware-aware model selection: effective_memory=%.1f GB has_gpu=%s -> %s",
        profile.effective_memory_gb,
        profile.has_gpu,
        chosen,
    )
    return chosen


def should_constrain(model_name: str, settings: Settings) -> bool:
    """Whether to wrap a local model in the grammar-constrained shim (ADR-011).

    ``off``/``on`` are explicit; ``auto`` wraps only tiny models — those at or
    below ``constrained_quality_max`` in the hardware catalog (unknown tags are
    left native, on the assumption they're a deliberate, capable choice).
    """
    mode = settings.constrained_tool_calls
    if mode == "off":
        return False
    if mode == "on":
        return True
    from src.core.hardware import catalog_quality

    quality = catalog_quality(model_name)
    return quality is not None and quality <= settings.constrained_quality_max


def build_chat_model(provider: Provider, settings: Settings) -> BaseChatModel:
    """Construct a chat model for `provider` (no network call at construction)."""
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        model_name = resolve_ollama_model(settings)
        kwargs: dict = {"num_ctx": settings.ollama_num_ctx} if settings.ollama_num_ctx else {}
        if settings.ollama_keep_alive:
            kwargs["keep_alive"] = settings.ollama_keep_alive
        base = ChatOllama(
            model=model_name,
            base_url=settings.ollama_base_url,
            temperature=0,
            **kwargs,
        )
        if should_constrain(model_name, settings):
            from src.agent.constrained import ConstrainedToolModel

            logger.info("constrained tool-calls ON for tiny model %s (ADR-011)", model_name)
            return ConstrainedToolModel(base=base)
        return base
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=0,
        )
    raise ValueError(f"Unknown provider: {provider!r}")
