"""Renders generated static-site pages from live settings.toml values (and,
for index_embedded.html, the live index.html itself). Stdlib-only
(`string.Template`, `re`), consistent with the rest of this project.

Two generated pages, each its own mechanism (they used to share one
`.tmpl`-substitution pattern -- see git history around 2026-07-16 -- but
index_embedded.html's own approach changed the same day once a simpler one
was worked out: deriving straight from the real index.html rather than
maintaining a second, parallel hand-authored template file):

- **privacy.html**, from privacy.html.tmpl + the live `[privacy]`
  retention values. Mandatory -- every install needs real legal text here.
  `render_privacy_html`/`write_privacy_html` below.
- **index_embedded.html** (2026-07-16), DERIVED directly from the live
  index.html's own HTML (stripping `<script>` blocks, retargeting known
  outbound links, splicing in the current schedule-exceptions banner) plus
  whatever upcoming `[[course.date_override]]` entries settings.toml
  currently has -- see `derive_index_embedded_html` below. Optional (see
  `[site].index_embedded_enabled` in app/config.py) -- a no-JavaScript
  variant of the homepage, meant specifically for embedding this site via
  `<iframe>` on another site; most deployments don't need this at all.
  Unlike privacy.html, there is no separate hand-maintained template file
  for this page at all -- index.html itself IS the source, so the two can
  never drift apart in wording the way a parallel template could.

Used two ways:

- **Build time**: `scripts/render-site.py` renders into this checkout's
  own `site/privacy.html` / `site/index_embedded.html` (the `%doc`
  reference copies shipped in the RPM), using this checkout's own
  `settings.toml` and (for index_embedded.html) this checkout's own
  `site/index.html`. Run by `scripts/build-rpm.sh` before every package
  build.
- **Run time**: `app/cli_setup.py` (via `my-bt setup --interactive`)
  renders privacy.html directly into `[site].static_site_dir` using the
  live settings.toml, and (if `[site].index_embedded_enabled` is true)
  derives index_embedded.html from the LIVE, currently-deployed
  index.html there -- not this checkout's own copy, since the live file is
  the authoritative source for whatever's actually being embedded right
  now. This is what lets a plain settings.toml/index.html edit reach the
  public page without a package rebuild/reinstall.

privacy.html gets a MANAGED marker prepended: a clear HTML comment saying
the file is machine-managed, so nobody mistakes it for hand-authored
content and edits it directly (those edits would just be silently
overwritten on the next render). index_embedded.html gets a similar
marker, pointing back at index.html as the real source to edit instead.
"""
from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Iterable

from .atomic_io import atomic_write_text
from .cancellation import join_attention_sections

MANAGED_MARKER = (
    "<!-- MANAGED BY my-bt -- generated from privacy.html.tmpl + settings.toml.\n"
    "     Do NOT hand-edit this file: the next `my-bt setup -i` or\n"
    "     scripts/render-site.py run will silently overwrite it. Edit\n"
    "     privacy.html.tmpl instead (see README.md \"Static-site pages\"). -->\n"
)

TEMPLATE_NAME = "privacy.html.tmpl"
OUTPUT_NAME = "privacy.html"

# index_embedded.html (2026-07-16) -- see this module's own docstring
# above. DERIVED from index.html itself, not a separate .tmpl -- so the
# marker below points editors back at index.html, not at a second file to
# maintain.
EMBEDDED_MANAGED_MARKER = (
    "<!-- MANAGED BY my-bt -- derived from index.html + settings.toml.\n"
    "     Do NOT hand-edit this file: the next `my-bt setup -i` or\n"
    "     scripts/render-site.py run will silently overwrite it. Edit\n"
    "     index.html instead (see README.md \"Static-site pages\"). -->\n"
)

EMBEDDED_OUTPUT_NAME = "index_embedded.html"


class IndexEmbeddedDerivationError(ValueError):
    """Raised by derive_index_embedded_html() when the source index.html is
    missing something the derivation depends on -- see that function's own
    docstring for the full list of checks. Deliberately its own exception
    type (not a bare ValueError) so callers (app/cli_checks.py,
    app/cli_setup.py) can catch exactly this and print a clear, specific
    message, rather than risk also swallowing an unrelated ValueError from
    somewhere else in the same try block."""


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


