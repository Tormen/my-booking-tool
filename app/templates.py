"""Tiny HTML helpers -- no Jinja/templating engine dependency. Every
user-supplied value must go through esc() before landing in HTML."""
from __future__ import annotations

import html


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{font-family:sans-serif;max-width:640px;margin:2em auto;padding:0 1em;color:#222}}
a{{color:#196B24}} .err{{color:#b00020}} .note{{color:#555;font-size:.9em}} .card{{border:1px solid #ddd;border-radius:8px;padding:1em;margin:1em 0}}
input,button,textarea{{font-size:1em;padding:.4em;margin:.2em 0}} button{{cursor:pointer}}
label{{display:block;margin-top:.6em}}
</style></head><body>
<h1>{esc(title)}</h1>
{body}
</body></html>"""
