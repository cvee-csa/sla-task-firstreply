#!/usr/bin/env python3
"""
Local-only review/approve web server for Zendesk IT-Operations draft replies.

Runs as a persistent launchd service (see com.csa.zendesk-review-server.plist),
completely separate from the 2-minute first_reply_check.py cron job. That
script only ever detects candidates and writes drafts into pending_drafts.json
-- it never posts anything. This server is the ONLY thing that can make an
actual Zendesk API call that posts a comment, and it only does that when you
click "Public Reply" (visible to the requester) or "Internal Comment" (agent
-only, same as Zendesk's own private-note toggle) here yourself, optionally
after editing the text. There is no reject/dismiss action -- every candidate
stays on the page until you post it one way or the other. Nothing here runs
unattended or automatically; it just sits and waits for you to open the page
and decide.

SECURITY: binds to 127.0.0.1 only (see BIND_HOST below) -- unreachable from
any other machine on your network, let alone the internet. There is no login
on top of "you're on this Mac" -- that's intentional for a single-user local
tool, but it does mean you should never point a reverse proxy, ngrok, or port
forward at this.
"""

import base64
import gzip
import html
import json
import re
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

BIND_HOST = "127.0.0.1"  # do not change to 0.0.0.0 -- see module docstring

# How often the main ticket list quietly reloads itself so newly-detected
# tickets (added by the 2-minute cron scan running in the background) show up
# without you needing to hit the browser's reload button. This is purely a
# local re-render of whatever's already in pending_drafts.json -- it does
# NOT trigger a new Zendesk scan itself (that stays on the cron's own
# schedule, or the explicit "Refresh data" button).
AUTO_REFRESH_SECONDS = 60

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "csa-logo.png"
TOPIC_ICONS_DIR = Path(__file__).resolve().parent / "assets" / "topic-icons"


def _load_logo_data_uri() -> str:
    """Inlined as base64 rather than served as a separate file/route -- this
    server intentionally has no static-file handler, and a data: URI means
    the page stays a single self-contained response with no second request.
    Returns "" if the asset is missing so a page render never breaks just
    because the logo file didn't make it into this checkout."""
    try:
        data = LOGO_PATH.read_bytes()
    except Exception:
        return ""
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


LOGO_DATA_URI = _load_logo_data_uri()

# A second, alternate logo swapped in ONLY while the B68FBF theme is active
# (see .logo-default/.logo-b68fbf's CSS below) -- B68FBF started as a
# literal-hex-color easter egg theme, so its own easter-egg logo fits the
# same spirit rather than looking like an oversight. Loaded the same
# graceful-degradation way as LOGO_DATA_URI above: a missing file just means
# this second <img> never renders, not a broken page.
LOGO_B68FBF_PATH = Path(__file__).resolve().parent / "assets" / "b68fbf-logo.png"


def _load_logo_b68fbf_data_uri() -> str:
    try:
        data = LOGO_B68FBF_PATH.read_bytes()
    except Exception:
        return ""
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


LOGO_B68FBF_DATA_URI = _load_logo_b68fbf_data_uri()

# Topic icons -- pulled from the CSA Asset Repository's own icon library
# (the same Airtable base other CSA teams draw brand icons from), used as
# decorative accents next to page headings, inside empty-state boxes, and
# inside the priority pill. Deliberately a different tier from the small
# monochrome _ICONS/_icon() glyphs above: those exist purely to sit inline
# in button labels and stay legible in both themes via `currentColor` on a
# transparent 24x24 stroke path. These topic icons are self-contained
# colour badges instead -- a filled rounded-square background baked
# directly into the PNG, not `currentColor` -- so they read the same
# regardless of theme by default. That's why two different variants get
# used below: the "-csa-blue" badge (icon + its own blue background) for
# anything shown on its own against the page background (headers, empty
# states), and the plain "-white" glyph-only variant (no background,
# transparent) for the one spot something is placed on top of an
# already-coloured background -- the priority pill, which already has its
# own fill color via .badge-priority. Any file missing from this checkout
# just degrades to no icon there, same graceful-degradation pattern as
# LOGO_DATA_URI above.
#
# B68FBF is the one exception to "reads the same in every theme" -- see
# _TOPIC_ICON_FILES_B68FBF right below.
_TOPIC_ICON_FILES = {
    "tasks": "checklist-csa-blue.png",
    "security": "shield-checkmark-csa-blue.png",
    "flagged": "conversation-csa-blue.png",
    "reconnect": "key-csa-blue.png",
    "breach": "error-csa-blue.png",
    # Same checkmark badge as "solved" below -- own key rather than reusing
    # that one directly, since "a blocked/flagged issue cleared" and "a
    # ticket got solved" are different things that happen to share the same
    # glyph; keeping them as separate keys means either one's file can
    # change independently later without silently affecting the other page.
    "breach-clear": "checkmark-csa-blue.png",
    "solved": "checkmark-csa-blue.png",
    "priority-alert": "error-white.png",
    # Was already sitting in assets/topic-icons/ unused -- a stopwatch
    # reads naturally as "time-based tracking," which is exactly what the
    # /sla page's four business-hours thresholds are about, distinct from
    # "breach"/"breach-clear" above (a specific rule's own violation
    # state, not the page's general topic).
    "sla": "stopwatch-csa-blue.png",
}

# B68FBF-only recolor of the same badges (see recolor_topic_icons.py) -- each
# "-csa-blue" badge's baked-in background was hue-rotated to B68FBF's own hue
# (288.75 degrees) while preserving its original saturation/value, the same
# fixed-hue-varying-shade idea the theme's CSS custom properties already use
# for button colors, just applied per-pixel to a raster badge instead of to
# a handful of named CSS variables. "priority-alert" has no entry here on
# purpose: error-white.png has no colored badge background to recolor in the
# first place (it's a plain white glyph already sitting on .badge-priority's
# own themed fill), so it always falls back to rendering just its one
# default image -- see _topic_icon below.
_TOPIC_ICON_FILES_B68FBF = {
    "tasks": "checklist-b68fbf.png",
    "security": "shield-checkmark-b68fbf.png",
    "flagged": "conversation-b68fbf.png",
    "reconnect": "key-b68fbf.png",
    "breach": "error-b68fbf.png",
    "breach-clear": "checkmark-b68fbf.png",
    "solved": "checkmark-b68fbf.png",
    "sla": "stopwatch-b68fbf.png",
}


def _load_icon_data_uris(file_map: dict) -> dict:
    """Shared loader for both the default and the B68FBF-only topic-icon
    file maps above -- a file missing from either just means its key is
    absent from the returned dict, so a caller checking `.get(key)` sees the
    same graceful "nothing rendered" degradation LOGO_DATA_URI already
    established, whichever map it came from."""
    uris = {}
    for key, filename in file_map.items():
        try:
            data = (TOPIC_ICONS_DIR / filename).read_bytes()
        except Exception:
            continue
        uris[key] = f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
    return uris


TOPIC_ICON_DATA_URIS = _load_icon_data_uris(_TOPIC_ICON_FILES)
TOPIC_ICON_DATA_URIS_B68FBF = _load_icon_data_uris(_TOPIC_ICON_FILES_B68FBF)


def _topic_icon(key: str, css_class: str = "topic-icon", alt: str = "") -> str:
    """<img> for one of TOPIC_ICON_DATA_URIS, sized via CSS the same way
    _icon() is -- "" (nothing rendered, no broken-image icon) if that key's
    file didn't make it into this checkout. alt defaults to "" (purely
    decorative, redundant with the visible heading/label text right next
    to it) rather than describing the icon itself, so a screen reader
    doesn't announce e.g. "checklist" immediately before "Tasks".

    When a B68FBF-recolored variant exists for this key, BOTH the default
    and the B68FBF <img> are rendered (classes `topic-icon-default` /
    `topic-icon-b68fbf` alongside the caller's own sizing class), and a
    pure-CSS toggle picks the one matching the active theme -- the exact
    same two-`<img>`-tags-plus-CSS-toggle trick the header logo swap
    already established (see LOGO_B68FBF_DATA_URI/.logo-b68fbf), so this
    rides the same no-flash, no-extra-JS path. A key with no B68FBF variant
    (just "priority-alert" today) renders its single default image
    unmodified, in every theme -- never wrapped in a class that could hide
    it with nothing to fall back to."""
    uri = TOPIC_ICON_DATA_URIS.get(key, "")
    if not uri:
        return ""
    b68fbf_uri = TOPIC_ICON_DATA_URIS_B68FBF.get(key, "")
    if not b68fbf_uri:
        return f'<img src="{uri}" alt="{html.escape(alt)}" class="{css_class}">'
    return (
        f'<img src="{uri}" alt="{html.escape(alt)}" class="{css_class} topic-icon-default">'
        f'<img src="{b68fbf_uri}" alt="{html.escape(alt)}" class="{css_class} topic-icon-b68fbf">'
    )


def _log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


_URL_RE = re.compile(r'(https?://[^\s<>"()]+)')


def _linkify(escaped_text: str) -> str:
    """Turns any http(s) URL already present in the confirmation-banner
    text into a clickable link, opened in a new tab -- e.g. the "Confirmed:
    PUBLIC REPLY sent to #123 (https://.../tickets/123)" banner _post()
    returns after posting a reply. Deliberately operates on text that has
    ALREADY been through html.escape() (see _page(), below), not raw banner
    text -- this never becomes an HTML-injection path, since it only ever
    wraps substrings that survived escaping unchanged, which holds for the
    plain ticket URLs this app builds (ticket_url() in _common.py), the
    only kind of URL that ever appears in a banner here.

    The regex excludes "(" and ")" from the URL body -- these banners
    routinely wrap the URL in its own parentheses (see url_part in _post()
    et al.), and a greedy match would otherwise swallow that closing paren
    (and any trailing sentence punctuation right after it) into the href
    and the visible link text alike."""
    def _replace(match):
        url = match.group(1)
        trailing = ""
        while url and url[-1] in ".,;:!?":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f'<a href="{url}" target="_blank">{url}</a>{trailing}'

    return _URL_RE.sub(_replace, escaped_text)


def _js_str(s: str) -> str:
    """Escapes a string for safe embedding inside a single-quoted JS string
    literal within an onclick="...confirm('...')" HTML attribute. This
    file's other confirm() messages are fixed strings with any apostrophe
    manually escaped by hand (e.g. the Remove button's "won\\'t re-detect");
    the Claim button's confirm message includes a Zendesk assignee's name,
    which isn't guaranteed apostrophe-free (or quote-free at all), so that
    can't be done by hand here -- this covers it generically instead.

    Two layers of escaping happen here, in the order the browser actually
    undoes them: first JS-string-literal escaping (backslash and single
    quote, so the name can't break out of confirm('...')), THEN
    html.escape (so a literal double quote in the name can't break out of
    the surrounding onclick="..." HTML attribute itself -- the browser's
    HTML parser decodes attribute entities before that text is ever handed
    to the JS engine, so this order is what makes both layers land
    correctly). Only escaping backslash/quote and leaving a raw " to break
    the attribute was the actual bug here for a while: a Zendesk assignee
    display name containing a double quote could terminate the onclick
    attribute early, silently dropping the confirmation dialog (the claim
    would just go through with no "are you sure" prompt at all) rather
    than being a script-injection risk in this single-user local tool --
    still a real correctness bug, so it's covered now regardless."""
    return html.escape(s.replace("\\", "\\\\").replace("'", "\\'"))


# Small inline stroke-icon set (feather/lucide-style: viewBox 0 0 24 24,
# fill="none", currentColor stroke) -- picked so every icon inherits
# whatever text/border color its button or link already has, rather than
# needing its own color variant per theme. Kept as plain strings (not a
# <defs>/<use> sprite) since this server renders one self-contained page
# per response with no shared external asset -- a sprite would just be a
# second thing to keep in sync with the single-file rendering model
# everywhere else here. Verified by rendering a swatch page in the real
# browser before wiring these in, not just by eyeballing the path data.
_ICONS = {
    "send": '<line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>',
    "lock": '<rect x="3" y="11" width="18" height="10" rx="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path>',
    "trash": '<path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14"></path><path d="M10 11v6"></path><path d="M14 11v6"></path>',
    "refresh": '<path d="M23 4v6h-6"></path><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>',
    "user-check": '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="8.5" cy="7" r="4"></circle><polyline points="17 11 19 13 23 9"></polyline>',
    "key": '<circle cx="7.5" cy="15.5" r="4.5"></circle><path d="M11 12L20 3"></path><path d="M16 8l3 3"></path><path d="M13.5 10.5l2.5 2.5"></path>',
    "bar-chart": '<line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line>',
    "external-link": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line>',
    "check-square": '<polyline points="9 11 12 14 22 4"></polyline><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>',
    "flag": '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"></path><line x1="4" y1="22" x2="4" y2="15"></line>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>',
    "check-circle": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>',
    "chevron-up": '<polyline points="18 15 12 9 6 15"></polyline>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"></polyline>',
    # Added for the icon-only theme toggle (see _page()'s brand-right) --
    # "Auto" reads as a monitor (matches whatever the OS/browser prefers),
    # "Light"/"Dark" are the obvious sun/moon pair. The fourth option,
    # B68FBF, deliberately has no icon here -- it's a literal custom hex
    # color, not a concept, so it renders as an actual color swatch (see
    # .theme-swatch) instead of a line-icon standing in for one.
    "sun": '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line>',
    # Added for the filter-as-you-type boxes (see _filter_box_html) on the
    # main queue, /flagged, /security, /solved, and the "Recently worked
    # on" sidebar.
    "search": '<circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>',
    # Added for the /overview nav link -- a four-quadrant grid reads as
    # "several separate things pulled into one view", which is exactly
    # what that page is (see _render_overview_page).
    "grid": '<rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect>',
    # Added for the /firstreply nav link -- a reply arrow (curving back to
    # the sender) is the literal action that page's whole queue exists to
    # drive, distinct from Discussion's "flag" (marking something, not
    # answering it) or Tasks'/Security's own icons.
    "reply": '<polyline points="9 14 4 9 9 4"></polyline><path d="M20 20v-7a4 4 0 0 0-4-4H4"></path>',
    # Added for the /sla nav link -- a plain clock face reads as "time
    # thresholds," distinct from Overview's "grid" (several things pulled
    # into one view) and Tasks'/Security's own topic icons.
    "clock": '<circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline>',
}


def _icon(name: str, css_class: str = "icon") -> str:
    """Returns one of the swatch-verified icons above sized via the
    `.icon`/`.icon-sm` CSS classes rather than inline width/height, so a
    single stylesheet change can resize every icon on the page at once."""
    body = _ICONS.get(name, "")
    return f'<svg class="{css_class}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'


_AVATAR_HUES = ["#0e7490", "#6d4aa8", "#b4530e", "#1a56c4", "#7d5a0a", "#3f7a3f", "#a8386d"]
# A palette distinct from the button hues above (teal/violet/orange/blue/
# gold reused, plus two more) -- deliberately not tied 1:1 to the button
# palette's meaning, since an avatar's color is just a stable per-person
# identifier, not a signal about which action applies to them the way a
# button's hue is.
#
# _AVATAR_HUES's own index (not the hex value) is what actually reaches the
# page now -- see _avatar_html below -- specifically so a B68FBF-only
# variant of this palette (right here) can exist without _avatar_html itself
# needing to know which theme is active. Each entry was produced by taking
# the matching index above and rotating it to one of B68FBF's two
# established theme hues (288.75 degrees / its 9E8FBF complementary,
# alternating -- the same two-hue split the button palette's own B68FBF CSS
# block already uses) while preserving that color's original saturation/
# value, so the seven stay just as distinguishable from each other as the
# default palette is, not seven shades of one single hue.
_AVATAR_HUES_B68FBF = ["#780e90", "#674aa8", "#950eb4", "#4f1ac4", "#670a7d", "#513f7a", "#9338a8"]


def _avatar_hue_css() -> str:
    """Generates the `.avatar-hue-N` / `html.theme-b68fbf .avatar-hue-N`
    rules straight from _AVATAR_HUES/_AVATAR_HUES_B68FBF above, rather than
    a second, hand-typed copy of the same seven-plus-seven hex values living
    in the stylesheet -- exactly the "one predicate, not two copies that
    happen to currently agree" principle this project already applies
    elsewhere (see README), just for a CSS palette instead of a Python
    function. Changing either list automatically keeps the rendered page in
    sync with no separate edit to remember."""
    lines = []
    for i, hue in enumerate(_AVATAR_HUES):
        lines.append(f"  .avatar-hue-{i} {{ background: {hue}; }}")
    for i, hue in enumerate(_AVATAR_HUES_B68FBF):
        lines.append(f"  html.theme-b68fbf .avatar-hue-{i} {{ background: {hue}; }}")
    return "\n".join(lines)


AVATAR_HUE_CSS = _avatar_hue_css()


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _avatar_html(name: str) -> str:
    """A small colored initials bubble next to a resolved person's name --
    the color is deterministic per name (a simple char-sum hash into the
    fixed hue list above), so the same person gets the same color every
    time this renders, without tracking any color assignment anywhere.
    Only ever called with a name we're reasonably confident in (see
    call sites) -- an avatar next to a guessed/unconfirmed name would lend
    it a false sense of certainty.

    Renders the hue as a CSS class (`avatar-hue-N`), not an inline
    `style="background:..."`, specifically so B68FBF can override it --
    an inline style baked in at render time can't be swapped by the same
    pure-CSS `html.theme-b68fbf` trick every other theme-conditional swap on
    this page uses (the logo, the topic-icon badges), since there'd be
    nothing for a CSS selector to override. A CSS class has no such
    problem: the default hue lives in `.avatar-hue-N`, B68FBF's own hue
    lives in `html.theme-b68fbf .avatar-hue-N`, and the browser picks
    whichever applies at paint time with no server-side or JS theme
    detection needed."""
    if not name:
        return ""
    hue_index = sum(ord(c) for c in name) % len(_AVATAR_HUES)
    initials = html.escape(_initials(name))
    return f'<span class="avatar avatar-hue-{hue_index}" title="{html.escape(name)}">{initials}</span>'


