"""Health/setup checks shared by `my-bt status` and `my-bt setup`
(scripts/my-bt). Pure(-ish) data functions: each returns a list of
`(label, level, detail)` triples ("ok"/"warn"/"fail") and never prints or
prompts -- that's `app/cli_setup.py`'s job. Kept in this importable
package (not in scripts/my-bt, which has no .py extension and lives
outside `app/`, so unittest can't reach it directly) specifically so
these are unit-testable the same way everything else in this project is:
mock `subprocess`/`shutil` here, or point a check at a tmp directory,
rather than only ever exercising this via manual smoke testing.

`status` and `setup` both call the exact same functions here -- if you
add a new check, add it once, in one place. Separate copies of this kind
of thing have drifted and caused real bugs in this project before (see
the maintainer's local notes).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from . import site_render

Check = tuple[str, str, str]  # (label, "ok"|"warn"|"fail", detail)


def secret_file_map(raw: dict) -> dict[str, str | None]:
    return {
        "caldav_password": raw.get("calendar", {}).get("caldav_password_file"),
        "smtp_password": raw.get("smtp", {}).get("password_file"),
        "admin_password_hash": raw.get("admin", {}).get("password_hash_file"),
        "erasure_pepper": raw.get("privacy", {}).get("erasure_pepper_file"),
    }


def check_secrets(raw: dict) -> list[Check]:
    checks: list[Check] = []
    for name, path_str in secret_file_map(raw).items():
        if not path_str:
            checks.append((f"secret: {name}", "warn", "not configured in settings.toml"))
            continue
        p = Path(path_str)
        if not p.exists():
            checks.append((f"secret: {name}", "fail", f"missing -- create {path_str}"))
            continue
        content = p.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            checks.append((f"secret: {name}", "fail", f"{path_str} exists but is empty"))
            continue
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode != 0o600:
            checks.append((f"secret: {name}", "warn", f"{path_str} is mode {oct(mode)}, expected 0600"))
        else:
            checks.append((f"secret: {name}", "ok", "present, mode 0600"))
        if name == "admin_password_hash" and "$" not in content:
            checks.append((f"secret: {name}", "fail",
                "doesn't look like a hash (no '$') -- did you paste the "
                "plain password instead of hash_admin_password()'s output? see README.md"))
        if name == "erasure_pepper":
            try:
                if len(bytes.fromhex(content)) != 32:
                    checks.append((f"secret: {name}", "warn",
                        "not 32 bytes of hex -- expected `openssl rand -hex 32` output"))
            except ValueError:
                checks.append((f"secret: {name}", "fail", "not valid hex -- expected `openssl rand -hex 32` output"))
    return checks


def check_systemd() -> list[Check]:
    if not shutil.which("systemctl"):
        return [("systemd", "warn", "systemctl not found -- skipping (not on the target server?)")]
    checks: list[Check] = []
    for unit in ("my-booking.service", "my-booking-retention.timer"):
        enabled = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True
        ).stdout.strip()
        active = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True
        ).stdout.strip()
        if enabled == "enabled" and active in ("active", "waiting"):
            checks.append((unit, "ok", f"enabled, {active}"))
        elif enabled != "enabled":
            checks.append((unit, "warn", f"not enabled -- sudo systemctl enable --now {unit}"))
        else:
            checks.append((unit, "warn", f"enabled but not active ({active or 'unknown'}) -- systemctl status {unit}"))
    return checks


def check_selinux() -> list[Check]:
    if not shutil.which("getenforce"):
        return [("SELinux", "ok", "not present on this system")]
    mode = subprocess.run(["getenforce"], capture_output=True, text=True).stdout.strip()
    if mode != "Enforcing":
        return [("SELinux", "ok", f"{mode or 'not present'} (nginx proxy restriction doesn't apply)")]
    if not shutil.which("getsebool"):
        return [("SELinux", "warn", "enforcing, but getsebool isn't available to check the boolean")]
    out = subprocess.run(
        ["getsebool", "httpd_can_network_connect"], capture_output=True, text=True
    ).stdout.strip()
    if out.endswith("on"):
        return [("SELinux httpd_can_network_connect", "ok", "on")]
    return [("SELinux httpd_can_network_connect", "fail",
             f"{out or 'unknown'} -- sudo setsebool -P httpd_can_network_connect on")]


# The location paths this app needs -- see nginx/my-booking.conf(.example).
# Kept as a plain tuple, not derived from the .example file itself, so this
# check has no dependency on that file's exact on-disk location/format at
# runtime (it only needs to know what to look FOR, not read the example).
_REQUIRED_NGINX_LOCATIONS = ("/book/", "/cancel/", "/my", "/admin")


def check_nginx_locations() -> list[Check]:
    """Checks whether each `location` block this app needs is already
    present in the LIVE, fully-merged nginx config (`nginx -T`, which
    resolves every `include` -- nginx.conf, conf.d/*, sites-enabled/*,
    snippets, etc. -- not just one guessed vhost file, so this can't miss a
    location block just because it lives in a different file than expected).
    Read-only: this never edits nginx config itself -- guessing at and
    rewriting a stranger's hand-maintained vhost would be worse than asking
    (see setup -i's own comment on this)."""
    if not shutil.which("nginx"):
        return [("nginx", "warn", "nginx not found -- skipping (not on the target server?)")]
    result = subprocess.run(["nginx", "-T"], capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return [("nginx -T", "warn", f"failed to read the live config ({detail}) -- check manually with sudo nginx -T")]
    merged = result.stdout
    checks: list[Check] = []
    for path in _REQUIRED_NGINX_LOCATIONS:
        # Matches e.g. "location /book/ {" or "location = /my {" -- allows
        # an optional match modifier (=, ~, ~*, ^~) between "location" and
        # the path, same as nginx itself accepts.
        pattern = re.compile(rf"^\s*location\s+(?:[=~^]+\*?\s+)?{re.escape(path)}\s*\{{", re.MULTILINE)
        if pattern.search(merged):
            checks.append((f"nginx location {path}", "ok", "found in the live config"))
        else:
            checks.append((f"nginx location {path}", "warn",
                            "not found in the live config -- add it from "
                            "/usr/share/my-booking-tool/my-booking.conf.example"))
    return checks


def _iter_server_blocks(merged_config: str):
    """Yields the raw text of each top-level `server { ... }` block in a
    `nginx -T` dump. Depth-tracks braces rather than doing full parsing --
    `nginx -T` only ever prints an already-`nginx -t`-valid config, so
    braces are guaranteed balanced; this is just enough to isolate one
    vhost's own directives (e.g. `root`) from any `location` sub-blocks it
    contains, without needing a real nginx-config parser."""
    depth = 0
    current: list[str] | None = None
    start_depth = None
    for line in merged_config.splitlines():
        if current is None and re.match(r"^\s*server\s*\{", line):
            current = [line]
            start_depth = depth
            depth += line.count("{") - line.count("}")
            continue
        if current is not None:
            current.append(line)
            depth += line.count("{") - line.count("}")
            if depth == start_depth:
                yield "\n".join(current)
                current = None
                start_depth = None
            continue
        depth += line.count("{") - line.count("}")


def _nginx_root_for_host(raw: dict) -> str | None:
    """Returns nginx's `root` for the server block matching
    `[site].base_url`'s hostname, or None if nginx/that block/its root
    can't be determined. Shared by check_static_pages_reachable()."""
    hostname = urlparse(raw.get("site", {}).get("base_url", "")).hostname
    if not hostname or not shutil.which("nginx"):
        return None
    result = subprocess.run(["nginx", "-T"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    name_re = re.compile(r"^\s*server_name\s+([^;]+);", re.MULTILINE)
    root_re = re.compile(r"^\s*root\s+([^;]+);", re.MULTILINE)
    for block in _iter_server_blocks(result.stdout):
        names = name_re.search(block)
        if not names or hostname not in names.group(1).split():
            continue
        root_match = root_re.search(block)
        return root_match.group(1).strip() if root_match else None
    return None


# Pages this tool never templates/generates (unlike privacy.html -- see
# check_static_site_drift) but that still benefit from knowing whether the
# LIVE deployed copy matches what's in the checkout.
_STATIC_PAGES_TO_DEPLOY = ("index.html", "impressum.html", "terms.html")


def check_static_pages_reachable(raw: dict) -> list[Check]:
    """For each page my-bt has actually put in `[site].static_site_dir`,
    checks it's reachable from nginx's real `root` for that host -- either
    because static_site_dir IS that root, or because a symlink (or copy)
    for that specific file exists there, same as an already-working
    `index.html -> ../index.html` symlink would. Deliberately does NOT
    assume static_site_dir should just equal nginx's root: some setups
    keep a git-tracked staging directory (static_site_dir) separate from
    the public webroot on purpose (e.g. to avoid exposing a `.git` folder
    under the public root), symlinking in only the specific files meant to
    be public -- see the maintainer's local notes. So the suggested fix here is always
    a per-file symlink, never "change static_site_dir". Hit in practice
    2026-07-05: privacy.html/terms.html/impressum.html existed in
    static_site_dir but had no matching symlink in nginx's actual root, so
    they 404'd for every visitor despite `status` reporting them fine."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    nginx_root = _nginx_root_for_host(raw)
    if nginx_root is None:
        return []  # nginx missing/not configured for this host -- nothing to cross-check
    if Path(nginx_root).resolve() == Path(static_site_dir).resolve():
        return []  # same directory -- every file is trivially reachable, nothing to report
    checks: list[Check] = []
    for name in (site_render.OUTPUT_NAME,) + _STATIC_PAGES_TO_DEPLOY:
        managed = Path(static_site_dir) / name
        if not managed.exists():
            continue  # not deployed at all yet -- already covered by other checks
        served = Path(nginx_root) / name
        if served.exists() and served.resolve() == managed.resolve():
            checks.append((f"nginx-reachable: {name}", "ok", f"symlinked/present at {served}"))
        else:
            checks.append((f"nginx-reachable: {name}", "warn",
                            f"{managed} exists but nginx's root ({nginx_root}) has no matching "
                            f"file -- visitors get a 404. Fix: ln -s {managed} {served}"))
    return checks


def _resolve_static_source(home: str, name: str) -> Path | None:
    """The checkout's real site/<name>, falling back to site/<name>.example
    if no real copy exists locally -- same real-preferred-over-.example
    precedence as everywhere else in this project (scripts/render-site.py's
    resolve_real_or_example(), scripts/install.sh's _src(), etc.)."""
    real = Path(home) / "site" / name
    if real.exists():
        return real
    example = Path(home) / "site" / f"{name}.example"
    if example.exists():
        return example
    return None


def check_static_pages_deployed(raw: dict, home: str) -> list[Check]:
    """For each of index.html/impressum.html/terms.html: is it deployed to
    [site].static_site_dir at all, and if so, does it match what's in this
    checkout right now? Purely informational -- these pages are
    hand-authored and deliberately never auto-copied (README.md
    "Static-site pages") -- but staying silent about a missing or stale
    page was itself the bug: an index.html footer edit sat in the checkout
    for weeks without ever reaching the live site, and neither `status`
    nor `setup` ever mentioned it (hit in practice 2026-07-05)."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    checks: list[Check] = []
    for name in _STATIC_PAGES_TO_DEPLOY:
        deployed = Path(static_site_dir) / name
        source = _resolve_static_source(home, name)
        if source is None:
            continue  # nothing in the checkout to compare against either
        if not deployed.exists():
            checks.append((f"static site content ({deployed})", "warn",
                            f"not deployed yet -- copy {source} there"))
            continue
        same = deployed.read_text(encoding="utf-8", errors="replace") == \
            source.read_text(encoding="utf-8", errors="replace")
        if same:
            checks.append((f"static site content ({deployed})", "ok", "matches your checkout"))
        else:
            checks.append((f"static site content ({deployed})", "warn",
                            f"differs from your checkout's {source} -- copy it over if that's the newer version"))
    return checks


def check_rpmnew(paths: list[str]) -> list[Check]:
    """settings.toml and the installed site/privacy.html.tmpl are both
    %config(noreplace) in the RPM (see packaging/*.spec) -- rpm never
    overwrites a locally-modified copy of either on upgrade, but if the
    packaged version also changed, it drops the new one alongside yours as
    `<path>.rpmnew` instead of silently discarding either side. Surface
    that here so a pending manual merge doesn't go unnoticed."""
    checks: list[Check] = []
    for path_str in paths:
        rpmnew = Path(path_str + ".rpmnew")
        name = Path(path_str).name
        if not rpmnew.exists():
            checks.append((f"{name}.rpmnew", "ok", "none pending -- nothing to merge"))
        else:
            checks.append((
                f"{name}.rpmnew ({rpmnew})", "warn",
                "a newer packaged version is waiting to be merged in by hand: "
                f"vimdiff {path_str} {rpmnew}, then remove the .rpmnew"
            ))
    return checks


def check_group_membership() -> list[Check]:
    import getpass
    import grp

    target_user = os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        g = grp.getgrnam("my-booking")
    except KeyError:
        return [("my-booking group membership", "warn",
                  "group 'my-booking' doesn't exist yet -- install the package first")]
    if target_user == "root" or target_user in g.gr_mem:
        return [(f"my-booking group membership ({target_user})", "ok", "already a member")]
    return [(
        f"my-booking group membership ({target_user})", "warn",
        f"not in the my-booking group yet -- sudo usermod -aG my-booking {target_user} "
        "(log out/in, or start a new shell, for it to take effect)"
    )]


# %config(noreplace) paths whose own drift is already tracked via
# check_rpmnew() above -- check_rpm_verify() skips these so a locally
# customized settings.toml/privacy.html.tmpl (the expected, supported case)
# doesn't also show up as a scary "modified since install" package-wide
# warning; rpm -V still catches anything unexpected elsewhere.
_KNOWN_CONFIG_MARKERS = ("c",)

# rpm -V's 9-char flag string, position by position: S=size M=mode 5=MD5
# sum D=device L=readlink U=user G=group T=mtime P=capabilities. Only
# S/5/L/D indicate the file's actual DATA (or type) changed -- everything
# else is metadata. %post's `chown -R my-booking:my-booking /etc/my-booking`
# (see packaging/my-booking-tool.spec) deliberately changes ownership away
# from whatever the RPM recorded at build time, so a U/G(/M/T)-only diff on
# a file under /etc/my-booking is the package's OWN intended behavior, not
# tampering -- flagging it as "modified since install" was a false
# positive (hit in practice 2026-07-05 on settings.toml.example). "missing"
# (the file doesn't exist at all) is always worth flagging regardless.
_CONTENT_FLAGS = frozenset("S5LD")


def check_rpm_verify(package_name: str = "my-booking-tool") -> list[Check]:
    """`rpm -V <package>` compares every file the package owns (size, mode,
    checksum, owner, mtime, ...) against what was actually installed --
    catches a manual edit to *any* packaged file, not just the ones
    deliberately marked %config(noreplace) (systemd units, the nginx
    example, the app code itself). Report-only: unlike %config(noreplace),
    these files get silently overwritten back to the packaged version on
    the next upgrade regardless, so this is purely "something changed,
    here's what" -- not a merge workflow like check_rpmnew()."""
    if not shutil.which("rpm"):
        return [("package integrity (rpm -V)", "ok", "rpm not present -- not an rpm install, skipping")]
    q = subprocess.run(["rpm", "-q", package_name], capture_output=True, text=True)
    if q.returncode != 0:
        return [("package integrity (rpm -V)", "ok",
                  "not installed via rpm -- skipping (manual scripts/install.sh install?)")]
    v = subprocess.run(["rpm", "-V", package_name], capture_output=True, text=True)
    lines = [ln for ln in v.stdout.splitlines() if ln.strip()]
    if not lines:
        return [("package integrity (rpm -V)", "ok", "no unexpected changes to any packaged file")]

    checks: list[Check] = []
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        flags, path = parts[0], parts[-1]
        # rpm -V line shape: "<9-char flags>[ <type-marker>] <path>" -- the
        # type marker (one of c/d/g/l/r) only appears when that file is
        # %config/%doc/%ghost/%license/%readme *and* differs from the
        # package's record. A "missing" file reports the literal word
        # "missing" in place of the flag string.
        marker = parts[1] if len(parts) >= 3 and parts[1] in ("c", "d", "g", "l", "r") else ""
        if marker in _KNOWN_CONFIG_MARKERS:
            continue  # settings.toml / privacy.html.tmpl -- tracked via check_rpmnew() instead
        if flags != "missing" and not any(f in flags for f in _CONTENT_FLAGS):
            continue  # ownership/mode/mtime-only -- expected (see _CONTENT_FLAGS above), not tampering
        checks.append((f"modified since install: {path}", "warn", f"rpm -V flags: {flags}"))
    if not checks:
        checks.append(("package integrity (rpm -V)", "ok",
                        "no unexpected content changes (ownership/mode/mtime-only differences -- "
                        "e.g. from the package's own postinstall chown -- and %config files, "
                        "tracked via .rpmnew checks above, are expected and excluded)"))
    return checks


def check_static_site_drift(raw: dict, template_path: str | Path) -> list[Check]:
    """If `[site].static_site_dir` is configured, compares the LIVE
    deployed privacy.html there against what the current settings.toml
    would actually render -- catches the case this whole mechanism exists
    for: someone changes retention_months in settings.toml but never
    re-runs the render step, so the public page silently keeps quoting the
    old number. A no-op (empty list) if static_site_dir isn't configured,
    same as the optional log_file check."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    template_path = Path(template_path)
    if not template_path.exists():
        return [("static site (privacy.html)", "warn",
                  f"can't check -- template not found at {template_path}")]
    privacy = raw.get("privacy", {})
    retention_months = privacy.get("retention_months", 24)
    canceled_retention_months = privacy.get("canceled_retention_months", 6)
    expected = site_render.render_privacy_html(template_path, retention_months, canceled_retention_months)

    deployed_path = Path(static_site_dir) / site_render.OUTPUT_NAME
    if not deployed_path.exists():
        # State only -- no "run `my-bt setup -i`" instruction here: this
        # same message is shown both by `my-bt status` (which already ends
        # its report with that instruction once, generically) and inside
        # `my-bt setup -i` itself, where telling the user to run the very
        # command they're already running is just confusing (see
        # the maintainer's local notes).
        return [(f"static site ({deployed_path})", "warn",
                  "not deployed yet -- once generated, copy site/*.html to "
                  "your live host as usual (see README.md)")]
    actual = deployed_path.read_text(encoding="utf-8", errors="replace")
    if actual == expected:
        return [(f"static site ({deployed_path})", "ok", "matches current settings.toml")]
    return [(
        f"static site ({deployed_path})", "warn",
        "doesn't match current settings.toml (retention numbers or wording "
        "changed since it was last generated)"
    )]


# Sentinel left in every tracked site/*.html.example placeholder (see
# site/index.html.example etc.) specifically so this check has something
# reliable to grep for -- if you see this in a LIVE page, the generic
# template was published without being customized first.
_PLACEHOLDER_MARKER = "REPLACE-ME"
_UNSUBSTITUTED_PLACEHOLDER = re.compile(r"\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

# Just the pages this project ships a generic .example starting point
# for (see "site/*.example" in the repo) -- privacy.html's own drift
# (stale retention numbers) is already covered by check_static_site_drift
# above, so this only adds the placeholder-marker check for it, same as
# the other three.
_SITE_PAGES = ("index.html", "impressum.html", "terms.html", "privacy.html")


def check_static_site_compliance(raw: dict) -> list[Check]:
    """If `[site].static_site_dir` is configured, checks the LIVE deployed
    site/*.html pages for signs the generic `.example` placeholder (see
    site/index.html.example et al) was published without being customized
    first: a leftover "REPLACE-ME" marker, or literal unsubstituted
    `${...}` template syntax that should have been filled in by
    app/site_render.py. Deliberately does NOT inspect or judge the actual
    legal wording you *did* write -- see README.md's disclaimer, that part
    is entirely your call/responsibility. This only catches the cheap,
    unambiguous mistake of "forgot to replace the placeholder at all"."""
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        return []
    checks: list[Check] = []
    for name in _SITE_PAGES:
        p = Path(static_site_dir) / name
        if not p.exists():
            continue  # optional/manual per-page copy -- nothing to flag yet
        text = p.read_text(encoding="utf-8", errors="replace")
        problems = []
        if _PLACEHOLDER_MARKER in text:
            problems.append(f'still contains a "{_PLACEHOLDER_MARKER}" placeholder marker')
        if _UNSUBSTITUTED_PLACEHOLDER.search(text):
            problems.append("still contains an unsubstituted ${...} template placeholder")
        if problems:
            checks.append((f"static site content ({p})", "warn", "; ".join(problems)))
        else:
            checks.append((f"static site content ({p})", "ok", "no leftover placeholder markers"))
    return checks