def _schedule_exceptions_html(
    items: Iterable[dict], new_tab_links: bool = True, custom_message: str = "",
) -> str:
    """ONE combined `.attention` `<div>` for every upcoming date_override
    item (same shape app/config.py::upcoming_date_overrides returns) plus
    the optional site-wide [site].custom_attention_message, rendered
    exactly like site/index.html's own small JS snippet already builds
    this same markup client-side from GET /schedule-exceptions -- see
    that file's own top-of-file comment. `course_title`/`message`/
    `custom_message` are operator-authored (settings.toml, same trust
    boundary as course `description` elsewhere in this app) and
    deliberately NOT escaped here either, for the same reason. ""
    (no `<div>` at all) when there's neither an upcoming item nor a
    custom message -- same as the JS version leaving `#schedule-
    exceptions` empty.

    2026-07-13: restructured from one `<div class="attention">` PER item
    to a single box wrapping a `<ul>` (several stacked boxes read as
    cluttered with more than one exception) -- each `<li>` leads with the
    WEEKDAY in bold, then the date, then the course name: this page lists
    exceptions across every course at once, so the weekday is the
    fastest way to tell which recurring session is affected (unlike the
    per-course banner on that course's own /book/<shortname> page,
    app/webapp.py::_course_date_overrides_html, where it's already
    obvious from context and stays a plain sentence, no list). The
    custom message, if set, is appended below the list (see
    join_attention_sections), separated by a `<hr>` only when there are
    also items.

    `new_tab_links` controls the embedded "details" link's own target/rel,
    same [site].index_embedded_new_tab_links setting the rest of
    derive_index_embedded_html() below applies to every other retargeted
    link on the page -- this banner is only ever rendered as part of that
    same derivation, so it must follow the same setting, not a hardcoded
    choice of its own."""
    target_attrs = ' target="_blank" rel="noopener noreferrer"' if new_tab_links else ' target="_top"'
    items_html = ""
    if items:
        items_html = "<ul>" + "".join(
            f'<li><b>{it["weekday"]}</b>, {it["date"]}: {it["course_title"]} starts at '
            f'{it["time_label"]} instead' +
            (f' -- {it["message"]}' if it["message"] else "") +
            f' -- <a href="/book/{it["course_shortname"]}"{target_attrs}>details</a></li>'
            for it in items
        ) + "</ul>"
    inner = join_attention_sections(items_html, custom_message)
    if not inner:
        return ""
    return f'<div class="attention"><b>⚠ ATTENTION:</b> {inner}</div>'


# --- index_embedded.html derivation (2026-07-16) -------------------------
#
# Derives a no-JavaScript variant straight from the real index.html's own
# markup, rather than maintaining a second, hand-authored template that
# could drift from index.html's actual wording over time. Three
# transformations, applied to a copy of index.html's text:
#
#   1. Every <script>...</script> block is stripped outright.
#   2. The <div id="schedule-exceptions">...</div> marker is replaced with
#      the CURRENT schedule-exceptions banner (see _schedule_exceptions_html
#      above), computed from live settings.toml date_override entries --
#      this page can't fetch that live via JavaScript (that's the whole
#      point of it), so it has to be baked in at derivation time instead.
#   3. Every <a href="..."> whose href matches one of THIS APP'S OWN known
#      routes (/my, /book/<shortname>, /terms.html, /privacy.html,
#      /impressum.html -- optionally prefixed with [site].base_url) gets
#      its target/rel rewritten per [site].index_embedded_new_tab_links.
#      Deliberately narrow: any OTHER link (a mailto:, an external site,
#      arbitrary custom markup) is left exactly as index.html has it --
#      this derivation only touches what it actually understands, rather
#      than guessing at links it has no business rewriting.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
# Real bug hit deriving from the actual production index.html (2026-07-13):
# its own top-of-file HTML comment mentions the word "<script>" in plain
# explanatory prose (documenting what the real script further down does).
# _SCRIPT_RE doesn't know it's inside a comment -- it saw that as a real
# opening tag and matched (non-greedily) all the way to the NEXT </script>
# it could find, which was the actual first real script's closing tag,
# silently swallowing every bit of markup in between (including every
# booking link) with it. Stripping HTML comments FIRST, before anything
# else, means a stray "<script>"/"<a href=...>" mentioned in developer prose
# can never be mistaken for real markup by any of the regexes below.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Same shape as _SCRIPT_RE above but with a capture group around the inner
# body only (excluding the <script ...>/</script> tags themselves) -- this
# is exactly the byte range a CSP 'sha256-...' hash must be computed over.
# Kept as its own regex (rather than deriving from _SCRIPT_RE) so a change to
# one can't accidentally desync the other.
_SCRIPT_BODY_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_SCHEDULE_EXCEPTIONS_DIV_RE = re.compile(
    r'<div\s+id="schedule-exceptions"[^>]*>.*?</div>', re.IGNORECASE | re.DOTALL
)
_ANCHOR_TAG_RE = re.compile(r'<a\s+[^>]*?href="([^"]*)"[^>]*>', re.IGNORECASE)
_TARGET_ATTR_RE = re.compile(r'\s+target="[^"]*"', re.IGNORECASE)
_REL_ATTR_RE = re.compile(r'\s+rel="[^"]*"', re.IGNORECASE)

