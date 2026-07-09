"""Minimal, stdlib-only macro/variable templating for guest-facing emails
(2026-07-09, the operator: "Add support for MAKROS in the templates, you need
support for VARIABLES anyways. Then the cancel_email.html for instance
should DEFINE how the final email is assembled (e.g. which makros are
used where / in which order)." -- following up on
`settings.toml`'s `email_templates_folder`: "place all email templates
into settings.toml [directory] to easily change something there if
needed").

Design: a template file (e.g. `email_templates/cancel_email.txt`) is
plain text/HTML with `{{name}}`-style placeholders. "Variables" and
"macros" are the exact same mechanism -- both are just named string
values substituted into those placeholders. The distinction is only in
WHERE the value comes from: a "variable" is a simple fact (a name, a
date, a URL); a "macro" is a whole pre-rendered block (a greeting, an
intro sentence, a message box) built by one of app.cancellation's
existing helper functions (greeting_html, intro_html, message_html,
course_recap_html, ...). Both are computed in Python and handed to
render_template() as an ordinary keyword-argument context -- the
TEMPLATE FILE decides where each one appears and in what order, instead
of that order being hardcoded as Python string concatenation.

Deliberately NOT a general-purpose template language: no conditionals,
loops, or filters. Presence/absence of a block (e.g. "omit the message
box entirely when there's no message") is still decided in Python, by
setting that context value to "" -- exactly the same convention the old
inline f-string assembly already used, just moved one level out.
`string.Template` (stdlib) was considered and rejected: its `$name`/
`${name}` syntax reads awkwardly inside HTML/CSS (`$` collides with
nothing in particular, but `{{name}}` is the far more familiar
convention -- Jinja2, Handlebars, Mustache all use it -- for anyone who
later opens these files expecting to edit them by hand)."""
from __future__ import annotations

import re
from pathlib import Path

from .config import Settings

# .../app/email_templates.py -> .../  (repo root in a dev checkout; in an
# RPM install this resolves to /opt/my-booking, where packaging/
# my-booking-tool.spec installs this same email_templates/ directory
# alongside app/ -- see that file's own install/%files blocks). This is
# the fallback used whenever settings.email_templates_folder is unset, OR
# is set but doesn't contain the specific file being loaded (so someone
# customizing just ONE template doesn't need to also copy every other one
# they don't care about).
_BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def render_template(template_text: str, **context: str) -> str:
    """Replaces every `{{name}}` in `template_text` with `context[name]`,
    verbatim, in one pass -- a value itself containing literal `{{...}}`
    (e.g. a guest-typed message that happens to include that text) is
    NOT re-scanned for further substitution, so this can never be tricked
    into recursively expanding attacker-controlled input. Raises KeyError
    with every valid name listed if the template references one that
    wasn't provided -- a typo'd macro name in a hand-edited template file
    should fail loudly and immediately, not silently leave `{{typo}}`
    sitting in a real email."""
    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in context:
            raise KeyError(
                f"email template references {{{{{name}}}}}, which isn't one of the "
                f"available variables/macros: {sorted(context)}"
            )
        return context[name]
    return _PLACEHOLDER_RE.sub(_sub, template_text)


def load_email_template(settings: Settings, name: str) -> str:
    """Reads template file `name` (e.g. "cancel_email.txt") -- from
    `settings.email_templates_folder` if set AND that specific file
    exists there, else from this repo's own built-in copy (see
    _BUILTIN_TEMPLATES_DIR above). Raises FileNotFoundError, naming both
    places it looked, if neither has it -- there is no third, silent
    fallback to an inline Python string; the built-in copy in
    email_templates/ IS the shipped default, not a backup for it."""
    if settings.email_templates_folder:
        custom_path = Path(settings.email_templates_folder) / name
        if custom_path.is_file():
            return custom_path.read_text(encoding="utf-8")
    builtin_path = _BUILTIN_TEMPLATES_DIR / name
    if builtin_path.is_file():
        return builtin_path.read_text(encoding="utf-8")
    looked_in = [str(Path(settings.email_templates_folder) / name)] if settings.email_templates_folder else []
    looked_in.append(str(builtin_path))
    raise FileNotFoundError(f"email template {name!r} not found in: {', '.join(looked_in)}")
