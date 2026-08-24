# my-booking-tool

Self-hosted booking tool for a small set of recurring classes/sessions --
built as a lightweight replacement for a third-party group-booking widget.
Stdlib-only Python, CSV storage, CalDAV integration for calendar sync
(works with any CalDAV provider, e.g. mailbox.org, Nextcloud, etc.), attendee
self-service with cancel links, an admin overview, and GDPR-oriented data
retention/erasure. This file is the practical "how to install / operate /
reinstall" reference.

**Disclaimer -- read this first.** This project is provided "AS IS", with
NO WARRANTY of any kind -- see `LICENSE` (AGPLv3) sections 15-16 for the
full legal text. In particular:

- **This is not legal advice, and nothing here is a compliance
  guarantee.** The GDPR-oriented features (retention/erasure, the
  privacy-policy template, the consent checkbox) are a reasonable-effort
  starting point, not a certification that your specific deployment is
  legally compliant. Data protection law, and what it requires for your
  situation, depends on where you and your users are, what you actually
  collect, and how you operate -- only you (optionally with your own
  legal counsel) can determine that for your case. Review and adapt
  every generated legal-page template (`site/*.html.example`) before
  publishing it, and don't rely on this software alone to make you
  compliant with GDPR, other privacy law, or any other regulation.
- **You are the data controller and operator of your own instance.**
  Security, backups, secret management, and the correctness of your own
  configuration are your responsibility.
- No contributor or author of this project accepts liability for how you
  configure, deploy, or operate it. Use at your own risk, and see
  `LICENSE` for the full disclaimer of warranty and limitation of
  liability that applies.

## License

AGPLv3 (GNU Affero General Public License v3) -- see `LICENSE`. In short:
you're free to run, study, modify, and redistribute this, including
running a modified version as a network service for others -- but if you
do run a modified version as a network-accessible service, AGPLv3
requires you to make that modified source available to your users too.

## Generic template vs. your real config

This repo is meant to be both a working tool for one specific deployment
*and* a generic template others can clone and configure for their own.
The split:

- `settings.toml.example`, `site/index.html.example`,
  `site/impressum.html.example`, `site/privacy.html.tmpl.example`,
  `site/terms.html.example`, `site/nginx-locations.conf.example` --
  generic, tracked in git, safe to publish. Placeholder content
  throughout, marked with `REPLACE-ME`.
- `settings.toml`, `site/index.html`, `site/impressum.html`,
  `site/privacy.html`, `site/privacy.html.tmpl`, `site/terms.html`,
  `site/nginx-locations.conf` -- your own real, filled-in versions. They
  are never overwritten, never deleted, and never published by `my-bt` or
  the RPM/install scripts. **Keeping them out of git is up to where you
  put them**: this repo's `.gitignore` hides anything named `*.local`, so
  put the whole set in a `*.local/` directory (next section) and they are
  invisible to git automatically. If you would rather keep them at the
  ordinary paths above, add your own rules for them to `.gitignore`
  first -- nothing hides them there. `%config(noreplace)` in the RPM spec gives the
  same guarantee at the installed-system level for the two of these
  (`settings.toml`, `site/privacy.html.tmpl`) that `my-bt` reads at
  runtime -- see "Installing" below.

**How this repo keeps personal files out of git.** `.gitignore` carries
two rules and nothing else -- `*.local` and `*.local.*`. Name anything
personal that way (the `*.local/` directory below is the tidiest form) and
git will not see it. Byte-code, build output, virtualenvs and editor/OS
droppings are not listed here on purpose: every project you own produces
them, so they belong in your user-level ignore file
(`~/.config/git/ignore`) rather than being repeated per repository.

**Keep your real files together in a `*.local/` directory.** Put the whole
set in one directory whose name ends in `.local` at the repo root,
mirroring the repo's own layout:

    my-booking.local/settings.toml
    my-booking.local/site/index.html
    my-booking.local/site/nginx-locations.conf

Every lookup prefers that copy -- `my-bt admin health`/`admin setup`,
`scripts/render-site.py` (which also writes the generated
`site/privacy.html` and `site/index_embedded.html` there), and
`scripts/build-rpm.sh`, which packages from it. Exactly one such directory
is allowed; two are an error, since which one is meant would be ambiguous.
The code does not require it -- with no such directory everything falls
back to the ordinary paths -- and it never applies to an installed system,
where `my-bt` reads the real files the package already baked in. Two
reasons to use it anyway: `.gitignore` hides it with no per-file rules,
and backup or sync tooling that works on name patterns can carry
`my-booking.local` as a unit, where seven files scattered at ordinary
paths are easy to leave behind. `my-bt admin health` warns if a
file ends up in both places, or if a real file is missing so that a generic
`.example` would be packaged in its place.

`site/nginx-locations.conf` is a real, hardened nginx vhost reference --
a FIXED filename (2026-07-10: renamed from being named after the host's own
domain, specifically so every real-vs-`.example` pair in `site/` follows
the exact same convention -- nginx itself doesn't care what the file on
disk is called, only that it's included). Unlike the others, this one is
entirely optional: `my-bt admin setup`/`admin health` only report on it if
`site/nginx-locations.conf` actually exists, and never auto-generate or
edit it (rewriting a hand-hardened vhost would be worse than asking) --
see `app/cli_checks.py::check_nginx_conf_repo_file()`. This file is
`%config(noreplace)`-packaged into `/opt/my-booking/site/` (2026-07-10;
see "Installing" step 2) specifically so this check -- and `setup -i`'s
vimdiff offer below -- work against a stock RPM install (default
`MY_BOOKING_HOME=/opt/my-booking`), not only when running `my-bt` straight
out of a source checkout via `MY_BOOKING_HOME`.

Want an even stricter check against the file nginx is *actually* running
with, not just this checkout's copy? Set `[site].nginx_conf_path` in your
real `settings.toml` to the absolute path nginx loads it from on this box
(it can be named anything there -- this checkout's own copy is always
looked up by the fixed name above, regardless). `my-bt admin health`/`admin setup`
then read that exact file directly off disk (not `nginx -T`'s merged
dump) and **hard-fail** -- not just warn -- if it's missing a required
location block or still has a leftover `REPLACE-ME` marker, since
configuring this path is a deliberate statement that the file is real and
matters. `setup -i` also offers a `vimdiff` between it and this
checkout's own `site/nginx-locations.conf(.example)` if the two differ.
See `app/cli_checks.py::check_nginx_conf_deployed()`.

Nothing at `nginx_conf_path` yet (e.g. right after changing the setting,
before the real file on the server has caught up)? `my-bt admin health`/
`admin setup -i` parse `nginx -T`'s own "# configuration file `<path>`:" markers
to find which file nginx is *actually* loading this vhost from right now,
and say so instead of a dead-end "not found". nginx itself doesn't care
what a conf.d file is named, so a mismatch here isn't inherently wrong --
`setup -i`'s first, lowest-risk offer is just to correct
`[site].nginx_conf_path` to point at that real file instead of touching
anything on disk. If you'd rather the live file itself match the setting
(e.g. you're enforcing one fixed name across every server), `setup -i`
also offers a vimdiff against this checkout's copy (to reconcile content
first -- e.g. a location block added here but never deployed) followed by
a root-gated rename into place and an offer to
`nginx -t && systemctl reload nginx` to pick it up -- but that's no longer
the only, or the first, option. Content is never rewritten automatically
either way. See `app/cli_checks.py::_live_nginx_conf_file_for_host()`.

First time setting this up? Copy each `.example` file to its real name
and fill it in:

```
cp settings.toml.example settings.toml
cp site/index.html.example site/index.html
cp site/impressum.html.example site/impressum.html
cp site/privacy.html.tmpl.example site/privacy.html.tmpl
cp site/terms.html.example site/terms.html
```

Already have real versions of these from a previous setup? Nothing to
do -- `scripts/build-rpm.sh`, `scripts/install.sh`, and `packaging/*.spec`
all prefer your real files over the `.example` ones automatically,
everywhere they're read from.

`my-bt admin health`/`admin setup` check the live, deployed copies of `site/*.html`
for a leftover `REPLACE-ME` marker or an unsubstituted `${...}` template
placeholder -- catching the mistake of publishing the generic template
without customizing it first (see "Static-site pages" below). They never
inspect or judge the legal wording you actually chose; that part is
entirely on you (see the disclaimer above).

## Layout

```
app/                        the application (stdlib-only Python package)
  config.py                 settings.toml + secrets loader
  atomic_io.py              shared crash-safe file write (temp file + fsync + rename + dir fsync)
  storage.py                CSV read/write, locking, right-to-erasure archival
  slots.py                  weekday/time occurrence math + waitlist-aware capacity
  caldav_client.py          minimal CalDAV client (PROPFIND/REPORT/PUT/DELETE)
  calendar_sync.py          keeps one VEVENT per course occurrence in sync
  ics.py                    minimal iCalendar build/parse
  emailer.py                SMTP client
  security.py               tokens/password hashing, erasure hashing, rate limiting
  erasure.py                GDPR Art. 17 orchestration
  retention.py              GDPR Art. 5(1)(e) purge job (the "cronjob")
  git_snapshot.py           hourly auto-commit of the data dir to its own git repo
  site_render.py            renders site/privacy.html + site/index_embedded.html -- see "Static-site pages"
  maintenance.py            `my-bt admin site-maintenance on/off/status` -- see "Maintenance mode"
  cli_checks.py             `my-bt admin health`/`admin setup` health checks -- pure, unit-tested
  cli_setup.py              `my-bt admin setup`/`admin setup -i` report + walkthrough logic
  version.py                `my-bt --version` (package version + git commit)
  webapp.py                 wsgiref WSGI app / routes
  serve.py                  entrypoint (python3 -m app.serve)

tests/                      unit tests -- run via `python3 -m unittest discover`,
                            or automatically during `rpmbuild` (packaging/my-booking-tool.spec's
                            `%check` section; there's no `my-bt test` anymore)

scripts/
  my-bt                     thin CLI wrapper -- see "The `my-bt` CLI" below
  install.sh                manual/dev installer (fallback -- see "Installing")
  build-rpm.sh              builds the Fedora RPM (the recommended path)
  render-site.py            regenerates site/privacy.html (run by build-rpm.sh)

packaging/
  my-booking-tool.spec      RPM spec

systemd/                    my-booking.service, my-booking-retention.{service,timer},
                            my-booking-watchdog.{service,timer},
                            my-booking-git-snapshot.{service,timer}
nginx/                      my-booking.conf -- location blocks for your vhost
.github/workflows/          CI: runs the test suite on push/PR

settings.toml.example       generic placeholder settings (tracked)
settings.toml               YOUR real settings (gitignored -- see above)

site/
  index.html.example        generic placeholder homepage (tracked)
  impressum.html.example    generic placeholder legal-notice page (tracked)
  privacy.html.tmpl.example generic placeholder privacy-policy template (tracked)
  terms.html.example        generic placeholder participation-terms page (tracked)
  index.html, impressum.html, privacy.html,
  privacy.html.tmpl, terms.html           YOUR real pages (gitignored -- see above)

LICENSE                     AGPLv3, full text
```

`app/cli_checks.py` and `app/cli_setup.py` inject every side effect (prompting,
reading a secret, running a command, checking for root) for testing -- see
`tests/test_cli_setup.py`. `site_render.py` runs at both build time
(`scripts/render-site.py`) and run time (`my-bt admin setup -i`).

## Data durability (hard-reboot / crash safety)

2026-07-15: the VPS this runs on can lose power/hard-reboot at any
time, unpredictably. The relevant question isn't OS-level sync tuning
(that only ever protects everything EXCEPT the one write that was
mid-flight at the exact moment of a crash) -- it's whether every write
this project makes to disk is safe against exactly that write being
interrupted. The scope was clarified to cover ALL writes linked to
my-booking-tool, my-bt and the site -- not just `storage.py`'s CSVs.

**`app/atomic_io.py`** is the one shared, crash-safe write primitive
every module uses (`atomic_write_text()` + `fsync_dir()`): write the new
content to a temp file in the SAME directory as the target, `flush()` +
`os.fsync()` it (new content durable on disk), `os.replace()` it over
the real file (atomic rename -- a reader, or a crash, only ever sees the
old, complete file or the new, complete file, never a torn/partial one),
THEN `fsync()` the containing directory too. That last step matters
because fsyncing the temp file only guarantees the new CONTENT is
durable -- on Linux, the rename() itself isn't guaranteed durable until
the directory's own inode is fsynced as well; without it, a hard power
cut in the narrow window right after `os.replace()` returns could, on
some filesystems/mount options, leave the rename uncommitted, so a
reboot shows the file as it was before that last write. Not corruption
(the old file is never torn), just a possible lost last write in that
window. Best-effort throughout (logs a warning, never raises) since
directory-fsync isn't supported on every conceivable mount.

Every file this project writes at runtime goes through this, or through
`app/storage.py`'s own CSV-specific `_LockedCsv._atomic_write` (same
pattern, plus locking/sanitization/chmod for CSVs -- it delegates its
own directory-fsync step to `atomic_io.fsync_dir` rather than
duplicating it):

- **CSVs** (`users.csv`, `registrations.csv`, `archived/*.csv`) --
  `_LockedCsv._atomic_write`, `app/storage.py`.
- **Config/secrets** -- `settings.toml` edits (nginx_access_log,
  nginx_conf_path) and every secret file (`admin_password_hash`,
  `erasure_pepper`, `caldav_password`, ...) written by
  `my-bt admin setup -i`, `app/cli_setup.py`.
- **Calendar invite format marker** (`.calendar_invite_format_version`)
  -- `app/calendar_sync.py`'s `resync_if_format_changed()`.
- **Maintenance mode** -- the `maintenance.json` flag file (`enable()`/
  `disable()`, the latter also fsyncing the directory after the
  `unlink()`) and the live homepage banner insert/remove
  (`apply_banner_to_file()`), `app/maintenance.py`.
- **Rendered static pages** -- `privacy.html` (build time via
  `scripts/render-site.py` and run time via `my-bt admin setup -i`) and
  the initial copy of `index.html`/`impressum.html`/`terms.html` to
  `[site].static_site_dir`, `app/site_render.py` and `app/cli_setup.py`.
- **git-snapshot's `.gitignore`** in the data dir, `app/cli_setup.py`.

**Multi-file operations are ordered so a crash mid-way is recoverable,
not lossy.** Two places touch more than one CSV as separate, non-atomic
transactions:

- Booking: `upsert_user_for_booking()` (users.csv) always runs BEFORE
  the matching registrations.csv row is written. A crash between the
  two just leaves a harmless orphan user row (invisible, self-heals --
  the upsert is idempotent), never a registration pointing at a user
  that was never persisted. See that method's own docstring -- this
  ordering is load-bearing, don't reorder without reading it.
- GDPR erasure (`erase_user()`): archives the user + their registrations
  into `archived/*.csv` FIRST, and only removes the live rows LAST
  (2026-07-15 -- this used to be the other way round). The old order
  meant a crash right after the live-removal write permanently lost the
  erasure record: gone from the live table, never archived, and
  un-recoverable (a missing `user_id` in `users.csv` makes
  `erase_user()` return `False`, "nothing to do"). Archiving first means
  a crash mid-operation leaves at worst a harmless DUPLICATE (already
  archived, still also live) -- recoverable by just re-running the
  erasure, since the archive-append steps skip rows already present and
  the final live-removal is unconditional either way.

**Directory fsync support is a one-time capability probe, not just a
routine log line.** `fsync_dir()` is deliberately best-effort on every
individual write -- an unsupported mount must never turn a successful
write into a crash, so a failure there is only ever a quiet WARNING.
2026-07-15: on review, a quiet WARNING was judged correct for
availability, but also the kind of failure that's invisible
until the one time it matters -- if the actual production mount
silently doesn't support directory fsync, every write since deploy has
been getting the weaker guarantee with nobody the wiser. That called for
a one-time capability probe at startup that logs loudly, rather than
relying on someone noticing a warning line in a log nobody tails.
Two places react loudly to `app.atomic_io.probe_dir_fsync_support()`
instead:

- **At process startup** -- `app/serve.py`'s
  `check_directory_fsync_support_at_startup()` runs the probe once when
  `my-booking.service` starts, logs at ERROR (not the routine WARNING)
  if it fails, and best-effort emails `admin_email` (reusing the same
  mechanism `app/watchdog.py` already uses, not bolted onto watchdog's
  own 15-minute timer -- that would re-alert on a persistent condition
  every single run instead of once per restart). A failed alert EMAIL
  itself is swallowed (logged as a warning) so an unreachable SMTP
  server at boot can never block the app from actually starting.
- **Any time via `my-bt admin setup`/`admin health`** --
  `app.cli_checks.check_directory_fsync_support`, participating in the
  same fails/warns/repeated-summary/exit-1 policy as every other check
  there, so it's re-checkable on demand, not just once at boot.

None of this protects against disk-level corruption (bad sectors, a
failing drive) or a crash during the retention/erasure/git-snapshot
*commit* step itself (`_git_commit_data_file` is a best-effort safety
net on top of the above, not the primary durability mechanism) -- for
that, see "Off-box encrypted backups" under "Known simplifications"
below. This is specifically about surviving a hard power loss without a
half-written booking, a torn config/secret file, or a lost erasure
record -- and it applies identically whether a write was triggered from
the web app or from `my-bt` (registration/cancellation/rename-course/
gdpr erase/purge/migrate-simplymeet all go through the same `Store`
class either way; `scripts/my-bt` never touches a CSV file directly).

## Installing (and reinstalling after a server reinstall)

The recommended path is the Fedora RPM -- keep this whole directory somewhere
durable (your own git remote, a backup, etc.) and after a fresh OS install:

```
sudo dnf install rpm-build rpmdevtools   # one-time, needs sudo
scripts/build-rpm.sh
sudo dnf install $(find "$HOME/rpmbuild/RPMS" -name 'my-booking-tool-1.0.0*.rpm')
```

