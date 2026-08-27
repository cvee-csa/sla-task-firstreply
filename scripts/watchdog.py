"""Standalone health check for the two Zendesk launchd services
(com.csa.zendesk-review-server and com.csa.zendesk-first-reply-check).

Deliberately has ZERO dependency on _common.py or zendesk_mcp_server -- the
whole point of this script is to keep working and tell you when something
else has silently broken (like the ModuleNotFoundError crash loop this was
written right after discovering, which had both services down/stalled with
nothing telling anyone), so it can't share a failure mode with the things
it's watching. Runs via its own launchd job on plain system python3
(/usr/bin/python3), not the project's own fragile venv -- see
com.csa.zendesk-watchdog.plist.

Checks:
  1. review_server: is it actually answering HTTP requests on :8765?
  2. first_reply_check: has it completed a scan recently, during business
     hours? Reads output/last_scan.json (written by _common.mark_scan_completed
     on every successful scan) as raw JSON here rather than importing
     _common's helper, for the same independence reason as above -- if that
     import chain breaks again, this file is still just plain JSON on disk.

On a NEW problem (one that wasn't already being alerted on), or an existing
problem still present after a cooldown, fires a macOS notification via
osascript. On recovery, fires a one-time "back to normal" notification and
clears the state file so a later recurrence alerts again immediately.
"""
import json
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path("/Users/catherinevee/Desktop/git/sla-task-firstreply")
OUTPUT_DIR = REPO_ROOT / "scripts" / "output"
LAST_SCAN_FILE = OUTPUT_DIR / "last_scan.json"
STATE_FILE = OUTPUT_DIR / "watchdog_state.json"
REVIEW_SERVER_ERR_LOG = OUTPUT_DIR / "review_server.err.log"
FIRST_REPLY_ERR_LOG = OUTPUT_DIR / "launchd.err.log"

REVIEW_SERVER_URL = "http://127.0.0.1:8765/"

# Mirrors _common.py's BUSINESS_HOURS / BUSINESS_WEEKDAYS / CRON_INTERVAL_SECONDS.
# Duplicated here on purpose rather than imported -- see module docstring.
# If those ever change in _common.py, update here too.
BUSINESS_HOURS = range(9, 17)
BUSINESS_WEEKDAYS = range(0, 5)
CRON_INTERVAL_SECONDS = 120

# How long first_reply_check can go without completing a scan during
# business hours before we consider it stalled -- 4 missed cycles (~8 min),
# enough margin for one slow/skipped tick (a Zendesk hiccup) without a false
# alarm, tight enough to catch a real crash loop well within the same
# working morning rather than days later.
STALE_SCAN_THRESHOLD_SECONDS = CRON_INTERVAL_SECONDS * 4

# Re-alert for a still-broken problem at most this often, so something you
# haven't fixed yet doesn't re-notify every 5 minutes forever.
REALERT_COOLDOWN_SECONDS = 30 * 60


def in_business_hours(now: datetime) -> bool:
    return now.weekday() in BUSINESS_WEEKDAYS and now.hour in BUSINESS_HOURS


def _last_error_line(log_path: Path) -> str:
    """Best-effort: the last line in the tail of a log file that looks like
    an actual exception (not one of the surrounding 'File "...", line N, in
    <module>' traceback frames) -- included in notifications so you can see
    what broke without going and finding the log file yourself."""
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines[-200:]):
        stripped = line.strip()
        if stripped and ("Error" in stripped or "Exception" in stripped) and 'File "' not in stripped:
            return stripped
    return ""


def check_review_server() -> str:
    """Empty string if healthy, else a short human-readable problem
    description."""
    try:
        with urllib.request.urlopen(REVIEW_SERVER_URL, timeout=5) as resp:
            if resp.status != 200:
                return f"review server responded with HTTP {resp.status}"
        return ""
    except Exception as exc:
        detail = _last_error_line(REVIEW_SERVER_ERR_LOG)
        suffix = f" -- last error in its log: {detail}" if detail else ""
        return f"review server (http://127.0.0.1:8765/) isn't responding ({exc}){suffix}"


def check_first_reply_check(now: datetime) -> str:
    if not in_business_hours(now):
        return ""  # legitimately idle outside business hours -- nothing to check
    if not LAST_SCAN_FILE.exists():
        return "first-reply-check has never completed a scan yet"
    try:
        completed_at = datetime.fromisoformat(json.loads(LAST_SCAN_FILE.read_text())["completed_at"])
    except Exception:
        return "first-reply-check's last_scan.json is unreadable/corrupt"
    age_seconds = (now - completed_at).total_seconds()
    if age_seconds <= STALE_SCAN_THRESHOLD_SECONDS:
        return ""
    detail = _last_error_line(FIRST_REPLY_ERR_LOG)
    suffix = f" -- last error in its log: {detail}" if detail else ""
    return f"first-reply-check hasn't completed a scan in {int(age_seconds // 60)} min during business hours{suffix}"


def notify(title: str, message: str, sound: str = "Basso") -> None:
    script = (
        f"display notification {json.dumps(message)} "
        f"with title {json.dumps(title)} sound name {json.dumps(sound)}"
    )
    subprocess.run(["osascript", "-e", script], check=False)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main() -> None:
    now = datetime.now()
    problems = {
        "review_server": check_review_server(),
        "first_reply_check": check_first_reply_check(now),
    }

    state = load_state()
    changed = False

    for key, problem in problems.items():
        prev = state.get(key, {})
        was_active = prev.get("active", False)

        if problem:
            last_alert_at = prev.get("last_alert_at")
            cooldown_elapsed = (
                last_alert_at is None
                or (now - datetime.fromisoformat(last_alert_at)).total_seconds() > REALERT_COOLDOWN_SECONDS
            )
            if not was_active or cooldown_elapsed:
                notify("Zendesk automation problem", problem)
                state[key] = {"active": True, "last_alert_at": now.isoformat(timespec="seconds"), "problem": problem}
                changed = True
            else:
                state[key] = prev  # still broken, still within cooldown -- no new alert
        else:
            if was_active:
                notify("Zendesk automation recovered", f"{key.replace('_', ' ')} is healthy again.", sound="Glass")
                changed = True
            state[key] = {"active": False, "last_alert_at": None, "problem": ""}

    if changed or not STATE_FILE.exists():
        save_state(state)


if __name__ == "__main__":
    main()
