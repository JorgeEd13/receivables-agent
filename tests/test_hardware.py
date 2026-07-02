"""Offline tests for hardware-aware model selection (ADR-010).

No real detection or network: the ``HardwareProfile`` is constructed directly and
the "downloaded" list is passed in, so these exercise the recommendation logic
and the ``auto`` wiring without touching ``nvidia-smi`` or Ollama.
"""

from __future__ import annotations

from src.agent.providers import AUTO_MODEL, resolve_ollama_model
from src.core.config import Settings
from src.core.hardware import (
    HardwareProfile,
    _CATALOG,
    recommend_model,
)


def _profile(*, ram=8.0, vram=None) -> HardwareProfile:
    return HardwareProfile(
        ram_total_gb=ram,
        ram_available_gb=ram / 2,
        cpu_cores_physical=4,
        cpu_cores_logical=8,
        cpu_name="Test CPU",
        gpu_name="Test GPU" if vram else None,
        gpu_vram_gb=vram,
    )


# --- effective memory -------------------------------------------------------


def test_effective_memory_uses_vram_when_gpu_present():
    p = _profile(ram=16.0, vram=6.0)
    assert p.has_gpu is True
    assert p.effective_memory_gb == 6.0  # VRAM, not RAM


def test_effective_memory_uses_80pct_ram_without_gpu():
    p = _profile(ram=8.0, vram=None)
    assert p.has_gpu is False
    assert p.effective_memory_gb == 6.4  # 8.0 * 0.80


def test_tiny_integrated_gpu_is_not_counted_as_gpu():
    # <=1 GB VRAM is treated as "no usable GPU" so we fall back to the RAM path.
    p = _profile(ram=8.0, vram=0.5)
    assert p.has_gpu is False
    assert p.effective_memory_gb == 6.4


# --- recommendation ---------------------------------------------------------


def test_catalog_is_ordered_by_quality():
    qualities = [s.quality for s in _CATALOG]
    assert qualities == sorted(qualities), "catalog should be tiny -> strong"


def test_prefers_best_downloaded_that_fits():
    # 6 GB VRAM fits qwen2.5:7b (needs 6). Both 7b and the tiny model are
    # downloaded → the higher-quality one wins.
    p = _profile(ram=16.0, vram=6.0)
    best, rows = recommend_model(p, ["qwen2.5:0.5b", "qwen2.5:7b"])
    assert best == "qwen2.5:7b"
    # sanity: the row for 7b is marked as fitting + downloaded
    row = next(r for r in rows if r["name"] == "qwen2.5:7b")
    assert row["fits"] and row["downloaded"]


def test_skips_downloaded_model_that_does_not_fit():
    # A big model is downloaded but 2 GB VRAM can't run it → pick the tiny one
    # that both fits and is downloaded.
    p = _profile(ram=4.0, vram=2.0)
    best, _ = recommend_model(p, ["qwen2.5:14b", "qwen2.5:1.5b"])
    assert best == "qwen2.5:1.5b"


def test_falls_back_to_best_that_would_fit_when_nothing_downloaded():
    # Nothing pulled yet: recommend the best model the hardware *could* run.
    p = _profile(ram=16.0, vram=6.0)
    best, _ = recommend_model(p, [])
    assert best == "qwen2.5:7b"  # highest-quality fitting model


def test_name_matching_is_tag_tolerant():
    # "qwen2.5:7b" downloaded should satisfy the catalog entry regardless of tag
    # normalization on either side.
    p = _profile(ram=16.0, vram=6.0)
    _, rows = recommend_model(p, ["qwen2.5:7b"])
    row = next(r for r in rows if r["name"] == "qwen2.5:7b")
    assert row["downloaded"] is True


# --- providers wiring -------------------------------------------------------


def test_resolve_passes_through_explicit_model():
    settings = Settings(ollama_model="llama3.1")
    assert resolve_ollama_model(settings) == "llama3.1"


def test_resolve_auto_returns_a_concrete_tag(monkeypatch):
    # Force a known hardware + downloaded set so "auto" resolves deterministically
    # without hitting nvidia-smi or Ollama.
    import src.agent.providers as providers

    monkeypatch.setattr(providers, "AUTO_MODEL", AUTO_MODEL)  # explicit, for clarity
    monkeypatch.setattr(
        "src.core.hardware.detect_hardware",
        lambda: _profile(ram=16.0, vram=6.0),
    )
    monkeypatch.setattr(
        "src.core.hardware.list_downloaded_models",
        lambda _base_url: ["qwen2.5:7b"],
    )
    settings = Settings(ollama_model="auto")
    assert resolve_ollama_model(settings) == "qwen2.5:7b"


def test_resolve_auto_never_returns_empty(monkeypatch):
    # Detection yields nothing (Ollama down, no model fits) → the tiny floor.
    monkeypatch.setattr(
        "src.core.hardware.detect_hardware",
        lambda: _profile(ram=0.0, vram=None),
    )
    monkeypatch.setattr(
        "src.core.hardware.list_downloaded_models",
        lambda _base_url: [],
    )
    settings = Settings(ollama_model="auto")
    assert resolve_ollama_model(settings) == "qwen2.5:0.5b"
