#!/bin/sh
# Refuses paths that must never enter this repository's history.
#
# This is enforcement, not an ignore list. A .gitignore rule is silent and
# only prevents an accidental `git add`: it does nothing about a file that
# was force-added, committed before the rule existed, or added by tooling
# that stages paths explicitly. This exits non-zero and names the file.
#
# Two callers, one implementation:
#   - .git/hooks/pre-commit, with --staged: blocks the commit locally.
#   - tests/test_repo_hygiene.py, over every tracked file: travels with
#     the repo, so it runs in every clone and in the RPM's own %check.
#
# Categories, and why each is refused:
#   - real per-deployment files at their ordinary paths. They belong in a
#     `*.local` directory (see app/local_overlay.py); at these paths they
#     are publishable by accident, and settings.toml carries secret file
#     paths and a private calendar feed URL.
#   - anything named *.local / *.local.* -- personal by definition.
#   - runtime state: data/ (participant records) and secrets/.
#   - byte-code, build output, editor and OS droppings.

set -eu

usage() {
    printf 'usage: check-repo-hygiene.sh [--staged]\n' >&2
    exit 2
}

MODE=tracked
case "${1:-}" in
    --staged) MODE=staged ;;
    "") ;;
    *) usage ;;
esac

if [ "$MODE" = staged ]; then
    FILES=$(git diff --cached --name-only --diff-filter=AM)
else
    FILES=$(git ls-files)
fi

# One extended regex, anchored per alternative. Kept here rather than
# duplicated in the hook and the test, so a new rule is added once.
PATTERN='^(settings\.toml|site/(index|impressum|privacy|terms|index_embedded)\.html|site/privacy\.html\.tmpl|site/nginx-locations\.conf)$|(^|/)[^/]*\.local(\..*)?$|(^|/)__pycache__/|\.py[co]$|(^|/)\.DS_Store$|\.swp$|(^|/)[^/]*\.egg-info/|^(data|secrets|build|dist|\.venv|venv)/'

OFFENDERS=$(printf '%s\n' "$FILES" | grep -E "$PATTERN" || true)

if [ -n "$OFFENDERS" ]; then
    printf 'These paths must not be in this repository:\n' >&2
    printf '%s\n' "$OFFENDERS" | sed 's/^/  /' >&2
    printf '\nReal per-deployment files belong in a *.local directory (README.md,\n' >&2
    printf '"Generic template vs. your real config"). Everything else here is\n' >&2
    printf 'generated: remove it from the index rather than committing it.\n' >&2
    exit 1
fi
