"""`my-bt admin backup` -- one .tar.gz holding everything that is not in
the RPM.

The package can be rebuilt from git at any time; what cannot is the
state this deployment accumulated on its own. That is what this
collects, and the test of what belongs in here is simple: if the VPS
died tonight, would a fresh install plus this file bring the site back
as it was? Registrations, both halves of the configuration, the secrets
those halves point at, the static pages, and the nginx vhost that
publishes them -- yes. The conflict-feed cache and the log -- no: the
first refetches itself within minutes, the second is history, not state.

SECRETS ARE INCLUDED, and that decides the file's permissions. It holds
the CalDAV and SMTP passwords, the admin password hash and the erasure
pepper in plain text, so the archive is created 0600 and, when written
as root, owned by root. Anyone who can read it can act as this
deployment -- which is exactly true of the secrets directory it copies
from, so the archive does not widen that circle as long as it stays
where it was written. Moving it somewhere less protected does widen it;
that is the operator's call to make knowingly, which is why the command
says so every time rather than hiding the fact in a manual. `--no-secrets`
produces an archive that is safe to hand around and cannot restore a
working service on its own.
"""
from __future__ import annotations

import os
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import cli_checks, config, version


@dataclass
class BackupItem:
    """One thing that goes in, and why it was or was not found."""
    label: str
    source: Path
    arcname: str
    note: str = ""


