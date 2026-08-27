Name:           my-booking-tool
Version:        1.2.0
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

# 2026-07-08/09: the requested zsh-compatible shell auto-complete, once
# reminded it wasn't actually built yet, was identified as a good fit for
# the RPM build -- generated HERE, from THIS EXACT build's own
# scripts/my-bt (MY_BOOKING_HOME=$(pwd) so its `from app import ...`
# resolves against this checkout rather than the installed /opt/my-booking
# path, same trick used to run it standalone before %install has even
# run), rather than hand-maintained or self-installed at runtime -- see
# scripts/my-bt's own generate_zsh_completion()/--print-zsh-completion
# docstrings for the full rationale. This can never drift out of sync
# with the real subcommands/flags a hand-maintained completion file
# would, since it's regenerated from the real argparse tree on every
# single build.
MY_BOOKING_HOME=$(pwd) python3 scripts/my-bt --print-zsh-completion > _my-bt.zsh-completion

# 2026-07-13: the tests were made to run as part of the rpm build itself,
# so `my-bt test` could be dropped -- tests/ isn't installed by
# %install below (never shipped in the final package, same effective
# result as .git being excluded from the source tarball entirely), but it
# DOES land in the extracted source tree %check runs from, since
# scripts/build-rpm.sh's tarball step never excludes it. Pure stdlib
# unittest -- no new BuildRequires needed beyond what %build already
# assumes. A failing test here aborts the whole build (rpmbuild's normal
# %check behavior on a non-zero exit), which is a real safety net that
# didn't exist before: nothing previously stopped a broken build from
# being packaged and installed.
%check
# No -q: rpmbuild's %check output streams straight to the terminal, and
# with -q there's nothing to see for the ~10-20s the suite takes -- looks
# like the build hung. Plain default verbosity prints one .  per test
# (F/E on failure/error) as they run, so there's visible progress and an
# immediate pointer to which test broke, without the much longer
# one-line-per-test output -v would add to the build log.
#
# The version line first: the suite takes ~20-40s, and this is the moment
# the operator is watching the build scroll by. It also stamps the build
# log with exactly WHICH source produced this package -- both GIT_COMMIT
# and SOURCE_STAMP are already staged by scripts/build-rpm.sh at this
# point, so this prints the same string `my-bt --version` will report
# once installed. Note the stamp dates the SOURCE, so an unchanged tree
# built twice prints the same line twice -- that is the intent, not a
# staleness bug.
MY_BOOKING_HOME="$(pwd)" python3 scripts/my-bt --version
# How many "s" to expect among the dots below, so a skip never has to be
# wondered about. Printed rather than only commented: the dots scroll past
# in the build log, and the answer wants to be right next to them.
#
#   1 skip, building as an ordinary user (the normal case here):
#     tests/test_real_settings.py's secret-files check. The secrets
#     directory is root-only BY DESIGN, so a non-root build cannot tell
#     "absent" from "cannot look" -- and saying nothing beats reporting
#     four files as missing when they are all present, which is what an
#     earlier version of that test did.
#   0 skips, building as root: it can stat them, so the check really runs.
#
# Avoiding the skip is therefore just "build as root" -- but that is NOT a
# recommendation: rpmbuild as root lets a misbehaving spec write anywhere
# on the system, which is why the ordinary-user build is the default here.
# The skip costs nothing either: `my-bt admin health`, which DOES run as
# root on the server, checks the same four secret files after install.
#
# A skip count other than the two above is worth reading:
#   python3 -m unittest discover -v 2>&1 | grep '\.\.\. skipped'
# The number of skips is CHECKED, not merely announced (2026-08-27, the
# operator): an unexpected skip means a test stopped running, which is
# indistinguishable from a green build if only the exit status is read.
# unittest exits 0 whether it ran 1880 tests or skipped half of them.
expected_skips=1
[ "$(id -u)" -eq 0 ] && expected_skips=0
echo "expecting $expected_skips skipped test(s) (\"s\" among the dots): the secrets check"
echo "  needs root to see /etc/my-booking/secrets. Build as root to run it too, or leave"
echo "  it -- \`my-bt admin health\` covers those files after install."

