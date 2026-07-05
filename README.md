# my-booking-tool

Self-hosted booking tool for a small set of recurring classes/sessions --
built as a lightweight replacement for a third-party group-booking widget.
Stdlib-only Python, CSV storage, CalDAV integration for calendar sync
(works with any CalDAV provider, e.g. mailbox.org, Nextcloud, etc.), guest
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
  `site/terms.html.example` -- generic, tracked in git, safe to publish.
  Placeholder content throughout, marked with `REPLACE-ME`.
- `settings.toml`, `site/index.html`, `site/impressum.html`,
  `site/privacy.html`, `site/privacy.html.tmpl`, `site/terms.html` -- your
  own real, filled-in versions. **Gitignored on purpose**: if you already
  have these (a real deployment), they are never overwritten, never
  deleted, and never published -- by this repo's own `.gitignore`, by
  `my-bt`, or by the RPM/install scripts. `%config(noreplace)` in the RPM
  spec gives the same guarantee at the installed-system level for the two
  of these (`settings.toml`, `site/privacy.html.tmpl`) that `my-bt` reads
  at runtime -- see "Installing" below.

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

`my-bt status`/`setup` check the live, deployed copies of `site/*.html`
for a leftover `REPLACE-ME` marker or an unsubstituted `${...}` template
placeholder -- catching the mistake of publishing the generic template
without customizing it first (see "Static-site pages" below). They never
inspect or judge the legal wording you actually chose; that part is
entirely on you (see the disclaimer above).

## Layout

```
app/                        the application (stdlib-only Python package)
  config.py                 settings.toml + secrets loader
  storage.py                CSV read/write, locking, right-to-erasure archival
  slots.py                  weekday/time occurrence math + waitlist-aware capacity
  caldav_client.py          minimal CalDAV client (PROPFIND/REPORT/PUT/DELETE)
  calendar_sync.py          keeps one VEVENT per course occurrence in sync
  ics.py                    minimal iCalendar build/parse
  emailer.py                SMTP client
  security.py               tokens/password hashing, erasure hashing, rate limiting
  erasure.py                GDPR Art. 17 orchestration
  retention.py              GDPR Art. 5(1)(e) purge job (the "cronjob")
  site_render.py            renders site/privacy.html -- see "Static-site pages"
  cli_checks.py             `my-bt status`/`setup` health checks -- pure, unit-tested
  cli_setup.py              `my-bt setup`/`setup -i` report + walkthrough logic
  version.py                `my-bt --version` (package version + git commit)
  webapp.py                 wsgiref WSGI app / routes
  serve.py                  entrypoint (python3 -m app.serve)

tests/                      unit tests (168, run with `my-bt test` or unittest)

scripts/
  my-bt                     thin CLI wrapper -- see "The `my-bt` CLI" below
  install.sh                manual/dev installer (fallback -- see "Installing")
  build-rpm.sh              builds the Fedora RPM (the recommended path)
  render-site.py            regenerates site/privacy.html (run by build-rpm.sh)

packaging/
  my-booking-tool.spec      RPM spec

systemd/                    my-booking.service, my-booking-retention.{service,timer}
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
(`scripts/render-site.py`) and run time (`my-bt setup -i`).

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

`%post` itself only prints a short pointer to `my-bt setup` (see "The
`my-bt` CLI" below) -- that command generates the full list dynamically,
checking what's already done instead of always repeating a static wall of
text (the old approach, where this list was duplicated across `%post`,
`scripts/install.sh`, and this README, drifted out of sync more than once).
Run `my-bt setup` any time to see it again, or `my-bt setup --interactive`
to be walked through it step by step. What follows is the same list, in
full detail, for reference:

1. Create secrets in `/etc/my-booking/secrets/` (mode 600, owned by
   `my-booking`), four files:
   - `caldav_password`, `smtp_password` -- plain text, your CalDAV/SMTP
     account password(s).
   - `erasure_pepper` -- random hex: `openssl rand -hex 32`.
   - `admin_password_hash` -- **not** plain text, a hash. Generate it with
     `my-bt hash-password` (prompts for the password with hidden input --
     it's never typed into a command line, so it never ends up in shell
     history), then save the printed output into the file, e.g.:
     `my-bt hash-password | sudo tee /etc/my-booking/secrets/admin_password_hash`.
     `my-bt status` (see below) specifically checks for and flags the
     common mistake of pasting the plain password here instead.

   The directory itself is already correctly SELinux-labeled by the RPM
   (and re-labeled via `restorecon` on every install/upgrade), and a file
   created *directly* inside it inherits that label automatically -- but
   `mv` (unlike `cp`) preserves a file's *original* label, so if you draft
   a secret elsewhere first and move it in, run `sudo restorecon -Rv
   /etc/my-booking/secrets` afterwards to be safe.
2. Review `/opt/my-booking/settings.toml` and
   `/opt/my-booking/site/privacy.html.tmpl` -- both are `%config(noreplace)`
   files (reinstalling/upgrading the RPM never overwrites your edits to
   either). If the packaged version of one also changed since you edited
   it, rpm can't just pick a side: it saves the new version alongside yours
   as `<file>.rpmnew` instead, and `%post` (and `my-bt status`/`my-bt
   setup`) flag it loudly so a pending merge can't go unnoticed. Merge by
   hand, then remove the `.rpmnew`, e.g.:
   `sudo vimdiff /opt/my-booking/settings.toml /opt/my-booking/settings.toml.rpmnew`.

   Every *other* file the package installs (systemd units, app code, the
   nginx example) isn't meant to be hand-edited, so it doesn't get the
   `%config(noreplace)` treatment -- instead `my-bt status`/`setup` run
   `rpm -V my-booking-tool` (rpm's own file-integrity verifier) and report
   any drift they find there too, so an accidental edit anywhere in the
   package still surfaces instead of silently persisting across upgrades.
3. Add the location blocks from
   `/usr/share/my-booking-tool/my-booking.conf.example` to your existing
   nginx vhost config, then `nginx -t && systemctl reload nginx`.
4. `sudo usermod -aG my-booking <your-login>` so `my-bt` works without sudo.
5. `sudo systemctl enable --now my-booking.service my-booking-retention.timer`
6. If SELinux is enforcing (default on Fedora -- check `getenforce`):
   `sudo setsebool -P httpd_can_network_connect on`. Without this, nginx
   (which runs as the confined `httpd_t` domain) is blocked from
   `proxy_pass`-ing to the app's local port, and `/book`, `/cancel`, `/my`,
   `/admin` all 502 even though the app itself is running fine -- confirm
   with `sudo ausearch -m avc -ts recent` if you hit this. The RPM's
   `%post` also relabels `/opt/my-booking`, `/etc/my-booking`, and
   `/var/lib/my-booking` via `restorecon` as a safety net.
7. **Not done by this package** -- your live static site is a separate
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
for the full option list. Highlights:

```
my-bt --version                         # package version + git commit it was built from

