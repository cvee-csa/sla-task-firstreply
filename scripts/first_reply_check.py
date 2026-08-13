#!/usr/bin/env python3
"""
Zendesk IT-Operations first-reply check -- runs locally on this machine via
launchd, independent of any cloud/Cowork scheduler.

What this does:
  1. Pulls recent IT-Operations tickets from Zendesk (reusing the same OAuth
     tokens the local zendesk-mcp-server already has on disk -- no new auth).
  2. Filters to tickets that genuinely have no first public reply yet.
  3. Drafts the fixed acknowledgment template for each one and adds it to the
     shared pending-drafts queue (output/pending_drafts.json).
  4. Fires a native macOS notification if there's anything new -- including
     the actual ticket subjects, not just a count -- and points it at the
     review server (see review_server.py) where you can read, edit, and post
     each draft as a public reply or an internal-only comment. The review
     page also has its own manual "Refresh from Zendesk now" button that
     runs this exact same scan on demand (see _common.scan_and_merge, shared
     by both).
  5. Also notifies (throttled to once per hour) if the check itself is
     broken -- stale OAuth, or the scan erroring out -- so a silent failure
     doesn't sit undiscovered in the log until you happen to check it.

What this deliberately does NOT do:
  - Post anything to Zendesk. This script only reads and drafts. Only
     review_server.py can post, and only when you click "Public Reply" or
     "Internal Comment" yourself -- see the zendesk-first-reply-finder
     skill's Tier 1/2/3 design. A cron job has no one to make that call, so
     it never gets to act past drafting.
  - Attempt any @mention or browser automation. That path needs a human
     watching the browser; this script has no browser at all.

Business hours are enforced INSIDE this script (not via a complex launchd
calendar schedule) so the plist can just say "run every 2 minutes" and this
script quietly no-ops outside Mon-Fri 9am-4pm local time. (The review page's
manual refresh button deliberately does NOT respect business hours -- if
you're clicking it, you already want a scan right now.)
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

BUSINESS_HOURS = range(9, 17)  # 9am - 4pm local, last check starts at 4pm
BUSINESS_WEEKDAYS = range(0, 5)  # Mon=0 ... Fri=4

LOG_FILE = _common.OUTPUT_DIR / "first_reply_check.log"

MAX_SUBJECTS_IN_NOTIFICATION = 3

# Plays alongside the new-ticket notification so it's audible even if you're
# not looking at the screen -- must be a valid macOS system sound name (see
# /System/Library/Sounds, e.g. Basso, Glass, Ping, Pop, Submarine), or "" to
# go back to a silent (visual-only) notification. Overridable via .env since
# taste in notification sounds is exactly the kind of thing worth letting
# someone change without touching code.
NEW_TICKET_SOUND = os.getenv("ZENDESK_NOTIFICATION_SOUND", "Glass").strip() or None

# Same logo shown on the review page (see review_server.py's LOGO_PATH),
# reused here as the notification's icon. terminal-notifier's -appIcon wants
# a URL rather than a bare path, hence as_uri() -- a file:// URL always
# parses the same way regardless of terminal-notifier's version. None if the
# asset is missing, so a notification never breaks just because the logo
# didn't make it into this checkout.
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "csa-logo.png"
LOGO_URI = LOGO_PATH.as_uri() if LOGO_PATH.exists() else None


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def in_business_hours() -> bool:
    now = datetime.now()
    return now.weekday() in BUSINESS_WEEKDAYS and now.hour in BUSINESS_HOURS


def notify(title: str, message: str, sound: str | None = None) -> None:
    """Native macOS notification. Uses terminal-notifier (if installed) so
    clicking the banner jumps straight to the review page; falls back to a
    plain osascript banner (no click-through) otherwise, with the review URL
    folded into the message text so it's still just a copy-paste away.
    Install terminal-notifier via `brew install terminal-notifier` to get the
    click-through behavior.

    Pass `sound` as a macOS system sound name (e.g. "Glass") to play an
    audible alert alongside the visual banner -- both terminal-notifier's
    -sound flag and osascript's `sound name` clause support this the same
    way. Leave it None (the default) for a silent notification, same as
    before this was added -- not every notification here should necessarily
    make noise (see NEW_TICKET_SOUND's usage below vs. the failure-alert
    call sites, which stay silent for now).

    The notification's icon uses the CSA logo (LOGO_URI above) via
    terminal-notifier's -appIcon flag -- but ONLY when terminal-notifier is
    the one delivering it. AppleScript's `display notification` command (the
    fallback path) has no icon parameter at all; a notification sent that way
    always shows Script Editor's/System Events' own icon, and there's no way
    to override that short of terminal-notifier. That's one more reason
    `brew install terminal-notifier` is worth doing, on top of click-through."""
    notifier = shutil.which("terminal-notifier")
    try:
        if notifier:
            cmd = [notifier, "-title", title, "-message", message, "-open", _common.REVIEW_SERVER_URL]
            if sound:
                cmd += ["-sound", sound]
            if LOGO_URI:
                cmd += ["-appIcon", LOGO_URI]
            subprocess.run(cmd, check=False, timeout=10)
        else:
            safe_message = f"{message} -- review at {_common.REVIEW_SERVER_URL}".replace('"', '\\"')
            safe_title = title.replace('"', '\\"')
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            if sound:
                safe_sound = sound.replace('"', '\\"')
                script += f' sound name "{safe_sound}"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception as exc:
        log(f"notify() failed (non-fatal): {exc}")


def format_new_ticket_lines(new_items: list) -> str:
    """Build the notification body from the actual tickets, not just a count
    -- one line per ticket (id + shortened subject), capped so a big batch
    doesn't produce an unreadable wall of text."""
    lines = []
    for item in new_items[:MAX_SUBJECTS_IN_NOTIFICATION]:
        subject = _common.shorten_subject(item["subject"])
        if len(subject) > 60:
            subject = subject[:57] + "..."
        lines.append(f"#{item['ticket_id']} -- {subject}")
    remaining = len(new_items) - len(lines)
    if remaining > 0:
        lines.append(f"+{remaining} more")
    return "\n".join(lines)