# Streamed AND captured: the dots have to stay visible (a silent 40s
# looks like a hung build), and the summary line has to be re-readable
# afterwards to count the skips. PIPESTATUS would be the obvious way to
# get the suite's own exit status through the pipe, but that is a
# bashism and %check runs under /bin/sh -e -- the status file is POSIX.
# `set +e` INSIDE the group: rpm runs %check with errexit, so a failing
# suite aborted this group before `echo $?` could record the status --
# the file was then missing, `cat` failed, and the expected-skips guard
# below never ran at all. The build did stop, but for the wrong reason
# and without the check that explains why.
{ set +e; python3 -m unittest discover 2>&1; echo $? > unittest-status; set -e; } \
  | tee unittest-output.log
test_status=$(cat unittest-status)
[ "$test_status" -eq 0 ] || exit "$test_status"

# "OK", "OK (skipped=1)", "OK (skipped=1, expected failures=2)" -- take
# the number if it is there, zero if it is not.
actual_skips=$(sed -n 's/.*skipped=\([0-9][0-9]*\).*/\1/p' unittest-output.log | tail -n 1)
: "${actual_skips:=0}"
if [ "$actual_skips" -ne "$expected_skips" ]; then
  cat <<MSG

my-booking-tool: FAILING the build -- $actual_skips test(s) skipped, expected $expected_skips.

A skip that was not expected means a test quietly stopped running, which
looks exactly like a passing build if only the exit status is read. Find
out which one, then either fix it or update the expected count here:

  python3 -m unittest discover -v 2>&1 | grep '\.\.\. skipped'

MSG
  exit 1
fi

%install
rm -rf %{buildroot}

