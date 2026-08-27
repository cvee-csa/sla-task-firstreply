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

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

# Business hours (and in_business_hours()) live in _common.py now, shared
# with review_server.py's "next automatic check" estimate on the review page
# -- two copies of "what counts as business hours" is exactly the kind of
# drift that caused the launchd OAuth-path bug earlier in this project.

LOG_FILE = _common.OUTPUT_DIR / "first_reply_check.log"

MAX_SUBJECTS_IN_NOTIFICATION = 3

# Plays alongside the new-ticket notification so it's audible even if you're
# not looking at the screen -- must be a valid macOS system sound name (see
# /System/Library/Sounds, e.g. Basso, Glass, Ping, Pop, Submarine), or "" to
# go back to a silent (visual-only) notification. Overridable via .env since
# taste in notification sounds is exactly the kind of thing worth letting
# someone change without touching code.
NEW_TICKET_SOUND = os.getenv("ZENDESK_NOTIFICATION_SOUND", "Glass").strip() or None

# A custom notification icon (the CSA logo, via terminal-notifier's -appIcon)
# was tried and dropped: modern macOS (Catalina+) largely ignores -appIcon
# for security reasons -- it stops CLI tools from spoofing which app a
# notification claims to be from -- so it silently showed terminal-notifier's
# own icon regardless. Instead, every notification's title is prefixed "CSA"
# below so it's still identifiable at a glance in Notification Center.
NOTIFICATION_PREFIX = "CSA"