def _short_date(iso: str) -> str:
    """'2026-08-12' -> 'Aug 12', matching the date-label style already used
    elsewhere on this page (see _render_worked_on_sidebar's clock-time format).
    Falls back to the raw string if it's not a plain ISO date."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d")
    except Exception:
        return iso


def _page(body: str, banner: str = "", banner_undo_path: str = "", auto_refresh: bool = False, header_meta: str = "", current_path: str = "/firstreply", wide: bool = False) -> bytes:
    """wide=True widens the content column from the default 1180px to
    1680px (see body.wide-page's CSS) -- opt in from the read-only card-
    grid pages (/security, /flagged, /solved, /tasks, /overview) whose
    rows are already responsive and were capping out well short of what
    an ordinary wide monitor could actually show. Left False (the
    default) everywhere else, including the main queue itself, whose
    ticket cards hold a reply-drafting textarea that reads better at the
    narrower width.

    header_meta lets the main page's "N pending / last checked / next
    check" line ride inside the SAME sticky header band as the logo/title
    (see .brand's CSS) instead of a second, separately-bordered block
    underneath -- one merged header, not two stacked ones. Defaults to ""
    for every other page (reconnect, confirm, 404), which just renders the
    logo/title with nothing extra, exactly as before this existed. (The
    Refresh button used to have its own header_action param following the
    same pattern -- it's since moved into .brand-status-row below, which
    renders unconditionally on every page, so there's no longer a
    per-page param needed for it.)

    The header itself is four stacked pieces, each thinner than the old
    single wide row-1 was: the logo/title (.brand-home-link) sits alone as
    a plain block-level first child, so it sizes to its own content and
    stays flush against .brand's left edge -- a compact badge anchored in
    the top-left corner, not a banner stretched across the full width. The
    theme toggle gets its own .brand-controls-row directly beneath it
    (right-aligned, echoing where it used to sit on the old row 1), the
    nav links keep their own .brand-nav-row below that, and the Refresh
    button (_refresh_button_html) plus the Zendesk OAuth status control
    (_zendesk_status_control_html) sit together last in their own
    .brand-status-row -- on wide viewports this is pinned to the very
    bottom of the sidebar (see that CSS rule's own comment for the flex
    trick behind it) rather than just trailing the nav list.

    current_path drives two small navigation aids that now render on
    EVERY page (not just /firstreply), since it was easy to lose track of
    overall status while working on /flagged or /tasks specifically:
    _top_nav_html highlights whichever of its six links matches
    current_path, and the logo/title itself becomes a link back to
    /firstreply carrying a "N pending" badge (see pending_badge_html
    below) so the main queue's count is always visible, not just when
    you're actually looking at it. Defaults to "/firstreply" -- the
    handful of callers that don't pass anything explicit (the confirm
    interstitial, 404s) are effectively reached from/related to the main
    queue anyway, so that's the reasonable default rather than requiring
    every call site to specify it. (The main queue's own real URL used to
    just be "/" -- bare "/" is now a redirect to /firstreply, kept alive
    for old bookmarks/muscle memory rather than removed outright, see
    do_GET.)

    banner_undo_path is an optional extra: when a caller's confirmation
    message describes a reversible action (right now, just a dismissed
    ticket -- see Handler._dismiss/_undo_dismiss), this is a relative POST
    path (e.g. "/undo_dismiss/156391") rendered as a small inline "Undo"
    button right inside the same toast, rather than making the person dig
    back through Zendesk to manually reverse a misclick. Defaults to "",
    which renders the toast exactly as before this existed."""
    pending_count = _common.summary_counts().get("pending", 0)
    pending_badge_html = f'<span class="stat-chip">{pending_count} pending</span>' if pending_count else ""
    # An inline form, not a plain link -- undoing is a mutation (same as
    # every other action in this app), so it goes through POST like
    # dismiss/claim/post_public do, not a GET link that could be triggered
    # by prefetching or an accidental middle-click-open-in-new-tab.
    banner_undo_html = (
        f'<form method="post" action="{html.escape(banner_undo_path)}" class="banner-undo">'
        f'<button type="submit" data-loading-text="Undoing…">Undo</button></form>'
        if banner_undo_path else ""
    )
    # data-toast marks this specific banner for the auto-dismiss behavior in
    # toast_script (below) -- distinct from the auth-warning banner (see
    # _auth_warning_html), which reuses the same .banner classes/colors but
    # is a standing condition, not a one-off event, so it stays put and
    # docked in normal flow (see .auth-warning's CSS override) rather than
    # floating as a toast that disappears on its own. data-toast-duration
    # gives an undoable toast longer on screen than a plain confirmation --
    # toast_script (below) reads it, falling back to its own default when
    # absent -- since noticing "Undo" is even there takes a beat longer
    # than just reading a one-line confirmation.
    duration_attr = ' data-toast-duration="12000"' if banner_undo_path else ""
    banner_html = (
        f'<div class="banner" role="status" aria-live="polite" data-toast="true"{duration_attr}>'
        f'{_linkify(html.escape(banner))}{banner_undo_html}</div>'
        if banner else ""
    )
    # Two <img> tags, not one whose src gets swapped by JS -- theme_init_script
    # (see below) already applies html.theme-b68fbf as early as possible via a
    # plain class, and a pure-CSS show/hide (.logo-default/.logo-b68fbf) keeps
    # the swap in that same no-flash-of-wrong-logo path rather than adding a
    # second, JS-driven mechanism that could race with it.
    logo_html = f'<img src="{LOGO_DATA_URI}" alt="Cloud Security Alliance" class="logo logo-default">' if LOGO_DATA_URI else ""
    if LOGO_B68FBF_DATA_URI:
        logo_html += f'<img src="{LOGO_B68FBF_DATA_URI}" alt="Cloud Security Alliance" class="logo logo-b68fbf">'
    # id + data-href-b68fbf let both inline scripts below swap the favicon
    # for B68FBF without embedding LOGO_B68FBF_DATA_URI a second time just
    # for JS to read back -- the tag itself carries the one extra copy
    # already needed (same duplication factor the default logo already has,
    # split between this href and the header <img>'s own src), and each
    # script lazily caches this tag's *original* href into data-href-default
    # itself the first time it runs, rather than the server embedding that
    # (already-duplicated-once) string a third time just to save JS one property read.
    favicon_html = (
        f'<link rel="icon" id="favicon" type="image/png" href="{LOGO_DATA_URI}"'
        f' data-href-b68fbf="{LOGO_B68FBF_DATA_URI}">'
        if LOGO_DATA_URI else ""
    )
    # A single JS-maintained theme-color meta rather than static per-scheme
    # tags -- covers all four modes (system/light/dark/b68fbf) including the
    # OS-preference case (checked live via matchMedia) with one code path in
    # each of the two inline scripts below, instead of duplicating a set of
    # `media="(prefers-color-scheme: ...)"` tags that would still need a
    # manual override for the two explicit non-system modes anyway.
    theme_color_meta_html = '<meta name="theme-color" id="theme-color-meta" content="#1a56c4">'
    # Applies a manually-forced theme (if one was ever picked via the Auto /
    # Light / Dark control below) to <html> as early as possible -- before
    # the stylesheet paints anything -- so a forced choice never flashes the
    # OS-default theme first. Reads localStorage directly rather than
    # anything server-side: the preference is a per-browser UI setting, not
    # ticket data, so there's nothing here worth persisting to
    # pending_drafts.json or threading through every page handler.
    theme_init_script = """<script>
(function() {
  try {
    var t = localStorage.getItem('csa-theme');
    if (t === 'light' || t === 'dark' || t === 'b68fbf') {
      document.documentElement.classList.add('theme-' + t);
      document.documentElement.style.colorScheme = t === 'dark' ? 'dark' : 'light';
    }
    var favicon = document.getElementById('favicon');
    if (favicon) {
      if (!favicon.dataset.hrefDefault) { favicon.dataset.hrefDefault = favicon.href; }
      favicon.href = (t === 'b68fbf' && favicon.dataset.hrefB68fbf) ? favicon.dataset.hrefB68fbf : favicon.dataset.hrefDefault;
    }
    var themeColorMeta = document.getElementById('theme-color-meta');
    if (themeColorMeta) {
      var isDark = t === 'dark' || (!t && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
      themeColorMeta.content = t === 'b68fbf' ? '#865392' : (isDark ? '#6fa3ef' : '#1a56c4');
    }
  } catch (e) {}
})();
</script>"""
    # Skips the reload while a textarea has an unsaved edit in it (tracked via
    # a plain 'input' listener), so auto-refresh can't silently blow away
    # something you're in the middle of writing. Also skips it while
    # scrolled away from the top -- being scrolled down usually means
    # reading something (an excerpt, an older ticket further down the
    # queue), and having the page yanked out from under you mid-read is its
    # own kind of interruption, even with nothing left half-typed. Reloads
    # to the bare path (dropping any ?msg=... query string) rather than the
    # exact current URL, so a flash banner from your last action doesn't
    # reappear on every tick.
    refresh_script = (
        f"""
<script>
(function() {{
  var dirty = false;
  document.addEventListener('input', function(e) {{
    if (e.target && e.target.tagName === 'TEXTAREA') {{ dirty = true; }}
  }});
  setInterval(function() {{
    if (!dirty && window.scrollY === 0) {{ window.location.href = window.location.pathname; }}
  }}, {AUTO_REFRESH_SECONDS * 1000});
}})();
</script>"""
        if auto_refresh
        else ""
    )
    # Ticks the "Last checked" / "Next automatic check" status-bar spans
    # every second so they read as a live clock instead of a string frozen
    # at page-load time. Harmless no-op on pages without these ids (the
    # reconnect page, 404s) -- getElementById just returns null and the tick
    # skips that span. The 60s auto-refresh above will eventually replace
    # these with a fresh server render anyway; this just fills the gap
    # between reloads (or entirely, if auto_refresh is off) with something
    # that keeps moving. Safe from timezone bugs: this page is only ever
    # opened on the same machine that renders it (127.0.0.1-only), so the
    # server's naive local-clock ISO strings and the browser's local Date()
    # parsing agree on what "now" means.
    tick_script = """
<script>
(function() {
  function formatDuration(totalSeconds) {
    totalSeconds = Math.max(0, Math.round(totalSeconds));
    var h = Math.floor(totalSeconds / 3600);
    var m = Math.floor((totalSeconds % 3600) / 60);
    var s = totalSeconds % 60;
    var parts = [];
    if (h > 0) { parts.push(h + 'h'); }
    if (h > 0 || m > 0) { parts.push(m + 'm'); }
    parts.push(s + 's');
    return parts.join(' ');
  }

  // Mirrors _common.next_scan_description()'s own "%a %-I:%M %p" formatting
  // (e.g. "Mon 9:00 AM") -- used below so the live countdown can keep
  // saying WHY a "precise" target is far away (paused for the weekend/
  // after-hours) instead of silently dropping that context the moment this
  // script's first tick overwrites the server-rendered fallback text.
  function formatClockLabel(date) {
    var days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    var h = date.getHours();
    var ampm = h >= 12 ? 'PM' : 'AM';
    var h12 = h % 12;
    if (h12 === 0) { h12 = 12; }
    var m = date.getMinutes();
    var mStr = (m < 10 ? '0' : '') + m;
    return days[date.getDay()] + ' ' + h12 + ':' + mStr + ' ' + ampm;
  }

  function tick() {
    var now = Date.now();

    var lastEl = document.getElementById('last-checked');
    if (lastEl && lastEl.dataset.iso) {
      var lastMs = new Date(lastEl.dataset.iso).getTime();
      var agoSeconds = (now - lastMs) / 1000;
      lastEl.textContent = agoSeconds < 1 ? 'just now' : formatDuration(agoSeconds) + ' ago';
    }

    var nextEl = document.getElementById('next-check');
    if (nextEl && nextEl.dataset.iso) {
      var targetDate = new Date(nextEl.dataset.iso);
      var remainingSeconds = (targetDate.getTime() - now) / 1000;
      if (remainingSeconds <= 0) {
        nextEl.textContent = 'any moment now';
      } else if (nextEl.dataset.precise === '1') {
        // "precise" means target_iso is a fixed business-hours boundary
        // (the automatic scan is paused entirely outside Mon-Fri 9am-5pm),
        // not a rolling ~2-minute estimate -- when that boundary is a day
        // or two out (e.g. Friday evening, or any time over a weekend), a
        // bare "in 49h 0m 58s" reads like a stuck or broken timer with no
        // explanation. Keeping the actual resume time alongside the
        // countdown (same wording the static fallback text already used
        // before this script's first tick ran) makes clear this is an
        // intentional pause, not a malfunction.
        nextEl.textContent = 'resumes ' + formatClockLabel(targetDate) + ' · in ' + formatDuration(remainingSeconds);
      } else {
        nextEl.textContent = '~in ' + formatDuration(remainingSeconds);
      }
    }
  }

  tick();
  setInterval(tick, 1000);
})();
</script>"""
    # Defines window.setTheme (called by the Auto/Light/Dark buttons' onclick)
    # and, on load, re-applies whatever theme_init_script already set so the
    # matching button gets its "active" state -- separate from
    # theme_init_script above because the buttons themselves don't exist yet
    # that early in <head>; this runs at the bottom of body once they do.
    theme_script = """
<script>
(function() {
  function applyTheme(mode) {
    document.documentElement.classList.remove('theme-light', 'theme-dark', 'theme-b68fbf');
    if (mode === 'light' || mode === 'dark' || mode === 'b68fbf') {
      document.documentElement.classList.add('theme-' + mode);
      document.documentElement.style.colorScheme = mode === 'dark' ? 'dark' : 'light';
    } else {
      document.documentElement.style.colorScheme = '';
    }
    document.querySelectorAll('.theme-toggle button').forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.theme === mode);
    });
    var favicon = document.getElementById('favicon');
    if (favicon) {
      if (!favicon.dataset.hrefDefault) { favicon.dataset.hrefDefault = favicon.href; }
      favicon.href = (mode === 'b68fbf' && favicon.dataset.hrefB68fbf) ? favicon.dataset.hrefB68fbf : favicon.dataset.hrefDefault;
    }
    var themeColorMeta = document.getElementById('theme-color-meta');
    if (themeColorMeta) {
      var isDark = mode === 'dark' || (mode === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
      themeColorMeta.content = mode === 'b68fbf' ? '#865392' : (isDark ? '#6fa3ef' : '#1a56c4');
    }
  }
  window.setTheme = function(mode) {
    try {
      if (mode === 'system') { localStorage.removeItem('csa-theme'); }
      else { localStorage.setItem('csa-theme', mode); }
    } catch (e) {}
    applyTheme(mode);
  };
  var stored = null;
  try { stored = localStorage.getItem('csa-theme'); } catch (e) {}
  applyTheme(stored === 'light' || stored === 'dark' || stored === 'b68fbf' ? stored : 'system');
})();
</script>"""
    # Three small, independent behaviors bundled into one script rather than
    # three separate <script> tags, since none of them are large enough to
    # justify their own:
    #   1. Disables + relabels whichever button actually triggered a form
    #      submission (via the SubmitEvent's `submitter`, not just the
    #      form's first button -- a ticket card has three buttons with
    #      different formactions in ONE form) so clicking Public Reply /
    #      Internal Comment / Remove / Refresh gives immediate feedback
    #      instead of looking like nothing happened during the round trip
    #      to Zendesk. Naturally resets itself on the next page load/
    #      redirect -- nothing to undo if the request fails and re-renders
    #      the same form. Skipped entirely if the submit was cancelled (e.g.
    #      the Remove confirm() dialog returning false), since the 'submit'
    #      event only fires once a submission actually proceeds.
    #   2. Cmd/Ctrl+Enter while focused in a draft textarea submits its form
    #      as a Public Reply (the form's own default action) without
    #      reaching for the mouse -- opt-in via data-shortcut-submit so it
    #      only ever applies to the actual draft textarea, not any future
    #      textarea that shouldn't have this behavior.
    #   3. Remembers whether each collapsible <details> section ("Replied
    #      today", "Last N replies") was left expanded, the same
    #      localStorage-per-browser approach as the theme toggle -- no
    #      server-side state, since this is a UI preference, not ticket
    #      data. Generalized over every details[data-storage-key] on the
    #      page (querySelectorAll, not a single querySelector) so adding a
    #      second collapsible section doesn't silently leave it without
    #      persistence the way hardcoding to one class name would.
    #   4. Optimistic fade: the instant a ticket/mini-ticket's own form is
    #      submitted (not the header Refresh button, not the reconnect
    #      forms -- closest('.ticket, .mini-ticket') simply finds nothing
    #      for those), that card immediately fades and shrinks slightly via
    #      the .is-removing CSS transition, rather than staying static
    #      until the full page reload lands. This is a purely cosmetic
    #      cover for the round trip -- the actual removal still only
    #      happens because the server re-renders the page without that
    #      ticket, exactly as before; nothing here is a real optimistic-
    #      update / local-state-patch the way a full AJAX rewrite would do.
    #      Skipped the same way the disable/relabel step is if a confirm()
    #      dialog cancels the submission first.
    # A raw string (r""") because the auto-fill-onto-subject listener below
    # embeds a literal JS regex (/^\[[^\]]*\]\s*/) -- as a plain (non-raw)
    # string, Python's parser doesn't recognize \[ as one of its own escape
    # sequences and, while it still passes the backslash through unchanged
    # today, it also raises a DeprecationWarning for it (visible running the
    # test suite under `pytest -W error` or a future Python version that
    # promotes this to a real SyntaxError, per Python's own documented
    # deprecation path for invalid escape sequences). Safe here specifically
    # because this string has no genuine Python escapes to lose -- no \n,
    # \t, or \\ anywhere in it that need real backslash processing, and it
    # isn't an f-string, so there's no {...} interpolation to worry about
    # colliding with -- confirmed by compiling this file with all warnings
    # enabled (not just "first occurrence") and finding exactly this one
    # spot flagged.
    interaction_script = r"""
<script>
(function() {
  // Warns before leaving the page (closing the tab, following a link,
  // typing a new URL -- anything that isn't submitting the very form
  // you've been editing) while a textarea has an unsaved edit sitting in
  // it. A hand-edited draft only ever lives in the browser until it's
  // actually posted -- there's no autosave, so navigating away by
  // accident is a genuine way to lose real work, not just an
  // inconvenience. `submitting` is what keeps this from misfiring on the
  // one kind of "leaving" that's actually fine: submitting has to come
  // before the (already-present) submit listener below can flip it, so
  // it's declared up here rather than down there.
  var draftDirty = false;
  var submitting = false;
  document.addEventListener('input', function(e) {
    if (e.target && e.target.tagName === 'TEXTAREA') { draftDirty = true; }
  });
  window.addEventListener('beforeunload', function(e) {
    if (!draftDirty || submitting) { return; }
    // Every modern browser shows its own fixed wording here and ignores
    // whatever string this is set to -- e.returnValue just has to be a
    // non-empty value to trigger that native prompt at all.
    e.preventDefault();
    e.returnValue = '';
  });

  document.addEventListener('submit', function(e) {
    submitting = true;
    var submitter = e.submitter;
    if (!submitter || submitter.tagName !== 'BUTTON') { return; }
    var loadingText = submitter.dataset.loadingText || (submitter.textContent.trim() + '…');
    submitter.disabled = true;
    submitter.textContent = loadingText;
    var card = submitter.closest('.ticket, .mini-ticket');
    if (card) { card.classList.add('is-removing'); }
  });

  document.addEventListener('keydown', function(e) {
    if (!(e.metaKey || e.ctrlKey) || e.key !== 'Enter') { return; }
    var el = document.activeElement;
    if (!el || el.tagName !== 'TEXTAREA' || el.dataset.shortcutSubmit !== 'true') { return; }
    var form = el.closest('form');
    if (!form) { return; }
    if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
  });

  // Auto-fills the Tasks "What needs doing?" subject box with whichever
  // category tag was just picked (data-auto-subject marks the <select>,
  // see _category_select_html) -- prepended as a "[Tag] " leading tag,
  // replacing any bracketed tag already there rather than stacking a
  // second one, and stripped out entirely if "No category" is chosen.
  // Delegated the same way the details/toast handling above is, so it
  // covers both the "Add task" form and every draft's own inline Edit
  // form without needing per-row ids.
  document.addEventListener('change', function(e) {
    var select = e.target;
    if (!select || select.tagName !== 'SELECT' || !select.hasAttribute('data-auto-subject')) { return; }
    var form = select.closest('form');
    var subjectInput = form ? form.querySelector('input[name="subject"]') : null;
    if (!subjectInput) { return; }
    var stripped = subjectInput.value.replace(/^\[[^\]]*\]\s*/, '');
    subjectInput.value = select.value ? (select.value + ' ' + stripped) : stripped;
  });

  document.querySelectorAll('details[data-storage-key]').forEach(function(details) {
    var key = details.dataset.storageKey;
    try {
      if (localStorage.getItem(key) === '1') { details.open = true; }
    } catch (e) {}
    details.addEventListener('toggle', function() {
      try { localStorage.setItem(key, details.open ? '1' : '0'); } catch (e) {}
    });
  });

  // Auto-dismisses the confirmation toast (see .banner's data-toast
  // attribute) after a few seconds, rather than leaving it parked in the
  // corner until the next full page reload clears it. The auth-warning
  // banner doesn't carry data-toast, so it's untouched here -- that one's
  // a standing condition, not a one-off event.
  var toast = document.querySelector('[data-toast="true"]');
  if (toast) {
    var toastDuration = parseInt(toast.dataset.toastDuration || '6000', 10);
    setTimeout(function() {
      toast.classList.add('toast-leaving');
      toast.addEventListener('animationend', function() { toast.remove(); }, {once: true});
    }, toastDuration);
  }

  // Filter-as-you-type (see _filter_box_html): each .filter-box names, via
  // data-filter-target, the CSS selector of the cards it should narrow
  // down. Purely a client-side textContent substring match against
  // whatever's currently in the DOM -- nothing here re-fetches or
  // re-renders, it just toggles display on the elements that don't match
  // and reports "N of M shown" next to the box. At most one of these
  // exists per page today, but this handles any number the same way,
  // each independent of the others.
  document.querySelectorAll('.filter-box[data-filter-target]').forEach(function(box) {
    var input = box.querySelector('.filter-input');
    var countEl = box.querySelector('.filter-count');
    var target = box.dataset.filterTarget;
    if (!input || !target) { return; }
    var apply = function() {
      var query = input.value.trim().toLowerCase();
      var items = document.querySelectorAll(target);
      var shown = 0;
      items.forEach(function(el) {
        var matches = !query || el.textContent.toLowerCase().indexOf(query) !== -1;
        el.style.display = matches ? '' : 'none';
        if (matches) { shown++; }
      });
      if (countEl) {
        countEl.textContent = query ? (shown + ' of ' + items.length + ' shown') : '';
      }
    };
    input.addEventListener('input', apply);
  });
})();
</script>"""
    # #page-loading-bar (see its CSS) -- there's no client-side router here,
    # every navigation is a real full-page reload, so there's no genuine
    # progress percentage to report. This shows the same honest-fake
    # indicator sites with a real router use for a slow request instead: an
    # immediate small jump (feels instant, not stuck) followed by a slow
    # crawl toward -- but deliberately never reaching -- 100%, via a long
    # CSS transition. Whichever finishes first, the real navigation or this
    # animation, doesn't matter: the entire DOM (bar included) gets torn
    # down the moment the browser actually navigates away, which is what
    # ends the "loading" state, not anything tracked in here.
    #
    # Fires on both plain link clicks and form submits (every action on this
    # page is one or the other) -- skipped for same-page "#" jump links,
    # mailto:/tel: links, anything opening in a new tab/window
    # (target != _self), and a click modified with a modifier key (which
    # the browser itself would open in a new tab). A submit already
    # cancelled upstream (the Remove/Discard/reassign-Claim confirm()
    # dialogs return false straight from the button's own onclick, which
    # pre-empts the browser ever dispatching 'submit' at all -- see
    # interaction_script above) never reaches this listener in the first
    # place, so a cancelled confirmation correctly never shows the bar.
    loading_bar_script = """
<script>
(function() {
  var bar = document.getElementById('page-loading-bar');
  if (!bar) { return; }

  function start() {
    bar.style.transition = 'width 0.25s ease-out, opacity 0.15s ease';
    bar.classList.add('active');
    bar.style.width = '20%';
    setTimeout(function() {
      bar.style.transition = 'width 5s cubic-bezier(0.1, 0.6, 0.2, 1), opacity 0.15s ease';
      bar.style.width = '85%';
    }, 260);
  }

  document.addEventListener('click', function(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    var link = e.target.closest ? e.target.closest('a[href]') : null;
    if (!link) { return; }
    var href = link.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || href.indexOf('mailto:') === 0 || href.indexOf('tel:') === 0) { return; }
    if (link.target && link.target !== '_self') { return; }
    start();
  });

  document.addEventListener('submit', function(e) {
    if (e.defaultPrevented) { return; }
    var form = e.target;
    if (form.target && form.target !== '_self') { return; }
    start();
  });

  // A page restored from the back/forward cache never re-runs the setup
  // above, but the bar itself could still be left visible from whatever
  // was in flight when the user navigated away from it -- reset it on
  // pageshow so a back/forward navigation never lands on a page with a
  // stuck bar.
  window.addEventListener('pageshow', function() {
    bar.classList.remove('active');
    bar.style.width = '0%';
  });
})();
</script>"""
    # Animates each [data-count-target] span's own text from 0 up to its
    # real value on load -- right now that's just the "Last week ... N
    # replies posted" figure on /overview (see _activity_line_html), which
    # isn't on an auto-refreshing page, so it only ever plays once per
    # visit rather than re-triggering itself every 60s the way it would if
    # this were also applied to something on /firstreply. Explicitly
    # skipped under prefers-reduced-motion -- CSS's own blanket
    # animation-duration override (see the stylesheet) has no power over a
    # plain JS setInterval loop like this one, so this needs its own check
    # rather than relying on that to already cover it; the real number is
    # already sitting in the span's text content regardless (see
    # _activity_line_html's own comment), so skipping the animation still
    # leaves a fully correct, resolved page.
    count_up_script = """
<script>
(function() {
  var spans = document.querySelectorAll('[data-count-target]');
  if (!spans.length) { return; }
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion) { return; }

  spans.forEach(function(span) {
    var target = parseInt(span.dataset.countTarget, 10);
    if (!isFinite(target) || target <= 0) { return; }
    var duration = 600;
    var start = null;
    span.textContent = '0';
    function step(timestamp) {
      if (start === null) { start = timestamp; }
      var progress = Math.min((timestamp - start) / duration, 1);
      span.textContent = String(Math.round(progress * target));
      if (progress < 1) { window.requestAnimationFrame(step); }
    }
    window.requestAnimationFrame(step);
  });
})();
</script>"""
    # A "(N)" prefix on the tab title so a background tab shows the pending
    # count at a glance, the same reason pending_badge_html exists on the
    # logo/title itself -- this is the one place that's visible without
    # even switching to the tab. Uses the same pending_count already
    # computed above for the header badge, so there's no second source of
    # truth to keep in sync.
    title_prefix = f"({pending_count}) " if pending_count else ""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title_prefix}Zendesk Workflow Asset for CSA</title>
{favicon_html}
{theme_color_meta_html}
{theme_init_script}
<style>
  /* Light theme (default). Every color used below is a variable specifically
     so the dark-mode block right after this one only has to override
     values, never re-derive rules -- one palette, two settings, not two
     independent stylesheets that could quietly drift apart. */
  :root {{
    color-scheme: light dark; /* lets native form-control chrome (e.g. the
      textarea's resize handle, default focus rings on some platforms) match
      whichever theme is active below, without any extra rules. */
    --bg: #ffffff;
    --bg-translucent: rgba(255,255,255,0.82);
    /* Glass-header-only tokens (see .brand's @supports block below): a soft
       translucent edge instead of a hard line, a thin top "shine" seam, a
       drop shadow that reads as a floating panel, and a light tint for the
       small pill controls (theme toggle, top-nav links) that sit on it --
       kept as their own variables, not reused from --divider/--card-border/
       --card-shadow, since those are tuned for opaque cards elsewhere on
       the page and would look wrong (too dark/too solid) against a blurred
       background. Duplicated across all four theme blocks in this file,
       same as every other palette value here. */
    --glass-border: rgba(15,23,42,0.08);
    --glass-shine: rgba(255,255,255,0.55);
    --glass-shadow: rgba(15,23,42,0.10);
    --glass-chip-bg: rgba(255,255,255,0.5);
    --text: #1a1a1a;
    --muted: #666666;
    --muted-2: #9a9a9a;
    --divider: #e5e5e5;
    --card-bg: #ffffff;
    --card-border: #e2e2e5;
    --card-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
    --input-bg: #ffffff;
    --input-border: #cccccc;
    --excerpt-bg: #f7f7f7;
    --excerpt-border: #cccccc;
    --link: #1a56c4;
    --accent: #1a56c4;
    --focus-ring: rgba(26,86,196,0.35);
    /* Button palette -- six distinct hues, deliberately none of them red or
       green, so "primary/positive" and "destructive/caution" are conveyed
       by which hue it is rather than a red/green traffic-light convention. */
    --btn-approve: #0e7490;       --btn-approve-hover: #0b5c72;   /* teal */
    --btn-internal: #6d4aa8;      --btn-internal-hover: #59397f;  /* violet */
    --btn-remove: #64748b;        --btn-remove-hover: #52606d;    /* slate */
    --btn-reject: #c2622a;        --btn-reject-hover: #a34f1f;    /* amber/orange */
    --btn-refresh: #1a56c4;       --btn-refresh-hover: #15408f;   /* blue */
    --btn-claim: #7d5a0a;         --btn-claim-hover: #664a08;     /* gold */
    --banner-bg: #eaf3fb;
    --banner-border: #a9cdf0;
    --warning-bg: #fdf3e8;
    --warning-border: #eac48a;
    --flag-color: #b45309;        /* name-confidence warnings -- caution tier */
    --flag-alert-color: #9a3412;  /* already-replied warning -- higher tier */
    /* HIGH and URGENT priority badges (.badge-priority, see
       _priority_badge_html) used to both render as --flag-alert-color --
       visually identical apart from the text label, which defeats the
       point of color-coding severity at a glance. This is a third,
       distinct tier: a deeper, more saturated red than --flag-alert-color
       rather than a wholly different hue, so URGENT still reads as "the
       same alert family, more of it" rather than an unrelated color. */
    --flag-alert-color-urgent: #b91c1c;
    --badge-bg: #666666;
    /* /overview's per-section left-edge accent stripe (see .overview-
       section-tasks/-discussion/-security's CSS) -- a third, purely
       functional color family alongside --accent (brand blue, used
       everywhere for buttons/links) and --flag-alert-color (already means
       "warning/priority" elsewhere on this page); dedicated tokens rather
       than reusing an existing --btn-* hue so a theme that collapses
       several button hues into one family (see html.theme-b68fbf below)
       can't accidentally make two of these three sections look the same
       color. */
    --overview-accent-tasks: #0e7490;
    --overview-accent-discussion: #6d4aa8;
    --overview-accent-security: #8a6d1a;
    /* /sla's "on track" state -- the third leg of a three-state signal
       alongside --flag-alert-color-urgent (breached, already red) and
       --flag-color/--warning-bg (due soon, already amber); neither of
       those two was a green, so this is the one genuinely new color this
       page needs rather than reusing what's already here. */
    --sla-ok-color: #1a6e2e;
    --sla-ok-bg: #e6f5ec;
    --sla-ok-border: #90d0a0;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #16171a;
      --bg-translucent: rgba(22,23,26,0.78);
      --glass-border: rgba(255,255,255,0.14);
      --glass-shine: rgba(255,255,255,0.09);
      --glass-shadow: rgba(0,0,0,0.45);
      --glass-chip-bg: rgba(255,255,255,0.06);
      --text: #e7e7ea;
      --muted: #9a9aa2;
      --muted-2: #77777f;
      --divider: rgba(255,255,255,0.09);
      --card-bg: #202226;
      --card-border: rgba(255,255,255,0.08);
      --card-shadow: 0 1px 3px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.35);
      --input-bg: #26272b;
      --input-border: rgba(255,255,255,0.18);
      --excerpt-bg: #232427;
      --excerpt-border: rgba(255,255,255,0.14);
      --link: #6fa3ef;
      --accent: #6fa3ef;
      --focus-ring: rgba(111,163,239,0.45);
      --btn-approve: #158a96;     --btn-approve-hover: #106e78;
      --btn-internal: #7c5cc4;    --btn-internal-hover: #6749a8;
      --btn-remove: #6b7684;      --btn-remove-hover: #57616d;
      --btn-reject: #c96a34;      --btn-reject-hover: #a8551f;
      --btn-refresh: #2f6fd1;     --btn-refresh-hover: #2559ab;
      --btn-claim: #9c7018;       --btn-claim-hover: #86600f;
      --banner-bg: rgba(111,163,239,0.12);
      --banner-border: rgba(111,163,239,0.35);
      --warning-bg: rgba(201,106,52,0.14);
      --warning-border: rgba(201,106,52,0.4);
      --flag-color: #dba15a;
      --flag-alert-color: #e28a5e;
      /* Lighter-but-redder than --flag-alert-color for the same reason
         its light-theme counterpart just above is darker-but-redder --
         see that comment. Kept light enough to still contrast against the
         dark background, same as --flag-alert-color already does. */
      --flag-alert-color-urgent: #e2604a;
      --badge-bg: #4b4b52;
      --overview-accent-tasks: #22b8cf;
      --overview-accent-discussion: #a78bda;
      --overview-accent-security: #d4b106;
      --sla-ok-color: #4ade80;
      --sla-ok-bg: rgba(34,197,94,0.14);
      --sla-ok-border: rgba(34,197,94,0.4);
    }}
  }}
  /* Manual Auto/Light/Dark override (see the theme-toggle control + its two
     small inline scripts near the end of _page()). A class on <html> beats
     both the plain :root light defaults above AND the prefers-color-scheme
     block above regardless of source order, since ".theme-dark"/".theme-
     light" on the html element outrank a bare ":root" selector on
     specificity alone -- no !important needed. These two blocks are exact
     duplicates of the values already above (light defaults / dark media
     query); CSS custom properties can't be composed or imported by
     reference, so keeping all three color definitions in sync by hand when
     a palette value changes is an accepted, deliberate cost of supporting a
     manual override at all. */
  html.theme-dark {{
    --bg: #16171a;
    --bg-translucent: rgba(22,23,26,0.78);
    --glass-border: rgba(255,255,255,0.14);
    --glass-shine: rgba(255,255,255,0.09);
    --glass-shadow: rgba(0,0,0,0.45);
    --glass-chip-bg: rgba(255,255,255,0.06);
    --text: #e7e7ea;
    --muted: #9a9aa2;
    --muted-2: #77777f;
    --divider: rgba(255,255,255,0.09);
    --card-bg: #202226;
    --card-border: rgba(255,255,255,0.08);
    --card-shadow: 0 1px 3px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.35);
    --input-bg: #26272b;
    --input-border: rgba(255,255,255,0.18);
    --excerpt-bg: #232427;
    --excerpt-border: rgba(255,255,255,0.14);
    --link: #6fa3ef;
    --accent: #6fa3ef;
    --focus-ring: rgba(111,163,239,0.45);
    --btn-approve: #158a96;     --btn-approve-hover: #106e78;
    --btn-internal: #7c5cc4;    --btn-internal-hover: #6749a8;
    --btn-remove: #6b7684;      --btn-remove-hover: #57616d;
    --btn-reject: #c96a34;      --btn-reject-hover: #a8551f;
    --btn-refresh: #2f6fd1;     --btn-refresh-hover: #2559ab;
    --btn-claim: #9c7018;       --btn-claim-hover: #86600f;
    --banner-bg: rgba(111,163,239,0.12);
    --banner-border: rgba(111,163,239,0.35);
    --warning-bg: rgba(201,106,52,0.14);
    --warning-border: rgba(201,106,52,0.4);
    --flag-color: #dba15a;
    --flag-alert-color: #e28a5e;
    --flag-alert-color-urgent: #e2604a;
    --badge-bg: #4b4b52;
    --overview-accent-tasks: #22b8cf;
    --overview-accent-discussion: #a78bda;
    --overview-accent-security: #d4b106;
    --sla-ok-color: #4ade80;
    --sla-ok-bg: rgba(34,197,94,0.14);
    --sla-ok-border: rgba(34,197,94,0.4);
  }}
  html.theme-light {{
    --bg: #ffffff;
    --bg-translucent: rgba(255,255,255,0.82);
    --glass-border: rgba(15,23,42,0.08);
    --glass-shine: rgba(255,255,255,0.55);
    --glass-shadow: rgba(15,23,42,0.10);
    --glass-chip-bg: rgba(255,255,255,0.5);
    --text: #1a1a1a;
    --muted: #666666;
    --muted-2: #9a9a9a;
    --divider: #e5e5e5;
    --card-bg: #ffffff;
    --card-border: #e2e2e5;
    --card-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
    --input-bg: #ffffff;
    --input-border: #cccccc;
    --excerpt-bg: #f7f7f7;
    --excerpt-border: #cccccc;
    --link: #1a56c4;
    --accent: #1a56c4;
    --focus-ring: rgba(26,86,196,0.35);
    --btn-approve: #0e7490;       --btn-approve-hover: #0b5c72;
    --btn-internal: #6d4aa8;      --btn-internal-hover: #59397f;
    --btn-remove: #64748b;        --btn-remove-hover: #52606d;
    --btn-reject: #c2622a;        --btn-reject-hover: #a34f1f;
    --btn-refresh: #1a56c4;       --btn-refresh-hover: #15408f;
    --btn-claim: #7d5a0a;         --btn-claim-hover: #664a08;
    --banner-bg: #eaf3fb;
    --banner-border: #a9cdf0;
    --warning-bg: #fdf3e8;
    --warning-border: #eac48a;
    --flag-color: #b45309;
    --flag-alert-color: #9a3412;
    --flag-alert-color-urgent: #b91c1c;
    --badge-bg: #666666;
    --overview-accent-tasks: #0e7490;
    --overview-accent-discussion: #6d4aa8;
    --overview-accent-security: #8a6d1a;
    --sla-ok-color: #1a6e2e;
    --sla-ok-bg: #e6f5ec;
    --sla-ok-border: #90d0a0;
  }}
  /* "B68FBF" theme: a fourth manual option alongside Auto/Light/Dark, built
     entirely from the literal hex color B68FBF and shades/tints of it
     (H=288.75 deg in HSL), rather than the CSA-blue accent used everywhere
     else in this file. Same variable set, same override mechanism as
     html.theme-dark/html.theme-light above -- just a different source
     palette. Backgrounds/text/borders/links/focus-ring are all derived from
     B68FBF itself (H=288.75). The six buttons use a complementary two-hue
     pair instead of pure single-hue monochrome: three (Remove, Approve,
     Refresh) stay on B68FBF's own hue (288.75 deg), and the other three
     (Reject, Internal, Claim) shift 30 deg to 9E8FBF's hue (258.75 deg) --
     9E8FBF shares B68FBF's exact lightness/saturation (L=0.655, S=0.273),
     so the pair reads as one deliberate family rather than an arbitrary
     second color. This replaces the six-distinct-hues convention used
     elsewhere in the file (see the plain :root block above) with "two
     complementary shades of B68FBF," per this theme's whole premise.
     --warning-bg/--warning-border/--flag-color/--flag-alert-color/
     --flag-alert-color-urgent are left as the same amber/orange/red used in
     the other three themes on purpose: those are functional caution/alert
     signal colors, not CSA branding, and folding them into the purple
     family would make them blend in instead of standing out. */
  html.theme-b68fbf {{
    --bg: #faf7fb;
    --bg-translucent: rgba(250,247,251,0.82);
    --glass-border: rgba(36,22,39,0.08);
    --glass-shine: rgba(255,255,255,0.55);
    --glass-shadow: rgba(36,22,39,0.10);
    --glass-chip-bg: rgba(255,255,255,0.5);
    --text: #241627;
    --muted: #684172;
    --muted-2: #a06dac;
    --divider: #eadeed;
    --card-bg: #fdfcfd;
    --card-border: #dcc6e2;
    --card-shadow: 0 1px 3px rgba(36,22,39,0.08), 0 1px 2px rgba(36,22,39,0.04);
    --input-bg: #fdfcfd;
    --input-border: #dcc6e2;
    --excerpt-bg: #f4edf5;
    --excerpt-border: #dcc6e2;
    --link: #865392;
    --accent: #865392;
    --focus-ring: rgba(135,62,152,0.35);
    --btn-approve: #a14ab5;       --btn-approve-hover: #8a409c;  /* B68FBF hue */
    --btn-internal: #452f74;      --btn-internal-hover: #36255b; /* 9E8FBF hue */
    --btn-remove: #b067c1;        --btn-remove-hover: #a34eb7;   /* B68FBF hue */
    --btn-reject: #52388a;        --btn-reject-hover: #432e70;   /* 9E8FBF hue */
    --btn-refresh: #8e419f;       --btn-refresh-hover: #773786;  /* B68FBF hue */
    --btn-claim: #38265e;         --btn-claim-hover: #291c45;    /* 9E8FBF hue */
    --banner-bg: rgba(135,62,152,0.10);
    --banner-border: rgba(135,62,152,0.35);
    --warning-bg: #fdf3e8;
    --warning-border: #eac48a;
    --flag-color: #b45309;
    --flag-alert-color: #9a3412;
    --flag-alert-color-urgent: #b91c1c;
    --badge-bg: #755b7b;
    /* Left as the same functional colors as the other three themes, same
       reasoning as --flag-alert-color/--warning-bg above: these are
       "which section is this" signal colors, not CSA branding, so folding
       them into the B68FBF/9E8FBF family would make them blend together
       instead of staying distinct from each other. */
    --overview-accent-tasks: #0e7490;
    --overview-accent-discussion: #6d4aa8;
    --overview-accent-security: #8a6d1a;
    --sla-ok-color: #1a6e2e;
    --sla-ok-bg: #e6f5ec;
    --sla-ok-border: #90d0a0;
  }}
  html {{ background: var(--bg); }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1180px; margin: 2rem auto; padding: 0 1rem; color: var(--text); background: var(--bg); }}
  /* Wider content column for the card-grid pages (/security, /flagged,
     /solved, /tasks, /overview -- see each render function's _page(...,
     wide=True) call) -- their .mini-ticket-grid/.overview-grid rows are
     already responsive (auto-fill/auto-fit) and were capping out at 3
     columns on an ordinary 1920px display with several hundred px of
     flat blank space to the right of them, never able to add a 4th no
     matter how wide the screen actually was. The main queue (/firstreply)
     is deliberately NOT included -- its ticket cards hold a reply-drafting
     textarea, and a much wider line length there would hurt readability
     rather than help, so it keeps the narrower 1180px default above.
     Higher specificity (body.wide-page vs body) wins regardless of
     source order. */
  body.wide-page {{ max-width: 1680px; }}
  /* Two EQUAL panels side by side -- used where both halves are peers
     with nothing to say one deserves more room than the other -- see the
     /flagged page's Blocked/Standup panels. Collapses to a single stacked
     column on narrow viewports rather than squeezing both side by side
     past the point of being readable. (The IT-Operations queue used to
     share the page with a narrower IT-Security side panel via its own
     asymmetric two-column `.layout` class -- that panel has since moved
     to its own /security page, and `.layout` was removed as dead code
     along with it. The main queue has since grown its own asymmetric
     column again below -- see `.main-with-sidebar` -- but for a
     different narrow-column purpose: a running "recently worked on"
     history rather than the old IT-Security panel.) */
  .two-panel {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.75rem; align-items: start; }}
  @media (max-width: 900px) {{ .two-panel {{ grid-template-columns: 1fr; }} }}
  /* /overview's four sections (see _render_overview_page). Deliberately a
     CSS multi-column layout rather than a grid: with a row-based grid, a
     row's height is set by its tallest cell, so a short section (e.g.
     "Tasks" with 2 items next to "Security recent" with 5) leaves a
     large blank gap under itself instead of ending early -- confirmed via
     screenshots showing 400px+ of dead space under short tiles. `columns:
     2` with `break-inside: avoid` on each section lets the browser pack
     tiles into whichever column has room (balancing column heights), so a
     short tile no longer holds up a row. Trade-off: reading order becomes
     column-first (tile 1 then tile 3 down the left column, tile 2 then
     tile 4 down the right) rather than strict left-to-right pairing --
     the four tiles' own relative order is unchanged, only which ones end
     up visually adjacent. Collapses to a single column below 900px (same
     breakpoint as .two-panel) since two narrow columns stop being useful
     once neither can comfortably fit a 380px-wide card grid. */
  .overview-grid {{ columns: 2; column-gap: 1.25rem; }}
  .overview-section {{ break-inside: avoid; margin-bottom: 1.25rem; border-left: 4px solid transparent; padding-left: 0.9rem; }}
  /* Per-section color from _render_overview_page's topic_key -- see the
     --overview-accent-* tokens above. A left-edge stripe rather than
     re-tinting the whole card, since these still need to read as the same
     "security-panel" card family the rest of the app uses, just with a
     colored edge marking which of the three sources this one is.

     Selector is ".security-panel.overview-section-tasks" (both classes),
     not just ".overview-section-tasks" alone -- .security-panel's own
     `border` shorthand (declared later in this stylesheet, see its own
     rule) sets border-left-color as part of that shorthand too, and a
     single-class selector here would have exactly the same specificity as
     that later rule, losing the "whoever comes last wins" tiebreak and
     silently rendering every stripe as plain --card-border gray instead
     of its own color. Matching both classes at once (0,2,0) outranks
     .security-panel's single-class (0,1,0) regardless of source order,
     which is the actually robust fix rather than relying on where either
     rule happens to sit in the file. */
  .security-panel.overview-section-tasks {{ border-left-color: var(--overview-accent-tasks); }}
  .security-panel.overview-section-discussion {{ border-left-color: var(--overview-accent-discussion); }}
  .security-panel.overview-section-security {{ border-left-color: var(--overview-accent-security); }}
  @media (max-width: 900px) {{ .overview-grid {{ columns: 1; }} }}
  .overview-section-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; margin-bottom: 0.75rem; }}
  .overview-section-head .column-heading {{ margin: 0; }}
  .overview-see-all {{ font-size: 0.82rem; font-weight: 600; white-space: nowrap; }}

  /* /sla page -- just the two Chart.js trend charts (queue health +
     agent activity) now; the KPI strip, four rule cards, and
     awaiting-first-response list that used to sit above them were
     removed by explicit request (the live per-ticket scan that fed them
     still runs -- see do_GET's /sla route -- it just now only feeds the
     nav badge's breach count, nothing rendered on this page directly).
     Built entirely from --card-bg/--card-border and the existing
     --flag-alert-color-urgent (breached, already red everywhere)/
     --flag-color+--warning-bg (due soon, already amber everywhere)
     tokens, plus the one genuinely new --sla-ok-* trio (see :root above)
     for "on track" -- no separate branded palette, so this reads as a
     native page rather than a bolted-on one. */
  .sla-chart-wrap {{ position: relative; background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px; padding: 1rem; margin-bottom: 1.25rem; }}
  .sla-chart-legend {{ display: flex; gap: 1rem; font-size: 0.78rem; color: var(--muted); margin-bottom: 0.5rem; flex-wrap: wrap; }}
  .sla-chart-legend span {{ display: inline-flex; align-items: center; gap: 0.35rem; }}
  .sla-chart-swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .sla-chart-swatch-line {{ width: 16px; height: 3px; display: inline-block; border-radius: 1px; }}
  .sla-history-note {{ font-size: 0.78rem; color: var(--muted-2); margin: -0.5rem 0 1rem; }}
  /* Chart.js trend charts (queue health + agent activity) -- range filter
     bar, drill-down panel, download/reset-zoom buttons, agent checkbox
     filter. See _sla_charts_html's own docstring for why these exist. */
  .sla-filter-bar {{ display: flex; align-items: center; gap: 0.5rem; margin: 0 0 0.75rem; flex-wrap: wrap; }}
  .sla-filter-label {{ font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-right: 0.2rem; }}
  .sla-range-btn {{ padding: 0.3rem 0.85rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; border: 1px solid var(--card-border); background: var(--card-bg); color: var(--muted); cursor: pointer; }}
  .sla-range-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .sla-range-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
  .sla-zoom-hint {{ font-size: 0.68rem; color: var(--muted-2); }}
  .sla-range-label {{ font-size: 0.75rem; color: var(--muted); margin-left: auto; }}
  .drill-hint {{ font-size: 0.72rem; color: var(--muted-2); margin: -0.35rem 0 0.5rem 0.1rem; }}
  .sla-chart-dl-btn {{ position: absolute; top: 0.65rem; right: 0.65rem; background: none; border: 1px solid var(--card-border); border-radius: 6px; padding: 0.1rem 0.5rem; font-size: 0.72rem; color: var(--muted); cursor: pointer; }}
  .sla-chart-dl-btn:hover {{ background: var(--excerpt-bg); color: var(--text); }}
  .sla-chart-reset-btn {{ right: 4.4rem; }}
  .sla-drilldown {{ margin: 0.4rem 0 1rem; padding: 0.75rem 1rem; background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px; }}
  .sla-drilldown-head {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.5rem; }}
  #sla-drilldown-title {{ font-size: 0.78rem; font-weight: 700; color: var(--text); }}
  #sla-drilldown-dismiss {{ font-size: 0.72rem; color: var(--muted); background: none; border: none; cursor: pointer; padding: 0; line-height: 1; }}
  #sla-drilldown-tickets {{ display: flex; flex-wrap: wrap; gap: 0.4rem; }}
  .sla-drilldown-chip {{ display: inline-block; padding: 0.15rem 0.6rem; background: var(--excerpt-bg); color: var(--accent); font-size: 0.75rem; font-weight: 600; border-radius: 999px; text-decoration: none; }}
  .sla-agent-filter-bar {{ display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap; margin: 0.25rem 0 0.6rem; }}
  .sla-agent-check {{ display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.78rem; font-weight: 600; color: var(--muted); cursor: pointer; padding: 0.25rem 0.7rem; border-radius: 999px; border: 1px solid var(--card-border); background: var(--card-bg); user-select: none; }}
  .sla-agent-check.active {{ background: var(--excerpt-bg); border-color: var(--accent); color: var(--accent); }}
  .sla-agent-check input[type=checkbox] {{ cursor: pointer; accent-color: var(--accent); width: 12px; height: 12px; }}
  /* "Recently worked on" history column pinned to the left of the main
     IT-Operations queue on /firstreply -- the same asymmetric narrow-plus-wide
     shape the old removed `.layout` class used for the IT-Security panel,
     revived here under a new name/purpose: unlike `.two-panel` above,
     these two columns are NOT peers -- the queue is the reason this page
     exists, the history list is a glanceable extra next to it. Wide
     enough (see the minmax below) to show a typical ticket subject in
     full rather than a sliver of one. Collapses to a single column on
     narrow viewports, with the history list dropping BELOW the queue
     rather than above it (via `order`), since the queue is what actually
     needs your attention first on a phone. */
  .main-with-sidebar {{ display: grid; grid-template-columns: minmax(300px, 400px) 1fr; gap: 1.75rem; align-items: start; }}
  @media (max-width: 900px) {{
    .main-with-sidebar {{ grid-template-columns: 1fr; }}
    .main-with-sidebar > .worked-on-sidebar {{ order: 2; }}
    .main-with-sidebar > .main-col {{ order: 1; }}
  }}
  /* Reuses .security-panel's card look (background/border/padding) and
     .security-panel .column-heading's heading size for visual consistency
     with the /flagged and /security pages -- this is the same "boxed
     panel of compact rows" idea, just a third place it shows up. Only the
     row/list shape below is new. */
  .worked-on-list {{ list-style: none; padding: 0; margin: 0; }}
  .worked-on-item {{ padding: 0.5rem 0.4rem; margin: 0 -0.4rem; border-bottom: 1px solid var(--divider); border-radius: 6px; transition: background-color 0.12s ease; }}
  .worked-on-item:hover {{ background: var(--divider); }}
  .worked-on-item:last-child {{ border-bottom: none; }}
  /* Full subject text, wrapping onto a second line rather than truncating
     -- the column is wide enough now (see .main-with-sidebar's minmax)
     that most subjects fit on one line anyway, and a genuinely long one
     wrapping to two lines beats silently hiding the end of it behind an
     ellipsis. word-break guards against a single unbroken long word/id
     overflowing the column instead of wrapping. */
  .worked-on-subject {{ font-size: 0.85rem; word-break: break-word; }}
  .worked-on-meta {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.1rem; }}
  /* /reconnect is genuinely short -- a sentence and a button, nothing
     more actually belongs there -- so instead of stretching that little
     content across the full page width (which reads as unfinished),
     this narrows it to a normal form-width column, the same idea as any
     small dialog/settings form. Content-only fix, not an attempt to add
     filler to a page that doesn't need any. */
  .narrow-page {{ max-width: 30rem; margin: 0 auto; }}
  .security-panel {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px; padding: 1rem; }}
  .security-panel .column-heading {{ font-size: 0.95rem; margin: 0 0 0.75rem 0; }}
  /* Row-hover highlight -- a small affordance that these rows are about
     something specific (each is a real ticket) rather than plain static
     text, without needing a border or shadow per row. */
  .mini-ticket {{ padding: 0.6rem 0.5rem; margin: 0 -0.5rem; border-bottom: 1px solid var(--divider); border-radius: 6px; transition: background-color 0.12s ease; }}
  .mini-ticket:hover {{ background: var(--divider); }}
  .mini-ticket:last-child {{ border-bottom: none; }}
  /* Same entrance-stagger idea as .ticket-list > .ticket below, for the
     compact rows shared by /tasks, /security, /flagged, /solved, and
     /overview's tiles -- .mini-ticket had no entrance animation at all
     before this. .mini-ticket-grid's own children are never mixed with
     any other element type, so plain nth-child already counts correctly
     here with no extra wrapper needed the way .ticket-list is for the
     main queue. A quicker fade (0.25s vs .ticket's 0.3s) and tighter
     steps, since these rows are smaller and denser -- a whole grid of
     them staggering as slowly as a handful of full ticket cards would
     feel sluggish rather than lively.

     Placed here, BEFORE .mini-ticket:target below, on purpose: both rules
     set the full `animation` shorthand and have equal selector
     specificity, so whichever comes LAST in the stylesheet is what
     actually wins when a row is both freshly rendered AND the current
     :target -- exactly the case on a fresh page load with a
     #task-<local_id>/#ticket-<tid> fragment already in the URL. Keeping
     :target below/after this block is what lets the highlight animation
     win over the plain entrance fade in that moment, instead of the
     fade silently overriding it. */
  .mini-ticket-grid > .mini-ticket {{ animation: fadeIn 0.25s ease-out backwards; }}
  .mini-ticket-grid > .mini-ticket:nth-child(1) {{ animation-delay: 0s; }}
  .mini-ticket-grid > .mini-ticket:nth-child(2) {{ animation-delay: 0.04s; }}
  .mini-ticket-grid > .mini-ticket:nth-child(3) {{ animation-delay: 0.08s; }}
  .mini-ticket-grid > .mini-ticket:nth-child(4) {{ animation-delay: 0.12s; }}
  .mini-ticket-grid > .mini-ticket:nth-child(n+5) {{ animation-delay: 0.16s; }}
  /* Landing on /tasks with a #task-<local_id> fragment (a task's title
     link -- see _task_title_link_html) scrolls here for free, just from
     the id existing on this row's wrapper (_render_draft_task_row/
     _render_open_task_row) -- no JS needed. :target then matches this
     exact row (and only this row -- every other .mini-ticket on the page
     has a different id, so nothing else lights up), fading its
     background from --focus-ring down to nothing so the row that was
     just scrolled to is unmistakable for a moment, then settles back to
     looking like every other row once you've had a beat to spot it. */
  .mini-ticket:target {{ animation: taskHighlight 2s ease-out; }}
  @keyframes taskHighlight {{
    from {{ background: var(--focus-ring); }}
    to {{ background: transparent; }}
  }}
  /* Same trick, applied to a main-queue ticket card instead of a /tasks
     row: landing on /firstreply#ticket-<tid> (see notify()'s open_url in
     first_reply_check.py -- the notification for a NEW ticket now opens
     straight at that ticket's own anchor, not just the bare page) makes
     the exact ticket that triggered the notification unmistakable the
     moment the page loads, rather than leaving you to find it yourself
     among everything else in the queue. Reuses taskHighlight rather than
     a second near-identical keyframe -- a momentary background fade means
     the same thing here as it does on a task row. */
  .ticket:target {{ animation: taskHighlight 2s ease-out; }}
  /* A compact-row list (a draft/open task, an IT-Security row) used to
     always stack one full-width .mini-ticket per row -- fine for a
     couple of items, wasteful once there are enough to scroll through,
     since each row's own content (a title line plus one row of small
     fields/buttons) rarely needs the full page width. Wrapping the same
     .mini-ticket markup in .mini-ticket-grid instead lets them flow into
     as many columns as fit (auto-fill, not a fixed 2 like .two-panel,
     since this is one list of same-shaped items rather than two named
     categories side by side) -- the row markup itself is untouched,
     only the container and these descendant overrides change. Higher
     specificity than the plain .mini-ticket rules above (two classes vs
     one) wins the border/margin/padding overrides without needing
     !important; declared after .mini-ticket:last-child so it also wins
     that tie (equal specificity, later wins) and every grid item gets a
     real border on all sides, last one included. */
  .mini-ticket-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 0.75rem; align-items: start; }}
  .mini-ticket-grid .mini-ticket {{
    border: 1px solid var(--card-border); border-radius: 10px; margin: 0; padding: 0.75rem 0.85rem;
  }}
  @media (max-width: 720px) {{ .mini-ticket-grid {{ grid-template-columns: 1fr; }} }}
  /* Filter-as-you-type box (see _filter_box_html) and, on the main queue
     only, the "Priority first" sort toggle beside it -- a shared row of
     small controls sitting directly above whichever list they act on.
     Both wrap onto their own line on a narrow viewport rather than
     squeezing the input down to nothing. */
  .list-controls {{ display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.85rem; }}
  .filter-box {{
    display: flex; align-items: center; gap: 0.4rem; flex: 1 1 240px; min-width: 160px;
    background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px;
    padding: 0.4rem 0.65rem; color: var(--muted);
  }}
  .filter-box .icon-sm {{ flex-shrink: 0; }}
  .filter-input {{
    flex: 1 1 auto; min-width: 0; border: none; background: transparent; font: inherit;
    color: var(--text); padding: 0; outline: none;
  }}
  .filter-input::placeholder {{ color: var(--muted); }}
  .filter-count {{ flex-shrink: 0; font-size: 0.78rem; color: var(--muted); white-space: nowrap; }}
  .sort-toggle {{
    display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; font-weight: 600;
    color: var(--accent); text-decoration: none; white-space: nowrap;
  }}
  .sort-toggle:hover {{ text-decoration: underline; }}
  .mini-ticket-head {{ font-size: 0.88rem; font-weight: 600; margin-bottom: 0.2rem; }}
  .mini-ticket-meta {{ font-size: 0.78rem; color: var(--muted); display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap; }}
  /* display:flex here (not just margin-top) so a row with TWO (or more)
     of these forms side by side (the /tasks page's draft row: promote +
     discard; the open-task row: Quick Solve + Mark done) lays them out
     with a gap instead of stacking full-width, without needing an inline
     style on any one form. A row with only one form (Claim) is
     unaffected -- a single flex item looks the same as before. */
  .mini-claim-form {{ margin-top: 0.4rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }}
  .mini-claim-form button {{ padding: 0.3rem 0.7rem; font-size: 0.78rem; display: inline-flex; align-items: center; gap: 0.3rem; }}
  /* The draft row's promote form has TWO controls of its own (the
     Estimated minutes input, then the button) that need to sit inline
     together, nested inside .mini-claim-form's own flex row alongside the
     separate Discard form -- without this, the input and button would
     stack full-width inside their shared <form>. */
  .promote-form {{ display: flex; align-items: flex-end; gap: 0.5rem; flex-wrap: wrap; }}
  .task-est-input {{
    width: 4.5rem; box-sizing: border-box; font-family: inherit; font-size: 0.78rem;
    padding: 0.3rem 0.5rem; border-radius: 6px; border: 1px solid var(--input-border);
    background: var(--input-bg); color: var(--text);
  }}
  .task-est-input:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus-ring); }}
  /* Assignee/Requester/Allocation selects on the promote form -- each
     wrapped in a .promote-field label so a small caption sits above the
     control (matching how the Estimated minutes input reads without one,
     since its placeholder already served that role before these existed).
     align-items: flex-end on .promote-form (above) keeps every field's
     control lined up on the same baseline as the submit button, despite
     the caption adding extra height above only the fields, not the button. */
  .promote-field {{ display: inline-flex; flex-direction: column; gap: 0.15rem; font-size: 0.68rem; color: var(--muted); }}
  .promote-field-label {{ font-weight: 600; }}
  .task-identity-select {{
    font-family: inherit; font-size: 0.78rem; padding: 0.3rem 0.4rem; border-radius: 6px;
    border: 1px solid var(--input-border); background: var(--input-bg); color: var(--text);
  }}
  .task-identity-select:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus-ring); }}
  /* Move up/down buttons on a draft row -- reuses the outlined .remove
     button style (same low-emphasis treatment as Discard) rather than
     introducing a seventh button color just for these two. A disabled
     button (already-first/already-last -- see is_first/is_last in
     _render_draft_task_row) dims via the existing global button:disabled
     rule, same as every other disabled button on this page. */
  .task-reorder {{ display: inline-flex; gap: 0.2rem; }}
  .task-reorder button {{ padding: 0.3rem 0.5rem; }}
  /* The "Edit" collapsible on a draft row -- same <details> pattern as
     the main queue's "Replied today"/"Last N replies" sections, just
     without a persisted open/closed state (see data-storage-key there):
     editing is a one-off action, not a standing preference worth
     remembering across page loads. */
  .task-edit {{ margin-top: 0.5rem; }}
  .task-edit summary {{ cursor: pointer; color: var(--muted); font-size: 0.82rem; font-weight: 600; }}
  .task-edit summary:focus-visible {{ outline: 2px solid var(--focus-ring); outline-offset: 2px; }}
  .task-edit-form {{ margin-top: 0.5rem; }}
  .task-edit-form input[type=text], .task-edit-form input[type=number], .task-edit-form select {{ margin-bottom: 0.4rem; }}
  .task-edit-form select {{ width: 100%; box-sizing: border-box; display: block; }}
  /* A short, purely local organizational tag on a draft/open row --
     reuses the header's .stat-chip pill styling (same shape/weight),
     rendered as a leading tag directly in front of the subject text so
     it reads as part of the title line itself, not a chip trailing
     after it. .stat-chip's own margin-right is exactly the gap needed
     before the subject, so no override is needed here. Never sent to
     Zendesk -- see _common.add_personal_task. */
  /* Optional Category/Estimated minutes row on the "Add task" form --
     sits between the notes textarea and the Add task button, same
     side-by-side layout as .promote-form's fields for visual consistency
     between the two places these values get entered. */
  .new-task-extra-fields {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.6rem; }}
  /* flex-grow (the "1" here) still lets these share leftover space evenly
     on a narrow form, but max-width stops either one from stretching to
     fill half of a wide page just because there's only one other field
     next to it -- a short dropdown or a 2-3 digit number doesn't need to
     be several hundred pixels wide. */
  .new-task-extra-fields input, .new-task-extra-fields select {{ flex: 1 1 12rem; max-width: 16rem; margin-bottom: 0; }}
  /* /tasks page -- notes are optional, so .mini-ticket-notes only ever
     shows when a task actually has some. */
  .mini-ticket-notes {{ font-size: 0.82rem; color: var(--muted); margin-top: 0.3rem; white-space: pre-wrap; }}
  h1 {{ font-size: 1.1rem; margin: 0; }}
  /* Shrink-wrapped to its own content (width: fit-content) rather than
     stretched across the full page -- the badge, controls row, and the
     narrow vertical icon column are all much narrower than the page itself
     now, so a full-bleed .brand left a large empty rectangle beside the
     icon column. A compact box needs a border on every side (not just the
     old bottom-only accent line) and rounded corners all around -- both
     were previously fine shortcuts for a bar that spanned edge-to-edge, but
     read as a broken/half-open shape once the box no longer touches either
     edge. Dropping the negative side-margins too, so the card's left edge
     lines up with the rest of the page's content instead of bleeding out
     further left than everything below it. */
  .brand {{
    position: sticky; top: 0; z-index: 5; background: var(--bg);
    width: fit-content; margin: 0 0 1rem 0; padding: 0.5rem 1rem 0.4rem 1rem;
    border: 2px solid var(--accent); border-radius: 14px;
  }}
  /* Frosted-glass sticky header: only applied when the browser actually
     supports backdrop-filter, so the plain opaque var(--bg) + solid accent
     border above stay the real fallback rather than a flash of unstyled
     background -- content scrolling underneath shows through slightly
     blurred instead of the header being a flat opaque bar. saturate(160%)
     alongside the blur is what pushes this from "foggy" toward the richer
     look of real glass UIs (macOS/iOS control-center panels) rather than
     just a dimmed, blurred rectangle. The hard accent-colored border is
     swapped for a soft translucent edge plus an inset top "shine" seam and
     a drop shadow, so the header reads as a floating glass panel rather
     than a bar with a divider line under it -- .brand's own border-radius
     above (unconditional, since it's just a shape and costs nothing on
     browsers without backdrop-filter) is what makes it look like a
     panel in the first place; this block is what makes it look like
     glass specifically. */
  @supports (backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)) {{
    .brand {{
      background: var(--bg-translucent);
      -webkit-backdrop-filter: blur(16px) saturate(160%);
      backdrop-filter: blur(16px) saturate(160%);
      border: 1px solid var(--glass-border);
      box-shadow: inset 0 1px 0 var(--glass-shine), 0 4px 24px var(--glass-shadow);
    }}
    /* The small pill controls living directly on the glass header get a
       lighter version of the same treatment (their own thin blur, on top
       of .brand's already-blurred background) so the whole header reads as
       one consistent material rather than solid chips sitting on a glass
       strip. Deliberately NOT applied to .refresh/.trigger/.approve-style
       buttons -- those are meant to read as solid, opaque, high-attention
       actions (see the "one filled primary action" design principle in
       the README), and making them translucent too would undercut that. */
    .theme-toggle, .top-nav-link, .zendesk-status-link {{
      -webkit-backdrop-filter: blur(6px);
      backdrop-filter: blur(6px);
    }}
  }}
  /* The logo/title (.brand-home-link, in the markup below) is deliberately
     NOT wrapped in a flex row that would stretch or center it -- as a plain
     block-level first child of .brand it just sizes to its own content and
     sits flush against the left edge, which is the whole point: a thin
     badge anchored in the header's top-left corner, not a banner spanning
     the full width. Everything that used to share row 1 with it (the page
     action button, the theme toggle) moved down into .brand-controls-row
     instead -- see that rule below -- rather than being squeezed into the
     same now-much-narrower line as the badge.

     This is the header's second structural pass: it used to be a single
     row-1 (.brand-top, splitting .brand-left/.brand-right across a
     justify-content: space-between line) with the nav on its own row-2
     below. That flat two-row shape is now four stacked pieces -- the
     badge, then .brand-controls-row, then .brand-nav-row, then the
     Zendesk OAuth status control's own .brand-status-row -- each one
     shorter/thinner than row 1 used to be on its own, with that last one
     pushed down to anchor at the sidebar's bottom on wide viewports
     (see .brand-status-row's own rule below). */
  .brand-controls-row {{
    display: flex; align-items: center; justify-content: flex-end;
    flex-wrap: wrap; gap: 0.5rem 0.75rem; margin-top: 0.35rem;
  }}
  /* Nav links get their own row below the badge + controls row, instead of
     crowding either one -- six links plus a multi-button theme toggle plus
     a Refresh button was fighting for the same strip of space and wrapping
     into an unpredictable number of stacked rows depending on viewport
     width and which warning badges happened to be showing. */
  .brand-nav-row {{ display: flex; align-items: flex-start; margin-top: 0.4rem; }}
  /* Icon-only now (see _page()) -- collapsed into .brand-controls-row's far
     corner, since which *theme* you're using is a standing preference you
     set once and forget about, not something that deserves the same visual
     weight as the actual navigation below it. Buttons keep their text as a
     title tooltip and data-theme attribute; theme_script's
     querySelectorAll('.theme-toggle button')/.active toggling is unchanged
     by dropping the label text. */
  .theme-toggle {{
    display: inline-flex; border: 1px solid var(--input-border); border-radius: 999px;
    overflow: hidden; background: var(--glass-chip-bg);
  }}
  .theme-toggle button {{
    background: transparent; color: var(--muted); padding: 0.4rem; font-size: 0.78rem;
    border-radius: 0; box-shadow: none; display: inline-flex; align-items: center; justify-content: center;
  }}
  .theme-toggle button:hover {{ background: var(--divider); }}
  .theme-toggle button.active {{ background: var(--accent); color: white; }}
  .theme-toggle button.active:hover {{ background: var(--accent); }}
  /* The B68FBF button is a literal custom hex color, not a concept a line-
     icon could stand in for (unlike Auto/Light/Dark's monitor/sun/moon) --
     so it gets an actual swatch of that color instead. */
  .theme-swatch {{
    display: inline-block; width: 12px; height: 12px; border-radius: 50%;
    background: #B68FBF; border: 1px solid rgba(0, 0, 0, 0.15);
  }}
  /* Nav list of actual working pages (Overview / Tasks / Discussion /
     Security / Recently Solved) -- its own dedicated
     stack (see .brand-nav-row) instead of as middot-separated text down
     in the meta line, so it reads as actual navigation (with room to
     grow) rather than accumulating links inside a status sentence.
     Stacked vertically with align-items: stretch (not flex-start) so
     each link's own hover/active fill spans the full sidebar width like
     a normal app nav row, not just the width of its icon+label. Just one
     flat list now, no second "status" cluster/divider -- Zendesk OAuth
     used to be the other category here and now lives entirely separately
     (see .zendesk-status-link, below), so there's nothing left to
     separate the remaining five links from. */
  .top-nav {{ display: flex; flex-direction: column; align-items: stretch; gap: 0.15rem; }}
  /* Icon + a real visible label now (see _link/_top_nav_html) -- a plain
     left-aligned row instead of the old centered icon-only pill, with
     enough padding to read as a proper nav item and enough gap between
     icon/label/badge to keep them from crowding each other. */
  .top-nav-link {{
    display: flex; align-items: center; gap: 0.6rem; color: var(--muted);
    font-size: 0.85rem; font-weight: 600; padding: 0.5rem 0.7rem; border-radius: 8px;
    border: 1px solid transparent; background: transparent;
    transition: background-color 0.12s ease, color 0.12s ease;
  }}
  /* flex: 1 is what pushes a trailing badge (see .nav-badge) to the row's
     far end without needing a separate spacer element -- the label just
     claims whatever width the icon and badge don't need. */
  .top-nav-label {{ flex: 1; }}
  .top-nav-link:hover {{ background: var(--divider); color: var(--text); text-decoration: none; }}
  /* Marks whichever nav link matches the page you're already on -- same
     filled treatment as the theme toggle's own .active state, so
     "where am I" is answered by the nav itself rather than only by the
     page's own heading. */
  .top-nav-link.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
  .top-nav-link.active:hover {{ background: var(--accent); color: white; }}
  /* Zendesk OAuth's own small icon-only control, living in its own
     .brand-status-row at the very bottom of the sidebar/header (below the
     nav list, not next to the theme toggle in .brand-controls-row and not
     in the nav list itself) -- see _zendesk_status_control_html's
     docstring for why it was pulled out of the nav to begin with, and
     .brand-status-row below for why it now anchors to the bottom.
     Muted/quiet by default; .zendesk-status-warn (applied when the last
     real check found it disconnected) is the only time this control
     should actually draw the eye. */
  .zendesk-status-link {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 2.2rem; height: 2.2rem; border-radius: 999px; flex-shrink: 0;
    color: var(--muted); border: 1px solid var(--input-border); background: var(--glass-chip-bg);
    transition: background-color 0.12s ease, color 0.12s ease, border-color 0.12s ease;
  }}
  .zendesk-status-link:hover {{ background: var(--divider); color: var(--text); }}
  .zendesk-status-warn {{ color: var(--flag-alert-color); border-color: var(--flag-alert-color); }}
  /* Its own row, separated from the nav list above by a thin divider (same
     treatment as .recently-handled's own top divider elsewhere) -- a
     standing "is this healthy"/"refresh now" utility footer reads
     differently from another nav destination, so it gets a visually
     distinct spot rather than blending into the links above it. Holds two
     small controls side by side (the Zendesk OAuth status icon and the
     Refresh button, in that order) with a small gap between them and
     wrapping allowed in case the narrow top-card layout's width ever gets
     tight; centered by default (that narrow layout has nothing to anchor
     "bottom" against). The wide sidebar media query below overrides
     margin-top to auto so this row actually pins to the bottom of the
     full-height rail instead of just trailing the nav list. */
  .brand-status-row {{
    display: flex; justify-content: center; align-items: center; flex-wrap: wrap;
    gap: 0.6rem; margin-top: 1rem; padding-top: 0.75rem;
    border-top: 1px solid var(--divider);
  }}
  /* Small count badge riding on a nav link (tasks awaiting a decision,
     flagged/security counts) -- lets that page's own status travel with
     you to every other page instead of only being visible when you're
     actually on it. .nav-badge-warn is available for a higher-severity
     count (the same color as the "already replied" flag elsewhere) via
     _badge()'s warn=True, though nothing currently uses it; the plain
     .nav-badge (Tasks' count, among others) is neutral, since a waiting
     task isn't inherently a problem. */
  .nav-badge {{
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 1.1rem; height: 1.1rem; padding: 0 0.3rem;
    border-radius: 999px; background: var(--badge-bg); color: white;
    font-size: 0.68rem; font-weight: 700; line-height: 1;
  }}
  .nav-badge-warn {{ background: var(--flag-alert-color); }}
  /* The logo/title becomes a link back to /firstreply -- color: inherit and
     text-decoration: none keep it looking like the plain heading it
     always was, not a normal blue underlined link. */
  .brand-home-link {{
    display: inline-flex; align-items: center; gap: 0.6rem;
    color: inherit; text-decoration: none;
  }}
  .brand-home-link:hover {{ text-decoration: none; }}
  .brand-home-link h1 {{ margin: 0; }}
  .brand .meta {{ margin: 0.5rem 0 0 0; line-height: 1.5; }}
  /* Wide viewports get a true left-hand sidebar instead of a header
     pinned above the content -- position: fixed with top/bottom both 0
     spans the full viewport height, flush against the left edge (no
     border-radius: butting right up against the edge is the point, same
     as the very first full-bleed header did before it became a compact
     card), with only a border-right marking the seam against the content
     beside it instead of the compact card's all-around border. overflow-y:
     auto lets the sidebar's own content (badge + controls + the nav
     column) scroll independently if it's ever taller than the viewport,
     rather than pushing itself off-screen or forcing the whole page to
     scroll just to reach the last nav link. A fixed element takes up no
     space in normal flow on its own, so body picks up a matching
     margin-left here too -- otherwise the page's real content would slide
     out from under the rail instead of starting clear of it.

     The badge, the controls row, and the nav row all need their own
     adjustments alongside it: a 240px-wide column is too narrow for the
     logo+title to sit beside each other on one line (title now stacks
     below the icon instead, wrapping across as many lines as it needs)
     and too narrow for the Refresh button + theme toggle to sit side by
     side (both go full-width and stack instead, same shape a sidebar's
     own primary action usually takes).

     Below ~860px there's no longer room for both a fixed rail and a
     readable content column next to it, so this block simply doesn't
     apply -- every rule above this one is that narrower fallback: the
     compact, in-flow top card from the previous pass, unchanged. */
  @media (min-width: 861px) {{
    body {{ margin: 2rem auto 2rem 264px; }}
    .brand {{
      position: fixed; top: 0; left: 0; bottom: 0; z-index: 5;
      width: 240px; box-sizing: border-box; overflow-y: auto;
      margin: 0; padding: 1.25rem 1rem;
      border: none; border-right: 2px solid var(--accent); border-radius: 0;
      /* Flex column so .brand-status-row (the Zendesk OAuth control, last
         child) can use margin-top: auto below to anchor itself to the
         bottom of this full-height rail, with everything above it
         (badge/controls/nav/meta) keeping its normal top-down stacking --
         same trick as a classic "push footer down" layout. */
      display: flex; flex-direction: column;
    }}
    @supports (backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)) {{
      .brand {{
        border: none; border-right: 1px solid var(--glass-border);
        box-shadow: inset -1px 0 0 var(--glass-shine), 4px 0 24px var(--glass-shadow);
      }}
    }}
    /* Logo stays INLINE with the title (same starting line) even in the
       sidebar, matching the narrow-fallback layout above rather than
       stacking the icon on its own row -- but a plain flex row can't do
       that here the way it does in the narrow card: .brand-home-link's
       flex row there never wraps because the CARD grows to fit an
       unwrapped title (width: fit-content), while the sidebar is a fixed
       240px column the title can't help but wrap inside. Flexbox wraps
       whole ITEMS, not text within one -- so a flex row would just bump
       the entire (long, unwrapped-width) title down to its own line
       below the icon, which is exactly the stacked look this is trying
       to undo. display: flow-root + float: left on the icon (below) is
       the one layout mode that actually lets text wrap AROUND a
       fixed-size image across multiple lines, the same way body text
       wraps around an inline image -- flow-root just gives this element
       its own block-formatting context so the floated icon's height is
       properly contained (a float alone doesn't push its parent's own
       height out to match). */
    .brand-home-link {{ display: flow-root; }}
    .brand-home-link .logo {{ float: left; margin: 0.05rem 0.6rem 0 0; }}
    .brand-home-link h1 {{ margin: 0; }}
    /* Sits right after the (float-wrapped) title in normal flow -- once
       the title's last line clears the icon's height, this drops below
       both rather than competing with them for the same cramped line. */
    .brand-home-link .stat-chip {{ margin-top: 0.4rem; }}
    .brand-controls-row {{
      flex-direction: column; align-items: stretch; justify-content: flex-start;
      gap: 0.6rem; margin-top: 1.1rem;
    }}
    /* No <form> left inside .brand-controls-row now that the Refresh
       button has moved to .brand-status-row below (only the theme toggle
       remains here) -- the button rule stays since it still governs the
       theme-toggle's own <button> elements. */
    .brand-controls-row button {{ width: 100%; justify-content: center; white-space: normal; text-align: center; }}
    .brand-controls-row .theme-toggle {{ align-self: stretch; justify-content: center; }}
    .brand-nav-row {{ margin-top: 1.1rem; }}
    /* The one override that actually makes this a bottom-anchored footer
       rather than just another stacked row: margin-top: auto on a flex
       column's last child eats all the remaining vertical space in the
       (full-height, position: fixed) rail, pushing this row down to sit
       flush against the bottom regardless of how much or how little
       content (badge/controls/nav, plus the meta line on /firstreply only) comes
       before it. Centering and the divider above it still come from the
       base .brand-status-row rule -- only the vertical anchor changes
       here. */
    .brand-status-row {{ margin-top: auto; }}
  }}
  .stat-chip {{
    display: inline-block; background: var(--divider); color: var(--text);
    font-size: 0.78rem; font-weight: 600; padding: 0.15rem 0.55rem;
    border-radius: 999px; margin-right: 0.4rem;
  }}
  /* Avatar-initials bubble next to a resolved person's name -- a stable
     per-name color (see _avatar_html) rather than a photo, for a quick
     visual anchor when scanning a list of names. */
  .avatar {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.3rem; height: 1.3rem; border-radius: 50%; color: white;
    font-size: 0.62rem; font-weight: 700; flex-shrink: 0; letter-spacing: 0.02em;
    margin-right: 0.35rem; vertical-align: -0.25em;
  }}
  /* One rule per _AVATAR_HUES index (see _avatar_html), generated by
     AVATAR_HUE_CSS from _AVATAR_HUES/_AVATAR_HUES_B68FBF -- the default
     palette, then B68FBF's own hue-rotated palette keyed off the same
     html.theme-b68fbf class every other theme-conditional swap on this
     page uses. */
{AVATAR_HUE_CSS}
  .icon {{ width: 14px; height: 14px; flex-shrink: 0; }}
  .icon-sm {{ width: 12px; height: 12px; flex-shrink: 0; }}
  .logo {{ height: 36px; width: auto; }}
  /* B68FBF gets its own logo (see LOGO_B68FBF_DATA_URI/_page()) -- both
     <img> tags are always in the DOM, CSS just shows the one matching the
     current theme, so the swap rides the exact same html.theme-b68fbf class
     theme_init_script already sets before first paint (no flash of the
     wrong logo, no extra JS needed just for this). */
  .logo-b68fbf {{ display: none; }}
  html.theme-b68fbf .logo-default {{ display: none; }}
  html.theme-b68fbf .logo-b68fbf {{ display: inline; }}
  /* Topic icons (see _topic_icon()) -- self-contained colour badges, sized
     for three different spots: next to a page's own h2 heading, floating
     above an empty-state's muted text, and inline inside the small
     already-coloured priority pill. */
  .topic-icon {{ width: 24px; height: 24px; flex-shrink: 0; vertical-align: middle; margin-right: 0.45rem; border-radius: 6px; }}
  .topic-icon-empty {{ display: block; width: 26px; height: 26px; margin: 0 auto 0.6rem auto; border-radius: 6px; opacity: 0.9; }}
  .badge-priority .topic-icon-inline {{ width: 11px; height: 11px; margin-right: 0.3rem; vertical-align: -1px; border-radius: 0; }}
  /* B68FBF-only recolor of the topic-icon badges (see _topic_icon()) -- same
     always-both-in-the-DOM, pure-CSS show/hide trick the header logo swap
     uses (.logo-default/.logo-b68fbf). Each of the three concrete sizing
     contexts these badges render in gets its own explicit "shown" rule
     below, since a bare "display: inline" here would otherwise clobber
     .topic-icon-empty's own "display: block" centering. */
  .topic-icon-b68fbf {{ display: none; }}
  html.theme-b68fbf .topic-icon-default {{ display: none; }}
  html.theme-b68fbf .topic-icon-b68fbf.topic-icon {{ display: inline; }}
  html.theme-b68fbf .topic-icon-b68fbf.topic-icon-empty {{ display: block; }}
  html.theme-b68fbf .topic-icon-b68fbf.topic-icon-inline {{ display: inline; }}
  /* Confirmation "banner" now renders as a toast: fixed to the corner of
     the viewport instead of docked above the page content, so it doesn't
     shove the queue down or require scrolling back up to notice. Still a
     real, always-in-the-DOM element (not injected by JS) so it works
     identically with JS disabled -- toastScript (see _page()) just adds
     the auto-fade-and-remove behavior on top as progressive enhancement.
     role="status"/aria-live="polite" (set where this is built) still
     announces it to screen readers the instant it appears, same as
     before. */
  .banner {{
    position: fixed; right: 1rem; bottom: 1rem; z-index: 20; max-width: min(420px, calc(100vw - 2rem));
    background: var(--banner-bg); border: 1px solid var(--banner-border); border-left: 4px solid var(--accent);
    padding: 0.75rem 1rem; border-radius: 10px; box-shadow: 0 4px 16px rgba(0,0,0,0.18);
    animation: toastIn 0.25s ease-out;
  }}
  .banner.toast-leaving {{ animation: toastOut 0.3s ease-in forwards; }}
  /* The Undo button rides right in the toast's own text flow (see
     banner_undo_html in _page()) rather than as a separate row -- it's one
     more clause in the same sentence, not a second action competing for
     attention. Aggressively overrides the generic `button` rule's solid
     filled-pill look (padding, background, white text, box-shadow) down to
     a plain underlined inline link instead, since a bold colored button
     stacked on top of an already-attention-grabbing toast would be
     overkill for what's meant to read as a quiet, easy-to-miss-on-purpose
     escape hatch. */
  .banner-undo {{ display: inline; margin: 0; }}
  .banner-undo button {{
    display: inline; background: transparent; color: var(--accent); border: none;
    padding: 0; margin-left: 0.4rem; font: inherit; font-weight: 700; text-decoration: underline;
    cursor: pointer; box-shadow: none;
  }}
  .banner-undo button:hover {{ text-decoration: none; }}
  .auth-warning {{ position: static; max-width: none; box-shadow: none; margin-bottom: 1rem; }}
  /* The auth-connection warning reuses .banner's colors but stays docked
     in the normal page flow (see .auth-warning override above) -- it's a
     standing condition ("reconnect needed"), not a one-off event like a
     post confirmation, so it shouldn't auto-disappear or float over
     content the way a toast does. */
  @keyframes toastIn {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  @keyframes toastOut {{
    from {{ opacity: 1; transform: translateY(0); }}
    to {{ opacity: 0; transform: translateY(8px); }}
  }}
  /* Flatter cards: a 1px border carries the shape, with shadow reserved
     for hover (a subtle lift) rather than sitting on every card at rest --
     closer to current flat-SaaS conventions than the permanent drop-shadow
     this used to have everywhere. */
  .ticket {{
    background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px;
    padding: 1rem; margin-bottom: 1.2rem; animation: fadeIn 0.3s ease-out backwards;
    transition: box-shadow 0.15s ease, border-color 0.15s ease, opacity 0.2s ease, transform 0.2s ease;
  }}
  .ticket:hover {{ box-shadow: var(--card-shadow); border-color: var(--muted-2); }}
  .ticket.is-removing, .mini-ticket.is-removing {{ opacity: 0; transform: scale(0.98); pointer-events: none; }}
  /* Staggers each ticket's own fadeIn (above) by a few hundredths of a
     second per position, so a list of them settles in one after another
     on page load instead of all popping in at the exact same instant --
     "backwards" fill-mode (added to .ticket's animation above) holds each
     card at fadeIn's own 0%% state (opacity 0) for the whole delay, rather
     than briefly flashing fully visible before its turn starts. .ticket-
     list (not .ticket alone) is what nth-child counts against here -- see
     do_GET's own comment for why: nth-child counts every sibling
     regardless of class, so without a wrapper scoped to JUST the ticket
     cards, this would silently miscount whenever the auth-warning banner
     or the filter/sort controls happened to precede them that day. Capped
     at 5 explicit steps, then flat -- a queue of 40 tickets shouldn't take
     two seconds to finish appearing just because there are 40 of them. */
  .ticket-list > .ticket:nth-child(1) {{ animation-delay: 0s; }}
  .ticket-list > .ticket:nth-child(2) {{ animation-delay: 0.05s; }}
  .ticket-list > .ticket:nth-child(3) {{ animation-delay: 0.1s; }}
  .ticket-list > .ticket:nth-child(4) {{ animation-delay: 0.15s; }}
  .ticket-list > .ticket:nth-child(n+5) {{ animation-delay: 0.2s; }}
  .ticket-eyebrow {{ font-size: 0.7rem; font-weight: 700; color: var(--muted-2); text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 0.15rem 0; }}
  .ticket h2 {{ font-size: 1.1rem; font-weight: 700; margin: 0 0 0.4rem 0; }}
  .ticket h2 .tid {{ color: var(--muted-2); font-weight: 500; font-size: 0.95rem; }}
  /* A ticket's title text sitting next to its "#tid" link (see
     _titled_link_html, shared by _render_ticket/_render_confirm_page/
     _render_readonly_ticket_row/_render_worked_on_sidebar) now opens the
     same Zendesk link the number does -- fixing a common miss-click
     aimed at the (much larger) title instead of the small number beside
     it. A personal task's title (draft or open -- _task_title_link_html,
     used by _render_draft_task_row/_render_open_task_row/
     _render_overview_draft_task_row) shares this same .ticket-title-link
     class/look but points somewhere different: back to that task's own
     row on /tasks, not out to Zendesk -- see that helper's docstring.
     Either way, kept looking like plain text rather than a normal blue/
     underlined link on purpose: color: inherit (matches whatever heading/
     row it's sitting in -- a .ticket h2, a .mini-ticket-head, a
     .worked-on-subject line) and no underline until hover/focus, so this
     doesn't add visual weight to already-busy cards; the cursor and hover
     underline are enough of a cue once a pointer is actually on it. */
  .ticket-title-link {{ color: inherit; text-decoration: none; }}
  .ticket-title-link:hover, .ticket-title-link:focus-visible {{ text-decoration: underline; }}
  .meta {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 0.6rem; }}
  textarea, input[type=text] {{
    width: 100%; box-sizing: border-box; font-family: inherit; font-size: 0.95rem;
    padding: 0.5rem; border-radius: 6px; border: 1px solid var(--input-border);
    background: var(--input-bg); color: var(--text);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }}
  textarea {{ min-height: 5rem; }}
  input[type=text] {{ margin-bottom: 0.6rem; }}
  textarea:focus, input[type=text]:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus-ring); }}
  .actions {{ margin-top: 0.6rem; display: flex; flex-wrap: wrap; gap: 0.5rem; }}
  .kbd-hint {{ margin-top: 0.4rem; color: var(--muted-2); font-size: 0.78rem; }}
  button {{
    padding: 0.5rem 1rem; border-radius: 6px; border: 1px solid transparent; font-size: 0.9rem;
    cursor: pointer; color: white; box-sizing: border-box;
    display: inline-flex; align-items: center; gap: 0.4rem;
    transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease, transform 0.05s ease, box-shadow 0.15s ease;
  }}
  button:active {{ transform: scale(0.97); }}
  button:focus-visible, a:focus-visible {{ outline: 2px solid var(--focus-ring); outline-offset: 2px; }}
  button:disabled {{ opacity: 0.7; cursor: default; transform: none; }}
  /* One filled primary action per page (Public Reply on the main queue,
     Complete reconnect / Refresh elsewhere) with everything else rendered
     as an outlined "ghost" button -- same six hues as before, just used
     for border+text at rest and only filling solid on hover, so the
     primary action reads as the obvious next step instead of every button
     competing at equal visual weight. */
  .approve {{ background: var(--btn-approve); border-color: var(--btn-approve); }}
  .approve:hover {{ background: var(--btn-approve-hover); border-color: var(--btn-approve-hover); }}
  .refresh {{ background: var(--btn-refresh); border-color: var(--btn-refresh); }}
  .refresh:hover {{ background: var(--btn-refresh-hover); border-color: var(--btn-refresh-hover); }}
  .trigger {{ background: var(--btn-claim); border-color: var(--btn-claim); }}
  .trigger:hover {{ background: var(--btn-claim-hover); border-color: var(--btn-claim-hover); }}
  .internal, .remove, .reject, .claim {{ background: transparent; }}
  .internal {{ color: var(--btn-internal); border-color: var(--btn-internal); }}
  .internal:hover {{ background: var(--btn-internal); color: white; }}
  .remove {{ color: var(--btn-remove); border-color: var(--btn-remove); }}
  .remove:hover {{ background: var(--btn-remove); color: white; }}
  .reject {{ color: var(--btn-reject); border-color: var(--btn-reject); }}
  .reject:hover {{ background: var(--btn-reject); color: white; }}
  .claim {{ color: var(--btn-claim); border-color: var(--btn-claim); }}
  .claim:hover {{ background: var(--btn-claim); color: white; }}
  .empty {{
    color: var(--muted); text-align: center; padding: 2.75rem 1rem;
    border: 1px dashed var(--card-border); border-radius: 14px; font-size: 0.95rem;
  }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  /* Text selection and (Firefox/Chromium) scrollbars otherwise fall back to
     the browser's own default blue/gray regardless of theme -- both read
     straight off the existing --accent/--bg custom properties, so they
     follow whichever theme is active automatically with no per-theme rule
     needed here, the same way every other themed element on this page
     already does. Selection swaps foreground/background (page-bg text on an
     accent-colored highlight) rather than a fixed white-on-accent, since
     --accent is chosen to read well *against* --bg in normal use (links),
     not necessarily as a background with white text on top of it. */
  ::selection {{ background: var(--accent); color: var(--bg); }}
  :root {{ scrollbar-color: var(--accent) var(--bg); }}
  /* Page-transition loading bar (see loading_bar_script) -- this server has
     no client-side router, every navigation is a real full-page reload, so
     there's no genuine "38% loaded" progress to report. What this shows
     instead is the same honest-fake indicator sites with real routers use
     for a slow request: jump partway immediately (feels instant), creep
     the rest of the way via a long CSS transition, and never actually
     reach 100% -- the bar (and the whole DOM it's sitting in) just gets
     replaced the moment the real navigation completes, which is the
     signal that "loading" is over, not anything this bar tracks itself.
     Colored via --accent so it matches whichever theme is active, same as
     every other themed chrome element on this page. */
  #page-loading-bar {{
    position: fixed; top: 0; left: 0; height: 3px; width: 0%;
    background: var(--accent); z-index: 9999; opacity: 0;
    transition: width 5s cubic-bezier(0.1, 0.6, 0.2, 1), opacity 0.15s ease;
  }}
  #page-loading-bar.active {{ opacity: 1; }}
  /* No extra prefers-reduced-motion override needed here -- the existing
     "* {{ transition-duration: 0.001ms !important; }}" rule further down
     already collapses this bar's transition too, same as every other
     animated element on the page. */
  .excerpt {{
    font-style: italic; color: var(--text); background: var(--excerpt-bg);
    border-left: 3px solid var(--excerpt-border); padding: 0.4rem 0.6rem; margin-bottom: 0.6rem;
    font-size: 0.9rem; white-space: pre-wrap; max-height: 20rem; overflow-y: auto; border-radius: 0 6px 6px 0;
  }}
  .flag {{ color: var(--flag-color); font-weight: 600; font-style: normal; }}
  .flag-alert {{ color: var(--flag-alert-color); font-weight: 700; font-style: normal; }}
  .badge-test {{ background: var(--badge-bg); color: white; font-size: 0.72rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 999px; margin-left: 0.5rem; vertical-align: middle; }}
  .badge-priority {{ background: var(--flag-alert-color); color: white; font-size: 0.72rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 999px; margin-left: 0.5rem; vertical-align: middle; }}
  /* HIGH and URGENT used to be the exact same color, distinguishable only
     by reading the badge's text -- this modifier (see _priority_badge_html)
     gives URGENT a distinct, more intense shade of the same alert family
     rather than sharing HIGH's color outright. */
  .badge-priority-urgent {{ background: var(--flag-alert-color-urgent); }}
  .auth-warning {{ background: var(--warning-bg); border-color: var(--warning-border); border-left-color: var(--btn-reject); }}
  .recently-handled {{ margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid var(--divider); }}
  .recently-handled summary {{ cursor: pointer; color: var(--muted); font-size: 0.9rem; font-weight: 600; }}
  .recently-handled summary:focus-visible {{ outline: 2px solid var(--focus-ring); outline-offset: 2px; }}
  .recently-handled ul {{ list-style: none; padding: 0; margin: 0.8rem 0 0 0; }}
  .recently-handled li {{
    padding: 0.4rem 0.5rem; margin: 0 -0.5rem; border-bottom: 1px solid var(--divider);
    font-size: 0.9rem; border-radius: 6px; transition: background-color 0.12s ease;
  }}
  .recently-handled li:hover {{ background: var(--divider); }}
  .recently-handled li:last-child {{ border-bottom: none; }}
  .handled-kind {{ color: var(--muted); }}
  .handled-time {{ color: var(--muted-2); float: right; }}
  @keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(2px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  /* Small ambient "this is watching" cue next to "Next automatic check"
     (see _header_meta_html) -- the page already auto-refreshes and the
     background scan already runs on its own schedule regardless of
     whether anyone's looking, but nothing on the page itself used to
     hint that quietly-live behavior at a glance; this is a soft breathing
     pulse rather than anything urgent (compare .badge-priority-urgent's
     much more saturated, attention-grabbing color -- this is the opposite
     end of that spectrum on purpose). */
  .live-pulse {{
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent); margin-right: 0.3rem; vertical-align: middle;
    animation: livePulse 2.4s ease-in-out infinite;
  }}
  @keyframes livePulse {{
    0%, 100% {{ opacity: 0.35; transform: scale(0.8); }}
    50% {{ opacity: 1; transform: scale(1); }}
  }}
  /* Cross-fades between full page loads (clicking a nav link, submitting a
     form that redirects, the auto-refresh reload) in browsers that support
     the View Transitions API's cross-document mode -- no JS, no change to
     how navigation actually works (still a normal server-rendered page
     load every time), just a smoother visual join between the old page and
     the new one instead of a blank flash. A no-op, not an error, in a
     browser that doesn't recognize @view-transition -- it's simply
     ignored, same as any unsupported at-rule. Default duration overridden
     down to something snappier (0.18s) to match the pace of everything
     else already animated on this page, rather than the browser's own
     slower default. */
  @view-transition {{
    navigation: auto;
  }}
  ::view-transition-old(root), ::view-transition-new(root) {{
    animation-duration: 0.18s;
  }}
  @media (prefers-reduced-motion: reduce) {{
    * {{ animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }}
    /* The universal selector above only reaches real elements -- the
       transition pseudo-elements View Transitions generates
       (::view-transition-old/new/group) live outside the normal DOM and
       aren't matched by "*" in every browser that implements this API, so
       this needs its own explicit opt-out rather than relying on the
       blanket rule above to already cover it. */
    ::view-transition-group(*), ::view-transition-old(*), ::view-transition-new(*) {{
      animation: none !important;
    }}
  }}
