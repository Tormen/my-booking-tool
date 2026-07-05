Name:           my-booking-tool
Version:        1.0.0
# Release is a timestamp, not a hand-bumped counter: scripts/build-rpm.sh
# passes --define "build_timestamp <UTC YYYYmmddHHMMSS>" on every run, so
# each build produces a strictly newer NEVRA. That means `dnf install` on
# the freshly built RPM always applies as an upgrade -- even if you didn't
# touch Version -- instead of dnf seeing "already installed, nothing to do"
# for same-Version rebuilds during day-to-day development. Falls back to
# computing its own timestamp if the spec is ever built directly with
# plain `rpmbuild -ba` (still valid, just less precisely reproducible).
%{!?build_timestamp: %global build_timestamp %(date -u +%%Y%%m%%d%%H%%M%%S)}
Release:        %{build_timestamp}%{?dist}
Summary:        Self-hosted recurring-class booking tool

License:        AGPL-3.0-only
# REPLACE-ME (forks): point at your own repo/site once you've published one.
URL:            https://github.com/Tormen/my-booking-tool
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.11
Requires:       nginx
Requires(pre):  shadow-utils

# We create the my-booking system user/group ourselves in %pre (below) via
# useradd/groupadd rather than systemd-sysusers. rpm's automatic dependency
# generator still notices %files below are owned by my-booking:my-booking,
# an account it doesn't otherwise know about, and adds an auto Requires on
# user(my-booking)/group(my-booking) -- which nothing else provides, so
# dnf refuses to install ("nothing provides user(my-booking)"). These two
# Provides lines are what's expected to satisfy that auto-Requires; see
# https://docs.fedoraproject.org/en-US/packaging-guidelines/UsersAndGroups/
Provides:       user(my-booking) = 1
Provides:       group(my-booking) = 1

%description
Self-hosted booking tool for a small set of recurring classes/sessions:
recurring course scheduling from a single settings file, CalDAV
integration for calendar sync (works with any CalDAV provider,
conflict-checking + one calendar event per course occurrence), guest
self-service with cancel links, an admin overview, and GDPR-oriented data
retention/erasure. Also installs the `my-bt` CLI for querying and
managing the CSV data.

See /usr/share/doc/%{name}/README.md after installing.

%prep
%setup -q

