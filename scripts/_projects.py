"""The Airtable "Projects" table: a read for the /projects page, and a small
allowlisted write-back and create (name, description, Status, assignee, requester, urgency,
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

SNAPSHOT_MIN_INTERVAL_SECONDS = 6 * 3600     # one snapshot per "save session", not one per click
SNAPSHOT_KEEP = 30                           # newest files kept in scripts/output/projects_snapshots/
SMOKE_PREFIX = "[Smoke test]"

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

# The fields the "New project" form can set (a subset of FIELD_SLUGS; Start and Completed dates, Blockers
# and Sprint notes are for a project that has begun, so they stay edit-only).
CREATE_SLUGS = ("name", "status", "assignee", "requester", "urgency", "end", "description")
DEFAULT_NEW_STATUS = "INTAKE"

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


# --- local snapshots (a backup beyond what Undo covers) ------------------------------------

def snapshot_dir():
    return _common.OUTPUT_DIR / "projects_snapshots"


def list_snapshots() -> list:
    """[(path, mtime)] newest first."""
    try:
        files = [p for p in snapshot_dir().glob("projects-*.json") if p.is_file()]
    except OSError:
        return []
    return sorted(((p, p.stat().st_mtime) for p in files), key=lambda t: t[1], reverse=True)


def take_snapshot(force: bool = False) -> dict:
    """Saves every record of the table, all fields (the page itself reads only some), to
    scripts/output/projects_snapshots/projects-<timestamp>.json, unless one is younger than
    SNAPSHOT_MIN_INTERVAL_SECONDS. Never raises. Returns {"ok", "taken", "path", "records", "error"}."""
    cfg = config()
    if not cfg["configured"]:
        return {"ok": False, "taken": False, "path": "", "records": 0, "error": "Airtable is not set up."}
    existing = list_snapshots()
    if existing and not force and time.time() - existing[0][1] < SNAPSHOT_MIN_INTERVAL_SECONDS:
        return {"ok": True, "taken": False, "path": existing[0][0].name, "records": 0, "error": ""}
    records, offset = [], ""
    try:
        for _page in range(MAX_PAGES):
            params = [("pageSize", "100")] + ([("offset", offset)] if offset else [])
            data = _request("GET", _table_path(cfg), params=params)
            records += data.get("records", [])
            offset = data.get("offset") or ""
            if not offset:
                break
        name = f"projects-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        folder = snapshot_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(json.dumps({"taken_at": datetime.now().isoformat(timespec="seconds"),
                                    "base": cfg["base"], "table": cfg["table"], "records": records},
                                   ensure_ascii=False), encoding="utf-8")
        _common._harden_file_permissions(path)
        for old, _mtime in list_snapshots()[SNAPSHOT_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass
    except (AirtableError, OSError) as exc:
        return {"ok": False, "taken": False, "path": "", "records": 0, "error": str(exc)}
    return {"ok": True, "taken": True, "path": name, "records": len(records), "error": ""}


def _snapshot_note() -> str:
    """Runs before a write; a failed snapshot never blocks the save, but is reported in its message."""
    result = take_snapshot()
    return "" if result["ok"] else f" (Snapshot skipped: {result['error']})"


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


def _check_value(slug: str, kind: str, label: str, new: str, people: dict, allow_empty_status: bool = False) -> str:
    """The problem with a proposed value for one allowlisted field, or "" when it is acceptable."""
    if kind == "select":
        if new not in STATUS_ORDER and not (allow_empty_status and not new):
            return f"{label}: \"{new}\" is not one of {', '.join(STATUS_ORDER)}."
    elif kind == "choice":
        if new and new not in URGENCY_OPTIONS:
            return f"{label}: \"{new}\" is not one of the Urgency Rating options."
    elif kind == "line":
        if "\n" in new:
            return f"{label}: must be a single line."
        if slug == "name" and not new:
            return f"{label}: cannot be empty."
        if len(new) > LINE_LIMIT:
            return f"{label}: longer than {LINE_LIMIT} characters."
    elif kind == "collaborator":
        if new and new not in people:
            return f"{label}: \"{new}\" is not in the list of people who can be assigned."
    elif kind == "date":
        if new and not _valid_date(new):
            return f"{label}: \"{new}\" is not a date (use YYYY-MM-DD)."
    else:
        if len(new) > TEXT_LIMIT:
            return f"{label}: longer than {TEXT_LIMIT} characters."
    return ""


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
        problem = _check_value(slug, kind, label, new, people)
        if problem:
            errors.append(problem)
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
    snap_note = _snapshot_note()
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
    return {"ok": True, "undo_id": undo_id, "message": f"Saved to Airtable{title} (confirmed): {summary}.{snap_note}"}


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


# --- creating a project ----------------------------------------------------------------

def plan_create(form: dict, existing_names=()) -> dict:
    """Validates a submitted "New project" form. Only CREATE_SLUGS are read; an unchecked-empty field
    is simply not sent. Returns {"values", "fields", "errors"}: values maps slug -> text for the
    review page, fields maps Airtable field name -> API value for the POST."""
    people = assignees()
    values, fields, errors = {}, {}, []
    for slug in CREATE_SLUGS:
        field, kind, label = FIELD_SLUGS[slug]
        vals = form.get(slug)
        new = _clean_text(vals[0]) if vals else ""
        problem = _check_value(slug, kind, label, new, people, allow_empty_status=True)
        if problem:
            errors.append(problem)
            continue
        values[slug] = new
        if new:
            fields[field] = _api_value(slug, new, people.get(new, ""))
    name = values.get("name", "")
    if name and name.casefold() in {n.strip().casefold() for n in existing_names}:
        errors.append(f"A project named \"{name}\" already exists. Pick another name, or open that project and edit it.")
    return {"values": values, "fields": fields, "errors": errors}


def commit_create(plan: dict) -> dict:
    """POSTs one new record, reads the reply back, logs it. Returns {"ok", "message", "record_id"}.
    Nothing is deleted anywhere: a create has no Undo, and the log entry records the new record's id."""
    if plan["errors"] or not plan["fields"].get(FIELD_SLUGS["name"][0]):
        return {"ok": False, "message": "Nothing was created: the form has problems.", "record_id": ""}
    cfg = config()
    snap_note = _snapshot_note()
    try:
        data = _request("POST", _table_path(cfg), body={"fields": plan["fields"], "typecast": False})
    except AirtableError as exc:
        return {"ok": False, "message": f"Nothing was created: {exc}.", "record_id": ""}
    record_id = str(data.get("id") or "")
    if not _RECORD_ID_RE.match(record_id) or data.get("fields") is None:
        return {"ok": False, "record_id": "",
                "message": "Airtable's reply held no new record. Check the table in Airtable before trying again."}
    saved = normalize(data, cfg)
    _cache_add(saved)
    now = current_values(saved)
    problems = [FIELD_SLUGS[s][2] for s, text in plan["values"].items() if text and not _same(s, now[s], text)]
    _log_append({"id": secrets.token_hex(6), "kind": "create", "at": datetime.now().isoformat(timespec="seconds"),
                 "record": record_id, "name": saved["name"],
                 "values": {s: t for s, t in plan["values"].items() if t}})
    if problems:
        return {"ok": True, "record_id": record_id,
                "message": f"Created \"{saved['name']}\". Airtable's reply did not match for {', '.join(problems)}; check the record."}
    return {"ok": True, "record_id": record_id, "message": f"Created \"{saved['name']}\" in Airtable (confirmed). Remove it in Airtable if it was a mistake.{snap_note}"}


