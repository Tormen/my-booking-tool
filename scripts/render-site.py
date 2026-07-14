#!/usr/bin/env python3
"""Regenerate generated static-site pages from their `.tmpl` source plus
settings.toml -- currently just site/privacy.html, whose "how long it's
kept" paragraph (EN + DE) must always match the real, enforced
[privacy].retention_months / canceled_retention_months values. Before this
existed, those two numbers were hand-typed directly into the HTML, so
changing settings.toml alone didn't change what the public page said --
exactly the kind of silent drift a legal/GDPR-facing page can't afford.

This is the *build-time* half of that fix: it renders into this
checkout's own site/privacy.html (the reference copy shipped as %doc in
the RPM), using this checkout's settings.toml. The actual rendering logic
lives in app/site_render.py, shared with the *run-time* half -- `my-bt
setup --interactive`, which renders straight into [site].static_site_dir
(the live, web-served copy) on an already-installed server, using the
live settings.toml, so a plain config change doesn't need a rebuild+
reinstall to reach the public page. See app/site_render.py's docstring.

Stdlib-only (tomllib + app.site_render's string.Template use), consistent
with the rest of this project -- no template-engine dependency.

Run this:
  - after changing settings.toml's [privacy] retention numbers, then
    re-copy site/*.html to your live static-site host as usual (README.md
    "Static-site pages") -- or just use `my-bt setup -i` on the server,
    which does both steps for you against the live config.
  - automatically, via scripts/build-rpm.sh, before every RPM build -- so
    a fresh package always ships site/privacy.html consistent with the
    settings.toml it was built from.

Only edit the `.tmpl` source for wording changes -- the generated
site/privacy.html gets overwritten every run.

**Generic-template fallback:** if this checkout doesn't have a real
settings.toml / site/privacy.html.tmpl (e.g. a fresh clone of the public
template repo that only has the tracked settings.toml.example /
site/privacy.html.tmpl.example), this falls back to those .example files
instead of failing -- see resolve_real_or_example() below. Your own real
files, if present, are always preferred and are never read-modified.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (dev/test only, not the target server)
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import site_render  # noqa: E402 - after sys.path setup, deliberately


def resolve_real_or_example(real_path: Path) -> Path:
    """Prefer `real_path` if it exists (your real, customized file --
    never touched); otherwise fall back to `<real_path>.example` (the
    tracked, generic placeholder). Raises FileNotFoundError if neither
    exists, so a genuinely missing file still fails loudly."""
    if real_path.exists():
        return real_path
    example_path = real_path.with_name(real_path.name + ".example")
    if example_path.exists():
        return example_path
    raise FileNotFoundError(f"neither {real_path} nor {example_path} exists")


DEFAULT_SETTINGS = REPO_ROOT / "settings.toml"

# (template source, generated output) pairs, both relative to REPO_ROOT.
# Add a pair here if another static page ever needs to reflect a
# settings.toml value too. The output path is always the real filename --
# even when built from a .example template, the *rendered result* is a
# normal, real generated file, not itself an example.
TEMPLATES = [
    ("site/privacy.html.tmpl", "site/privacy.html"),
]

# index_embedded.html -- rendered separately from the loop above, not
# folded into TEMPLATES: it's DERIVED from site/index.html itself (real or
# .example, same resolve_real_or_example() fallback as everything else
# here), not rendered from a `.tmpl` + plain settings.toml numbers -- see
# app.site_render.derive_index_embedded_html's own docstring for the three
# transformations applied (strip <script> blocks, retarget known links,
# splice in the schedule-exceptions banner).
EMBEDDED_SOURCE_REL = "site/index.html"
EMBEDDED_OUTPUT_REL = "site/index_embedded.html"


def render(settings_path: Path | None = None) -> tuple[list[str], dict]:
    settings_path = resolve_real_or_example(settings_path or DEFAULT_SETTINGS)
    with settings_path.open("rb") as f:
        raw = tomllib.load(f)
    privacy = raw.get("privacy", {})
    retention_months = privacy.get("retention_months", 24)
    canceled_retention_months = privacy.get("canceled_retention_months", 6)

    written = []
    for tmpl_rel, out_rel in TEMPLATES:
        tmpl_path = resolve_real_or_example(REPO_ROOT / tmpl_rel)
        site_render.write_privacy_html(
            tmpl_path, retention_months, canceled_retention_months, REPO_ROOT / out_rel
        )
        written.append(out_rel)

    from app import config as app_config

    embedded_source_path = resolve_real_or_example(REPO_ROOT / EMBEDDED_SOURCE_REL)
    courses = app_config.courses_from_raw(raw)
    today = app_config.today_in_raw_timezone(raw)
    base_url = raw.get("site", {}).get("base_url", "")
    new_tab_links = bool(raw.get("site", {}).get("index_embedded_new_tab_links", True))
    custom_attention_message = raw.get("site", {}).get("custom_attention_message", "")
    try:
        site_render.write_derived_index_embedded_html(
            embedded_source_path.read_text(encoding="utf-8"), courses, today, base_url, new_tab_links,
            REPO_ROOT / EMBEDDED_OUTPUT_REL, custom_attention_message,
        )
        written.append(EMBEDDED_OUTPUT_REL)
    except site_render.IndexEmbeddedDerivationError as exc:
        # Best-effort, not fatal to the build: a customized real
        # site/index.html that's missing something this derivation depends
        # on (see that exception's own docstring) shouldn't break
        # scripts/build-rpm.sh for a site that never touched anything
        # related to the <iframe>-embed feature -- print and move on,
        # leaving whatever site/index_embedded.html (if any) already exists
        # from a previous run untouched.
        print(f"warning: could not derive {EMBEDDED_OUTPUT_REL} from {embedded_source_path}: {exc}",
              file=sys.stderr)

    values = {"retention_months": retention_months, "canceled_retention_months": canceled_retention_months}
    return written, values


def main() -> int:
    written, values = render()
    for out_rel in written:
        if out_rel == "site/privacy.html":
            print(
                f"wrote {out_rel} (retention_months={values['retention_months']}, "
                f"canceled_retention_months={values['canceled_retention_months']})"
            )
        else:
            print(f"wrote {out_rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
