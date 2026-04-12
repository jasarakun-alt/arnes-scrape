#!/usr/bin/env bash
# Ustvari repozitorij na GitHub pod tvojim računom in potisne vejo main.
# Pred tem: gh auth login (enkrat na računalniku)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GH="${GH:-/opt/homebrew/bin/gh}"
if ! command -v "$GH" &>/dev/null && command -v gh &>/dev/null; then
  GH="$(command -v gh)"
fi

if ! "$GH" auth status &>/dev/null; then
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "$GITHUB_TOKEN" | "$GH" auth login --with-token -h github.com
  else
    echo "Nisi prijavljen v GitHub. Zaženi v terminalu:"
    echo "  $GH auth login"
    echo "(HTTPS + prijava v brskalniku). Nato znova zaženi ta skript."
    exit 1
  fi
fi

REPO_NAME="${1:-arnes-scrape}"
if git remote get-url origin &>/dev/null; then
  echo "Remote 'origin' že obstaja. Če je napačen: git remote remove origin"
  exit 1
fi

"$GH" repo create "$REPO_NAME" --public --source=. --remote=origin --push
USER="$("$GH" api user -q .login)"
echo "Končano: https://github.com/${USER}/${REPO_NAME}"