install -d %{buildroot}/opt/my-booking/app
install -d %{buildroot}/opt/my-booking/bin
install -m 644 app/*.py %{buildroot}/opt/my-booking/app/
install -m 755 scripts/my-bt %{buildroot}/opt/my-booking/bin/my-bt

# 2026-07-09: email templates were moved to a directory referenced by
# settings.toml, so wording can easily be changed there if needed --
# installed
# one level up from app/ (i.e. directly under /opt/my-booking, mirroring
# site/ below) so app/email_templates.py's own Path(__file__)-relative
# default resolution (".../app/email_templates.py" -> parent.parent)
# finds this same directory whether running from a dev checkout or this
# installed layout. %config(noreplace): these are genuinely hand-editable
# wording, same treatment as site/privacy.html.tmpl below -- an upgrade
# must not silently clobber a customized template.
# 2026-07-14: glob rather than naming each file -- the template set grew
# from 1 pair (cancel_email) to a full sweep across every guest-facing
# email the same day, and a plain wildcard here means the NEXT template
# added doesn't also require a spec edit to actually ship.
install -d %{buildroot}/opt/my-booking/email_templates
install -m 644 email_templates/*.txt email_templates/*.html \
  %{buildroot}/opt/my-booking/email_templates/

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
# SOURCE_STAMP -- the newest mtime across the packaged SOURCE,
# "YYYY-MM-DD_HHMM" UTC. Deliberately NOT the build time (which is what
# Release above already is): this dates the CODE, so two builds of an
# untouched tree report the same string. Written unconditionally for the
# same reason as GIT_COMMIT: %files can then reference it plainly.
if [ -f SOURCE_STAMP ]; then
  install -m 644 SOURCE_STAMP %{buildroot}/opt/my-booking/SOURCE_STAMP
else
  echo "" > %{buildroot}/opt/my-booking/SOURCE_STAMP
fi

install -d %{buildroot}/usr/local/bin
# Relative target (../../../opt/my-booking/bin/my-bt -- /usr/local/bin is
# 3 levels below /, same as /opt), not an absolute
# /opt/my-booking/bin/my-bt one -- functionally identical once installed
# (both resolve to the same file), but rpmbuild's own file-classification
# pass warns "absolute symlink: /usr/local/bin/my-bt -> /opt/my-booking/
# bin/my-bt" for a symlink whose target is an absolute in-buildroot path,
# since that can break if the buildroot is ever relocated/chrooted
# differently. A relative target is what every other RPM's /usr/local/bin
# or /usr/bin symlink to a vendored binary normally uses, and avoids the
# warning outright.
ln -sf ../../../opt/my-booking/bin/my-bt %{buildroot}/usr/local/bin/my-bt

# zsh completion (see %build's own comment on _my-bt.zsh-completion) --
# %{_datadir}/zsh/site-functions is already on every zsh's fpath by
# default on Fedora, so this needs no per-user setup at all: a new shell
# (or `compinit`) just picks it up.
install -d %{buildroot}%{_datadir}/zsh/site-functions
install -m 644 _my-bt.zsh-completion %{buildroot}%{_datadir}/zsh/site-functions/_my-bt

install -d %{buildroot}%{_unitdir}
install -m 644 systemd/my-booking.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-retention.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-retention.timer %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-watchdog.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-watchdog.timer %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-git-snapshot.service %{buildroot}%{_unitdir}/
install -m 644 systemd/my-booking-git-snapshot.timer %{buildroot}%{_unitdir}/

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

# Same reasoning, added 2026-07-10: `my-bt setup`/`status`/`setup -i`
# (app/cli_checks.py::check_nginx_conf_repo_file/_resolve_nginx_conf_checkout_source)
# read THIS installed copy at runtime too (default MY_BOOKING_HOME is
# /opt/my-booking, same as privacy.html.tmpl above) -- a real, personal,
# hand-hardened resource these checks compare the live deployed vhost
# against and offer a vimdiff for, not just documentation. Before this,
# the package never carried it at all, so that vimdiff offer could never
# fire on a stock RPM install no matter how complete the SOURCE checkout's
# own copy was (scripts/build-rpm.sh's materialize-from-.example step
# guarantees this file exists by the time %install runs, same as
# privacy.html.tmpl) -- the whole point of having this file locally.
install -m 644 site/nginx-locations.conf %{buildroot}/opt/my-booking/site/nginx-locations.conf
install -m 644 site/nginx-locations.conf.example %{buildroot}/opt/my-booking/site/nginx-locations.conf.example

# 2026-07-08: to avoid spreading files all across the
# system without good reason, this bare, non-hardened reference (just
# the proxied location blocks, no rate limiting/CSP/security headers) used
# to live under %{_datadir}/%{name} (a separate top-level directory) for
# no reason stronger than "it's pure read-only package data, so FHS says
# _datadir" -- but /opt/my-booking/site already mixes package templates
# (this one, nginx-locations.conf.example) with %config(noreplace) real
# files (nginx-locations.conf, privacy.html.tmpl) the admin is expected to
# read/edit directly, so there's no consistency win left in keeping this
# one file split off on its own. Every nginx reference file now lives in
# the one place.
install -m 644 nginx/my-booking.conf %{buildroot}/opt/my-booking/site/my-booking.conf.example

install -d %{buildroot}%{_sharedstatedir}/my-booking

install -d %{buildroot}%{_docdir}/%{name}
install -m 644 README.md %{buildroot}%{_docdir}/%{name}/
install -m 644 LICENSE %{buildroot}%{_docdir}/%{name}/LICENSE
# SOLUTION-DESIGN.md and the *-suggestion.html early drafts are NOT
# packaged: they're personal design-rationale/history for one specific
# deployment (real server details, real legal analysis), gitignored for
# the same reason -- see the maintainer's local notes. Only the generic
# README.md and the site/*.html below (real if you have them, else the
# generic .example placeholders -- see scripts/build-rpm.sh) get shipped.
#
# site/index_embedded.html is included here unconditionally -- it's
# DERIVED straight from site/index.html itself (real or .example, same
# resolve_real_or_example() fallback scripts/render-site.py already uses
# for privacy.html.tmpl -- see app.site_render.derive_index_embedded_html's
# own docstring), so the rendered output always exists by the time
# %install runs, exactly as safe as the other reference copies on this
# line. Whether a deployment actually USES this page at all is a runtime
# choice (see [site].index_embedded_enabled in settings.toml.example) --
# packaging the %doc reference copy unconditionally doesn't opt anyone
# into anything.
install -d %{buildroot}%{_docdir}/%{name}/site
install -m 644 site/index.html site/privacy.html site/terms.html site/impressum.html site/index_embedded.html %{buildroot}%{_docdir}/%{name}/site/

%pre
getent group my-booking >/dev/null || groupadd -r my-booking
getent passwd my-booking >/dev/null || \
  useradd -r -g my-booking -d %{_sharedstatedir}/my-booking -s /sbin/nologin my-booking

# 2026-07-10: the rpm package now checks that no one is logged in
# currently before proceeding, and fails if there is an open session
# reported by my-bt -- only matters on an UPGRADE ($1 -ge 2, same test
# %post already uses below): a first install has no running service yet
# to protect. Shells out to the OLD my-bt (still fully intact at %pre
# time -- rpm hasn't touched any files yet on an upgrade), which queries
# the live process's own in-memory session list directly over its
# loopback listener (see app/webapp.py::internal_status and scripts/
# my-bt::_print_live_status's "active sessions" line). If the service
# isn't running at all, or the OLD my-bt predates that line's exact
# wording (nothing to grep -> $sessions empty), this fails OPEN -- only
# an actual reported count > 0 blocks the transaction.
#
# 2026-07-13: capture `my-bt status`'s full output ONCE (rather than
# throwing it away and just hand-writing a 3-line message on refusal) --
# it already has the exact same "logged-in users" overview (name/email/
# session start/last activity/timeout, see app.cli_checks.
# active_sessions_rows) `my-bt setup`'s own active-session gate/warning
# shows, so reusing it verbatim here means this message can never drift
# out of sync with that one -- one rendering, not two.
if [ "$1" -ge 2 ] 2>/dev/null && [ -x /usr/local/bin/my-bt ]; then
  status_output=$(/usr/local/bin/my-bt status 2>/dev/null)
  sessions=$(echo "$status_output" \
    | sed -n 's/^active sessions[[:space:]]*: *\([0-9][0-9]*\).*/\1/p')
  if [ -n "$sessions" ] && [ "$sessions" -gt 0 ] 2>/dev/null; then
    echo "my-booking-tool: refusing to upgrade -- $sessions active session(s) right now:" >&2
    echo "" >&2
    echo "$status_output" | sed -n '/^logged-in users:/,$p' >&2
    echo "" >&2
    echo "Wait for them to log out/expire, or force-clear them yourself with the" >&2
    echo "command above, then re-run this upgrade." >&2
    exit 1
  fi
