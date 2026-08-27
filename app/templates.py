"""Tiny HTML helpers -- no Jinja/templating engine dependency. Every
user-supplied value must go through esc() before landing in HTML."""
from __future__ import annotations

import re

import html

from .version import short_version


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# 2026-07-11: a real Cancel submission was observed sitting at 2.05s in
# devtools' Network tab with every button still clickable and no
# indication at all that the click had been registered. A cancel/
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
      waitFor("submit");
    }, 0);
  });

  // Any same-page navigation can be slow -- /book and /my's booking
  // overlay both consult the calendar before they can render, and a cold
  // conflict feed turns that into seconds. Until the new document
  // arrives the browser keeps showing THIS page, so a click looks
  // ignored. One rule for every link rather than a list of the slow
  // ones: a click starts a timer, and the loading panel appears only if
  // the page has not been replaced by then -- fast navigations never
  // flash it.
  // A form can carry a hidden mirror of a field that lives in ANOTHER
  // form (data-mirror="<id>"): filled from that field the moment this
  // one is submitted, so two independent forms on a page can preserve
  // each other's drafts without sharing a form -- sharing one makes the
  // browser validate both, which is how a half-typed address came to
  // block a name from being saved (the account page, 2026-08-27).
  // NOTE: this script ships on EVERY page, so a comment here must not
  // quote a route or a UI string verbatim -- a test grepping the page
  // text finds the comment and reads it as markup that is present.
  document.addEventListener("submit", function(ev) {
    var form = ev.target;
    if (!form || !form.querySelectorAll) return;
    form.querySelectorAll("input[data-mirror]").forEach(function(hidden) {
      var live = document.getElementById(hidden.getAttribute("data-mirror"));
      if (live) hidden.value = live.value;
    });
  }, true);

  var DELAY_MS = 250;
  var timer = null;

  function waitFor(_why) {
    if (timer) return;
    timer = setTimeout(show, DELAY_MS);
  }

  function show() {
    if (document.querySelector(".loading-overlay")) return;
    var back = document.createElement("div");
    back.className = "loading-overlay";
    // A shape, not a spinner: the bars stand where the heading, the
    // date grid and the fields of the page being fetched will be, so
    // the wait reads as "this is coming" rather than "something is
    // happening somewhere".
    back.innerHTML = "<div class='skeleton' role='status' aria-label='Loading'>" +
      "<span class='sk sk-title'></span>" +
      "<span class='sk sk-line'></span><span class='sk sk-line short'></span>" +
      "<span class='sk sk-grid'><i></i><i></i><i></i><i></i><i></i><i></i></span>" +
      "<span class='sk sk-line'></span><span class='sk sk-btn'></span></div>";
    document.body.appendChild(back);
  }

  document.addEventListener("click", function(ev) {
    var a = ev.target.closest ? ev.target.closest("a") : null;
    if (!a || ev.defaultPrevented || ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;   // opens a tab
    if (a.target && a.target !== "_self" && a.target !== "_top") return;
    var href = a.getAttribute("href") || "";
    // A fragment, a mailto:/tel:, or a download navigates nowhere.
    if (!href || href.charAt(0) === "#" || a.hasAttribute("download")) return;
    if (/^[a-z]+:/i.test(href) && a.origin !== window.location.origin) return;
    waitFor("link");
  });

  // ESC closes the server-rendered overlay too. A NON-modal <dialog
  // open> -- which is what that one is, since it is rendered without any
  // script -- gets no ESC handling from the browser: only showModal()
  // buys that. So every overlay on the site closes the same three ways
  // (X, click outside, ESC) rather than two of them depending on how the
  // overlay happened to be opened. Progressive enhancement: without JS
  // the X and the backdrop are plain links and still work.
  document.addEventListener("keydown", function(ev) {
    if (ev.key !== "Escape") return;
    var panel = document.querySelector("dialog[open]");
    if (!panel) return;
    var close = panel.querySelector(".dialog-x");
    if (close && close.getAttribute("href")) { window.location.href = close.getAttribute("href"); }
  });

  // Coming BACK to a cached page (the back button) must not leave the
  // panel sitting on top of it.
  window.addEventListener("pageshow", function() {
    if (timer) { clearTimeout(timer); timer = null; }
    var panel = document.querySelector(".loading-overlay");
    if (panel) panel.remove();
  });
})();
</script>"""


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _strip_css_comments(css: str) -> str:
    """The stylesheet ships INSIDE every page, so its comments are page
    TEXT: a grep or an assertion over a rendered page matches them, and
    four separate test failures this month were a comment containing an
    ordinary English word ("already"), not a bug in the markup.

    Comments are for whoever edits app/templates.py, and they stay in the
    source. Stripping them here removes the whole class of failure rather
    than rewording one comment at a time -- and takes a few KB off every
    response as a side effect. Done once at import, not per request."""
    text = _CSS_COMMENT_RE.sub("", css)
    return "\n".join(line for line in text.splitlines() if line.strip())


_CSS = _strip_css_comments("""body{font-family:sans-serif;max-width:1000px;margin:2em auto;padding:0 1em;color:#222}
.session-banner{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;
  gap:.3em 1em;background:#f4f7f4;border:1px solid #ddd;border-radius:8px;padding:.5em 1em;
  margin-bottom:1em;overflow-wrap:anywhere;font-style:italic}
.session-banner form{display:inline}
/* The role this session is in, in the one place it needs saying. Red,
   bold and UPRIGHT against the banner's italic, at the same size --
   loud enough to notice on every admin page, quiet enough not to shout. */
.session-role{color:#b00020;font-weight:bold;font-style:normal}
a{color:#196B24} .err{color:#b00020} .note{color:#555;font-style:italic} .card{border:1px solid #ddd;border-radius:8px;padding:1em;margin:1em 0}
input,button,select,textarea{font-size:1em;padding:.25em .5em;margin:.2em 0} button{cursor:pointer}
/* ONE height for every single-line control, expressed in rem so it does
   NOT scale with the control's own font-size: a .big-input (1.25em) and
   the plain button next to it then line up, which matching paddings by
   hand never achieves. `height`, not `min-height`: a floor does not
   equalise anything -- the 1.25em field's own box came out ~1.5px above
   it, so the floor never bound and the pair drifted 2px apart again
   (measured). Checkboxes/radios keep their native size; the dialog X
   and .link-button opt out below. */
input:not([type=checkbox]):not([type=radio]),button,select{
  height:2.05rem;box-sizing:border-box;line-height:1.2}
input[readonly]{background:#eee;color:#777;cursor:not-allowed}
label{display:block;margin-top:.6em}
.subtitle{color:#444;margin:-.4em 0 1em;font-size:1.2em;font-weight:500}
.req{color:#b00020}
.hint{color:#555;margin:.1em 0 0;font-style:italic}
.th-note{display:block;font-weight:normal;font-style:italic;color:#666}
.scope-active{font-weight:bold}
/* Vertical padding trimmed to fit the shared height above (32.8px): at
   1.25em this field's own text needs 24px, so the padding is what had to
   give. Lowering the height alone would have shrunk the buttons beside
   it and NOTHING else -- reopening the mismatch it was meant to close. */
.big-input{font-size:1.25em;width:100%;box-sizing:border-box;padding:.1em .5em;display:block}
.id-input{max-width:50ch}
/* grid-auto-rows:1fr -- EVERY row the same height, not just every box
   within a row. height:100% on the boxes below equalises them against
   their own row, so a first row holding a three-line box (date, spots
   and a changed time) left the lone box on the second row visibly
   shorter (reported 2026-08-27). Rows are equal to each other now, so a
   date looks the same wherever it falls in the grid. */
.dates{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  grid-auto-rows:1fr;gap:.5em;margin:.4em 0}
/* margin-top:0 (2026-07-14): a bookable box is a <label>, and the generic
   label{margin-top:.6em} rule above gave it a top margin INSIDE the grid
   cell -- so it stretched to the row height MINUS that margin, rendering
   ~10px shorter and lower than the <span>-based Booked badges in the same
   row (measured live; the .dates grid's own gap handles spacing here). */
.date-btn{position:relative;display:block;margin-top:0}
.date-btn input{position:absolute;opacity:0;width:1px;height:1px}
.date-btn span{display:block}
/* height:100% + flex centering (2026-07-14, from a live screenshot):
   EVERY date box stretches to its grid row's height -- a row can mix
   1-line (badge without override time), 2-line (date + spots), and
   3-line (date + spots + override time) boxes, and unequal heights read
   as broken. Content stays vertically centered in the stretched box. */
.date-btn > span{padding:.5em .8em;border:1px solid #ccc;border-radius:6px;cursor:pointer;text-align:center;line-height:1.3;
  height:100%;box-sizing:border-box;display:flex;flex-direction:column;justify-content:center}
.date-btn .d-date{font-style:italic}
.date-btn .d-spots{color:#666;margin-top:.1em;font-style:italic}
.date-btn .d-override-time{color:#b45f06;font-weight:bold;margin-top:.1em}
.date-btn input:checked + span{background:#196B24;color:#fff;border-color:#196B24}
.date-btn input:checked + span .d-spots{color:#dff0e2}
.date-btn input:checked + span .d-override-time{color:#ffd54f}
.date-btn input:focus-visible + span{outline:2px solid #196B24;outline-offset:1px}
/* min-height (2026-07-14): keeps two-line proportions even in a row of
   nothing but single-line badges, so the diagonal ribbon (clipped by
   this rule's own overflow:hidden) always has room -- the stretch/flex
   centering itself now lives on the shared .date-btn > span rule above,
   since ALL boxes equalize to the row height, not just badges. */
.date-badge>span{cursor:default;background:#f2f2f2;color:#888;position:relative;overflow:hidden;min-height:3.6em}
.date-badge .ribbon{position:absolute;top:.6em;right:-3.2em;width:11em;transform:rotate(45deg);
  background:#666;color:#fff;font-weight:bold;text-align:center;padding:.15em 0;
  box-shadow:0 1px 2px rgba(0,0,0,.25)}
/* margin-bottom 0 (2026-07-14, live screenshot): the box's own .8em
   bottom margin stacked on the card's 1em padding read as a stray gap
   under "Selected date" -- the card's padding alone is enough there. */
.selected-box{background:#f4f7f4;border:1px solid #ddd;border-radius:8px;padding:.7em 1em;margin:.8em 0 0}
/* The logged-in booking-identity line, rendered INSIDE the guests card
   above the add-participant link (2026-07-14, live screenshot: it
   dangled between two cards) -- top margin 0 so it doesn't double up
   with the card's own padding. NOTE: this <style> block ships on every
   page, so comments here must not quote visible UI strings verbatim
   (tests and greps over page text would match the comment first). */
.booking-as{margin:0 0 .8em}
.description{background:#fdf8ef;border:1px solid #eee0c0;border-radius:8px;padding:1em 1.2em;margin:.8em 0}
.description ul,.description ol{margin:.4em 0;padding-left:1.4em}
.description p:first-child{margin-top:0} .description p:last-child{margin-bottom:0}
.guests-section{margin-bottom:1.2em;padding-bottom:1em;border-bottom:1px solid #ddd}
.guest-row{border:1px solid #ddd;border-radius:8px;padding:.8em 1em .6em;margin-bottom:.8em}
.guest-row label{margin-top:.4em} .guest-row label:first-child{margin-top:0}
.guest-row .remove-guest-btn{display:inline-block;margin-top:.6em}
#add-guest-btn{display:inline-block;margin-top:.2em}
.submit-row{margin-top:1.4em;display:flex;flex-wrap:wrap;align-items:center;gap:.6em}
button:disabled{opacity:.5;cursor:not-allowed}
.link-button{background:none;border:none;height:auto;padding:0;margin:0;color:#196B24;text-decoration:underline;font:inherit;cursor:pointer}
.link-button:disabled{color:#888;text-decoration:none;opacity:1}
.table-tools{margin-bottom:.6em}
table{border-collapse:collapse;width:100%}
th{user-select:none;white-space:nowrap}
.sort-indicator{font-style:normal}
.nowrap{white-space:nowrap}
.hash-cell{word-break:break-all;font-family:monospace;font-style:italic}
.course-card{border:1px solid #ddd;border-radius:8px;padding:1em 1.2em;margin:1em 0}
.course-card h2{margin:0 0 .2em;font-size:1.15em}
.tab-radio{display:none}
.tab-panel{display:none;padding-top:.2em}
#my-tab-login:checked ~ #my-panel-login{display:block}
#my-tab-signup:checked ~ #my-panel-signup{display:block}
/* 2026-07-13: same Login/Sign-up tab-switcher, embedded a second time on
   /book/<shortname> (see App._login_signup_tabs_html()'s own docstring)
   -- "book" gets its own ID-namespaced pair of rules rather than sharing
   /my's literal IDs, even though only one of the two is ever rendered
   in a given response. */
#book-tab-login:checked ~ #book-panel-login{display:block}
#book-tab-signup:checked ~ #book-panel-signup{display:block}
/* /admin's Future Sessions box (2026-08-27): per-date status pills, the
   weekday+time chip each tab leads with (that pair is how a course is
   recognised at a glance), and the row note under a changed time. */
/* No font-size shrink anywhere here: the standing rule is that nothing
   renders below the 1em button baseline, and de-emphasis is done with
   italics. These pills read as secondary through their background,
   which costs no legibility at all. */
.st{padding:.1em .5em;border-radius:10px;white-space:nowrap}
.st-ok{background:#e6f4e8;color:#196B24}
.st-full{background:#fdf3e0;color:#8a5a00}
.st-can{background:#fde8e8;color:#b00020;font-weight:bold}
.st-con{background:#eee;color:#666}
.st-hid{background:#ece7f6;color:#5b3fa0;font-weight:bold}
.row-note{font-style:italic;color:#666;white-space:normal;max-width:34ch}
/* Equal air above and below this line: the same gap under the tab rule
   as over the table's own header row, so the course line sits between
   them rather than clinging to one. */
.course-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6em;margin:1em 0}
/* Deliberately SHORTER than a control (2026-08-27, the operator: "here
   the green then looks like a green button"). Shape is a promise about
   behaviour: at the height of the button beside it, a solid green chip
   you cannot click reads as one you can. This is read, not operated, so
   it takes its size from its text -- the same reason .st pills, .note
   and every other label stay off the shared control height.

   line-height + equal vertical padding: with the inherited line-height
   the glyphs sat low in the pill, leaving more colour below the text
   than above it. Pinning both makes the box symmetric about the text. */
.when{background:#196B24;color:#fff;border-radius:6px;padding:.03em .5em;font-weight:bold;
  line-height:1.15;display:inline-block;white-space:nowrap}
table.sessions td,table.sessions th{border-bottom:1px solid #eee;padding:.45em .6em;
  text-align:left;vertical-align:top}
/* Buttons in a table cell, aligned with the text beside them. Two
   earlier attempts with vertical-align alone did NOT work, and the
   reason is worth writing down: an inline-block button is laid out in a
   LINE BOX, so it is subject to the line's baseline and to the global
   `button{margin:.2em 0}` -- in a tall row it ends up visibly lower
   than the first line of text next to it. Taking the buttons out of
   inline layout entirely (a flex box, items aligned to the start, no
   stray top margin) removes every one of those variables instead of
   fighting them one at a time.

   The flex box is an inner <div>, NOT the cell: `display:flex` on a <td>
   takes it out of table layout, so border-collapse stops applying and
   its border renders twice as thick as every other cell's -- a visibly
   heavier frame around the Cancel column. The cell stays a table-cell;
   its child does the work. */
td.actions .btn-row{display:flex;flex-wrap:wrap;gap:.4em;align-items:flex-start}
/* Introduces the guests block on the booking page. NOTE: this <style>
   ships on every page, so comments here must avoid ordinary English
   words a test or a grep might look for in the page TEXT -- see the
   .booking-as comment above for the same warning. */
.guests-intro{margin:0 0 .8em;color:#444}
/* The loading panel: shown by the script above only when a navigation
   has taken longer than a moment. A SHAPE of the page being fetched --
   heading, two lines, a date grid, a button -- rather than a spinner,
   because the shape says what is coming. The sweep is a background
   gradient in motion, which costs no layout and no script tick.
   prefers-reduced-motion gets a still panel: the shape by itself
   conveys waiting, and the movement is only reassurance.
   NOTE: this <style> ships on every page, so no ordinary English word a
   test might grep for in the page TEXT belongs in these comments. */
.loading-overlay{position:fixed;inset:0;z-index:1200;background:rgba(255,255,255,.75);
  display:flex;align-items:center;justify-content:center}
.skeleton{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.4em 1.6em;
  box-shadow:0 8px 30px rgba(0,0,0,.18);width:min(92%,520px);display:block}
.sk{display:block;border-radius:6px;background:#ececec;margin:0 0 .7em;
  background-image:linear-gradient(90deg,#ececec 0%,#f7f7f7 40%,#ececec 80%);
  background-size:300% 100%;animation:sk-sweep 1.2s linear infinite}
.sk-title{height:1.5em;width:55%;margin-bottom:1.1em}
.sk-line{height:.9em}
.sk-line.short{width:70%}
.sk-grid{background:none;animation:none;display:grid;
  grid-template-columns:repeat(3,1fr);gap:.5em;margin:1em 0}
.sk-grid i{display:block;height:3.2em;border-radius:6px;background:#ececec;
  background-image:linear-gradient(90deg,#ececec 0%,#f7f7f7 40%,#ececec 80%);
  background-size:300% 100%;animation:sk-sweep 1.2s linear infinite}
.sk-btn{height:2.2em;width:9em;margin-top:1.2em}
@keyframes sk-sweep{from{background-position:150% 0}to{background-position:-150% 0}}
@media (prefers-reduced-motion:reduce){.sk,.sk-grid i{animation:none}}
/* --- the settings console (/admin/settings) --------------------------
   Ported from my-booking.local/,mockups/admin-settings.html, which is
   now a preview of THIS stylesheet rather than a holder of its own copy
   (a mockup that redefines app rules goes on showing the old version).
   Everything below is used only by that page; it costs every other page
   the bytes and nothing else. */
.sn-input:invalid,.name-input:invalid{border-color:#b00020;background:#fff6f6}
/* The field and its preview are one thing seen two ways, so they share a
   row. TWO COLUMNS AND TWO ROWS, not two self-contained halves: both
   labels sit in row 1 and both boxes in row 2, so the labels stay on one
   line and the boxes start level. Stacking each label above its own box
   inside a half is what let the two sides drift apart.

   minmax(0,1fr) rather than 1fr: a grid column's default minimum is its
   CONTENT, so one long unbroken line in the preview would push the halves
   out of true and overflow the card. Stacks below 900px, where two halves
   are narrower than either deserves. */
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  grid-template-rows:auto auto;align-items:stretch;column-gap:1.2em;row-gap:0;
  margin:.6em 0 .2em}
@media (max-width:900px){.split{grid-template-columns:minmax(0,1fr)}
/* Same margins on both, overriding the block-label rule on the left one,
   and .2em to its box -- the same distance every other field has, which
   is the input's own margin. */
.split-head{margin:.6em 0 .2em;align-self:end}
/* Sits against the box above it, full width under BOTH halves: the
   macros apply to the description as a whole, and the hint's own top
   margin would otherwise leave it floating between the two. */
.split-foot{margin:.2em 0 0}
/* A tag INSIDE the field, at its right edge, saying that the grey text
   in front of it is generated rather than typed. It disappears the
   moment the field holds a real value, because then the grey text is
   gone and there is nothing left to explain. */
.with-tag{position:relative;display:block}
.with-tag .big-input{padding-right:9.5em}
.auto-tag{position:absolute;right:.5em;top:50%;transform:translateY(-50%);
  background:#e8eef8;color:#3a5a99;border:1px solid #cdd9ef;border-radius:4px;
  padding:.05em .4em;font-style:normal;letter-spacing:.04em;
  pointer-events:none}
.auto-tag[hidden]{display:none}
/* A label that explains itself on hover. Dotted underline so it is
   visibly askable -- an invisible tooltip is one nobody finds. */
/* ONE shape for every field: name (and its note) on its own line, the
   control beneath it. In front, each control starts at a different x
   depending on how long its name is, so a row of four can never line up
   -- above, every field shares one left edge whatever it is called. */
label > input,label > select,label > textarea{display:block}
/* The name reads as a small header rather than as running text: a slight
   grey, hugging the words (width:fit-content, so the shade and the dotted
   hover underline stop where the text does, not at the far edge of a
   full-width field). */
.lbl{display:inline-block;background:#f1f3f1;border-radius:4px;padding:.05em .4em;
  border-bottom:1px dotted #999;cursor:help}
/* Preview is NOT a field: no shading, no dotted underline. Both of those
   mark something you can type into or ask about, and it is neither -- it
   is a read-only view of the box to its left. */
.preview-head{color:#555;font-style:italic}
/* Sized to the largest value each field could sensibly hold -- three
   digits plus the stepper -- instead of stretching to whatever the row
   allows. A box far wider than its content invites the reader to wonder
   what else belongs in it. */
.num-input{width:7ch}
.lang-input{width:9ch}
/* Bullet lines wrap under their own text, not back under the marker. */
.tip-body{display:block;text-indent:-.75em;padding-left:.75em}
.split-foot .hint{margin:0}
.split-half{min-width:0;display:flex;flex-direction:column}
/* Both boxes start on the row's top edge. align-items on the CONTAINER
   was :end, which aligned the boxes by their BOTTOMS -- so any fraction
   of a pixel between the two heights (the script sets a measured
   scrollHeight) opened a visible gap above one of them. Only the labels
   want end-alignment, and they ask for it themselves. The textarea also
   had .2em of its own top margin from the shared control rule, which the
   preview does not; zeroed here so the two top edges are the same edge. */
.split-half .desc-input,.split-half .preview-body{margin:0;align-self:stretch}
/* border-box so a height set on it INCLUDES its padding and border: the
   script sets the same number on this and on the textarea, and without
   this the padding was added on top and the preview ran taller. */
.preview-body{box-sizing:border-box;min-height:6em;overflow:auto;margin:0}
/* An unresolved name is shown rather than left to look like literal text
   the reader will see: it is the one thing a preview must not render
   silently, because on the live page it would either be an error or the
   braces themselves. */
.macro-missing{background:#fde8e8;color:#b00020;border-radius:4px;padding:0 .2em;
  font-family:monospace}
/* A macro name is typed INTO other texts as {name}, so it stays short:
   20 characters, letters/digits/underscore. The box is sized to hold
   exactly that many, so the field itself states the limit -- no sentence
   needed, and a name that fits is visibly a name that fits. */
.name-input{width:21ch;max-width:100%}
/* Hovering hint on a macro chip. A real element rather than the browser's
   own title=: that one cannot wrap, cannot be styled, and opens after a
   fixed ~1s delay the page has no say over. This one is instant -- no
   transition at all. Shown on focus too, so it is reachable by keyboard. */
/* The hint is a real ELEMENT, not a ::after fed by one attribute. A
   pseudo-element carries a single string, so no part of it can be
   coloured -- and a consequence like the shortname's needs to be red.
   Built by script from data-tip/data-warn (see buildTips) so every hover
   surface on the page uses this one mechanism: labels, macro chips and
   the HTML badge alike. */
[data-tip]{position:relative}
.tip{display:none;position:absolute;left:0;bottom:calc(100% + .4em);z-index:5;
  min-width:14em;max-width:38em;width:max-content;
  background:#333;color:#fff;border-radius:6px;padding:.5em .7em;font-family:sans-serif;
  font-style:normal;font-weight:normal;line-height:1.35;text-align:left;
  letter-spacing:normal;text-transform:none;box-shadow:0 4px 14px rgba(0,0,0,.3)}
[data-tip]:hover > .tip,[data-tip]:focus-visible > .tip{display:block}
.tip-warn{display:block;margin-top:.5em;background:#b00020;color:#fff;
  border-radius:4px;padding:.3em .5em}
/* A name whose hint carries a consequence says so on its face: the hover
   holds the detail, but nobody hovers what looks ordinary. Yellow rather
   than red -- red is the panel's own line, and an alarm on a field you
   are only reading would cry wolf. */
.lbl[data-warn]::after{content:"⚠";color:#e0a500;margin-left:.4em;
  font-style:normal;text-shadow:0 0 1px rgba(0,0,0,.25)}
/* .course-head, .when and .wd come from the app's own stylesheet, which
   this page already loads -- redefining them here meant the mockup went
   on showing a button-sized green chip after the app rule was made
   shorter. A mockup that keeps its own copy of a rule stops being a
   preview of the real page. Only the digit alignment is added, and only
   because the mockup shows four courses side by side. */
.when{font-variant-numeric:tabular-nums}
/* One row for every short field. align-items:end keeps the CONTROLS on
   one line even where a name wraps to two; flex-wrap lets the row break
   by itself on a narrow window instead of being split by hand. */
.field-row{display:flex;flex-wrap:wrap;gap:.4em 1.2em;align-items:flex-end}
.field-row label{margin-top:.6em}
.macro-chip{cursor:help;background:#eef2fb;color:#3a5a99;border-radius:4px;padding:.05em .35em}
.macro-rich{cursor:help;background:#f0f0f0;color:#999;text-decoration:line-through}
/* Sits INSIDE the value cell, beside the field it describes -- "contains
   HTML" is a property of the value, not of the name. On its own line under
   the name box it dangled, and stretched that one row taller than the
   others. Same palette as a macro chip, so it reads as part of the family
   rather than as a warning. */
/* The marker hangs OUTSIDE the box, to its left, rather than sharing the
   row with it: every value box then starts on the same left edge as the
   column heading, marked or not, and the marker costs the field no width
   at all. A spacer for unmarked rows would have aligned the boxes but
   pushed all of them right, away from the heading. */
.value-cell{position:relative}
/* A macro value is prose, not a word: it wraps and the box grows with it.
   Scrolling a one-line field to read the end of a sentence is the thing
   this replaces. No resize grip: the box already settles at the height
   its text needs on every edit and on blur, so a hand-dragged height
   would only be overwritten a moment later. */
textarea.grow{resize:none;overflow:hidden;line-height:1.35;min-height:2.05rem;
  font-family:inherit}
.value-cell .big-input{flex:1;min-width:0}
/* Vertical: writing-mode turns the text on its side, the rotation makes
   it read bottom-to-top (the usual direction for a spine label). Costs
   ~1.6em of width whatever the value's length, instead of a chip whose
   width follows its own label. */
.macro-badge{position:absolute;right:100%;top:.2em;bottom:.2em;margin-right:.45em;
  width:1.7em;overflow:hidden;cursor:help;
  background:#eef2fb;color:#3a5a99;border:1px solid #cdd9ef;border-radius:4px;
  font-weight:bold;font-style:normal}
/* The label is CONDENSED along its own length rather than shrunk: rotated
   upright, then scaled on the axis it reads along, so the letters keep
   their height and only the run gets shorter. "HTML" needs ~41px upright
   and a one-line row is 33px, which is what overflowed. Absolutely
   positioned so its unrotated width never widens the spine. */
.macro-badge i{position:absolute;top:50%;left:50%;white-space:nowrap;
  font-style:normal;letter-spacing:.04em;
  transform:translate(-50%,-50%) rotate(-90deg) scaleX(var(--squeeze,1))}
.macro-badge[hidden]{display:none}
/* Red says the markup will not render as written -- a tag left open or
   closed out of order. Same spine, so the eye finds it in the same place. */
.macro-badge.is-broken{background:#fde8e8;color:#b00020;border-color:#f0b8b8}
/* The legend in the note below is prose, so it stays horizontal. */
.badge-inline{background:#eef2fb;color:#3a5a99;border:1px solid #cdd9ef;border-radius:4px;
  padding:.05em .4em;font-weight:bold;letter-spacing:.04em;font-style:normal}
table.sessions td,table.sessions th{border-bottom:1px solid #eee;padding:.45em .6em;
  text-align:left;vertical-align:top}
td.actions .btn-row{display:flex;gap:.4em;align-items:flex-start}
/* Which build this page came from, so a screenshot of it identifies
   itself. Italic + grey rather than smaller: nothing here renders below
   the 1em button baseline (see the .note/.hint rules above). */
.version{margin:2.5em 0 0;text-align:right;font-style:italic;color:#999}
/* Controls in a table cell take their spacing from the cell's padding,
   never from their own margin. Zeroing it for buttons ALONE (which this
   rule used to do) left them sitting .2em higher than the fields beside
   them -- and at a 1.25em field that is 4px, plainly visible in a
   screenshot. Same rule for all of them, so they share one top edge. */
table td input,table td select,table td button,table td textarea{margin-top:0;margin-bottom:0}
/* EVERY overlay on every page, in one rule (2026-08-27, the operator:
   "ALL are now centered please"). There was no `dialog` rule at all
   before this, so each one sat wherever the browser put it -- typically
   pinned near the top. `margin:auto` is what actually centres a modal
   <dialog>: several engines apply `auto` only in the inline axis by
   default, so the block axis has to be asked for. The height cap
   matters as much: a booking form with a date grid, guest rows and an
   acknowledgement runs past the bottom of a laptop viewport, and the
   submit button becomes unreachable with no scrollbar. */
/* /my's "New booking" frame: one row per course, led by the same chip
   /admin uses. Equal chip widths without a guessed min-width -- the
   labels differ only in the weekday and the digits, so pinning those two
   makes the natural widths match (see .wd and tabular-nums). */
.course-pick{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6em;width:100%;
  box-sizing:border-box;text-align:left;background:none;border:1px solid #ddd;
  border-radius:8px;padding:.7em .9em;margin:.4em 0;cursor:pointer;font:inherit;
  color:inherit;text-decoration:none}
.course-pick:hover{background:#f4f7f4;border-color:#196B24}
.course-pick .when{font-variant-numeric:tabular-nums}
.course-pick .wd{display:inline-block;min-width:2.4em}
.pick-title{font-weight:bold}
/* A dialog opened by the SERVER (<dialog open>, no script) is
   non-modal: it renders in normal document flow, which is why the
   booking overlay appeared inline rather than floating. Only
   showModal() puts a dialog in the top layer with a real ::backdrop,
   and that is a JS call this design deliberately avoids.
   So the floating is done here instead: fixed position, centred, above
   its own dimmed backdrop element. `.overlay-backdrop` is a LINK, so
   clicking outside the panel closes it -- the behaviour people expect
   from a modal, without the modal. */
dialog.book-dialog{max-width:720px}
dialog.book-dialog[open]{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
  z-index:1001;box-shadow:0 8px 30px rgba(0,0,0,.35);background:#fff}
.overlay-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;
  display:block}
/* The "done" panel after booking from /my's overlay: shown in the same
   place the booking form was, then faded out while a <meta refresh>
   carries the reader back to /my. CSS, not script -- the fade is
   cosmetic and the refresh is what actually returns them, so a browser
   that honours neither still lands on a page with a plain link. */
.done-panel{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1001;
  background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.6em 2em;text-align:center;
  box-shadow:0 8px 30px rgba(0,0,0,.35);animation:done-fade .8s ease 2.2s forwards}
.done-panel h2{margin:0 0 .3em;color:#196B24}
@keyframes done-fade{to{opacity:0;visibility:hidden}}
dialog .x{position:absolute;top:.4em;right:.5em;font-size:1.6em;line-height:1;
  color:#666;text-decoration:none;padding:0 .2em}
/* The tabs ARE this frame's title, so they carry the <h2>'s size and
   weight; only colour marks which one is active. */
#my-tab-upcoming:checked ~ #my-panel-upcoming{display:block}
#my-tab-past:checked ~ #my-panel-past{display:block}
#my-tab-upcoming:checked ~ .tab-labels label[for="my-tab-upcoming"],
#my-tab-past:checked ~ .tab-labels label[for="my-tab-past"]{color:#196B24;
  border-bottom-color:#196B24}
/* Tabs that ARE a frame's title (/my): heading size and weight, sitting
   at the frame's own padding exactly where an <h2> would. Opt-in via
   .tab-title -- /admin's Future Sessions tabs sit UNDER an <h2> and must
   keep the ordinary tab look, or nothing distinguishes the selected one
   from a row of equally-bold headings. */
/* A row of tabs used AS a frame's title must occupy the same box an
   <h2> would, or the two frames on /my start their content at different
   heights -- measured from a screenshot: 16px under the border in one,
   36px in the other. The 20px was this row's own top margin plus each
   tab's .5em top padding, neither of which a heading has. Zero both, keep
   the .25em under the text that the h2 uses for its underline, and drop
   the first tab's left padding so the title starts on the frame's own
   left edge. */
.tab-labels.tab-title{margin:0 0 .6em}
.tab-labels.tab-title .tab-label{font-size:1.3em;font-weight:bold;
  padding:0 1.2em .25em}
.tab-labels.tab-title .tab-label:first-of-type{padding-left:0}

/* Tabs rendered as LINKS (/admin, since 2026-08-27 -- CSS-radio tabs
   needed every course's panel in the DOM). Without this they inherit
   a{color:#196B24} and every tab is green, so the selected one is
   indistinguishable from the rest. Inactive is the same grey the
   radio-based tabs use; only the active one is green, bold and
   underlined. */
/* /admin's course tabs read as titles too, so they carry the same size
   and weight as /my's (.tab-title). They differ only in that they are
   links: colour and the underline mark the selected one. */
.tab-labels.tab-courses .tab-label{font-size:1.3em;font-weight:bold}
a.tab-label{text-decoration:none;color:#555}
a.tab-label:hover{color:#196B24}
.tab-label.tab-active{color:#196B24;border-bottom-color:#196B24;font-weight:bold}
/* A FRAME OWNS ITS OWN INNER EDGES. Whatever a card begins or ends
   with -- an <h2>, a <p> (16px), a <label> (9.6px), an <input> (3.2px) --
   contributes no margin of its own there, so every frame on the site
   starts and ends at exactly its 1em padding. Without this the gap under
   a frame's top border was whatever its first element happened to carry,
   which is how two frames on the same page came out 16px and 36px
   (measured, 2026-08-27). Applies to all 27 cards, not the two that were
   reported. */
.card > :first-child{margin-top:0}
.card > :last-child{margin-bottom:0}
.card > h2:first-child,.card-head h2{margin:0 0 .6em;font-size:1.3em;
  border-bottom:2px solid #196B24;padding-bottom:.25em;display:inline-block}
/* ONE gap between a frame's title and whatever it holds, wherever the
   title is an <h2> or a row of tabs: .6em, owned by the title, and the
   first thing under it contributes nothing of its own. Left to each
   child, the two frames on /my came out at 16px and 6px -- the title's
   .6em plus a course row's own .4em in one, a tab panel's padding plus
   an input's margin in the other. A gap belongs to the thing above it,
   or it is the sum of two decisions nobody made together. */
.card > h2:first-child + *,
.tab-panel > :first-child,
.tab-panel > .table-tools:first-child > :first-child{margin-top:0}
.tab-panel{padding-top:0}
/* A frame's title row when it also carries a link on the right. */
.card-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;
  gap:1em}
/* The chip inside a heading keeps its own box: no underline bleed from
   the h2 rule above, and the same tabular digits + fixed weekday width
   that make every chip on the site come out the same size. */
.card-head h2 .when{border-bottom:none;font-variant-numeric:tabular-nums}
.card-head h2 .wd{display:inline-block;min-width:2.4em}
/* A dialog carries its own look -- border, radius, padding, ground --
   instead of borrowing class="card" for it. A CLASS beats an ELEMENT
   selector at equal weight whatever the order, so .card{margin:1em 0}
   silently overrode margin:auto here and every card-classed dialog was
   pinned to the top of the viewport rather than centred. Nothing a
   dialog needs should be reachable by a class that can fight it. */
dialog{margin:auto;max-width:640px;width:92%;max-height:88vh;overflow:auto;
  background:#fff;border:1px solid #ddd;border-radius:8px;padding:1em}
/* NOT position:relative here. A modal <dialog> is centred by the browser
   itself with position:fixed + inset:0 + margin:auto; overriding
   `position` drops it out of that and it renders at the top of the flow
   instead -- which is exactly what happened (2026-08-27). The X inside
   does not need it either: a modal dialog is itself a positioned
   ancestor, so an absolutely-positioned child anchors to it. */
dialog::backdrop{background:rgba(0,0,0,.45)}
/* The X every overlay carries, in the same corner on every one of them.
   Added by _DIALOG_WIRING_SCRIPT for script-opened dialogs and written
   into the markup for the server-rendered booking overlay, which has no
   script at all -- one look, two ways of getting there. */
dialog .dialog-x{position:absolute;top:.35em;right:.5em;background:none;
  border:none;height:auto;font-size:1.6em;line-height:1;color:#666;cursor:pointer;padding:0 .25em;
  text-decoration:none}
dialog .dialog-x:hover{color:#b00020}
/* Room for it, so a heading cannot run under the X. */
dialog>h2:first-of-type,dialog>h3:first-of-type,dialog>p:first-of-type{padding-right:1.6em}
details summary{cursor:pointer;margin:.8em 0;color:#196B24}
button.danger{background:#b00020;color:#fff;border:1px solid #b00020;border-radius:6px}
.tab-labels{display:flex;border-bottom:1px solid #ddd;margin-top:.6em}
.tab-label{padding:.5em 1.2em;cursor:pointer;color:#555;border-bottom:2px solid transparent;margin-bottom:-1px}
#my-tab-login:checked ~ .tab-labels label[for="my-tab-login"],
#my-tab-signup:checked ~ .tab-labels label[for="my-tab-signup"],
#book-tab-login:checked ~ .tab-labels label[for="book-tab-login"],
#book-tab-signup:checked ~ .tab-labels label[for="book-tab-signup"]{color:#196B24;border-bottom-color:#196B24;font-weight:bold}
""")


def page(title: str, body: str, banner: str = "", head_extra: str = "",
         heading: str | None = None) -> str:
    """Every page in the app gets `_SUBMIT_FEEDBACK_SCRIPT` appended
    automatically (2026-07-11) -- see that constant's own docstring/comment
    above for why (submissions with no feedback, buttons stayed
    clickable during a slow one). No per-page opt-in needed or possible.

    `heading` overrides the visible <h1>; pass "" for a page that needs
    no heading at all. /admin is the case: its banner already says
    "Admin" in red, so a second "Admin overview" underneath it was one
    label too many for the same fact. `title` still names the browser
    tab either way -- a tab must always be identifiable.

    `head_extra` is raw markup for the <head> -- currently only the
    <meta http-equiv="refresh"> that carries someone back to /my after a
    booking made in the overlay there. Not escaped and not guest-
    reachable: every caller passes a literal built in this codebase.

    `banner` (2026-07-06, see app/webapp.py's _session_banner_html) is
    OPTIONAL, small, session-aware markup rendered above the page's own
    heading -- e.g. "Logged in as x@example.org - Logout" on /book and
    /courses when reached with an active guest session. Blank by default
    for every other page, unchanged from before this existed.

    .submit-row is flex+gap (2026-07-09: adjacent buttons were too close
    together) -- previously adjacent buttons/forms in the same row relied on plain
    inline whitespace for spacing, which visually collapsed them together
    (worst on /my's bottom row). One shared fix here covers every
    .submit-row in the app, not just /my's.

    .guests-section/.guest-row (2026-07-09: the booking form's "+ Add
    participant" rows needed each guest visibly grouped with its own
    remove link, and visibly separated from the main user's own fields
    and the "+ Add participant" link below) -- previously neither class had ANY CSS at
    all, so a guest row was just three bare, unboxed form fields blending
    into the main "Your email" field above and the "+ Add participant"
    link below. .guests-section now gets a bottom border + padding
    (2026-07-14: was a TOP border, flipped so the "+ Add participant"
    link sits ABOVE the separator line, with the acknowledge checkbox
    and Book button below it) to set the whole guest block apart;
    .guest-row boxes each individual guest's email+name+"Remove
    participant" together like a mini-card, so it reads as one group.
    See app/webapp.py's _book_page() guest-rows script, which is where
    .guest-row elements are actually created (client-side, one per "+ Add
    participant" click).

    body max-width is 1000px (2026-07-11: widen the app's table layout to
    match the STATIC homepage's own photo-backed content column, which
    was noticeably wider than the app pages -- applied to every
    application page, not just site/index.html's own table) -- matches site/index.html's own
    `div.WordSection1{{max-width:1000px}}`, the container the homepage's
    background photo fills, so every dynamic page (courses/book/my/admin)
    now lines up with that same width instead of its own narrower
    640px. `table{{width:100%}}` below is the other half of the same fix:
    a <table> with no width of its own only ever shrinks to its content
    width regardless of how wide the surrounding body is, so /my's
    bookings table and /admin's overview table wouldn't actually have
    gotten any wider just from the body change alone.

    Font sizes are harmonized app-wide (2026-07-11: nothing should be
    smaller than the button labels' own font-size -- button
    labels are `input,button,textarea{{font-size:1em}}` below, i.e. the
    same as ordinary body text). Every rule that was previously SMALLER
    than 1em (`.session-banner`, `.note`, `.hint`, `.date-btn .d-date`,
    `.date-btn .d-spots`, `.sort-indicator`, `.hash-cell` -- all were
    .8em-.95em) had its own font-size declaration dropped, so it now
    inherits the same ambient 1em as everything else, and gained
    `font-style:italic` instead so these
    still read visually as secondary/de-emphasized text without actually
    being smaller than a button label. Deliberately scoped to the app
    pages only, not site/index.html -- see that file's own top-of-file
    comment for why its content (a raw Word paste) isn't touched here.

    `.id-input` (2026-07-08: /admin/login's password field was stretched
    across the full-width 1000px body -- Name/Email/Password fields
    should not be that wide, just wide enough for really long passwords
    and emails; 50 characters was settled on as the sizing target) caps `.big-input`'s
    own `width:100%` at `max-width:50ch` -- `ch` scales with `.big-input`'s
    own font-size, so this is a character-count cap, not a fixed pixel
    width. Applied ALONGSIDE `big-input` (`class="big-input id-input"`),
    never replacing it, and ONLY on single-line Name/Email/Password
    `<input>` fields app-wide (every page that has one -- login, signup,
    admin login, /my settings, the booking form's own guest rows).
    Also applied to the `type="search"` table-filter boxes on /my and
    /admin (2026-07-08: capped to the same 50-char width -- overriding
    this docstring's earlier reasoning that they
    should stay full-width to visually pair with their table). Still
    deliberately NOT applied to `.big-input` textareas (the Cancel/
    Reinstate reason/message boxes -- free text benefits from the full
    width)."""
    shown = title if heading is None else heading
    heading_html = f"<h1>{esc(shown)}</h1>" if shown else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{head_extra}
<link rel="icon" type="image/png" href="/favicon/favicon-96x96.png" sizes="96x96">
<link rel="icon" type="image/svg+xml" href="/favicon/favicon.svg">
<link rel="shortcut icon" href="/favicon/favicon.ico">
<link rel="apple-touch-icon" sizes="180x180" href="/favicon/apple-touch-icon.png">
<link rel="manifest" href="/favicon/site.webmanifest">
<style>
{_CSS}</style></head><body>
{banner}
{heading_html}
{body}
<p class="version">{esc(short_version())}</p>
{_SUBMIT_FEEDBACK_SCRIPT}
</body></html>"""