</style>
</head>
<body{' class="wide-page"' if wide else ''}>
<div id="page-loading-bar" aria-hidden="true"></div>
<div class="brand">
  <a class="brand-home-link" href="/firstreply">
    {logo_html}
    <h1>Zendesk Workflow Asset for CSA</h1>
    {pending_badge_html}
  </a>
  <div class="brand-controls-row">
    <div class="theme-toggle" role="group" aria-label="Theme">
      <button type="button" data-theme="system" title="Auto (match system)" onclick="setTheme('system')">{_icon("monitor", "icon-sm")}</button>
      <button type="button" data-theme="light" title="Light" onclick="setTheme('light')">{_icon("sun", "icon-sm")}</button>
      <button type="button" data-theme="dark" title="Dark" onclick="setTheme('dark')">{_icon("moon", "icon-sm")}</button>
      <button type="button" data-theme="b68fbf" title="B68FBF" onclick="setTheme('b68fbf')"><span class="theme-swatch"></span></button>
    </div>
  </div>
  <div class="brand-nav-row">
    {_top_nav_html(current_path)}
  </div>
  {header_meta}
  <div class="brand-status-row">
    {_zendesk_status_control_html()}
    {_refresh_button_html(current_path)}
  </div>
</div>
{banner_html}
<main>
{body}
</main>
{refresh_script}
{tick_script}
{theme_script}
{interaction_script}
{loading_bar_script}
{count_up_script}
</body>
</html>""".encode("utf-8")


def _header_meta_html() -> str:
    """The "N pending / last checked / next check / OAuth link" line, shown
    inside the merged sticky header band below the logo/title + Refresh
    button row (see _page()'s header_meta param). Split out from the old
    _status_bar() so that band can be a single sticky container instead of
    two separately-bordered blocks stacked on top of each other."""
    last_checked = _common.last_scan_completed_at()
    last_checked_text = html.escape(last_checked) if last_checked else "never yet"
    next_target = _common.next_scan_target()
    next_check_text = html.escape(_common.next_scan_description())
    counts = _common.summary_counts()
    # Small pill instead of a plain "M posted today" sentence -- same
    # information, but reads as a glanceable stat rather than a sentence you
    # have to parse. A bigger-number KPI-tile version of this (all five
    # summary_counts()) was tried and then explicitly reverted -- back to a
    # plain pill rather than any five-count variant. This used to be a
    # *second* pill alongside "N pending", but
    # that count is also shown on the logo/title's pending_badge_html on
    # every page (see _page()) -- showing it twice in the same header was
    # redundant, so only "posted today" (which has no other home) stays here.
    summary = f'<span class="stat-chip">{counts["posted_today"]} posted today</span>'
    # data-* attributes carry the raw machine-readable values (both server
    # and browser are the same local machine, so no timezone conversion is
    # needed) for tick.js (see _page()) to turn into a live "X ago" / "in Xm
    # Ys" display. The text nodes themselves are the static fallback -- what
    # shows if JS is disabled, and what's briefly visible before the first
    # tick runs on page load.
    last_checked_attr = f' data-iso="{html.escape(last_checked)}"' if last_checked else ""
    next_check_attr = (
        f' data-iso="{html.escape(next_target["target_iso"])}" data-precise="{"1" if next_target["precise"] else "0"}"'
        if next_target["target_iso"]
        else ""
    )
    # The Zendesk OAuth status link used to live here, middot-separated at
    # the end of this sentence -- moved to _top_nav_html() (rendered in the
    # header's brand-right area on every page) instead; this line goes back
    # to being just the counts/timing it was always meant to show.
    # A small pulsing dot ahead of "Next automatic check" -- purely
    # decorative reinforcement of what the text already says (this is the
    # one page that auto-refreshes AND runs its own background scan every
    # couple of minutes), not new information, so aria-hidden keeps it out
    # of a screen reader's way rather than announcing an unlabeled
    # graphic. _header_meta_html is only ever called for /firstreply (see
    # do_GET) -- the one page auto_refresh=True actually applies to -- so
    # this doesn't need its own conditional the way a shared helper would.
    live_pulse_html = '<span class="live-pulse" aria-hidden="true"></span>'
    return (
        f'<div class="meta">{summary}<br>Last checked: <span id="last-checked"{last_checked_attr}>{last_checked_text}</span> '
        f'&middot; {live_pulse_html}Next automatic check: <span id="next-check"{next_check_attr}>{next_check_text}</span></div>'
    )


def _top_nav_html(current_path: str = "") -> str:
    """The nav list of actual working pages, shown in its own vertical
    stack below the logo/title (see _page()'s .brand-nav-row), on every
    page this tool has (main queue, reconnect, /tasks, /flagged,
    /security, /solved) -- not just the main page, the same way the theme
    toggle already appears everywhere (tucked into .brand-controls-row
    instead, off this stack entirely -- see _page()). Consolidates what
    used to be middot-separated text links buried at the end of the meta
    status sentence into something that actually reads as navigation, with
    room for a future page without turning that sentence into a run-on.

    Each link shows its icon AND a visible text label now, not just an
    icon with the label tucked away in a title tooltip -- icon-only worked
    when this was a compact strip inside a header fighting for horizontal
    space, but once the header became a full-height sidebar (see .brand's
    CSS) there's a whole vertical column of room, and six unlabeled
    symbols asking to be memorized (or hovered one at a time) was real
    friction for anyone who hadn't already done that memorizing. Zendesk
    OAuth is no longer one of the six links here at all -- see
    _zendesk_status_control_html below -- since it's a connection-status
    indicator you almost never click on purpose (only when something's
    broken), not a page you navigate to as part of the actual workflow the
    other five are, so it doesn't belong in the same list.

    /flagged and /security now carry a count badge too, but a cache-only
    one rather than a live one: there's no local file tracking "block"/
    "standup"-tagged tickets or the IT-Security group's own recent
    activity the way personal_tasks.json backs Tasks' own badge, so a
    genuinely live count for either would mean calling
    _common.fetch_flagged_tickets/fetch_recent_tickets from this function
    itself, on every page load, not just their own -- and
    fetch_flagged_tickets in particular can page through up to
    FLAGGED_TICKET_SCAN_PAGES pages (1000 tickets) of the IT-Operations
    group on a cold cache. That's a real cost a page with nothing to do
    with Discussion (say, /tasks) shouldn't ever risk paying just to
    draw a header badge. _common.cached_flagged_ticket_count/
    cached_security_ticket_count read those two functions' own caches
    without ever calling Zendesk themselves -- so the badge shows the last
    real count for as long as an actual visit to /flagged or /security
    keeps that cache warm (SECURITY_CACHE_TTL_SECONDS/
    FLAGGED_CACHE_TTL_SECONDS, 120 seconds each), and quietly disappears
    once it goes stale rather than forcing a fresh scan. In practice that
    means the badge is closer to "still warm from your last visit" than
    "always current" the way Tasks' own count is -- an accepted
    trade-off for keeping every other page's render free of a Zendesk
    call it has no other reason to make.

    Two things layered on top of the plain link list: an "active" state on
    whichever link matches current_path, so it's obvious at a glance which
    page you're already on (mirroring the theme toggle's own .active
    treatment); and a small count badge per link -- the number of
    not-yet-done personal tasks on Tasks, and the cache-only Discussion and
    Security counts described above -- riding at the end of the row rather
    than crowding the label."""
    tasks_count = len(_common.draft_personal_tasks()) + len(_common.open_personal_tasks())
    flagged_count = _common.cached_flagged_ticket_count()
    security_count = _common.cached_security_ticket_count(_common.IT_SECURITY_GROUP_ID)
    sla_breach_count = _common.cached_sla_breach_count()

    def _badge(count, warn=False):
        if not count:
            return ""
        cls = "nav-badge nav-badge-warn" if warn else "nav-badge"
        return f'<span class="{cls}">{count}</span>'

    def _link(path, icon, label, badge_html=""):
        is_active = current_path == path
        active_class = " active" if is_active else ""
        aria = ' aria-current="page"' if is_active else ""
        # The label is real visible text now (a .top-nav-label span, not a
        # title attribute) -- see the docstring above for why. flex:1 on
        # that span (its own CSS) is what pushes badge_html to the row's
        # far end without needing an extra wrapper here.
        return (
            f'<a class="top-nav-link{active_class}" href="{path}"{aria}>'
            f'{_icon(icon)}<span class="top-nav-label">{html.escape(label)}</span>{badge_html}</a>'
        )

    # One flat list, ordered roughly by how often you'd actually reach for
    # it -- First Reply first (the original main queue, and still the
    # single most-worked page day to day), then Overview as the "check
    # this before anything else" glance across the rest, then the two
    # other working queues (Tasks, Discussion), then Security, then
    # Recently Solved last as the one you check in on rather than work
    # from day to day. No more splitting this into "status" vs "workflow"
    # clusters with a divider between them -- that split existed to set
    # Zendesk OAuth apart from the pages you actually work in, and now
    # that OAuth has its own separate control entirely (see
    # _zendesk_status_control_html), every remaining link here IS a
    # working page, so there's no second category left to separate from
    # the first.
    nav_links = (
        # No badge here despite this being the page with arguably the
        # single most actionable count of any of them -- pending_badge_html
        # (see _page()'s logo/title) already shows "N pending" on every
        # page, so a matching badge on this link would be the exact
        # duplicate-count problem that got the old second "N pending"
        # stat-chip removed from the header meta line; one visible count
        # is enough.
        _link("/firstreply", "reply", "First Reply")
        # Overview sits second -- it's the "check this before anything
        # else" glance across all four of the pages below it (see
        # _render_overview_page), so it goes near the top of the list
        # rather than slotted in by topic like the rest.
        + _link("/overview", "grid", "Overview")
        # SLA sits right after Overview -- another "check this before you
        # dig in" glance page rather than a queue you work from day to
        # day, and its badge (unlike Tasks/Discussion/Security's own) is a
        # warn-colored count specifically because a breach is something
        # that needs escalating, not just a to-do count.
        + _link("/sla", "clock", "SLA", _badge(sla_breach_count, warn=True))
        + _link("/tasks", "check-square", "Tasks", _badge(tasks_count))
        + _link("/flagged", "flag", "Discussion", _badge(flagged_count))
        + _link("/security", "shield", "Security", _badge(security_count))
        # No badge here on purpose, unlike the three above -- those counts
        # are all "does this need a look" signals (open tasks, blocked/
        # standup tickets, Security's recent activity). Recently Solved
        # would just show a static number that maxes out at
        # _common.RECENTLY_SOLVED_LIMIT the moment its cache is warm --
        # nothing about that number says "go look at this," so a badge
        # here would just be visual noise dressed up as a signal.
        + _link("/solved", "check-circle", "Recently Solved")
    )
    return f'<div class="top-nav">{nav_links}</div>'


def _zendesk_status_control_html() -> str:
    """A small icon-only connection-status control living in its own
    .brand-status-row (see _page()), pinned to the very bottom of the
    sidebar on wide viewports -- below the nav list, not alongside the
    theme toggle in .brand-controls-row and not among the working pages in
    _top_nav_html's list above. Zendesk OAuth is a standing "is this
    healthy" indicator you almost never click on purpose -- only when
    something's broken -- much closer in spirit to the theme toggle (a
    standing per-browser control you set once and mostly ignore) than to
    Tasks/Discussion/Security/Recently Solved, which are pages you
    actually navigate to as part of the day's work; living at the bottom,
    below everything you actually use day to day, reinforces that it's a
    standing status check rather than another destination. Icon-only even
    after the main nav list gained its labels back, since this one control
    genuinely is meant to fade into the background until it needs
    attention -- the title tooltip plus (when broken) the warning color
    and border are what carry that attention, not a permanent label."""
    auth_ok = _common.auth_status().get("ok", True)
    warn_class = "" if auth_ok else " zendesk-status-warn"
    title = "Zendesk OAuth" if auth_ok else "Zendesk OAuth -- isn't connected right now"
    return (
        f'<a class="zendesk-status-link{warn_class}" href="/reconnect" title="{html.escape(title)}">'
        f'{_icon("key", "icon-sm")}</a>'
    )


def _refresh_button_html(return_to: str = "/firstreply") -> str:
    """The Refresh button -- rendered unconditionally in .brand-status-row
    (see _page()) next to the Zendesk OAuth status control, on every page,
    rather than only on the main queue via a per-page header_action param
    the way it originally worked (split out from the old _status_bar() to
    sit beside the logo/title). Grouping it with the OAuth control instead
    of leaving it main-page-only reads better now that both are "standing
    utility actions available anywhere" rather than the main queue's own
    page-specific action -- which is also why the label shrank from
    "Refresh from Zendesk now" to just "Refresh data": a persistent,
    always-visible button earns a shorter, more generic name than a
    one-off primary action would.

    return_to (see the hidden field below, and do_POST's ["refresh"]
    branch) is what actually makes "available on every page" work rather
    than just LOOK like it does -- without it, clicking Refresh while on
    /tasks would still bounce you back to /firstreply, since a plain POST
    has no other way to know which page it was submitted from. _page()
    passes its own current_path through here so the redirect after
    refreshing lands you back on the exact page you clicked from, the
    same return_to pattern _render_readonly_ticket_row's Claim button
    already established for the same reason."""
    return f"""<form method="post" action="/refresh">
      <input type="hidden" name="return_to" value="{html.escape(return_to)}">
      <button class="refresh" type="submit" data-loading-text="Refreshing…">{_icon("refresh")}<span>Refresh data</span></button>
    </form>"""


def _filter_box_html(target_selector: str, placeholder: str = "Filter by subject or requester…") -> str:
    """A small filter-as-you-type input, reused as-is on the main queue,
    /flagged, /security, /solved, and the "Recently worked on" sidebar --
    every one of those pages/panels is a list of same-shaped cards with no
    way to narrow it down beyond the browser's own page search, which
    can't be scoped to just one list on a page with other content around
    it (the sidebar sits right next to the main queue's own list, for
    instance) and doesn't show a "how many matched" count.

    Purely client-side (see interaction_script's filter-box handler,
    below in _page()) -- `target_selector` is a plain CSS selector
    matched against document.querySelectorAll at filter time, checked
    against each matched element's textContent, no request round-trip
    and no server-side filtering state to keep in sync. data-filter-target
    lives on the wrapping div rather than the input itself so the JS only
    needs one query to find both the input and the count span together."""
    safe_target = html.escape(target_selector)
    safe_placeholder = html.escape(placeholder)
    return f"""<div class="filter-box" data-filter-target="{safe_target}">
  {_icon("search", "icon-sm")}
  <input type="search" class="filter-input" placeholder="{safe_placeholder}" aria-label="{safe_placeholder}">
  <span class="filter-count" aria-live="polite"></span>