my-bt list                              # all registrations, live + archived
my-bt list --live                       # only the live CSV
my-bt list --archive                    # only the archived (erased) CSV
my-bt list --year 2026 --course example-monday-class
my-bt list --status waitlisted --email guest@example.com
my-bt list --format json   # or --format csv

my-bt users [--email ...] [--live|--archive]
my-bt show <registration_id>
my-bt stats [--year 2026]

my-bt hash-password                     # prompts (hidden input), prints
                                         # the admin_password_hash value

my-bt erase --email guest@example.com          # asks for confirmation
my-bt erase --email guest@example.com --yes    # scripted/non-interactive

my-bt purge-retention [--dry-run]       # same purge the nightly timer runs
my-bt test [--repo-root /path/to/checkout]      # runs the unit test suite

my-bt status                            # health check -- see below
my-bt setup                             # guided post-install steps -- see below
my-bt setup --interactive               # ...or -i: be walked through them

my-bt -D erase --email guest@example.com   # -D/--debug: full traceback on
                                            # error instead of one clean line
                                            # (same as MY_BOOKING_DEBUG=1,
                                            # just for this one command)
my-bt -L status                            # -L/--log: also append this
                                            # run's output to settings.toml's
                                            # [logging].log_file (configure
                                            # that first, see below)