That installs the code to `/opt/my-booking`, the systemd units, `my-bt` at
`/usr/local/bin/my-bt`, creates the `my-booking` system user/group, and
prints the remaining one-time steps (it deliberately does *not* auto-start
the service, since secrets don't exist yet on a fresh install).

**Updating after a code change:** run the exact same two commands again
(`scripts/build-rpm.sh` then the `dnf install` line above -- copy/paste,
nothing to remember). Every build's `Release` is a fresh UTC timestamp
(see `packaging/my-booking-tool.spec`), so even without bumping `VERSION`
dnf always sees a newer package and upgrades in place rather than saying
"already installed, nothing to do." On an upgrade (as opposed to a first
install) `%post` also runs `systemctl try-restart my-booking.service` for
you, so the new code is actually running afterwards, not just unpacked.

**Refuses to upgrade while someone's logged in (2026-07-10):** `%pre`
queries the currently-running service's own live session list (`my-bt
status`'s "active sessions" line -- see "The `my-bt` CLI" below) and
aborts the whole `dnf`/`rpm` transaction if it's non-zero, rather than
silently restarting the service (and clearing every session) out from
under someone. Nothing is lost either way -- sessions are in-memory only,
so a restart just logs people out, it doesn't touch booking data -- this
purely avoids the surprise. Only checked on an actual upgrade (a first
install has no running service yet); if the service isn't running, or
you're upgrading from a build old enough to predate this check, it fails
open rather than blocking. Wait for the session(s) to log out/expire, or
force them out with `my-bt admin logout <email>` / `--all` (see "The
`my-bt` CLI" below), then re-run the same install command once `my-bt
status` shows none.

`my-bt admin setup --interactive`'s own "restart my-booking.service now?"
prompt (separate from the RPM's `%pre` gate above -- this one fires any
time settings.toml has changed, not just on a package upgrade) runs the
same check: if sessions are active it refuses and prints the exact
`my-bt admin logout` command to run first, instead of silently
restarting and logging everyone out.

`%post` itself only prints a short pointer to `my-bt admin setup` (see "The
`my-bt` CLI" below) -- that command generates the full list dynamically,
checking what's already done instead of always repeating a static wall of
text (the old approach, where this list was duplicated across `%post`,
`scripts/install.sh`, and this README, drifted out of sync more than once).
Run `my-bt admin setup` any time to see it again, or `my-bt admin setup --interactive`
to be walked through it step by step. What follows is the same list, in
full detail, for reference:

1. Create secrets in `/etc/my-booking/secrets/` (mode 600, owned by
   `my-booking`), four files:
   - `caldav_password`, `smtp_password` -- plain text, your CalDAV/SMTP
     account password(s).
   - `erasure_pepper` -- random hex: `openssl rand -hex 32`.
   - `admin_password_hash` -- **not** plain text, a hash. Generate it with
     `my-bt admin hash-password` (prompts for the password with hidden input --
     it's never typed into a command line, so it never ends up in shell
     history), then save the printed output into the file, e.g.:
     `my-bt admin hash-password | sudo tee /etc/my-booking/secrets/admin_password_hash`.
     `my-bt admin health` (see below) specifically checks for and flags the
     common mistake of pasting the plain password here instead.

   The directory itself is already correctly SELinux-labeled by the RPM
   (and re-labeled via `restorecon` on every install/upgrade), and a file
   created *directly* inside it inherits that label automatically -- but
   `mv` (unlike `cp`) preserves a file's *original* label, so if you draft
   a secret elsewhere first and move it in, run `sudo restorecon -Rv
   /etc/my-booking/secrets` afterwards to be safe.
2. Review `/etc/my-booking/settings.toml`,
   `/opt/my-booking/site/privacy.html.tmpl`, and
   `/opt/my-booking/site/nginx-locations.conf` -- all three are
   `%config(noreplace)` files (reinstalling/upgrading the RPM never
   overwrites your edits to any of them). If the packaged version of one
   also changed since you edited it, rpm can't just pick a side: it saves
   the new version alongside yours as `<file>.rpmnew` instead, and `%post`
   (and `my-bt admin health`/`my-bt admin setup`) flag it loudly so a pending merge
   can't go unnoticed. Merge by hand, then remove the `.rpmnew`, e.g.:
   `sudo vimdiff /etc/my-booking/settings.toml /etc/my-booking/settings.toml.rpmnew`.
   (2026-07-10: `nginx-locations.conf` only just joined this list -- before
   that, the RPM never carried this file at all, so `my-bt admin setup -i`'s own
   vimdiff offer against it could never fire on a stock install, no matter
   how complete your source checkout's own copy was.)

   Every *other* file the package installs (systemd units, app code, the
   nginx example) isn't meant to be hand-edited, so it doesn't get the
   `%config(noreplace)` treatment -- instead `my-bt admin health`/`admin setup` run
   `rpm -V my-booking-tool` (rpm's own file-integrity verifier) and report
   any drift they find there too, so an accidental edit anywhere in the
   package still surfaces instead of silently persisting across upgrades.
3. Add the location blocks from
   `/opt/my-booking/site/my-booking.conf.example` to your existing
   nginx vhost config, then `nginx -t && systemctl reload nginx`. Want a
   fully hardened reference instead of bare location blocks (rate
   limiting, CSP/HSTS/Permissions-Policy headers, an optional admin-IP
   allowlist)? `site/nginx-locations.conf.example` is a real production
   vhost, anonymized -- see "Generic template vs. your real config" above
   for the same real-vs-`.example` convention applied to it.
4. `sudo usermod -aG my-booking <your-login>` so `my-bt` works without sudo.
   Log out and back in (or just open a fresh shell) afterwards -- group
   membership only applies to new sessions, not your current one.
5. `my-bt` tab-completion for zsh is installed automatically as part of
   the package (`/usr/share/zsh/site-functions/_my-bt`, already on zsh's
   default `fpath`) -- same deal as step 4: open a new shell (or run
   `compinit`) to pick it up, nothing else to install.
6. `sudo systemctl enable --now my-booking.service` (the three recurring
   timers below are already enabled by default -- see next point).
7. If SELinux is enforcing (default on Fedora -- check `getenforce`):
   `sudo setsebool -P httpd_can_network_connect on`. Without this, nginx
   (which runs as the confined `httpd_t` domain) is blocked from
   `proxy_pass`-ing to the app's local port, and `/book`, `/cancel`, `/my`,
   `/admin` all 502 even though the app itself is running fine -- confirm
   with `sudo ausearch -m avc -ts recent` if you hit this. The RPM's
   `%post` also relabels `/opt/my-booking`, `/etc/my-booking`, and
   `/var/lib/my-booking` via `restorecon` as a safety net.
8. **Not done by this package** -- your live static site is a separate
   checkout/repo, not this one, so nothing here touches it automatically
   (and `%post` runs on every future upgrade too, which would risk
   clobbering your own later edits there if it did). Copy `site/index.html`,
   `site/impressum.html`, `site/privacy.html`, and `site/terms.html` from
   this repo to your live static-site host, and make sure each course's
   booking link points at `/book/<shortname>` (matching your
   `settings.toml` `[[course]]` shortnames). Nothing is live for real
   users until this step.

Prefer not to build an RPM? `sudo scripts/install.sh` does the same thing
directly (also idempotent), skipping the packaging step.

### Uninstalling

`dnf remove my-booking-tool` stops the services but deliberately leaves
`/var/lib/my-booking` (registrations/users) and `/etc/my-booking`
(secrets) in place -- that's booking data and key material, not disposable
package files, and shouldn't vanish as a side effect of a package removal.
Remove them yourself, on purpose, if you really want to: `sudo rm -rf
/var/lib/my-booking /etc/my-booking`.

## The `my-bt` CLI

Installed on PATH as `my-bt`. Run `my-bt --help` / `my-bt <command> --help`
/ `my-bt admin --help` for the full option list -- every subcommand and
flag has its own short help text. Frequently-used commands (`list`,
`users`, `show`, `stats`, `cancel`, `status`) live at the top level;
rarer, heavier site-administration actions (`hash-password`, `gdpr`
[including `erase`, moved here 2026-07-14], `maintenance`, `git-snapshot`,
`watchdog-check`, `setup`, `health`) are grouped under `my-bt admin`
(2026-07-13 restructuring, `gdpr`/`watchdog-check` added 2026-07-14) so
they don't clutter the commands you reach for daily. The nightly/hourly/
periodic systemd timers (retention, git-snapshot, watchdog) all now run
their work through these same `my-bt admin` subcommands rather than
invoking the `app.*` modules directly, so the on-demand and scheduled
paths can never drift apart.

**Removed 2026-07-14 (GDPR violation):** `my-bt admin dearchive`
(formerly `my-bt merge`) used to permanently re-attach an erased
attendee's pre-erasure booking history onto their new live account --
undoing the point of a GDPR Art. 17 erasure by re-linking data it had
de-linked. It's gone entirely now. The read-only equivalent (`/admin`
and `my-bt list --all`/`--past` merging pre-erasure history into the
display on the fly, nothing written to disk) is unaffected and stays.

`list`/`show` all include a "party" column (guest bookings, 2026-07 -- see
"Guests" under "Booking page layout" below): "+N guest(s)" on the leader's
own row, "guest of `<email>`" on a guest's row, blank for an ordinary solo
booking.

```
my-bt --version                         # package version + git commit it was built from

my-bt list                              # today + future, live only (default -- mimics /admin's own table)
my-bt list --all                        # every date, live + archived (pre-erasure history merged in
                                         # on the fly for any live user -- nothing written to disk;
                                         # this is display-only, nothing persists a merge -- see GDPR notes)
my-bt list --past                       # same merge as --all, occurrence_date strictly before today only
my-bt list --year 2026 --course example-monday-class
my-bt list --status waitlisted --email guest@example.com
my-bt list --format json   # or --format csv
my-bt list --raw                        # every raw CSV column (ids/hashes) instead of the clean default view
my-bt list --all --email guest@example.com   # an attendee's full history, live + pre-erasure combined

my-bt users [--email ...]                # default: live accounts that have actually logged in
my-bt users --live                       # every live account, incl. ones that never logged in
my-bt users --archive                    # only archived accounts
my-bt users --all                        # live + archived combined, no login-date filter (the old default)
my-bt users logout guest@example.com     # same as `my-bt admin logout` -- reachable from here too
my-bt users logout --all                 # same as `my-bt admin logout --all`
my-bt show <anything>                    # auto-detects: registration id, course shortname, YYYY-MM-DD date,
                                          # or a name/email substring -- or force a type: --course/--user
my-bt stats [--year 2026]

my-bt cancel --registration-id <id>                          # asks for confirmation
my-bt cancel --registration-id <id> --yes                    # scripted/non-interactive
my-bt cancel --registration-id <id> -m "course canceled"     # optional message in the cancellation emails
my-bt cancel --date 2026-08-01                                # cancel EVERY live registration on one occurrence
my-bt cancel --date 2026-08-01 --course example-monday-class  # --course only needed if that date is ambiguous

my-bt status                            # live server summary: up/running, maintenance mode, logged-in users

my-bt admin hash-password                # prompts (hidden input), prints the admin_password_hash value
my-bt admin gdpr erase guest@example.com          # asks for confirmation (--email guest@example.com also works)
my-bt admin gdpr erase guest@example.com --yes    # scripted/non-interactive
my-bt admin gdpr                         # overview: retention window(s) + counts past due (bookings+accounts)
my-bt admin gdpr bookings                # list every registration + the date it would be purged
my-bt admin gdpr bookings --purge        # actually delete rows past their retention window
                                          # (same job the nightly systemd timer already runs)
my-bt admin gdpr accounts                # list every live account + the date it reaches retention_months of inactivity
my-bt admin gdpr accounts --purge        # send any due warning emails + erase accounts already past their deadline
                                          # (same job the nightly systemd timer already runs; unconditional --
                                          # runs regardless of whether the warning email is even enabled)
my-bt admin site-maintenance on [-m "back Monday"]  # block new bookings, banner site/index.html
my-bt admin site-maintenance off                    # reopen bookings, remove the banner
my-bt admin site-maintenance status                 # report current state, touches nothing
my-bt admin git-snapshot [--dry-run]     # commit data-dir changes now (same as the hourly timer)
my-bt admin watchdog-check               # run the "strange usage patterns" sweep now (same as the periodic timer)
my-bt admin csp-violations                # full detail on browser-reported CSP violations (see Watchdog below)
my-bt admin setup                        # guided post-install steps -- see below
my-bt admin setup --interactive          # ...or -i: be walked through them
my-bt admin health                       # full install-health diagnostic -- see below
my-bt admin health report [--last 2h] [--since TS] [--till TS]  # aggregate raw logs for a window
my-bt admin health errors [--last 2h] [--since TS] [--till TS]  # ...filtered to actual problems
                                          # (same two, also reachable as `admin log-report`/`log-errors`;
                                          # default window: since nginx's own last restart -- see Watchdog below)
my-bt admin logout guest@example.com     # force-log-out one attendee (every device)
my-bt admin logout --all                 # force-log-out EVERYONE, guest and admin alike

my-bt -D admin gdpr erase guest@example.com   # -D/--debug: full traceback on
                                            # error instead of one clean line
                                            # (same as MY_BOOKING_DEBUG=1,
                                            # just for this one command)
my-bt -L status                            # -L/--log: also append this
                                            # run's output to settings.toml's
                                            # [logging].log_file (on by
                                            # default, see below)
```

There's no more `my-bt test` -- the unit test suite instead runs
automatically during `rpmbuild` (packaging/my-booking-tool.spec's `%check`
section), aborting the build on any failure. Run
`python3 -m unittest discover` directly from a checkout if you want to run
it by hand.

`my-bt admin gdpr erase` only touches the CSVs (no CalDAV dependency by design,
so it works even if your CalDAV/SMTP provider is unreachable); if the
erased attendee had a future confirmed/waitlisted booking, the app's own
cancellation path re-syncs the calendar the next time it touches that
occurrence. If you need the calendar updated immediately after a CLI
erase, restart `my-booking.service` or just wait for the next
booking/cancellation on that occurrence.

If an erased attendee later books again with the same email, they get a
brand-new live account -- their old, erased identity is now just a hash.
`/admin` and `my-bt list --all`/`--past` both show any pre-erasure
registrations sharing that same real email merged onto the new live
account automatically (the merge is automatic as long as the history is
also displayed on the /admin page) -- purely a display-time
merge, computed fresh on every page load/query, nothing written to disk
(2026-07-13: this used to actually rewrite the CSVs on every `/admin` page
load; it doesn't anymore -- see `app/cli_list.py::merge_archived_for_display`).
This is the only merge behavior that exists: `my-bt admin dearchive`
(renamed from `my-bt merge`; `my-bt history` was dropped entirely earlier,
folded into `list --all`/`--past`) used to be a command that actually
PERSISTED a merge -- rewriting the archived registration rows onto the
live user_id for real. Removed entirely 2026-07-14 as a clear GDPR
violation -- permanently re-linking booking history to a live,
identifiable account undoes the point of the Art. 17 erasure that
de-linked it in the first place. The display-time merge above was kept:
it writes nothing, and never touches the archived user row either way
(that old identity's name stays `[erased]` and email stays the hash,
forever, regardless of which merge behavior is in play).

`my-bt cancel --registration-id ...` is the CLI equivalent of the web
admin's cancel button (`/admin` -> Cancel): same status transition (->
`canceled_by_host`), same optional message, and it sends the exact same
cancellation emails to both the attendee and `admin_email` -- there's no
separate email logic to drift out of sync. Cancelable statuses now include
an attendee who hasn't yet clicked their account-confirmation email link
(`pending_confirmation`), not just confirmed/waitlisted (2026-07-13 fix --
this used to be uncancelable by any path). Unlike the web admin path it
does NOT promote the next waitlisted person or re-sync the calendar (no
CalDAV dependency here by design, same reasoning as `my-bt admin gdpr erase`) --
use the web admin, or restart `my-booking.service` (which re-syncs
lazily), if the calendar needs to reflect this immediately.

`my-bt cancel --date ...` ("cancel the entire session", 2026-07-13) cancels
every live confirmed/waitlisted/pending-confirmation registration for one
course occurrence at once (illness, venue unavailable, ...) -- the same
`app.cancel_flow.cancel_occurrence` behind the web admin's per-row "cancel
entire session" checkbox and the no-login
`/host-cancel-occurrence/<course>/<date>` magic link reachable from your
own CalDAV event. `--course` is optional: auto-detected from that date's
own live registrations, erroring with the list of candidates if more than
one course actually has a booking there. Every participant is emailed --
since this is always host-initiated, each one also gets a short apology
("this is the exception, not the rule") plus a link to book the course's
next occurrence, to keep them engaged despite the cancellation.
Canceling an entire session also BLOCKS new bookings for that date
(2026-07-14, verified live: without this, the date reappeared on the
booking page as bookable with full capacity the moment its calendar
event was deleted): a visible **"CANCELED: `<course>`" blocker event**
is placed on your booking calendar at the course hours, and the same
real-time conflict check that already hides dates overlapping any of
your own calendar events (a vacation entry, say) hides this one -- "no
slot shown = no session", and a direct/stale POST for it is rejected
server-side too. Reopen the date by simply **deleting that blocker
event in your calendar app**, or by rebooking any participant (from
`/admin`, or your cancellation email's own rebook link) -- both mean
the session is happening after all. Requirement (checked by `my-bt
admin health`): some blocks-mode `[[conflict_calendar]]` entry must
cover your booking calendar (the shipped config's `source =
"booking_calendar"` entry), or the blocker would never be seen by the
conflict check. See "Calendars" below for the full conflict model.

## Calendars

Two kinds of calendar configuration (2026-07-18 redesign; the old
`[calendar]` section is gone, and the app refuses to start with a clear
migration message if one is still present):

**`[booking_calendar]` -- READ+WRITE, CalDAV only.** Where the tool's
own course events, CANCELED blocker events and sync live
(`caldav_url` + `username` + `password_file` + `calendar`), plus
`trainer_reminder_minutes` / `participant_reminder_minutes` for the two
invite flavors. CalDAV only by nature: published `.ics` links are
one-way exports, there is no writable-ics standard.

**`[[conflict_calendar]]` -- READ-ONLY, any number of entries.** Each
entry is one calendar source consulted live for every candidate date; a
source is either a published ICS link (`ics_url`, e.g. an Outlook "publish
calendar" URL), a CalDAV calendar with its own credentials, or `source =
"booking_calendar"` to reuse the booking connection. Two modes:

- `mode = "blocks"`: any matching event overlapping the `from`--`till`
  window (default: the course's own start/end) HIDES the date -- vacation
  entries, the CANCELED blockers.
- `mode = "requires"` (the default): a SINGLE matching event must span
  the whole `from`--`till` window or the date is hidden -- e.g. "these
  courses only happen when my work calendar shows an out-of-office event".

`from`/`till` work in **both** modes -- the overlap window for `blocks`,
the span window for `requires` -- and both default to the course's own
start/end.

**Course scoping.** An entry applies to every course unless one of two
mutually-exclusive keys narrows it: `courses = [...]` (a whitelist -- only
those shortnames) or `all_courses_but = [...]` (a blacklist -- every
course except those). Both are optional, work on every source kind
(including the `source = "booking_calendar"` block), and are validated at
load time (unknown shortnames, or setting both at once, fail the service
start with a named error).

Scoping a course out of the booking-calendar block does **not** disable
"cancel entire session" for it. The CANCELED blocker event is a
booking-tool internal, not a user-configured conflict, so it is caught by
an always-on check keyed on the blocker's own deterministic UID -- it
hides the date for *every* course regardless of `courses` /
`all_courses_but`. (Only a genuine *personal* event on the booking
calendar respects the scoping.) That check reads the booking calendar
only for a course scoped out of every booking-calendar blocks entry (no
extra query otherwise), and **fails closed**: if the booking calendar is
unreachable, the date is hidden -- no booking is taken while the tool
can't confirm the session wasn't canceled.

Which events count is filtered per entry: `show_as` (default `"oof"`
in requires mode, `"any"` in blocks mode; other values `busy` /
`tentative` / `free` / `workingelsewhere`, matched from Outlook's
`X-MICROSOFT-CDO-BUSYSTATUS`, falling back to RFC `TRANSP` where only
free-vs-busy is detectable), `title_contains`, and three all-day knobs:
`all_day_events_also_count` (on by default -- an all-day vacation entry
blocks, an all-day out-of-office satisfies a requires entry), the
`all_day_non_blocking_title_marker` escape hatch (an all-day event
titled e.g. "Conference Day #course-ok" is ignored; `""` = disabled),
and `all_day_free_events_do_not_block` (an all-day event marked "show
as Free" is ignored -- CAUTION: many calendar apps create all-day
events as Free BY DEFAULT, so a blocking all-day event may need a
manual flip to "Busy"; that trap is exactly why the explicit title
marker exists alongside it). Timed events never get the all-day escape
hatches. All of these filters apply to **every** source kind, ICS links
included -- for an ICS feed the "Free" status comes from the feed's own
`TRANSP` / `X-MICROSOFT-CDO-BUSYSTATUS`. The tool's own synced course
event never blocks its own course (recognized by UID and excluded), so a
date with sign-ups stays bookable for further participants.

Two per-entry keys are ICS-source-only and ignored for CalDAV /
`booking_calendar` sources: `cache_minutes` (in-process fetch-cache TTL,
default 10) and `debug` (verbose per-fetch trace, below).

ICS feeds are parsed in full (recurring events with EXDATEs and
moved/edited instances included, Windows timezone names resolved from
the feed's own VTIMEZONE rules) and cached in-process for
`cache_minutes` (default 10). **Source errors:** every successful ICS
fetch is stored as a last-known-good copy under
`/var/lib/my-booking/conflict_cache/`; if the source goes down,
bookings continue against that copy indefinitely and a
`WARNING:`-subject email goes to `admin_email` -- at most one per day
per source. No cached copy yet (or a CalDAV conflict source failing)
hides the affected dates instead, fail-closed, with the same email.
When a source that had been failing is read successfully again, a
one-off `RESOLVED:`-subject email follows (the "calendar is back"
notice), reporting how long it was down; recovery also resets the
once-per-day rate limit, so a fresh incident later alerts immediately.
Both transitions leave greppable lines in the log
(`/var/lib/my-booking/my-booking.log`, timestamps in UTC): failures at
`ERROR` (`... CalDAV error: ...`) and `sent alert email`, recovery at
`... RESOLVED -- reachable again ...` and `sent RESOLVED email` -- e.g.
`grep -E "CalDAV error|RESOLVED|sent .* email" my-booking.log` shows the
full down/up history of every source.
`my-bt admin health` shows the live state of every source at any time.
One caveat for Outlook published links: Microsoft regenerates them on
ITS own schedule, so the feed can lag the real mailbox by minutes to
hours.

**Seeing what a feed change actually did (2026-07-22).** Two lags sit
between editing your work calendar and the booking page reflecting it:
Outlook's own publish delay (minutes to hours), then the app's
`cache_minutes` in-process TTL. The booking page only re-fetches an ICS
source once that TTL has expired on a page request -- reloading inside
the window changes nothing on disk. To force a fresh fetch immediately,
`sudo systemctl restart my-booking` (this clears the in-process cache;
the very next `/book/...` request re-fetches), then reload the page.

To confirm the *upstream* feed independently of the app, fetch the
published link directly:

```
curl -s '<the ics_url from settings.toml>' | grep -i 20260722   # your date, YYYYMMDD
```

On each **changed** fetch the app now keeps the immediately-previous copy
alongside the current one, so you can diff exactly what just arrived:

```
diff /var/lib/my-booking/conflict_cache/<name>.ics.prev \
     /var/lib/my-booking/conflict_cache/<name>.ics
```

`<name>` is the `[[conflict_calendar]]` `name` (e.g. `dbg-work`). The
`.prev` file is written only when the content genuinely changes, so it
always holds the last *distinct* version rather than being overwritten
with an identical copy on every routine TTL refresh; a first-ever fetch
has no `.prev` yet. Both files live under `conflict_cache/`, which is
excluded from the data-dir git snapshots.

**Tracing fetches in the log (2026-07-22).** With `MY_BOOKING_DEBUG=1`
each ICS source logs one `DEBUG` line per *actual* network fetch --
`conflict feed 'dbg-work': fetched N bytes (changed -> rotated .prev |
unchanged -> .prev kept)` -- plus a `served from in-process cache` line
each time a page consult reuses the cached parse instead. Counting those
for a single `/book/...` request is how you confirm a page load fetches
the feed only once. A **failure** fetching a source, or writing its
`.prev`, is logged at `ERROR` and so stays visible in the default
(debug-off) log; only the routine success line is DEBUG-gated.

**Per-source verbose trace (`debug = true`).** Set `debug = true` on a
single `[[conflict_calendar]]` block and restart the service to get a full
`WARNING`-level trace of every fetch of *that* source (no `MY_BOOKING_DEBUG`
needed) -- millisecond-timestamped, one block per fetch:

```
[conflict-debug dbg-work] pid=1234 ===== FETCH BEGIN =====
[conflict-debug dbg-work] pid=1234 BEFORE  .ics      size=1112238 mtime=... sha256=...
[conflict-debug dbg-work] pid=1234 BEFORE  .ics.prev size=1076837 mtime=... sha256=...
[conflict-debug dbg-work] pid=1234 BACKUP  /bin/cp -a .ics .ics.prev -> rc=0
[conflict-debug dbg-work] pid=1234 AFTER-CP .ics.prev size=1112238 mtime=... sha256=...
[conflict-debug dbg-work] pid=1234 FETCHED 1112238 bytes in 2.9s sha256=...
[conflict-debug dbg-work] pid=1234 AFTER   .ics      size=1112238 mtime=... sha256=...
[conflict-debug dbg-work] pid=1234 ===== FETCH END =====
```

In this mode the current `.ics` is copied to `.ics.prev` with a real
`/bin/cp -a` (preserving mtime/owner/SELinux context) **before** the
network fetch, so `.ics.prev` is byte-identical to the pre-fetch `.ics`.
The `pid` and the `FETCH BEGIN/END` pairing make it unmissable if one
`/book/...` request fetches the feed more than once (two blocks, same pid),
or if a second process is writing the same cache (different pid).
Diagnostic only -- deliberately noisy; turn it off again afterward.

**Undoing a cancellation:** both the attendee's own `/my` page and the web
admin's `/admin` overview show a "Rebook" button (2026-07-10; relabeled
from "Reinstate" 2026-07-14, chosen as a simpler, more
intuitive word than "reinstate" -- the underlying routes/functions below
still say "reinstate", only the visible text changed) on any canceled
booking whose occurrence is still in the future. It's an undo, not a
reschedule to a different date -- it puts the SAME registration back to
confirmed (or waitlisted, if the class filled up in the meantime),
re-checking capacity fresh at the moment you click it. Attendees can
only rebook their own bookings; the admin can rebook anyone's (handy
when an attendee cancels by mistake and asks you to fix it). Same
confirm-dialog-with-optional-comment flow as Cancel -- whatever you type
is emailed to the other side in a light-grey box, and the admin's own
dialog shows the attendee's email address next to their name so you can
tell same-named attendees apart before acting.

Every cancellation email also carries its own no-login "Rebook this
booking" link straight to a dedicated page (same What/When/Where recap +
optional comment + confirm button as the popup, just as a real page
since email can't open one) -- the participant's copy links to
`/reinstate/<token>` (a fresh, single-use token minted at that specific
cancellation -- not the original booking's own cancel token, whose
plaintext is never kept around), and the admin's own copy links to
`/host-reinstate/<reg_id>`, gated the same way `/host-cancel/<reg_id>`
already is (an unguessable ID, no login wall). **New nginx locations**
(`/reinstate/`, `/host-reinstate/`) are needed for these -- see
`nginx/my-booking.conf`; `my-bt admin health` flags them if missing.
No CLI equivalent yet.

**Submission feedback (2026-07-11):** every form in the app -- Cancel,
Rebook, booking, account settings, delete-account, login/signup,
all of it -- disables every button on the page and relabels the one you
clicked "Please wait..." the instant it's submitted. This is a plain
(non-AJAX) form POST followed by a full-page redirect, so there's a real
gap (occasionally a couple of seconds, e.g. while cancellation emails are
sent) between your click and the new page loading; without this, the old
page's buttons stayed fully clickable the whole time with no sign
anything had happened. It's a client-side courtesy, not the actual safety
net -- every mutating route already treats a repeat submission as a safe
no-op server-side -- but it stops a slow request from looking broken and
stops an impatient second click from doing anything at all. One shared
script (`app/templates.py::page()`) covers every current and future
form automatically; see `site/nginx-locations.conf.example`'s CSP
comment if you're hand-maintaining your own vhost, since this adds a
fifth allow-listed inline-script hash.

### `my-bt status`

A fast summary -- whether the process is actually up and
answering requests right now (queried directly over HTTP on its own
loopback port), whether maintenance mode is on (highlighted if so), and
who's currently logged in (any unexpired session, with since-when-connected
and their current/last-loaded page). 2026-07-14: followed by an
"activity (last 24h)" block -- accounts logged in and bookings made
(from the CSVs), nginx request/4xx-5xx counts (the vhost access log,
same combined-format parsing the watchdog uses), and app WARNING+ line
counts ([logging].log_file when configured, else the service journal --
one source, never both, since the same events land in each).
Deliberately COUNTS only, never log dumps, so `status` stays fast; its
own output points at the deeper commands. This is NOT the deep
install-health diagnostic (2026-07-13 -- that content moved to
`my-bt admin health`, see below), and the actual log lines live in
`my-bt admin log-errors --last 24h` / `admin log-report --last 24h`.

### `my-bt admin health`

A guided health check across the whole install -- run this first whenever
something seems off, or after any install/reinstall (this is what plain
`my-bt status` used to print, 2026-07-13):

- `settings.toml` parses, and how many `[[course]]` blocks it has.
- Whether `settings.toml.rpmnew` or `privacy.html.tmpl.rpmnew` is sitting
  unmerged (see "Installing" above).
- Each of the four secret files: exists, mode 0600, non-empty -- and for
  `admin_password_hash`/`erasure_pepper` specifically, whether the
  *content* looks right (catches, for example, accidentally pasting the
  plain admin password into `admin_password_hash` instead of its hash --
  see "Installing" above for the difference).
- The data directory exists and is writable.
- The configured log file (if any) is writable.
- Your login is in the `my-booking` group.
- `my-booking.service`, `my-booking-retention.timer`,
  `my-booking-watchdog.timer`, and `my-booking-git-snapshot.timer`:
  enabled and active.
- Whether `settings.toml` has been edited more recently than
  `my-booking.service` last (re)started -- it's only read once, at
  startup, so an edit made after that isn't live yet even though the file
  on disk is already correct (a stale-in-memory config, not a bug --
  `admin setup -i` offers to restart the service for you).
- SELinux: enforcing or not, and if enforcing, whether
  `httpd_can_network_connect` is on (see the SELinux note above).
- `rpm -V my-booking-tool`: report-only integrity check across every file
  the package owns (not just the two `%config(noreplace)` ones) --
  flags any other packaged file (systemd units, app code, ...) that's
  been hand-modified since install, so drift there doesn't go unnoticed
  either. Doesn't block anything; it's a heads-up, not an enforcement.
- Whether `[watchdog].nginx_access_log` matches nginx's own live config
  (offering to add/detect it if not), and if set, whether the
  `my-booking` user can actually read it (see "Watchdog" above --
  `admin setup -i` offers to fix both).
- If `[site].static_site_dir` is set: whether the live `privacy.html` at
  that path actually matches what current `settings.toml` values would
  render (see "Static-site pages" below) -- catches a `retention_months`
  edit that hasn't been pushed out to the live page yet -- and, if
  `[site].index_embedded_enabled` is on (see "Static-site pages" below),
  whether the deployed `index_embedded.html` matches what the LIVE
  `index.html` + current `settings.toml` would currently derive -- and
  whether any
  live `site/*.html` page still contains a leftover `REPLACE-ME` or
  `${...}` placeholder (i.e. the generic template was published without
  being customized).
- Whether the data directory (`--data-dir`) is already protected by its
  own, separate git repository (see "Data dir git snapshot" below) --
  `admin setup -i` offers to initialize one.
- Whether the data directory actually supports directory fsync (see
  "Data durability" above) -- a marker-file/local-fd probe, no network
  call, same reasoning as the calendar-invite-format check below.
- Group ownership + SELinux file context for `data_dir` itself,
  `[logging].log_file`, and `[site].static_site_dir` (2026-07-16) -- one
  shared check (real `os.stat()`, never `os.access()`, for the same
  root-masking reason as the CSV-ownership check above) applied to every
  data path the service reads or writes, not just `*.csv` uid. Flags a
  path whose group isn't `my-booking` (how a DIFFERENT process, e.g.
  nginx reading `static_site_dir`, is meant to reach it without running
  as the `my-booking` user itself), and -- only when SELinux is
  Enforcing -- a file context that doesn't match what `matchpathcon`
  says policy expects (needs `policycoreutils-python-utils`, same
  package `setsebool` already comes from). `admin setup -i` offers
  `chgrp -R`/`restorecon -Rv` to fix either, root-gated same as the CSV
  chown step. Any FUTURE settings.toml-configurable directory goes
  through this exact same check.
- Whether any occurrence failed to resync on the last calendar-invite
  resync attempt (persistent CalDAV conflict) -- a marker-file check, no
  network call, see "Per-occurrence resync failures" below.
- Browser-reported CSP violations (`[logging].log_file`, on by default --
  see "Watchdog" above), and, separately, whether every inline `<script>`
  hash this app can currently produce is actually allow-listed in the
  live nginx CSP header right now (`app.cli_checks.expected_csp_hashes()`/
  `check_csp_hashes_deployed()`, computed from source -- catches a
  forgotten hash update BEFORE a browser has to report the violation
  itself; see "Static-site pages" -> "CSP hash automation" below).

Each line is `[OK]`/`[WARN]`/`[FAIL]` with a one-line fix where relevant;
**exits non-zero if anything is `[WARN]` or `[FAIL]`** (2026-07-10 --
previously only a `[FAIL]` did, but a `[WARN]` can still be a real,
actionable gap -- e.g. a missing nginx `location` block silently makes a
whole route unreachable -- so `my-bt admin health && <next step>` in a
script/cron/CI context now actually catches it instead of quietly
continuing). Only a fully clean report exits 0. Deliberately doesn't touch
the network/CalDAV (same reasoning as `admin gdpr erase` -- no CalDAV
dependency by design), so it still works to narrow things down even if
your CalDAV/SMTP provider itself is unreachable.

### `my-bt admin setup` / `my-bt admin setup --interactive`

The same checks `admin health` runs, reorganized as a guided post-install
list (secrets, `.rpmnew` merge, a `settings.toml` values summary, nginx,
group membership, systemd, SELinux, the static site, live CalDAV calendar
names, the watchdog's nginx access log, the data dir git snapshot, and
`-i`-only, the calendar invite format) -- this is the single source of
truth for those steps now;
`%post` and `scripts/install.sh` just
point here instead of each keeping their own copy of the text (which used
to drift out of sync). The logic itself lives in `app/cli_checks.py` (the
check functions) and `app/cli_setup.py` (report-printing and the
interactive walkthrough) -- `scripts/my-bt` is just a thin
argument-parsing wrapper around them, which is also what makes them
unit-testable (`tests/test_cli_checks.py`, `tests/test_cli_setup.py`)
without needing a real tty/root/systemd/rpm.

Plain `my-bt admin setup` prints the list, annotating each item with
whatever `admin health` would say about it, so a re-run after partial
setup shows only what's actually left. It ends with the same rollup line
`admin health` prints (`N problem(s), N warning(s)`, or `all checks
passed`) and exits non-zero on any WARN or FAIL, same policy as `admin
health` -- so `my-bt admin setup && <next step>` is a safe gate to
script/cron against, not just a human-readable report. `-i`/`--interactive`
gets the exact same exit-code behavior (reflecting the CURRENT state after
whatever `-i` just fixed, not what it started at) -- `my-bt admin setup -i
&& <next step>` is just as safe to chain as the plain form. Add
`-i`/`--interactive` to be walked through it step by step and have `my-bt`
perform what it safely can:

- Missing secrets: prompts and writes them (hidden input for passwords;
  offers to auto-generate `erasure_pepper`; reuses the same hashing as
  `my-bt admin hash-password` for `admin_password_hash`), mode 0600.
- A pending `settings.toml.rpmnew` or `privacy.html.tmpl.rpmnew`: offers to
  open `vimdiff` for you.
- Group membership, enabling the systemd units, and the SELinux boolean:
  offered when run as root (needs `sudo my-bt admin setup -i` for these --
  without root it tells you the exact command instead of guessing).
- nginx: never automated (editing your existing, hand-maintained vhost
  isn't something to guess at), always shown as a reminder.
- The static site: if `[site].static_site_dir` is configured, offers to
  (re)generate `privacy.html` there from the current `settings.toml`
  values right away -- no rebuild/reinstall needed. See "Static-site
  pages" below.
- Calendars: if `[booking_calendar]` is fully configured, does a live
  PROPFIND against your CalDAV server and reports whether the booking
  calendar and every CalDAV `[[conflict_calendar]]` name actually
  exists there right now (ICS entries get a live fetch+parse check, and
  a missing blocks-mode entry covering the booking calendar is warned
  about) -- catches a
  calendar that was renamed, reset, or never created on the provider's
  side, which otherwise 500s every single `/book/<shortname>` page with
  no earlier warning. Informational only (there's no safe guess at which
  real calendar you meant), and -- unlike every other step above -- this
  one does reach out over the network, so it's skipped entirely if the
  CalDAV secret isn't set up yet.
- Data dir git snapshot: if the data directory isn't a git repo yet,
  offers to initialize one right there (`git init`, a `.gitignore`
  excluding `*.tmp`, local `user.email`/`user.name`, and an initial
  commit) -- not gated behind root, since this only needs filesystem
  write access to the data directory, which the `my-booking` group
  already grants. See "Data dir git snapshot" below.
- Calendar invite format (2026-07-14, `-i` only): if CalDAV is fully
  configured, automatically re-syncs every future occurrence's HOST
  calendar event (same as `my-bt admin resync-calendar`, run by hand)
  whenever `app.calendar_sync.CALENDAR_INVITE_FORMAT_VERSION` has changed
  since the last time this ran -- covers the "on install" half of the
  standing calendar-invite-format rule (see SOLUTION-DESIGN.md section
  24); the "next time you touch it" half was already automatic. Never
  prompts -- idempotent and a no-op once caught up, so there's nothing
  for a human to weigh in on. Best-effort: a network/CalDAV hiccup is a
  `[warn]`, not a crash. The read-only half of this (has the marker
  caught up with the current format version?) is a proper structured
  Check (`cli_checks.check_calendar_invite_format()`), so it's also
  step 13 of plain `admin setup`'s report and part of `admin health` --
  a stale marker counts towards fails/warns, gets repeated in the
  "Warnings/failures, repeated from above" summary, and causes exit 1
  on either command, same as any other check here (2026-07-15: after a
  `[warn]` here didn't stop `setup -i` from reporting "Done -- all checks
  pass now", it was decided that setup and health should both (a) repeat
  any warn or error at the end and (b) exit 1 to fail on any warning or
  error).

**Per-occurrence resync failures are now tracked and surfaced, not just
counted (2026-07-16).** On a real production run, a bulk resync hit
persistent CalDAV conflicts (HTTP 412, likely from concurrent live-guest
traffic during the same upgrade) on 3 occurrences, logged only as a
`WARNING` each -- yet `setup -i` still printed `[ok] calendar invite
format changed -- resynced 6 upcoming occurrence(s)` and exited 0, even
though step 13's output plainly showed partial failures that went
unnoticed. Root cause: `resync_all_future_calendar_events()`/
`resync_if_format_changed()` used to return a plain `int` (occurrences
fixed), which structurally discarded which occurrences failed -- no
caller could ever report a partial failure. Fixed two ways:

- Both functions now return a `ResyncResult(fixed, skipped)` dataclass
  instead of a bare `int`. `setup -i`'s step 13 now prints `[warn]
  ... resynced N upcoming occurrence(s), but M FAILED (persistent CalDAV
  conflict)`, naming each failed occurrence, whenever `skipped` is
  non-empty -- it can no longer print `[ok]` or let the run exit 0 while
  hiding a real failure. `my-bt admin resync-calendar` (the manual,
  network-only command) prints the same failure list and now `exit(1)`s
  if anything was skipped, instead of always exiting 0.
- Skips are also persisted to a `.calendar_invite_resync_skipped` marker
  file in the data dir (`app.calendar_sync.record_resync_skips()`),
  parallel to the existing format-version marker, and read back by a new
  structured Check, `cli_checks.check_calendar_invite_resync_skips()` --
  step 13 of `admin health`/`admin setup` (no CalDAV round-trip, no
  gating on CalDAV being configured, since the marker only exists after a
  real resync ran). This is what actually closes the gap identified above:
  the skip stays a `[warn]` -- counted, repeated in the summary, exit-1 --
  in *every later* `admin health`/`admin setup` run, not just the one run
  that happened to discover it. A clean resync afterwards clears the
  marker automatically.
- A pending skip marker is now, by itself, enough to trigger `setup -i`'s
  automatic resync (2026-07-16) -- see `resync_if_format_changed()`'s
  own docstring. Previously it only ran when the format-version marker
  was stale, so a skip marker left over from an ALREADY-FIXED underlying
  problem (e.g. the SEQUENCE incident above) would just sit there
  forever as a `[warn]`/exit-1 -- nothing would ever re-attempt it,
  since the format marker already matched. This raised the question of
  why `my-bt setup` shouldn't just retry this itself, instead of requiring
  a separate manual `admin resync-calendar` run to find out a fix
  actually worked -- a fair point. Now a non-empty skip marker is its own independent trigger,
  same standing as a stale format-version marker: `setup -i` retries
  known failures itself instead of leaving a human to remember to.

**Retrying harder was tried and deliberately reverted (2026-07-16).** An
earlier version of this gave the bulk resync path (used by both `-i` and
`admin resync-calendar`) more attempts with increasing backoff than a
live booking/cancel request gets, reasoning that 3 unrelated occurrences
conflicting in the same run was probably just an active concurrent
writer that hadn't finished yet. Further review pushed back on that
explanation, arguing there had to be another underlying problem with the
calendar sync, and that the fix should be to collect DEBUG output rather
than retry more aggressively -- rightly skeptical that
more patience was the correct fix for something that hit 3 *different*
occurrences at once, a pattern just as consistent with a real,
structural bug as with a slow concurrent writer, and one that retrying
harder can't distinguish. So the bulk resync path now uses the exact
same `_SYNC_CONFLICT_MAX_ATTEMPTS` (3 attempts, zero delay) as a live
request -- no special case. In its place, the conflict-handling code now
collects much richer diagnostics at DEBUG level (enable with `my-bt -D`
or `MY_BOOKING_DEBUG=1`, see "Logs & debugging" below), so the *next*
occurrence of this can actually be root-caused instead of just outlasted:

- `sync_occurrence()` logs, on every conflict, both the ETag it attempted
  with and the server's ETag right after re-reading it -- explicitly
  flagged `[UNCHANGED -- ...]` if they're identical, which is NOT what a
  genuinely concurrent writer racing us should produce (that writer's own
  change should yield a *new* ETag each time). An unchanging ETag across
  retries points at something else being wrong -- e.g. our own
  etag-comparison logic, or a mismatch between the href we assume for an
  event (`<uid>.ics`) and the href the server actually reports it under.
- `app.caldav_client.query_events()` now also logs each event's uid, etag,
  and the href the server reported it at, next to the href
  `put_event()`/`delete_event()` would actually write to -- if those
  routinely differ, that's a real, structural explanation for a
  conflict that no amount of retrying could ever resolve.
- `app.caldav_client.HttpTransport` now logs the full request headers
  (minus `Authorization`) and the full, untruncated response body/headers
  on any HTTP error status (412 included), not just the
  200-char-truncated message that ends up in the warning line -- a
  WebDAV/CalDAV error body normally names exactly which precondition
  failed.

None of this is on by default (production runs at `WARNING` unless
`MY_BOOKING_DEBUG=1` is set -- see "Logs & debugging" below) to avoid
flooding the journal on every routine request; turn it on ahead of the
next `my-bt admin resync-calendar -D` (or set `MY_BOOKING_DEBUG=1` in
the service's environment before a `setup -i`) if this recurs.

**Root cause found and fixed (2026-07-16), from the very DEBUG output
above.** Running with `-D` against the actual production log
showed this was never a race in the first place: the re-read ETag was
IDENTICAL on every single retry (not different, as a real concurrent
writer would produce), and the server -- mailbox.org's Open-Xchange --
named the real reason in its own error body: `"Concurrent modification
[id 1081, client sequence 0, actual sequence 1]"`. Every single UPDATE
to an already-existing operator event failed with HTTP 412, every time,
while the one occurrence with no prior event (a brand-new create)
succeeded fine. `calendar_sync.sync_occurrence()` built its VEvent with
the field default `sequence=0` (RFC 5545 SEQUENCE) and never
incremented it -- so the FIRST PUT for an occurrence (server also at 0)
succeeds, and every PUT after that keeps sending the same stale
SEQUENCE forever while the server's own tracked value has moved past
it. Open-Xchange enforces that as a genuine, and permanent, conflict --
independent of the ETag/If-Match matching correctly -- so no amount of
retrying (3 attempts or 300) could ever have fixed it; only a correct
SEQUENCE could.

Fixed in `app.ics.parse_sequence()` (new, mirrors `parse_uid()`) plus
`sync_occurrence()`: the occurrence's CURRENT event is read (uid, etag,
*and* sequence, off the same `query_events()` call already used for the
ETag) before building the PUT body, and the new event is sent with
`current_sequence + 1` (or plain `0` for a brand-new event -- nothing
to increment past). Re-read on every retry attempt too, not just once
up front, in case the server's own tracked sequence moved again in the
meantime. This is what actually "makes the calendar sync work," as
opposed to the reverted retry-harder attempt above -- the DEBUG
diagnostics from that revert are exactly what pinpointed this, and stay
in place for any future, genuinely different incident.

The closing "Done." line (2026-07-08) re-checks everything fresh (so it
reflects whatever the walkthrough just fixed, not the state at the start)
and says so: `"Done -- all checks pass now..."` if clean, or
`"Done -- N problem(s), M warning(s) still need attention..."` if not --
previously this was a flat "Done." with no relation to what was just shown.

## Migrating history from SimplyMeet.me (one-off)

`scripts/migrate-simplymeet-history.py` imports a SimplyMeet.me "List view"
CSV export into this tool's own data directory -- written for the 2026-07
cutover away from SimplyMeet.me and kept around for reference, not a
`my-bt` subcommand (see SOLUTION-DESIGN.md entry #28 for why: it's run
once, not a piece of permanent CLI surface). It is NOT installed by the RPM
-- run it straight from a checkout of this repo:

```
scripts/migrate-simplymeet-history.py path/to/export.csv
  # dry run: parses the export and prints exactly what it WOULD do,
  # writes nothing

scripts/migrate-simplymeet-history.py path/to/export.csv --commit
  # actually writes -- safe to re-run (including with --commit): rows
  # already imported are detected and skipped, never duplicated

scripts/migrate-simplymeet-history.py path/to/export.csv --commit \
  --settings /etc/my-booking/settings.toml --data-dir /var/lib/my-booking
  # both flags already default to those paths; only needed if yours differ
```

Only imports bookings whose occurrence is strictly before today (the same
"past" cutoff `/admin` and `my-bt list --past` already use) -- "all except
future bookings," per the original ask. A SimplyMeet.me "Meeting type"
column value is matched against your `settings.toml` `[[course]]` titles in
three tiers: exact string match, then case/whitespace-insensitive match,
then a fuzzy match (if the wording drifted slightly since the export was
taken, e.g. a course title edited in `settings.toml` afterward). Anything
still unmatched is skipped and listed in the report rather than guessed at
(fix the mismatch in either place and re-run). Any non-exact (tier 2/3)
match is also listed in the report -- double-check those before trusting
`--commit`, since a fuzzy match could in principle pick the wrong course.
If two configured courses are equally close to one "Meeting type" value,
that's treated as no match (never guessed) and flagged as ambiguous.

Before running with `--commit`, read the assumptions documented at the top
of `app/migrate_simplymeet.py` -- SimplyMeet.me's export can't tell us who
canceled a booking (every canceled row imports as guest-canceled) or when
it was originally booked (a placeholder is used).

SimplyMeet.me's "Other participants" column IS imported (2026-07-06, once
this tool grew its own guest-booking model -- see "Guests" under "Booking
page layout" above): the row's own "Client email" becomes the party
leader, each "Other participants" address becomes a linked guest sharing
the same party (`my-bt list`/`show` will show "+N guest(s)" on the
leader's row). A guest's name is never known from this export, so it
resolves the same way a live guest booking does: an existing account's
real name if there is one, else the placeholder "Guest". A malformed,
duplicate, or already-erased guest email is skipped and counted in the
report -- it never blocks the leader's own row from importing.

## Logs & debugging

**Viewing logs:**

```
journalctl -u my-booking.service              # the web app
journalctl -u my-booking-retention.service    # the nightly retention job
journalctl -u my-booking-watchdog.service     # the periodic watchdog run (see "Watchdog" below)
journalctl -u my-booking.service -f           # follow live
journalctl -u my-booking.service --since "1 hour ago"
```

By default this is quiet -- routine operation isn't logged -- but real
problems are never silenced: an unhandled exception anywhere in a request
always logs at ERROR with the full traceback, and a few other
always-worth-seeing events (the nightly retention summary, an attendee
self-erasing their account via `/my`, a rate-limiter rejection on
login/reset, each watchdog run's outcome) log at WARNING. Both levels show
up with no extra configuration.

**Verbose mode:** set `MY_BOOKING_DEBUG=1` for full tracing -- every
request (method + path only, never form data/cookies), every CalDAV call
(method/path/HTTP status, never credentials/calendar contents), and every
outgoing email attempt (subject + masked recipient, e.g. `k***@example.com`
-- never the full address). Same for `my-bt`, via either the env var or its
own `-D`/`--debug` flag (identical effect, `-D` is just easier to remember
for a one-off command than the env var name): without it, a failing
command prints one clean line (`error: ...`); with it, the full Python
traceback.

2026-07-16: after a persistent CalDAV-conflict incident that
retrying harder didn't actually explain (see "Calendar invite format"
above, where richer DEBUG output was called for instead): this mode now also logs, for
every CalDAV request that comes back an error status (a 412 conflict
included), the full request headers (minus `Authorization`) and the
full, untruncated response body/headers -- not just the 200-char-
truncated message in the warning line. A stale-ETag retry additionally
logs the ETag it attempted with next to the server's ETag right after
re-reading it, flagged `[UNCHANGED -- ...]` if they're identical (a
genuinely concurrent writer should have produced a *different* one).
And every `query_events()` lookup logs each event's uid/etag next to
BOTH the href the server reported it at and the href we'd actually
PUT/DELETE it at, so a mismatch between the two -- one possible
structural explanation for a conflict no amount of retrying could ever
resolve -- would be visible. Run `my-bt admin resync-calendar -D` (or
set `MY_BOOKING_DEBUG=1` before a `setup -i`) if a persistent conflict
happens again.

To run the service itself in debug mode temporarily:

```
sudo systemctl edit my-booking.service
# add:
#   [Service]
#   Environment=MY_BOOKING_DEBUG=1
sudo systemctl restart my-booking.service
# ...reproduce the problem, then revert:
sudo systemctl revert my-booking.service
sudo systemctl restart my-booking.service
```

Or for a one-off `my-bt` command: `MY_BOOKING_DEBUG=1 my-bt <command>`.

**A single log file** (in addition to journald), **on by default**: with
`[logging].log_file` absent from `settings.toml`, the web service and the
retention job write to `/var/lib/my-booking/my-booking.log` -- a
directory the RPM already creates with the right ownership, so no setup
needed. Set the key to a different path to move it, or to `""` to
explicitly disable file logging (stdout/journal only). It's on by
default because two features read *only* this file and are silently
blind without it: the watchdog's rate-limit-block alerting and the
CSP-violation checks in `my-bt admin health`/`setup`/`csp-violations`
(`my-bt status`'s 24h activity block degrades to the journal, the only
one that does). The file is size-capped with rotation (~2 MB x 4 files,
about 8 MB total ceiling) -- no logrotate config needed, it can't grow
unbounded. For `my-bt`, add `-L`/`--log` to any command to append that
run's *entire* output (not just log records -- the actual table/JSON
output too) to the same file, with a timestamped `=== my-bt ... ===`
line marking where each run starts, so a file with several runs in it
stays easy to read.

**First thing to try if something's wrong:** `my-bt admin health` (see
above) -- it checks most of what actually goes wrong in practice (a
missing/misconfigured secret, a disabled systemd unit, the SELinux
boolean) before you need to dig through logs at all. `my-bt status` is the
much faster "is it actually up and responding right now" check, worth
running first if the site itself seems down.

**Before sharing logs** (with anyone): `journalctl` output is meant to be
safe to paste as-is under normal (non-debug) operation -- log lines are
written to avoid raw attendee emails/names on purpose (user IDs instead,
masked email prefixes, etc.). In `MY_BOOKING_DEBUG=1` mode it's still
designed to avoid raw addresses, but skim before pasting anyway,
especially anything unexpected (e.g. an exception message that happens to
include user-supplied text). Note also that journald has its own log
retention, independent of this app's own `retention_months` GDPR setting --
another reason to keep `MY_BOOKING_DEBUG` off except when actively
troubleshooting.

## Testing

```
python3 -m unittest discover -s tests -t . -v   # from this checkout
```

There's no more `my-bt test` -- the suite instead runs automatically
during `rpmbuild` (`packaging/my-booking-tool.spec`'s `%check` section),
aborting the build on any failure, so there's no separate step to remember
before shipping a package.

1500+ tests covering slot generation (including DST via `zoneinfo`, and that
occurrences stay bookable right up to start), CSV storage/locking/CSV-injection
guarding, atomic capacity-checked booking (no overbooking race), the
late-booking quorum gate (`min_required_participants`), the CalDAV client
(mocked transport, no network) and multi-calendar conflict-checking,
erasure/archival, retention-purge boundaries (including the separate
`pending_confirmation_hours` purge rule for abandoned signups), ICS
build/parse/line-folding, token/password hashing, rate limiting, the
account-confirmation flow end-to-end (`BookingFlowTest` in
`test_webapp.py`: instant booking for a known email, pending-then-confirm
for a new one, capacity re-checked fresh at confirm time, `/my/reset`
returning an identical response whether or not the email exists, and a
regression test that booking never overwrites an existing account's
password), the spots-left display A/B-test knob
(never fakes "FULL", never drops below "1 spot(s) left" while still
bookable-as-confirmed), `site/privacy.html` rendering (`test_site_render.py`),
the `my-bt admin health`/`admin setup` health checks and interactive walkthrough,
including a live CalDAV PROPFIND check that the booking calendar and
every `[[conflict_calendar]]` source actually exist/respond right now
(`test_cli_checks.py`, `test_cli_setup.py` -- every side effect, including
prompting, running external commands, and the CalDAV connection itself, is
a fake, so these don't need root/systemd/rpm/a real tty/network), and the
real-file-vs-generic-.example resolution used by the build/install scripts
(`test_render_site_script.py` -- explicitly asserts a real file is never
modified, deleted, or replaced by its `.example` counterpart), and the
watchdog's four independent checks plus its single-combined-email
behavior (`test_watchdog.py` -- every check is a pure function over
already-read lines/rows, so none of it needs a real nginx log file,
journald, or filesystem), the nginx `access_log` auto-detection/
cross-check against a live `nginx -T` dump (`NginxAccessLogForHostTest`,
`CheckWatchdogNginxAccessLogConfigTest`), the interactive offer to write
`nginx_access_log` into `settings.toml` as plain text without disturbing
any other line (`AddNginxAccessLogSettingTest`), and the read-access
check's actual-OS-check-via-`runuser` behavior including its root/
`runuser`-missing fallback (`MyBookingCanReadTest`, `CheckWatchdogNginxAccessTest`
in `test_cli_checks.py`, `InteractiveSetupWatchdogTest` in
`test_cli_setup.py`).

## GDPR notes

**See the disclaimer at the top of this file first: none of the below is
legal advice, and using these features doesn't by itself make your
deployment compliant.** It documents the reasoning built into this
software so you (optionally with your own legal counsel) can evaluate
whether it fits your situation.

**Retention** (`[privacy]` in `settings.toml`): `retention_months` (default
24) and `canceled_retention_months` (default 6) are config, not code --
change them to whatever you determine is appropriate; there's no single
number this software can pick for you, just the Art. 5(1)(e) "no longer
than necessary" principle it's built around. The nightly systemd timer
(`my-booking-retention.timer`, 03:30) is the cronjob-equivalent that
enforces it, by running `my-bt admin gdpr bookings --purge` and `my-bt
admin gdpr accounts --purge` (2026-07-14: moved under `admin gdpr`,
replacing the old top-level `gdpr-retention`, since it now covers
accounts too -- see below). `my-bt admin gdpr` gives you the overview
(retention window(s) + counts past due); `my-bt admin gdpr bookings` /
`my-bt admin gdpr accounts` list every row/account and the date it would
be (or was) purged; add `--purge` to either to actually act on demand,
same as the nightly timer.

**Account-deletion warning + purge** (2026-07-09 warning email,
2026-07-14 the actual enforcement): optional
`how_many_days_before_account_deletion_send_warning_mail` in `[privacy]`
-- 0, a blank string, or leaving it commented out (the default) disables
the WARNING only. When set, `my-bt admin gdpr accounts --purge` sends a
guest ONE warning email this many days before their account would reach
`retention_months` of inactivity (latest of last login, account
creation, or the most recent booking they made -- a regular who books
week by week without ever logging into `/my` is active, not dormant; see
`app/retention.py::account_activity_date`).
Separately, and regardless of whether that warning is enabled: the same
`--purge` run also actually ERASES (archives with a hashed email, same
mechanism as `/my`'s own self-erasure and `my-bt admin gdpr erase`) every
account already past its `retention_months` deadline -- tied exactly to
that setting, no separate on/off switch, since this is the actual
GDPR-mandated limit rather than a courtesy notice. A guest can still
avoid this by logging in before their deadline (which resets the clock),
or by deleting their own account sooner via `/my`.

**Data dir git snapshot** -- a separate git repository, rooted at
`/var/lib/my-booking/.git` -- entirely independent of this project's own
git checkout -- with TWO layers committing to it:

- **Per-write** (2026-07-07: the requirement is that after any change to
  any of the CSV files -- create, update, or delete -- a git commit
  happens directly, with a commit message stating what changed without
  revealing personal data, as a safety net in case of any bugs): every single Store method that
  mutates users.csv/registrations.csv/an archived/*.csv commits that ONE
  file immediately after writing it (`app/storage.py::_git_commit_data_file`),
  with a short, specific, PII-free message (e.g. "cancel registration",
  "set password" -- never an email or name). This is the primary safety
  net now -- immediate, not up to an hour stale.
- **Hourly** (`systemd/my-booking-git-snapshot.timer`): `app/git_snapshot.py`
  additionally stages the WHOLE data dir (`git add -A`) and commits only if
  something actually changed (`git diff --cached --quiet` to check; no
  empty commits, generic "automatic snapshot: <timestamp>" message). This
  catches anything the per-write layer above can't, by construction --
  most importantly a manual/out-of-band CSV edit made outside the app.

Both are a cheap, local safety net on top of whatever off-box backup you
already run (see "Known simplifications" below -- that's still your own
job), useful for recovering from an accidental `my-bt admin gdpr erase`, a bad
manual CSV edit, or a botched migration. `my-bt admin git-snapshot
[--dry-run]` runs the hourly layer's logic on demand; `my-bt admin setup
-i` offers to initialize the repo (`git init`, a `.gitignore` excluding
`*.tmp`, local `user.email`/`user.name`) if it isn't one yet -- **the
per-write layer deliberately never does this itself**, same "don't
silently turn a data dir into a git repo" principle the hourly layer
already followed: until `my-bt admin setup -i` (or a manual `git init`)
has been run once, both layers are silent no-ops.

**Compliance caveat, stated plainly: git commit history is immutable by
default.** A snapshot committed *before* an attendee's GDPR erasure still
contains their real name/email in that OLD commit, forever -- erasing the
live CSVs does nothing to a git history that already recorded them.
**This tool deliberately does NOT prune, squash, or rewrite that history
automatically** -- there's no built-in mechanism for it, the same way
there's no single `retention_months` number this software can pick for
you. If an immutable local history containing pre-erasure data is a
problem for your situation, that's a tradeoff only you can resolve: decide
your own policy for periodically rewriting/pruning this git history (e.g.
`git filter-repo`/history-rewriting after each erasure, or on a schedule),
or use a different backup approach entirely if immutable history is a
dealbreaker. Weigh this against the safety net the snapshot itself
provides before deciding either way.

**Right to erasure** (Art. 17): an attendee can delete their own account from
`/my`, or you can run `my-bt admin gdpr erase <email>` (or `--email <email>`,
2026-07-16: both forms work -- the `--email` flag was made optional so a
plain positional email address, e.g. `my-bt admin erase someone@example.com`,
works too) on their behalf. Either way:
any future confirmed/waitlisted booking is canceled first (freeing the spot
for the waitlist), then the user row and all their registration rows move
from the live CSVs into `data/archived/{users,registrations}.csv` with the
email replaced by a **keyed** HMAC-SHA256 hash (`security.hash_email_for_erasure`,
key = `secrets/erasure_pepper`). A keyed hash is what makes this a real
erasure rather than security theatre: a bare `sha256(email)` is reversible by
dictionary/rainbow-table attack since email addresses are low-entropy and
guessable; keying it with a secret pepper that's never stored alongside the
archive removes that attack. `my-bt list --all`/`--past` and `my-bt
users` (or `/admin`) query live and archived data together (or live-only
with plain `list`/`--live`, archived-only with `--archive`) so you retain
statistical/audit value (how many sessions happened, aggregate attendance)
without retaining identifiable personal data past the point someone asked to
be forgotten.

**Re-booking after erasure:** an attendee who books again under the same
email gets a fresh live account. `/admin` and `my-bt list --all`/`--past`
both show their pre-erasure registrations merged onto that new live
user_id automatically -- purely a DISPLAY-TIME merge (2026-07-13: this used
to actually rewrite the CSVs on every `/admin` page load; it doesn't
anymore, see `app/cli_list.py::merge_archived_for_display`), computed
fresh on every load/query, nothing written to disk. This is the ONLY
form of "merge" this software does: there used to also be a `my-bt admin
dearchive` command that PERSISTED this merge for real (rewriting the
live registrations.csv to re-attach pre-erasure history to a live,
identifiable account) -- removed entirely 2026-07-14 as a GDPR violation:
permanently re-linking history that an Art. 17 erasure had deliberately
de-linked defeats the point of the erasure. The display-time merge above
was kept (it writes nothing, and the underlying archived identity is
never touched or de-anonymized either way).

**DPIA (Data Protection Impact Assessment):** whether you need one depends
on your own scale, data categories, and risk profile -- this is a
judgement call for you to make (or have made by qualified counsel), not
something this software can determine on your behalf.

**Records of Processing (Art. 30) / Technical & Organisational Measures
(Art. 32):** this README documents the technical measures built into the
software; you're responsible for maintaining your own records of
processing and any additional organisational measures your situation
requires.

**Processors:** confirm whether your CalDAV/SMTP provider and your
hosting provider offer a GDPR-compliant Data Processing Agreement (DPA/AVV),
and whether it's actually applied to your account (some providers only
offer this on business tariffs, or only on request) -- and confirm your
hosting location for any data-residency requirements that apply to you.
This is genuinely provider- and account-specific; check directly with
each provider you use.

## Static-site pages (`site/`)

`site/index.html` is your homepage, linking out to `terms.html`,
`privacy.html`, and `impressum.html` via a small fine-print footer -- see
`site/index.html.example` for a minimal generic starting point.

**Login banner (2026-07-06):** `site/index.html.example` now has a small,
static "Login" link in the top-right corner, linking to `/my`. This repo
has no way to touch your own real, hand-maintained `site/index.html` for
you (it's gitignored, not templated -- see "Generic template vs. your
real config" above), so if you want this on your live homepage, add it
yourself: paste this CSS into your `<style>` block --

```css
.top-bar { display: flex; justify-content: flex-end; margin-bottom: 0.5em; }
.login-btn { font-size: 0.85em; padding: 0.3em 0.9em; border: 1px solid #ccc; border-radius: 4px;
             text-decoration: none; color: #222; }
.login-btn:hover { background: #f4f7f4; }
```

-- and this right after `<body>`:

```html
<div class="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
```

The `target="_top"` is load-bearing, not decoration: this page is also
embedded via `<iframe>` on a separate "center homepage," and `target="_top"`
is what makes clicking Login break OUT of that iframe and navigate the
whole browser tab to `/my`, instead of trying (and likely failing, or
looking broken) to load the login page inside a small embedded iframe.
Viewed standalone (not embedded), `target="_top"` behaves like a normal
same-tab link -- no downside either way.

**Every link needs `target="_top"`, not just Login (2026-07-16):** a real
embed (`booking.example.org/book/<shortname>` inside an `<iframe>` on
ayuryoga-trier.de) surfaced that Login wasn't the only thing that broke
inside a cross-site iframe -- ANY request that sets this app's session
cookie does. The cookie is `SameSite=Lax` (`app/webapp.py::
_session_cookie_header()`); a browser only attaches/keeps a `SameSite=Lax`
cookie across a cross-site *sub-frame* request if it's a top-level
navigation, which nothing happening inside an `<iframe>` ever is. So a
correct login (or a new sign-up-and-book) submitted from inside the embed
would succeed server-side, then the very next page load would silently
come back anonymous again -- no error, just as if nothing had happened,
since the browser never sent the cookie back. Two fixes, no cookie
attribute changes and no new CSRF exposure (see below for why that
matters):

- `site/index.html`'s course links, footer (Terms/Privacy/Impressum), and
  the schedule-exceptions script's "details" links now all carry
  `target="_top"` too, same as Login already did -- apply this to any new
  link you add to your own real `index.html`.
- The dynamic pages' own session-creating forms -- `/my`'s Login and
  Sign-up tabs, and `/book/<shortname>`'s inline Login tab and its
  anonymous sign-up-and-book submission -- now unconditionally target
  `_top` too (`app/webapp.py::_login_form_html()`/`_book_page()`'s own
  `#book-form`). Unconditional, not just when actually embedded: viewed
  directly (not in an iframe), `target="_top"` on a page that's already
  the top frame is a no-op, so there's no behavior difference for a normal
  direct visit.

Why not just relax the cookie to `SameSite=None` instead? Once a form
targets `_top`, the browser treats the whole exchange (POST, redirect,
Set-Cookie) as an ordinary top-level, same-site request by the time the
cookie matters -- so `SameSite=Lax` keeps working exactly as before, with
no CSRF trade-off. `SameSite=None` would have needed a CSRF token added
everywhere (this app has none today -- `SameSite=Lax` IS its only CSRF
defense), and even then isn't reliably enough on its own in 2026: Chrome
now has third-party cookies mostly opt-in-disabled by default, Safari
blocks them outright regardless of `SameSite`, and only `Partitioned`
(CHIPS) cookies are exempt from Chrome's blocking -- but Safari doesn't
support that attribute at all. Breaking out to the top frame sidesteps
all of that browser-policy churn entirely.

**Session-aware upgrade (2026-07-09):** the requirement was that if you
are already logged in, https://booking.example.org should show the same banner
instead of a 'Login' button.
`site/index.html.example` now has a small `<script>` at the bottom of
`<body>` that calls `GET /my/session` (a same-origin `fetch()`, which
carries the attendee's session cookie automatically even though this page's
own JS can never read that cookie directly) and, only if it comes back
`{"logged_in": true, ...}`, swaps the Login button for a "My bookings"
link + "Log out" button -- same `.login-btn` styling, no new CSS needed.
Add the id and the script yourself if you're not starting from the
`.example` file fresh:

```html
<div class="top-bar" id="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
```

```html
<script>
(function () {
  fetch('/my/session', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.logged_in) return;
      var bar = document.getElementById('top-bar');
      if (!bar) return;
      bar.innerHTML =
        '<a class="login-btn" href="/my" target="_top">My bookings</a>' +
        '<form method="post" action="/my/logout" target="_top" style="display:inline;margin-left:0.4em">' +
        '<button type="submit" class="login-btn" style="border:none;cursor:pointer">Log out</button>' +
        '</form>';
    })
    .catch(function () { /* stay on the plain Login button */ });
})();
</script>
```

This does NOT reverse the reasoning above about having no JS/session state
at all -- it's a pure enhancement layered on top of the same static
fallback. A same-origin fetch from a *direct/standalone* visit to the
homepage works exactly as you'd expect. But a fetch from *inside* the
`<iframe>` embed is a third-party request relative to the embedding page's
origin, and modern browsers increasingly block or partition third-party
cookies by default -- so the swap may simply not happen there even when
the attendee really is logged in, in the top-level tab. Any failure or
negative result (network error, JS disabled, blocked cookie) just leaves
the plain Login button exactly as it was before this existed, so there's
no regression case, only an upgrade for the common direct-visit path. The
dynamic, session-aware equivalent ("Logged in as x@example.org...") on
`/courses` and `/book/<shortname>` doesn't have this limitation, since
those pages are server-rendered by this app itself, not fetched from
across an iframe boundary -- see "Course overview page" above. Both
banners now also link back to the homepage itself (`settings.base_url`),
not just to `/my` -- the banner was extended to also link back to
https://booking.example.org.

**Variant: overlaying the button onto a boxed/backgrounded layout.** If
your real homepage wraps its content in its own box (e.g. a fixed-width
`<div>` with a `background-image`, like a hand-styled or Word-exported
page might), placing `.top-bar` as a sibling *before* that box -- as
above -- puts the button at the top of the whole page, not the top of
your content box, and right-aligns it to the browser window instead of
the box. To overlay it onto the box instead and align it with the box's
own right edge: give the box `position: relative`, then absolutely
position `.top-bar` inside it (as its first child) instead of using flex:

```css
.your-content-box { /* whatever it already is, plus: */ position: relative; }
.top-bar { position: absolute; top: 12px; right: 12px; margin: 0; z-index: 1; }
.login-btn { /* same as above, plus a backdrop so it stays legible over a photo/pattern: */
             background: rgba(255,255,255,0.85); }