# Optional third notification path: a small Automator "Application" (see
# README's "Notification helper app" section for the one-time setup) that
# reads its text from NOTIFY_PAYLOAD_FILE below and calls AppleScript's
# `display notification` from inside its own real, Launch-Services-registered
# .app process. This exists because terminal-notifier and plain osascript
# (both above/below) turned out to be silently dropped by macOS specifically
# when triggered by launchd with no interactive parent -- both report a
# clean, successful exit, but no banner ever appears, and neither shows up as
# an entry in System Settings > Notifications for that headless invocation
# at all. A genuine .app bundle gets its own row in that list regardless of
# what launched it, which a raw CLI binary apparently doesn't on this Mac.
# Entirely optional: if the app hasn't been set up at this path, this step is
# skipped without logging, since it's an opt-in extra layer, not a
# requirement for the other two paths above.
NOTIFIER_APP_PATH = os.getenv(
    "ZENDESK_NOTIFIER_APP_PATH", str(Path.home() / "Applications" / "CSA Notifier.app")
)
NOTIFY_PAYLOAD_FILE = _common.OUTPUT_DIR / "notify_payload.json"


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(title: str, message: str, sound: str | None = None) -> None:
    """Native macOS notification -- fires through BOTH terminal-notifier (if
    installed) AND osascript's `display notification`, rather than picking
    whichever one shutil.which() happens to find first.

    Why both: this project hit a real case where a terminal-notifier
    command run interactively in Terminal produced a visible banner, but
    the exact same binary invoked headlessly by launchd (no Terminal
    window, no interactive session) did not -- with no error surfaced
    either way, since macOS's per-process notification permissioning can
    silently drop a send it doesn't recognize as approved instead of
    failing loudly. Rather than keep guessing which single delivery path
    this Mac's launchd context will actually let through, both fire every
    time. Worst case if both work: one harmless duplicate banner. Worst
    case if only one path is actually permitted here: total silence, which
    is exactly the failure mode this is meant to rule out.

    Click-through (the banner jumping straight to the review page) is only
    available via terminal-notifier's -open flag; the osascript banner has
    no equivalent, so its message folds the review URL into the text
    instead.

    Pass `sound` as a macOS system sound name (e.g. "Glass") to play an
    audible alert alongside the visual banner -- both mechanisms support
    this. Leave it None for a silent notification (see NEW_TICKET_SOUND's
    usage below vs. the two failure-alert call sites, which stay silent).

    `title` is expected to already carry the NOTIFICATION_PREFIX (see call
    sites below) -- notify() itself stays agnostic about branding.

    Every attempt's exit code and stderr get logged, not just Python-level
    exceptions -- the previous version only caught exceptions around
    subprocess.run, so a real but non-zero exit (e.g. notification
    permission denied) from either tool was invisible in the log. If
    notifications are still silent after this change, the log is the next
    place to look.

    A third path also fires if the optional helper .app (NOTIFIER_APP_PATH)
    has been set up -- see the README's "Notification helper app" section.
    That one exists precisely because, on this Mac, BOTH mechanisms above
    turned out to exit cleanly (returncode 0, nothing in stderr) while
    triggered via launchd, yet displayed no banner at all -- confirmed by
    directly comparing an interactive-Terminal run (worked) against a
    `launchctl submit` run of the identical command (silent)."""
    notifier = shutil.which("terminal-notifier")

    if notifier:
        try:
            cmd = [notifier, "-title", title, "-message", message, "-open", _common.REVIEW_SERVER_URL]
            if sound:
                cmd += ["-sound", sound]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                log(f"terminal-notifier exited {result.returncode}: {result.stderr.strip()}")
        except Exception as exc:
            log(f"terminal-notifier notify() failed (non-fatal): {exc}")

    try:
        safe_message = f"{message} -- review at {_common.REVIEW_SERVER_URL}".replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        script = f'display notification "{safe_message}" with title "{safe_title}"'
        if sound:
            safe_sound = sound.replace('"', '\\"')
            script += f' sound name "{safe_sound}"'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            log(f"osascript exited {result.returncode}: {result.stderr.strip()}")
    except Exception as exc:
        log(f"osascript notify() failed (non-fatal): {exc}")

    notifier_app = Path(NOTIFIER_APP_PATH)
    if notifier_app.exists():
        try:
            NOTIFY_PAYLOAD_FILE.write_text(
                json.dumps({"title": title, "message": message, "sound": sound or ""})
            )
            result = subprocess.run(
                ["open", "-a", str(notifier_app)], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                log(f"CSA Notifier.app exited {result.returncode}: {result.stderr.strip()}")
        except Exception as exc:
            log(f"CSA Notifier.app notify() failed (non-fatal): {exc}")


def _format_ticket_lines(items: list) -> str:
    """Shared formatter for both "new ticket" and "already handled elsewhere"
    notification bodies -- one line per ticket (id + shortened subject),
    capped so a big batch doesn't produce an unreadable wall of text."""
    lines = []
    for item in items[:MAX_SUBJECTS_IN_NOTIFICATION]:
        subject = _common.shorten_subject(item["subject"])
        if len(subject) > 60:
            subject = subject[:57] + "..."
        lines.append(f"#{item['ticket_id']} -- {subject}")
    remaining = len(items) - len(lines)
    if remaining > 0:
        lines.append(f"+{remaining} more")
    return "\n".join(lines)


def main() -> None:
    if not _common.in_business_hours():
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
                f"{NOTIFICATION_PREFIX} Zendesk IT-Ops check needs attention",
                f"Not authenticated -- reconnect at {_common.REVIEW_SERVER_URL}reconnect. "
                "(This won't notify again for an hour.)",
            )
            _common.mark_failure_notified()
        return

    try:
        candidates, new_items, handled_elsewhere = _common.scan_and_merge(client, log_fn=log)
    except Exception as exc:
        log(f"Check failed: {exc}")
        if _common.should_notify_failure():
            notify(
                f"{NOTIFICATION_PREFIX} Zendesk IT-Ops check needs attention",
                f"Scan failed: {exc} (This won't notify again for an hour.)",
            )
            _common.mark_failure_notified()
        return

    # Reaching here means the check itself ran cleanly -- clear any throttled
    # failure state so a FUTURE failure notifies right away, rather than
    # silently waiting out a throttle window left over from an old problem.
    _common.clear_failure_notified()
    _common.mark_auth_ok()

    # Checked (and notified) BEFORE the `if not candidates: return` below --
    # find_candidates() deliberately excludes tickets someone else already
    # replied to, so it's entirely possible for `candidates` to be empty on
    # a run that still found pending tickets to pull out of the queue. Early
    # -returning before this would silently skip telling you about exactly
    # the race this feature exists to catch.
    if handled_elsewhere:
        handled_ids = ", ".join(f"#{i['ticket_id']}" for i in handled_elsewhere)
        log(
            f"{len(handled_elsewhere)} pending ticket(s) already got a reply from someone else -- "
            f"pulled from the queue: {handled_ids}"
        )
        notify(
            f"{NOTIFICATION_PREFIX} Zendesk IT-Ops: {len(handled_elsewhere)} ticket(s) already handled elsewhere",
            _format_ticket_lines(handled_elsewhere),
        )

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
            f"{NOTIFICATION_PREFIX} Zendesk IT-Ops: {len(new_items)} new ticket(s) awaiting a reply",
            _format_ticket_lines(new_items),
            sound=NEW_TICKET_SOUND,
        )
    # A ticket already in the pending queue doesn't re-notify every run --
    # it stays visible on the review page until you post it (publicly or as
    # an internal comment), so the notification's job is just "something new
    # showed up," not a daily nag about something you haven't gotten to yet.


if __name__ == "__main__":
    main()