@dataclass
class BackupResult:
    archive: Path
    included: list[BackupItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total_bytes: int = 0
    with_secrets: bool = True


def timestamp(now: datetime | None = None) -> str:
    """The stamp used in the archive name: 2026-09-01_0004, UTC.

    Same shape as every other stamp this project writes into a file
    (annotate_superseded_override, comment_out_superseded_courses), so
    an archive and the config note explaining it sort together."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d_%H%M")


def _tree(root: Path, arcroot: str, skip_names=()) -> list[BackupItem]:
    items: list[BackupItem] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if any(part in skip_names for part in rel.parts):
            continue
        items.append(BackupItem(f"{arcroot}/{rel}", path, f"{arcroot}/{rel}"))
    return items


def plan(
    settings_path: str | Path,
    data_dir: str | Path,
    *,
    with_secrets: bool = True,
    raw: dict | None = None,
) -> tuple[list[BackupItem], list[tuple[str, str]]]:
    """What would go in, and what would not (with the reason).

    Separate from writing it so the caller can print the plan, and so
    the tests can check the decisions without ever creating an archive."""
    settings_path = Path(settings_path)
    data_dir = Path(data_dir)
    if raw is None:
        raw = config.load_raw_toml(settings_path) or {}
    items: list[BackupItem] = []
    skipped: list[tuple[str, str]] = []

    def take(label: str, source: Path, arcname: str, why_not: str = "not present") -> None:
        if source.is_file():
            items.append(BackupItem(label, source, arcname))
        else:
            skipped.append((label, why_not))

    take("settings.toml", settings_path, "config/settings.toml")

    editable = config.web_editable_path(settings_path)
    take("settings.web-editable.toml", editable,
         f"config/web-editable/{editable.name}",
         "no console-owned file yet (nothing has been saved from /admin)")

    secrets = {name: Path(p) for name, p in cli_checks.secret_file_map(raw).items() if p}
    if not with_secrets:
        skipped.append(("secrets", f"{len(secrets)} file(s) left out (--no-secrets) -- "
                                   "this archive cannot restore a working service on its own"))
    else:
        for name, path in sorted(secrets.items()):
            take(f"secret: {name}", path, f"secrets/{path.name}")

    # Registrations, users, the archive of erased rows, the override
    # history: the data the site cannot be rebuilt without. The
    # conflict-feed cache is deliberately left out -- it refetches
    # itself, and a stale copy restored over a live one would be worse
    # than none. .git is the data dir's own hourly snapshot history:
    # large, and already a backup of the same CSVs.
    if data_dir.is_dir():
        found = _tree(data_dir, "data", skip_names=("conflict_cache", ".git"))
        csvs = [i for i in found if i.source.suffix == ".csv"]
        items.extend(csvs)
        if not csvs:
            skipped.append(("data", f"no CSV files in {data_dir} yet"))
    else:
        skipped.append(("data", f"{data_dir} does not exist"))

    # The vhost nginx actually serves this site from: it carries the CSP
    # hash list, the rate-limit zones and the proxy rules, none of which
    # the RPM installs (it ships a reference copy under /opt to diff
    # against, not the live file). Located by base_url's host, the same
    # name check_nginx_locations uses, rather than by parsing `nginx -T`
    # -- a backup must not need a running nginx to be taken.
    host = urlsplit((raw.get("site") or {}).get("base_url", "")).hostname
    if host:
        vhost = Path("/etc/nginx/conf.d") / f"{host}.conf"
        take(f"nginx vhost ({vhost.name})", vhost, f"nginx/{vhost.name}",
             f"{vhost} not found -- if the vhost lives elsewhere, add it by hand")
    else:
        skipped.append(("nginx vhost", "no [site].base_url to find the vhost by"))

    site_dir = (raw.get("site") or {}).get("static_site_dir")
    if site_dir and Path(site_dir).is_dir():
        for page in sorted(Path(site_dir).glob("*.html")):
            items.append(BackupItem(f"site/{page.name}", page, f"site/{page.name}"))
    else:
        skipped.append(("static site", "no [site].static_site_dir configured"
                        if not site_dir else f"{site_dir} does not exist"))

    return items, skipped


def _manifest(result: BackupResult, settings_path: Path, data_dir: Path) -> str:
    """Written into the archive as MANIFEST.txt.

    An archive that cannot say what it is, where it came from and how to
    put it back is a puzzle, not a backup -- and it will be opened on the
    worst day, by someone who has forgotten the details."""
    lines = [
        f"my-booking-tool backup -- {result.archive.name}",
        "",
        f"version   : {version.version_string(version._HOME)}",
        f"host      : {os.uname().nodename}",
        f"taken     : {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC",
        f"settings  : {settings_path}",
        f"data dir  : {data_dir}",
        f"secrets   : {'INCLUDED -- keep this file 0600' if result.with_secrets else 'NOT included (--no-secrets)'}",
        "",
        "CONTENTS",
    ]
    lines += [f"  {item.arcname}" for item in result.included]
    if result.skipped:
        lines += ["", "NOT INCLUDED"]
        lines += [f"  {label}: {why}" for label, why in result.skipped]
    lines += [
        "",
        "RESTORE",
        "  1. Install the RPM of the same version on a clean host.",
        "  2. Copy config/settings.toml to the settings path above, and",
        "     config/web-editable/ beside it.",
        "  3. Copy secrets/ into the secrets directory settings.toml names,",
        "     then: chown my-booking:my-booking, chmod 600 (each file).",
        "  4. Copy data/*.csv into the data dir, same owner, then",
        "     `my-bt admin health` before starting the service.",
        "  5. Copy site/*.html into [site].static_site_dir.",
        "  6. The conflict-feed cache is NOT here on purpose: it refetches",
        "     itself on the first booking page load.",
        "",
    ]
    return "\n".join(lines)


def create(
    settings_path: str | Path,
    data_dir: str | Path,
    *,
    dest_dir: str | Path = ".",
    with_secrets: bool = True,
    now: datetime | None = None,
) -> BackupResult:
    """Write the archive and return what went into it.

    Created 0600 BEFORE anything is written to it (os.open with the mode,
    not a chmod afterwards): a chmod leaves a window in which the secrets
    are already on disk world-readable, and on a shared host that window
    is all an attacker needs."""
    settings_path = Path(settings_path)
    data_dir = Path(data_dir)
    items, skipped = plan(settings_path, data_dir, with_secrets=with_secrets)
    archive = Path(dest_dir) / f"my-booking-backup-{timestamp(now)}.tar.gz"
    result = BackupResult(archive=archive, included=items, skipped=skipped,
                          with_secrets=with_secrets)

    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw_file:
        with tarfile.open(fileobj=raw_file, mode="w:gz") as tar:
            for item in items:
                tar.add(item.source, arcname=item.arcname, recursive=False)
                result.total_bytes += item.source.stat().st_size
            text = _manifest(result, settings_path, data_dir).encode("utf-8")
            info = tarfile.TarInfo("MANIFEST.txt")
            info.size = len(text)
            info.mtime = int((now or datetime.now(timezone.utc)).timestamp())
            info.mode = 0o600
            import io

            tar.addfile(info, io.BytesIO(text))
    return result
