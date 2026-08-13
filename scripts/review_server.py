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
import html
import sys
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
# schedule, or the explicit "Refresh from Zendesk now" button).
AUTO_REFRESH_SECONDS = 60

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "csa-logo.png"


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


def _log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def _page(body: str, banner: str = "", auto_refresh: bool = False) -> bytes:
    banner_html = f'<div class="banner">{html.escape(banner)}</div>' if banner else ""
    logo_html = f'<img src="{LOGO_DATA_URI}" alt="Cloud Security Alliance" class="logo">' if LOGO_DATA_URI else ""
    # Skips the reload while a textarea has an unsaved edit in it (tracked via
    # a plain 'input' listener), so auto-refresh can't silently blow away
    # something you're in the middle of writing. Reloads to the bare path
    # (dropping any ?msg=... query string) rather than the exact current URL,
    # so a flash banner from your last action doesn't reappear on every tick.
    refresh_script = (
        f"""
<script>
(function() {{
  var dirty = false;
  document.addEventListener('input', function(e) {{
    if (e.target && e.target.tagName === 'TEXTAREA') {{ dirty = true; }}
  }});
  setInterval(function() {{
    if (!dirty) {{ window.location.href = window.location.pathname; }}
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
      var targetMs = new Date(nextEl.dataset.iso).getTime();
      var remainingSeconds = (targetMs - now) / 1000;
      if (remainingSeconds <= 0) {
        nextEl.textContent = 'any moment now';
      } else {
        var prefix = nextEl.dataset.precise === '1' ? 'in ' : '~in ';
        nextEl.textContent = prefix + formatDuration(remainingSeconds);
      }
    }
  }

  tick();
  setInterval(tick, 1000);
})();
</script>"""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Zendesk IT-Ops -- pending replies</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; margin: 0; }}
  .brand {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }}
  .logo {{ height: 36px; width: auto; }}
  .banner {{ background: #eaf6ea; border: 1px solid #a7d7a7; padding: 0.6rem 1rem; border-radius: 6px; margin-bottom: 1rem; }}
  .ticket {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin-bottom: 1.2rem; }}
  .ticket h2 {{ font-size: 1rem; margin: 0 0 0.3rem 0; }}
  .meta {{ color: #666; font-size: 0.85rem; margin-bottom: 0.6rem; }}
  textarea {{ width: 100%; min-height: 5rem; font-family: inherit; font-size: 0.95rem; box-sizing: border-box; padding: 0.5rem; border-radius: 6px; border: 1px solid #ccc; }}
  .actions {{ margin-top: 0.6rem; display: flex; gap: 0.5rem; }}
  button {{ padding: 0.5rem 1rem; border-radius: 6px; border: none; font-size: 0.9rem; cursor: pointer; }}
  .approve {{ background: #2f7d32; color: white; }}
  .internal {{ background: #6b4fa0; color: white; }}
  .reject {{ background: #b3261e; color: white; }}
  .refresh {{ background: #1a56c4; color: white; }}
  .empty {{ color: #666; }}
  a {{ color: #1a56c4; }}
  .refresh-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; }}
  .refresh-bar .meta {{ margin-bottom: 0; line-height: 1.5; }}
  .excerpt {{ font-style: italic; color: #444; background: #f7f7f7; border-left: 3px solid #ccc; padding: 0.4rem 0.6rem; margin-bottom: 0.6rem; font-size: 0.9rem; }}
  .flag {{ color: #b3261e; font-weight: 600; font-style: normal; }}
  .badge-test {{ background: #666; color: white; font-size: 0.72rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 999px; margin-left: 0.5rem; vertical-align: middle; }}
  .auth-warning {{ background: #fdeaea; border-color: #e0a0a0; }}
  input[type=text] {{ width: 100%; padding: 0.5rem; box-sizing: border-box; border-radius: 6px; border: 1px solid #ccc; margin-bottom: 0.6rem; font-size: 0.95rem; }}
</style>
</head>
<body>
<div class="brand">
  {logo_html}
  <h1>Zendesk IT-Ops -- tickets awaiting a first reply</h1>
</div>
{banner_html}
{body}
{refresh_script}
{tick_script}
</body>
</html>""".encode("utf-8")


def _status_bar() -> str:
    last_checked = _common.last_scan_completed_at()
    last_checked_text = html.escape(last_checked) if last_checked else "never yet"
    next_target = _common.next_scan_target()
    next_check_text = html.escape(_common.next_scan_description())
    counts = _common.summary_counts()
    summary = (
        f'{counts["pending"]} pending &middot; '
        f'{counts["posted_today"]} posted today'
    )
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
    return f"""
<div class="refresh-bar">
  <div class="meta">{summary}<br>Last checked: <span id="last-checked"{last_checked_attr}>{last_checked_text}</span> &middot; Next automatic check: <span id="next-check"{next_check_attr}>{next_check_text}</span> &middot; <a href="/reconnect">Zendesk OAuth</a></div>
  <form method="post" action="/refresh">
    <button class="refresh" type="submit">Refresh from Zendesk now</button>
  </form>
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
<p><a href="/">&larr; back to pending tickets</a></p>
<h2>Reconnect Zendesk OAuth</h2>
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
    <button class="approve" type="submit">Complete reconnect</button>
  </div>
</form>
<form method="post" action="/reconnect/start" style="margin-top: 1rem;">
  <button class="reject" type="submit">Start over (get a fresh link)</button>
</form>
<p class="meta">This link expires after {_common.OAUTH_PENDING_TTL_MINUTES} minutes -- if it's gone stale, start over.</p>
"""
    else:
        body = """
<p><a href="/">&larr; back to pending tickets</a></p>
<h2>Reconnect Zendesk OAuth</h2>
<p>No reconnect in progress.</p>
<form method="post" action="/reconnect/start">
  <button class="refresh" type="submit">Start Zendesk reconnect</button>
</form>
"""
    return _page(body, banner=banner)


def _relative_age(iso_ts: str) -> str:
    """Turn a created_at timestamp into 'waiting Nh' style text, so the
    oldest, most overdue tickets are obviously overdue at a glance rather
    than requiring you to compare raw timestamps yourself."""
    if not iso_ts:
        return ""
    try:
        then = datetime.fromisoformat(iso_ts)
    except Exception:
        return ""
    seconds = max((datetime.now() - then).total_seconds(), 0)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"waiting {minutes} min"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"waiting {hours}h"
    days = int(seconds // 86400)
    return f"waiting {days}d"


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
        return f"Requester: {html.escape(name or 'unknown')}"
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


def _render_ticket(item: dict) -> str:
    tid = item["ticket_id"]
    subject = html.escape(item.get("subject", ""))
    url = item.get("url") or ""
    link = f'<a href="{html.escape(url)}" target="_blank">#{tid}</a>' if url else f"#{tid}"
    draft = html.escape(item.get("draft", ""))
    age = _relative_age(item.get("created_at", ""))
    age_html = f" &middot; {age}" if age else ""
    excerpt = item.get("description_excerpt")
    excerpt_html = f'<div class="excerpt">&ldquo;{html.escape(excerpt)}&rdquo;</div>' if excerpt else ""
    badge_html = (
        f'<span class="badge-test">{html.escape((item.get("test_tag") or "TEST").upper())}</span>'
        if item.get("is_test_ticket")
        else ""
    )
    return f"""
<div class="ticket">
  <h2>{link} -- {subject}{badge_html}</h2>
  <div class="meta">{_requester_line(item)}{age_html}</div>
  {excerpt_html}
  <form method="post" action="/post_public/{tid}">
    <textarea name="draft">{draft}</textarea>
    <div class="actions">
      <button class="approve" type="submit">Public Reply</button>
      <button class="internal" formaction="/post_internal/{tid}" type="submit">Internal Comment</button>
    </div>
  </form>
</div>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        _log(f"{self.address_string()} - {fmt % args}")

    def _send_html(self, body: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        banner = unquote(qs.get("msg", [""])[0])

        if parsed.path == "/reconnect":
            self._send_html(_render_reconnect_page(banner=banner))
            return

        if parsed.path != "/":
            self._send_html(_page('<p class="empty">Not found.</p>'), status=404)
            return

        items = sorted(_common.pending_items(), key=lambda i: i.get("created_at", ""))
        ticket_html = (
            "\n".join(_render_ticket(i) for i in items)
            if items
            else '<p class="empty">Nothing waiting on you right now.</p>'
        )
        body = _auth_warning_html() + _status_bar() + ticket_html
        self._send_html(_page(body, banner=banner, auto_refresh=True))

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")

        if parts == ["refresh"]:
            message = self._refresh()
            self._redirect(f"/?msg={quote(message)}")
            return

        if parts == ["reconnect", "start"]:
            message = self._reconnect_start()
            self._redirect(f"/reconnect?msg={quote(message)}")
            return

        if parts == ["reconnect", "complete"]:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8") if length else ""
            form = parse_qs(raw_body)
            callback_url = form.get("callback_url", [""])[0]
            message, redirect_to = self._reconnect_complete(callback_url)
            self._redirect(f"{redirect_to}?msg={quote(message)}")
            return

        if len(parts) != 2 or parts[0] not in ("post_public", "post_internal"):
            self._send_html(_page('<p class="empty">Not found.</p>'), status=404)
            return
        action, ticket_id = parts

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(raw_body)
        edited_draft = form.get("draft", [""])[0]

        message = self._post(ticket_id, edited_draft, public=(action == "post_public"))
        self._redirect(f"/?msg={quote(message)}")

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
        return (f"Zendesk reconnected -- new token expires in {expires_in} seconds.", "/")

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
                candidates, new_items = _common.scan_and_merge(client, log_fn=_log)
            except Exception as exc:
                _log(f"Manual refresh failed: {exc}")
                return f"Refresh failed: {exc}"
            _common.mark_auth_ok()
            _common.clear_failure_notified()
            if not candidates:
                return "Refreshed -- no tickets currently awaiting a first reply."
            return f"Refreshed -- {len(candidates)} candidate(s) found, {len(new_items)} new."
        finally:
            run_lock.close()

    def _post(self, ticket_id: str, draft_text: str, public: bool) -> str:
        """Posts the (possibly edited) draft as either a public reply, visible
        to the requester, or an internal comment, visible only to agents --
        same `public` flag Zendesk's own comment composer uses. There is no
        separate reject/dismiss path: every candidate stays pending until you
        post it one way or the other.

        On success, the returned message deliberately echoes back the exact
        text that was sent (not just "done") plus an explicit "confirmed
        posted" statement -- once a ticket posts, its card disappears from
        the page (only pending items render), so this banner is the only
        place left to see what actually went out and know for certain it
        reached Zendesk rather than silently failing."""
        kind = "public reply" if public else "internal comment"
        kind_label = "PUBLIC REPLY" if public else "INTERNAL COMMENT"
        existing = _common.load_pending().get(ticket_id, {})
        subject = existing.get("subject", "")
        url = existing.get("url", "")

        client = _common.make_client()
        if client.client is None:
            _common.mark_auth_broken("not authenticated (checked during post)")
            _log(f"Post ({kind}) for #{ticket_id} blocked: Zendesk client not authenticated.")
            return (
                f"Could not post the {kind} for #{ticket_id}: Zendesk client isn't "
                f"authenticated right now. Left it pending -- see /reconnect and try again."
            )
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
        _log(f"Posted {kind} to #{ticket_id}.")

        excerpt = draft_text if len(draft_text) <= 300 else draft_text[:297] + "..."
        subject_part = f' — "{subject}"' if subject else ""
        url_part = f" ({url})" if url else ""
        return (
            f"Confirmed: {kind_label} sent to #{ticket_id}{subject_part}{url_part}. "
            f"Text sent to Zendesk: “{excerpt}”"
        )


def main() -> None:
    server = ThreadingHTTPServer((BIND_HOST, _common.REVIEW_SERVER_PORT), Handler)
    _log(f"Zendesk review server listening on http://{BIND_HOST}:{_common.REVIEW_SERVER_PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
