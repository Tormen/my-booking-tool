"""Renders generated static-site pages -- currently just `privacy.html` --
from their `.tmpl` source plus the live `[privacy]` retention values in
settings.toml. Stdlib-only (`string.Template`), consistent with the rest
of this project.

Used two ways, sharing this one implementation so they can't drift apart:

- **Build time**: `scripts/render-site.py` renders into this checkout's
  own `site/privacy.html` (the `%doc` reference copy shipped in the RPM),
  using this checkout's `settings.toml`. Run by `scripts/build-rpm.sh`
  before every package build.
- **Run time**: `app/cli_setup.py` (via `my-bt setup --interactive`)
  renders directly into `[site].static_site_dir` -- the actual live,
  web-served copy (e.g. `/var/www/example.org`) -- using the live
  settings.toml. This is what lets a plain settings.toml edit reach the
  public page without a package rebuild/reinstall.

Every generated page gets `MANAGED_MARKER` prepended: a clear HTML comment
saying the file is machine-managed, so nobody mistakes it for hand-authored
content and edits it directly (those edits would just be silently
overwritten on the next render).
"""
from __future__ import annotations

import string
from pathlib import Path

from .atomic_io import atomic_write_text

MANAGED_MARKER = (
    "<!-- MANAGED BY my-bt -- generated from privacy.html.tmpl + settings.toml.\n"
    "     Do NOT hand-edit this file: the next `my-bt setup -i` or\n"
    "     scripts/render-site.py run will silently overwrite it. Edit\n"
    "     privacy.html.tmpl instead (see README.md \"Static-site pages\"). -->\n"
)

TEMPLATE_NAME = "privacy.html.tmpl"
OUTPUT_NAME = "privacy.html"


def render_privacy_html(
    template_path: Path | str,
    retention_months: int,
    canceled_retention_months: int,
) -> str:
    """Returns the fully rendered page text (marker + substituted HTML).
    Takes the two plain numbers rather than a full app.config.Settings so
    callers that only have the *raw* parsed TOML (e.g. `my-bt status`/
    `setup`, which deliberately avoid requiring secrets to be present just
    to check things) don't need a fully loaded Settings object either."""
    text = Path(template_path).read_text(encoding="utf-8")
    rendered = string.Template(text).substitute(
        retention_months=retention_months,
        canceled_retention_months=canceled_retention_months,
    )
    return MANAGED_MARKER + rendered


def write_privacy_html(
    template_path: Path | str,
    retention_months: int,
    canceled_retention_months: int,
    out_path: Path | str,
) -> None:
    # 2026-07-15: atomic_write_text, not a bare write_text() -- this is
    # the live, publicly-served privacy.html at both build time (this
    # checkout's own site/) and run time ([site].static_site_dir). See
    # app/atomic_io.py.
    atomic_write_text(
        out_path, render_privacy_html(template_path, retention_months, canceled_retention_months),
    )
