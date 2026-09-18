#!/bin/bash
set -euo pipefail

REPO_DIR={repo_dir}
if [ ! -d "$REPO_DIR/.git" ]; then
  printf 'Expected repository is missing: %s\n' "$REPO_DIR" >&2
  exit 127
fi
cd "$REPO_DIR"

cat > /tmp/solution_patch.diff << '__SOLUTION__'
{patch}
__SOLUTION__

if ! git update-index --really-refresh >/dev/null; then
  printf 'Unable to refresh the Git index\n' >&2
  exit 1
fi

if ! git diff-files --quiet --; then
  printf 'Repository worktree differs from the index; refusing to apply solution patch\n' >&2
  git diff -- >&2 || true
  exit 1
fi

git apply -v --3way --recount --ignore-space-change --whitespace=nowarn /tmp/solution_patch.diff