def _cache_add(item: dict) -> None:
    with _cache_lock:
        cached = _cache["result"]
        if cached is None:
            return
        _cache["result"] = {**cached, "items": [item] + [i for i in cached["items"] if i["id"] != item["id"]]}


# --- real-write smoke test ----------------------------------------------------------------

def smoke_test() -> dict:
    """Creates a record named "[Smoke test] <time>", edits its Sprint Notes, checks both replies, then
    deletes it. Only the record this run created is ever deleted, and only after its name is re-read
    and still starts with SMOKE_PREFIX. Returns {"ok", "steps": [(label, ok, detail)], "record_id", "message"}.
    Meant to be started by a person; nothing calls it automatically."""
    cfg = config()
    steps, record_id = [], ""
    if not cfg["configured"]:
        return {"ok": False, "steps": [], "record_id": "", "message": "Airtable is not set up."}
    if not begin_action("smoke-test"):
        return {"ok": False, "steps": [], "record_id": "", "message": "A smoke test is already running."}
    name_field, notes_field = FIELD_SLUGS["name"][0], FIELD_SLUGS["notes"][0]

    def step(label, ok, detail=""):
        steps.append((label, ok, detail))
        return ok

    try:
        name = f"{SMOKE_PREFIX} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        try:
            data = _request("POST", _table_path(cfg), body={"fields": {name_field: name}, "typecast": False})
        except AirtableError as exc:
            step("Create a temporary record", False, str(exc))
            return _smoke_result(steps, record_id)
        record_id = str(data.get("id") or "")
        if not (_RECORD_ID_RE.match(record_id) and (data.get("fields") or {}).get(name_field) == name):
            step("Create a temporary record", False, "Airtable's reply did not match what was sent.")
            return _smoke_result(steps, record_id if _RECORD_ID_RE.match(record_id) else "")
        step("Create a temporary record", True, record_id)

        try:
            data = _request("PATCH", f"{_table_path(cfg)}/{urllib.parse.quote(record_id)}",
                            body={"fields": {notes_field: "smoke test edit"}, "typecast": False})
            step("Edit it (Sprint Notes) and read the reply back",
                 (data.get("fields") or {}).get(notes_field) == "smoke test edit",
                 "" if (data.get("fields") or {}).get(notes_field) == "smoke test edit" else "The reply did not hold the edit.")
        except AirtableError as exc:
            step("Edit it (Sprint Notes) and read the reply back", False, str(exc))

        try:
            current = _request("GET", f"{_table_path(cfg)}/{urllib.parse.quote(record_id)}")
            safe = str((current.get("fields") or {}).get(name_field, "")).startswith(SMOKE_PREFIX)
        except AirtableError as exc:
            step("Delete it", False, f"Could not re-read the record before deleting: {exc}")
            return _smoke_result(steps, record_id)
        if not safe:
            step("Delete it", False, "The record no longer carries the smoke-test name, so it was left alone.")
            return _smoke_result(steps, record_id)
        try:
            data = _request("DELETE", f"{_table_path(cfg)}/{urllib.parse.quote(record_id)}")
            deleted = bool(data.get("deleted")) and data.get("id") == record_id
            step("Delete it", deleted, "" if deleted else "Airtable did not confirm the deletion.")
            if deleted:
                _cache_remove(record_id)
        except AirtableError as exc:
            step("Delete it", False, str(exc))
        return _smoke_result(steps, record_id)
    finally:
        end_action("smoke-test")


