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
        checks.append((f"modified since install: {path}", "warn", f"rpm -V flags: {flags}"))
    if not checks:
        checks.append(("package integrity (rpm -V)", "ok",
                        "only your %config files differ (expected -- tracked via .rpmnew checks above)"))
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
        return [(f"static site ({deployed_path})", "warn",
                  "not deployed yet -- run `my-bt setup -i` to generate it, "
                  "then copy site/*.html as usual (see README.md)")]
    actual = deployed_path.read_text(encoding="utf-8", errors="replace")
    if actual == expected:
        return [(f"static site ({deployed_path})", "ok", "matches current settings.toml")]
    return [(
        f"static site ({deployed_path})", "warn",
        "doesn't match current settings.toml (retention numbers or wording "
        "changed since it was last generated) -- run `my-bt setup -i` to regenerate"
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
