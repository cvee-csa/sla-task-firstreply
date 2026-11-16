"""The Airtable "Projects" table: a read for the /projects page, and a small
allowlisted write-back (name, description, Status, assignee, requester, urgency,
blockers, notes, dates).

Everything here talks to Airtable's REST API with a personal access token that
lives in .env (AIRTABLE_TOKEN; never logged or rendered). Reads are cached in
memory for a couple of minutes and fall back to the last good copy when
Airtable is unreachable. Writes are one PATCH per record, only for fields in
FIELD_SLUGS: the form names a *slug* ("status", "assignee", ...), never an
Airtable field name, so a forged form cannot reach any other field. Each write
is checked against a fresh copy of the record first (an edit made in Airtable
since the page loaded is a conflict, not an overwrite), read back afterwards,
and logged locally (scripts/output/projects_edits.jsonl) so it can be undone.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import _common

API_ROOT = "https://api.airtable.com/v0"
CACHE_TTL_SECONDS = 120
MAX_PAGES = 10            # 100 records a page; a base this size never needs more
REQUEST_TIMEOUT_SECONDS = 20
TEXT_LIMIT = 10000
LINE_LIMIT = 255          # one-line text fields (project name, requester)

STATUS_ORDER = ("INTAKE", "SCOPING", "ACTIVE", "BLOCKED", "COMPLETE", "CANCELLED")

# The options of the single-select "Urgency Rating" field, exactly as Airtable spells them (writes
# run with typecast off, so any other text is refused rather than creating a new option). Empty clears it.
URGENCY_OPTIONS = ("5 - Critical / SLA Breach", "4 - Overdue", "3 - Hard Deadline", "2 - Soft Deadline", "1 - No Deadline")

# The Airtable fields the page reads (names as they appear in the base).
READ_FIELDS = (
    "Project name", "Description", "Status", "Assigned to", "Start date", "End date",
    "Actual Completion Date", "Blockers", "Sprint Notes", "Source Zendesk Ticket ID",
    "Source Zendesk Ticket URL", "Workstream", "Urgency Rating", "Ease Score", "Resource Importance",
    "Requester",
)

# slug -> (Airtable field name, kind, label shown to people). The ONLY fields that can be written.
# Kinds: "line" = one-line text, "text" = long text, "select" = Status, "choice" = a single select
# that may be cleared (urgency), "collaborator", "date".
FIELD_SLUGS = {
    "name": ("Project name", "line", "Project name"),
    "status": ("Status", "select", "Status"),
    "assignee": ("Assigned to", "collaborator", "Assignee"),
    "requester": ("Requester", "line", "Requester"),
    "urgency": ("Urgency Rating", "choice", "Urgency"),
    "start": ("Start date", "date", "Start date"),
    "end": ("End date", "date", "End date"),
    "completed": ("Actual Completion Date", "date", "Completed on"),
    "blockers": ("Blockers", "text", "Blockers"),
    "notes": ("Sprint Notes", "text", "Sprint notes"),
    "description": ("Description", "text", "Description"),
}

_RECORD_ID_RE = re.compile(r"^rec[A-Za-z0-9]{14}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AirtableError(Exception):
    """A failed Airtable call; the message is safe to show (it never holds the token)."""


# --- configuration ---------------------------------------------------------------

def config() -> dict:
    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table = os.getenv("AIRTABLE_TABLE_ID", "").strip()
    view = os.getenv("AIRTABLE_VIEW_ID", "").strip()
    return {"token": token, "base": base, "table": table, "view": view,
            "configured": bool(token and base and table)}


def assignees() -> dict:
    """{display name: email} from PROJECTS_ASSIGNEES="Name:email,Name:email". Airtable's
    collaborator field is written by email, and the people who can be picked are the ones listed here."""
    out = {}
    for part in os.getenv("PROJECTS_ASSIGNEES", "").split(","):
        name, _, email = part.partition(":")
        name, email = name.strip(), email.strip()
        if name and "@" in email:
            out[name] = email
    return out


def record_url(cfg: dict, record_id: str) -> str:
    parts = [cfg["base"], cfg["table"]] + ([cfg["view"]] if cfg["view"] else []) + [record_id]
    return "https://airtable.com/" + "/".join(parts)


# --- HTTP ------------------------------------------------------------------------

def _request(method: str, path: str, params=None, body=None) -> dict:
    """One Airtable REST call. params is a list of (key, value) pairs. Raises AirtableError."""
    cfg = config()
    url = f"{API_ROOT}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        kind = ""
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error")
            kind = err.get("type", "") if isinstance(err, dict) else str(err or "")
        except Exception:
            pass
        raise AirtableError(f"Airtable returned {exc.code}" + (f" ({kind})" if kind else "")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AirtableError(f"Airtable could not be reached ({reason})") from None
    except ValueError:
        raise AirtableError("Airtable sent a response that was not JSON") from None


def _table_path(cfg: dict) -> str:
    return f"{urllib.parse.quote(cfg['base'])}/{urllib.parse.quote(cfg['table'])}"


# --- reading ---------------------------------------------------------------------

def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("email") or "")
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    return str(value).strip()


def _person(value) -> tuple:
    """(name, email) of a collaborator cell (a dict, or a list when the field allows several)."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return (str(value.get("name") or value.get("email") or "").strip(), str(value.get("email") or "").strip())
    return ("", "")


