"""Local health checks for the /diagnostics page.

Every check here reads only local state (environment variable *names*, files, the Airtable cache,
launchctl) and never calls Zendesk or Airtable by itself, so the page loads instantly even when a
service is down. A secret's value is never read into a result: only whether it is set.
Each check is {"name", "status": "ok" | "warn" | "fail" | "info", "detail"}."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import datetime

import _common
import _ops_tasks
import _projects

LAUNCH_LABELS = (
    "com.csa.zendesk-review-server",
    "com.csa.zendesk-first-reply-check",
    "com.csa.zendesk-watchdog",
)


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _set(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def check_env_file() -> dict:
    path = _common.REPO_ROOT / ".env"
    if not path.exists():
        return _check(".env file", "fail", "Not found in the repo root. Copy .env.example to .env and fill it in.")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return _check(".env file", "warn", f"Present, but its permissions could not be read ({exc}).")
    if mode & 0o077:
        return _check(".env file", "warn", f"Present, but readable by other accounts (mode {mode:o}). Run: chmod 600 .env")
    return _check(".env file", "ok", "Present and readable only by this account.")


def check_zendesk() -> list:
    out = []
    missing = [n for n in ("ZENDESK_SUBDOMAIN", "ZENDESK_CLIENT_ID", "ZENDESK_CLIENT_SECRET") if not _set(n)]
    out.append(_check("Zendesk settings", "fail" if missing else "ok",
                      f"Not set: {', '.join(missing)}." if missing else "Subdomain, client ID and client secret are set."))
    token_path = _common.Path(_common.resolve_token_path())
    out.append(_check("Zendesk OAuth tokens", "ok" if token_path.exists() else "warn",
                      "Token file present." if token_path.exists() else "No token file yet. Use Zendesk OAuth (Reconnect) to create one."))
    state = _common.auth_status()
    if state.get("ok", True):
        when = f" Last check: {state['checked_at']}." if state.get("checked_at") else " No check has been recorded yet."
        out.append(_check("Zendesk connection (last check)", "ok", "Last recorded check passed." + when))
    else:
        out.append(_check("Zendesk connection (last check)", "fail",
                          f"Last check failed: {state.get('reason') or 'no reason recorded'} ({state.get('checked_at', 'unknown time')}). Use Reconnect."))
    return out


def check_airtable() -> list:
    cfg = _projects.config()
    out = []
    missing = [n for n, key in (("AIRTABLE_TOKEN", "token"), ("AIRTABLE_BASE_ID", "base"), ("AIRTABLE_TABLE_ID", "table")) if not cfg[key]]
    if missing:
        return [_check("Airtable settings", "fail", f"Not set: {', '.join(missing)}. See the README Airtable section.")]
    out.append(_check("Airtable settings", "ok", "Token, base ID and table ID are set." + ("" if cfg["view"] else " No view ID (optional).")))
    people = _projects.assignees()
    out.append(_check("Assignee list", "ok" if people else "warn",
                      f"{len(people)} people in PROJECTS_ASSIGNEES." if people else
                      "PROJECTS_ASSIGNEES is empty, so Assignee cannot be set from the Projects page."))
    cached = _projects._cache["result"]
    if cached is None:
        out.append(_check("Projects data", "info", "Not loaded yet in this server process. Open Projects, or use Test Airtable connection."))
    elif cached.get("error"):
        out.append(_check("Projects data", "warn", f"Last load problem: {cached['error']}"))
    else:
        when = datetime.fromtimestamp(cached["fetched_at"]).strftime("%H:%M") if cached.get("fetched_at") else "unknown time"
        out.append(_check("Projects data", "ok", f"{len(cached.get('items', []))} projects loaded at {when}."))
    snaps = _projects.list_snapshots()
    if snaps:
        newest = datetime.fromtimestamp(snaps[0][1]).strftime("%Y-%m-%d %H:%M")
        out.append(_check("Local snapshots", "ok", f"{len(snaps)} kept (newest {newest}). A snapshot is taken before the first save in each 6-hour stretch."))
    else:
        out.append(_check("Local snapshots", "info", "None yet. One is taken before the first save from the Projects page."))
    log = _projects._log_entries()
    out.append(_check("Edit log", "info", f"{len(log)} {'entry' if len(log) == 1 else 'entries'} in projects_edits.jsonl."))
    return out


def check_ops_tasks() -> list:
    cfg = _ops_tasks.config()
    missing = [n for n, key in (("AIRTABLE_TOKEN", "token"), ("OPS_TASKS_BASE_ID", "base"), ("OPS_TASKS_TABLE_ID", "table")) if not cfg[key]]
    if missing:
        return [_check("Ops tasks settings", "info", f"Not set: {', '.join(missing)}. The Ops Tasks page shows setup instructions until they are.")]
    out = [_check("Ops tasks settings", "ok", f"Token, base ID and table ID are set; listing the {cfg['group']} group.")]
    cached = _ops_tasks._cache["result"]
    if cached is None:
        out.append(_check("Ops tasks data", "info", "Not loaded yet in this server process. Open Ops Tasks."))
    elif cached.get("error"):
        out.append(_check("Ops tasks data", "warn", f"Last load problem: {cached['error']}"))
    else:
        when = datetime.fromtimestamp(cached["fetched_at"]).strftime("%H:%M") if cached.get("fetched_at") else "unknown time"
        out.append(_check("Ops tasks data", "ok", f"{len(cached.get('items', []))} tasks loaded at {when}."))
    return out


def check_sla_history() -> list:
    snap = _common.sla_history_snapshot()
    if snap.get("error"):
        return [_check("SLA history", "fail", snap["error"])]
    age = _common.sla_history_age_days(snap.get("as_of"))
    detail = f"{len(snap['entries'])} daily entries, newest {snap.get('as_of') or 'unknown'} (source: {snap.get('source')})."
    if age is not None and age > _common.SLA_STALE_AFTER_DAYS:
        return [_check("SLA history", "warn", f"{detail} {age} days old; use Update history on the SLA page.")]
    return [_check("SLA history", "ok", detail)]


def check_sla_checkout() -> dict:
    repo = _common.SLA_HISTORY_FILE.parent.parent
    if not (repo / ".git").exists():
        return _check("sla-report-slack checkout", "warn", f"No git checkout at {repo}. Update history needs one next to this repo.")
    return _check("sla-report-slack checkout", "ok", f"Found at {repo}.")


def check_launch_agents() -> list:
    exe = shutil.which("launchctl")
    if not exe:
        return [_check("Background services", "info", "launchctl is not available on this system, so LaunchAgents were not checked.")]
    try:
        proc = subprocess.run([exe, "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return [_check("Background services", "warn", f"launchctl could not be run ({exc}).")]
    rows = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows[parts[2].strip()] = (parts[0].strip(), parts[1].strip())
    out = []
    for label in LAUNCH_LABELS:
        if label not in rows:
            out.append(_check(label, "warn", "Not loaded. Run scripts/reload_launchd.sh (see the README)."))
            continue
        pid, code = rows[label]
        if pid.isdigit():
            out.append(_check(label, "ok", f"Running (pid {pid})."))
        elif code in ("0", "-"):
            out.append(_check(label, "ok", "Loaded; runs on its schedule (not running at this moment)."))
        else:
            out.append(_check(label, "warn", f"Loaded, last exit status {code}."))
    return out


def check_output_folder() -> dict:
    folder = _common.OUTPUT_DIR
    if folder.is_dir() and os.access(folder, os.W_OK):
        return _check("Output folder", "ok", f"{folder} exists and is writable.")
    return _check("Output folder", "fail", f"{folder} is missing or not writable.")


def run_checks() -> list:
    """[(section title, [checks])]. Instant: no network."""
    sections = [
        ("This machine", [check_env_file(), check_output_folder()]),
        ("Zendesk", check_zendesk()),
        ("Airtable", check_airtable()),
        ("Ops tasks", check_ops_tasks()),
        ("SLA history", check_sla_history() + [check_sla_checkout()]),
        ("Background services", check_launch_agents()),
    ]
    return sections
