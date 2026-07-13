"""Renders generated static-site pages from their `.tmpl` source plus live
settings.toml values. Stdlib-only (`string.Template`), consistent with the
rest of this project.

Two generated pages, sharing this same implementation pattern so they
can't drift apart from each other's own two use sites:

- **privacy.html**, from privacy.html.tmpl + the live `[privacy]`
  retention values. Mandatory -- every install needs real legal text here.
- **index_embedded.html** (2026-07-16), from index_embedded.html.tmpl +
  whatever upcoming `[[course.date_override]]` entries settings.toml
  currently has. Optional -- a no-JavaScript variant of the homepage,
  meant specifically for embedding this site via `<iframe>` on another
  site (see site/index_embedded.html.tmpl.example's own docstring for the
  full reasoning); most deployments don't need this at all, so its own
  real .tmpl simply not existing yet is never itself a problem to report
  (see app/cli_checks.py::check_index_embedded_drift).

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

Every generated page gets its own MANAGED marker prepended: a clear HTML
comment saying the file is machine-managed, so nobody mistakes it for
hand-authored content and edits it directly (those edits would just be
silently overwritten on the next render).
"""
from __future__ import annotations

import string
from pathlib import Path
from typing import Iterable

from .atomic_io import atomic_write_text

MANAGED_MARKER = (
    "<!-- MANAGED BY my-bt -- generated from privacy.html.tmpl + settings.toml.\n"
    "     Do NOT hand-edit this file: the next `my-bt setup -i` or\n"
    "     scripts/render-site.py run will silently overwrite it. Edit\n"
    "     privacy.html.tmpl instead (see README.md \"Static-site pages\"). -->\n"
)

TEMPLATE_NAME = "privacy.html.tmpl"
OUTPUT_NAME = "privacy.html"

# index_embedded.html (2026-07-16) -- see this module's own docstring above
# and site/index_embedded.html.tmpl.example for the full reasoning. Same
# real-.tmpl-or-.example resolution, same generated/never-hand-edited
# relationship to its own output, as privacy.html.tmpl -> privacy.html
# above -- just optional, and rendering different substitution data
# (upcoming date_override entries, not retention numbers).
EMBEDDED_MANAGED_MARKER = (
    "<!-- MANAGED BY my-bt -- generated from index_embedded.html.tmpl + settings.toml.\n"
    "     Do NOT hand-edit this file: the next `my-bt setup -i` or\n"
    "     scripts/render-site.py run will silently overwrite it. Edit\n"
    "     index_embedded.html.tmpl instead (see README.md \"Static-site pages\"). -->\n"
)

EMBEDDED_TEMPLATE_NAME = "index_embedded.html.tmpl"
EMBEDDED_OUTPUT_NAME = "index_embedded.html"


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


def _schedule_exceptions_html(items: Iterable[dict]) -> str:
    """One `.attention` `<div>` per upcoming date_override item (same shape
    app/config.py::upcoming_date_overrides returns), rendered exactly like
    site/index.html.example's own small JS snippet already builds this same
    markup client-side from GET /schedule-exceptions -- see that file's own
    top-of-file comment. `course_title`/`message` are operator-authored
    (settings.toml, same trust boundary as course `description` elsewhere
    in this app) and deliberately NOT escaped here either, for the same
    reason. "" (no `<div>` at all) when there's nothing upcoming -- same as
    the JS version leaving `#schedule-exceptions` empty."""
    return "".join(
        '<div class="attention"><b>⚠ ATTENTION:</b> ' + it["course_title"] +
        " on " + it["date"] + " starts at " + it["time_label"] + " instead" +
        (f' -- {it["message"]}' if it["message"] else "") +
        f' -- <a href="/book/{it["course_shortname"]}" target="_blank" rel="noopener noreferrer">details</a></div>'
        for it in items
    )


def render_index_embedded_html(
    template_path: Path | str,
    courses: Iterable,
    today: str,
) -> str:
    """The index_embedded.html twin of render_privacy_html above -- takes
    plain `Course` objects (see app.config.courses_from_raw, for a caller
    that only has the *raw* parsed TOML) and `today` ("YYYY-MM-DD") rather
    than a full app.config.Settings, for the exact same "don't require
    secrets just to render/check this" reason render_privacy_html's own
    docstring already explains."""
    from .config import upcoming_date_overrides

    text = Path(template_path).read_text(encoding="utf-8")
    items = upcoming_date_overrides(courses, today)
    rendered = string.Template(text).substitute(
        schedule_exceptions_html=_schedule_exceptions_html(items),
    )
    return EMBEDDED_MANAGED_MARKER + rendered


def write_index_embedded_html(
    template_path: Path | str,
    courses: Iterable,
    today: str,
    out_path: Path | str,
) -> None:
    # 2026-07-16: atomic_write_text, not a bare write_text() -- same
    # reasoning as write_privacy_html above: this is a live, publicly-served
    # page at both build time (this checkout's own site/) and run time
    # ([site].static_site_dir). See app/atomic_io.py.
    atomic_write_text(out_path, render_index_embedded_html(template_path, courses, today))