```

```html
<div class="your-content-box">
  <div class="top-bar"><a class="login-btn" href="/my" target="_top">Login</a></div>
  <!-- ... rest of your existing content ... -->
</div>
```

The button ends up inside the box's own coordinate space, so `right: 12px`
means "12px from the box's right edge" (which tracks the box's own
`max-width`/`margin: auto` centering) rather than the page's.

`site/impressum.html`, `site/privacy.html` and `site/terms.html`:
- `impressum.html` -- legal notice / responsible-party identification.
  Split into its own page rather than inlined on the homepage, so it's a
  stable, permanent link. Whether you need this page, and what it must
  contain, depends on your own jurisdiction -- see the disclaimer above.
- `privacy.html` -- what's collected, legal basis, processors, retention,
  rights. **Generated, not hand-edited:** the two retention-period
  numbers in its "how long it's kept" paragraph come from `settings.toml`'s
  `[privacy].retention_months` / `canceled_retention_months`, rendered by
  `app/site_render.py` from `site/privacy.html.tmpl`. Every generated copy
  starts with an HTML comment: `MANAGED BY my-bt -- generated from
  privacy.html.tmpl + settings.toml. Do NOT hand-edit...` -- edit
  `site/privacy.html.tmpl` for wording changes, never the generated
  `privacy.html` directly.

  Two ways this gets (re)generated:
  - **At build time:** `scripts/render-site.py` writes this checkout's own
    `site/privacy.html` (also run automatically by `scripts/build-rpm.sh`
    before every build) -- this is the `%doc`-shipped reference copy.
  - **At run time, without a rebuild:** if `[site].static_site_dir` is set
    in `settings.toml` (the actual live, separately-checked-out copy of
    `site/`), `my-bt admin health` compares that live `privacy.html` against
    what current `settings.toml` values would render and warns on drift,
    and `my-bt admin setup --interactive` offers to regenerate it right there.
    This closes the gap where changing just `retention_months` in
    `settings.toml` used to require a full rebuild+reinstall before the
    live legal page reflected it -- now it's one `my-bt admin setup -i` away.
    `site/privacy.html.tmpl` itself is also `%config(noreplace)` (see
    "Installing" above), so a package upgrade never clobbers wording
    edits you've made to it.
- `terms.html` -- your participation/liability disclaimer as a full page,
  matching whatever checkbox text you show on the booking form itself
  (`app/webapp.py`) -- keep the two in sync by hand.

**`index_embedded.html` -- a no-JavaScript variant for `<iframe>` embedding
(2026-07-16, mechanism reworked the same day):** `site/index.html`'s two
`<script>` blocks (the Login/Logout swap and the schedule-exceptions
banner, both above) make same-origin `fetch()` calls that, from *inside* a
third-party `<iframe>` embed on another site, can look like tracking
behavior to privacy-conscious browsers and extensions and can also run into
third-party-cookie partitioning, so the session-aware Login swap in
particular may just silently not work there. (Note: if your embed shows up
completely blank/refused rather than degraded, check your CSP's
`frame-ancestors` directive first -- a same-origin-only `frame-ancestors`
blocks the browser from rendering the frame at all, before any script ever
runs, and no static-page variant can work around that; see
`site/nginx-locations.conf.example`'s own `frame-ancestors` comment.)
`index_embedded.html` has **no `<script>` tags whatsoever**, so none of
that ever applies to it:
- The Login button is always plain, static "Login" -- never session-aware
  (no swap to "My bookings"/"Log out"; that swap needs a live `fetch()`,
  which doesn't reliably work from inside a third-party iframe anyway, see
  above).
- The schedule-exceptions banner is baked in **at derivation time** from
  whatever upcoming `[[course.date_override]]` entries `settings.toml` has,
  not fetched live in the visitor's browser.
- EVERY link that navigates somewhere gets its `target`/`rel` rewritten
  per `[site].index_embedded_new_tab_links` (see below) -- this app's own
  routes (Login/`/my`, each course's `/book/<shortname>`, the footer's
  legal pages) and any other link alike, including one pointing back at
  the embedding site itself: left alone, that would render the embedding
  page nested inside its own frame. Links that do not navigate --
  `mailto:`, `tel:`, a bare `#fragment` -- are left exactly as
  `index.html` has them, since opening a mail link in a new tab just
  strands a blank one.

