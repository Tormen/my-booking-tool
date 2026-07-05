"""Builds and prints/walks-through `my-bt setup`'s report (scripts/my-bt).
Kept in this importable package (see app/cli_checks.py's docstring for
why) so the interactive walkthrough is unit-testable: every
side-effecting dependency -- asking a yes/no question, reading a secret,
running an external command, checking for root -- is injected as a plain
callable, defaulting to the real thing. Tests substitute canned
answers/fake runners instead of piping stdin at a real process or
needing an actual tty/root/systemd/rpm on the machine running the suite.
"""
from __future__ import annotations

import getpass
import os
import secrets as _secrets
import subprocess
from pathlib import Path
from typing import Callable

from . import cli_checks, security as app_security, site_render


def _default_prompt(message: str) -> bool:
    try:
        return input(f"{message} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _default_read_secret(label: str) -> str:
    return getpass.getpass(f"{label}: ")


def _default_is_root() -> bool:
    return os.geteuid() == 0


def _run_tolerant(cmd: list[str], print_fn: Callable[[str], None]) -> None:
    """subprocess.call, but tolerant of the binary not being installed -- a
    minimal Fedora image may be missing e.g. vim-enhanced (`vimdiff`) or
    policycoreutils-python-utils (`setsebool`), and a missing tool here
    shouldn't crash the whole interactive walkthrough, just that step."""
    try:
        subprocess.call(cmd)
    except FileNotFoundError:
        print_fn(f"[fail] {cmd[0]}: not installed -- run this step manually: {' '.join(cmd)}")


def tmpl_path(home: str) -> Path:
    return Path(home) / "site" / site_render.TEMPLATE_NAME


def _course_summary(raw: dict) -> str:
    return ", ".join(c.get("shortname", "?") for c in raw.get("course", [])) or "(none configured)"


def _privacy_summary(raw: dict) -> str:
    privacy = raw.get("privacy", {})
    return (f"retention_months={privacy.get('retention_months', 24)}, "
            f"canceled_retention_months={privacy.get('canceled_retention_months', 6)}")


def build_report(raw: dict, settings_path: str, home: str) -> dict[str, list[cli_checks.Check]]:
    """All the check groups `setup` shows, computed once here so the
    printed and interactive modes (and `status`, for the ones it also
    reports) can't drift out of sync with each other."""
    config_paths = [settings_path, str(tmpl_path(home))]
    return {
        "secrets": cli_checks.check_secrets(raw),
        "rpmnew": cli_checks.check_rpmnew(config_paths),
        "group": cli_checks.check_group_membership(),
        "systemd": cli_checks.check_systemd(),
        "selinux": cli_checks.check_selinux(),
        "nginx_locations": cli_checks.check_nginx_locations(),
        "static_site": cli_checks.check_static_site_drift(raw, tmpl_path(home)),
        "static_site_compliance": cli_checks.check_static_site_compliance(raw),
        "static_pages_deployed": cli_checks.check_static_pages_deployed(raw, home),
        "static_pages_reachable": cli_checks.check_static_pages_reachable(raw),
    }


def print_report(raw: dict, settings_path: str, home: str, print_fn: Callable[[str], None] = print) -> None:
    report = build_report(raw, settings_path, home)

    def show(checks: list[cli_checks.Check]) -> None:
        for label, level, detail in checks:
            print_fn(f"   [{level.upper():4}] {label}" + (f" -- {detail}" if detail else ""))

    print_fn("my-booking-tool: setup steps (generated fresh from current state,")
    print_fn("full detail also in README.md)\n")

    print_fn("1. Secrets in /etc/my-booking/secrets/ (mode 600, owned by my-booking):")
    show(report["secrets"])
    print_fn("   Generate erasure_pepper with: openssl rand -hex 32")
    print_fn("   Generate admin_password_hash with: my-bt hash-password")
    print_fn("     my-bt hash-password | sudo tee /etc/my-booking/secrets/admin_password_hash")
    print_fn("   If written elsewhere and `mv`d in: sudo restorecon -Rv /etc/my-booking/secrets")

    print_fn("\n2. Pending .rpmnew merges (settings.toml, privacy.html.tmpl):")
    show(report["rpmnew"])

    print_fn(f"\n3. Review {settings_path}:")
    print_fn(f"   courses: {_course_summary(raw)}")
    print_fn(f"   {_privacy_summary(raw)}")

    print_fn("\n4. nginx location blocks (checked live via `nginx -T`):")
    show(report["nginx_locations"])
    if any(level != "ok" and label.startswith("nginx location ")
           for label, level, _ in report["nginx_locations"]):
        print_fn("   Add any missing block(s) from /usr/share/my-booking-tool/my-booking.conf.example")
        print_fn("   to your existing vhost (not edited automatically -- see README.md \"Installing\"),")
        print_fn("   then: sudo nginx -t && sudo systemctl reload nginx")

    print_fn("\n5. my-booking group membership:")
    show(report["group"])

    print_fn("\n6. Service + retention timer:")
    show(report["systemd"])

    print_fn("\n7. SELinux:")
    show(report["selinux"])

    print_fn("\n8. Static site (site/*.html on your live host):")
    static_site_checks = (
        report["static_site"] + report["static_site_compliance"]
        + report["static_pages_deployed"] + report["static_pages_reachable"]
    )
    if static_site_checks:
        show(static_site_checks)
    else:
        print_fn("   [SKIP] [site].static_site_dir not configured -- not checked")
    print_fn("   Swap each course's booking link to /book/<shortname> if you haven't.")

    print_fn("\nRun `my-bt setup --interactive` to be walked through what's left.")


def interactive_setup(
    raw: dict,
    settings_path: str,
    home: str,
    *,
    prompt: Callable[[str], bool] = _default_prompt,
    read_secret: Callable[[str], str] = _default_read_secret,
    run: Callable[[list[str]], None] | None = None,
    is_root: Callable[[], bool] = _default_is_root,
    print_fn: Callable[[str], None] = print,
) -> None:
    if run is None:
        run = lambda cmd: _run_tolerant(cmd, print_fn)  # noqa: E731

    print_fn("my-bt setup --interactive: walking through remaining steps.")
    print_fn("Already-done steps are shown and skipped without asking.\n")

    # 1. secrets
    print_fn("-- 1. Secrets --")
    for name, path_str in cli_checks.secret_file_map(raw).items():
        if not path_str:
            print_fn(f"[skip] {name}: not configured in settings.toml")
            continue
        p = Path(path_str)
        if p.exists() and p.read_text(encoding="utf-8", errors="replace").strip():
            print_fn(f"[ok] {name} already present ({path_str})")
            continue
        print_fn(f"\n{name} ({path_str}) is missing.")
        value = None
        if name == "admin_password_hash":
            if prompt("Set the admin password now?"):
                pw1 = read_secret("Admin password")
                pw2 = read_secret("Confirm")
                if pw1 and pw1 == pw2:
                    value = app_security.hash_admin_password(pw1)
                else:
                    print_fn("passwords empty or didn't match -- skipped")
        elif name == "erasure_pepper":
            if prompt("Generate a random erasure_pepper now?"):
                value = _secrets.token_hex(32)
        else:
            if prompt(f"Enter {name} now?"):
                value = read_secret(name).strip() or None
        if value is None:
            print_fn(f"[skip] {name}")
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(value + "\n", encoding="utf-8")
            p.chmod(0o600)
            if is_root():
                import grp
                import pwd
                try:
                    os.chown(p, pwd.getpwnam("my-booking").pw_uid, grp.getgrnam("my-booking").gr_gid)
                except KeyError:
                    pass
            print_fn(f"[ok] wrote {path_str}")
        except OSError as exc:
            print_fn(f"[fail] could not write {path_str}: {exc}")

    # 2. rpmnew merges
    print_fn("\n-- 2. Pending .rpmnew merges --")
    for path_str in (settings_path, str(tmpl_path(home))):
        rpmnew = Path(path_str + ".rpmnew")
        if not rpmnew.exists():
            print_fn(f"[ok] {Path(path_str).name}.rpmnew: none pending")
            continue
        print_fn(f"{rpmnew} exists: a newer packaged version is waiting to be merged.")
        if prompt(f"Open vimdiff {path_str} {rpmnew} now?"):
            run(["vimdiff", path_str, str(rpmnew)])
            if prompt(f"Merged? Remove {rpmnew} now?"):
                rpmnew.unlink()

    # 3. settings.toml review
    print_fn(f"\n-- 3. {settings_path} current values (review, edit by hand if needed) --")
    print_fn(f"   courses: {_course_summary(raw)}")
    print_fn(f"   {_privacy_summary(raw)}")

    # 4. nginx -- checked live via `nginx -T` (the fully merged config, not
    # a guessed single vhost file); never edited automatically -- rewriting
    # a stranger's hand-maintained nginx config would be worse than asking.
    print_fn("\n-- 4. nginx --")
    nginx_checks = cli_checks.check_nginx_locations()
    missing_locations = [
        label for label, level, _ in nginx_checks
        if level != "ok" and label.startswith("nginx location ")
    ]
    for label, level, detail in nginx_checks:
        print_fn(f"[{level}] {label}: {detail}")
    if missing_locations:
        print_fn("Add the missing location block(s) above from")
        print_fn("  /usr/share/my-booking-tool/my-booking.conf.example")
        print_fn("to your existing vhost (not automated -- this would mean")
        print_fn("guessing at and editing your hand-maintained nginx config).")
    if prompt("Run `nginx -t && systemctl reload nginx` now?"):
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])

    # 5. group membership
    print_fn("\n-- 5. my-booking group membership --")
    label, level, detail = cli_checks.check_group_membership()[0]
    if level == "ok":
        print_fn(f"[ok] {label}")
    else:
        print_fn(detail)
        if is_root() and prompt("Run that usermod now?"):
            target = os.environ.get("SUDO_USER") or getpass.getuser()
            run(["usermod", "-aG", "my-booking", target])
        elif not is_root():
            print_fn("(needs root -- re-run `sudo my-bt setup -i` to have this done for you)")

    # 6. systemd
    print_fn("\n-- 6. Service + retention timer --")
    for unit, level, detail in cli_checks.check_systemd():
        if level == "ok":
            print_fn(f"[ok] {unit}: {detail}")
        else:
            print_fn(f"{unit}: {detail}")
            if is_root() and prompt(f"Enable+start {unit} now?"):
                run(["systemctl", "enable", "--now", unit])
            elif not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")

    # 7. SELinux
    print_fn("\n-- 7. SELinux --")
    for label, level, detail in cli_checks.check_selinux():
        if level == "ok":
            print_fn(f"[ok] {label}: {detail}")
        else:
            print_fn(f"{label}: {detail}")
            if "httpd_can_network_connect" in label and is_root() and prompt("Run setsebool now?"):
                run(["setsebool", "-P", "httpd_can_network_connect", "on"])
            elif not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")

    # 8. static site
    print_fn("\n-- 8. Static site --")
    static_site_dir = raw.get("site", {}).get("static_site_dir")
    if not static_site_dir:
        print_fn("[site].static_site_dir not configured -- skipping live-page checks.")
        print_fn("Copy site/*.html to your live host and swap each course's booking")
        print_fn("link to /book/<shortname> (separate checkout, manual on purpose).")
    else:
        t = tmpl_path(home)
        out_path = Path(static_site_dir) / site_render.OUTPUT_NAME
        for label, level, detail in cli_checks.check_static_site_drift(raw, t):
            print_fn(f"[{level}] {label}: {detail}")
        # Compliance (leftover REPLACE-ME/${...} placeholders) is
        # informational only here -- it's about YOUR wording, which this
        # tool never edits for you, so there's nothing to prompt/act on
        # beyond surfacing it.
        for label, level, detail in cli_checks.check_static_site_compliance(raw):
            if level != "ok":
                print_fn(f"[{level}] {label}: {detail}")
        if prompt(f"(Re)generate {out_path} from current settings.toml now?"):
            privacy = raw.get("privacy", {})
            try:
                site_render.write_privacy_html(
                    t, privacy.get("retention_months", 24),
                    privacy.get("canceled_retention_months", 6), out_path,
                )
                print_fn(f"[ok] wrote {out_path}")
            except OSError as exc:
                print_fn(f"[fail] could not write {out_path}: {exc}")

        # index.html/impressum.html/terms.html: hand-authored, so never
        # auto-copied silently -- but actively OFFER to copy each one your
        # checkout has a real (or .example) source for, instead of just
        # reporting "differs" and leaving you to find/run the cp yourself.
        # Staying silent here was the actual bug (see the maintainer's local notes).
        for label, level, detail in cli_checks.check_static_pages_deployed(raw, home):
            print_fn(f"[{level}] {label}: {detail}")
        for name in cli_checks._STATIC_PAGES_TO_DEPLOY:
            source = cli_checks._resolve_static_source(home, name)
            if source is None:
                continue  # nothing in the checkout to offer copying from
            deployed = Path(static_site_dir) / name
            if prompt(f"Copy {source} to {deployed} now?"):
                try:
                    deployed.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                    print_fn(f"[ok] wrote {deployed}")
                except OSError as exc:
                    print_fn(f"[fail] could not write {deployed}: {exc}")

        # Reachability from nginx's actual root -- a per-file symlink, never
        # a static_site_dir rewrite (see check_static_pages_reachable()'s
        # own comment: some setups keep static_site_dir as a separate
        # git-tracked staging dir on purpose, symlinking in only what's
        # meant to be public).
        for label, level, detail in cli_checks.check_static_pages_reachable(raw):
            if level == "ok":
                print_fn(f"[ok] {label}: {detail}")
                continue
            print_fn(f"{label}: {detail}")
            name = label.split(": ", 1)[1]
            nginx_root = cli_checks._nginx_root_for_host(raw)
            if nginx_root is None:
                continue
            link_path = Path(nginx_root) / name
            target = Path(static_site_dir) / name
            if not is_root():
                print_fn("(needs root -- re-run `sudo my-bt setup -i`)")
            elif prompt(f"Symlink {link_path} -> {target} now?"):
                try:
                    link_path.symlink_to(target)
                    print_fn(f"[ok] symlinked {link_path} -> {target}")
                except OSError as exc:
                    print_fn(f"[fail] could not symlink {link_path}: {exc}")

    print_fn("\nDone. Re-run `my-bt status` any time to re-check everything.")