%build
python3 -m py_compile app/*.py

%install
rm -rf %{buildroot}

install -d %{buildroot}/opt/my-booking/app
install -d %{buildroot}/opt/my-booking/bin
install -m 644 app/*.py %{buildroot}/opt/my-booking/app/
install -m 755 scripts/my-bt %{buildroot}/opt/my-booking/bin/my-bt

# `my-bt --version` (app/version.py) reads this -- written by
# scripts/build-rpm.sh from `git rev-parse` in the checkout being
# packaged. Always create it (falling back to a clear placeholder) so
# %files below can reference it unconditionally rather than needing a
# conditional %files entry for a file that might not exist.
if [ -f GIT_COMMIT ]; then
  install -m 644 GIT_COMMIT %{buildroot}/opt/my-booking/GIT_COMMIT
else
  echo "unknown (not built via scripts/build-rpm.sh)" > %{buildroot}/opt/my-booking/GIT_COMMIT
fi

install -d %{buildroot}/usr/local/bin
ln -sf /opt/my-booking/bin/my-bt %{buildroot}/usr/local/bin/my-bt

install -d %{buildroot}%{_unitdir}
install -m 644 systemd/my-booking.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-retention.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-retention.timer %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-watchdog.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-watchdog.timer %{buildroot}%{_unitdir}/

install -d %{buildroot}/etc/my-booking
install -m 644 settings.toml %{buildroot}/etc/my-booking/settings.toml
# Always the tracked, generic settings.toml.example -- NEVER whatever
# settings.toml happens to resolve to for this particular build (which,
# for your own real deployment, is your actual real config). Without this
# distinction, "the example" would just be a copy of your real settings on
# your own machine -- correct-looking but not actually a generic example.
# scripts/build-rpm.sh's staging step ensures settings.toml.example always
# exists (it's a tracked file) regardless of who's building.
install -m 644 settings.toml.example %{buildroot}/etc/my-booking/settings.toml.example
install -d %{buildroot}/etc/my-booking/secrets

# The live template my-bt actually reads/writes at runtime (`my-bt setup
# -i`, app/site_render.py) -- a REAL resource, not just documentation, and
# genuinely hand-editable (wording changes), so it gets the same
# %config(noreplace) + .rpmnew-merge treatment as settings.toml (see
# %files below and app/cli_checks.py::check_rpmnew). The %doc/site/ copy
# below is just the "ready to copy to /var/www" reference bundle.
install -d %{buildroot}/opt/my-booking/site
install -m 644 site/privacy.html.tmpl %{buildroot}/opt/my-booking/site/privacy.html.tmpl

install -d %{buildroot}%{_sharedstatedir}/my-booking

install -d %{buildroot}%{_docdir}/%{name}
install -m 644 README.md %{buildroot}%{_docdir}/%{name}/
install -m 644 LICENSE %{buildroot}%{_docdir}/%{name}/LICENSE
# SOLUTION-DESIGN.md and the *-suggestion.html early drafts are NOT
# packaged: they're personal design-rationale/history for one specific
# deployment (real server details, real legal analysis), gitignored for
# the same reason -- see the maintainer's local notes. Only the generic README.md and
# the site/*.html below (real if you have them, else the generic
# .example placeholders -- see scripts/build-rpm.sh) get shipped.
install -d %{buildroot}%{_docdir}/%{name}/site
install -m 644 site/index.html site/privacy.html site/terms.html site/impressum.html %{buildroot}%{_docdir}/%{name}/site/

install -d %{buildroot}%{_datadir}/%{name}
install -m 644 nginx/my-booking.conf %{buildroot}%{_datadir}/%{name}/my-booking.conf.example

%pre
getent group my-booking >/dev/null || groupadd -r my-booking
getent passwd my-booking >/dev/null || \
  useradd -r -g my-booking -d %{_sharedstatedir}/my-booking -s /sbin/nologin my-booking
exit 0

%post
chown -R my-booking:my-booking %{_sharedstatedir}/my-booking /etc/my-booking
chmod 750 %{_sharedstatedir}/my-booking
chmod 700 /etc/my-booking/secrets
# Defensive SELinux relabel: rpm's own SELinux plugin should already label
# these correctly on a stock Fedora system, but re-labeling explicitly here
# means it's still correct even if the package was installed some other way
# (e.g. staged/copied rather than through rpm's normal file-install path).
# `|| :` because restorecon may be a no-op (or absent) on a non-SELinux box.
command -v restorecon >/dev/null 2>&1 && \
  restorecon -R /opt/my-booking /etc/my-booking %{_sharedstatedir}/my-booking >/dev/null 2>&1
systemctl daemon-reload

# $1 is 2+ on an upgrade (reinstall of a newer Release over an existing
# one), 1 on a genuine first install.
if [ "$1" -ge 2 ] 2>/dev/null; then
  systemctl try-restart my-booking.service >/dev/null 2>&1 || true
  echo "my-booking-tool: upgraded; service restarted if running."
fi

# settings.toml and site/privacy.html.tmpl are both %config(noreplace)
# (see %files) -- if you've edited either, rpm never overwrites it on
# upgrade. If the packaged version also changed, rpm instead drops the new
# one alongside yours as <file>.rpmnew, so nothing is silently lost either
# way -- but the new version's changes then need merging in by hand. Flag
# that loudly right here rather than relying on you noticing a spare
# .rpmnew file later (`my-bt status`/`setup` check for these too).
if [ -f /etc/my-booking/settings.toml.rpmnew ]; then
  cat <<'MSG'

my-booking-tool: settings.toml has local changes -- the new
packaged version was saved alongside it, not applied. Merge by
hand, then remove the .rpmnew:
  sudo vimdiff /etc/my-booking/settings.toml \
    /etc/my-booking/settings.toml.rpmnew

MSG
fi
if [ -f /opt/my-booking/site/privacy.html.tmpl.rpmnew ]; then
  cat <<'MSG'

my-booking-tool: privacy.html.tmpl has local changes -- merge:
  sudo vimdiff /opt/my-booking/site/privacy.html.tmpl \
    /opt/my-booking/site/privacy.html.tmpl.rpmnew

MSG
fi

# Lines here are kept short (well under 80 cols, prefix included) on
# purpose: dnf's "Scriptlet output:" display truncates/wraps long lines
# to the terminal width rather than letting them wrap naturally, so a
# too-long line here silently loses its tail (seen in practice with
# lines ~80+ chars getting cut mid-word). Full step-by-step detail (which
# secrets to create, nginx, systemd, SELinux, ...) used to be duplicated
# here as a wall of static text -- now it lives in one place, `my-bt
# setup`, which also checks what's already done instead of always
# repeating everything. That's the single source of truth going forward;
# keep this printed blurb itself short.
cat <<'MSG'

my-booking-tool installed. Next, run:
  my-bt setup
for the full guided steps (secrets, nginx, systemd, SELinux, the
static site). Add -i/--interactive to be walked through them one
at a time. Full detail is also in README.md.

MSG
exit 0

%preun
if [ "$1" = "0" ]; then
  systemctl disable --now my-booking.service my-booking-retention.timer my-booking-watchdog.timer 2>/dev/null || true
fi
exit 0

%postun
# Deliberately NOT removing /var/lib/my-booking or /etc/my-booking here:
# that is booking/registration data (GDPR-relevant) and secret material.
# It must only be deleted by a deliberate, separate decision -- see
# README.md "Uninstalling" section for how to do that on purpose.
exit 0

%files
%defattr(-,root,root,-)
/opt/my-booking/app
/opt/my-booking/GIT_COMMIT
/usr/local/bin/my-bt
%attr(755,root,root) /opt/my-booking/bin/my-bt
%config(noreplace) /opt/my-booking/site/privacy.html.tmpl
%config(noreplace) /etc/my-booking/settings.toml
/etc/my-booking/settings.toml.example
%dir %attr(700,my-booking,my-booking) /etc/my-booking/secrets
%dir %attr(750,my-booking,my-booking) %{_sharedstatedir}/my-booking
%{_unitdir}/my-booking.service
%{_unitdir}/my-booking-retention.service
%{_unitdir}/my-booking-retention.timer
%{_unitdir}/my-booking-watchdog.service
%{_unitdir}/my-booking-watchdog.timer
%license %{_docdir}/%{name}/LICENSE
%doc %{_docdir}/%{name}/README.md
%doc %{_docdir}/%{name}/site/index.html
%doc %{_docdir}/%{name}/site/privacy.html
%doc %{_docdir}/%{name}/site/terms.html
%doc %{_docdir}/%{name}/site/impressum.html
%{_datadir}/%{name}/my-booking.conf.example

%changelog
# REPLACE-ME (forks): use your own name/email on entries for your own real
# builds (this is just packaging metadata, not tracked separately from
# this file, so it's your call each release, same as git commit
# authorship).
* Sat Jul 04 2026 Tormen <tormen@mail.ch> - 1.0.0-1
- Initial package