**Serving it to embeds without the embedding site changing anything.**
`site/nginx-locations.conf(.example)` maps `Sec-Fetch-Dest` -- which every
current browser sends, `iframe` for a framed load and `document` for an
ordinary one -- so `/` serves `index_embedded.html` inside a frame and
`index.html` everywhere else, with `Vary: Sec-Fetch-Dest` set at server
level so caches keep them apart. Nothing depends on WHO is embedding (that
would need `Referer`, which the embedding page controls and can suppress);
which sites may embed at all remains `frame-ancestors`' job. A browser too
old to send the header, or a deployment that never deployed
`index_embedded.html`, simply gets `index.html` -- so the mapping is safe
to leave in place whether or not you embed. Pointing the `<iframe>`
straight at `/index_embedded.html` also works and needs no mapping; the
mapping is what makes an embed you do not control work anyway.

**Optional, and DERIVED straight from `index.html` itself -- no separate
template file to maintain:** unlike `privacy.html` (rendered from a
hand-maintained `privacy.html.tmpl`), `index_embedded.html` doesn't have
its own source file at all. It's produced by
`app.site_render.derive_index_embedded_html()`, which takes `index.html`'s
own real markup and: strips every `<script>...</script>` block, retargets
the known-route links described above, and splices the current
schedule-exceptions banner in over the `<div id="schedule-exceptions">`
marker. Because there's no second file, `index.html`'s own wording can
never drift out of sync with the embedded variant the way a parallel
template could.

