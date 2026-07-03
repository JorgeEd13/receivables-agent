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

# main and space-deploy have INTENTIONALLY unrelated histories (space-deploy carries the
# HF front-matter README + the tiny-Ollama image as the literal Dockerfile), AND
# space-deploy's history was rewritten by `git lfs migrate`. So neither `BRANCH..main`
# nor patch-id (`git cherry`) reliably auto-detects "new" commits — both try to re-apply
# the initial scaffold and explode into add/add conflicts. Deploy is therefore EXPLICIT:
# pass the exact main commit SHAs to forward (or `--last N` for the last N on main).
if [ "$#" -eq 0 ]; then
  echo "usage: bash scripts/deploy_space.sh <commit-ish>...   # exact main commits to forward" >&2
  echo "       bash scripts/deploy_space.sh --last N          # the last N commits on main" >&2
  echo "(auto-detect is intentionally NOT supported — the branches have unrelated, LFS-rewritten histories)" >&2
  git checkout "${start_branch}" >/dev/null 2>&1 || true
  exit 2
fi

if [ "$1" = "--last" ]; then
  n="${2:?--last needs a count}"
  mapfile -t picks < <(git rev-list --reverse --no-merges -n "${n}" main)
  echo "[deploy] forwarding the last ${n} commit(s) on main: ${picks[*]}"
  git cherry-pick "${picks[@]}"
else
  echo "[deploy] cherry-picking: $*"
  git cherry-pick "$@"
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
