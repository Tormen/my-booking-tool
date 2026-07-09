"""Tiny HTML helpers -- no Jinja/templating engine dependency. Every
user-supplied value must go through esc() before landing in HTML."""
from __future__ import annotations

import html


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# 2026-07-11, the operator (screenshot of a Cancel submission sitting at 2.05s in
# devtools' Network tab): "(a) the buttons remain all clickable .. not sure
# what would happen if you would fire again or click cancel finally... (b)
# there is no indication that the button press was taken into consideration
# and that the system is working on it (spinning wheel or so)." A cancel/
# reinstate/booking POST is a plain (non-fetch) form submission followed by a
# full-page redirect -- there's a real, sometimes multi-second gap between
# the click and the new page replacing this one, during which the OLD page's
# DOM (every button on it, not just the one just clicked) stays perfectly
# interactive. Note this is UX-only, not a correctness fix: every mutating
# route already treats a repeat submission as a safe no-op server-side (see
# e.g. guest_cancel()'s "already guarded ... additionally closes a genuine
# concurrent double-submit" comment) -- this just stops the page from
# LOOKING like nothing happened, and stops a bored/impatient click from
# doing anything at all while a submission is already in flight.
# Deliberately global (page()-level, not opt-in per page like
# _DIALOG_WIRING_SCRIPT) so every current and future form gets this for
# free. Deliberately a plain constant with no interpolation, same reason
# _SORTABLE_FILTERABLE_TABLE_SCRIPT/_DIALOG_WIRING_SCRIPT are (see
# app/webapp.py) -- one exact string means one CSP script-src sha256 hash
# covers it on every single page, forever, rather than only the first page
# whose copy happened to get hashed. Adding this script means the CSP
# script-src allow-list needs a FIFTH hash -- see
# site/nginx-locations.conf.example's own comment on that.
_SUBMIT_FEEDBACK_SCRIPT = """<script>
(function() {
  document.addEventListener("submit", function(ev) {
    // Deferred one tick: a legacy onsubmit="confirm(...)" handler (kept
    // only for browsers predating <dialog>/showModal, e.g. /my's
    // delete-account form) calls preventDefault() synchronously during
    // this same dispatch if the guest answers "No" -- checking
    // ev.defaultPrevented only AFTER that has had a chance to run avoids
    // leaving every button on the page stuck disabled with nothing
    // actually submitted.
    setTimeout(function() {
      if (ev.defaultPrevented) return;
      document.querySelectorAll("button").forEach(function(b) { b.disabled = true; });
      if (ev.submitter) ev.submitter.textContent = "Please wait...";
    }, 0);
  });
})();
</script>"""


