# Recording the demo GIF

The README embeds `docs/demo.gif`. This is the one step that must run on a real
machine with a GUI and unrestricted internet — it can't be done in a sandboxed /
corporate-proxy environment (TLS interception blocks both the Gemini API and the
one-time ONNX embedding-model download). Use a machine on a normal network; a
free-tier `GEMINI_API_KEY` is enough (or local Ollama with a tool-capable model).

## 1. Configure

```bash
cp .env.example .env
# Edit .env:
#   GEMINI_API_KEY=<your-key>      # free tier is fine
#   APP_API_KEY=<any-string>       # also baked into the UI build by compose
```

## 2. Bring it up (one command)

```bash
docker compose up --build
```

First boot is slow: it builds the synthetic ledger and, on the first question,
downloads the embedding model once. Wait until logs show uvicorn is serving.

## 3. Verify

Open <http://localhost:8000>. Check `GET /api/health` returns
`{"status":"ok","agent_ready":true}`.

## 4. The three scripted questions

This order makes the `tools_used` badge tell the story (numbers, then a rule,
then both together):

1. **`Which 5 customers have the most overdue money?`** — `query_ledger` only.
2. **`What's our policy on credit holds?`** — `search_policy` only.
3. **`Which overdue accounts should go on credit hold, and what's the rule?`** —
   both tools: the prioritized accounts *and* the cited policy. This is the
   strongest frame; make sure it's in the GIF.

## 5. Record

- **Windows:** [ScreenToGif](https://www.screentogif.com/) — records straight to
  `.gif`, trims frames, lets you cap the size.
- **Linux:** [Peek](https://github.com/phw/peek), or record `.mp4` and convert
  with `ffmpeg -i in.mp4 -vf "fps=12,scale=900:-1" demo.gif`.

Keep it ~15–25 s and **under ~8 MB** so GitHub renders it inline (drop the FPS or
the width if it's too big).

## 6. Capture the README numbers (same session)

You're on a live provider — grab the two real numbers the README still needs:

- **Eval pass rate:** `pip install -e ".[gemini]"` (or `.[ollama]`) then
  `python -m evals.run`. Note the final `N/M cases passed (X%)`.
- **Latency:** note one question's response time.

Both go into the README as real figures (an "Evaluation" line and a "typical
latency" line) — never guessed. If an eval case fails, check whether it's a real
regression or a stale expectation in `evals/golden.py` (ledger regenerated).

## 7. Save and publish

Save as `docs/demo.gif` (the README already points there), then:

```bash
git add docs/demo.gif && git commit -m "docs: add demo GIF" && git push
```

That closes the ship gate: working MVP + GIF = a sendable "shipped link",
with or without the optional Hugging Face Space deploy.