def main() -> None:
    if not in_business_hours():
        log("Outside business hours (Mon-Fri 9am-4pm local) -- skipping this run.")
        return

    run_lock = _common.acquire_run_lock()
    if run_lock is None:
        log("Previous run (or a manual refresh from the review page) still in "
            "progress -- skipping this tick rather than running concurrently.")
        return

    try:
        _run_check()
    finally:
        run_lock.close()


def _run_check() -> None:
    client = _common.make_client()

    if client.client is None:
        reason = "not authenticated (stale/missing OAuth token)"
        log(
            f"Zendesk client {reason} -- "
            f"looked for it at {_common.resolve_token_path()}. If that path looks wrong, "
            "check ZENDESK_OAUTH_TOKEN_PATH in .env. Otherwise reconnect at "
            f"{_common.REVIEW_SERVER_URL}reconnect (no Cowork session needed), "
            "then this script will pick it back up automatically next run."
        )
        _common.mark_auth_broken(reason)
        if _common.should_notify_failure():
            notify(
                "Zendesk IT-Ops check needs attention",
                f"Not authenticated -- reconnect at {_common.REVIEW_SERVER_URL}reconnect. "
                "(This won't notify again for an hour.)",
            )
            _common.mark_failure_notified()
        return

    try:
        candidates, new_items = _common.scan_and_merge(client, log_fn=log)
    except Exception as exc:
        log(f"Check failed: {exc}")
        if _common.should_notify_failure():
            notify(
                "Zendesk IT-Ops check needs attention",
                f"Scan failed: {exc} (This won't notify again for an hour.)",
            )
            _common.mark_failure_notified()
        return

    # Reaching here means the check itself ran cleanly -- clear any throttled
    # failure state so a FUTURE failure notifies right away, rather than
    # silently waiting out a throttle window left over from an old problem.
    _common.clear_failure_notified()
    _common.mark_auth_ok()

    if not candidates:
        log("No candidates found.")
        return

    pending_count = len(_common.pending_items())

    log(
        f"{len(candidates)} candidate(s), {len(new_items)} new today. "
        f"{pending_count} awaiting review at {_common.REVIEW_SERVER_URL}"
    )

    if new_items:
        notify(
            f"Zendesk IT-Ops: {len(new_items)} new ticket(s) awaiting a reply",
            format_new_ticket_lines(new_items),
            sound=NEW_TICKET_SOUND,
        )
    # A ticket already in the pending queue doesn't re-notify every run --
    # it stays visible on the review page until you post it (publicly or as
    # an internal comment), so the notification's job is just "something new
    # showed up," not a daily nag about something you haven't gotten to yet.


if __name__ == "__main__":
    main()