def page(title: str, body: str, banner: str = "") -> str:
    """Every page in the app gets `_SUBMIT_FEEDBACK_SCRIPT` appended
    automatically (2026-07-11) -- see that constant's own docstring/comment
    above for why (the operator: submissions with no feedback, buttons stayed
    clickable during a slow one). No per-page opt-in needed or possible.

    `banner` (2026-07-06, see app/webapp.py's _session_banner_html) is
    OPTIONAL, small, session-aware markup rendered above the page's own
    heading -- e.g. "Logged in as x@example.org - Logout" on /book and
    /courses when reached with an active guest session. Blank by default
    for every other page, unchanged from before this existed.

    .submit-row is flex+gap (2026-07-09, the operator: "buttons too close") --
    previously adjacent buttons/forms in the same row relied on plain
    inline whitespace for spacing, which visually collapsed them together
    (worst on /my's bottom row). One shared fix here covers every
    .submit-row in the app, not just /my's.

    .guests-section/.guest-row (2026-07-09, the operator, screenshot of the
    booking form's "+ Add participant" rows: "lets please group each
    guest with it's remove link visibly. and visibly separate the guests
    from the main user ... here this is too close and so is the + Add
    participant link below") -- previously neither class had ANY CSS at
    all, so a guest row was just three bare, unboxed form fields blending
    into the main "Your email" field above and the "+ Add participant"
    link below. .guests-section now gets a top border + padding to set
    the whole guest block apart from the main guest's own fields;
    .guest-row boxes each individual guest's email+name+"Remove
    participant" together like a mini-card, so it reads as one group.
    See app/webapp.py's _book_page() guest-rows script, which is where
    .guest-row elements are actually created (client-side, one per "+ Add
    participant" click).

    body max-width is 1000px (2026-07-11, the operator, screenshot comparing the
    STATIC homepage's own photo-backed content column against the much
    narrower app pages: "Widen homepage table layout to match photo
    width" -- clarified to mean every application page, not just
    site/index.html's own table) -- matches site/index.html's own
    `div.WordSection1{{max-width:1000px}}`, the container the homepage's
    background photo fills, so every dynamic page (courses/book/my/admin)
    now lines up with that same width instead of its own narrower
    640px. `table{{width:100%}}` below is the other half of the same fix:
    a <table> with no width of its own only ever shrinks to its content
    width regardless of how wide the surrounding body is, so /my's
    bookings table and /admin's overview table wouldn't actually have
    gotten any wider just from the body change alone.

    Font sizes are harmonized app-wide (2026-07-11, the operator: "nothing
    smaller than the current font-size of your button labels" -- button
    labels are `input,button,textarea{{font-size:1em}}` below, i.e. the
    same as ordinary body text). Every rule that was previously SMALLER
    than 1em (`.session-banner`, `.note`, `.hint`, `.date-btn .d-date`,
    `.date-btn .d-spots`, `.sort-indicator`, `.hash-cell` -- all were
    .8em-.95em) had its own font-size declaration dropped, so it now
    inherits the same ambient 1em as everything else, and gained
    `font-style:italic` instead (the operator, same round: "making the smaller
    fonts italic instead -- as I had suggested to you before!") so these
    still read visually as secondary/de-emphasized text without actually
    being smaller than a button label. Deliberately scoped to the app
    pages only, not site/index.html -- see that file's own top-of-file
    comment for why its content (a raw Word paste) isn't touched here.

    `.id-input` (2026-07-08, the operator, screenshot of /admin/login's password
    field stretched across the full-width 1000px body: "the Name, Email,
    Password fields should not be that wide ... wide enough for really
    long passwords (maybe 50 chars) and emails like
    firstname.doublebarrelled-name@long-company.example" -- 54 chars;
    confirmed "50 chars is OK" as the sizing target) caps `.big-input`'s
    own `width:100%` at `max-width:50ch` -- `ch` scales with `.big-input`'s
    own font-size, so this is a character-count cap, not a fixed pixel
    width. Applied ALONGSIDE `big-input` (`class="big-input id-input"`),
    never replacing it, and ONLY on single-line Name/Email/Password
    `<input>` fields app-wide (every page that has one -- login, signup,
    admin login, /my settings, the booking form's own guest rows).
    Also applied to the `type="search"` table-filter boxes on /my and
    /admin (2026-07-08, the operator: "lets also set the filter to 50 chars
    width" -- overriding this docstring's earlier reasoning that they
    should stay full-width to visually pair with their table). Still
    deliberately NOT applied to `.big-input` textareas (the Cancel/
    Reinstate reason/message boxes -- free text benefits from the full
    width)."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" type="image/png" href="/favicon/favicon-96x96.png" sizes="96x96">
<link rel="icon" type="image/svg+xml" href="/favicon/favicon.svg">
<link rel="shortcut icon" href="/favicon/favicon.ico">
<link rel="apple-touch-icon" sizes="180x180" href="/favicon/apple-touch-icon.png">
<link rel="manifest" href="/favicon/site.webmanifest">
<style>
body{{font-family:sans-serif;max-width:1000px;margin:2em auto;padding:0 1em;color:#222}}
.session-banner{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;
  gap:.3em 1em;background:#f4f7f4;border:1px solid #ddd;border-radius:8px;padding:.5em 1em;
  margin-bottom:1em;overflow-wrap:anywhere;font-style:italic}}
.session-banner form{{display:inline}}
a{{color:#196B24}} .err{{color:#b00020}} .note{{color:#555;font-style:italic}} .card{{border:1px solid #ddd;border-radius:8px;padding:1em;margin:1em 0}}
input,button,textarea{{font-size:1em;padding:.4em;margin:.2em 0}} button{{cursor:pointer}}
input[readonly]{{background:#eee;color:#555;cursor:not-allowed}}
label{{display:block;margin-top:.6em}}
.subtitle{{color:#444;margin:-.4em 0 1em;font-size:1.2em;font-weight:500}}
.req{{color:#b00020}}
.hint{{color:#555;margin:.1em 0 0;font-style:italic}}
.th-note{{display:block;font-weight:normal;font-style:italic;color:#666}}
.scope-active{{font-weight:bold}}
.big-input{{font-size:1.25em;width:100%;box-sizing:border-box;padding:.35em .5em;display:block}}
.id-input{{max-width:50ch}}
.dates{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.5em;margin:.4em 0}}
.date-btn{{position:relative;display:block}}
.date-btn input{{position:absolute;opacity:0;width:1px;height:1px}}
.date-btn span{{display:block}}
.date-btn > span{{padding:.5em .8em;border:1px solid #ccc;border-radius:6px;cursor:pointer;text-align:center;line-height:1.3}}
.date-btn .d-date{{font-style:italic}}
.date-btn .d-spots{{color:#666;margin-top:.1em;font-style:italic}}
.date-btn input:checked + span{{background:#196B24;color:#fff;border-color:#196B24}}
.date-btn input:checked + span .d-spots{{color:#dff0e2}}
.date-btn input:focus-visible + span{{outline:2px solid #196B24;outline-offset:1px}}
.date-badge>span{{cursor:default;background:#f2f2f2;color:#888;position:relative;overflow:hidden}}
.date-badge .ribbon{{position:absolute;top:.6em;right:-3.2em;width:11em;transform:rotate(45deg);
  background:#666;color:#fff;font-weight:bold;text-align:center;padding:.15em 0;
  box-shadow:0 1px 2px rgba(0,0,0,.25)}}
.selected-box{{background:#f4f7f4;border:1px solid #ddd;border-radius:8px;padding:.7em 1em;margin:.8em 0}}
.description{{background:#fdf8ef;border:1px solid #eee0c0;border-radius:8px;padding:1em 1.2em;margin:.8em 0}}
.description ul,.description ol{{margin:.4em 0;padding-left:1.4em}}
.description p:first-child{{margin-top:0}} .description p:last-child{{margin-bottom:0}}
.guests-section{{margin-top:1.2em;padding-top:1em;border-top:1px solid #ddd}}
.guest-row{{border:1px solid #ddd;border-radius:8px;padding:.8em 1em .6em;margin-bottom:.8em}}
.guest-row label{{margin-top:.4em}} .guest-row label:first-child{{margin-top:0}}
.guest-row .remove-guest-btn{{display:inline-block;margin-top:.6em}}
#add-guest-btn{{display:inline-block;margin-top:.2em}}
.submit-row{{margin-top:1.4em;display:flex;flex-wrap:wrap;align-items:center;gap:.6em}}
button:disabled{{opacity:.5;cursor:not-allowed}}
.link-button{{background:none;border:none;padding:0;margin:0;color:#196B24;text-decoration:underline;font:inherit;cursor:pointer}}
.link-button:disabled{{color:#888;text-decoration:none;opacity:1}}
.table-tools{{margin-bottom:.6em}}
table{{border-collapse:collapse;width:100%}}
th{{user-select:none;white-space:nowrap}}
.sort-indicator{{font-style:normal}}
.nowrap{{white-space:nowrap}}
.hash-cell{{word-break:break-all;font-family:monospace;font-style:italic}}
.course-card{{border:1px solid #ddd;border-radius:8px;padding:1em 1.2em;margin:1em 0}}
.course-card h2{{margin:0 0 .2em;font-size:1.15em}}
.tab-radio{{display:none}}
.tab-panel{{display:none;padding-top:.2em}}
#my-tab-login:checked ~ #my-panel-login{{display:block}}
#my-tab-signup:checked ~ #my-panel-signup{{display:block}}
.tab-labels{{display:flex;border-bottom:1px solid #ddd;margin-top:.6em}}
.tab-label{{padding:.5em 1.2em;cursor:pointer;color:#555;border-bottom:2px solid transparent;margin-bottom:-1px}}
#my-tab-login:checked ~ .tab-labels label[for="my-tab-login"],
#my-tab-signup:checked ~ .tab-labels label[for="my-tab-signup"]{{color:#196B24;border-bottom-color:#196B24;font-weight:bold}}
</style></head><body>
{banner}
<h1>{esc(title)}</h1>
{body}
{_SUBMIT_FEEDBACK_SCRIPT}
</body></html>"""