# The routes this app itself serves. Their <a> tags are the ones whose
# presence is verified (see derive_index_embedded_html's fail-fast checks);
# retargeting itself applies to every link on the page, see
# _retarget_links below.
_KNOWN_STATIC_PATHS = ("/terms.html", "/privacy.html", "/impressum.html")

# Schemes that must NEVER be given target="_blank": opening a mail or phone
# link in a new tab leaves the visitor with a blank tab that never
# navigates, and a bare "#fragment" scrolls the page it is already on.
_NON_NAVIGATING_HREF_PREFIXES = ("mailto:", "tel:", "sms:", "javascript:", "#")


def extract_script_bodies(html_text: str) -> list[str]:
    """Returns the exact inner text of every <script>...</script> block in
    `html_text`, in document order -- NOT including the <script ...>/
    </script> tags themselves, i.e. exactly the bytes a CSP
    'sha256-...' hash must be computed over.

    Strips HTML comments FIRST, same as derive_index_embedded_html() above
    and for the exact same reason (see _HTML_COMMENT_RE's own comment): a
    stray literal "<script>" mentioned in developer-authored prose inside an
    HTML comment gets mistaken for a real opening tag by a naive regex,
    which then non-greedily matches all the way to the NEXT real closing
    tag, silently swallowing everything in between. This bit index.html
    itself twice in practice (once here, once ad hoc while hand-computing a
    hash after a script rewrite) before this shared, comment-safe helper
    existed.

    Used by app.cli_checks.expected_csp_hashes() to compute index.html's own
    script hashes automatically instead of by hand."""
    stripped = _HTML_COMMENT_RE.sub("", html_text)
    return _SCRIPT_BODY_RE.findall(stripped)


def _known_app_path(href: str, base_url: str) -> str | None:
    """"/my", "/book/", or one of _KNOWN_STATIC_PATHS if `href` (after
    stripping a leading `base_url`, if present) matches one of this app's
    own routes -- else None. `base_url` lets an absolute link
    (https://example.org/my) match exactly like a plain relative one
    (/my) does."""
    path = href
    if base_url and path.startswith(base_url):
        path = path[len(base_url):]
    if path == "/my":
        return "/my"
    if path.startswith("/book/"):
        return "/book/"
    if path in _KNOWN_STATIC_PATHS:
        return path
    return None


def _retargetable(href: str) -> bool:
    """Whether `href` is a link that navigates somewhere and can therefore
    sensibly be retargeted -- see _NON_NAVIGATING_HREF_PREFIXES."""
    stripped = href.strip()
    if not stripped:
        return False
    return not stripped.lower().startswith(_NON_NAVIGATING_HREF_PREFIXES)


def _retarget_links(html_text: str, base_url: str, new_tab_links: bool) -> tuple[str, int, int]:
    """Rewrites target/rel on EVERY navigating <a> tag, not only the ones
    pointing at this app's own routes. Anything left on its own target
    would load inside the embedding <iframe>, which for a link back to the
    embedding site itself means rendering that page nested inside its own
    frame. Links that do not navigate (mailto:, tel:, #fragment -- see
    _retargetable) are returned byte-identical.

    Returns (rewritten_html, my_link_count, book_link_count) -- the two
    counts are exactly what derive_index_embedded_html()'s own fail-fast
    checks below need, counted here (one regex pass) rather than
    re-scanning the text a second time."""
    counts = {"/my": 0, "/book/": 0}

    def repl(m: re.Match) -> str:
        tag, href = m.group(0), m.group(1)
        matched = _known_app_path(href, base_url)
        if matched in counts:
            counts[matched] += 1
        if matched is None and not _retargetable(href):
            return tag
        tag = _TARGET_ATTR_RE.sub("", tag)
        tag = _REL_ATTR_RE.sub("", tag)
        attrs = ' target="_blank" rel="noopener noreferrer"' if new_tab_links else ' target="_top"'
        return tag[:-1] + attrs + ">"

    rewritten = _ANCHOR_TAG_RE.sub(repl, html_text)
    return rewritten, counts["/my"], counts["/book/"]