</div>"""


def _auth_warning_html() -> str:
    """Shown on the main page whenever the last real check (a cron tick, or
    a manual post/refresh) found the OAuth token broken -- read from a
    cheap cached file, not a live probe, so GET / still never touches
    Zendesk itself just to render this."""
    status = _common.auth_status()
    if status.get("ok", True):
        return ""
    reason = html.escape(status.get("reason", "unknown"))
    checked_at = html.escape(status.get("checked_at", ""))
    return f"""
<div class="banner auth-warning">
  Zendesk connection needs attention: {reason} (last checked {checked_at}).
  <a href="/reconnect">Reconnect now</a> -- no Cowork session needed.
</div>"""


def _render_reconnect_page(banner: str = "") -> bytes:
    """Runs the same OAuth authorization-code flow as begin_oauth_authorization/
    complete_oauth_authorization (used previously via a live Cowork session) --
    but entirely through this local page, so reconnecting never depends on
    Cowork being open. See _common.save_oauth_pending's docstring for why the
    state has to be persisted to a file between these two steps."""
    pending = _common.load_oauth_pending()
    if pending:
        auth_url = html.escape(pending["authorization_url"])
        body = f"""
<div class="narrow-page">
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("reconnect")}Reconnect Zendesk OAuth</h2>
<p>1. Click below and approve access in Zendesk:</p>
<p><a href="{auth_url}" target="_blank" rel="noopener">Authorize Zendesk access</a></p>
<p>2. Zendesk will redirect your browser to a page that won't load -- that's
expected, nothing is listening on that address. Copy the full URL from your
browser's address bar (it will look like
<code>https://localhost/callback?code=...&amp;state=...</code>) and paste it
below.</p>
<form method="post" action="/reconnect/complete">
  <input type="text" name="callback_url" placeholder="https://localhost/callback?code=...&state=...">
  <div class="actions">
    <button class="approve" type="submit" data-loading-text="Connecting…">{_icon("key")}<span>Complete reconnect</span></button>
  </div>