def _status_key(status: str) -> str:
    """The status used to group projects: a known status spelled with other case or stray spaces
    ("Active ", "active") groups with ACTIVE. `status` itself stays as Airtable has it, because that
    exact text is what an edit compares against and what Undo writes back."""
    key = " ".join(status.split()).upper()
    return key if key in STATUS_ORDER else " ".join(status.split())


def normalize(record: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    f = record.get("fields") or {}
    name, email = _person(f.get("Assigned to"))
    ticket_ids = _text(f.get("Source Zendesk Ticket ID"))
    return {
        "id": record.get("id", ""),
        "name": _text(f.get("Project name")) or "(untitled project)",
        "raw_name": _text(f.get("Project name")),      # what an edit compares against, without the placeholder
        "requester": _text(f.get("Requester")),
        "description": str(f.get("Description") or "").strip(),
        "status": _text(f.get("Status")),
        "status_key": _status_key(_text(f.get("Status"))),
        "assignee": name,
        "assignee_email": email,
        "start": _text(f.get("Start date")),
        "end": _text(f.get("End date")),
        "completed": _text(f.get("Actual Completion Date")),
        "blockers": str(f.get("Blockers") or "").strip(),
        "notes": str(f.get("Sprint Notes") or "").strip(),
        "ticket_ids": re.findall(r"\d{4,8}", ticket_ids),
        "ticket_url": _text(f.get("Source Zendesk Ticket URL")),
        "workstream": _text(f.get("Workstream")),
        "urgency": _text(f.get("Urgency Rating")),
        "ease": _text(f.get("Ease Score")),
        "importance": _text(f.get("Resource Importance")),
        "url": record_url(cfg, record.get("id", "")) if cfg.get("base") else "",
    }


_cache = {"at": 0.0, "result": None}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["result"] = None


def _fetch_all(cfg: dict) -> tuple:
    items, capped, offset = [], False, ""
    for page in range(MAX_PAGES):
        params = [("pageSize", "100")] + [("fields[]", f) for f in READ_FIELDS]
        if cfg["view"]:
            params.append(("view", cfg["view"]))
        if offset:
            params.append(("offset", offset))
        data = _request("GET", _table_path(cfg), params=params)
        items += [normalize(r, cfg) for r in data.get("records", [])]
        offset = data.get("offset") or ""
        if not offset:
            break
    else:
        capped = True
    return items, capped


def fetch_projects(force: bool = False) -> dict:
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
        # Network failures are not cached; a real answer is. Keep showing the last good copy.
        if cached is not None:
            return {**cached, "error": f"{exc}. Showing the last copy that loaded.", "stale": True}
        return {"configured": True, "items": [], "error": str(exc), "stale": False, "capped": False, "fetched_at": 0.0}
    result = {"configured": True, "items": items, "error": "", "stale": False, "capped": capped, "fetched_at": time.time()}
    with _cache_lock:
        _cache["result"], _cache["at"] = result, result["fetched_at"]
    return result


def _cache_replace(item: dict) -> None:
    """After a successful write, show the record as saved without waiting for the next refresh."""
    with _cache_lock:
        cached = _cache["result"]
        if cached is None:
            return
        _cache["result"] = {**cached, "items": [item if i["id"] == item["id"] else i for i in cached["items"]]}


def fetch_record(record_id: str) -> dict:
    cfg = config()
    data = _request("GET", f"{_table_path(cfg)}/{urllib.parse.quote(record_id)}")
    return normalize(data, cfg)


# --- editing ---------------------------------------------------------------------

def current_values(item: dict) -> dict:
    """slug -> the text the edit form shows and compares."""
    return {"name": item["raw_name"], "status": item["status"], "assignee": item["assignee"],
            "requester": item["requester"], "urgency": item["urgency"], "start": item["start"],
            "end": item["end"], "completed": item["completed"], "blockers": item["blockers"],
            "notes": item["notes"], "description": item["description"]}


def _clean_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _valid_date(text: str) -> bool:
    if not _DATE_RE.match(text):
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _api_value(slug: str, text: str, email: str = ""):
    """What PATCH sends for a slug's text ("" clears the field)."""
    if text == "":
        return None
    if slug == "assignee":
        return {"email": email}
    return text


def _same(slug: str, a: str, b: str) -> bool:
    return a.casefold() == b.casefold() if slug == "assignee" else a == b


def plan_edit(form: dict, item: dict, today: str | None = None) -> dict:
    """Works out what a submitted form would change on `item` (a freshly read record).

    form: {field: [values]} as parsed from the POST body. For each slug the form carries the new
    value ("<slug>") and the value the page showed ("orig_<slug>"); only a field whose new value
    differs from the one shown counts as an edit. A shown value that no longer matches the record
    means someone changed it in Airtable meanwhile: a conflict, never an overwrite."""
    people = assignees()
    now = current_values(item)
    changes, errors, conflicts = [], [], []
    payload, restore = {}, {}

    def first(key):
        vals = form.get(key)
        return vals[0] if vals else None

    for slug, (field, kind, label) in FIELD_SLUGS.items():
        raw = first(slug)
        if raw is None:
            continue
        new = _clean_text(raw)
        shown = _clean_text(first(f"orig_{slug}") or "")
        if _same(slug, new, shown):
            continue
        if kind == "select":
            if new not in STATUS_ORDER:
                errors.append(f"{label}: \"{new}\" is not one of {', '.join(STATUS_ORDER)}.")
                continue
        elif kind == "choice":
            if new and new not in URGENCY_OPTIONS:
                errors.append(f"{label}: \"{new}\" is not one of the Urgency Rating options.")
                continue
        elif kind == "line":
            if "\n" in new:
                errors.append(f"{label}: must be a single line.")
                continue
            if slug == "name" and not new:
                errors.append(f"{label}: cannot be empty.")
                continue
            if len(new) > LINE_LIMIT:
                errors.append(f"{label}: longer than {LINE_LIMIT} characters.")
                continue
        elif kind == "collaborator":
            if new and new not in people:
                errors.append(f"{label}: \"{new}\" is not in the list of people who can be assigned.")
                continue
        elif kind == "date":
            if new and not _valid_date(new):
                errors.append(f"{label}: \"{new}\" is not a date (use YYYY-MM-DD).")
                continue
        else:
            if len(new) > TEXT_LIMIT:
                errors.append(f"{label}: longer than {TEXT_LIMIT} characters.")
                continue
        if not _same(slug, now[slug], shown):
            conflicts.append({"slug": slug, "label": label, "yours": new, "shown": shown, "now": now[slug]})
            continue
        email = people.get(new, "")
        changes.append({"slug": slug, "field": field, "label": label, "before": now[slug], "after": new,
                        "api_after": _api_value(slug, new, email),
                        "api_before": _api_value(slug, now[slug], item["assignee_email"])})

    # Marking a project COMPLETE can fill in today's completion date (unless a date was typed or already set).
    changed = {c["slug"] for c in changes}
    if ("status" in changed and next(c for c in changes if c["slug"] == "status")["after"] == "COMPLETE"
            and first("complete_today") == "yes" and "completed" not in changed and not now["completed"]
            and "completed" in form):
        day = today or date.today().isoformat()
        changes.append({"slug": "completed", "field": FIELD_SLUGS["completed"][0], "label": FIELD_SLUGS["completed"][2],
                        "before": "", "after": day, "api_after": day, "api_before": None})

    for c in changes:
        payload[c["field"]] = c["api_after"]
        restore[c["field"]] = c["api_before"]
    return {"changes": changes, "errors": errors, "conflicts": conflicts, "payload": payload, "restore": restore}


_in_progress = set()
_in_progress_lock = threading.Lock()


def begin_action(record_id: str) -> bool:
    with _in_progress_lock:
        if record_id in _in_progress:
            return False
        _in_progress.add(record_id)
        return True


def end_action(record_id: str) -> None:
    with _in_progress_lock:
        _in_progress.discard(record_id)


# --- the local edit log (what makes Undo possible) -----------------------------------

_log_lock = threading.Lock()


def _log_path():
    return _common.OUTPUT_DIR / "projects_edits.jsonl"


def _log_append(entry: dict) -> None:
    with _log_lock, open(_log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _log_entries() -> list:
    try:
        lines = _log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _slug_of(field: str) -> str:
    return next(s for s, (f, _k, _l) in FIELD_SLUGS.items() if f == field)


def commit_edit(record_id: str, plan: dict) -> dict:
    """PATCHes the planned changes, reads them back, logs them. Returns
    {"ok": bool, "message": str, "undo_id": str}. The caller has already shown the plan and has the
    per-record guard (begin_action)."""
    if not plan["changes"]:
        return {"ok": False, "message": "Nothing changed, so nothing was sent to Airtable.", "undo_id": ""}
    cfg = config()
    try:
        data = _request("PATCH", f"{_table_path(cfg)}/{urllib.parse.quote(record_id)}",
                        body={"fields": plan["payload"], "typecast": False})
    except AirtableError as exc:
        return {"ok": False, "message": f"Nothing was saved: {exc}.", "undo_id": ""}
    saved = normalize(data, cfg) if data.get("fields") is not None else None
    problems = []
    if saved is not None:
        _cache_replace(saved)
        now = current_values(saved)
        problems = [c["label"] for c in plan["changes"] if not _same(c["slug"], now[c["slug"]], c["after"])]
    else:
        problems = ["the response held no record to compare"]
    undo_id = secrets.token_hex(6)
    _log_append({
        "id": undo_id, "kind": "edit", "at": datetime.now().isoformat(timespec="seconds"), "record": record_id,
        "name": saved["name"] if saved else "",
        "changes": [{"slug": c["slug"], "field": c["field"], "label": c["label"], "before": c["before"],
                     "after": c["after"], "api_before": c["api_before"]} for c in plan["changes"]],
    })
    summary = "; ".join(f"{c['label']}: {c['before'] or '(empty)'} → {c['after'] or '(empty)'}" for c in plan["changes"])
    title = f" \"{saved['name']}\"" if saved else ""
    if problems:
        return {"ok": True, "undo_id": undo_id,
                "message": f"Sent to Airtable{title}: {summary}. Airtable's reply did not match for {', '.join(problems)}; check the record."}
    return {"ok": True, "undo_id": undo_id, "message": f"Saved to Airtable{title} (confirmed): {summary}."}


def undo_edit(undo_id: str) -> str:
    """Restores the values an earlier edit replaced, but only while the record still holds what that edit wrote."""
    entries = _log_entries()
    edit = next((e for e in entries if e.get("id") == undo_id and e.get("kind") == "edit"), None)
    if edit is None:
        return "That edit is not in the local log, so it cannot be undone from here."
    if any(e.get("kind") == "undo" and e.get("undoes") == undo_id for e in entries):
        return "That edit was already undone."
    record_id = edit["record"]
    if not _RECORD_ID_RE.match(record_id):
        return "That log entry has no valid record id."
    if not begin_action(record_id):
        return "Another change to this project is in progress. Try again in a moment."
    try:
        try:
            item = fetch_record(record_id)
        except AirtableError as exc:
            return f"Could not read the project from Airtable: {exc}. Nothing was undone."
        now = current_values(item)
        moved = [c["label"] for c in edit["changes"] if not _same(c["slug"], now.get(c["slug"], ""), c["after"])]
        if moved:
            return f"Not undone: {', '.join(moved)} changed again since that edit."
        cfg = config()
        fields = {c["field"]: c["api_before"] for c in edit["changes"]}
        try:
            data = _request("PATCH", f"{_table_path(cfg)}/{urllib.parse.quote(record_id)}",
                            body={"fields": fields, "typecast": False})
        except AirtableError as exc:
            return f"Nothing was undone: {exc}."
        if data.get("fields") is not None:
            _cache_replace(normalize(data, cfg))
        _log_append({"id": secrets.token_hex(6), "kind": "undo", "undoes": undo_id,
                     "at": datetime.now().isoformat(timespec="seconds"), "record": record_id})
        return "Undone: " + "; ".join(f"{c['label']} back to {c['before'] or '(empty)'}" for c in edit["changes"]) + "."
    finally:
        end_action(record_id)


def valid_record_id(record_id: str) -> bool:
    return bool(_RECORD_ID_RE.match(record_id or ""))
