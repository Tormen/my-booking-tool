"""Tiny HTML helpers -- no Jinja/templating engine dependency. Every
user-supplied value must go through esc() before landing in HTML."""
from __future__ import annotations

import html


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str, banner: str = "") -> str:
    """`banner` (2026-07-06, see app/webapp.py's _session_banner_html) is
    OPTIONAL, small, session-aware markup rendered above the page's own
    heading -- e.g. "Logged in as x@example.org - Logout" on /book and
    /courses when reached with an active guest session. Blank by default
    for every other page, unchanged from before this existed."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{font-family:sans-serif;max-width:640px;margin:2em auto;padding:0 1em;color:#222}}
.session-banner{{display:flex;justify-content:space-between;align-items:center;gap:1em;
  background:#f4f7f4;border:1px solid #ddd;border-radius:8px;padding:.5em 1em;
  margin-bottom:1em;font-size:.9em}}
.session-banner form{{display:inline}}
a{{color:#196B24}} .err{{color:#b00020}} .note{{color:#555;font-size:.9em}} .card{{border:1px solid #ddd;border-radius:8px;padding:1em;margin:1em 0}}
input,button,textarea{{font-size:1em;padding:.4em;margin:.2em 0}} button{{cursor:pointer}}
label{{display:block;margin-top:.6em}}
.subtitle{{color:#444;margin:-.4em 0 1em;font-size:1.2em;font-weight:500}}
.req{{color:#b00020}}
.hint{{color:#555;font-size:.85em;margin:.1em 0 0}}
.big-input{{font-size:1.25em;width:100%;box-sizing:border-box;padding:.35em .5em}}
.dates{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.5em;margin:.4em 0}}
.date-btn{{position:relative;display:block}}
.date-btn input{{position:absolute;opacity:0;width:1px;height:1px}}
.date-btn span{{display:block}}
.date-btn > span{{padding:.5em .8em;border:1px solid #ccc;border-radius:6px;cursor:pointer;text-align:center;line-height:1.3}}
.date-btn .d-date{{font-size:.95em}}
.date-btn .d-spots{{font-size:.85em;color:#666;margin-top:.1em}}
.date-btn input:checked + span{{background:#196B24;color:#fff;border-color:#196B24}}
.date-btn input:checked + span .d-spots{{color:#dff0e2}}
.date-btn input:focus-visible + span{{outline:2px solid #196B24;outline-offset:1px}}
.selected-box{{background:#f4f7f4;border:1px solid #ddd;border-radius:8px;padding:.7em 1em;margin:.8em 0}}
.description{{background:#fdf8ef;border:1px solid #eee0c0;border-radius:8px;padding:1em 1.2em;margin:.8em 0}}
.description ul,.description ol{{margin:.4em 0;padding-left:1.4em}}
.description p:first-child{{margin-top:0}} .description p:last-child{{margin-bottom:0}}
.submit-row{{margin-top:1.4em}}
button:disabled{{opacity:.5;cursor:not-allowed}}
.link-button{{background:none;border:none;padding:0;margin:0;color:#196B24;text-decoration:underline;font:inherit;cursor:pointer}}
.link-button:disabled{{color:#888;text-decoration:none;opacity:1}}
.table-tools{{margin-bottom:.6em}}
table{{border-collapse:collapse}}
th{{user-select:none}}
.sort-indicator{{font-size:.8em}}
.hash-cell{{word-break:break-all;font-family:monospace;font-size:.85em}}
.course-card{{border:1px solid #ddd;border-radius:8px;padding:1em 1.2em;margin:1em 0}}
.course-card h2{{margin:0 0 .2em;font-size:1.15em}}
</style></head><body>
{banner}
<h1>{esc(title)}</h1>
{body}
</body></html>"""
