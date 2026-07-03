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
  echo "[deploy] cherry-picking every commit on main not yet on ${BRANCH}…"
  # --no-merges: linear history only; cherry-pick can't apply merge commits blindly.
  mapfile -t picks < <(git rev-list --reverse --no-merges "${BRANCH}..main")
  if [ "${#picks[@]}" -eq 0 ]; then
    echo "[deploy] nothing new to forward."
  else
    git cherry-pick "${picks[@]}"
  fi
fi

echo "[deploy] pushing ${BRANCH} → ${REMOTE}/main…"
git push "${REMOTE}" "${BRANCH}:main"

git checkout "${start_branch}"
echo "[deploy] done. Watch the build at https://huggingface.co/spaces/JorgeEd/receivables-agent"
echo "[deploy] then verify: open https://jorgeed-receivables-agent.hf.space/ and ask a question."
