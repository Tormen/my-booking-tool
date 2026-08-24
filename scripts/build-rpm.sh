#!/usr/bin/env bash
# Builds the my-booking-tool RPM from this checkout.
#
# One-time setup on the Fedora server (needs sudo, do it yourself):
#   sudo dnf install rpm-build rpmdevtools
#
# Then, any time (no sudo needed for the build itself):
#   scripts/build-rpm.sh
#
# This is the "easily reinstall everything after a server reinstall" path:
# keep this whole directory (e.g. in your own git remote / backed-up copy),
# and after a fresh OS install just re-clone it, run this script, then
# `sudo dnf install` the resulting RPM.
set -euo pipefail

NAME="my-booking-tool"
VERSION="1.0.0"
# UTC timestamp -- becomes the RPM's Release (see packaging/my-booking-tool.spec).
# Every build gets a strictly newer NEVRA this way, so `dnf install` on the
# result always applies as an upgrade instead of dnf refusing with
# "already installed, nothing to do" when you rebuild without bumping VERSION.
BUILD_TS="$(date -u +%Y%m%d%H%M%S)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Absolute path, not "~" -- this script builds as your regular user but the
# RPM is later installed with sudo/as root, so any "~" here would be
# ambiguous depending on who/how it gets re-run or copy-pasted. Resolve the
# invoking user's home explicitly, once, up front.
RPMBUILD_DIR="${HOME:?HOME is not set}/rpmbuild"

# 2026-07-10: running this via `sudo -u me /bin/sh .../build-rpm.sh`
# from a root shell whose OWN cwd was /root inherits that /root cwd here
# too (`sudo -u` drops privileges but does NOT chdir anywhere on its own,
# unless run with `-H`/`--chdir`) -- "me" can't stat/access /root (700,
# root-only), so nothing that actually NEEDS the cwd breaks, but `find`
# (used below to prune old RPMs) tries to restore its starting directory
# once it's done traversing and fails to, printing a harmless-but-
# confusing "find: Failed to restore initial working directory: /root:
# Permission denied" to stderr. Fixed at the source: explicitly cd into
# this checkout (which "me" -- the user this script actually runs as --
# always owns) before anything else runs, so no subprocess here ever
# depends on whatever cwd the invoking shell happened to have.
cd "$HERE"

if ! command -v rpmbuild >/dev/null; then
  echo "rpmbuild not found. Run: sudo dnf install rpm-build rpmdevtools" >&2
  exit 1
fi

rpmdev-setuptree >/dev/null 2>&1 || mkdir -p "$RPMBUILD_DIR"/{SOURCES,SPECS,RPMS,SRPMS,BUILD,BUILDROOT}