Two settings, both under `[site]`, control this entirely (see
`settings.toml.example`):
- **`index_embedded_enabled`** (default `false`): whether this mechanism is
  active at all. Off by default -- most deployments don't embed their site
  via `<iframe>` elsewhere, so nothing changes for them.
- **`index_embedded_new_tab_links`** (default `true`): `true` retargets
  every known-route link to `target="_blank" rel="noopener noreferrer"`
  (opens in a new tab -- the booking flow and `/my` genuinely need
  JavaScript, which should run in an ordinary top-level tab, not inside the
  embedding iframe; `rel="noopener noreferrer"` is the plain-HTML defense
  against the new tab getting a `window.opener` handle back to the
  embedding page). `false` uses `target="_top"` instead (breaks out of the
  iframe in the *same* tab -- the same convention `index.html`'s own Login
  link already uses).

A third, independent setting also feeds into the same schedule-exceptions
banner (both the live `index.html` and the derived `index_embedded.html`),
not gated on `index_embedded_enabled` at all:
- **`custom_attention_message`** (default `""`), also under `[site]`: an
  operator-authored message shown in the same red ATTENTION box as any
  auto-generated `[[course.date_override]]` line, below it, separated by an
  `<hr>` when both are present (shown alone, no `<hr>`, if there are no
  upcoming overrides). Understands raw HTML formatting tags -- not
  escaped, same trust boundary as `Course.description`. Example: `"On
  vacation from 2026-08-01 til 2026-08-15, courses resume afterwards at
  their usual schedule."` The per-course `/book/<shortname>` page shows
  this too (same box as its own override line), but never the bold
  weekday-first bullet-list styling described below -- that's specific to
  the site-wide banner, since which weekday a course falls on is already
  obvious from context on its own booking page.

