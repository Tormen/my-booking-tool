"""Optional per-deployment OVERLAY directory for an operator's real files.

A deployment's real, personal files -- settings.toml and the real
site/*.html pages -- must never be published, and the operator's own
tooling carries between machines only what is named `*.local`. Keeping
each real file at its ordinary path and naming it individually in
.gitignore satisfies the first requirement but not the second: such a
file matches no `*.local` pattern, so nothing backs it up, and it is
silently left behind whenever the checkout is copied or moved. That is
not hypothetical -- a whole set of them was lost exactly that way.

So the real files may instead live together in ONE directory named
`*.local` at the repo root, mirroring the repo's own layout:

    my-booking.local/settings.toml
    my-booking.local/site/index.html
    my-booking.local/site/nginx-locations.conf

The directory name is matched by GLOB, never hardcoded: settings.toml
cannot be located through a setting inside settings.toml, so the lookup
cannot be configurable -- and baking one operator's chosen name into a
published template would be worse. Any single `*.local/` directory works;
two are an error, since the lookup would be ambiguous.

Purely a SOURCE-side convention, used when building a package or working
in a checkout. An installed system has no such directory: `my-bt` there
resolves against /opt/my-booking and /etc/my-booking, reading the real
files the package baked in at build time. `find()` simply returns None,
and every caller falls through to the behaviour it always had.
"""

from __future__ import annotations

from pathlib import Path


class LocalOverlayError(Exception):
    """More than one `*.local/` directory -- which one is meant is
    ambiguous, so refuse rather than pick one silently."""


def find(home: str | Path) -> Path | None:
    """The single `*.local/` directory in `home`, or None if there is
    none. Raises LocalOverlayError if there is more than one.

    Only visible directories qualify. `Path.glob` -- unlike the `glob`
    module -- DOES match names beginning with a dot, so hidden entries are
    skipped explicitly here; an unrelated `.update-LINKS.local` must never
    be mistaken for the overlay."""
    matches = sorted(
        p for p in Path(home).glob("*.local")
        if p.is_dir() and not p.name.startswith(".")
    )
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise LocalOverlayError(
            f"more than one *.local/ overlay directory in {home} ({names}) -- "
            "which one holds the real files is ambiguous; keep exactly one"
        )
    return matches[0] if matches else None


def source(home: str | Path, rel: str) -> Path | None:
    """The overlay's copy of `rel` (a repo-relative path such as
    "settings.toml" or "site/index.html") if the overlay exists AND
    actually holds that file -- else None, meaning "fall through to the
    ordinary lookup"."""
    overlay = find(home)
    if overlay is None:
        return None
    candidate = overlay / rel
    return candidate if candidate.is_file() else None


def output(home: str | Path, rel: str) -> Path:
    """Where a GENERATED real file (site/privacy.html,
    site/index_embedded.html) should be written: into the overlay when
    there is one -- generated files are as personal as their sources and
    belong beside them -- else at its ordinary in-repo path, exactly as
    before. Parent directories are created."""
    overlay = find(home)
    path = (overlay / rel) if overlay is not None else (Path(home) / rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
