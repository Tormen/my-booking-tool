#!/usr/bin/env bash
# Manual/dev install path -- NOT the recommended way to (re)install after a
# server reinstall, that's scripts/build-rpm.sh + `dnf install` (see
# packaging/my-booking-tool.spec and README.md). This script exists for
# quick local testing or systems where building an RPM is inconvenient.
# Idempotent: safe to re-run.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo scripts/install.sh)" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve $1 (a real, per-deployment filename like settings.toml) to
# itself if you already have a real copy in this checkout, else to its
# tracked "$1.example" placeholder (a fresh clone of the public template
# repo only has the .example ones) -- mirrors app/site_render.py's/
# scripts/render-site.py's resolve_real_or_example(). Never overwrites a
# real file that exists; only chooses which one to read FROM.
_src() {
  if [ -f "$HERE/$1" ]; then echo "$HERE/$1"; else echo "$HERE/$1.example"; fi
}

getent group my-booking >/dev/null || groupadd -r my-booking
getent passwd my-booking >/dev/null || \
  useradd -r -g my-booking -d /var/lib/my-booking -s /sbin/nologin my-booking

install -d -m 755 /opt/my-booking/app /opt/my-booking/bin
install -m 644 "$HERE"/app/*.py /opt/my-booking/app/
install -m 755 "$HERE"/scripts/my-bt /opt/my-booking/bin/my-bt
ln -sf /opt/my-booking/bin/my-bt /usr/local/bin/my-bt

install -d -m 755 /etc/my-booking
if [ ! -f /etc/my-booking/settings.toml ]; then
  install -m 644 "$(_src settings.toml)" /etc/my-booking/settings.toml
else
  echo "settings.toml already exists -- not overwriting. If $HERE/settings.toml"
  echo "has changes you want (e.g. a new [defaults] key), merge by hand:"
  echo "  vimdiff /etc/my-booking/settings.toml $HERE/settings.toml"
fi

# site/privacy.html.tmpl: same not-overwriting treatment as settings.toml
# above (the RPM ships both as %config(noreplace) -- see packaging/*.spec).
# Without this, `my-bt setup`'s static-site regeneration step (app/
# site_render.py, [site].static_site_dir) would have no template to read
# on an install.sh-installed system -- it fails gracefully (a [fail] line,
# not a crash) but the feature would be silently unusable, which defeats
# the point of this script being a full alternative to the RPM.
install -d -m 755 /opt/my-booking/site
if [ ! -f /opt/my-booking/site/privacy.html.tmpl ]; then
  install -m 644 "$(_src site/privacy.html.tmpl)" /opt/my-booking/site/privacy.html.tmpl
else
  echo "privacy.html.tmpl already exists -- not overwriting. If $HERE/site/privacy.html.tmpl"
  echo "has wording changes you want, merge by hand:"
  echo "  vimdiff /opt/my-booking/site/privacy.html.tmpl $HERE/site/privacy.html.tmpl"
fi

# Email templates: the built-in fallback directory app/email_templates.py
# reads (/opt/my-booking/email_templates) when [site].email_templates_folder
# isn't set. 2026-07-14 (review finding G1): this script never installed
# them at all -- the RPM always did -- so EVERY email send on an
# install.sh-installed system raised FileNotFoundError. Copy-if-missing
# per file, mirroring the RPM's %config(noreplace) treatment: a template
# you've customized in place is never overwritten.
install -d -m 755 /opt/my-booking/email_templates
for tmpl in "$HERE"/email_templates/*.txt "$HERE"/email_templates/*.html; do
  dest="/opt/my-booking/email_templates/$(basename "$tmpl")"
  [ -f "$dest" ] || install -m 644 "$tmpl" "$dest"
done

install -d -m 750 -o my-booking -g my-booking /var/lib/my-booking
install -d -m 700 -o my-booking -g my-booking /etc/my-booking/secrets
# Always the tracked, generic settings.toml.example -- see the matching
# comment in packaging/my-booking-tool.spec for why this must NOT be
# "$HERE/settings.toml" (that could be your real, personal config).
install -m 644 "$HERE"/settings.toml.example /etc/my-booking/settings.toml.example

install -m 644 "$HERE"/systemd/my-booking.service /etc/systemd/system/
install -m 644 "$HERE"/systemd/my-booking-retention.service /etc/systemd/system/
install -m 644 "$HERE"/systemd/my-booking-retention.timer /etc/systemd/system/
# 2026-07-14 (review finding G1): the watchdog and git-snapshot pairs were
# never installed by this script (both postdate it) -- the RPM always
# ships all four; without them an install.sh system silently ran with no
# abuse watchdog and no hourly data-dir snapshot at all.
install -m 644 "$HERE"/systemd/my-booking-watchdog.service /etc/systemd/system/
install -m 644 "$HERE"/systemd/my-booking-watchdog.timer /etc/systemd/system/
install -m 644 "$HERE"/systemd/my-booking-git-snapshot.service /etc/systemd/system/
install -m 644 "$HERE"/systemd/my-booking-git-snapshot.timer /etc/systemd/system/
systemctl daemon-reload

# SELinux: unlike the RPM path, `install` here doesn't go through rpm's
# SELinux plugin, so relabel explicitly (no-op on a non-SELinux box).
command -v restorecon >/dev/null 2>&1 && \
  restorecon -R /opt/my-booking /etc/my-booking /var/lib/my-booking >/dev/null 2>&1 || true

echo
echo "Installed. Run 'my-bt setup' for the full guided remaining steps"
echo "(secrets, nginx, group membership, systemd, SELinux, static site) --"
echo "it checks what's already done instead of always repeating everything."
echo "Add -i/--interactive to be walked through them one at a time."