When `index_embedded_enabled` is on, `my-bt admin setup --interactive`:
- Derives what `index_embedded.html` should currently look like from the
  **LIVE, currently-deployed** `index.html` at `[site].static_site_dir` --
  not this checkout's own copy, since the live file is the authoritative
  source for whatever's actually being embedded right now. This step runs
  *after* `index.html`'s own copy/vimdiff reconciliation (see "Deployed vs.
  checkout drift" below), so any `index.html` drift is resolved first.
- If nothing's deployed yet, offers to write the freshly-derived page
  straight to `[site].static_site_dir`.
- If something's already deployed and it doesn't match what would
  currently derive, offers a vimdiff against the fresh version -- same
  reconcile-by-hand pattern already used for `index.html`/`terms.html`/
  `impressum.html`, never a blind overwrite.
- Fails loudly (a clear `[fail]` line, not a silent bad page) if the live
  `index.html` is missing something this derivation depends on -- no
  schedule-exceptions marker, no `/my` or `/book/` link to retarget, or
  neither of the two `<script>` blocks this app itself ships -- since any
  of those suggest `index.html` was restructured in a way the derivation
  no longer understands.

`my-bt admin health`/`admin setup -i` also compare the deployed
`index_embedded.html` against what would currently derive and warn on
drift (a no-op, not a warning, while `index_embedded_enabled` is off).
`my-bt admin site-maintenance on/off` patches the maintenance banner into
this file too, exactly like `index.html` (see "Maintenance mode" below).

**CSP `frame-ancestors` + violation reporting (2026-07-13):** embedding a
site via `<iframe>` only works at all if the embedded site's own CSP
`frame-ancestors` directive allows the embedding origin -- a same-origin-only
`frame-ancestors 'self'` (the shipped default in
`site/nginx-locations.conf.example`) blocks the browser from rendering the
frame *at all*, before any script or static-page variant ever gets a
chance to run. If you use `index_embedded.html` to embed your site
elsewhere, add that origin to `frame-ancestors` too (see the `.example`
file's own comment) -- keep it a narrow allow-list, not wide open, since
`frame-ancestors` is real clickjacking protection for `/my`/`/admin`/
`/book/<slug>`'s own state-changing actions. The same header's
`report-uri /csp-report` has the browser POST a violation report (logged
at WARNING by `app/webapp.py::csp_report`) any time any CSP directive is
violated -- including an embed attempt from anywhere OTHER than what
`frame-ancestors` allows, so that shows up in your logs instead of
silently vanishing into the visitor's own browser console.

**CSP hash automation (2026-07-13, self-heal added 2026-07-16):** every
inline `<script>` block's CSP `script-src` hash going stale after a
hand-edit (silent -- the browser just refuses to run it, console-only
warning, no server-side error at all) has hit production five separate
times now -- see `site/nginx-locations.conf.example`'s own dated incident
notes. Three parts, none of which requires ever computing a hash by hand:

- **Live server, reactive + proactive detection** (`my-bt admin health`/
  `admin setup`): `app.cli_checks.expected_csp_hashes()` computes, straight
  from source, the hash every inline script *should* currently have -- the
  8 static, non-interpolated Python module constants in `app/webapp.py`/
  `app/templates.py` (always checked), plus (only if
  `[site].static_site_dir` is configured) the two `<script>` blocks in the
  LIVE, currently-deployed `index.html` at that path.
  `check_csp_hashes_deployed()` compares that against what's actually
  allow-listed in the live nginx CSP header (`nginx -T`, same
  `base_url`-hostname matching every other live-nginx check here uses) and
  reports any mismatch with the exact `'sha256-...'` value to add --
  proactive, unlike the reactive `csp_report`-based check above (Watchdog
  section), which can only ever fire after a real browser has already hit
  the stale hash.
- **Live server, self-heal** (`my-bt admin setup --interactive` only --
  `admin health`/plain `setup` stay read-only, detection above): if a hash
  is missing, offers to add it directly. Prompt-gated (never silent),
  additive-only (`app.cli_checks.csp_script_src_patch()` -- same "ADD,
  never replace" rollback-safety rule as every hand-authored hash update in
  this file's own history, nothing else on the CSP line or elsewhere in the
  file is touched), and verified with a real `nginx -t` pass/fail check
  before the edit is ever kept -- reverts to the original file content
  immediately if that fails, so this can never leave a broken CSP header
  (which would break every inline script on the site at once, not just
  one) on a live server. Folds into the same single end-of-section
  "reload nginx now?" prompt every other nginx change in that walkthrough
  already uses, rather than asking to reload twice.
- **RPM build, static** (`tests/test_cli_checks.py::
  TrackedNginxExampleFileTest`): two tests, neither requiring a live
  server. `test_example_file_has_every_static_script_hash_current` checks
  the 8 static Python-constant hashes against the tracked
  `site/nginx-locations.conf.example` reference file -- deliberately NOT
  `index.html.example`'s own two script hashes, since
  `nginx-locations.conf.example` is the real, anonymized production config
  for booking.example.org, while `index.html.example` is a generic placeholder for a
  brand-new deployment; their script bodies (and hashes) are expected to
  differ. `test_real_index_html_script_hashes_are_all_in_the_example_conf`
  (2026-07-16) instead checks the REAL, gitignored `site/index.html` (not
  `.example`) against that same reference file -- skips (rather than
  fails) if that real file isn't present in the checkout, so it only ever
  runs, and only ever protects against drift, on a machine (yours) where it
  lives alongside the checkout. This closed a real gap: a `target="_top"`
  edit to the schedule-exceptions script's generated link shipped in an RPM
  and was only caught by `my-bt admin health` against the live server
  afterward, since the first test above deliberately never looks at
  `index.html`'s own scripts at all. `packaging/my-booking-tool.spec`'s
  `%check` already runs `python3 -m unittest discover` on every RPM build,
  so either test failing there means `scripts/build-rpm.sh` refuses to
  produce a package with a forgotten hash update, with no new build-time
  scripting at all.

`app.site_render.extract_script_bodies()` is the one shared, comment-safe
`<script>` extraction helper both of the above (and
`derive_index_embedded_html()`, above) rely on -- strips HTML comments
before matching, so a literal "`<script>`" mentioned in developer prose
inside a comment can never be mistaken for a real opening tag (a real bug,
twice, before this was shared in one place).

**Language:** the `.example` templates are English-only. Whether to
support more languages (and which ones) is entirely your call, based on
who actually uses your instance -- hand-edit the HTML for now, there's no
i18n framework here (deliberately, given the scale this project targets).

These pages are versioned in this repo/package (see `packaging/*.spec`,
which ships them under `%doc`) so a server reinstall gets them back even
if the separate site checkout is ever lost -- but publishing them is a
deliberate, separate step, not something the RPM install does
automatically: copy them to your live static-site host when you're ready,
ideally at the same time as each course's booking link starts pointing at
`/book/<shortname>`.

`my-bt admin health`/`admin setup -i` actively help with that step now (added
2026-07-05, after this exact gap caused a real stale-page incident):
- **Deployed vs. checkout drift**: for each of `index.html`/`impressum.html`/
  `terms.html`, compares the live copy in `[site].static_site_dir` against
  this checkout's real (or `.example`) version, and warns if it's missing
  or stale rather than staying silent. `setup -i` then actively offers to
  copy the newer version over, the same way it already offers to
  regenerate `privacy.html`.
- **Reachability from nginx**: `[site].static_site_dir` and nginx's actual
  `root` for your `base_url` hostname don't have to be the same directory
  -- some setups keep a git-tracked staging directory separate from the
  public webroot on purpose, symlinking in only what's meant to be public.
  `setup -i` checks each managed page is actually reachable that way
  (present in `static_site_dir`, and either identical to nginx's root or
  symlinked into it) and, as root, offers to create the missing symlink --
  never to repoint `static_site_dir` itself, since that's a deliberate
  architectural choice this tool has no business overriding.

## Maintenance mode (`my-bt admin site-maintenance on|off|status`) (2026-07-10)

A sitewide toggle for planned downtime: "add my-bt commands to set/unset
a maintenance mode ... a downtime warning right at the top of
`index.html` ... and any booking URL (like the links on index.html)
should result in a page version of this maintenance message."

```
my-bt admin site-maintenance on                    # enable, no custom message
my-bt admin site-maintenance on -m "back Monday"   # enable with a custom message
my-bt admin site-maintenance off                   # disable
my-bt admin site-maintenance status                # report current state, changes nothing
```

State lives in a small JSON flag file in the data dir (`maintenance.json`),
not `settings.toml` -- `settings.toml` is only read once at process
startup (see `my-bt admin health`'s own settings-freshness check), so a
setting there wouldn't take effect until a service restart, defeating the
point of a quick toggle. This flag file is what both the running app and
`my-bt` itself consult, so `on`/`off` take effect on the very next
request, no restart needed.

**What gets blocked:** every ATTENDEE-facing route -- `/courses`,
`/book/<shortname>`, `/cancel/<token>`, `/reinstate/<token>`, and every
`/my/*` endpoint (login, signup, reset, confirm, cancel, reinstate,
settings, delete-account, ...) -- via one shared check,
`app/webapp.py::App._maintenance_guard`, called first thing by each of
those. Originally scoped narrowly to just `/courses`/`/book/<shortname>`,
widened the same day after a real external-IP test caught
`/my`'s login page still working completely normally during
maintenance, when it should have been blocked like everything else.

`/admin/*`, `/host-cancel/<reg_id>`, `/host-reinstate/<reg_id>`, and
`/host-cancel-occurrence/<course>/<date>` are the one deliberate
exception -- those are the HOST's own tools (the latter three are
unguessable-enough "magic links" only ever reachable from the operator's
own CalDAV event or `admin_email`), and blocking the host's own ability to
manage bookings during a maintenance window they themselves declared would
be counterproductive. `/my/logout` and the JSON-only `/my/session` status
check (polled by the static homepage's own JS) are also left unblocked --
neither is a booking or management action, so gating them would only
cause confusing side effects (an attendee stuck "logged in" against their
wishes, or a broken JSON parse) for no real benefit.

Each blocked request gets the same 503 maintenance page, which includes
a way back to `{yourdomain}` (2026-07-10: the maintenance page needed a
back link or button) -- so clicking "Login" on the static
homepage during maintenance either shows this page (with a way back) or,
for the recognized bypass IP, works completely normally. 2026-07-14: this
is now the same boxed banner every other guest-facing page uses, not a
one-off "Back to `{yourdomain}`" text link.

**What gets written:** `on`/`off` also directly insert/remove an
idempotent, clearly-marked banner (HTML comments delimit it, so re-running
`on`/`off` any number of times never duplicates or corrupts it) right
after the LIVE, deployed `index.html`'s `<body>` tag, i.e. at
`[site].static_site_dir` if configured -- and, if it's ever been generated,
`index_embedded.html`'s too (2026-07-16, see "Static-site pages" above).
`index.html` is hand-authored and never auto-copied otherwise (see
"Static-site pages" above), but maintenance needs to show up immediately,
not at the next manual copy, so this is a deliberate exception: an
explicit, dedicated command whose entire purpose is to touch these exact
files, not a background auto-sync. `index_embedded.html` not existing yet
(the common case -- it's optional) is silently skipped, same as any other
missing target. If `static_site_dir` isn't configured, `on`/`off` still
flip the flag file (the app-side gating above still works) but print a
note that no banner was written anywhere. (2026-07-10: my-bt should not modify
the package-installed TEMPLATE folder site -- this used to ALSO patch
this checkout's own `HOME/site/index.html`, i.e. `/opt/my-booking/site/`
on a stock install, but that copy is a template/reference only, never
what nginx actually serves; `static_site_dir` is the one real, live
location, same as `privacy.html`/`terms.html` already treat it -- see
"Generic template vs. your real config" above.)

**The message itself** (identical wording on the banner and on the
503 booking pages): "This site is currently down for maintenance. Booking
links won't work right now." plus your optional `-m/--message` text, plus
a `mailto:` link to `[site].admin_email` and a note to reach out via
Teams if you're a DBG Lux colleague.

**Left on by accident?** `my-bt status` highlights maintenance mode
prominently whenever it's ON (impossible to miss in the live summary), and
`my-bt admin health`/`admin setup` report it as a `warn` (not silence,
exits non-zero), specifically so it can't stay enabled for days after a
real maintenance window ends without anyone
noticing -- see `app/cli_checks.py::check_maintenance_mode`.

**Bypassing it for yourself** (2026-07-10: the requirement was that
maintenance mode still let the operator access the site from
ssh.example.net): two
optional, independent `[site]` settings let a matching request keep using
every gated route above normally even while everyone else is blocked (the
one unavoidable exception is the static `index.html` banner itself --
nginx serves that as a plain file with no per-visitor awareness, so it
shows the banner to every visitor including a bypassed one; only the
DYNAMIC pages behave differently for the bypass IP) --

```
maintenance_bypass_hostname = "ssh.example.net"   # your own dynamic-DNS name
maintenance_bypass_ip_log   = "/home/me/my-ip.log" # last line = your current IP
```

Either one alone is enough; both are re-checked fresh on every request
(never cached), and both fail CLOSED -- an unresolvable hostname or an
unreadable/missing log file just means that source doesn't match, not an
error, and if NEITHER setting is configured the bypass does nothing at
all (maintenance blocks everyone, same as before these existed). The two
sources mirror exactly what nginx's own `sync-dynamic-ip-acls.sh` already
checks to keep `/admin`'s IP allowlist current (see
`site/nginx-locations.conf`'s own comment on that script) -- DNS can lag
an actual IP change by however long the record's TTL/propagation takes,
while a locally-written log file updates the moment the IP itself
changes, so checking both covers either kind of lag. Trust model: the
same as `_client_ip()`'s own (this app is only ever reachable through its
own nginx reverse proxy on 127.0.0.1, which is what actually sets
`X-Forwarded-For`, so this can't be spoofed by an outside client). See
`app/webapp.py::_maintenance_bypass_allowed()`.

## Spots-left display (`[defaults]` in `settings.toml`)

`show_spots_left` (default `true`) toggles the "N spots left" / "FULL,
join waitlist" text on the booking page on or off entirely (correctly
singular as "1 spot left").

`spots_left_offset` (default `0`) shifts the *displayed* number, for
A/B-testing whether perceived scarcity changes booking behaviour --
positive shows fewer spots than are really available (more urgency),
negative shows more. This is deliberately display-only
(`app/webapp.py::_spots_left_text`):

- The actual confirmed-vs-waitlisted decision always uses the true
  confirmed count (`Store.add_registration_checking_capacity`) -- this
  setting can never cause over-booking or a wrongly-waitlisted attendee, no
  matter what number is shown.
- An occurrence that's genuinely full always says "FULL, join waitlist,"
  regardless of the offset -- what that promises in the confirmation email
  has to stay true. Only the number shown while there's real room left is
  adjustable, and it's floored at "1 spot left" (never "0" while a
  booking from there would in fact still be confirmed) and capped at the
  course's real capacity.

## Course overview page (`/courses`)

`/courses` (2026-07-06) lists every `[[course]]` configured in
`settings.toml`, each linking to its own `/book/<shortname>` -- a
SimplyMeet.me-style "pick a class" landing page. It's the destination for
`/my`'s "New booking" button (see "Account confirmation" above), and can
also be linked to directly from your static site if you want one page
that lists all your offerings instead of separate links per course.
`audience` (`"private"`/`"public"`) is display-only (see
`settings.toml.example`) and does NOT filter this list -- every course is
already reachable via a direct `/book/<shortname>` link regardless, so
hiding one here would only make it harder to find, not more private.

Course order on this page (2026-07-09) follows each `[[course]]`'s optional
`order_in_all_courses` key, ascending -- lower first, omitted defaults to
0. Leaving it unset everywhere keeps your existing `settings.toml`'s own
course order exactly as before this existed (a tie falls back to file
order, not a re-shuffle). See `settings.toml.example` for the field itself.

### Logged-in banner (every page except `site/index.html`)

All three pages (2026-07-06, and `/my` too as of 2026-07-09) show a small
"Logged in as x@example.org · My bookings · booking.example.org · Log out" banner
above their own heading when reached with an active `/my` attendee session --
e.g. after clicking "New booking" from `/my`. It also carries through to
the booking result page ("Booked!"/"Almost there"/waitlisted). An
anonymous visitor to `/courses` or `/book/<shortname>` sees the same box
with a plain "Login · booking.example.org" instead (2026-07-09: the requirement
was that the top-bar always be visible, either showing LOGIN or the full
banner -- see `_anonymous_banner_html()`); these pages work perfectly well
without ever logging in first, this is purely a courtesy cue plus a quick
way back to `/my`, to the main site, or to log out for someone who arrived
here already signed in.

That "Login" link (2026-07-11: made to return to the originating
page) carries a `?next=/courses` or `?next=/book/<shortname>` query
param, so a successful login lands back on the exact page the attendee
clicked Login from instead of always on `/my`'s bookings list -- see
`_safe_next_path()`'s own docstring for the allowlist this is validated
against (both on the way in and again out of the login form's hidden
field) before it's ever used in a redirect.

The "booking.example.org" link in the middle
(`settings.base_url`, labeled with the same hostname `_site_label()` uses
elsewhere) was added 2026-07-09 (the banner was extended to also go
back to https://booking.example.org) -- this is a normal same-tab link, distinct
from the STATIC homepage's own separate, session-aware corner widget (see
"Login banner" further below), which is intentionally NOT this same big
banner: the homepage never shows "Logged in as ...", only a compact
"My bookings"/"Log out" swap-in for its plain "Login" button, since it's a
much smaller/differently-styled corner widget by design.

On `/my` itself the "My bookings" link is dropped from the banner
(`_session_banner_html(..., on_my_page=True)`, 2026-07-09) -- flagged
from a screenshot of `/my`'s own banner showing a redundant "My bookings"
link on the my bookings page itself. A link back to the exact page you're
already looking at isn't a shortcut, just clutter -- `/courses` and
`/book` still show it since it's a genuine link elsewhere from there.

`/my`'s own ANONYMOUS view (the Login/Sign up form) and `/admin`'s own
login form deliberately skip the "Not logged in / Login" text -- a
"Login" banner sitting above a login FORM would be redundant -- but
still get the same boxed `.session-banner` style with just the homepage
link (`_homepage_only_banner_html()`). That used to leave `/my`'s login
page as the one page in the app with no way back to the marketing
homepage short of editing the URL by hand (2026-07-10: a way back to
https://booking.example.org was missing there), first fixed with a plain "Back to
`{yourdomain}`" text link, then (2026-07-14: reusing the same boxed
banner was judged better) upgraded to the same box.

2026-07-14: this was expanded further so that /admin, and basically all
pages except index.html, get the same boxed banner --
every remaining page in the app (every guest/host magic link --
`/cancel/<token>`, `/reinstate/<token>`, `/host-cancel/<id>`,
`/host-reinstate/<id>`, `/host-cancel-occurrence/<shortname>/<date>` --
plus `/my/reset`, `/my/confirm/<token>`, `/my/confirm-email/<token>`,
`/my/cancel-email-change/<token>`, `/admin/login`, and `/admin` itself)
now shows the same boxed banner. `/admin`'s own pages (`admin_overview()`,
`admin_cancel()`, `admin_reinstate()`) use a separate `_admin_banner_html()`
variant instead of `_session_banner_html()` -- admin sessions are a
different `kind` ("admin", a single shared password) that
`_session_banner_html()` doesn't recognize, and showing "Not logged in"
to an admin who genuinely is logged in would be misleading. That admin
banner has no logout link -- there's no admin logout route today (an
admin session just sits in memory until the process restarts or a fresh
login overwrites it) -- just an "Admin" label and the homepage link.