</form>
<form method="post" action="/reconnect/start" style="margin-top: 1rem;">
  <button class="reject" type="submit" data-loading-text="Starting…">{_icon("refresh")}<span>Start over (get a fresh link)</span></button>
</form>
<p class="meta">This link expires after {_common.OAUTH_PENDING_TTL_MINUTES} minutes -- if it's gone stale, start over.</p>
</div>
"""
    else:
        body = f"""
<div class="narrow-page">
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("reconnect")}Reconnect Zendesk OAuth</h2>
<p>No reconnect in progress.</p>
<form method="post" action="/reconnect/start">
  <button class="refresh" type="submit" data-loading-text="Starting…">{_icon("key")}<span>Start Zendesk reconnect</span></button>
</form>
</div>
"""
    return _page(body, banner=banner, current_path="/reconnect")

def _priority_badge_html(priority: str) -> str:
    """The small colored "HIGH"/"URGENT" pill next to a ticket's subject
    (Zendesk's own priority field, surfaced free-of-charge -- see the call
    site in _render_ticket). Only shown
    for "high"/"urgent"; "normal"/"low" get no badge at all, same
    reasoning as the TEST badge elsewhere -- a badge on every single card
    would be noise most of the time.

    HIGH and URGENT used to render in the exact identical color
    (--flag-alert-color for both), distinguishable only by actually
    reading the text -- which defeats the point of a color-coded severity
    badge in the first place. URGENT now gets the .badge-priority-urgent
    modifier (--flag-alert-color-urgent, a deeper/more saturated red in
    every theme) so the two are visually distinct at a glance, not just by
    label."""
    priority = (priority or "").lower()
    if priority not in ("high", "urgent"):
        return ""
    css_class = "badge-priority badge-priority-urgent" if priority == "urgent" else "badge-priority"
    return f'<span class="{css_class}">{_topic_icon("priority-alert", "topic-icon-inline")}{html.escape(priority.upper())}</span>'


def _titled_link_html(subject_html: str, url: str) -> str:
    """Wraps an already-escaped ticket subject in the same live-Zendesk
    href its neighboring "#tid" link already uses (see every call site
    below: _render_ticket, _render_confirm_page, _render_readonly_ticket_row,
    _render_worked_on_sidebar) -- before this, only the small "#tid" number
    itself opened the ticket, even though the much larger, more-clicked-on
    title text sitting right next to it looked just as clickable and
    wasn't. A single shared helper instead of repeating this at each call
    site so all four stay in sync if the styling or target ever needs to
    change.

    Deliberately styled via .ticket-title-link to look like ordinary text
    -- color: inherit, no underline by default -- rather than picking up
    the same blue/underlined treatment as "#tid". The goal here is just
    fixing the surprisingly common miss-click of aiming for the title
    instead of the number, not visually announcing "this is a link" on
    every card; a hover/focus underline (see the CSS rule alongside .tid)
    is enough of a cue once someone's pointer is actually on it.

    Falls back to the plain subject text, unwrapped, when there's no url.

    NOT used for a personal task's title (draft or open) -- see
    _task_title_link_html below instead. A task is a different kind of
    thing from a ticket shown elsewhere in this tool: Discussion/Security/
    the main queue are all live Zendesk tickets that exist independently
    of this tool and are primarily managed in Zendesk itself, so their
    title opens the same place their "#tid" does. A task, promoted or
    not, is managed HERE -- Quick Solve/Mark done/Promote/Discard/Edit are
    all only available on /tasks -- so its title should take you back to
    this app, to that exact row, not out to Zendesk."""
    if not url:
        return subject_html
    return f'<a href="{html.escape(url)}" target="_blank" class="ticket-title-link">{subject_html}</a>'


def _task_title_link_html(subject_html: str, local_id: str) -> str:
    """Links a task's title (draft or open, wherever it's shown -- the
    /tasks page's own rows, or the compact previews on /overview's Tasks
    tile) to that exact row's anchor on /tasks (#task-<local_id>, set on
    the row's own wrapper by _render_draft_task_row/_render_open_task_row)
    -- same-tab, no target="_blank", since this is normal in-app
    navigation, not a hop out to another site.

    Deliberately NOT the same href an open task's "#tid" link uses (see
    _titled_link_html's docstring for the reasoning): the title always
    means "take me to this task, in this app," while "#tid" (when a task
    has one) means "take me to the Zendesk ticket it became." Two
    different destinations, two different affordances, so clicking each
    does what its own label suggests rather than one silently overriding
    the other.

    Landing back on /tasks with a matching #task-<local_id> fragment
    scrolls to that row natively (no JS needed) and briefly highlights it
    via the .mini-ticket:target CSS rule -- see the taskHighlight keyframe
    alongside .mini-ticket's other rules."""
    return f'<a href="/tasks#task-{html.escape(str(local_id))}" class="ticket-title-link">{subject_html}</a>'


def _duration_bucket(then: datetime) -> str:
    """Shared bucketing core for _relative_age (a pending ticket's
    "waiting") and _updated_age (a ticket's Zendesk-side "updated") --
    'just now' / 'N min' / 'Nh' / 'Nd' for a naive local datetime. Callers
    parse their own timestamp into a naive local datetime first (see
    both functions below) since created_at and Zendesk's updated_at
    aren't in the same format, and shouldn't duplicate this threshold
    logic just because they start from different string shapes."""
    seconds = max((datetime.now() - then).total_seconds(), 0)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours}h"
    days = int(seconds // 86400)
    return f"{days}d"


def _relative_age(iso_ts: str) -> str:
    """Turn a created_at timestamp into 'waiting Nh' style text, so the
    oldest, most overdue tickets are obviously overdue at a glance rather
    than requiring you to compare raw timestamps yourself. created_at here
    is this tool's own local "first seen" timestamp (naive, no UTC
    offset) -- see _updated_age below for Zendesk's own (UTC) updated_at."""
    if not iso_ts:
        return ""
    try:
        then = datetime.fromisoformat(iso_ts)
    except Exception:
        return ""
    duration = _duration_bucket(then)
    return duration if duration == "just now" else f"waiting {duration}"


def _updated_age(iso_ts: str) -> str:
    """Turn a ticket's Zendesk-side updated_at into 'updated Nh ago' style
    text -- refreshed every scan by recheck_pending_for_assignment, so a
    ticket with genuine Zendesk-side activity (a note added, a field
    changed) but no reply yet reads differently on the card from one
    that's been completely silent since this tool first saw it. Unlike
    created_at above, this is Zendesk's own timestamp -- UTC with a "Z"
    suffix -- so it needs converting to local time before it's comparable
    to created_at's already-local, already-naive value."""
    if not iso_ts:
        return ""
    try:
        then_utc = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        then_local = then_utc.astimezone().replace(tzinfo=None)
    except Exception:
        return ""
    duration = _duration_bucket(then_local)
    return "updated just now" if duration == "just now" else f"updated {duration} ago"


def _solved_age(iso_ts: str) -> str:
    """Turn a solved ticket's Zendesk-side updated_at into 'solved Nh ago'
    style text, for /solved's rows -- same UTC-to-local conversion as
    _updated_age just above (this IS that same field, just relabeled: there
    is no separate "solved_at" field in Zendesk's own ticket payload, and
    updated_at is a reasonable proxy for "when it was solved" on a ticket
    that's actually reached that status)."""
    if not iso_ts:
        return ""
    try:
        then_utc = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        then_local = then_utc.astimezone().replace(tzinfo=None)
    except Exception:
        return ""
    duration = _duration_bucket(then_local)
    return "solved just now" if duration == "just now" else f"solved {duration} ago"


def _format_clock_time(iso_ts: str) -> str:
    """Turn a posted_at timestamp into plain clock time ('2:15 PM') for the
    "Replied today" section -- unlike _relative_age (which is for PENDING
    tickets and deliberately ticks as time passes), a posted ticket's time
    is a fixed historical fact, so an absolute clock time reads better than
    a "how long ago" figure that would otherwise just keep climbing every
    time you reload the page."""
    if not iso_ts:
        return ""
    try:
        return datetime.fromisoformat(iso_ts).strftime("%-I:%M %p")
    except Exception:
        return ""


def _task_added_label(iso_ts: str) -> str:
    """'Aug 15, 3:10 PM' -- a personal task can sit open for days (unlike a
    same-day posted reply), so unlike _format_clock_time this needs a date
    alongside the clock time, not just the time alone."""
    if not iso_ts:
        return ""
    try:
        return datetime.fromisoformat(iso_ts).strftime("%b %-d, %-I:%M %p")
    except Exception:
        return ""


def _requester_avatar_prefix(item: dict) -> str:
    """Avatar + name prefix for a compact recently-handled/recent-replies
    row -- same confirmed-only gating as _requester_line's ticket-card
    version (see above), so these list rows pick up the same visual
    consistency the redesign gave the live queue cards, without lending a
    guessed name false confidence via an avatar it hasn't earned."""
    name = item.get("requester_name")
    if item.get("name_confidence") == "confirmed" and name:
        return f'{_avatar_html(name)}{html.escape(name)} — '
    return ""


def _render_recently_handled() -> str:
    """"Replied today" section: every ticket actually posted through this
    tool (public or internal) today, most-recent first. Collapsed by
    default via a native <details> element -- no JS needed for the
    expand/collapse, and it stays out of the way of the pending queue
    (the thing that actually needs your attention) while still being one
    click away as today's record of what you've already handled.
    Deliberately excludes Removed and already-handled-elsewhere tickets --
    this section is specifically about replies YOU sent, not everything
    that left the queue."""
    items = _common.posted_today_items()
    if not items:
        return ""

    rows = []
    for item in items:
        tid = item.get("ticket_id")
        subject = html.escape(item.get("subject", ""))
        url = item.get("url") or ""
        link = f'<a href="{html.escape(url)}" target="_blank">#{tid}</a>' if url else f"#{tid}"
        kind = "Public Reply" if item.get("posted_public") else "Internal Comment"
        time_str = html.escape(_format_clock_time(item.get("posted_at", "")))
        requester_prefix = _requester_avatar_prefix(item)
        rows.append(
            f'<li>{link} — {requester_prefix}{subject} '
            f'<span class="handled-kind">({html.escape(kind)})</span> '
            f'<span class="handled-time">{time_str}</span></li>'
        )

    return f"""
<details class="recently-handled" data-storage-key="csa-recently-handled-open">
  <summary>Replied today ({len(items)})</summary>
  <ul>
    {"".join(rows)}
  </ul>
</details>"""


def _render_worked_on_sidebar(items: list, capped: bool = False, zendesk_unavailable: bool = False) -> str:
    """Persistent left-hand column on the main queue page (/firstreply) listing the
    last 30 tickets actually worked on, most-recent first -- via
    _common.fetch_recently_worked_on, which now covers two sources: replies
    sent THROUGH this tool (the original, always-available local scope) and,
    when Zendesk is reachable, replies sent directly in Zendesk that this
    tool has no local record of at all (a live scan of the IT-Operations
    group's comments -- see that function's own docstring for the scan
    budget and the "already tracked locally" simplification it makes).

    This absorbs what used to be a separate "Last N replies" collapsed
    <details> section (limit=10, tucked below the queue) -- the two were
    near-total overlapping views of the exact same data (this one is a
    longer-running, always-visible superset), so keeping both was
    redundant rather than complementary. "Replied today" just below this
    in the main column stays separate on purpose: it's scoped to today
    only and resets at midnight, a genuinely different question ("what
    did I do today" vs. "what have I done most recently") than this
    sidebar answers.

    zendesk_unavailable/capped surface the same two honesty signals
    fetch_recently_worked_on returns -- a small muted note under the
    heading when either is true, so a quiet Zendesk outage (or a scan that
    hit its own budget) reads as "might be missing something," not as
    "there's nothing more to see."

    Wide enough (see .main-with-sidebar's CSS) to show most subjects on
    one line, with longer ones wrapping rather than truncating -- see
    .worked-on-subject. Carries the same confirmed-requester avatar prefix
    the old "Last N replies" section had (_requester_avatar_prefix), so
    folding that section in here doesn't lose it."""
    note = ""
    if zendesk_unavailable:
        note = (
            '<p class="meta">Only showing replies sent through this tool right now -- '
            "Zendesk isn't reachable to check for ones sent directly there.</p>"
        )
    elif capped:
        note = (
            '<p class="meta">This may not include every reply sent directly in Zendesk -- '
            "the scan for those stopped early.</p>"
        )

    if not items:
        return (
            '<div class="security-panel worked-on-sidebar">'
            '<h2 class="column-heading">Recently worked on</h2>'
            f"{note}"
            '<p class="empty">Nothing posted yet.</p>'
            '</div>'
        )

    rows = []
    for item in items:
        tid = item.get("ticket_id")
        subject = html.escape(item.get("subject") or f"Ticket #{tid}")
        url = item.get("url") or ""
        link = f'<a href="{html.escape(url)}" target="_blank">#{tid}</a>' if url else f"#{tid}"
        subject = _titled_link_html(subject, url)
        kind = "Public reply" if item.get("posted_public") else "Internal comment"
        posted_at = item.get("posted_at", "")
        try:
            when = datetime.fromisoformat(posted_at).strftime("%b %-d, %-I:%M %p")
        except Exception:
            when = ""
        requester_prefix = _requester_avatar_prefix(item)
        meta = html.escape(kind) + (f" · {html.escape(when)}" if when else "")
        rows.append(
            f'<li class="worked-on-item">'
            f'<div class="worked-on-subject">{link} — {requester_prefix}{subject}</div>'
            f'<div class="worked-on-meta">{meta}</div>'
            f'</li>'
        )

    return f"""
<div class="security-panel worked-on-sidebar">
  <h2 class="column-heading">Recently worked on ({len(items)})</h2>
  {note}
  {_filter_box_html(".worked-on-item", "Filter…")}
  <ul class="worked-on-list">
    {"".join(rows)}
  </ul>
</div>"""


def _render_confirm_page(ticket_id, draft_text: str, public: bool, reply_check: dict, subject: str, url: str) -> bytes:
    """Interstitial shown instead of posting outright when check_already_replied
    finds a public comment already on the ticket -- for BOTH a public reply
    and an internal comment (previously public silently auto-skipped the
    post and internal silently posted anyway with just a warning noted
    after the fact; neither asked first). Requires an explicit second click
    (the hidden confirm=yes field) to actually post, so the decision is
    always a human one made with the "already replied" reason in front of
    them, not a default this tool picked on their behalf.

    The (possibly edited) draft text rides along in a hidden field rather
    than a fresh textarea -- this page is a confirmation step, not another
    editing opportunity; if the wording still needs changing, cancel back
    to the main page and edit there first."""
    kind = "public reply" if public else "internal comment"
    action_path = "post_public" if public else "post_internal"
    subject_html = html.escape(subject) if subject else ""
    link = f'<a href="{html.escape(url)}" target="_blank" class="tid">#{ticket_id}</a>' if url else f'<span class="tid">#{ticket_id}</span>'
    subject_html = _titled_link_html(subject_html, url)
    draft_escaped = html.escape(draft_text)
    reason_html = html.escape(reply_check.get("reason", "an existing reply"))
    button_class = "approve" if public else "internal"
    button_icon = _icon("send") if public else _icon("lock")
    return _page(f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<div class="ticket">
  <h2>{link} — {subject_html}</h2>
  <p class="flag-alert">This ticket already appears to have {reason_html}.</p>
  <p>Do you still want to post this {html.escape(kind)}?</p>
  <div class="excerpt">{draft_escaped}</div>
  <div class="actions">
    <form method="post" action="/{action_path}/{ticket_id}">
      <input type="hidden" name="draft" value="{draft_escaped}">
      <input type="hidden" name="confirm" value="yes">
      <button class="{button_class}" type="submit" data-loading-text="Posting…">{button_icon}<span>Yes, post the {html.escape(kind)} anyway</span></button>
    </form>
    <form method="post" action="/dismiss/{ticket_id}">
      <button class="remove" type="submit" data-loading-text="Removing…" onclick="return confirm('Remove #{ticket_id} from the pending queue instead? This does not post anything to Zendesk.');">{_icon("trash")}<span>Remove instead (don't post)</span></button>
    </form>
  </div>
</div>""")


def _requester_line(item: dict) -> str:
    """Show a resolved name instead of a bare id where we have one -- and
    make an unconfirmed guess visibly different from a confirmed one, since
    an unconfirmed name is exactly the case where a first-name collision (or
    a wrong guess entirely) could slip an incorrect greeting past you.

    Also surfaces *why* a name wasn't confirmed (name_confidence_reason),
    since "verify before approving" alone doesn't tell you whether there was
    simply nothing to go on (e.g. a one-line test ticket) versus a name that
    was found but didn't match this requester -- those call for different
    amounts of scrutiny."""
    confidence = item.get("name_confidence")
    reason = item.get("name_confidence_reason")
    name = item.get("requester_name")
    requester_id = html.escape(str(item.get("requester_id", "unknown")))

    if confidence is None:
        # Drafted before this field existed (an older pending_drafts.json
        # row) -- render plainly rather than implying a check was run.
        return f"Requester id: {requester_id}"
    if confidence == "confirmed":
        # The only branch that gets an avatar bubble -- this is the one
        # case we're confident enough in the name to lend it that extra
        # visual weight; every other branch below already carries its own
        # "verify before approving" flag, and an avatar next to a guessed
        # name would undercut that warning by looking more certain than it is.
        return f"Requester: {_avatar_html(name or '')}{html.escape(name or 'unknown')}"
    if confidence == "unconfirmed":
        detail = (
            "Zendesk user search was unreachable during the scan"
            if reason == "search_unavailable"
            else "unconfirmed"
        )
        return (
            f'Requester: {html.escape(name or "unknown")} '
            f'<span class="flag">{detail} &mdash; verify before approving</span>'
        )
    # confidence == "none"
    if reason == "no_match" and name and name != "there":
        return (
            f'Requester: possibly {html.escape(name)} (id {requester_id}) '
            '<span class="flag">name found in the ticket text but didn\'t match '
            'this requester &mdash; verify before approving</span>'
        )
    return (
        f'Requester: unknown (id {requester_id}) '
        '<span class="flag">no name found in the ticket text &mdash; verify before approving</span>'
    )


# Used by the main queue's "Priority first" sort toggle (see do_GET's
# "/firstreply" handler) to rank Zendesk's own priority field -- the same field
# _render_ticket already surfaces as a badge for "high"/"urgent". Missing
# or unrecognized values (blank, "low", "normal", or something Zendesk adds
# later that this tool doesn't know about yet) all rank alongside "normal"
# rather than erroring, since the fallback sort key (created_at) still
# orders them sensibly relative to each other either way.
_PRIORITY_RANK = {"urgent": 3, "high": 2, "normal": 1, "low": 0}


def _priority_sort_key(item: dict) -> tuple:
    rank = _PRIORITY_RANK.get((item.get("priority") or "").lower(), 1)
    return (-rank, item.get("created_at", ""))


def _render_ticket(item: dict) -> str:
    tid = item["ticket_id"]
    subject = html.escape(item.get("subject", ""))
    url = item.get("url") or ""
    link = f'<a href="{html.escape(url)}" target="_blank" class="tid">#{tid}</a>' if url else f'<span class="tid">#{tid}</span>'
    subject = _titled_link_html(subject, url)
    # When someone else is already working this ticket in Zendesk, don't
    # pre-fill the auto-drafted acknowledgment -- the stored draft in
    # pending_drafts.json is left untouched (so it's still there if the
    # assignee clears later), but the box renders empty here so nothing
    # gets sent on autopilot over a colleague's ticket; a reply only goes
    # out if a human deliberately types one in.
    draft = "" if item.get("assigned_to_other") else html.escape(item.get("draft", ""))
    draft_placeholder = (
        'No draft filled in -- someone else is already working this ticket (see notice above).'
        if item.get("assigned_to_other")
        else ""
    )
    age = _relative_age(item.get("created_at", ""))
    age_html = f" &middot; {age}" if age else ""
    updated_age = _updated_age(item.get("updated_at", ""))
    updated_html = f" &middot; {updated_age}" if updated_age else ""
    excerpt = item.get("description_excerpt")
    excerpt_html = f'<div class="excerpt">&ldquo;{html.escape(excerpt)}&rdquo;</div>' if excerpt else ""
    badge_html = (
        f'<span class="badge-test">{html.escape((item.get("test_tag") or "TEST").upper())}</span>'
        if item.get("is_test_ticket")
        else ""
    )
    # Zendesk's own priority field -- already included free-of-charge in
    # every ticket payload (see build_drafts in _common.py), just never
    # surfaced on a card before. Only shown for "high"/"urgent" -- the two
    # priorities that actually change how urgently you'd want to act --
    # rather than a badge on every single card for "normal"/"low", which
    # would just be noise most of the time (mirrors why the TEST badge
    # above is conditional too, not shown on every card).
    priority_html = _priority_badge_html(item.get("priority"))
    # Zendesk's own Assignee field, surfaced as a heads-up: someone else is
    # already working this ticket directly in Zendesk, even though no
    # public reply has posted yet (so it's still sitting here as a
    # candidate). Purely informational -- doesn't block or gate posting the
    # way an existing reply does (see _post's confirm flow below) -- just
    # gives you the context to decide whether drafting a reply here would
    # step on a colleague's toes.
    assignee_html = ""
    if item.get("assigned_to_other"):
        who = html.escape(item.get("assignee_name") or f'user id {item.get("assignee_id")}')
        assignee_html = f'<p class="flag-alert">Also being worked on in Zendesk — currently assigned to {who}.</p>'
    # Claim button: the direct complement to the badge above. Hidden only
    # when the ticket is already assigned to this tool's own account --
    # shown for both an unassigned ticket and one assigned to someone
    # else, since both are cases where clicking it changes something. The
    # someone-else case asks for confirmation first (taking over a
    # colleague's ticket is a bigger deal than claiming an unclaimed one);
    # the unassigned case doesn't need to ask, since there's nothing to
    # take over.
    already_ours = bool(item.get("assignee_id")) and not item.get("assigned_to_other")
    claim_html = ""
    if not already_ours:
        confirm_attr = ""
        if item.get("assigned_to_other"):
            who_js = _js_str(item.get("assignee_name") or f'user id {item.get("assignee_id")}')
            confirm_attr = (
                f" onclick=\"return confirm('This ticket is currently assigned to {who_js} "
                f"in Zendesk — claim it for yourself instead?');\""
            )
        claim_html = (
            f'<button class="claim" formaction="/claim/{tid}" type="submit" '
            f'data-loading-text="Claiming…"{confirm_attr}>{_icon("user-check")}<span>Claim for yourself</span></button>'
        )
    return f"""
<div class="ticket" id="ticket-{tid}">
  <p class="ticket-eyebrow">Ticket</p>
  <h2>{link} — {subject}{badge_html}{priority_html}</h2>
  <div class="meta">{_requester_line(item)}{age_html}{updated_html}</div>
  {assignee_html}
  {excerpt_html}
  <form method="post" action="/post_public/{tid}">
    <textarea name="draft" data-shortcut-submit="true" placeholder="{draft_placeholder}">{draft}</textarea>
    <div class="actions">
      <button class="approve" type="submit" data-loading-text="Posting…">{_icon("send")}<span>Public Reply</span></button>
      <button class="internal" formaction="/post_internal/{tid}" type="submit" data-loading-text="Posting…">{_icon("lock")}<span>Internal Comment</span></button>
      <button class="remove" formaction="/dismiss/{tid}" type="submit" data-loading-text="Removing…" onclick="return confirm('Remove #{tid} from the pending queue? This does not post anything to Zendesk, and this tool won\\'t re-detect it later even if it still has no reply there -- Undo is available for a few seconds right after, if this was a misclick.');">{_icon("trash")}<span>Remove</span></button>
      {claim_html}
    </div>
    <div class="kbd-hint">&#8984;Enter to send as Public Reply</div>
  </form>
</div>"""


def _render_readonly_ticket_row(item: dict, return_to: str = "/firstreply") -> str:
    """One read-only ticket row: link, subject, status, requester,
    assignee, and a Claim button when it isn't already ours -- shared by
    the IT-Security page (_render_security_page/_render_security_column)
    and the /flagged page (_render_flagged_page), since both are the same
    kind of thing: a live Zendesk ticket shown exactly as Zendesk has it
    right now, with no review/draft/post workflow attached the way the
    main queue's own _render_ticket has.

    `return_to` becomes a hidden field on the Claim form so the POST
    handler (Handler.do_POST's "claim" branch) can redirect back to
    whichever page this row was actually rendered on, rather than always
    landing on "/" -- without it, claiming from /security or /flagged
    would silently bounce you back to the main queue instead of back to
    the page you were just looking at.

    An item carrying "solved_at" (only /solved's rows do) gets a "solved
    Nh ago" segment appended to the meta line via _solved_age -- everything
    else about the row (including "Assignee: You" and no Claim button,
    since every /solved item is already_ours=True) falls out of the exact
    same fields /security and /flagged already populate, no special case
    needed beyond this one optional segment."""
    tid = item.get("ticket_id")
    subject = html.escape(item.get("subject", ""))
    url = item.get("url") or ""
    link = f'<a href="{html.escape(url)}" target="_blank" class="tid">#{tid}</a>' if url else f'<span class="tid">#{tid}</span>'
    subject = _titled_link_html(subject, url)
    status = html.escape((item.get("status") or "").capitalize() or "Unknown")
    requester_name = item.get("requester_name")
    requester = (
        f'{_avatar_html(requester_name)}{html.escape(requester_name)}'
        if requester_name
        else html.escape(f'user id {item.get("requester_id") or "unknown"}')
    )

    assignee_id = item.get("assignee_id")
    already_ours = item.get("already_ours")
    if already_ours:
        assignee_label_html = "You"
    elif item.get("assignee_name"):
        assignee_label_html = f'{_avatar_html(item["assignee_name"])}{html.escape(item["assignee_name"])}'
    elif assignee_id:
        assignee_label_html = html.escape(f"user id {assignee_id}")
    else:
        assignee_label_html = "Unassigned"

    claim_html = ""
    if not already_ours:
        confirm_attr = ""
        if assignee_id:
            who_js = _js_str(item.get("assignee_name") or f"user id {assignee_id}")
            confirm_attr = (
                f" onclick=\"return confirm('This ticket is currently assigned to {who_js} "
                f"in Zendesk — claim it for yourself instead?');\""
            )
        claim_html = (
            f'<form method="post" action="/claim/{tid}" class="mini-claim-form">'
            f'<input type="hidden" name="return_to" value="{html.escape(return_to)}">'
            f'<button type="submit" class="claim" data-loading-text="Claiming…"{confirm_attr}>{_icon("user-check", "icon-sm")}<span>Claim</span></button>'
            f"</form>"
        )

    solved_meta = f" &middot; {html.escape(_solved_age(item['solved_at']))}" if item.get("solved_at") else ""

    return f"""
<div class="mini-ticket">
  <div class="mini-ticket-head">{link} — {subject}</div>
  <div class="mini-ticket-meta">{status} &middot; Requester: {requester} &middot; Assignee: {assignee_label_html}{solved_meta}</div>
  {claim_html}
</div>"""


def _render_security_column(items: list, error: str = "", return_to: str = "/security") -> str:
    """Read-only "recent activity" panel for the IT-Security queue -- not
    another review-and-draft workflow. Shows the N most recent tickets
    from that group exactly as Zendesk has them right now (any status, no
    already-replied filtering -- see _common.fetch_recent_tickets), each
    with a Claim button so grabbing one to work on doesn't require
    switching over to Zendesk directly first.

    Originally rendered as a side panel next to the main IT-Operations
    queue; now lives on its own /security page (_render_security_page)
    instead, so the main queue no longer has to share its layout with it
    -- this function itself is unchanged, just called from a different
    place.

    `error`, when set, means the fetch couldn't be served at all (not even
    from cache) -- shown in place of the list rather than silently
    rendering an empty panel that could be mistaken for "the queue is
    actually empty right now."""
    heading = f'<h2 class="column-heading">{_topic_icon("security")}Security — 5 most recent</h2>'
    if error:
        return f'<div class="security-panel">{heading}<p class="flag-alert">{html.escape(error)}</p></div>'
    if not items:
        return f'<div class="security-panel">{heading}<p class="empty">{_topic_icon("security", "topic-icon-empty")}No recent tickets.</p></div>'

    # .mini-ticket-grid lets these short, same-shaped rows flow into
    # however many columns fit instead of one full-width column each --
    # /flagged already gets a two-column layout for the exact same row
    # markup, just by splitting into two named categories (Blocked/
    # Standup) side by side; this page has one list, not two categories,
    # so an auto-fill grid is the equivalent for a single list.
    rows = "".join(_render_readonly_ticket_row(item, return_to=return_to) for item in items)
    return f'<div class="security-panel">{heading}<div class="mini-ticket-grid">{rows}</div></div>'


def _fmt_sla_history_date(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %-d")
    except Exception:
        return date_str


def _sla_charts_html(history: dict) -> str:
    """The /sla page's two trend charts, rendered client-side with
    Chart.js over the sibling sla-report-slack project's own daily
    snapshots (see _common.sla_history_snapshot/sla_chart_data): a
    stacked-area "queue health" chart (on_track/due_soon/breached, plus a
    dashed compliance-% line) with click-to-drill-down into that day's
    breached ticket IDs, and a stacked-bar "agent activity" chart (public
    replies + internal notes per agent) -- both sharing a Last 7 / Last 14
    / All time range toggle, mouse-wheel zoom + drag-to-pan, and a PNG
    export button. This is a deliberate, explicitly-requested departure
    from this file's own house style of zero external script
    dependencies: built to be genuinely identical to
    sla_dashboard.html's own charts (same Chart.js 4.4.1 +
    chartjs-plugin-zoom 2.0.1 + hammer.js 2.0.8, same chart types and
    interactions) rather than a look-alike, just recolored to read off
    THIS tool's own CSS custom properties at chart-build time instead of
    that report's hardcoded brand hex -- consistent with the "match this
    tool's theme" decision made for the rest of the /sla page.

    Deliberately narrower than the full email-report dashboard: no status
    banner / streak badge / weekly-snapshot table / avg-response KPIs --
    those need fields (median/percentile response times, per-ticket
    response timing) this page has no live equivalent for, and would just
    be redisplaying stale numbers. Just the two graphs the history file
    already carries everything needed for.

    Colors are resolved via getComputedStyle(document.documentElement) at
    chart-build time rather than baked in here, so they automatically
    track this tool's active theme (including a live OS light/dark
    switch while in "system" mode) -- see the MutationObserver/matchMedia
    listeners at the bottom of the inline script, both of which just
    rebuild the charts."""
    entries = history.get("entries") or []
    if not entries:
        message = history.get("error") or "No history entries found yet."
        return f'<p class="empty">{_topic_icon("sla", "topic-icon-empty")}{html.escape(message)}</p>'

    data = _common.sla_chart_data(history)
    data["labels"] = [_fmt_sla_history_date(d) for d in data["labels"]]
    # "</" -> "<\/" so a literal "</script>" inside e.g. an agent name or
    # ticket subject can never prematurely close this embedded JSON block.
    data_json = json.dumps(data).replace("</", "<\\/")
    subdomain_json = json.dumps(_common.ZENDESK_SUBDOMAIN)

    return f"""
<div class="sla-filter-bar">
  <span class="sla-filter-label">Range</span>
  <button type="button" class="sla-range-btn active" data-range="7" onclick="_slaSetRange(7)">Last 7</button>
  <button type="button" class="sla-range-btn" data-range="14" onclick="_slaSetRange(14)">Last 14</button>
  <button type="button" class="sla-range-btn" data-range="0" onclick="_slaSetRange(0)">All time</button>
  <span class="sla-zoom-hint">scroll to zoom &middot; drag to pan</span>
  <span class="sla-range-label" id="sla-range-label"></span>
</div>

<div class="sla-chart-legend">
  <span><span class="sla-chart-swatch" style="background:var(--sla-ok-color)"></span>Meeting SLA</span>
  <span><span class="sla-chart-swatch" style="background:var(--flag-color)"></span>Due soon</span>
  <span><span class="sla-chart-swatch" style="background:var(--flag-alert-color-urgent)"></span>SLA not met</span>
  <span><span class="sla-chart-swatch-line" style="background:var(--accent)"></span>Compliance %</span>
</div>
<p class="drill-hint">&#8627; Click a data point to see breached tickets for that day</p>
<div class="sla-chart-wrap">
  <button type="button" class="sla-chart-dl-btn sla-chart-reset-btn" onclick="if(window._slaAreaChart)window._slaAreaChart.resetZoom()" title="Reset zoom">&#8635;</button>
  <button type="button" class="sla-chart-dl-btn" onclick="_slaDownloadChart('sla-area-chart','queue-health')">&#8595; PNG</button>
  <div style="position:relative;height:260px"><canvas id="sla-area-chart"></canvas></div>
</div>
<div id="sla-drilldown" class="sla-drilldown" style="display:none">
  <div class="sla-drilldown-head">
    <div id="sla-drilldown-title"></div>
    <button type="button" id="sla-drilldown-dismiss">&#x2715; dismiss</button>
  </div>
  <div id="sla-drilldown-tickets"></div>
</div>

<h2 class="column-heading">Agent activity <span class="meta">public replies &amp; internal notes per agent</span></h2>
<div class="sla-agent-filter-bar" id="sla-agent-filter-bar"></div>
<div class="sla-chart-wrap">
  <button type="button" class="sla-chart-dl-btn sla-chart-reset-btn" onclick="if(window._slaAgentChart)window._slaAgentChart.resetZoom()" title="Reset zoom">&#8635;</button>
  <button type="button" class="sla-chart-dl-btn" onclick="_slaDownloadChart('sla-agent-chart','agent-activity')">&#8595; PNG</button>
  <div style="position:relative;height:220px"><canvas id="sla-agent-chart"></canvas></div>
</div>
<p id="sla-agent-empty" class="empty" style="display:none">No agent activity recorded in this history file.</p>

<script type="application/json" id="sla-chart-data">{data_json}</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hammer.js/2.0.8/hammer.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.0.1/chartjs-plugin-zoom.min.js"></script>
<script>
(function() {{
  var dataEl = document.getElementById('sla-chart-data');
  if (!dataEl || typeof Chart === 'undefined') return;
  var RAW = JSON.parse(dataEl.textContent);
  var SUBDOMAIN = {subdomain_json};
  var AGENT_PALETTE = ['#2a78d6', '#6c3fc5', '#1baf7a', '#eda100', '#d03b3b', '#0e9e9e'];
  var zoomOpts = {{ pan: {{ enabled: true, mode: 'x' }}, zoom: {{ wheel: {{ enabled: true }}, pinch: {{ enabled: true }}, mode: 'x' }} }};
  var filterAgents = new Set(RAW.agent_names);
  var currentRange = 7;
  var areaChart = null, agentChart = null;

  function slice(arr, n) {{ return (!n || n >= arr.length) ? arr : arr.slice(-n); }}

  function themeColor(name) {{
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }}

  function hexToRgba(hex, alpha) {{
    var m = /^#([0-9a-f]{{6}})$/i.exec((hex || '').trim());
    if (!m) return hex;
    var r = parseInt(m[1].substr(0, 2), 16), g = parseInt(m[1].substr(2, 2), 16), b = parseInt(m[1].substr(4, 2), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
  }}

  function buildAgentFilterBar() {{
    var bar = document.getElementById('sla-agent-filter-bar');
    if (!bar) return;
    bar.innerHTML = RAW.agent_names.map(function(name, i) {{
      var active = filterAgents.has(name) ? ' active' : '';
      var checked = filterAgents.has(name) ? ' checked' : '';
      return '<label class="sla-agent-check' + active + '"><input type="checkbox" data-agent-idx="' + i
        + '"' + checked + '>' + name.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</label>';
    }}).join('');
    Array.prototype.forEach.call(bar.querySelectorAll('input[type=checkbox]'), function(cb) {{
      cb.addEventListener('change', function() {{
        var name = RAW.agent_names[parseInt(cb.dataset.agentIdx, 10)];
        if (cb.checked) {{ filterAgents.add(name); }} else {{ filterAgents.delete(name); }}
        cb.closest('label').classList.toggle('active', cb.checked);
        buildCharts(currentRange);
      }});
    }});
  }}

  function buildCharts(n) {{
    currentRange = n;
    var labels = slice(RAW.labels, n);
    var onTrack = slice(RAW.on_track, n);
    var dueSoon = slice(RAW.due_soon, n);
    var breached = slice(RAW.breached, n);
    var totals = slice(RAW.totals, n);
    var breachedIds = slice(RAW.breached_ids, n);
    var compliance = slice(RAW.compliance_pct, n).map(function(v, i) {{
      if (v !== null && v !== undefined) return v;
      var t = totals[i];
      return t > 0 ? Math.round(((t - breached[i]) / t) * 100) : null;
    }});

    var rangeLabelEl = document.getElementById('sla-range-label');
    if (rangeLabelEl) {{
      rangeLabelEl.textContent = labels.length
        ? (labels[0] + ' \\u2013 ' + labels[labels.length - 1] + ' \\u00b7 ' + labels.length + (labels.length === 1 ? ' day' : ' days'))
        : 'No data yet';
    }}

    var okColor = themeColor('--sla-ok-color') || '#1a6e2e';
    var dueColor = themeColor('--flag-color') || '#b45309';
    var breachColor = themeColor('--flag-alert-color-urgent') || '#b91c1c';
    var accent = themeColor('--accent') || '#1a56c4';
    var gridColor = themeColor('--card-border') || '#e2e2e5';
    var tickColor = themeColor('--muted') || '#666';
    var tickCfg = {{ color: tickColor, font: {{ size: 11 }}, maxRotation: 45, autoSkip: true, maxTicksLimit: 10 }};

    if (areaChart) areaChart.destroy();
    var drillPanel = document.getElementById('sla-drilldown');
    var areaCanvas = document.getElementById('sla-area-chart');
    if (areaCanvas) {{
      areaChart = new Chart(areaCanvas, {{
        type: 'line',
        data: {{ labels: labels, datasets: [
          {{ label: 'SLA not met', data: breached, borderColor: breachColor, backgroundColor: hexToRgba(breachColor, 0.7), borderWidth: 1.5, pointRadius: 3, tension: 0, fill: true, stack: 'q', order: 1, yAxisID: 'y' }},
          {{ label: 'Due soon', data: dueSoon, borderColor: dueColor, backgroundColor: hexToRgba(dueColor, 0.75), borderWidth: 1.5, pointRadius: 3, tension: 0, fill: true, stack: 'q', order: 2, yAxisID: 'y' }},
          {{ label: 'Meeting SLA', data: onTrack, borderColor: okColor, backgroundColor: hexToRgba(okColor, 0.75), borderWidth: 1.5, pointRadius: 3, tension: 0, fill: true, stack: 'q', order: 3, yAxisID: 'y' }},
          {{ label: 'Compliance %', data: compliance, type: 'line', borderColor: accent, backgroundColor: 'transparent', borderWidth: 2, borderDash: [4, 3], pointRadius: 2, tension: 0.3, spanGaps: true, order: 0, yAxisID: 'yPct' }}
        ] }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{ mode: 'index', intersect: false, callbacks: {{ label: function(ctx) {{
              if (ctx.dataset.yAxisID === 'yPct') return 'Compliance: ' + ctx.parsed.y + '%';
              return ctx.dataset.label + ': ' + ctx.parsed.y;
            }} }} }},
            zoom: zoomOpts
          }},
          scales: {{
            x: {{ ticks: tickCfg, grid: {{ display: false }} }},
            y: {{ ticks: {{ color: tickColor, font: {{ size: 11 }}, stepSize: 10 }}, grid: {{ color: gridColor }}, min: 0, stacked: true }},
            yPct: {{ position: 'right', min: 0, max: 100, ticks: {{ color: accent, font: {{ size: 10 }}, callback: function(v) {{ return v + '%'; }} }}, grid: {{ display: false }} }}
          }},
          onClick: function(evt, elements) {{
            if (!elements.length) {{ drillPanel.style.display = 'none'; return; }}
            var idx = elements[0].index;
            var ids = breachedIds[idx] || [];
            if (!ids.length) {{ drillPanel.style.display = 'none'; return; }}
            document.getElementById('sla-drilldown-title').textContent =
              labels[idx] + ' \\u2014 ' + ids.length + ' breached ticket' + (ids.length !== 1 ? 's' : '');
            document.getElementById('sla-drilldown-tickets').innerHTML = ids.map(function(id) {{
              return SUBDOMAIN
                ? '<a class="sla-drilldown-chip" href="https://' + SUBDOMAIN + '.zendesk.com/agent/tickets/' + id + '" target="_blank">#' + id + '</a>'
                : '<span class="sla-drilldown-chip">#' + id + '</span>';
            }}).join('');
            drillPanel.style.display = '';
          }}
        }}
      }});
    }}

    if (agentChart) agentChart.destroy();
    var agentCanvas = document.getElementById('sla-agent-chart');
    var agentEmpty = document.getElementById('sla-agent-empty');
    var activeAgents = RAW.agent_names.filter(function(name) {{
      if (!filterAgents.has(name)) return false;
      var pub = slice(RAW.agents[name].public, n), internal = slice(RAW.agents[name].internal, n);
      return pub.some(function(v) {{ return v > 0; }}) || internal.some(function(v) {{ return v > 0; }});
    }});
    if (agentCanvas) {{
      var wrap = agentCanvas.closest('.sla-chart-wrap');
      if (activeAgents.length) {{
        if (wrap) wrap.style.display = '';
        if (agentEmpty) agentEmpty.style.display = 'none';
        agentChart = new Chart(agentCanvas, {{
          type: 'bar',
          data: {{ labels: labels, datasets: activeAgents.map(function(name, i) {{
            var pub = slice(RAW.agents[name].public, n), internal = slice(RAW.agents[name].internal, n);
            return {{
              label: name,
              data: pub.map(function(p, j) {{ return p + (internal[j] || 0); }}),
              backgroundColor: AGENT_PALETTE[i % AGENT_PALETTE.length],
              borderRadius: 2,
              stack: 'h'
            }};
          }}) }},
          options: {{
            responsive: true, maintainAspectRatio: false,
            plugins: {{
              legend: {{ position: 'bottom', labels: {{ color: tickColor, font: {{ size: 11 }}, boxWidth: 12, padding: 10 }} }},
              tooltip: {{ callbacks: {{ label: function(ctx) {{
                var name = ctx.dataset.label, i = ctx.dataIndex;
                var pub = slice(RAW.agents[name].public, n)[i] || 0;
                var internal = slice(RAW.agents[name].internal, n)[i] || 0;
                return name + ': ' + ctx.parsed.y + ' (' + pub + ' pub / ' + internal + ' int)';
              }} }} }},
              zoom: zoomOpts
            }},
            scales: {{
              x: {{ ticks: tickCfg, grid: {{ display: false }}, stacked: true }},
              y: {{ ticks: {{ color: tickColor, font: {{ size: 11 }}, stepSize: 5 }}, grid: {{ color: gridColor }}, min: 0, stacked: true }}
            }}
          }}
        }});
      }} else {{
        if (wrap) wrap.style.display = 'none';
        if (agentEmpty) agentEmpty.style.display = '';
      }}
    }}

    window._slaAreaChart = areaChart;
    window._slaAgentChart = agentChart;
  }}

  window._slaSetRange = function(n) {{
    Array.prototype.forEach.call(document.querySelectorAll('.sla-range-btn'), function(btn) {{
      btn.classList.toggle('active', parseInt(btn.dataset.range, 10) === n);
    }});
    buildCharts(n);
  }};

  window._slaDownloadChart = function(canvasId, filename) {{
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    var a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = filename + '.png';
    a.click();
  }};

  var dismissBtn = document.getElementById('sla-drilldown-dismiss');
  if (dismissBtn) {{
    dismissBtn.addEventListener('click', function() {{
      document.getElementById('sla-drilldown').style.display = 'none';
    }});
  }}

  buildAgentFilterBar();
  buildCharts(7);

  // Recolor on any theme change -- an explicit toggle (adds/removes a
  // theme-* class on <html>) or a live OS-level light/dark switch while
  // in "system" mode (no class change at all, so matchMedia is the only
  // way to notice that one).
  new MutationObserver(function() {{ buildCharts(currentRange); }})
    .observe(document.documentElement, {{ attributes: true, attributeFilter: ['class'] }});
  if (window.matchMedia) {{
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function() {{ buildCharts(currentRange); }});
  }}
}})();
</script>
"""


def _render_sla_page(sla_result: dict, history: dict, banner: str = "") -> bytes:
    """Native /sla page: now just the two Chart.js trend charts read
    read-only from the sibling sla-report-slack project's own history
    file (see _common.sla_history_snapshot/sla_chart_data and
    _sla_charts_html). Used to also render a live KPI strip, four rule
    cards, and an awaiting-first-response list computed directly from
    _common.fetch_sla_tickets/evaluate_sla_ticket -- removed by explicit
    request. That live per-ticket scan itself is untouched (do_GET's
    /sla route still calls fetch_sla_tickets every load, same as before)
    since it's also what keeps the nav badge's live breach count fresh;
    sla_result is kept as a param purely for its error/capped state,
    still worth surfacing here even with nothing else from it rendered."""
    error_html = (
        f'<p class="empty">{_topic_icon("breach", "topic-icon-empty")}{html.escape(sla_result["error"])}</p>'
        if sla_result.get("error")
        else ""
    )
    capped_note = (
        '<p class="sla-history-note">Some tickets may be missing from this scan -- '
        "the IT-Operations queue is large enough that this page didn't reach every page of it. "
        "See the README for SLA_TICKET_SCAN_PAGES.</p>"
        if sla_result.get("capped")
        else ""
    )
    as_of = history.get("as_of")
    history_note = (
        f'<p class="sla-history-note">Trend history as of {html.escape(as_of)} '
        "-- only as fresh as the last <code>git pull</code> of the sla-report-slack repo on this machine; "
        "this page never writes to that file.</p>"
        if as_of
        else ""
    )

    body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("sla")}SLA</h2>
<p class="meta">Historical SLA trends for the IT-Operations queue, sourced from the sibling sla-report-slack project's own daily snapshots -- the same four business-hours thresholds it tracks: initial response ({_common.SLA_INITIAL_RESPONSE_HRS}h), requester wait ({_common.SLA_REQUESTER_WAIT_HRS}h), no-update window ({_common.SLA_NO_UPDATE_HRS}h), and resolution age ({_common.SLA_RESOLUTION_HRS:g}h). The live breach count for the current queue is in the nav badge above.</p>
{error_html}
{capped_note}
<h2 class="column-heading">Queue health over time</h2>
{history_note}
{_sla_charts_html(history)}
"""
    return _page(body, banner=banner, current_path="/sla", wide=True)


