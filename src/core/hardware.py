"""Hardware-aware local-model selection.

Detects the machine's RAM / GPU-VRAM / CPU and picks the best **local Ollama**
model that fits, so the agent's local path auto-scales from a tiny CPU-only model
(the free-tier / laptop floor) to a stronger one where the hardware allows — the
cloud provider remaining the ceiling. This is what makes the demo's *"runs a tiny
model on free CPU, shines with a better model"* claim real from the first run,
not just a README promise.

Design notes:

* **LLM-only catalog.** Unlike a general Ollama setup, this project embeds with
  ChromaDB's bundled ONNX MiniLM (see ``src/rag/embeddings.py``), so there is no
  Ollama embedding model to select — only the chat model.
* **No new dependency.** ``psutil`` is used *if present* but every detector has a
  stdlib fallback (``/proc``, ``os``, ``nvidia-smi``), so the module runs with the
  system Python and adds nothing to ``pyproject``.
* **Public data only (clean room).** The catalog is public Ollama model names and
  generic memory heuristics — no confidential material.

Standalone diagnostic::

    python -m src.core.hardware            # readable table
    python -m src.core.hardware --json     # profile + recommendations as JSON
    python -m src.core.hardware --select   # machine-readable OLLAMA_MODEL=... line

Programmatic::

    from src.core.hardware import detect_hardware, recommend_model
    profile = detect_hardware()
    best, catalog = recommend_model(profile, downloaded)
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Model catalog — public Ollama chat models, tiny → strong.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """A local chat model and the memory it needs to run comfortably."""

    name: str  # Ollama tag
    ram_gb: float  # approx. RAM needed to run on CPU
    vram_gb: float  # approx. VRAM needed to run on GPU
    quality: int  # coarse 1–10 capability rank (higher = better)
    description: str

    def requirement_for(self, has_gpu: bool) -> float:
        """Memory this model needs given whether a usable GPU is present."""
        return self.vram_gb if has_gpu else self.ram_gb


# Ordered by quality. Tiny models are the free-CPU / laptop floor; the mid tier
# is where a typical dev box with a GPU lands; the top is cloud/high-end.
_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("qwen2.5:0.5b", 1.0, 1.5, 1, "Absolute floor — the free-CPU demo model"),
    ModelSpec("qwen2.5:1.5b", 1.5, 2.0, 2, "Tiny but tool-capable — small footprint"),
    ModelSpec("llama3.2:3b", 3.0, 4.0, 4, "Balanced — good for ~8 GB RAM, no GPU"),
    ModelSpec("qwen2.5:3b", 3.0, 4.0, 5, "Balanced — solid tool-calling at 3B"),
    ModelSpec("qwen2.5:7b", 5.0, 6.0, 7, "Good quality — needs ~6 GB VRAM or ~10 GB RAM"),
    ModelSpec("llama3.1:8b", 6.0, 7.0, 7, "Alternative to qwen2.5:7b"),
    ModelSpec("gemma2:9b", 7.0, 8.0, 8, "High quality — needs ~12 GB RAM"),
    ModelSpec("qwen2.5:14b", 9.0, 10.0, 9, "Very high quality — needs ~16 GB VRAM"),
    ModelSpec("llama3.3:70b", 42.0, 45.0, 10, "Excellent — high-end GPU or 64 GB+ RAM"),
)


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------


@dataclass
class HardwareProfile:
    ram_total_gb: float
    ram_available_gb: float
    cpu_cores_physical: int
    cpu_cores_logical: int
    cpu_name: str
    gpu_name: str | None
    gpu_vram_gb: float | None

    @property
    def has_gpu(self) -> bool:
        """A GPU with enough VRAM to matter (small integrated ones don't)."""
        return self.gpu_vram_gb is not None and self.gpu_vram_gb > 1.0

    @property
    def effective_memory_gb(self) -> float:
        """Memory usable for the model: VRAM on a GPU box, else ~80% of RAM.

        The 20% headroom keeps the OS and this very process (agent + Chroma)
        from being starved when the model loads on CPU.
        """
        if self.has_gpu:
            return float(self.gpu_vram_gb)
        return round(self.ram_total_gb * 0.80, 1)

    def summary(self) -> str:
        lines = [
            f"  RAM: {self.ram_total_gb:.1f} GB total "
            f"({self.ram_available_gb:.1f} GB free now)",
            f"  CPU: {self.cpu_name} — "
            f"{self.cpu_cores_physical} physical / {self.cpu_cores_logical} logical cores",
        ]
        if self.has_gpu:
            lines.append(f"  GPU: {self.gpu_name} — {self.gpu_vram_gb:.1f} GB VRAM")
        else:
            lines.append("  GPU: none detected — models run on CPU")
        basis = " (VRAM)" if self.has_gpu else " (80% of RAM)"
        lines.append(f"  Effective memory for Ollama: {self.effective_memory_gb:.1f} GB{basis}")
        return "\n".join(lines)


def _detect_ram() -> tuple[float, float]:
    """Return ``(total_gb, available_gb)``; ``(0.0, 0.0)`` if undetectable."""
    try:
        import psutil

        m = psutil.virtual_memory()
        return round(m.total / 1024**3, 1), round(m.available / 1024**3, 1)
    except ImportError:
        pass

    # Stdlib Linux fallback: /proc/meminfo (kB). Lets the diagnostic run with the
    # system Python, outside the venv, with no psutil installed.
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = int(rest.split()[0])
        total_kb = meminfo.get("MemTotal", 0)
        # MemAvailable is the kernel's own estimate (>= 3.14); fall back to MemFree.
        avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        if total_kb:
            return round(total_kb / 1024**2, 1), round(avail_kb / 1024**2, 1)
    except Exception:
        pass

    # Stdlib Windows fallback: wmic.
    try:
        import re

        out = subprocess.check_output(
            "wmic OS get TotalVisibleMemorySize,FreePhysicalMemory /value",
            shell=True,
            text=True,
            timeout=5,
        )
        total_kb = int(re.search(r"TotalVisibleMemorySize=(\d+)", out).group(1))
        free_kb = int(re.search(r"FreePhysicalMemory=(\d+)", out).group(1))
        return round(total_kb / 1024**2, 1), round(free_kb / 1024**2, 1)
    except Exception:
        pass

    return 0.0, 0.0


def _detect_cpu() -> tuple[int, int, str]:
    """Return ``(physical_cores, logical_cores, name)``."""
    try:
        import psutil

        physical = psutil.cpu_count(logical=False) or 1
        logical = psutil.cpu_count(logical=True) or physical
    except ImportError:
        import os

        physical = logical = os.cpu_count() or 1

    name = platform.processor() or platform.machine() or "unknown"
    # platform.processor() is usually empty on Linux → read the real name from
    # /proc/cpuinfo (same motivation as the RAM fallback: run without psutil).
    if name in ("", "x86_64", "AMD64", "unknown"):
        try:
            with open("/proc/cpuinfo", encoding="ascii", errors="ignore") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    return physical, logical, name


def _detect_nvidia_gpu() -> tuple[str | None, float | None]:
    """Detect an NVIDIA GPU via ``nvidia-smi``. Return ``(name, vram_gb)`` or ``(None, None)``."""
    candidates = ["nvidia-smi"]
    if platform.system() == "Windows":
        candidates += [
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]

    for cmd in candidates:
        try:
            out = subprocess.check_output(
                [cmd, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                text=True,
                timeout=5,
                stderr=subprocess.DEVNULL,
            )
            line = out.strip().splitlines()[0]
            name, vram = line.rsplit(",", 1)
            return name.strip(), round(int(vram.strip()) / 1024, 1)
        except Exception:
            continue

    return None, None


def detect_hardware() -> HardwareProfile:
    """Profile the current machine (RAM, CPU, NVIDIA GPU)."""
    ram_total, ram_avail = _detect_ram()
    physical, logical, cpu_name = _detect_cpu()
    gpu_name, gpu_vram = _detect_nvidia_gpu()
    return HardwareProfile(
        ram_total_gb=ram_total,
        ram_available_gb=ram_avail,
        cpu_cores_physical=physical,
        cpu_cores_logical=logical,
        cpu_name=cpu_name,
        gpu_name=gpu_name,
        gpu_vram_gb=gpu_vram,
    )


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """Compare model names tolerant of the ``:tag`` (``qwen2.5`` == ``qwen2.5:7b``)."""
    return name.split(":")[0].lower() if ":" not in name else name.lower()


def recommend_model(
    profile: HardwareProfile,
    downloaded: list[str],
) -> tuple[str | None, list[dict]]:
    """Pick the best model for this hardware.

    Prefers the highest-quality model that both **fits** the effective memory and
    is **already downloaded** (so the first request doesn't stall on a pull). If
    none of the fitting models are downloaded yet, returns the best that *would*
    fit (the caller can pull it, or fall back to the cloud provider).

    Returns ``(best_name, catalog_rows)`` where each row is
    ``{name, fits, downloaded, quality, description, memory_required}``.
    """
    effective = profile.effective_memory_gb
    downloaded_norms = {_normalize(d) for d in downloaded}

    rows: list[dict] = []
    for spec in _CATALOG:
        required = spec.requirement_for(profile.has_gpu)
        rows.append(
            {
                "name": spec.name,
                "fits": effective >= required,
                "downloaded": _normalize(spec.name) in downloaded_norms,
                "quality": spec.quality,
                "description": spec.description,
                "memory_required": required,
            }
        )

    by_quality = sorted(rows, key=lambda r: -r["quality"])
    best = next((r["name"] for r in by_quality if r["fits"] and r["downloaded"]), None)
    if best is None:  # nothing downloaded fits → best that would fit
        best = next((r["name"] for r in by_quality if r["fits"]), None)
    return best, rows


def list_downloaded_models(base_url: str) -> list[str]:
    """Return the chat models Ollama has pulled, via ``/api/tags``.

    Stdlib-only (``urllib``) on purpose, so the diagnostic runs before any
    ``pip install``. Fails soft (Ollama down → ``[]``), in which case the
    recommender returns "the best that would fit".
    """
    import json
    import urllib.request

    try:
        url = f"{base_url.rstrip('/')}/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (local URL)
            data = json.loads(resp.read().decode("utf-8"))
        names = [m["name"] for m in data.get("models", [])]
    except Exception:
        return []
    # Drop embedding models — this project doesn't select an Ollama embedder.
    return [n for n in names if "embed" not in n and "minilm" not in n.lower()]


# ---------------------------------------------------------------------------
# CLI — standalone diagnostic
# ---------------------------------------------------------------------------


def _print_table(rows: list[dict], current: str | None = None) -> None:
    for r in sorted(rows, key=lambda x: -x["quality"]):
        here = "<- current" if current and _normalize(r["name"]) == _normalize(current) else ""
        dl = "downloaded" if r["downloaded"] else "needs pull "
        fit = "fits" if r["fits"] else "too big"
        stars = "*" * r["quality"] + "." * (10 - r["quality"])
        print(f"  {r['name']:<16} {stars}  {fit:<8}  {dl}  {r['description']}  {here}")


def _main() -> int:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(
        description="Hardware diagnostic + local Ollama model recommendation.",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="Machine-readable output (KEY=value) for a container entrypoint / "
        "compose to consume. Prints OLLAMA_MODEL and HAS_GPU.",
    )
    parser.add_argument("--json", action="store_true", help="Profile + recommendations as JSON.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        help="Ollama URL (default: OLLAMA_BASE_URL or http://localhost:11434).",
    )
    args = parser.parse_args()

    profile = detect_hardware()
    downloaded = list_downloaded_models(args.base_url)
    best, rows = recommend_model(profile, downloaded)

    if args.select:
        print(f"OLLAMA_MODEL={best or ''}")
        print(f"HAS_GPU={'1' if profile.has_gpu else '0'}")
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "hardware": {
                        "ram_total_gb": profile.ram_total_gb,
                        "ram_available_gb": profile.ram_available_gb,
                        "cpu_name": profile.cpu_name,
                        "cpu_cores_physical": profile.cpu_cores_physical,
                        "cpu_cores_logical": profile.cpu_cores_logical,
                        "gpu_name": profile.gpu_name,
                        "gpu_vram_gb": profile.gpu_vram_gb,
                        "has_gpu": profile.has_gpu,
                        "effective_memory_gb": profile.effective_memory_gb,
                    },
                    "recommended": best,
                    "catalog": rows,
                },
                indent=2,
            )
        )
        return 0

    print("=" * 72)
    print("  receivables-agent — hardware diagnostic")
    print("=" * 72)
    print("\n[HARDWARE]")
    print(profile.summary())

    if not downloaded:
        print(
            f"\n[note] Ollama did not answer at {args.base_url} — 'fits' reflects "
            "the hardware, 'downloaded' is empty."
        )

    current = os.getenv("OLLAMA_MODEL", "auto")
    print("\n[LLM] Models for this hardware:")
    print(f"  Current (.env): {current}")
    if best and _normalize(best) != _normalize(current):
        print(f"  Recommended   : {best}  <- set OLLAMA_MODEL={best} (or 'auto')")
    print()
    _print_table(rows, current)

    print("\n[tip] Set OLLAMA_MODEL=auto to let the agent pick the best fit at startup.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