fi
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

# 2026-07-08: this should be enabled by default (manually disabled if
# wished, with the installer informing the installing user that this
# mechanism is part of the rpm package), along with the other recurring
# services installed with the package. Only on a GENUINE first install ($1 ==
# 1) -- never on an upgrade, so an admin who deliberately disabled one
# of these is never silently re-enabled just by upgrading the package.
# my-booking.service itself is deliberately NOT included here: it needs
# real configuration (secrets, CalDAV credentials, nginx) before it can
# actually serve anything, so auto-starting it on a bare fresh install
# would just crash-loop confusingly -- `my-bt admin setup` is what walks
# you through enabling that one once it's actually ready.
if [ "$1" -eq 1 ] 2>/dev/null; then
  systemctl enable --now \
    my-booking-retention.timer my-booking-watchdog.timer my-booking-git-snapshot.timer \
    >/dev/null 2>&1 || true
  cat <<'MSG'

my-booking-tool: these recurring jobs are ENABLED BY DEFAULT (part of
this rpm package, not something you need to set up separately):
  my-booking-retention.timer     nightly GDPR retention purge (03:30)
  my-booking-watchdog.timer      liveness check every 15 minutes
  my-booking-git-snapshot.timer  hourly data-dir git snapshot
Disable any one you don't want:
  sudo systemctl disable --now <unit>

my-bt tab-completion for zsh is also installed (part of this package,
%{_datadir}/zsh/site-functions/_my-bt, already on your fpath) -- open a
new shell (or run `compinit`) to pick it up.

MSG
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
if [ -f /opt/my-booking/site/nginx-locations.conf.rpmnew ]; then
  cat <<'MSG'