# Real, per-deployment files may live in a `*.local/` OVERLAY directory
# (see app/local_overlay.py) instead of at their ordinary paths, so that
# the operator's own backup/sync tooling -- which carries what is named
# `*.local` -- actually carries them. Locate it here for the staging step
# further down; exactly one is allowed, none is perfectly normal.
OVERLAY=""
for _candidate in "$HERE"/*.local; do
  [ -d "$_candidate" ] || continue
  if [ -n "$OVERLAY" ]; then
    echo "more than one *.local/ overlay directory in $HERE -- keep exactly one" >&2
    exit 1
  fi
  OVERLAY="$_candidate"
done
[ -n "$OVERLAY" ] && echo "using real files from $(basename "$OVERLAY")/"

# Regenerate generated static-site pages (site/privacy.html) from their
# .tmpl source + the actual settings.toml in this checkout, so a package
# built after e.g. changing [privacy].retention_months always ships a
# privacy page that says the same number, not a stale hand-typed one.
python3 "$HERE/scripts/render-site.py"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

DEST="$STAGE/$NAME-$VERSION"
mkdir -p "$DEST"
# Copy everything except runtime/data/vcs cruft AND personal files that
# are never packaged (same set .gitignore keeps out of git -- see the
# maintainer's local notes): the package ships generic code + templates
# only, never your actual registrations/secrets/personal design doc.
tar -C "$HERE" --exclude='./data' --exclude='./secrets' --exclude='./.git' \
    --exclude='./__pycache__' --exclude='./app/__pycache__' \
    --exclude='./tests/__pycache__' \
    --exclude='./*.local' --exclude='./*.local.*' \
    -cf - . | tar -C "$DEST" -xf -

# Now put the real files in place INSIDE the staging copy only -- this
# never writes into the checkout itself. Two sources, in the same order
# every other lookup uses: the overlay first, then the generic .example
# placeholder for anything still missing (a fresh clone of the public
# template has only the latter, and must still build).
#
# site/nginx-locations.conf is in this list since 2026-07-10: without it
# the packaged RPM never carried the file at all (see packaging/
# my-booking-tool.spec's %install/%files), so `my-bt setup -i` on the
# installed system could never find a real one to vimdiff against, no
# matter how complete this SOURCE checkout's copy was.
for real in settings.toml site/index.html site/impressum.html site/terms.html \
            site/privacy.html site/privacy.html.tmpl site/index_embedded.html \
            site/nginx-locations.conf; do
  if [ -n "$OVERLAY" ] && [ -f "$OVERLAY/$real" ]; then
    mkdir -p "$DEST/$(dirname "$real")"
    cp "$OVERLAY/$real" "$DEST/$real"
  elif [ ! -f "$DEST/$real" ] && [ -f "$DEST/$real.example" ]; then
    cp "$DEST/$real.example" "$DEST/$real"
    echo "no $real found -- using the generic $real.example as a starting point"
  fi
done

# `my-bt --version` (app/version.py) reads this at runtime -- the
# installed tree has no .git directory (excluded above), so bake the
# commit this build came from in here instead. "-dirty" flags an
# uncommitted-changes build so a version string can never be mistaken for
# an exact, reproducible commit when it isn't one. Falls back to a plain
# "unknown" (not this checkout's problem to solve) if this isn't a git
# checkout at all -- e.g. a source tarball downloaded without history.
if git -C "$HERE" rev-parse --short=12 HEAD >/dev/null 2>&1; then
  GIT_COMMIT_VALUE="$(git -C "$HERE" rev-parse --short=12 HEAD)"
  if ! git -C "$HERE" diff --quiet 2>/dev/null || ! git -C "$HERE" diff --cached --quiet 2>/dev/null; then
    GIT_COMMIT_VALUE="$GIT_COMMIT_VALUE-dirty"
  fi
else
  GIT_COMMIT_VALUE="unknown (not built from a git checkout)"
fi
echo "$GIT_COMMIT_VALUE" > "$DEST/GIT_COMMIT"

tar -C "$STAGE" -czf "$RPMBUILD_DIR/SOURCES/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"
cp "$HERE/packaging/$NAME.spec" "$RPMBUILD_DIR/SPECS/"

rpmbuild --define "build_timestamp $BUILD_TS" -ba "$RPMBUILD_DIR/SPECS/$NAME.spec"

# Prune older builds of this package from the local rpmbuild tree. Without
# this, RPMS/ accumulates one file per rebuild (all matching
# "$NAME-$VERSION*.rpm" since only Release changes between them), and the
# install command below -- and the one in README.md/SOLUTION-DESIGN.md -- would
# expand to multiple paths instead of exactly the one you just built.
find "$RPMBUILD_DIR/RPMS" -name "$NAME-$VERSION-*.rpm" ! -name "*-$BUILD_TS.*" -delete
find "$RPMBUILD_DIR/SRPMS" -name "$NAME-$VERSION-*.src.rpm" ! -name "*-$BUILD_TS.*" -delete

BUILT_RPM="$(find "$RPMBUILD_DIR/RPMS" -name "$NAME-$VERSION-$BUILD_TS.*.rpm")"

echo
echo "Built RPM:"
echo "  $BUILT_RPM"
echo
echo "Run this ON THIS SAME MACHINE ($(hostname)) -- the path above only"
echo "exists here. If your Fedora server is a *different* host than the one"
echo "you just built on, copy the file there first, e.g.:"
echo "  scp $BUILT_RPM youruser@your-fedora-host:/tmp/"
echo "then run the dnf install command below on that host instead (against"
echo "the copied path, and only if that host actually has dnf/is Fedora --"
echo "this RPM only installs on a Fedora/RHEL-family system)."
echo
echo "Install / update with (same command every time -- a rebuild always"
echo "produces a newer Release, so dnf applies it as an upgrade, restarting"
echo "the running service automatically):"
echo "  sudo dnf install \$(find $RPMBUILD_DIR/RPMS -name '$NAME-$VERSION*.rpm')"