Logging out (`POST /my/logout`, the one form every banner above shares)
redirects to the homepage (`settings.base_url`), not `/my` (2026-07-11:
pressing logout was changed to bring you back to https://booking.example.org).
Since the same banner/logout form is shared by the homepage's own
JS-rendered copy, `/courses`, `/book/<shortname>`, and `/my` itself, the
old `/my` target was most jarring from the homepage -- logging out there
used to jump straight into the app's `/my` login page instead of staying
on the site you were just on. See `App.my_logout()` in `app/webapp.py`.

## Booking page layout (`/book/<shortname>`)

Dates are shown as clickable buttons (not a dropdown), each showing the
date and, on its own line, the spots-left text -- laid out in an even
grid so buttons of different widths still line up. A "Selected date: ..."
box below the buttons repeats the chosen date as plain text. Name and
email use a larger input (`.big-input` in `app/templates.py`) since real
addresses/names can be long. Every required field is marked `(required)`:
name, email, and the participation-terms checkbox -- there is no
password/PIN field here at all (see "Account confirmation" below); a hint
explains that a brand-new email will need to confirm its address by
email before the booking is finally held.

The submit button is progressively enhanced: it starts enabled (the
`required`/`pattern` attributes alone already block an invalid submit
with no JS at all), and with JS enabled it also disables itself until
every required field validates, and its label switches between
`[defaults].book_button_label` (default `"Book"`) and `"Join waitlist"`
depending on the selected date's availability -- the waitlist label is
never configurable, since it has to stay literally true to what
submitting the form does.

### Guests ("+ Add participant")

Below the email field, "+ Add participant" adds a row per guest (email
required, name optional, up to 9) -- mirrors SimplyMeet.me's own "add more
participants" UX. Submitting with at least one guest books the whole
party -- the person who filled out the form (the "leader") plus every
guest -- as ONE atomic decision: either everyone is confirmed, or (if
there isn't room for all of them) everyone is waitlisted together, never
split. A brand-new guest's email does NOT have to click a confirmation
link first (unlike a brand-new solo booker) -- the leader vouches for
whoever they add, same trust model SimplyMeet.me used; guests still get a
real account. Their booking email (confirmed or waitlisted) includes an
OPTIONAL "set up your account" link (2026-07-06) -- the same
/my/confirm/&lt;token&gt; flow a solo booker's first confirmation email uses --
so they can set a password and see/manage this booking at `/my` later; the
booking itself is never gated behind clicking it. Anyone in the party who
already has a password set (an existing account) gets no such link, so a
returning guest's email doesn't dangle a redundant offer. If adding a
guest would exceed the session's real remaining
capacity, a warning appears live on the form (before you submit) so you
can remove a guest and get confirmed instantly instead of waitlisting the
whole group -- this uses the TRUE spots-left count, never the
display-only `spots_left_offset`-adjusted number (see "Spots-left
display" above).

Cancellation is always per-person, regardless of party membership --
if one guest (or the leader) cancels later, it only frees their own spot;
everyone else in the party is untouched. `/admin`'s overview table has a
Party column showing "+N guest(s)" on the leader's row and
"guest of `<leader>`" on each guest's row, so it's always clear who booked
together and who was a guest. The calendar invite (2026-07-06) shows the
same thing: a "Participants:" table (status, name, email, self/guest,
registered-at, cancel link) for active/waitlisted registrants, and a
separate "Canceled:" table (same columns, canceled-at + who canceled
instead) for anyone who dropped out -- see `app/calendar_sync.py`'s
`sync_occurrence()` docstring for the exact line format. That per-participant
"cancel:" line is a `/host-cancel/<reg_id>` link (2026-07-09) -- a no-login
"magic link" straight to a Cancel Booking confirmation page (What/Where/When
+ an optional reason), so tapping it from your phone's calendar app doesn't
first bounce you through `/admin/login`. Gated only by the registration ID
being an unguessable `uuid4`, not a separate secret -- see `host_cancel()`'s
own docstring in `app/webapp.py` for the trust-boundary reasoning.

Two more `[[course]]` fields control the page header:
`subtitle` (optional plain text -- omit it to auto-show
"<Weekday>s <from>h<mm> - <till>h<mm> -- <location>" (e.g. "Saturdays
10h45 - 12h45 -- Ayur Yoga Center Trier Nord"), set it to `""` to show
nothing, or override it with your own text) and `description` (rendered as **raw HTML**, not
escaped, so bold/italic/underline, links, and bullet lists all work --
safe because this is your own settings.toml content, not attendee input, the
same trust boundary as the hand-authored `site/*.html` pages).

### Exceptional date/time overrides (`[[course.date_override]]`) (2026-07-16)

A course can list any number of exceptional dates where it starts at a
different time (and, optionally, runs a different length) than its usual
weekly slot -- e.g. a one-off earlier start because the instructor has to
leave early that day. Add one `[[course.date_override]]` sub-table per
exceptional date, right after the `[[course]]` block it belongs to:

```toml
[[course.date_override]]
date = "2026-07-18"           # required, "YYYY-MM-DD"
start_time = "09:45"          # required, "HH:MM"
duration_minutes = 60         # optional -- omit to keep the course's own duration
message = "I need to be in Kaiserslautern before 13h."  # optional
```

`date` must match one of the course's normal weekly occurrence dates --
this shifts an existing session's time, it does not add an extra one.
`duration_minutes` only needs to be set if the session's length itself
changes too; omitted, the course's normal duration is kept (so the end
time still shifts along with the new start time). `message` is optional,
free-text, and rendered as raw HTML (same operator-authored trust
boundary as `description` above).

Any upcoming override is shown automatically, with no further
configuration: as a red "ATTENTION" line on that course's `/book/<shortname>`
page (right below the subtitle), in every booking-related email that
mentions that date (booking confirmed/waitlisted, promoted-from-waitlist,
cancellation, rebook), and on `site/index.html` itself via a small
`fetch('/schedule-exceptions')` call in that page's own `<script>` (same
opportunistic, same-origin-fetch pattern as its Login/Logout banner --
see that file's own comments). `GET /schedule-exceptions` is a public,
read-only JSON endpoint listing every course's upcoming overrides
(`course_shortname`, `course_title`, `date`, `time_label`, `message`),
sorted by date then course -- past dates are left out. The operator's
own synced calendar event and the guest's `.ics` email attachment both
reflect the shifted time too, not just the web page/email text.

## Account confirmation (`/my`, `/my/reset`, `/my/confirm/<token>`)

The booking page only ever asks for name + email -- there is no
password/PIN field to fill in, and, crucially, **booking never changes an
existing account's password.** Older versions let anyone "take over" an
attendee's `/my` login just by resubmitting their email with an
attacker-chosen PIN (`Store.upsert_user` used to overwrite `pin_hash`
unconditionally on every booking); `Store.upsert_user_for_booking` now
only ever touches `name`, never password fields, for an email that
already has an account.

What happens on submit depends on whether the email is already known:

- **Known, confirmed email:** books instantly, exactly as before --
  confirmed or waitlisted per the normal capacity rules, calendar synced
  right away.
- **Brand-new email:** the booking is created with an internal
  `pending_confirmation` status. A pending row **holds no real capacity**
  (it's excluded from `count_confirmed`, waitlist promotion, and calendar
  sync -- see `app/storage.py::STATUS_PENDING_CONFIRMATION`) and does not
  sync to the calendar, so nobody can grab every open spot on a course
  just by submitting a pile of made-up email addresses. The attendee gets an
  email with a confirmation link instead of a "you're booked" email.

Clicking the confirmation link (`/my/confirm/<token>`) lets the attendee set
a password (the page suggests "e.g. a 6-digit code" only as an example,
not an enforced format) for their `/my` login. Setting it both confirms
the account and **promotes every one of that email's pending
registrations**, re-checking capacity *fresh at that moment* -- an
occurrence that filled up while the attendee hadn't yet confirmed their
address correctly lands the promoted booking on the waitlist instead of
overbooking. Each promoted registration gets its own confirmation email
and its own cancel link (generated at promotion time, not at the original
pending-booking time).

**Link expiry & re-requesting (2026-07-07):** a confirm/reset link is only
valid for `CONFIRM_TOKEN_TTL_HOURS` (24) after it was sent -- an older link
shows "This link has expired" rather than silently pretending to work.
Requesting a new link (via `/my/reset` or `/my/signup`) also immediately
invalidates whatever link was outstanding before it; clicking that
now-superseded link shows "a newer link was already sent to you -- check
your inbox", not the generic invalid-link message, so an attendee who
double-submitted knows to look for the newest email instead of assuming
something's broken. The confirm/reset email itself states the expiry and
that only the latest email's link works. Both the expiry and the
superseded-link check only recognize the single most recent request; a
link from two or more requests back falls back to the generic "invalid or
already been used" message.

`/my` shows two CSS-only tabs (2026-07-06, no JS needed to switch between
them): **Login** (default) and **Sign up**. Login asks for email +
password (relabeled from "PIN"), and links to `/my/reset` for both
"forgot your password" and "resend my confirmation email" -- the same
flow handles both, since both cases reduce to "prove you own this inbox
via a one-time link, then set a password." `/my/reset` always returns the
same response regardless of whether the submitted email exists, to avoid
leaking which addresses have an account; it's rate-limited the same way
as login (see "Logs & debugging" for the shared `RateLimiter`). A wrong
email/password shows "Email and/or password did not match." -- never
which of the two was wrong.

**Sign up** (2026-07-06) asks for name + email and, on submit, always
shows the same generic "check your email" response: if that email has no
account yet, one is created (with the given name) and a confirm link is
sent; if it already has one, its name is left completely untouched and it
gets a plain reset-or-resend link instead -- functionally identical to
`/my/reset`'s own "forgot password" for that case, so signing up with an
email you already have an account under can never clobber your real name
with whatever you happened to type into the sign-up form. Deliberately
shares `/my/reset`'s own rate-limiter keys (`reset:<email>`/
`reset-ip:<ip>`), not separate ones -- both endpoints end up doing the
same thing (create/confirm an account and email a token), so a lockout on
one applies to the other too; POST `/my/signup` is the tab's target.

**Admin shortcut (2026-07-06):** entering `admin` as the email on the
Login tab, with the admin password, logs into `/admin` instead -- so the
admin doesn't need to remember a separate URL. This reuses
`/admin/login`'s own rate-limit bucket (keyed by client IP, not by
email), not the per-email attendee one, so it can't be used to sidestep --
or worsen -- either lockout: hammering "admin" via `/my` from one IP
trips the exact same lockout `/admin/login` itself would from that IP,
and vice versa. A wrong password for "admin" shows the same generic
"Email and/or password did not match." a real attendee mismatch gets, never
"Wrong password" -- nothing about the response reveals that "admin" is
treated specially. `/admin/login` itself is unchanged and still works
exactly as before; this is purely an additional way in through `/my`.

Once logged in, `/my` (2026-07-06) shows bookings in two separate tables:
**Upcoming** (all of them, soonest first) and **Past** (capped at the 3
most recent, so someone who's been coming for years doesn't get a
page-long history) -- both always show a friendly "You have no ... 
bookings." message when empty (2026-07-09: Past used to be omitted
entirely when empty, which looked indistinguishable from broken/missing).
A **New booking** button links to `/courses` (see below) rather than to
any one course. `/my` also shows the same session banner `/courses` and
`/book` do (see "Course overview page" above) instead of its own separate
Logout button -- that banner's own link back to the main site
(`settings.base_url`) replaced a dedicated "visit the homepage" link `/my`
used to show on its own (2026-07-09: the redundant sentence behind New
bookings could be dropped now that https://booking.example.org already appears in
the top-bar).