def _render_security_page(items: list, error: str = "", banner: str = "") -> bytes:
    """Native /security page: the IT-Security group's own read-only
    "recent activity" list, previously embedded as a narrower side panel
    next to the main IT-Operations queue via the page's old `.layout`
    two-column grid. Moved to its own page (next to Tasks/Discussion)
    so the main queue reads as one focused list again, rather
    than always reserving a column for a different team's queue whether
    or not you're currently looking at it.

    Reuses _render_security_column exactly as it already rendered on the
    main page -- same card, same heading, same rows, same Claim buttons --
    only the page it's placed inside is new."""
    filter_html = _filter_box_html(".mini-ticket") if items else ""
    body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("security")}Security</h2>
<p class="meta">A read-only view of the IT-Security group's own queue -- any status, no already-replied filtering, no drafting -- with a Claim button if you'd like to pick one up. Previously a side panel on the main page; moved here so the main queue is a single focused list.</p>
{filter_html}
{_render_security_column(items, error)}
"""
    return _page(body, banner=banner, current_path="/security", wide=True)


def _render_flagged_page(items: list, error: str = "", capped: bool = False, banner: str = "") -> bytes:
    """Native /flagged page: every non-terminal IT-Operations ticket
    tagged "block" or "standup" (see _common.fetch_flagged_tickets),
    regardless of who it's assigned to or whether this tool has already
    drafted/posted a reply to it. Deliberately its own page next to My
    tasks rather than a section bolted onto the main queue -- these tags
    answer a different question than "needs a first reply": a ticket
    that's already been replied to (and so never appears in the main
    queue at all) can still be actively blocking something or worth
    raising at standup.

    Rendered as two side-by-side panels (Blocked / Standup) in a
    `.two-panel` grid -- the same "card with its own heading" shape
    _render_security_column already established for the IT-Security
    column, just two of them side by side instead of one next to the
    main queue. Collapses to stacked panels on narrow viewports, same
    breakpoint as the main page's own `.layout` grid. A ticket tagged
    both appears in both panels, which is accurate rather than a
    limitation to work around: it really is both."""
    if error:
        body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("flagged")}Discussion</h2>
<p class="flag-alert">{html.escape(error)}</p>
"""
        return _page(body, banner=banner, current_path="/flagged", wide=True)

    blocked = [i for i in items if "block" in (i.get("matched_tags") or [])]
    standup = [i for i in items if "standup" in (i.get("matched_tags") or [])]

    # .mini-ticket-grid (same wrapper /security and /solved already use
    # around this identical row markup) lets each panel's own rows flow
    # into however many columns fit ITS width, instead of always stacking
    # one full-width row each -- previously the only thing missing here
    # was this wrapper; the rows themselves never changed.
    blocked_html = (
        f'<div class="mini-ticket-grid">{"".join(_render_readonly_ticket_row(i, return_to="/flagged") for i in blocked)}</div>'
        if blocked
        else f'<p class="empty">{_topic_icon("flagged", "topic-icon-empty")}No tickets currently tagged &quot;block&quot;.</p>'
    )
    standup_html = (
        f'<div class="mini-ticket-grid">{"".join(_render_readonly_ticket_row(i, return_to="/flagged") for i in standup)}</div>'
        if standup
        else f'<p class="empty">{_topic_icon("flagged", "topic-icon-empty")}No tickets currently tagged &quot;standup&quot;.</p>'
    )
    capped_note = (
        '<p class="meta">This scan stopped early -- either Zendesk\'s own 1000-result search '
        "cap, or this tool's own scan-depth limit, was reached (sorted by most recently updated "
        "first). An older flagged ticket that hasn't been touched in a while could in theory not "
        "appear here.</p>"
        if capped
        else ""
    )

    # One filter box narrows down both panels at once (target selector
    # matches .mini-ticket rows in either) rather than one per panel --
    # they're the same list conceptually, just split across two tag
    # categories, and a ticket tagged both would otherwise need typing the
    # same query twice.
    filter_html = _filter_box_html(".mini-ticket") if (blocked or standup) else ""
    body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("flagged")}Discussion</h2>
<p class="meta">Every IT-Operations ticket tagged &quot;block&quot; or &quot;standup&quot; in Zendesk, regardless of who it's assigned to or whether it's already been replied to -- separate from the pending-reply queue on the main page, which only tracks tickets that still need a first reply.</p>
{capped_note}
{filter_html}
<div class="two-panel">
  <div class="security-panel"><h2 class="column-heading">Blocked ({len(blocked)})</h2>{blocked_html}</div>
  <div class="security-panel"><h2 class="column-heading">Standup ({len(standup)})</h2>{standup_html}</div>
</div>
"""
    return _page(body, banner=banner, current_path="/flagged", wide=True)


def _render_solved_page(items: list, error: str = "", capped: bool = False, banner: str = "") -> bytes:
    """Native /solved page: the last _common.RECENTLY_SOLVED_LIMIT tickets
    in the IT-Operations group that are both status=="solved" and assigned
    to this tool's own configured Zendesk account
    (_common.fetch_recently_solved_tickets), most recently solved first.
    Read-only, same "no drafting, no already-replied checks" posture as
    /security and /flagged -- this is a look-back at what's already been
    closed out, not another queue to work through.

    Reuses _render_readonly_ticket_row for its rows exactly as /security
    and /flagged do -- every item here is already_ours=True (nothing to
    Claim, it's already yours) and carries "solved_at", which that shared
    row renderer turns into a "solved Nh ago" segment on the meta line."""
    if error:
        body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("solved")}Recently solved</h2>
<p class="flag-alert">{html.escape(error)}</p>
"""
        return _page(body, banner=banner, current_path="/solved", wide=True)

    rows_html = (
        f'<div class="mini-ticket-grid">{"".join(_render_readonly_ticket_row(i, return_to="/solved") for i in items)}</div>'
        if items
        else f'<p class="empty">{_topic_icon("solved", "topic-icon-empty")}No solved tickets assigned to you found in the IT-Operations group yet.</p>'
    )
    capped_note = (
        '<p class="meta">This scan stopped early -- either Zendesk\'s own 1000-result search '
        "cap, or this tool's own scan-depth limit, was reached (sorted by most recently updated "
        f"first) before finding {_common.RECENTLY_SOLVED_LIMIT} matching tickets. There may be "
        "more solved tickets assigned to you further back than this page shows.</p>"
        if capped
        else ""
    )

    filter_html = _filter_box_html(".mini-ticket") if items else ""
    body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("solved")}Recently solved</h2>
<p class="meta">The last {len(items)} tickets in the IT-Operations group that are both solved and assigned to you in Zendesk, most recently solved first -- read-only, no drafting or reply actions here. Doesn't include solved tickets assigned to you in other groups (e.g. Security) -- see the docstring on _common.fetch_recently_solved_tickets for why.</p>
{capped_note}
{filter_html}
<div class="security-panel"><h2 class="column-heading">Recently solved ({len(items)})</h2>{rows_html}</div>
"""
    return _page(body, banner=banner, current_path="/solved", wide=True)


_OVERVIEW_SECTION_CAP = 5


def _render_overview_draft_task_row(item: dict) -> str:
    """Compact, read-only row for /overview's "Tasks" section covering
    a not-yet-promoted draft (see _common.draft_personal_tasks). Mirrors
    the other overview rows' glanceable shape (the same .mini-ticket
    layout _render_readonly_ticket_row uses for Discussion/Security) --
    just enough (subject, category tag, added date) to tell whether it's
    worth clicking through to /tasks -- rather than reusing
    _render_draft_task_row's full Estimated-minutes/Assignee/Requester/
    Allocation/Promote/Discard/reorder/Edit form, which would turn this
    snapshot page into a second workflow. That's exactly what this page's
    own docstring says it deliberately avoids for the other sections, and
    a draft needs the same restraint: nothing here can promote or discard
    it, only /tasks itself can. Its title links to that exact draft's row
    on /tasks (see _task_title_link_html) -- "clicking through to /tasks"
    used to mean landing at the top of a possibly-long list; now it
    scrolls straight to the row in question."""
    local_id = item.get("local_id")
    raw_subject = item.get("subject", "")
    subject = _task_title_link_html(html.escape(raw_subject), local_id)
    category = item.get("category", "")
    # Same "skip the chip when the subject already carries the category as
    # its own leading tag" rule _render_draft_task_row/_render_open_task_row
    # use, so a row here looks identical in style to its /tasks counterpart.
    category_html = (
        f'<span class="stat-chip task-category-chip">{html.escape(category)}</span>'
        if category and not raw_subject.strip().startswith(category)
        else ""
    )
    added = html.escape(_task_added_label(item.get("created_at", "")))
    return f"""<div class="mini-ticket">
  <div class="mini-ticket-head">{category_html}{subject}</div>
  <div class="mini-ticket-meta">Added {added} &middot; not yet in Zendesk</div>