def derive_index_embedded_html(
    index_html_text: str,
    courses: Iterable,
    today: str,
    base_url: str = "",
    new_tab_links: bool = True,
    custom_attention_message: str = "",
) -> str:
    """Derives index_embedded.html's full text from `index_html_text` (the
    real index.html's own markup -- see module comment above for the three
    transformations applied) plus `courses`/`today` (see
    app.config.courses_from_raw/today_in_raw_timezone, for a caller that
    only has the *raw* parsed TOML, same reasoning render_privacy_html's
    own docstring explains for privacy.html).

    Fails LOUDLY (raises IndexEmbeddedDerivationError) rather than silently
    producing a subtly-broken page, if index_html_text is missing anything
    this derivation depends on -- catches index.html having been
    restructured in a way this function doesn't understand, rather than
    quietly deploying a page with a missing banner or broken links:
    - no `<div id="schedule-exceptions">` marker -- nowhere to splice the
      ATTENTION banner.
    - neither of the two `<script>` blocks my-bt itself ships in
      index.html.example (identified by their own fetch() targets,
      '/my/session' and '/schedule-exceptions', found anywhere in the raw
      text) -- suggests index.html was restructured beyond what this
      derivation's other assumptions can be trusted against.
    - no link matching "/my" (the Login link) -- nothing to retarget, so
      Login would stay broken inside the iframe.
    - no link matching "/book/" (a course booking link) -- same problem for
      every booking link.
    """
    # Strip HTML comments FIRST, before any check or transformation below --
    # see _HTML_COMMENT_RE's own comment for the real incident this fixes
    # (a stray "<script>" mentioned in developer prose inside a comment
    # silently swallowing the entire page body once treated as real markup).
    index_html_text = _HTML_COMMENT_RE.sub("", index_html_text)

    if not _SCHEDULE_EXCEPTIONS_DIV_RE.search(index_html_text):
        raise IndexEmbeddedDerivationError(
            'index.html has no <div id="schedule-exceptions"> marker -- nowhere to splice '
            "the ATTENTION banner. Derivation aborted -- check index.html hasn't been "
            "restructured (see app/site_render.py::derive_index_embedded_html)."
        )
    if "/my/session" not in index_html_text and "/schedule-exceptions" not in index_html_text:
        raise IndexEmbeddedDerivationError(
            "index.html doesn't contain either of the two <script> blocks my-bt expects "
            "(fetch('/my/session', ...) or fetch('/schedule-exceptions', ...)) -- it may have "
            "been restructured in a way this derivation doesn't understand. Derivation aborted."
        )

    without_scripts = _SCRIPT_RE.sub("", index_html_text)
    rewritten, my_count, book_count = _retarget_links(without_scripts, base_url, new_tab_links)
    if my_count == 0:
        raise IndexEmbeddedDerivationError(
            'no link to "/my" found in index.html (the Login link) -- nothing to retarget, '
            "so Login would stay broken inside the iframe. Derivation aborted."
        )
    if book_count == 0:
        raise IndexEmbeddedDerivationError(
            'no link to "/book/<shortname>" found in index.html -- nothing to retarget, so '
            "every booking link would stay broken inside the iframe. Derivation aborted."
        )

    from .config import upcoming_date_overrides

    items = upcoming_date_overrides(courses, today)
    banner_html = (
        _schedule_exceptions_html(items, new_tab_links, custom_attention_message)
        or '<div id="schedule-exceptions"></div>'
    )
    # A callable `repl` (not a plain string) -- re.sub never interprets its
    # return value for backreferences (\1 etc.), so banner_html's own
    # content (which can contain operator-authored, unescaped text -- see
    # _schedule_exceptions_html's own docstring) is spliced in exactly as-is.
    rewritten = _SCHEDULE_EXCEPTIONS_DIV_RE.sub(lambda _m: banner_html, rewritten, count=1)

    return EMBEDDED_MANAGED_MARKER + rewritten


def write_derived_index_embedded_html(
    index_html_text: str,
    courses: Iterable,
    today: str,
    base_url: str,
    new_tab_links: bool,
    out_path: Path | str,
    custom_attention_message: str = "",
) -> None:
    """derive_index_embedded_html() + atomic_write_text -- used by
    scripts/render-site.py (build time, deriving from this checkout's own
    site/index.html). app/cli_setup.py (run time) calls
    derive_index_embedded_html() directly instead, since it needs the
    returned string itself to compare against what's already deployed
    before deciding whether to write anything."""
    atomic_write_text(
        out_path,
        derive_index_embedded_html(
            index_html_text, courses, today, base_url, new_tab_links, custom_attention_message,
        ),
    )
