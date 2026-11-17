"""The Airtable "Recurring IT Tasks" table, read-only: the Operations group, for the /ops-tasks page.

Same personal access token as the Projects page (AIRTABLE_TOKEN in .env; never logged or rendered), so the
token has to be allowed to read this second base. Nothing here writes to Airtable. Reads are cached in memory
for a couple of minutes and fall back to the last good copy when Airtable is unreachable.

Settings (all in .env):
  OPS_TASKS_BASE_ID, OPS_TASKS_TABLE_ID   required
  OPS_TASKS_VIEW_ID                       optional; records come back in that view's order
  OPS_TASKS_GROUP                         the value of the table's "Group" field to list (default "Operations")
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime

import _projects
from _projects import AirtableError

CACHE_TTL_SECONDS = 120
MAX_PAGES = 10            # 100 records a page; this table never needs more
DEFAULT_GROUP = "Operations"
STATUS_ORDER = ("Enabled", "Disabled")
BODY_LIMIT = 6000


def config() -> dict:
    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base = os.getenv("OPS_TASKS_BASE_ID", "").strip()
    table = os.getenv("OPS_TASKS_TABLE_ID", "").strip()
    view = os.getenv("OPS_TASKS_VIEW_ID", "").strip()
    group = os.getenv("OPS_TASKS_GROUP", "").strip() or DEFAULT_GROUP
    return {"token": token, "base": base, "table": table, "view": view, "group": group,
            "configured": bool(token and base and table)}


def record_url(cfg: dict, record_id: str) -> str:
    parts = [cfg["base"], cfg["table"]] + ([cfg["view"]] if cfg["view"] else []) + [record_id]
    return "https://airtable.com/" + "/".join(parts)


def table_url(cfg: dict) -> str:
    return "https://airtable.com/" + "/".join([cfg["base"], cfg["table"]] + ([cfg["view"]] if cfg["view"] else []))


def _table_path(cfg: dict) -> str:
    return f"{urllib.parse.quote(cfg['base'])}/{urllib.parse.quote(cfg['table'])}"


def group_formula(group: str) -> str:
    """Airtable formula matching the Group field exactly, with quotes and backslashes in the value escaped."""
    safe = group.replace("\\", "\\\\").replace("'", "\\'")
    return "{Group}='" + safe + "'"


_BLOCK_END_RE = re.compile(r"(?i)<\s*(?:br\s*/?|/p|/div|/li|/h[1-6]|/tr)\s*>")
_LI_RE = re.compile(r"(?i)<\s*li[^>]*>")
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(value: str) -> str:
    """A task body is HTML. Show it as plain text: block ends become line breaks, tags are dropped, entities
    decoded. The result is escaped again wherever it is rendered."""
    text = _LI_RE.sub("- ", value or "")
    text = _BLOCK_END_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()[:BODY_LIMIT]


def _when(value) -> str:
    """Airtable dates arrive as ISO 8601 (UTC for date-times). Shown in this machine's local time."""
    text = _projects._text(value)
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if len(text) <= 10:
        return stamp.strftime("%Y-%m-%d")
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def normalize(record: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    f = record.get("fields") or {}
    text = _projects._text
    status = text(f.get("Status"))
    return {
        "id": record.get("id", ""),
        "name": text(f.get("Name")) or "(untitled task)",
        "status": status,
        "status_key": " ".join(status.split()).capitalize(),
        "group": text(f.get("Group")),
        "frequency": text(f.get("Frequency")),
        "method": text(f.get("Method")),
        "assignee": text(f.get("Assignee")),
        "last_run": _when(f.get("Last Run")),
        "notes": str(f.get("Notes") or "").strip(),
        "subject": text(f.get("Subject")),
        "body": html_to_text(str(f.get("Body") or "")),
        "url": record_url(cfg, record.get("id", "")) if cfg.get("base") else "",
    }


def sort_key(item: dict) -> tuple:
    status = item["status_key"]
    rank = STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)
    return (rank, item["name"].casefold())


_cache = {"at": 0.0, "result": None}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["result"] = None


def _fetch_all(cfg: dict) -> tuple:
    items, capped, offset = [], False, ""
    for _ in range(MAX_PAGES):
        params = [("pageSize", "100"), ("filterByFormula", group_formula(cfg["group"]))]
        if cfg["view"]:
            params.append(("view", cfg["view"]))
        if offset:
            params.append(("offset", offset))
        data = _projects._request("GET", _table_path(cfg), params=params)
        items += [normalize(r, cfg) for r in data.get("records", [])]
        offset = data.get("offset") or ""
        if not offset:
            break
    else:
        capped = True
    # Belt and braces: whatever the formula did, only the wanted group is ever shown.
    items = [i for i in items if i["group"].casefold() == cfg["group"].casefold()]
    items.sort(key=sort_key)
    return items, capped


def fetch_tasks(force: bool = False) -> dict:
    """{"configured", "items", "error", "stale", "capped", "fetched_at"}. Never raises."""
    cfg = config()
    if not cfg["configured"]:
        return {"configured": False, "items": [], "error": "", "stale": False, "capped": False, "fetched_at": 0.0}
    with _cache_lock:
        cached, at = _cache["result"], _cache["at"]
    if cached is not None and not force and time.time() - at < CACHE_TTL_SECONDS:
        return cached
    try:
        items, capped = _fetch_all(cfg)
    except Exception as exc:
        if cached is not None:
            return {**cached, "error": f"{exc}. Showing the last copy that loaded.", "stale": True}
        return {"configured": True, "items": [], "error": str(exc), "stale": False, "capped": False, "fetched_at": 0.0}
    result = {"configured": True, "items": items, "error": "", "stale": False, "capped": capped, "fetched_at": time.time()}
    with _cache_lock:
        _cache["result"], _cache["at"] = result, result["fetched_at"]
    return result


def check_connection() -> dict:
    """One read of a single record: {"ok", "message"}. Never raises."""
    cfg = config()
    if not cfg["configured"]:
        return {"ok": False, "message": "Ops tasks are not set up (AIRTABLE_TOKEN, OPS_TASKS_BASE_ID and OPS_TASKS_TABLE_ID)."}
    try:
        data = _projects._request("GET", _table_path(cfg), params=[("pageSize", "1")])
    except AirtableError as exc:
        return {"ok": False, "message": f"Airtable read failed: {exc}."}
    return {"ok": True, "message": f"Airtable read worked ({len(data.get('records', []))} record fetched to test)."}