my-booking-tool: nginx-locations.conf has local changes -- merge:
  sudo vimdiff /opt/my-booking/site/nginx-locations.conf \
    /opt/my-booking/site/nginx-locations.conf.rpmnew

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
  systemctl disable --now my-booking.service my-booking-retention.timer my-booking-watchdog.timer my-booking-git-snapshot.timer 2>/dev/null || true
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
/opt/my-booking/SOURCE_STAMP
/usr/local/bin/my-bt
%attr(755,root,root) /opt/my-booking/bin/my-bt
%{_datadir}/zsh/site-functions/_my-bt
%config(noreplace) /opt/my-booking/email_templates/*.txt
%config(noreplace) /opt/my-booking/email_templates/*.html
%config(noreplace) /opt/my-booking/site/privacy.html.tmpl
%config(noreplace) /opt/my-booking/site/nginx-locations.conf
/opt/my-booking/site/nginx-locations.conf.example
/opt/my-booking/site/my-booking.conf.example
%config(noreplace) /etc/my-booking/settings.toml
/etc/my-booking/settings.toml.example
%dir %attr(700,my-booking,my-booking) /etc/my-booking/secrets
%dir %attr(750,my-booking,my-booking) %{_sharedstatedir}/my-booking
%{_unitdir}/my-booking.service
%{_unitdir}/my-booking-retention.service
%{_unitdir}/my-booking-retention.timer
%{_unitdir}/my-booking-watchdog.service
%{_unitdir}/my-booking-watchdog.timer
%{_unitdir}/my-booking-git-snapshot.service
%{_unitdir}/my-booking-git-snapshot.timer
%license %{_docdir}/%{name}/LICENSE
%doc %{_docdir}/%{name}/README.md
%doc %{_docdir}/%{name}/site/index.html
%doc %{_docdir}/%{name}/site/privacy.html
%doc %{_docdir}/%{name}/site/terms.html
%doc %{_docdir}/%{name}/site/impressum.html
%doc %{_docdir}/%{name}/site/index_embedded.html

%changelog
# REPLACE-ME (forks): use your own name/email on entries for your own real
# builds (this is just packaging metadata, not tracked separately from
# this file, so it's your call each release, same as git commit
# authorship).
* Thu Aug 27 2026 Tormen <tormen@mail.ch> - 1.2.0-1
- Text macros: define a piece of text once (a studio name, an address, a
  standing note) and use it in course texts, in emails and in the privacy
  page. Three kinds, told apart by the name itself: {{studio}} is yours,
  {{!retention_months}} comes from settings.toml, {{$name}} is supplied by
  the code for one send. A sigil means the system owns the name.
- settings.web-editable.toml, optional and new: the half of the config a
  web process is trusted with ([macros] and [[course]]). settings.toml,
  which holds the CalDAV account, the secret paths and the admin password
  hash, is never written from a browser.
- /admin/settings, reached from the banner: add, rename and edit macros,
  and edit every course field, with a live preview of the description and
  the markup allowlist applied on save. Saved config is live on the next
  request -- nothing is restarted, and the service keeps its last known
  good config if a file will not load.
- privacy.html.tmpl uses the same macro syntax as everything else;
  ${retention_months} is now {{!retention_months}}. Existing templates
  need that one substitution (`my-bt admin health` reports a leftover).
- `my-bt admin health` reports the new file: a parse failure (the site is
  serving older config), and any course defined in both files.
- Fixes: a date already booked was offered twice in /my's booking
  overlay; nine dialogs were never centred; /my/settings validated both
  its forms at once, so a half-typed address blocked saving a name; a
  slow click now shows a loading panel instead of looking ignored.

* Thu Aug 27 2026 Tormen <tormen@mail.ch> - 1.1.0-1
- Future Sessions in the admin console: per-date time overrides, hide and
  cancel, journalled in date_overrides.csv instead of settings.toml
- /my rebuilt: New booking frame with an in-page booking overlay, and
  Upcoming/Past as tabs
- Conflict checks batched: one CalDAV query per page render instead of
  one per candidate date
- Static-site pages are written world-readable (they were 0600, so nginx
  served 403)
- Version now identifies the SOURCE it was built from, and every page
  carries it

* Sat Jul 04 2026 Tormen <tormen@mail.ch> - 1.0.0-1
- Initial package