def _smoke_result(steps: list, record_id: str) -> dict:
    ok = bool(steps) and all(s[1] for s in steps) and len(steps) == 3
    leftover = bool(record_id) and not (len(steps) == 3 and steps[2][1])
    if ok:
        message = "Write test passed: a temporary record was created, edited and deleted."
    elif leftover:
        message = (f"Write test failed. Record {record_id} (named \"{SMOKE_PREFIX} ...\") may still be in the table; "
                   "delete it in Airtable.")
    else:
        message = "Write test failed. Nothing was left in the table."
    _log_append({"id": secrets.token_hex(6), "kind": "smoke", "at": datetime.now().isoformat(timespec="seconds"),
                 "record": record_id, "ok": ok, "steps": [[a, b, c] for a, b, c in steps]})
    return {"ok": ok, "steps": steps, "record_id": record_id, "message": message}


def _cache_remove(record_id: str) -> None:
    with _cache_lock:
        cached = _cache["result"]
        if cached is not None:
            _cache["result"] = {**cached, "items": [i for i in cached["items"] if i["id"] != record_id]}


def check_connection() -> dict:
    """One read of a single record: {"ok", "message"}. Never raises."""
    cfg = config()
    if not cfg["configured"]:
        return {"ok": False, "message": "Airtable is not set up (AIRTABLE_TOKEN, AIRTABLE_BASE_ID and AIRTABLE_TABLE_ID)."}
    try:
        data = _request("GET", _table_path(cfg), params=[("pageSize", "1"), ("fields[]", FIELD_SLUGS["name"][0])])
    except AirtableError as exc:
        return {"ok": False, "message": f"Airtable read failed: {exc}."}
    return {"ok": True, "message": f"Airtable read worked ({len(data.get('records', []))} record fetched to test)."}


def valid_record_id(record_id: str) -> bool:
    return bool(_RECORD_ID_RE.match(record_id or ""))