</div>"""


def _render_overview_page(
    open_tasks: list,
    draft_tasks: list,
    flagged_items: list,
    security_items: list,
    flagged_error: str = "",
    security_error: str = "",
    activity_summary: dict | None = None,
    banner: str = "",
) -> bytes:
    """/overview page: a single glanceable "what needs me" snapshot
    pulling together three otherwise-separate places a ticket or task can
    be waiting on you -- Tasks (both not-yet-promoted drafts and
    already-promoted open tasks), Discussion, and Security's own recent
    activity -- since checking each of those pages individually just to
    answer "is there anything I'm missing right now" was the exact
    fragmented-view complaint this page exists to fix.

    First replies used to be a fourth section here ("Needs a first
    reply"), but that queue is now its own real page at /firstreply
    (linked first in the top nav) rather than a tile duplicated onto
    Overview too -- see the activity_summary paragraph below for the one
    thing about first replies that's still surfaced here.

    activity_summary (see _common.personal_activity_summary) adds a small
    "Last week" personal-pace line above the sections -- replies posted
    through this tool during the most recently completed business week
    (Monday-Friday; the weekend doesn't count) and their average wait,
    purely local and read-only. This is what replaced the old /sla page's
    team-wide compliance dashboard: a much smaller, personal-only figure
    rather than a second team metrics page, per the removal decision.
    Optional (defaults to None/omitted) so existing callers/tests that
    don't pass it still render a page, just without that line.

    Deliberately NOT a second workflow layered on top of the other pages:
    every section here is read-only or lightly actionable (open tasks keep
    their Quick Solve/Mark done buttons via _render_open_task_row, drafts
    are shown read-only via _render_overview_draft_task_row rather than
    reusing _render_draft_task_row's full promote/discard/reorder/edit
    form; Discussion and Security keep their Claim buttons via
    _render_readonly_ticket_row -- reused as-is, not reimplemented), each
    capped to _OVERVIEW_SECTION_CAP items, with a "See all" link through
    to that section's own full page for anything past the cap or the real
    per-ticket workflow the destination page itself has. This page
    answers "where do I need to look first"; the destination pages answer
    "now what."

    All four inputs are the exact same cached/local reads those pages
    already make on their own (see do_GET's "/overview" branch) --
    nothing here triggers an extra live Zendesk scan beyond what visiting
    each page individually would already do. flagged_error/security_error
    surface the same "couldn't be served at all, not even from cache"
    signal /flagged and /security show standalone, in place of that one
    section rather than silently rendering it as an empty (and
    misleadingly reassuring) "nothing here.\""""
    # Drafts first, then open tasks -- same order the /tasks page itself
    # uses for its two lists ("Not yet in Zendesk" above "Open"). Combined
    # and capped together (not each capped separately then concatenated)
    # so the cap reflects the section as a whole, matching how the other
    # three sections cap their one underlying list.
    tasks_combined = [("draft", i) for i in draft_tasks] + [("open", i) for i in open_tasks]
    tasks_capped = tasks_combined[:_OVERVIEW_SECTION_CAP]
    tasks_body = (
        '<div class="mini-ticket-grid">'
        + "".join(
            _render_overview_draft_task_row(i) if kind == "draft" else _render_open_task_row(i)
            for kind, i in tasks_capped
        )
        + "</div>"
        if tasks_capped
        else f'<p class="empty">{_topic_icon("tasks", "topic-icon-empty")}No tasks right now.</p>'
    )
    tasks_total = len(draft_tasks) + len(open_tasks)

    flagged_capped = flagged_items[:_OVERVIEW_SECTION_CAP]
    if flagged_error:
        flagged_body = f'<p class="flag-alert">{html.escape(flagged_error)}</p>'
    elif flagged_capped:
        flagged_body = f'<div class="mini-ticket-grid">{"".join(_render_readonly_ticket_row(i, return_to="/overview") for i in flagged_capped)}</div>'
    else:
        flagged_body = f'<p class="empty">{_topic_icon("flagged", "topic-icon-empty")}No blocked or standup tickets right now.</p>'

    security_capped = security_items[:_OVERVIEW_SECTION_CAP]
    if security_error:
        security_body = f'<p class="flag-alert">{html.escape(security_error)}</p>'
    elif security_capped:
        security_body = f'<div class="mini-ticket-grid">{"".join(_render_readonly_ticket_row(i, return_to="/overview") for i in security_capped)}</div>'
    else:
        security_body = f'<p class="empty">{_topic_icon("security", "topic-icon-empty")}No recent tickets.</p>'

    def _last_week_range_label(summary: dict) -> str:
        """'Aug 17-21' for a business week within one month, 'Aug 31 - Sep
        4' when it spans a month boundary (or, rarer still, New Year's,
        where %b already disambiguates the year change). Always a Mon-Fri
        span (week_start/week_end -- see _common.personal_activity_summary)
        since the weekend is deliberately excluded from what "last week"
        counts here."""
        start, end = summary["week_start"], summary["week_end"]
        if start.month == end.month:
            return f'{start.strftime("%b")} {start.day}–{end.day}'
        return f'{start.strftime("%b")} {start.day} – {end.strftime("%b")} {end.day}'

    def _activity_line_html(summary: dict | None) -> str:
        """Small 'Last week' personal-pace line -- see
        _common.personal_activity_summary's docstring for exactly what's
        being measured (the most recently completed Monday-through-Friday
        business week, not a rolling trailing window and not counting the
        weekend) and why it's deliberately not framed as an SLA/compliance
        figure. Renders nothing at all (not even an empty-state message)
        when there's no summary or nothing posted in the window, since an
        always-present-but-usually-zero line would just be noise on a
        quiet week."""
        if not summary or not summary.get("count"):
            return ""
        count = summary["count"]
        reply_word = "reply" if count == 1 else "replies"
        breakdown = f'{summary["public_count"]} public, {summary["internal_count"]} internal'
        avg_hours = summary.get("avg_hours")
        avg_html = (
            f", averaging {avg_hours:.1f}h from first seen to reply" if avg_hours is not None else ""
        )
        range_label = _last_week_range_label(summary)
        # data-count-target carries the real number for count_up_script
        # (see _page()) to animate up from 0 on load -- the span's own
        # text content is ALSO the real, correct count from the start, not
        # "0", so nothing here depends on that script actually running:
        # with JS disabled, or under prefers-reduced-motion (which the
        # script itself checks before touching this), the number simply
        # sits there fully resolved, exactly as it always did before this.
        return (
            f'<p class="meta overview-activity-line">Last week ({range_label}): '
            f'<span class="stat-chip"><span class="count-up" data-count-target="{count}">{count}</span> {reply_word} posted</span> '
            f"({breakdown}){avg_html}.</p>"
        )

    activity_html = _activity_line_html(activity_summary)

    # topic_key drives a per-section left-edge accent stripe (see
    # .overview-section-<key>'s CSS) -- purely a color-coded "which of the
    # three sources is this" cue for a page that deliberately blends all
    # three together (see this function's own docstring); Tasks/Discussion/
    # Security recent each get their own hue, distinct from --accent (the
    # general brand blue already used everywhere for buttons/links) and
    # from --flag-alert-color (already means "warning/priority" elsewhere
    # on this page), so this doesn't accidentally borrow meaning from
    # either of those.
    def _section(title: str, icon_html: str, count: int, body_html: str, see_all_href: str, topic_key: str) -> str:
        return f"""<div class="security-panel overview-section overview-section-{topic_key}">
  <div class="overview-section-head">
    <h2 class="column-heading">{icon_html}{html.escape(title)} ({count})</h2>
    <a class="overview-see-all" href="{see_all_href}">See all &rarr;</a>
  </div>
  {body_html}
</div>"""

    sections_html = (
        _section("Tasks", _topic_icon("tasks"), tasks_total, tasks_body, "/tasks", "tasks")
        + _section("Discussion", _topic_icon("flagged"), len(flagged_items), flagged_body, "/flagged", "discussion")
        + _section("Security recent", _topic_icon("security"), len(security_items), security_body, "/security", "security")
    )

    body = f"""
<h2>{_topic_icon("overview")}Overview</h2>
<p class="meta">A single glanceable snapshot of everything that might need you right now, pulled together from Tasks, Discussion, and Security -- each section capped to the {_OVERVIEW_SECTION_CAP} most relevant, with a link through to that page's own full list to act on anything past the cap. Need to see what's waiting on a first reply? That's its own page now -- <a href="/firstreply">First Reply</a>.</p>
{activity_html}
<div class="overview-grid">
{sections_html}
</div>
"""
    return _page(body, banner=banner, current_path="/overview", wide=True)


def _identity_select_html(field_name: str) -> str:
    """The Assignee/Requester <select> on a draft's promote form -- same
    two options (PERSONAL_TASK_IDENTITY_LABELS) either way, resolved
    independently server-side per field name (see
    _common.resolve_personal_task_identity), defaulting to "self"
    (Catherine Vee) -- the only option that existed before this choice
    did, so an untouched form still behaves exactly as before."""
    options = "".join(
        f'<option value="{value}"{" selected" if value == _common.PERSONAL_TASK_IDENTITY_SELF else ""}>{html.escape(label)}</option>'
        for value, label in _common.PERSONAL_TASK_IDENTITY_LABELS.items()
    )
    return f'<select name="{field_name}" class="task-identity-select">{options}</select>'


def _allocation_select_html() -> str:
    """The Allocation <select> on a draft's promote form -- defaults to
    CSA (PERSONAL_TASK_ALLOCATION_VALUE), the only value this field ever
    sent before Allocation was a per-task choice."""
    options = "".join(
        f'<option value="{value}"{" selected" if value == _common.PERSONAL_TASK_ALLOCATION_VALUE else ""}>{html.escape(label)}</option>'
        for value, label in _common.PERSONAL_TASK_ALLOCATION_LABELS.items()
    )
    return f'<select name="allocation" class="task-identity-select">{options}</select>'


def _category_select_html(selected: str = "") -> str:
    """Category <select> on the "Add task" form and a draft's inline Edit
    form -- a closed choice of two (_common.PERSONAL_TASK_CATEGORY_CHOICES)
    rather than the free-text field this used to be. The whole point of
    the fixed list is that picking one auto-fills its bracketed text onto
    the subject box as a leading tag (the 'change' listener in
    interaction_script, keyed off data-auto-subject below) -- free text
    couldn't drive that predictably, since there'd be no fixed tag to
    prepend/replace/strip.

    An existing draft's category may still be an older free-typed string
    from before this changed (nothing here migrates existing data) -- if
    `selected` is non-empty and isn't one of the two fixed choices, it's
    added as its own extra selected option so editing an old draft never
    silently discards or overwrites a category that was already there."""
    options = ['<option value="">No category</option>']
    for choice in _common.PERSONAL_TASK_CATEGORY_CHOICES:
        is_selected = " selected" if choice == selected else ""
        options.append(f'<option value="{html.escape(choice)}"{is_selected}>{html.escape(choice)}</option>')
    if selected and selected not in _common.PERSONAL_TASK_CATEGORY_CHOICES:
        options.append(f'<option value="{html.escape(selected)}" selected>{html.escape(selected)}</option>')
    return (
        f'<select name="category" class="task-identity-select" data-auto-subject="true">'
        f'{"".join(options)}</select>'
    )


def _render_draft_task_row(item: dict, is_first: bool = True, is_last: bool = True) -> str:
    """One row in the /tasks page's "Not yet in Zendesk" list -- a local-
    only draft (see _common.add_personal_task), with the actions that
    apply to a draft specifically: edit it in place, move it up/down in
    the manually-orderable list, promote it into a real ticket (choosing
    Estimated minutes -- pre-filled if it was already given when the
    draft was added -- plus Assignee/Requester/Allocation), or discard it
    outright. None of these exist for an already-promoted "open" task --
    see _render_open_task_row below.

    is_first/is_last (from _render_tasks_page's enumerate over the
    already-ordered list) disable the Move up / Move down button that
    would otherwise do nothing -- there's no draft above the first one or
    below the last one to swap positions with.

    Carries id="task-{local_id}" on its own wrapper -- the landing spot
    for the title link _task_title_link_html builds elsewhere (this row's
    own compact preview on /overview, or a bookmark/history entry pointing
    straight back here), so #task-{local_id} in the URL scrolls to and
    briefly highlights this exact row (see .mini-ticket:target)."""
    local_id = item.get("local_id")
    raw_subject = item.get("subject", "")
    subject = _task_title_link_html(html.escape(raw_subject), local_id)
    notes = html.escape(item.get("notes", ""))
    notes_html = f'<div class="mini-ticket-notes">{notes}</div>' if notes else ""
    category = item.get("category", "")
    # No separate chip when the category is already the subject's own
    # leading tag (the normal case now that picking one auto-fills the
    # subject box -- see _category_select_html's paired 'change' listener)
    # -- showing both would just repeat the same bracketed text twice. An
    # older draft whose category was typed free-text, before the subject
    # auto-fill existed, won't match this way and still gets its chip.
    category_html = (
        f'<span class="stat-chip task-category-chip">{html.escape(category)}</span>'
        if category and not raw_subject.strip().startswith(category)
        else ""
    )
    added = html.escape(_task_added_label(item.get("created_at", "")))
    est_mins = item.get("est_mins")
    est_value_attr = f' value="{int(est_mins)}"' if est_mins else ""

    up_disabled = " disabled" if is_first else ""
    down_disabled = " disabled" if is_last else ""

    return f"""
<div class="mini-ticket" id="task-{html.escape(str(local_id))}">
  <div class="mini-ticket-head">{category_html}{subject}</div>
  <div class="mini-ticket-meta">Added {added} &middot; not yet in Zendesk</div>
  {notes_html}
  <div class="mini-claim-form">
    <form method="post" action="/tasks/{local_id}/promote" class="promote-form">
      <label class="promote-field"><span class="promote-field-label">Est. min</span>
        <input type="number" name="est_mins" min="1" max="{_common.PERSONAL_TASK_EST_MINUTES_MAX}" step="1" required{est_value_attr} class="task-est-input" aria-label="Estimated minutes">
      </label>
      <label class="promote-field"><span class="promote-field-label">Assignee</span>
        {_identity_select_html("assignee_identity")}
      </label>
      <label class="promote-field"><span class="promote-field-label">Requester</span>
        {_identity_select_html("requester_identity")}
      </label>
      <label class="promote-field"><span class="promote-field-label">Allocation</span>
        {_allocation_select_html()}
      </label>
      <button type="submit" class="trigger" data-loading-text="Creating…">{_icon("external-link", "icon-sm")}<span>Make Zendesk Ticket from Task</span></button>
    </form>
    <form method="post" action="/tasks/{local_id}/discard">
      <button type="submit" class="remove" data-loading-text="Discarding…" onclick="return confirm('Discard this task? It was never created in Zendesk, so nothing there will be affected.');">{_icon("trash", "icon-sm")}<span>Discard</span></button>
    </form>
    <div class="task-reorder">
      <form method="post" action="/tasks/{local_id}/move/up">
        <button type="submit" class="remove" title="Move up"{up_disabled}>{_icon("chevron-up", "icon-sm")}</button>
      </form>
      <form method="post" action="/tasks/{local_id}/move/down">
        <button type="submit" class="remove" title="Move down"{down_disabled}>{_icon("chevron-down", "icon-sm")}</button>
      </form>
    </div>
  </div>
  <details class="task-edit">
    <summary>Edit</summary>
    <form method="post" action="/tasks/{local_id}/edit" class="task-edit-form">
      <input type="text" name="subject" value="{subject}" required placeholder="What needs doing?">
      <textarea name="notes" placeholder="Notes (optional)">{notes}</textarea>
      {_category_select_html(category)}
      <input type="number" name="est_mins" min="1" max="{_common.PERSONAL_TASK_EST_MINUTES_MAX}" step="1"{est_value_attr} placeholder="Estimated minutes (optional)">
      <div class="actions">
        <button type="submit" class="approve" data-loading-text="Saving…">{_icon("send", "icon-sm")}<span>Save changes</span></button>
      </div>
    </form>
  </details>
</div>"""


def _render_open_task_row(item: dict) -> str:
    """One row in the /tasks page's "Open" list -- a task that HAS been
    promoted to a real Zendesk ticket (see _common.promote_personal_task,
    which deliberately does NOT post any comment on that ticket), with
    the two actions that apply once a ticket actually exists: "Quick
    Solve" (see _common.quick_solve_personal_task) posts the intake
    internal note + public reply and solves the ticket in one click, and
    "Mark done" (see _common.complete_personal_task) solves it with no
    comments at all, for cases where the requester was already told
    something manually outside this tool. Both buttons coexist -- neither
    replaces the other.

    A task promoted before Estimated minutes was required (see
    _common._resolve_est_mins_for_solve) has no est_mins to show in the
    meta line below, and can't be solved without one -- Zendesk rejects a
    solve missing any of the three IT Requests fields. For that (rare,
    legacy-only) case, both forms below carry their own inline "Est. min"
    input, required, so solving it collects the missing estimate right
    there instead of failing with no way to fix it from this page. A
    normal task (the vast majority, already having one from promote time)
    shows its estimate as plain text in the meta line instead and neither
    form gets this extra input at all.

    Carries id="task-{local_id}" on its own wrapper, same as
    _render_draft_task_row -- an open task's title (see
    _task_title_link_html) points back at this row, not at the "#tid"
    link's Zendesk URL, so wherever this row shows up (here, or the
    compact preview on /overview) linking through lands on this exact
    row on /tasks rather than just the top of the page."""
    local_id = item.get("local_id")
    tid = item.get("ticket_id")
    raw_subject = item.get("subject", "")
    subject = html.escape(raw_subject)
    notes = html.escape(item.get("notes", ""))
    notes_html = f'<div class="mini-ticket-notes">{notes}</div>' if notes else ""
    url = _common.ticket_url(tid)
    link = f'<a href="{html.escape(url)}" target="_blank" class="tid">#{tid}</a>' if url else f'<span class="tid">#{tid}</span>'
    subject = _task_title_link_html(subject, local_id)
    added = html.escape(_task_added_label(item.get("ticket_created_at") or item.get("created_at", "")))
    est_mins = item.get("est_mins")
    est_html = f" &middot; Est. {int(est_mins)}m" if est_mins else ""
    category = item.get("category", "")
    # See _render_draft_task_row's identical comment -- skip the chip when
    # the subject already carries the category as its own leading tag.
    category_html = (
        f'<span class="stat-chip task-category-chip">{html.escape(category)}</span>'
        if category and not raw_subject.strip().startswith(category)
        else ""
    )
    legacy_est_input = ""
    if not est_mins:
        legacy_est_input = (
            '<label class="promote-field"><span class="promote-field-label">Est. min</span>'
            f'<input type="number" name="est_mins" min="1" max="{_common.PERSONAL_TASK_EST_MINUTES_MAX}" step="1" required '
            'class="task-est-input" aria-label="Estimated minutes (required to solve this task)"></label>'
        )
    return f"""
<div class="mini-ticket" id="task-{html.escape(str(local_id))}">
  <div class="mini-ticket-head">{link} — {category_html}{subject}</div>
  <div class="mini-ticket-meta">Created {added}{est_html}</div>
  {notes_html}
  <div class="mini-claim-form">
    <form method="post" action="/tasks/{local_id}/quick-solve">
      {legacy_est_input}
      <button type="submit" class="trigger" data-loading-text="Solving…">{_icon("send", "icon-sm")}<span>Quick Solve</span></button>
    </form>
    <form method="post" action="/tasks/{local_id}/done">
      {legacy_est_input}
      <button type="submit" class="approve" data-loading-text="Marking done…">{_icon("check-square", "icon-sm")}<span>Mark done</span></button>
    </form>
  </div>
</div>"""


def _render_recently_completed_tasks() -> str:
    """"Recently completed" collapsible on /tasks -- mirrors the main
    queue's "Replied today" pattern (_render_recently_handled): a task
    marked done shouldn't just vanish from the Open list with nothing to
    show for it,
    the same way a posted reply doesn't just disappear from the main
    queue. Same collapsed-by-default <details> element with its own
    localStorage key, so this section's open/closed state doesn't fight
    the main queue's own sections for storage."""
    items = _common.completed_personal_tasks(limit=10)
    if not items:
        return ""

    rows = []
    for item in items:
        subject = html.escape(item.get("subject", ""))
        tid = item.get("ticket_id")
        url = _common.ticket_url(tid)
        link = f'<a href="{html.escape(url)}" target="_blank">#{tid}</a>' if url else f"#{tid}"
        completed_at = item.get("completed_at", "")
        try:
            when = datetime.fromisoformat(completed_at).strftime("%b %-d, %-I:%M %p")
        except Exception:
            when = ""
        rows.append(
            f'<li>{link} — {subject} '
            f'<span class="handled-time">{html.escape(when)}</span></li>'
        )

    return f"""
<details class="recently-handled" data-storage-key="csa-tasks-recently-completed-open">
  <summary>Recently completed ({len(items)})</summary>
  <ul>
    {"".join(rows)}
  </ul>
</details>"""


def _render_tasks_page(draft_tasks: list, open_tasks: list, banner: str = "") -> bytes:
    """Native /tasks page: a quick-add form that saves a personal to-do as
    a LOCAL DRAFT ONLY (see _common.add_personal_task -- no Zendesk call
    happens just from typing a task in), a "Not yet in Zendesk" list of
    those drafts each with a "Make Zendesk Ticket from Task" button (see
    _common.promote_personal_task -- this is the one moment a real ticket
    gets created) or a Discard option, and an "Open" list of tasks that
    HAVE been promoted, each with a one-click "Quick Solve" (see
    _common.quick_solve_personal_task -- posts the intake note/reply,
    deferred here from ticket-creation time, then solves it) or "Mark
    done" (see _common.complete_personal_task -- solves it with no
    comments). Deliberately its own page rather than
    a panel on the main queue -- these are your own tickets, not a
    customer's, so this keeps the main page focused purely on ticket
    triage the way /flagged and /security already keep their own
    different-purpose lists off of it too.

    This two-step draft-then-promote flow replaced an earlier version
    where "Add task" created the Zendesk ticket immediately: that turned
    out to be too eager for quickly jotting down an idea that might not be
    worth tracking as an actual ticket -- see the README for the full
    rationale.

    Not yet in Zendesk / Open render as two side-by-side panels in the
    same `.two-panel` grid /flagged already uses for its Blocked/Standup
    split -- previously these were two full-width sections stacked one
    above the other, so seeing an open task meant scrolling past every
    draft first even though the two lists have nothing to do with each
    other. Side by side, both are visible together without scrolling (on
    a wide enough viewport); .two-panel collapses back to stacked panels
    on narrow ones, same breakpoint as everywhere else it's used. The
    #tasks-drafts/#tasks-open anchors (and the "Jump to" links in the meta
    paragraph below) move onto each panel's own wrapper now instead of a
    heading, so they still work the same on a stacked/narrow layout."""
    # .mini-ticket-grid (shared with /security's read-only list) lets these
    # compact rows flow into however many columns fit instead of one
    # full-width row each -- each row's own content (a title line plus one
    # row of small fields/buttons) rarely needs the full page width, so a
    # couple of tasks side by side reads better than a long single column.
    # Only wraps the actual rows, not the empty-state message, same as
    # _render_security_column.
    last_idx = len(draft_tasks) - 1
    draft_html = (
        '<div class="mini-ticket-grid">'
        + "\n".join(
            _render_draft_task_row(item, is_first=(i == 0), is_last=(i == last_idx))
            for i, item in enumerate(draft_tasks)
        )
        + "</div>"
        if draft_tasks
        else f'<p class="empty">{_topic_icon("tasks", "topic-icon-empty")}All clear -- anything you add above shows up here first.</p>'
    )
    open_html = (
        '<div class="mini-ticket-grid">' + "\n".join(_render_open_task_row(i) for i in open_tasks) + "</div>"
        if open_tasks
        else f'<p class="empty">{_topic_icon("tasks", "topic-icon-empty")}Nothing open right now -- all caught up here too.</p>'
    )

    completed_html = _render_recently_completed_tasks()

    body = f"""
<p><a href="/firstreply">&larr; back to pending tickets</a></p>
<h2>{_topic_icon("tasks")}Tasks</h2>
<p class="meta">Personal to-do tickets -- self-requested and self-assigned in Zendesk (tagged &quot;{html.escape(_common.PERSONAL_TASK_TAG)}&quot;) once promoted, kept entirely separate from the IT-Operations pending-reply queue on the main page. Jump to: <a href="#tasks-drafts">Not yet in Zendesk ({len(draft_tasks)})</a> &middot; <a href="#tasks-open">Open ({len(open_tasks)})</a></p>
<div class="ticket">
  <p class="ticket-eyebrow">New task</p>
  <form method="post" action="/tasks/new">
    <input type="text" name="subject" placeholder="What needs doing?" required autofocus>
    <textarea name="notes" placeholder="Notes (optional)"></textarea>
    <div class="new-task-extra-fields">
      {_category_select_html()}
      <input type="number" name="est_mins" min="1" max="{_common.PERSONAL_TASK_EST_MINUTES_MAX}" step="1" placeholder="Estimated minutes (optional)">
    </div>
    <div class="actions">
      <button class="approve" type="submit" data-loading-text="Saving…">{_icon("send")}<span>Add task</span></button>
      <button class="trigger" type="submit" formaction="/tasks/new/quick-solve" data-loading-text="Solving…">{_icon("send", "icon-sm")}<span>Add + Quick Solve</span></button>
    </div>
    <div class="kbd-hint">Saved here only -- nothing is created in Zendesk until you click "Make Zendesk Ticket from Task" below. Estimated minutes here is optional and just pre-fills that step -- it's still asked for again if you leave it blank. "Add + Quick Solve" skips straight to a solved Zendesk ticket in one click -- it does need Estimated minutes filled in first.</div>
  </form>
</div>
<div class="two-panel">
  <div class="security-panel" id="tasks-drafts"><h2 class="column-heading">Not yet in Zendesk ({len(draft_tasks)})</h2>{draft_html}</div>
  <div class="security-panel" id="tasks-open"><h2 class="column-heading">Open ({len(open_tasks)})</h2>{open_html}</div>
</div>
{completed_html}
"""
    return _page(body, banner=banner, current_path="/tasks", wide=True)


class Handler(BaseHTTPRequestHandler):
    # Every response here already sends an explicit Content-Length (never
    # chunked) -- the one thing http.server actually requires to keep a
    # connection alive under HTTP/1.1 -- so there's no reason to still pay
    # the http.server DEFAULT of HTTP/1.0 (a fresh TCP handshake for every
    # single request: every page nav, every post/dismiss/claim redirect,
    # every 60s auto-refresh tick). _redirect()'s explicit
    # "Content-Length: 0" below is the one companion fix this needs -- an
    # empty-body response with no length at all is ambiguous framing on a
    # connection the client expects to reuse.
    protocol_version = "HTTP/1.1"

    # Below this, gzip's own ~20-byte header/footer overhead plus the CPU
    # cost of compressing can net out LARGER or not-worth-it versus just
    # sending the bytes -- most requests here are quick redirect/claim/
    # dismiss round-trips with tiny or empty bodies, and it's not worth
    # paying the compression cost on those.
    _MIN_GZIP_BYTES = 500

    def log_message(self, fmt, *args):
        _log(f"{self.address_string()} - {fmt % args}")

    def _write_body(self, body: bytes, content_type: str, status: int = 200) -> None:
        """Shared by _send_html. Gzips the body when the
        client's Accept-Encoding says it understands gzip (every real
        browser does; a plain curl/script request with no such header --
        including this app's own test suite -- gets the exact same bytes
        back as before this existed). Most of what this app sends is a
        big, highly repetitive inline <style>/<script> block plus base64
        image data (see the README/performance notes) -- both compress
        very well, so this is a large transfer-size win for a few lines
        of code, with zero change to what actually gets rendered."""
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        if accepts_gzip and len(body) >= self._MIN_GZIP_BYTES:
            body = gzip.compress(body)
            content_encoding = "gzip"
        else:
            content_encoding = ""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes, status: int = 200):
        self._write_body(body, "text/html; charset=utf-8", status)

    def _redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        # Required now that protocol_version is HTTP/1.1 (see above): a
        # response with no body still needs an explicit length so the
        # client knows exactly where it ends and the next response on the
        # same reused connection begins. Harmless under the old HTTP/1.0
        # behavior too (each connection just closed anyway), so this is
        # safe regardless.
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _parse_form_body(self) -> dict:
        """Reads this POST's application/x-www-form-urlencoded body and
        parses it into the same {key: [values, ...]} shape urllib.parse
        .parse_qs already returns everywhere else in this file. Centralizes
        what used to be the same three lines (Content-Length parse, body
        read, parse_qs) copy-pasted at every do_POST branch that needs a
        body -- nine call sites, all doing `int(self.headers.get(
        "Content-Length", 0))` directly.

        That direct int() was the one place in this whole app where a
        malformed request could raise past every fail-open banner/message
        the rest of this file goes out of its way to show instead: a
        Content-Length header that isn't a plain integer (blank, "abc", a
        stray decimal) raises ValueError right out of int(), and
        ThreadingHTTPServer's per-request thread has nothing else catching
        it -- the request just drops with a raw traceback logged
        server-side and a broken connection on the client end, unlike
        literally everything else this app does when something goes wrong.
        A negative Content-Length (a technically-valid int(), e.g. "-1")
        was a second, subtler version of the same problem: the old
        `self.rfile.read(length) if length else ""` treats any nonzero
        length as truthy and would pass a negative count straight to
        rfile.read(), which is undefined/hangs on a socket-backed stream
        rather than erroring cleanly. A body that isn't valid UTF-8 hit the
        same kind of unguarded raise a step later, from .decode("utf-8").

        All three now degrade to "no form fields" (an empty dict, same as
        parse_qs("") would give) instead of raising -- a request malformed
        enough to hit any of them was never going to carry usable field
        values anyway, so failing open here costs nothing a real request
        would notice."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw_body = self.rfile.read(length)
        try:
            raw_body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return {}
        return parse_qs(raw_body)

    def do_GET(self):
        # See _common._request_cache: gives every GET its own fresh read
        # of pending_drafts.json/personal_tasks.json the first time each
        # is actually needed this request, instead of carrying over
        # whatever an earlier request on this same worker thread happened
        # to cache.
        _common.clear_request_cache()
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        banner = unquote(qs.get("msg", [""])[0])
        # Only ever set by _dismiss's own redirect (see do_POST) -- every
        # other action's redirect has nothing to undo, so this is "" (and
        # _page's banner_undo_path renders nothing extra) everywhere else.
        banner_undo_path = unquote(qs.get("undo", [""])[0])
        # /firstreply's own sort toggle -- see the priority-sort block below.
        # Anything other than the one recognized value falls back to the
        # default (oldest-first, i.e. no query param at all), same
        # fail-open posture used throughout this handler.
        sort_param = qs.get("sort", [""])[0]

        if parsed.path == "/reconnect":
            self._send_html(_render_reconnect_page(banner=banner))
            return

        if parsed.path == "/tasks":
            # Reconciles against live Zendesk status first (catches a task
            # solved/closed directly in Zendesk rather than via this page's
            # own "Mark done" button) -- skipped when Zendesk isn't
            # authenticated right now, same fail-open posture as the rest
            # of this page, since open_personal_tasks() below is a pure
            # local read either way.
            client = _common.make_client()
            if client.client is not None:
                _common.reconcile_personal_tasks(client, log_fn=_log)
            draft_tasks = _common.draft_personal_tasks()
            open_tasks = _common.open_personal_tasks()
            self._send_html(_render_tasks_page(draft_tasks, open_tasks, banner=banner))
            return

        if parsed.path == "/flagged":
            # Read-only, like the IT-Security column -- see
            # _common.fetch_flagged_tickets for the cache that keeps this
            # from turning every page load into a fresh multi-page Zendesk
            # scan.
            client = _common.make_client()
            flagged = _common.fetch_flagged_tickets(client, log_fn=_log)
            self._send_html(_render_flagged_page(
                flagged["items"], flagged["error"] or "", flagged.get("capped", False), banner=banner
            ))
            return

        if parsed.path == "/security":
            # Own page now, previously a side panel embedded directly in
            # "/" below -- see _render_security_page. Same cached fetch
            # (_common.fetch_recent_tickets) as before, just called from
            # here instead.
            client = _common.make_client()
            security = _common.fetch_recent_tickets(client, _common.IT_SECURITY_GROUP_ID, limit=_common.IT_SECURITY_COLUMN_LIMIT, log_fn=_log)
            self._send_html(_render_security_page(security["items"], security["error"] or "", banner=banner))
            return

        if parsed.path == "/sla":
            # Live four-rule SLA evaluation (_common.fetch_sla_tickets,
            # cached the same SLA_CACHE_TTL_SECONDS as Discussion/Security's
            # own panels) plus a read-only look at the sibling sla-report-
            # slack project's own history file (_common.sla_history_snapshot,
            # never written here) for the trend chart -- see
            # _render_sla_page.
            client = _common.make_client()
            sla_result = _common.fetch_sla_tickets(client, log_fn=_log)
            history = _common.sla_history_snapshot()
            self._send_html(_render_sla_page(sla_result, history, banner=banner))
            return

        if parsed.path == "/solved":
            # Read-only, same posture as /security and /flagged -- see
            # _common.fetch_recently_solved_tickets for the cache/scan-depth
            # details.
            client = _common.make_client()
            solved = _common.fetch_recently_solved_tickets(client, log_fn=_log)
            self._send_html(_render_solved_page(
                solved["items"], solved["error"] or "", solved.get("capped", False), banner=banner
            ))
            return

        if parsed.path == "/overview":
            # Every one of these three is the exact same cached/local read
            # its own dedicated page already makes -- see
            # _render_overview_page's docstring for why that's true even
            # though this route calls all three at once (nothing here adds
            # a new Zendesk scan beyond what visiting each page
            # individually would already trigger). First replies used to
            # be a fourth read here (_common.pending_items()) but that
            # queue is now /firstreply's own page, not a tile duplicated
            # onto Overview too.
            client = _common.make_client()
            open_tasks = _common.open_personal_tasks()
            draft_tasks = _common.draft_personal_tasks()
            flagged = _common.fetch_flagged_tickets(client, log_fn=_log)
            security = _common.fetch_recent_tickets(
                client, _common.IT_SECURITY_GROUP_ID, limit=_common.IT_SECURITY_COLUMN_LIMIT, log_fn=_log
            )
            # Purely local (pending_drafts.json only, see
            # _common.personal_activity_summary's docstring) -- costs
            # nothing extra beyond the reads above, no Zendesk call.
            activity_summary = _common.personal_activity_summary()
            self._send_html(_render_overview_page(
                open_tasks, draft_tasks, flagged["items"], security["items"],
                flagged_error=flagged["error"] or "", security_error=security["error"] or "",
                activity_summary=activity_summary,
                banner=banner,
            ))
            return

        if parsed.path == "/":
            # The main queue's real URL, below -- kept alive as a redirect
            # (not removed, not still rendering the page itself) so an old
            # bookmark or muscle-memory "/" still lands somewhere useful
            # rather than a 404. Drops the query string on purpose (a
            # ?sort=priority from an old link isn't worth preserving
            # through the extra hop for something this rare).
            self._redirect("/firstreply")
            return

        if parsed.path != "/firstreply":
            self._send_html(_page('<p class="empty">Not found.</p>'), status=404)
            return

        items = (
            sorted(_common.pending_items(), key=_priority_sort_key)
            if sort_param == "priority"
            else sorted(_common.pending_items(), key=lambda i: i.get("created_at", ""))
        )
        # .ticket-list wraps just the ticket cards (not the auth-warning
        # banner or filter/sort controls that can sit above them in
        # .main-col) so its CSS nth-child stagger (see .ticket-list >
        # .ticket's rules) counts cleanly among tickets only -- nth-child
        # counts ALL siblings regardless of class, so without this
        # wrapper the stagger delays would shift depending on whether an
        # auth warning happened to be showing that day.
        ticket_html = (
            '<div class="ticket-list">' + "\n".join(_render_ticket(i) for i in items) + "</div>"
            if items
            else '<p class="empty">You\'re all caught up -- nothing waiting on you right now.</p>'
        )
        # Filter box + sort toggle sit directly above the queue -- only
        # worth showing once there's actually more than an empty-state
        # message to act on.
        list_controls_html = ""
        if items:
            sort_toggle_html = (
                f'<a class="sort-toggle" href="/firstreply">{_icon("chevron-down", "icon-sm")}<span>Oldest first</span></a>'
                if sort_param == "priority"
                else f'<a class="sort-toggle" href="/firstreply?sort=priority">{_icon("bar-chart", "icon-sm")}<span>Priority first</span></a>'
            )
            list_controls_html = (
                f'<div class="list-controls">'
                f'{_filter_box_html(".main-col .ticket")}'
                f'{sort_toggle_html}'
                f'</div>'
            )
        main_col_html = (
            _auth_warning_html() + list_controls_html + ticket_html + _render_recently_handled()
        )
        # The one live Zendesk call GET /firstreply now makes -- see
        # fetch_recently_worked_on's own docstring for why /firstreply
        # otherwise never touching Zendesk still holds in spirit: the
        # local-only half of this result is always available on its own,
        # so a disconnected or slow Zendesk degrades this one sidebar,
        # never breaks the page.
        client = _common.make_client()
        worked_on = _common.fetch_recently_worked_on(client, log_fn=_log)
        # .main-with-sidebar (see CSS) -- "recently worked on" history column
        # pinned to the left of the queue; see _render_worked_on_sidebar
        # (which now also covers what used to be a separate "Last N
        # replies" section here).
        body = (
            f'<div class="main-with-sidebar">'
            f'{_render_worked_on_sidebar(worked_on["items"], worked_on.get("capped", False), worked_on.get("zendesk_unavailable", False))}'
            f'<div class="main-col">{main_col_html}</div>'
            f'</div>'
        )

        self._send_html(_page(
            body, banner=banner, banner_undo_path=banner_undo_path, auto_refresh=True,
            header_meta=_header_meta_html(),
            current_path="/firstreply",
        ))

    def do_POST(self):
        # See do_GET's identical call -- a POST branch can still render a
        # page directly (e.g. the already-replied confirm interstitial)
        # rather than just redirecting, so this needs the same fresh-per-
        # request cache reset.
        _common.clear_request_cache()
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")

        if parts == ["refresh"]:
            # return_to (see _refresh_button_html's hidden field) sends you
            # back to whichever page you actually clicked Refresh from --
            # the button now renders on every page (see .brand-status-row
            # in _page()), so hardcoding the redirect to /firstreply would
            # bounce you to a different page than the one you were just
            # looking at. Whitelisted rather than trusted outright, same
            # reasoning as the "claim" action's own return_to below: this
            # is a value from the request body, and blindly redirecting
            # wherever it says would be an open redirect.
            form = self._parse_form_body()
            return_to = form.get("return_to", ["/firstreply"])[0]
            if return_to not in ("/firstreply", "/overview", "/tasks", "/flagged", "/security", "/solved", "/reconnect"):
                return_to = "/firstreply"
            message = self._refresh()
            self._redirect(f"{return_to}?msg={quote(message)}")
            return

        if parts == ["tasks", "new"]:
            form = self._parse_form_body()
            subject = form.get("subject", [""])[0]
            notes = form.get("notes", [""])[0]
            category = form.get("category", [""])[0]
            est_mins = form.get("est_mins", [""])[0]
            message = self._add_task(subject, notes, est_mins, category)
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if parts == ["tasks", "new", "quick-solve"]:
            form = self._parse_form_body()
            subject = form.get("subject", [""])[0]
            notes = form.get("notes", [""])[0]
            category = form.get("category", [""])[0]
            est_mins = form.get("est_mins", [""])[0]
            message = self._add_and_quick_solve_task(subject, notes, est_mins, category)
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "edit":
            form = self._parse_form_body()
            subject = form.get("subject", [""])[0]
            notes = form.get("notes", [""])[0]
            category = form.get("category", [""])[0]
            est_mins = form.get("est_mins", [""])[0]
            message = self._edit_task(parts[1], subject, notes, est_mins, category)
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "move" and parts[3] in ("up", "down"):
            message = self._move_task(parts[1], parts[3])
            # #task-<local_id> (not just "/tasks") so the row that just
            # moved gets the same .mini-ticket:target highlight a task
            # title link already lands on -- after a reorder, that row's
            # position in the list just changed, so calling out which one
            # it was saves re-hunting for it in the new order.
            self._redirect(f"/tasks?msg={quote(message)}#task-{quote(parts[1])}")
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "promote":
            form = self._parse_form_body()
            est_mins = form.get("est_mins", [""])[0]
            assignee_identity = form.get("assignee_identity", [""])[0]
            requester_identity = form.get("requester_identity", [""])[0]
            allocation = form.get("allocation", [""])[0]
            message = self._promote_task(parts[1], est_mins, assignee_identity, requester_identity, allocation)
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "discard":
            message = self._discard_task(parts[1])
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "done":
            form = self._parse_form_body()
            # Only actually present (and required) on a legacy task's row --
            # see _render_open_task_row's legacy_est_input -- blank for
            # every normal task, which already has its own est_mins on file.
            est_mins = form.get("est_mins", [""])[0]
            message = self._complete_task(parts[1], est_mins)
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "quick-solve":
            form = self._parse_form_body()
            est_mins = form.get("est_mins", [""])[0]
            message = self._quick_solve_task(parts[1], est_mins)
            self._redirect(f"/tasks?msg={quote(message)}")
            return

        if parts == ["reconnect", "start"]:
            message = self._reconnect_start()
            self._redirect(f"/reconnect?msg={quote(message)}")
            return

        if parts == ["reconnect", "complete"]:
            form = self._parse_form_body()
            callback_url = form.get("callback_url", [""])[0]
            message, redirect_to = self._reconnect_complete(callback_url)
            self._redirect(f"{redirect_to}?msg={quote(message)}")
            return

        if len(parts) != 2 or parts[0] not in ("post_public", "post_internal", "dismiss", "undo_dismiss", "claim"):
            self._send_html(_page('<p class="empty">Not found.</p>'), status=404)
            return
        action, ticket_id = parts

        form = self._parse_form_body()

        if action == "claim":
            # return_to (see _render_readonly_ticket_row's hidden field)
            # sends you back to whichever page the Claim button was
            # actually clicked from -- "/firstreply" (the main queue's own
            # claim button doesn't set this, so it falls through to this
            # same default), "/security", "/flagged", or "/overview" (its
            # Discussion and Security sections reuse the same row/claim
            # form -- see _render_overview_page) -- rather than always
            # bouncing back to /firstreply regardless of where you were.
            # ("/sla" used to be in this list too, for its awaiting-
            # first-response list's own Claim buttons -- removed along
            # with that list, see _render_sla_page.)
            # Whitelisted rather than trusted outright: this is a value
            # from the request body, and blindly redirecting wherever it
            # says would be an open redirect.
            return_to = form.get("return_to", ["/firstreply"])[0]
            if return_to not in ("/firstreply", "/security", "/flagged", "/overview"):
                return_to = "/firstreply"
            message = self._claim(ticket_id)
            self._redirect(f"{return_to}?msg={quote(message)}")
            return

        edited_draft = form.get("draft", [""])[0]

        if action == "dismiss":
            message, undo_ticket_id = self._dismiss(ticket_id)
            undo_qs = f"&undo={quote(f'/undo_dismiss/{undo_ticket_id}')}" if undo_ticket_id else ""
            self._redirect(f"/firstreply?msg={quote(message)}{undo_qs}")
            return

        if action == "undo_dismiss":
            message = self._undo_dismiss(ticket_id)
            self._redirect(f"/firstreply?msg={quote(message)}")
            return

        confirmed = form.get("confirm", [""])[0] == "yes"
        result = self._post(ticket_id, edited_draft, public=(action == "post_public"), confirmed=confirmed)
        if isinstance(result, tuple):
            # Needs a human decision before posting -- render the confirm
            # interstitial directly (200) instead of redirecting, so the
            # "already replied" reason and the draft survive to the next
            # click without a round trip through pending_drafts.json.
            self._send_html(result[1])
            return
        self._redirect(f"/firstreply?msg={quote(result)}")

    def _reconnect_start(self) -> str:
        client = _common.make_client()
        try:
            result = client.begin_oauth_authorization()
        except Exception as exc:
            _log(f"Could not start OAuth reconnect: {exc}")
            return f"Could not start reconnect: {exc}"
        _common.save_oauth_pending(result["authorization_url"], result["state"])
        _log("OAuth reconnect flow started -- authorization link generated.")
        return "Click the authorization link below, then paste the redirect URL back here."

    def _reconnect_complete(self, callback_url: str):
        """Returns (message, redirect_path)."""
        pending = _common.load_oauth_pending()
        if not pending:
            return ("No reconnect in progress (or it expired) -- start over.", "/reconnect")

        parsed = urlparse(callback_url.strip())
        qs = parse_qs(parsed.query)
        code = qs.get("code", [""])[0]
        state = qs.get("state", [""])[0]
        if not code or not state:
            return (
                "Couldn't find code/state in that URL -- paste the full URL you were "
                "redirected to, including everything after the ?.",
                "/reconnect",
            )

        client = _common.make_client()
        # complete_oauth_authorization checks `state` against
        # client._pending_oauth_state, which normally lives in memory on
        # whichever client instance called begin_oauth_authorization. Since
        # that instance didn't survive between this request and the earlier
        # /reconnect/start request, we re-inject the state we persisted
        # ourselves -- see _common.save_oauth_pending's docstring.
        client._pending_oauth_state = pending["state"]
        try:
            result = client.complete_oauth_authorization(code, state)
        except Exception as exc:
            _log(f"OAuth reconnect failed: {exc}")
            return (f"Reconnect failed: {exc}. The link may have expired -- try 'Start over.'", "/reconnect")

        _common.clear_oauth_pending()
        _common.mark_auth_ok()
        _common.clear_failure_notified()
        expires_in = result.get("expires_in")
        _log(f"OAuth reconnected successfully (expires_in={expires_in}).")
        return (f"Zendesk reconnected -- new token expires in {expires_in} seconds.", "/firstreply")

    def _refresh(self) -> str:
        """Runs the exact same scan-and-draft pass the cron script runs every
        2 minutes, but right now, on demand -- so you don't have to wait out
        the schedule after handling something urgent. Shares the same
        run-lock as the cron script, so this and a scheduled tick can never
        hit Zendesk at the same moment; deliberately does NOT check business
        hours, since clicking this button is itself the request to check
        right now."""
        run_lock = _common.acquire_run_lock()
        if run_lock is None:
            return "A scan is already in progress (likely the scheduled check) -- try again in a moment."
        try:
            client = _common.make_client()
            if client.client is None:
                _common.mark_auth_broken("not authenticated (checked during manual refresh)")
                return "Can't refresh: Zendesk client isn't authenticated right now. See /reconnect."
            try:
                candidates, new_items, handled_elsewhere = _common.scan_and_merge(client, log_fn=_log)
            except Exception as exc:
                _log(f"Manual refresh failed: {exc}")
                return f"Refresh failed: {exc}"
            _common.mark_auth_ok()
            _common.clear_failure_notified()
            handled_part = (
                f" {len(handled_elsewhere)} pending ticket(s) already got a reply from someone else "
                f"and were pulled from the queue."
                if handled_elsewhere
                else ""
            )
            if not candidates:
                return f"Refreshed -- no tickets currently awaiting a first reply.{handled_part}"
            return f"Refreshed -- {len(candidates)} candidate(s) found, {len(new_items)} new.{handled_part}"
        finally:
            run_lock.close()

    def _add_task(self, subject: str, notes: str, est_mins, category: str) -> str:
        """The /tasks page's quick-add form -- saves a LOCAL-ONLY draft via
        _common.add_personal_task. Deliberately makes no Zendesk call at
        all (unlike the earlier version of this feature): see
        _promote_task below for the action that actually creates the
        ticket. est_mins/category are both optional here -- see
        add_personal_task's docstring."""
        result = _common.add_personal_task(subject, notes, est_mins=est_mins, category=category, log_fn=_log)
        if not result["ok"]:
            return f"Could not add task: {result['error']}"
        return "Saved -- not yet created in Zendesk."

    def _add_and_quick_solve_task(self, subject: str, notes: str, est_mins, category: str) -> str:
        """The /tasks page's "New task" form's second button -- "Add +
        Quick Solve" -- collapses the normal add -> promote -> quick-solve
        sequence into one click, chaining the same three _common functions
        _add_task/_promote_task/_quick_solve_task each call individually.

        Unlike plain "Add task", this one actually needs est_mins up front
        (promote_personal_task requires it), but the shared form input
        stays optional in HTML -- it's also used by "Add task" alone,
        which must NOT be forced to fill it in -- so that's validated here
        instead, before anything is created.

        Each step below already leaves the task in a normal, visible,
        non-broken state on its own failure (nothing needs rolling back):
        a failed promote just leaves an ordinary draft sitting in "Not yet
        in Zendesk", and a failed quick-solve just leaves an ordinary open
        ticket sitting in "Open" -- so the messages below say how far it
        got rather than trying to undo anything."""
        if not est_mins or not est_mins.strip():
            return (
                "Add + Quick Solve needs an estimate -- fill in Estimated minutes "
                "first (Add task alone doesn't need one)."
            )

        add_result = _common.add_personal_task(subject, notes, est_mins=est_mins, category=category, log_fn=_log)
        if not add_result["ok"]:
            return f"Could not add task: {add_result['error']}"
        local_id = add_result["local_id"]

        client = _common.make_client()
        if client.client is None:
            _common.mark_auth_broken("not authenticated (checked while creating a task's ticket)")
            return (
                "Saved as a draft, but could not create the Zendesk ticket: Zendesk "
                "isn't authenticated right now -- see /reconnect and try again "
                "(nothing lost -- it's sitting in \"Not yet in Zendesk\")."
            )

        agent_user_id = _common.resolve_own_agent_id(client, log_fn=_log)
        promote_result = _common.promote_personal_task(client, local_id, agent_user_id, est_mins, log_fn=_log)
        if not promote_result["ok"]:
            return f"Saved as a draft, but could not create the Zendesk ticket: {promote_result['error']}"
        _common.mark_auth_ok()

        ticket_id = promote_result["ticket_id"]
        url_part = f" ({promote_result['url']})" if promote_result.get("url") else ""

        solve_result = _common.quick_solve_personal_task(client, local_id, est_mins=est_mins, log_fn=_log)
        if not solve_result["ok"]:
            return (
                f"Created Zendesk ticket #{ticket_id}{url_part}, but could not "
                f"quick-solve it: {solve_result['error']} (it's sitting in "
                f"\"Open\" -- use Quick Solve there when ready)."
            )

        message = f"Created and solved Zendesk ticket #{ticket_id}{url_part}."
        warnings = [w for w in (promote_result.get("warning"), solve_result.get("warning")) if w]
        if warnings:
            message += f" ({'; '.join(warnings)})"
        return message

    def _edit_task(self, local_id: str, subject: str, notes: str, est_mins, category: str) -> str:
        """The /tasks page's "Edit" action on a draft row -- see
        _common.edit_personal_task's docstring for why this refuses to
        act on anything already promoted."""
        result = _common.edit_personal_task(local_id, subject, notes, est_mins=est_mins, category=category, log_fn=_log)
        if not result["ok"]:
            return f"Could not save changes: {result['error']}"
        return "Task updated."

    def _move_task(self, local_id: str, direction: str) -> str:
        """The /tasks page's Move up/down buttons on a draft row -- see
        _common.move_personal_task_draft's docstring."""
        result = _common.move_personal_task_draft(local_id, direction, log_fn=_log)
        if not result["ok"]:
            return f"Could not reorder task: {result['error']}"
        return "Reordered."

    def _promote_task(self, local_id: str, est_mins, assignee_identity: str, requester_identity: str, allocation: str) -> str:
        """The /tasks page's "Make Zendesk Ticket from Task" button -- see
        _common.promote_personal_task's docstring for the type=task,
        PERSONAL_TASK_TAG-tagged ticket this creates (with the Estimated
        Minutes/Assignee/Requester/Allocation values just chosen on the
        form). Deliberately posts no comment at creation time -- see
        _quick_solve_task for where that now happens.

        The success banner includes the new ticket's own URL (promote_
        personal_task's "url" key), the same "(https://.../tickets/N)"
        convention _post() already uses for its own "Confirmed: ... sent"
        banner -- _linkify() (see _page()) turns any plain URL surviving
        html.escape() in banner text into a clickable link automatically,
        so no HTML needs to be built here at all; just including the URL
        as plain text is enough to get a real link on the page."""
        client = _common.make_client()
        if client.client is None:
            _common.mark_auth_broken("not authenticated (checked while creating a task's ticket)")
            return "Could not create the Zendesk ticket: Zendesk isn't authenticated right now -- see /reconnect and try again."

        agent_user_id = _common.resolve_own_agent_id(client, log_fn=_log)
        result = _common.promote_personal_task(
            client, local_id, agent_user_id, est_mins,
            identity_assignee=assignee_identity, identity_requester=requester_identity, allocation=allocation,
            log_fn=_log,
        )
        if not result["ok"]:
            return f"Could not create the Zendesk ticket: {result['error']}"

        _common.mark_auth_ok()
        url_part = f" ({result['url']})" if result.get("url") else ""
        message = f"Created Zendesk ticket #{result['ticket_id']}{url_part}."
        if result.get("warning"):
            message += f" ({result['warning']})"
        return message

    def _discard_task(self, local_id: str) -> str:
        """The /tasks page's "Discard" button, for a draft that never
        became a real ticket -- see _common.discard_personal_task's
        docstring for why this refuses to act on anything already
        promoted."""
        result = _common.discard_personal_task(local_id, log_fn=_log)
        if not result["ok"]:
            return f"Could not discard task: {result['error']}"
        return "Discarded."

    def _complete_task(self, local_id: str, est_mins: str = "") -> str:
        """The /tasks page's "Mark done" button -- see
        _common.complete_personal_task's docstring. est_mins only matters
        (and is only ever non-blank) for a legacy task with none on file
        already -- see _render_open_task_row's legacy_est_input and
        _common._resolve_est_mins_for_solve."""
        client = _common.make_client()
        if client.client is None:
            _common.mark_auth_broken("not authenticated (checked while marking a task done)")
            return "Could not mark task done: Zendesk isn't authenticated right now -- see /reconnect and try again."

        result = _common.complete_personal_task(client, local_id, est_mins=est_mins, log_fn=_log)
        if not result["ok"]:
            return f"Could not mark task done: {result['error']}"

        _common.mark_auth_ok()
        return "Marked done."

    def _quick_solve_task(self, local_id: str, est_mins: str = "") -> str:
        """The /tasks page's "Quick Solve" button -- see
        _common.quick_solve_personal_task's docstring. Posts the intake
        internal note + public reply (deferred here from ticket creation
        time) and solves the ticket in one click. est_mins is the same
        legacy-task-only value _complete_task takes, above.

        The banner wording reflects whether this specific click actually
        posted anything (result["comments_posted_this_call"]) -- a retry
        after an earlier attempt already posted both comments but then
        failed to solve shouldn't claim to have "posted the reply/note"
        a second time when it didn't."""
        client = _common.make_client()
        if client.client is None:
            _common.mark_auth_broken("not authenticated (checked while quick-solving a task)")
            return "Could not quick-solve: Zendesk isn't authenticated right now -- see /reconnect and try again."

        result = _common.quick_solve_personal_task(client, local_id, est_mins=est_mins, log_fn=_log)
        if not result["ok"]:
            return f"Could not quick-solve: {result['error']}"

        _common.mark_auth_ok()
        if result.get("comments_posted_this_call"):
            message = "Quick-solved: posted the reply/note and marked the ticket solved."
        else:
            message = "Marked the ticket solved (the intake note/reply had already posted on an earlier attempt)."
        if result.get("warning"):
            message += f" ({result['warning']})"
        return message

    def _dismiss(self, ticket_id: str) -> "tuple[str, str | None]":
        """Removes a ticket from the pending queue without posting anything
        to Zendesk -- for cases where you've decided this tool shouldn't be
        the one to handle it (already dealt with outside this page, not
        actually IT-Operations' to answer, etc.). Unlike _post(), this makes
        no Zendesk API call at all.

        Deliberately marks the entry "dismissed" and keeps it in
        pending_drafts.json rather than deleting it outright -- if it were
        deleted, and Zendesk still shows no public reply on it, the very
        next scan would just re-detect it as a "new" candidate and put it
        right back on the page, defeating the point of dismissing it. That
        same keep-the-entry choice is also what makes _undo_dismiss (below)
        possible: reversing a Remove just flips this same entry's status
        back to "pending" rather than trying to reconstruct one from
        scratch, since nothing about it was ever actually deleted.

        Re-checks the entry's status INSIDE the same locked read-modify-
        write as the mutation, rather than trusting the plain read taken
        above -- the browser tab that rendered this Remove button may be up
        to a minute stale (the page auto-refreshes every 60s), and in that
        window a background scan could have already transitioned this exact
        ticket to "already_handled" (someone else replied) or it could have
        been posted/dismissed by another tab. Blindly overwriting status to
        "dismissed" in that case would silently discard that outcome (e.g.
        losing the "already answered elsewhere" resolution note) and
        mischaracterize what actually happened as "you removed it" when you
        didn't -- the same class of bug as posting a redundant reply
        because a stale check didn't reflect the ticket's current state.

        Returns (message, ticket_id) on an actual dismiss -- that second
        value is what do_POST turns into the toast's "Undo" button (see
        _page's banner_undo_path) -- or (message, None) for the two cases
        where there's nothing to undo because nothing was actually
        dismissed just now (already gone, or already in some other
        state)."""
        existing = _common.load_pending().get(ticket_id, {})
        subject = existing.get("subject", "")
        url = existing.get("url", "")
        subject_part = f' — "{subject}"' if subject else ""
        url_part = f" ({url})" if url else ""

        outcome = {"result": "missing"}

        def _mutate(data):
            entry = data.get(ticket_id)
            if not entry:
                outcome["result"] = "missing"
                return
            if entry.get("status") != "pending":
                outcome["result"] = "changed"
                outcome["status"] = entry.get("status")
                return
            entry["status"] = "dismissed"
            entry["dismissed_at"] = datetime.now().isoformat(timespec="seconds")
            outcome["result"] = "dismissed"

        _common.update_pending(_mutate)

        if outcome["result"] == "missing":
            return (f"#{ticket_id} isn't tracked (it may have already been cleared) -- nothing to remove.", None)

        if outcome["result"] == "changed":
            status = outcome.get("status")
            _log(f"Remove clicked for #{ticket_id}, but it had already changed to '{status}' -- left it as-is.")
            return (
                f"#{ticket_id}{subject_part}{url_part} had already changed to '{status}' (likely a "
                f"background scan noticing someone else replied, or another tab acting on it) before "
                f"Remove was clicked here -- left it as-is rather than overwriting that. Reload the page "
                f"to see its current state.",
                None,
            )

        _log(f"Dismissed #{ticket_id} from the pending queue (no Zendesk post made).")
        return (
            f"Removed #{ticket_id}{subject_part}{url_part} from the pending queue. Nothing was posted "
            f"to Zendesk. Handle it directly in Zendesk if it still needs a reply -- or click Undo if "
            f"that was a misclick.",
            ticket_id,
        )

    def _undo_dismiss(self, ticket_id: str) -> str:
        """Reverses a Remove click (see _dismiss, above) by flipping a
        'dismissed' entry back to 'pending' -- the entry and every field it
        had (subject, draft text, requester, url, ...) is still sitting in
        pending_drafts.json exactly as _dismiss left it, since dismissing
        never deletes anything, just relabels it. So undoing is just
        relabeling it back, not reconstructing anything.

        Same race-guard shape as _dismiss itself, mirrored: only proceeds
        if the entry is STILL 'dismissed' at the moment of the locked
        read-modify-write. The toast this is reached from can go stale
        just as easily as the ticket queue can (see _dismiss's own
        docstring) -- if a background scan already moved this entry
        somewhere else (say, "already_handled" because someone replied on
        Zendesk in the meantime) between the Remove click and this Undo
        click, silently overwriting that back to 'pending' would be wrong
        for the exact same reason overwriting a fresher status in _dismiss
        would be."""
        existing = _common.load_pending().get(ticket_id, {})
        subject = existing.get("subject", "")
        subject_part = f' — "{subject}"' if subject else ""

        outcome = {"result": "missing"}

        def _mutate(data):
            entry = data.get(ticket_id)
            if not entry:
                outcome["result"] = "missing"
                return
            if entry.get("status") != "dismissed":
                outcome["result"] = "changed"
                outcome["status"] = entry.get("status")
                return
            entry["status"] = "pending"
            entry.pop("dismissed_at", None)
            outcome["result"] = "restored"

        _common.update_pending(_mutate)

        if outcome["result"] == "missing":
            return f"#{ticket_id} isn't tracked anymore -- nothing to restore."

        if outcome["result"] == "changed":
            status = outcome.get("status")
            _log(f"Undo clicked for #{ticket_id}, but it was already '{status}' (not 'dismissed') -- left it as-is.")
            return (
                f"#{ticket_id}{subject_part} is already '{status}' now, not still 'dismissed' -- "
                f"left it as-is rather than overwriting that."
            )

        _log(f"Restored #{ticket_id} to the pending queue (undo of a Remove).")
        return f"Restored #{ticket_id}{subject_part} to the pending queue."

    def _claim(self, ticket_id: str) -> str:
        """Self-assigns a ticket to this tool's own configured Zendesk
        account -- see _common.claim_ticket's docstring for the full
        rationale. Works the same way whether ticket_id came from the
        IT-Operations pending queue (its local entry gets updated in place)
        or the read-only IT-Security column (nothing local to update;
        claim_ticket() is a no-op there beyond the Zendesk API call
        itself)."""
        client = _common.make_client()
        if client.client is None:
            _common.mark_auth_broken("not authenticated (checked during claim)")
            return f"Could not claim #{ticket_id}: Zendesk isn't authenticated right now -- see /reconnect and try again."

        agent_user_id = _common.resolve_own_agent_id(client, log_fn=_log)
        if agent_user_id is None:
            return (
                f"Could not claim #{ticket_id}: couldn't resolve this tool's own Zendesk account id "
                f"(ZENDESK_EMAIL search failed or returned no match) -- nothing was changed."
            )

        result = _common.claim_ticket(client, ticket_id, agent_user_id, log_fn=_log)
        if not result["ok"]:
            return f"Could not claim #{ticket_id}: {result['error']}."

        _common.mark_auth_ok()
        return f"Claimed #{ticket_id} -- now assigned to you in Zendesk."

    def _post(self, ticket_id: str, draft_text: str, public: bool, confirmed: bool = False):
        """Posts the (possibly edited) draft as either a public reply, visible
        to the requester, or an internal comment, visible only to agents --
        same `public` flag Zendesk's own comment composer uses. The only way
        to clear a candidate without posting is the separate Remove button
        (see _dismiss(), above) -- there's still no implicit auto-dismiss;
        every candidate stays pending until you explicitly act on it one way
        or another.

        Returns EITHER a plain string (the redirect banner message, for the
        normal success/failure paths) OR a ("confirm", html_bytes) tuple
        when check_already_replied finds an existing reply and `confirmed`
        isn't already True -- the caller (do_POST) renders that page
        directly instead of redirecting. This applies to both public
        replies and internal comments alike: previously a public reply
        silently skipped posting (no ask) and an internal comment silently
        posted anyway with only an after-the-fact warning noted -- neither
        gave you a chance to decide first. Now both pause for an explicit
        "post anyway" click before anything happens, since "someone else
        already replied" is exactly the kind of thing you'd want a say in,
        not a judgment call this tool should make for you either way.

        On success, the returned message deliberately echoes back the exact
        text that was sent (not just "done") plus an explicit "confirmed
        posted" statement -- once a ticket posts, its card disappears from
        the page (only pending items render), so this banner is the only
        place left to see what actually went out and know for certain it
        reached Zendesk rather than silently failing.

        Guarded by _common._begin_pending_ticket_action/_end_pending_ticket_action
        for the same reason promote_personal_task/complete_personal_task are
        guarded (see that guard's comment in _common.py): a double-click, or
        a browser retrying a slow request, is a real scenario on this
        ThreadingHTTPServer, and without this guard both overlapping
        requests could pass the already-replied check below and both call
        post_comment -- posting the same reply or internal comment twice.
        The confirm-first-then-post-on-a-second-request flow above still
        works fine across two SEPARATE requests, since the guard is only
        held for the duration of one call to this method, not across the
        round trip to the confirm page and back."""
        kind = "public reply" if public else "internal comment"
        kind_label = "PUBLIC REPLY" if public else "INTERNAL COMMENT"

        if not _common._begin_pending_ticket_action(ticket_id):
            return f"Could not post the {kind} for #{ticket_id}: another post to this same ticket is already in progress -- please wait a moment and refresh."

        try:
            existing = _common.load_pending().get(ticket_id, {})
            subject = existing.get("subject", "")
            url = existing.get("url", "")
            requester_id = existing.get("requester_id")
            subject_part = f' — "{subject}"' if subject else ""
            url_part = f" ({url})" if url else ""

            # Fresh under the guard, not just the value read above -- if a
            # just-finished overlapping post already flipped this ticket out
            # of "pending" (e.g. posted, or dismissed) in the moment between
            # this request arriving and acquiring the guard, there's nothing
            # left here to post.
            if existing.get("status") and existing.get("status") != "pending":
                return f"Could not post the {kind} for #{ticket_id}: it's no longer pending (status: {existing['status']}) -- it may have just been handled."

            client = _common.make_client()
            if client.client is None:
                _common.mark_auth_broken("not authenticated (checked during post)")
                _log(f"Post ({kind}) for #{ticket_id} blocked: Zendesk client not authenticated.")
                return (
                    f"Could not post the {kind} for #{ticket_id}: Zendesk client isn't "
                    f"authenticated right now. Left it pending -- see /reconnect and try again."
                )

            # Re-check right before posting, not just at detection time -- a
            # candidate can sit pending indefinitely (no reject/dismiss action),
            # and someone else may have replied to it directly in Zendesk since
            # it was first drafted. Posting our template on top of that would be
            # redundant at best, contradictory at worst. agent_user_id lets this
            # recognize OUR OWN prior reply as "already answered" even on a
            # self-created test ticket where the requester and the replying
            # account are the same Zendesk user -- see resolve_own_agent_id.
            agent_user_id = _common.resolve_own_agent_id(client, log_fn=_log)
            reply_check = _common.check_already_replied(client, ticket_id, requester_id, agent_user_id=agent_user_id, log_fn=_log)

            if reply_check["already_replied"] and not confirmed:
                _log(f"Post ({kind}) for #{ticket_id} needs confirmation: {reply_check['reason']}")
                return ("confirm", _render_confirm_page(ticket_id, draft_text, public, reply_check, subject, url))

            try:
                client.post_comment(int(ticket_id), draft_text, public=public)
            except Exception as exc:
                _log(f"Post ({kind}) for #{ticket_id} failed: {exc}")
                return f"Could not post the {kind} for #{ticket_id}: {exc}. Left it pending so nothing is lost."

            _common.mark_auth_ok()

            def _mutate(data):
                if ticket_id in data:
                    data[ticket_id]["status"] = "posted"
                    data[ticket_id]["draft"] = draft_text
                    data[ticket_id]["posted_at"] = datetime.now().isoformat(timespec="seconds")
                    data[ticket_id]["posted_public"] = public

            _common.update_pending(_mutate)
            _log(f"Posted {kind} to #{ticket_id}." + (" (confirmed despite an existing reply)" if reply_check["already_replied"] else ""))

            excerpt = draft_text if len(draft_text) <= 300 else draft_text[:297] + "..."
            warning = ""
            if reply_check["already_replied"]:
                # You explicitly confirmed posting anyway (public or internal) --
                # still worth flagging in the banner as a record of that choice.
                warning = f" Note: you confirmed posting despite {reply_check['reason']}."
            return (
                f"Confirmed: {kind_label} sent to #{ticket_id}{subject_part}{url_part}. "
                f"Text sent to Zendesk: “{excerpt}”{warning}"
            )
        finally:
            _common._end_pending_ticket_action(ticket_id)


def main() -> None:
    server = ThreadingHTTPServer((BIND_HOST, _common.REVIEW_SERVER_PORT), Handler)
    _log(f"Zendesk review server listening on http://{BIND_HOST}:{_common.REVIEW_SERVER_PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
