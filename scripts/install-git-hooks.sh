#!/bin/sh
# Installs this repository's git hooks into .git/hooks.
#
# Hooks live inside .git, which is never cloned, so they cannot ship with
# the repository -- run this once per clone. The rules themselves are in
# scripts/check-repo-hygiene.sh (tracked), and tests/test_repo_hygiene.py
# enforces the same rules in clones where nobody ran this.

set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$HERE/.git/hooks/pre-commit"

[ -d "$HERE/.git" ] || { printf 'not a git checkout: %s\n' "$HERE" >&2; exit 1; }

cat > "$HOOK" <<'HOOK_EOF'
#!/bin/sh
# Installed by scripts/install-git-hooks.sh -- do not edit here.
exec "$(git rev-parse --show-toplevel)/scripts/check-repo-hygiene.sh" --staged
HOOK_EOF
chmod +x "$HOOK"
printf 'installed %s\n' "$HOOK"
