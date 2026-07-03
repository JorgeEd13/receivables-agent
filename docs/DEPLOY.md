# Deploy — the live "click here and try it" link (Phase 6, Layer 3)

The Layer 3 goal: a **reachable public URL** so the serving layer isn't just a
`docker compose up` claim but a page a reviewer can open and ask a question. Target:
**Hugging Face Spaces** (a Docker Space) — a permanent free URL, no card required.

## What the Space runs

A **self-contained tiny-Ollama image** ([`Dockerfile.hf`](../Dockerfile.hf), ADR-012):
the container installs the Ollama server, bakes the synthetic ledger at build time,
and at **startup** ([`scripts/hf_entrypoint.sh`](../scripts/hf_entrypoint.sh)) runs
`ollama serve`, pulls a tiny model, pre-warms the plan-cache, then serves the API +
the built React UI. **No API key, no cloud quota, always up** — a free URL that runs
its *own* model.

**Honest boundary.** The Space runs a **tiny** model (default `qwen2.5:1.5b`) on a
free CPU tier. The plan-cache (ADR-009) replays the headline questions instantly; a
novel question is slower and its SQL may be weaker — that's the "shines with a better
model" story (ADR-011). The strong-model numbers are captured on a GPU box and quoted
separately; the Space never implies the tiny model *is* the ceiling.

## Latency shape (set expectations)

- **Headline / seeded questions** → plan-cache hit → replayed deterministically
  **without the LLM** → fast.
- **Novel questions** → tiny model on CPU → tens of seconds on the free tier. The
  guarded SQL tool (ADR-003) keeps a weak query safe.

## The `space-deploy` branch (why it exists)

The Space tracks a dedicated **`space-deploy`** branch, separate from the GitHub
`main` showcase branch, because HF needs three things `main` deliberately does not:

1. A **README whose first lines are the HF front-matter** (below).
2. The tiny-Ollama image as the **literal `Dockerfile`** — HF **ignores** the
   front-matter `dockerfile_path`, so the file it builds must be named `Dockerfile`.
   On `main` that name is the Gemini image; on `space-deploy` it's `Dockerfile.hf`.
3. A **`.gitattributes`** — here it only LF-pins the shell entrypoint (a CRLF shebang
   breaks in the Linux container). We ship **no baked binaries** (the ledger is built
   in-image; the model is pulled at startup), so LFS is a near-noop safety net —
   unlike the forge-pdm Space, which LFS-tracks a parquet fixture.

Keeping those off `main` means the GitHub repo's `Dockerfile` stays the Gemini one
and `clone && pytest` needs no LFS.

## Deploy to Hugging Face Spaces

1. **Create a Space** (owner `JorgeEd`, name `receivables-agent`) → SDK **Docker**,
   hardware **CPU basic (free)**, visibility **Public**.
2. HF reads its config from the **Space README front-matter**. The `space-deploy`
   branch README starts with:

   ```yaml
   ---
   title: receivables-agent
   emoji: 💸
   colorFrom: indigo
   colorTo: green
   sdk: docker
   dockerfile_path: Dockerfile.hf
   app_port: 8000
   pinned: false
   license: mit
   short_description: Ask a synthetic receivables ledger a question — live tiny-model demo
   ---
   ```

   `app_port: 8000` matches the app's `EXPOSE`/`CMD`. (`dockerfile_path` is kept for
   correctness even though HF ignores it — hence the literal `Dockerfile` on this branch.)

3. **Push the repo to the Space** (a Space is a git remote):

   ```bash
   git remote add space https://huggingface.co/spaces/JorgeEd/receivables-agent
   git lfs install
   bash scripts/deploy_space.sh          # forwards main → space-deploy → push space-deploy:main
   ```

   HF builds `Dockerfile.hf` on its runners; the model pull + plan-cache warm happen
   at container startup (no secrets, no build-time network beyond the base image + pip).

4. Once the build is green, the Space serves at
   `https://JorgeEd-receivables-agent.hf.space`. Open it in a browser and ask a
   question (the same-origin UI carries the baked API key), or:

   ```bash
   curl -s https://JorgeEd-receivables-agent.hf.space/api/health   # {"status":"ok"}
   ```

5. **Put the live link in the README** (the Layer 4 DoD): a reachable badge + a
   "try it" line.

## Build & smoke-test the image locally (needs Docker)

```bash
docker build -f Dockerfile.hf -t receivables-agent:hf .
docker run --rm -p 8000:8000 receivables-agent:hf
# First boot: ollama serve + pull the tiny model + warm the plan-cache — give it a
# couple of minutes before the first request.
curl -s localhost:8000/api/health        # {"status":"ok"}
curl -s -X POST localhost:8000/api/chat \
  -H 'X-API-Key: demo-key' -H 'content-type: application/json' \
  -d '{"message":"Who are the top overdue customers?"}'
# → a real tool-using answer with a number (headline Q → plan-cache replay, fast).
# Then open http://localhost:8000/ in a browser for the chat UI.
```

**Why the model is pulled at startup, not baked.** Baking would bloat the image and
freeze the choice; pulling at startup keeps it small and lets `OLLAMA_MODEL=auto`
(ADR-010) pick per the host box. The pull runs as the runtime user (UID 1000) into a
writable `HOME`, so there's no root-owned-store pitfall.

## Alternatives (same image, different host)

- **Render** — a free Docker web service; set the Dockerfile path to `Dockerfile.hf`
  and the port to 8000. Cold-starts after idle.
- **Fly.io** — `fly launch --dockerfile Dockerfile.hf`; scale-to-zero free allowance
  (needs a card on file).

All three serve the same self-contained image; only the platform glue differs.