Abandoned pending signups (a confirmation link never clicked) are purged
by the nightly retention job after `pending_confirmation_hours` (default
48, `[defaults]` in `settings.toml`) -- independent of, and much sooner
than, `retention_months`/`canceled_retention_months` below, since a
pending row never held a real booking in the first place.

## Host notifications (`admin_email`)

Every registration event -- a new booking, a waitlist entry, a
cancellation, a rebooking, a promotion off the waitlist -- emails a short
notification to `admin_email` (`[site]` in `settings.toml`), separately
from the attendee's own copy. Its `Reply-To` is the participant's
address, so replying from your inbox reaches them directly (2026-07-16).

**All five follow one shape** (2026-08-19 -- before this, the
new-booking ones were plain ASCII with an occupancy count while the
cancellation/rebooking/promotion ones were emoji-rich HTML without one):

- **Plain text, no HTML part.** These land only in your own inbox; they
  are receipts, not letters to a guest.
- **Pure ASCII** -- no emoji. (The participant-facing emails keep theirs;
  only the host copies are stripped, by the same shared builder, so the
  What/When/Where layout itself can never drift between the two.)
- **No course description repeated back at you** -- you wrote it in
  `settings.toml`.
- **Subject: `<what happened>: <shortname> on <date> [taken/capacity]`.**
  The course SHORTNAME, not its long title, so you can tell at a glance
  which course it is; the full title is still on the body's `What:` line.
  This is the one place a shortname is deliberately still shown.
- **Occupancy everywhere**, in the subject and as one body line,
  `3 / 12 spots taken now.` -- always the state AFTER whatever the email
  is reporting, so a cancellation already shows you the freed-up number.

Examples:

```
New booking: lux-wed-yoga on 2026-09-09 [1/14]
New waitlist entry: trier-sat-yoga on 2026-08-22 [12/12]
Canceled: lux-wed-yoga on 2026-08-19 [0/14]
Rebooked: lux-fri-yoga on 2026-08-21 [8/12]
Promoted from waitlist: lux-fri-yoga on 2026-08-21 [8/12]
```

Everything above is template-driven: each host email is one file under
`email_templates/` (`new_booking_admin_email.txt`,
`new_booking_party_admin_email.txt`, `cancel_email_admin.txt`,
`reinstate_email_admin.txt`, `promoted_admin_email.txt`), all built from
the same macros -- `{{intro}}`, `{{message_line}}`, `{{details}}`,
`{{spots_taken}}`, `{{capacity}}`, plus each one's own links. Reorder
them, reword the occupancy line, drop a section: override the file via
`[site].email_templates_folder` (see "Email templates") without touching
any code. Note a template referencing a macro that isn't passed to it
fails loudly rather than shipping a half-rendered email.

The count is the confirmed total (`Store.count_confirmed`), which by
definition excludes waitlisted and not-yet-confirmed `pending_confirmation`
rows -- so a waitlist entry correctly reports a full `capacity / capacity`,
and a brand-new email's still-pending booking doesn't inflate it. Every
path reports it from the same helper (`App._occupancy`), and every
subject is built by the same `host_subject()`, so they can't drift apart.

The new-booking notification also carries no-login **cancel links** (2026-07-22), so
you can act on it straight from your inbox without opening `/admin` --
the same unguessable-token "magic links" that already appear in your
calendar event (see "Calendars" above), just surfaced in the email too:

- **Cancel this booking** -- `/host-cancel/<registration_id>`, cancels
  just this one registration.
- **Cancel the entire session (all participants)** --
  `/host-cancel-occurrence/<course>/<date>`, cancels every registration
  for that occurrence at once and blocks new bookings for the date (see
  "cancel the entire session" under the CLI/Calendars sections).

For a party booking there's no single row to cancel as a unit, so the
email lists one "Cancel `<name>`'s booking" link per party member,
alongside the one entire-session link. Both link types are built by
`host_cancel_url`/`host_cancel_occurrence_url` (`app/cancellation.py`) --
the single source shared with the calendar-event body so the two can't
format the same link two different ways.

## Account settings (`/my/settings`, `/my/confirm-email/<token>`) (2026-07-10)

A logged-in attendee can change their display **name** (immediately, no
confirmation needed) and their login **email** (a two-step,
dual-address-notified flow) from a new **Account settings** button next
to `/my`'s "New booking" one.

Requesting an email change (`POST /my/settings/email`) rejects an invalid
address, the account's own current address, or an address already
claimed by a *different* account -- and is rate-limited per account
(5/hour, same ceiling as login, just its own bucket since this action is
only ever reachable from an authenticated session, unlike the anonymous
`/my/reset`). On success it sends **two different emails**:

- The **new** address gets the actionable confirm link
  (`/my/confirm-email/<token>`), plus a note on what it's replacing and
  when it becomes active.
- The **current** address gets an informational notice only (no link) --
  which new address was requested, and how to cancel the change from
  `/my/settings` if this wasn't the account owner's own doing.

Only **one** pending email change can ever be outstanding at a time; a
second request simply replaces the first (same "a newer link
supersedes an older one" behavior as `/my/confirm/<token>` -- see above).
While a change is pending, `/my/settings` shows its status (old -> new,
plus a **Cancel this change** button) instead of the request form.

Clicking the link in the new address's email (`GET
/my/confirm-email/<token>`) only *previews* the change -- nothing is
applied until the attendee submits the confirmation form on that page
(`POST`), the same GET-preview/POST-consume shape `/my/confirm/<token>`
uses, so an email-scanning security service pre-fetching the link can't
silently consume it. This route deliberately does **not** require an
active login session -- the new address may be checked from a completely
different browser or device than the one the change was requested from.
An expired (`CONFIRM_TOKEN_TTL_HOURS`, same 24h as account confirmation),
superseded, or otherwise invalid/already-used link shows the same three
distinct messages `/my/confirm/<token>` does.

Once confirmed, **both** addresses get a short final notice: the new one
told it's now the account's login email, the old one told it no longer
has access.

## Watchdog (`[watchdog]` in `settings.toml`)

A periodic health check (`systemd/my-booking-watchdog.timer`, every 15 min
by default -- see `my-bt admin health`/`admin setup`'s systemd check, which
now covers this timer alongside the app service and the retention timer) that emails
`admin_email` once per run if anything below crosses its threshold in the
last `window_minutes`; completely silent otherwise. This is deliberately a
coarse, sitewide, periodic signal -- **not** a replacement for either of
two finer-grained defenses this project already has: the per-key
`RateLimiter` (see "Logs & debugging" above) already blocks/slows a single
attacker in real time, and fail2ban (recommended, configured outside this
repo) already bans a single abusive IP outright. The watchdog only notices
the aggregate pattern afterwards, as a heads-up.

Five independent signals, each optional:

- **nginx request bursts**: one IP making at least `nginx_request_threshold`
  requests, or with a 4xx/5xx share of at least `nginx_error_rate_threshold`
  (only evaluated once that IP has made enough requests to be meaningful),
  within the window. Disabled entirely unless `nginx_access_log` is set.
- **Booking-tool abuse**: at least `pending_signup_threshold` brand-new
  pending_confirmation registrations (see "Account confirmation" above)
  created within the window -- the shape a capacity-grab attempt against
  the booking page would take, since a real confirmed booking never
  produces this signal.
- **Rate-limiter blocks**: at least `rate_limit_block_threshold`
  login/reset rejections (any key combined) logged by the app within the
  window.
- **CSP violations** (2026-07-13): at least `csp_violation_threshold`
  browser-reported Content-Security-Policy violations (`app/webapp.py::
  csp_report`, see "Static-site pages" -> CSP above) within the window,
  across every distinct violation combined -- most often a stale
  script-src hash after an inline `<script>` edit, occasionally a genuine
  embed/injection attempt from outside the allow-listed `frame-ancestors`
  origin. Unlike the other four signals, this one is ALSO always surfaced
  (un-thresholded) by `my-bt health`/`admin setup`, and its full,
  ungrouped detail is available any time via `my-bt admin
  csp-violations` -- so you don't have to wait for (or rely solely on) a
  threshold-gated email to notice a stale hash; see
  `app.cli_checks.find_csp_violations`/`check_csp_violations`, the one
  shared place this parsing lives (the watchdog's own check calls that
  same function rather than keeping a second copy).
- **sshd failures**: at least `sshd_failure_threshold` failed-password
  attempts (any source, sitewide) within the window, read via `journalctl
  -u sshd` -- deliberately cruder than fail2ban's own per-IP ban
  threshold; an early heads-up, not a substitute for it.

Set `enabled = false` to turn the whole thing off (the timer still runs,
every check just becomes a no-op). See `settings.toml.example`'s
`[watchdog]` section for every default value and a short explanation of
each.

**nginx_access_log auto-detection:** `my-bt admin health`/`admin setup`
cross-check `[watchdog].nginx_access_log` against nginx's own live config
(`nginx -T`, same live-config approach the nginx-location/SELinux/CalDAV
checks already use) for the vhost matching `[site].base_url`. Three outcomes:
not configured but a real `access_log` was detected for this vhost --
`admin setup --interactive` offers to write `nginx_access_log = "..."` into
`settings.toml` for you; configured but it doesn't match what nginx is
actually logging to -- `admin setup --interactive` offers to update it in place
too (same prompt-and-write pattern, not just a manual-fix warning); or
already matches -- nothing to do. This is a detect-and-*offer*, never a silent
auto-enable: nginx's `log_format` can be customized, and the nginx-burst
parser only understands the standard combined format, so silently
turning the check on could give false confidence that it's monitoring
something it can't actually parse -- the detection also spot-checks the
log's first line against that format and adds a caveat if it looks custom.

**nginx log read access:** once `nginx_access_log` is set, `my-bt admin health`/
`admin setup` also check whether the `my-booking` user can actually read it --
`ReadOnlyPaths=-/var/log/nginx` in the watchdog's own systemd unit only
grants a sandboxing exception, it does nothing about the file's actual
owner/group/mode/ACLs, which is nginx's/the distro's call. Fedora's nginx
package typically leaves `/var/log/nginx` unreadable to another
unprivileged user by default. This check works by actually asking the OS
(`runuser -u my-booking -- test -r <path>`, root only) rather than
inspecting permission bits itself -- a bit-based version of this check
was tried first and had a real bug: `setfacl` grants access via a POSIX
ACL entry, which never shows up in the file's mode bits at all, so that
version kept reporting "unreadable" even immediately after the exact
`setfacl` fix it recommended had already been run. Without root, this
check honestly reports "can't verify" rather than guessing.
`setup --interactive` offers the `setfacl` fix itself (as root), including
a default ACL so it survives nginx's own log rotation. `acl` (for
`setfacl`) is deliberately **not** an RPM dependency -- it's only needed
if you opt into nginx-burst checking, so `setup -i` just tells you to
`sudo dnf install acl` first if it's missing, rather than making every
install pull it in.

**On-demand forensic aggregation (`my-bt admin health report`/`errors`,
2026-07-13):** for investigating "anything strange happening with
booking.example.org" (the operator's own phrasing) beyond what the periodic watchdog
sweep or a single `my-bt admin health` snapshot covers. Both collect the
SAME set of sources for a time window and print them, source-labeled:
nginx's own global access/error logs (outside any vhost -- covers every
site on this box, not just this one), THIS vhost's own access/error logs
(matching `[site].base_url`, same live `nginx -T` derivation the
watchdog's own nginx-log config-drift check already uses -- no new
settings.toml path to configure or keep in sync by hand), the app's own
`[logging].log_file`, sshd (`journalctl -u sshd`), and the
`my-booking.service`/`my-booking-watchdog.service` journals.

- `my-bt admin health report` -- every matching line, verbatim, exactly
  as it appears in its source (raw material for a human to read, not a
  merged single timeline across sources).
- `my-bt admin health errors` -- the same sources/window, filtered down
  to actual problems: access logs to 4xx/5xx status, the app log to
  WARNING-or-above (this naturally includes CSP violations and
  rate-limiter rejections, both already logged at WARNING -- see
  "CSP violations" above), sshd to failed-password attempts, the service
  journals to WARNING-or-above, PLUS a grouped/counted CSP-violation
  summary appended at the end (the same data `my-bt admin
  csp-violations` shows in full). This combines the same curated
  detectors `my-bt health`/the watchdog already have with a raw
  severity-based pass, so a kind of problem no existing detector knows
  about yet still shows up too.

Both are also reachable as flat top-level commands, `my-bt admin
log-report`/`my-bt admin log-errors` -- exactly the same commands, just
without having to remember they live under `health`.

**Time window:** `--last DURATION` (e.g. `2h`, `90m`, `1h30m`, `45s`,
`1d`), or `--since`/`--till` (ISO-8601 timestamps; either or both --  an
omitted `--till` defaults to now, an omitted `--since` alongside a
`--till` defaults to 24h before it). With none of the three given, both
default to the window **since nginx's own last restart** (via
`systemctl show nginx.service --property=ActiveEnterTimestamp`), falling
back to the last 24h if that can't be determined.

## Late-booking quorum (`min_notice_hours` / `min_required_participants`)

Occurrences are always shown and bookable right up until they start --
`min_notice_hours` no longer hides anything (it used to). Instead, together
with `min_required_participants` (default `1`), it only gates a LATE
booking, i.e. one made within `min_notice_hours` of start
(`app/webapp.py::App._late_booking_rejection`):

- If the course already has `min_required_participants` confirmed, or this
  booking would be the one that reaches it, it's accepted like any other
  booking -- no warning, nothing special.
- If it would still leave the course short, it's rejected with a short
  message naming the required headcount and the notice window, asking the
  attendee to book earlier next time.
- Never applies once the slot is already full -- that booking only joins
  the waitlist, which can't affect whether the course runs.

Default `min_required_participants = 1` makes this a permanent no-op: a
single confirmed booking always reaches 1 on its own. Raise it only for a
course that genuinely shouldn't run below a certain group size. The booking
page shows a short note about this rule automatically
(`App._policy_note`), but only when `min_required_participants > 1` -- at
the default, there's nothing to explain, so nothing is shown.

## Known simplifications (by design, not oversights)

- **CSRF**: relies on `SameSite=Lax` session cookies rather than a separate
  per-form CSRF token. Reasonable for this app's size/risk profile; revisit
  if that changes.
- **Sessions & rate limiting are in-process memory** (`webapp.SESSIONS`,
  `security.RateLimiter`): fine for the single `wsgiref` worker this runs as;
  they reset on restart and wouldn't be shared if you ever ran multiple
  worker processes. Flagged in code comments where relevant.
- **Waitlist promotion is synchronous and simple**: FIFO by registration
  time, one promotion per cancellation, no partial/batch promotion logic --
  matches the scale of a single weekly course. If your scale is
  meaningfully larger, review whether this still fits before relying on it.
- **No per-IP rate limiting** (only per-key, in-process) -- someone could
  still hammer many different guessed emails at volume without tripping
  the per-email limiter. Worth adding an IP-based limiter too if you
  expect wider exposure than a small community course.
- **Off-box encrypted backups are not configured by this project** -- you
  need to set up your own backup destination and schedule.