```

`my-bt erase` only touches the CSVs (no CalDAV dependency by design, so it
works even if your CalDAV/SMTP provider is unreachable); if the erased
guest had a future confirmed/waitlisted booking, the app's own
cancellation path re-syncs the calendar the next time it touches that
occurrence. If you need the calendar updated immediately after a CLI
erase, restart `my-booking.service` or just wait for the next
booking/cancellation on that occurrence.

### `my-bt status`

A guided health check across the whole install -- run this first whenever
something seems off, or after any install/reinstall:

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
- `my-booking.service` and `my-booking-retention.timer`: enabled and
  active.
- SELinux: enforcing or not, and if enforcing, whether
  `httpd_can_network_connect` is on (see the SELinux note above).
- `rpm -V my-booking-tool`: report-only integrity check across every file
  the package owns (not just the two `%config(noreplace)` ones) --
  flags any other packaged file (systemd units, app code, ...) that's
  been hand-modified since install, so drift there doesn't go unnoticed
  either. Doesn't block anything; it's a heads-up, not an enforcement.
- If `[site].static_site_dir` is set: whether the live `privacy.html` at
  that path actually matches what current `settings.toml` values would
  render (see "Static-site pages" below) -- catches a `retention_months`
  edit that hasn't been pushed out to the live page yet -- and whether any
  live `site/*.html` page still contains a leftover `REPLACE-ME` or
  `${...}` placeholder (i.e. the generic template was published without
  being customized).

Each line is `[OK]`/`[WARN]`/`[FAIL]` with a one-line fix where relevant;
exits non-zero if anything is `[FAIL]`. Deliberately doesn't touch the
network/CalDAV (same reasoning as `erase` -- no CalDAV dependency by
design), so it still works to narrow things down even if your CalDAV/SMTP
provider itself is unreachable.

### `my-bt setup` / `my-bt setup --interactive`

The same checks `status` runs, reorganized as an 8-step guided post-install
list (secrets, `.rpmnew` merge, a `settings.toml` values summary, nginx,
group membership, systemd, SELinux, the static site) -- this is the single
source of truth for those steps now; `%post` and `scripts/install.sh` just
point here instead of each keeping their own copy of the text (which used
to drift out of sync). The logic itself lives in `app/cli_checks.py` (the
check functions) and `app/cli_setup.py` (report-printing and the
interactive walkthrough) -- `scripts/my-bt` is just a thin
argument-parsing wrapper around them, which is also what makes them
unit-testable (`tests/test_cli_checks.py`, `tests/test_cli_setup.py`)
without needing a real tty/root/systemd/rpm.

Plain `my-bt setup` prints the list, annotating each item with whatever
`status` would say about it, so a re-run after partial setup shows only
what's actually left. Add `-i`/`--interactive` to be walked through it
step by step and have `my-bt` perform what it safely can:

- Missing secrets: prompts and writes them (hidden input for passwords;
  offers to auto-generate `erasure_pepper`; reuses the same hashing as
  `my-bt hash-password` for `admin_password_hash`), mode 0600.
- A pending `settings.toml.rpmnew` or `privacy.html.tmpl.rpmnew`: offers to
  open `vimdiff` for you.
- Group membership, enabling the systemd units, and the SELinux boolean:
  offered when run as root (needs `sudo my-bt setup -i` for these --
  without root it tells you the exact command instead of guessing).
- nginx: never automated (editing your existing, hand-maintained vhost
  isn't something to guess at), always shown as a reminder.
- The static site: if `[site].static_site_dir` is configured, offers to
  (re)generate `privacy.html` there from the current `settings.toml`
  values right away -- no rebuild/reinstall needed. See "Static-site
  pages" below.

## Logs & debugging

**Viewing logs:**

```
journalctl -u my-booking.service              # the web app
journalctl -u my-booking-retention.service    # the nightly retention job
journalctl -u my-booking.service -f           # follow live
journalctl -u my-booking.service --since "1 hour ago"
```

By default this is quiet -- routine operation isn't logged -- but real
problems are never silenced: an unhandled exception anywhere in a request
always logs at ERROR with the full traceback, and a few other
always-worth-seeing events (the nightly retention summary, a guest
self-erasing their account via `/my`) log at WARNING. Both levels show up
with no extra configuration.

**Verbose mode:** set `MY_BOOKING_DEBUG=1` for full tracing -- every
request (method + path only, never form data/cookies), every CalDAV call
(method/path/HTTP status, never credentials/calendar contents), and every
outgoing email attempt (subject + masked recipient, e.g. `k***@example.com`
-- never the full address). Same for `my-bt`, via either the env var or its
own `-D`/`--debug` flag (identical effect, `-D` is just easier to remember
for a one-off command than the env var name): without it, a failing
command prints one clean line (`error: ...`); with it, the full Python
traceback.

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

**A single log file** (in addition to journald), if you'd rather `tail -f`
or attach one file than juggle `journalctl` across two units: set
`[logging].log_file` in `settings.toml` (commented out by default; see the
example there) to a path writable by the `my-booking` user -- the default
suggestion, `/var/lib/my-booking/my-booking.log`, is inside a directory the
RPM already creates with the right ownership, so no extra setup needed if
you use it as-is. Once set, the web service and the retention job append
there automatically (no restart-time flag needed, just restart the
service/timer so it picks up the new setting). For `my-bt`, add `-L`/
`--log` to any command to append that run's *entire* output (not just log
records -- the actual table/JSON output too) to the same file, with a
timestamped `=== my-bt ... ===` line marking where each run starts, so a
file with several runs in it stays easy to read.

**First thing to try if something's wrong:** `my-bt status` (see above) --
it checks most of what actually goes wrong in practice (a missing/
misconfigured secret, a disabled systemd unit, the SELinux boolean) before
you need to dig through logs at all.

**Before sharing logs** (with anyone): `journalctl` output is meant to be
safe to paste as-is under normal (non-debug) operation -- log lines are
written to avoid raw guest emails/names on purpose (user IDs instead,
masked email prefixes, etc.). In `MY_BOOKING_DEBUG=1` mode it's still
designed to avoid raw addresses, but skim before pasting anyway,
especially anything unexpected (e.g. an exception message that happens to
include user-supplied text). Note also that journald has its own log
retention, independent of this app's own `retention_months` GDPR setting --
another reason to keep `MY_BOOKING_DEBUG` off except when actively
troubleshooting.

## Testing

```
my-bt test                       # from anywhere, once installed
python3 -m unittest discover -s tests -t . -v   # from this checkout
```

168 tests covering slot generation (including DST via `zoneinfo`, and that
occurrences stay bookable right up to start), CSV storage/locking/CSV-injection
guarding, atomic capacity-checked booking (no overbooking race), the
late-booking quorum gate (`min_required_participants`), the CalDAV client
(mocked transport, no network) and multi-calendar conflict-checking,
erasure/archival, retention-purge boundaries, ICS build/parse/line-folding,
token/PIN hashing, rate limiting, the spots-left display A/B-test knob
(never fakes "FULL", never drops below "1 spot(s) left" while still
bookable-as-confirmed), `site/privacy.html` rendering (`test_site_render.py`),
the `my-bt status`/`setup` health checks and interactive walkthrough
(`test_cli_checks.py`, `test_cli_setup.py` -- every side effect, including
prompting and running external commands, is a fake, so these don't need
root/systemd/rpm/a real tty), and the real-file-vs-generic-.example
resolution used by the build/install scripts (`test_render_site_script.py`
-- explicitly asserts a real file is never modified, deleted, or replaced
by its `.example` counterpart).

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
enforces it; `my-bt purge-retention --dry-run` lets you preview what the next
run would remove.

**Right to erasure** (Art. 17): a guest can delete their own account from
`/my`, or you can run `my-bt erase --email ...` on their behalf. Either way:
any future confirmed/waitlisted booking is canceled first (freeing the spot
for the waitlist), then the user row and all their registration rows move
from the live CSVs into `data/archived/{users,registrations}.csv` with the
email replaced by a **keyed** HMAC-SHA256 hash (`security.hash_email_for_erasure`,
key = `secrets/erasure_pepper`). A keyed hash is what makes this a real
erasure rather than security theatre: a bare `sha256(email)` is reversible by
dictionary/rainbow-table attack since email addresses are low-entropy and
guessable; keying it with a secret pepper that's never stored alongside the
archive removes that attack. `my-bt list`/`users` query live and archived
data together (or separately with `--live`/`--archive`) so you retain
statistical/audit value (how many sessions happened, aggregate attendance)
without retaining identifiable personal data past the point someone asked to
be forgotten.

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
    `site/`), `my-bt status` compares that live `privacy.html` against
    what current `settings.toml` values would render and warns on drift,
    and `my-bt setup --interactive` offers to regenerate it right there.
    This closes the gap where changing just `retention_months` in
    `settings.toml` used to require a full rebuild+reinstall before the
    live legal page reflected it -- now it's one `my-bt setup -i` away.
    `site/privacy.html.tmpl` itself is also `%config(noreplace)` (see
    "Installing" above), so a package upgrade never clobbers wording
    edits you've made to it.
- `terms.html` -- your participation/liability disclaimer as a full page,
  matching whatever checkbox text you show on the booking form itself
  (`app/webapp.py`) -- keep the two in sync by hand.

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

`my-bt status`/`setup -i` actively help with that step now (added
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

## Spots-left display (`[defaults]` in `settings.toml`)

`show_spots_left` (default `true`) toggles the "N spot(s) left" / "FULL,
join waitlist" text on the booking page on or off entirely.

`spots_left_offset` (default `0`) shifts the *displayed* number, for
A/B-testing whether perceived scarcity changes booking behaviour --
positive shows fewer spots than are really available (more urgency),
negative shows more. This is deliberately display-only
(`app/webapp.py::_spots_left_text`):

- The actual confirmed-vs-waitlisted decision always uses the true
  confirmed count (`Store.add_registration_checking_capacity`) -- this
  setting can never cause over-booking or a wrongly-waitlisted guest, no
  matter what number is shown.
- An occurrence that's genuinely full always says "FULL, join waitlist,"
  regardless of the offset -- what that promises in the confirmation email
  has to stay true. Only the number shown while there's real room left is
  adjustable, and it's floored at "1 spot(s) left" (never "0" while a
  booking from there would in fact still be confirmed) and capped at the
  course's real capacity.

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
  guest to book earlier next time.
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
