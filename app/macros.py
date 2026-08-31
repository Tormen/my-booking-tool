"""Operator-defined text macros, and the rules that keep the three kinds
of them apart.

A macro is a named piece of text written once and used in many places --
a studio name, an address, a standing note. Which KIND a macro is can be
read off its own name, and that is the whole design:

    {{studio}}              USER    -- defined by the operator, in
                                       settings.web-editable.toml
    {{!retention_months}}   SYSTEM  -- from settings.toml, the file the
                                       admin console can never write
    {{$name}}, {{$details}} DYNAMIC -- supplied by the code for one send

A sigil means the system owns the name; the bare namespace belongs to the
operator, because it is the one they type constantly. That division is not
cosmetic: the code already owns 27 names across email_templates/ (site,
name, details, capacity, intro, recap, ...) and sharing one namespace
would mean a shipped template could never gain a macro again without
possibly colliding with one the operator already uses.

`!` and `$` both mark system-owned names but stay separate sigils because
they differ in AVAILABILITY: a system macro always resolves wherever it
is allowed, a dynamic one only where the code hands it over. `{{$recap}}`
in a template that is never given a recap is an error at send time.

Two CONTEXTS, because the same macro can land in either:

    RICH  -- the value renders as markup (a course description, an
             email's HTML part)
    PLAIN -- the value is reduced to its text (a title, a location, a
             calendar SUMMARY, an email subject -- destinations that
             have no HTML at all)

A macro is never refused for being in the "wrong" field: in a plain
context its markup is reduced to text. Refusing would need a rule the
reader has to learn, plus the machinery to police it.

Stdlib only (re, html.parser), like everything else here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = [
    "MacroError", "MAX_NAME_LENGTH", "USER", "SYSTEM", "DYNAMIC",
    "validate_name", "kind_of", "names_used", "expand", "sanitize",
    "SanitizeResult", "ALLOWED_TAGS", "ALLOWED_ATTRS",
]

USER = "user"
SYSTEM = "system"
DYNAMIC = "dynamic"

# A macro name is typed into other texts as {{name}}, so it stays short.
MAX_NAME_LENGTH = 20
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# The sigil is part of the REFERENCE, never of the stored name: a user
# macro is stored as "studio" and written {{studio}}.
_REF_RE = re.compile(r"\{\{([!$]?)([A-Za-z_][A-Za-z0-9_]*)\}\}")

_SIGIL_KIND = {"": USER, "!": SYSTEM, "$": DYNAMIC}
_KIND_SIGIL = {USER: "", SYSTEM: "!", DYNAMIC: "$"}


class MacroError(ValueError):
    """A macro name that cannot exist, or a reference that cannot be
    resolved. Raised rather than rendered: a half-expanded text reaching
    a guest is worse than a loud failure at save or send time."""


def validate_name(name: str) -> None:
    """Raises MacroError unless `name` is a usable macro name. The rules
    are the ones the console enforces while typing, restated here because
    a value can also arrive by hand-edit or by any HTTP client."""
    if not name:
        raise MacroError("a macro name cannot be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise MacroError(
            f"macro name {name!r} is longer than {MAX_NAME_LENGTH} characters"
        )
    if name[0] in "!$":
        raise MacroError(
            f"macro name {name!r} starts with a sigil: `!` and `$` mark names the "
            f"system owns, so they can never begin one of yours"
        )
    if not _NAME_RE.match(name):
        raise MacroError(
            f"macro name {name!r} must be letters, digits and underscores, "
            f"and cannot start with a digit"
        )


def kind_of(reference: str) -> str:
    """USER / SYSTEM / DYNAMIC for a whole reference like "{{!x}}"."""
    m = _REF_RE.fullmatch(reference)
    if not m:
        raise MacroError(f"{reference!r} is not a macro reference")
    return _SIGIL_KIND[m.group(1)]


def names_used(text: str, kind: str | None = None) -> list[str]:
    """Every macro name `text` refers to, in order of first appearance,
    optionally limited to one kind. Used by the rename path (which must
    find every use) and by the guard that refuses a system macro in a
    value the console can write."""
    seen: list[str] = []
    for m in _REF_RE.finditer(text):
        if kind is not None and _SIGIL_KIND[m.group(1)] != kind:
            continue
        if m.group(2) not in seen:
            seen.append(m.group(2))
    return seen


def expand(
    text: str,
    *,
    user: dict[str, str] | None = None,
    system: dict[str, str] | None = None,
    dynamic: dict[str, str] | None = None,
    rich: bool,
    to_text: "callable[[str], str] | None" = None,
) -> str:
    """Replaces every macro reference in `text` with its value, in ONE
    pass -- a value that itself contains `{{...}}` is not rescanned, so
    expansion can never be driven recursively by whatever a value holds.

    `rich=False` is a plain-text destination: each value is reduced to
    text through `to_text` (app.cancellation.html_to_text by default at
    the call sites) rather than being refused for containing markup.

    An unknown name raises MacroError naming what IS available. A macro
    that silently disappears is the failure mode worth preventing: it
    reaches a guest looking like the text was simply never written."""
    tables = {USER: user or {}, SYSTEM: system or {}, DYNAMIC: dynamic or {}}

    def _sub(m: re.Match[str]) -> str:
        kind = _SIGIL_KIND[m.group(1)]
        name = m.group(2)
        table = tables[kind]
        if name not in table:
            sigil = _KIND_SIGIL[kind]
            raise MacroError(
                f"{{{{{sigil}{name}}}}} is not a {kind} macro that exists here "
                f"-- available: {sorted(table) or 'none'}"
            )
        value = table[name]
        return value if rich or to_text is None else to_text(value)

    return _REF_RE.sub(_sub, text)


# -- sanitizing ------------------------------------------------------------
#
# An ALLOWLIST, not a list of things to strip: a blocklist is a promise
# that every dangerous tag was thought of, and nobody can make it.
ALLOWED_TAGS = frozenset(
    "a b i u em strong small code span div p br hr ul ol li "
    "h1 h2 h3 h4 blockquote".split()
)
ALLOWED_ATTRS = frozenset(("href", "title", "target", "rel"))
_VOID_TAGS = frozenset(("br", "hr"))
_SAFE_URL_RE = re.compile(r"(https?:|mailto:|tel:|/|#)", re.I)


@dataclass
class SanitizeResult:
    """The cleaned markup, plus what was taken out of it -- the console
    reports the second half, so a value is never silently stored as
    something other than what was typed."""
    html: str
    dropped: list[str] = field(default_factory=list)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.dropped: list[str] = []
        # A disallowed element is dropped WITH its content: <script>
        # alert(1)</script> must not leave "alert(1)" behind as text.
        self._skip_depth = 0
        self._skipping: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skipping:
            if tag == self._skipping:
                self._skip_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            self.dropped.append(f"<{tag}>")
            if tag not in _VOID_TAGS:
                self._skipping, self._skip_depth = tag, 1
            return
        kept: list[str] = []
        for name, value in attrs:
            low = name.lower()
            if low not in ALLOWED_ATTRS:
                self.dropped.append(f"{tag}[{low}]")
                continue
            if low == "href" and not _SAFE_URL_RE.match((value or "").strip()):
                # javascript: and data: never reach the page.
                self.dropped.append(f"{tag}[href]")
                continue
            kept.append(f' {low}="{_attr_escape(value or "")}"')
        if tag == "a" and any(k.strip().startswith('target="_blank"') for k in kept):
            kept = [k for k in kept if not k.strip().startswith("rel=")]
            kept.append(' rel="noopener noreferrer"')
        self.out.append(f"<{tag}{''.join(kept)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skipping:
            if tag == self._skipping:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skipping = None
            return
        if tag in ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.out.append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def handle_comment(self, data: str) -> None:  # never kept
        return


def _attr_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def sanitize(markup: str) -> SanitizeResult:
    """Reduces `markup` to the allowlist above.

    The booking page's CSP already blocks script execution (script-src is
    'self' plus per-script hashes, with no 'unsafe-inline'), so this is
    the second of two independent controls -- and it covers what a CSP
    does not: emails have no CSP at all, and phishing or defacement
    markup needs no script to work."""
    parser = _Sanitizer()
    parser.feed(markup)
    parser.close()
    seen: list[str] = []
    for item in parser.dropped:
        if item not in seen:
            seen.append(item)
    return SanitizeResult("".join(parser.out), seen)


# A closing tag whose "<" was lost while editing: "</li>" typed, or
# half-deleted, into "/li>". The sanitizer escapes it faithfully -- it is
# text, not markup, by then -- so nothing downstream can tell it was ever
# meant to be a tag. It reached the live booking page that way once
# (2026-08-31, trier-sat-yoga: "<li>{{no_slot}}/li&gt;").
# Matched in both spellings: as typed ("/li>") and as the sanitizer
# stores it once it has decided the text is data ("/li&gt;").
_ORPHAN_END_TAG_RE = re.compile(r"(?<!<)/([A-Za-z][A-Za-z0-9]*)(?:>|&gt;)")

# Tags HTML lets you leave unclosed: a new <li> ends the previous one,
# and </ul> ends the last. Warning about those would fire on markup that
# renders exactly as it reads, and a warning that cries wolf gets
# ignored -- including on the day it is right.
_IMPLICITLY_CLOSED = frozenset(("li", "p"))


class _Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problem = ""

    def _close_implicit(self, until: str = "") -> None:
        while self.stack and self.stack[-1] in _IMPLICITLY_CLOSED and self.stack[-1] != until:
            self.stack.pop()

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ALLOWED_TAGS and tag not in _VOID_TAGS:
            if tag in _IMPLICITLY_CLOSED and self.stack[-1:] == [tag]:
                self.stack.pop()
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS or tag in _VOID_TAGS or self.problem:
            return
        self._close_implicit(until=tag)
        if not self.stack:
            self.problem = f"</{tag}> closes a tag that was never opened"
        elif self.stack[-1] != tag:
            self.problem = f"</{tag}> closes out of order -- <{self.stack[-1]}> is still open"
            self.stack.pop()
        else:
            self.stack.pop()


def describe_markup_problem(markup: str) -> str:
    """A one-line reason this markup does not hold together, or "".

    Advisory, never a refusal: the text is the operator's, and saving it
    is their call. But the console knows, and staying silent is how a
    half-deleted tag ends up live for days."""
    orphan = _ORPHAN_END_TAG_RE.search(markup)
    if orphan and orphan.group(1).lower() in ALLOWED_TAGS:
        return f'"{orphan.group(0)}" looks like a closing tag with its "<" missing'
    parser = _Balance()
    parser.feed(markup)
    parser.close()
    if parser.problem:
        return parser.problem
    parser._close_implicit()
    if parser.stack:
        return f"<{parser.stack[-1]}> is never closed"
    return ""
