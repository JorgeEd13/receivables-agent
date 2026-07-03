#!/usr/bin/env bash
# Update the Hugging Face Space (Phase 6 Layer 3) from the current `main`.
#
# The Space tracks a dedicated `space-deploy` branch that carries what `main` (the
# GitHub showcase branch) deliberately does NOT:
#   * a README whose FIRST lines are the HF Space front-matter (title/sdk/app_port/…),
#   * the self-contained tiny-Ollama bake image as the *literal* `Dockerfile`
#     (HF ignores the front-matter `dockerfile_path`, so the file it builds must be
#     named `Dockerfile`; on `main` that name is the Gemini image),
#   * a `.gitattributes` (LF-pin for the shell entrypoint; LFS is a near-noop here
#     since the image ships no baked binaries — the ledger is built in-image and the
#     model is pulled at startup).
# Keeping those off `main` means the GitHub repo's `Dockerfile` stays the Gemini one
# and its README hero has no HF front-matter.
#
# This script forwards new `main` commits onto `space-deploy` (cherry-pick — the two
# branches have intentionally unrelated content at those files) and pushes to the Space.
#
# Prereqs (one-time):
#   git remote add space https://huggingface.co/spaces/JorgeEd/receivables-agent
#   git lfs install
# Usage (from the repo root, on an up-to-date `main`):
#   bash scripts/deploy_space.sh [<commit-ish>...]
# With no args, it cherry-picks every commit on `main` that `space-deploy` doesn't have.
set -euo pipefail

BRANCH="space-deploy"
REMOTE="space"

start_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "${start_branch}" != "main" ]; then
  echo "warning: you are on '${start_branch}', not 'main' — deploying its commits." >&2
fi

git checkout "${BRANCH}"

if [ "$#" -gt 0 ]; then
  echo "[deploy] cherry-picking: $*"
  git cherry-pick "$@"
else
  # main and space-deploy have INTENTIONALLY unrelated histories (space-deploy carries
  # the HF front-matter README + the tiny-Ollama image as the literal Dockerfile), so
  # `BRANCH..main` lists ALL of main and explodes into add/add conflicts. Use `git
  # cherry`, which compares by PATCH content (works across unrelated histories): a
  # leading `+` marks a main commit whose change isn't yet on space-deploy.
  echo "[deploy] forwarding main commits not yet applied to ${BRANCH} (by patch id)…"
  mapfile -t picks < <(git cherry "${BRANCH}" main | awk '/^\+/ {print $2}')
  if [ "${#picks[@]}" -eq 0 ]; then
    echo "[deploy] nothing new to forward."
  else
    echo "[deploy] picking ${#picks[@]} commit(s)."
    git cherry-pick "${picks[@]}"
  fi
fi

# HF ignores the front-matter dockerfile_path and builds the literal `Dockerfile`,
# so keep it a byte-for-byte copy of the maintained `Dockerfile.hf`. Auto-sync here
# (instead of by hand every deploy) so the Space can never build a stale image.
if ! cmp -s Dockerfile Dockerfile.hf; then
  echo "[deploy] syncing literal Dockerfile ← Dockerfile.hf"
  cp Dockerfile.hf Dockerfile
  git add Dockerfile
  git commit -m "deploy(space): sync literal Dockerfile with Dockerfile.hf" >/dev/null
fi

echo "[deploy] pushing ${BRANCH} → ${REMOTE}/main…"
git push "${REMOTE}" "${BRANCH}:main"

git checkout "${start_branch}"
echo "[deploy] done. Watch the build at https://huggingface.co/spaces/JorgeEd/receivables-agent"
echo "[deploy] then verify: open https://jorgeed-receivables-agent.hf.space/ and ask a question."
