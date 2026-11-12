"""Shared helpers for the Zendesk local automation scripts (first_reply_check.py
and review_server.py). Kept in one place so both scripts resolve paths, load
config, and touch the shared pending-drafts store the same way -- the launchd
OAuth bug earlier this session was caused by exactly this kind of logic being
duplicated (and drifting) across files, so this time there's one source of
truth both scripts import from.
"""

import copy
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time as time_module
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from zendesk_mcp_server.zendesk_client import ZendeskClient  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
PENDING_FILE = OUTPUT_DIR / "pending_drafts.json"
PENDING_LOCK_FILE = OUTPUT_DIR / "pending_drafts.json.lock"
PENDING_ARCHIVE_FILE = OUTPUT_DIR / "pending_drafts_archive.json"

# How long a resolved (posted/dismissed/already_handled, or a done personal
# task) entry stays in its live file before archive_old_pending()/
# archive_old_personal_tasks() move it out -- see those functions below.
ARCHIVE_AFTER_DAYS = 30

REVIEW_SERVER_PORT = int(os.getenv("ZENDESK_REVIEW_PORT", "8765"))
REVIEW_SERVER_URL = f"http://127.0.0.1:{REVIEW_SERVER_PORT}/"

ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "")


def ticket_url(ticket_id) -> str:
    if not ZENDESK_SUBDOMAIN:
        return ""
    return f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{ticket_id}"


def resolve_token_path() -> str:
    """.env's ZENDESK_OAUTH_TOKEN_PATH is conventionally a bare relative
    filename (see .env.example), which only resolves correctly if cwd happens
    to be the repo root. launchd does not guarantee that -- that's what broke
    the cron script before -- so this resolves it against REPO_ROOT explicitly
    rather than trusting whatever the process's cwd happens to be."""
    raw = os.getenv("ZENDESK_OAUTH_TOKEN_PATH", str(REPO_ROOT / ".zendesk_oauth_tokens.json"))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return str(path)


def _harden_file_permissions(path) -> None:
    """Best-effort chmod to owner-read/write-only (0600) for a file that
    holds OAuth tokens or auth state -- plain write_text() (or
    zendesk_client.py's own token-file writes, which this project
    deliberately never modifies) both leave a file at whatever the process
    umask happens to allow, which on a shared/multi-user Mac account could
    be group- or world-readable. Never raises -- a permissions tweak
    failing shouldn't break anything it's just trying to harden, and a
    missing path (nothing written yet) is a no-op, not an error."""
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except Exception:
        pass


# --- Real page-load progress (the /sla, /firstreply, ... loading bar) --------
# The browser cannot see inside a pending navigation, so progress is measured
# on the server: while a request carrying a progress id (the csa_pid cookie
# that review_server.py's loading bar sets on click/submit) is being served,
# every Zendesk API call it makes through make_client() is counted. The bar
# polls /__progress for done / expected, where expected is the number of
# calls the previous load of the same route needed (learned, not guessed).
# Until a route has been loaded once, expected is unknown and the bar falls
# back to a simulated creep -- it never reports a percentage it cannot
# justify. A load that makes no API calls (all caches warm) has nothing to
# measure, and finishes in milliseconds anyway.
PROGRESS_COOKIE_NAME = "csa_pid"
_PROGRESS_ID_RE = re.compile(r"^[a-f0-9]{16}$")
_PROGRESS_KEEP_SECONDS = 300
_PROGRESS_MAX_RECORDS = 500
_PROGRESS_PCT_CAP = 0.95  # only "finished" may report 100%
_progress_registry: dict = {}
_progress_history: dict = {}
_progress_lock = threading.Lock()
_progress_tls = threading.local()


class _Progress:
    __slots__ = ("id", "created", "updated", "done", "parts", "finished", "lock")

    def __init__(self, pid: str):
        self.id = pid
        self.created = self.updated = time_module.time()
        self.done = 0
        self.parts: list = []  # expected calls per request in this navigation chain (None = unknown)
        self.finished = False
        self.lock = threading.Lock()

    def note_call(self) -> None:
        with self.lock:
            self.done += 1
            self.updated = time_module.time()


def progress_id_from_cookie(cookie_header: str):
    """The validated progress id carried by a Cookie header, or None."""
    if not cookie_header:
        return None
    try:
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(PROGRESS_COOKIE_NAME)
    except Exception:
        return None
    value = morsel.value if morsel else ""
    return value if _PROGRESS_ID_RE.match(value) else None


def progress_begin(pid: str, key: str):
    """Starts (or, after a redirect, continues) tracking for one request on
    this thread. Returns a token for progress_end."""
    now = time_module.time()
    with _progress_lock:
        for old_id in [i for i, r in _progress_registry.items() if now - r.updated > _PROGRESS_KEEP_SECONDS]:
            del _progress_registry[old_id]
        while len(_progress_registry) >= _PROGRESS_MAX_RECORDS:
            del _progress_registry[min(_progress_registry, key=lambda i: _progress_registry[i].updated)]
        progress = _progress_registry.get(pid)
        if progress is None or progress.finished:
            progress = _Progress(pid)
            _progress_registry[pid] = progress
        progress.parts.append(_progress_history.get(key))
        progress.updated = now
    _progress_tls.current = progress
    return (progress, key, progress.done)


def progress_end(token, redirected: bool = False) -> None:
    """Ends one request. A redirect leaves the record open so the follow-up
    GET (same cookie) continues it; anything else finishes it."""
    progress, key, done_at_begin = token
    _progress_tls.current = None
    calls = progress.done - done_at_begin
    with _progress_lock:
        if calls > 0:
            _progress_history[key] = calls
        progress.updated = time_module.time()
        if not redirected:
            progress.finished = True


def progress_snapshot(pid: str) -> dict:
    with _progress_lock:
        progress = _progress_registry.get(pid)
    if progress is None:
        return {"known": False, "done": 0, "expected": None, "pct": None, "finished": False}
    done = progress.done
    parts = progress.parts
    expected = sum(parts) if parts and all(x is not None for x in parts) else None
    if progress.finished:
        pct = 1.0
    elif expected:
        pct = min(done / expected, _PROGRESS_PCT_CAP)
    else:
        pct = None
    return {"known": True, "done": done, "expected": expected, "pct": pct, "finished": progress.finished}


class _ProgressClient:
    """Transparent wrapper around a ZendeskClient that counts completed
    Zendesk API calls for the request that created it. Everything else
    (attribute reads and writes, other methods) passes straight through."""
    _COUNTED = frozenset({
        "get_tickets", "get_ticket", "get_ticket_comments", "get_ticket_attachment", "search_users", "get_user",
        "get_groups", "get_all_articles", "post_comment", "create_ticket", "update_ticket",
    })

    def __init__(self, inner, progress):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_progress", progress)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in self._COUNTED and callable(attr):
            progress = self._progress

            def counted(*args, **kwargs):
                try:
                    return attr(*args, **kwargs)
                finally:
                    progress.note_call()
            return counted
        return attr

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


def progress_wrap(client):
    """client, wrapped to count API calls when this thread is serving a
    tracked request; otherwise returned unchanged."""
    current = getattr(_progress_tls, "current", None)
    return _ProgressClient(client, current) if current is not None else client


def make_client() -> ZendeskClient:
    # The shared oauth token file (see resolve_token_path -- also read and
    # refreshed by the sibling zendesk-mcp-server project) is written by
    # zendesk_client.py's own OAuth methods, not by this file, so there's
    # no write_text() call here to attach a chmod to directly. Instead,
    # opportunistically harden it every time a client is constructed --
    # cheap (a single stat+chmod syscall), and review_server.py builds a
    # fresh client on essentially every request, so this runs often enough
    # to matter shortly after any token refresh.
    _harden_file_permissions(resolve_token_path())
    return progress_wrap(ZendeskClient(
        subdomain=os.getenv("ZENDESK_SUBDOMAIN"),
        email=os.getenv("ZENDESK_EMAIL"),
        token=os.getenv("ZENDESK_API_KEY"),
        client_id=os.getenv("ZENDESK_CLIENT_ID"),
        client_secret=os.getenv("ZENDESK_CLIENT_SECRET"),
        redirect_uri=os.getenv("ZENDESK_REDIRECT_URI", "https://localhost/callback"),
        oauth_scopes=os.getenv("ZENDESK_OAUTH_SCOPES", "read write"),
        oauth_token_path=resolve_token_path(),
        timeout=int(os.getenv("ZENDESK_TIMEOUT_SECONDS", "30")),
    ))


def _atomic_write_json(path: Path, data) -> None:
    """Writes JSON to `path` atomically: serialize to a sibling `.tmp` file
    in the same directory, then os.replace() it over the real path.
    os.replace is atomic on POSIX -- the destination is always either the
    old complete file or the new complete file, never a half-written one
    -- so a crash or a full disk mid-write can't leave pending_drafts.json
    / personal_tasks.json truncated the way a plain write_text() call
    could. That matters here specifically because load_pending() /
    load_personal_tasks() both fail open with a broad except-return-{} on
    a parse error, which would otherwise silently make every tracked
    ticket/task vanish with nothing logged. Always called from inside the
    relevant file's own lock (update_pending/update_personal_tasks), so
    there's no separate locking concern here -- just the write itself."""
    tmp_path = path.parent / f"{path.name}.tmp"
    tmp_path.write_text(json.dumps(data, indent=2))
    os.replace(tmp_path, path)


def _pending_lock():
    lock_file = open(PENDING_LOCK_FILE, "w")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def load_pending() -> dict:
    """Plain (unlocked) read -- fine for rendering the review page, where a
    torn read is astronomically unlikely and not safety-critical. Anything
    that mutates the store should go through update_pending() instead."""
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            return {}
    return {}


# Per-request memoization for load_pending()/load_personal_tasks(), used
# ONLY by the read-only rendering call sites below (pending_items,
# posted_today_items, summary_counts, recent_replied_items,
# draft_personal_tasks, open_personal_tasks, completed_personal_tasks) --
# never by update_pending/update_personal_tasks, the candidate-scan
# function, or any of the pre-mutation freshness checks
# (promote/complete/quick-solve_personal_task, reconcile_personal_tasks).
# Those all keep calling load_pending()/load_personal_tasks() directly, so
# a read-modify-write -- or a decision about whether to make a live
# Zendesk call -- always sees the true current file, never a snapshot
# cached earlier in the same request.
#
# Rendering "/" alone calls the equivalent of load_pending() four separate
# times today (the pending-count badge, the main queue itself, "Replied
# today", and the worked-on sidebar's local half) -- each re-reading and
# re-json.loads-ing the identical file from disk. /tasks (and /overview)
# do the same to personal_tasks.json for their nav badge count and page
# body. This collapses each back down to one real read per request.
#
# threading.local rather than a plain module-level dict: review_server.py's
# ThreadingHTTPServer hands each incoming request its own new thread (not
# a reused pool), so this already starts empty on every request -- but
# clear_request_cache() (called at the top of do_GET/do_POST regardless)
# makes that explicit rather than relying on it silently, in case the
# server's threading model ever changes.
_request_cache = threading.local()


def _cached_load_pending() -> dict:
    """load_pending(), memoized for the lifetime of one request -- see the
    comment above _request_cache."""
    cached = getattr(_request_cache, "pending", None)
    if cached is None:
        cached = load_pending()
        _request_cache.pending = cached
    return cached


def clear_request_cache() -> None:
    """Called once at the top of every incoming request (see
    review_server.py's do_GET/do_POST) so each request gets its own fresh
    read of each store the first time it's actually needed, never one left
    over from an earlier request on the same worker thread."""
    _request_cache.pending = None
    _request_cache.personal_tasks = None


def update_pending(mutate_fn) -> None:
    """Read-modify-write the pending store under an exclusive lock, so the
    cron script (adding new candidates every 2 minutes) and the review server
    (marking approve/reject whenever you click) can never clobber each
    other's concurrent writes. mutate_fn receives the current dict and
    mutates it in place.

    Clears _request_cache's memoized copy (see _cached_load_pending) right
    after writing -- not just at a request boundary. review_server.py's
    real request lifecycle never reads via the cache and then writes
    within that same request today, so this is currently a pure safety
    net rather than something the happy path depends on -- but it's what
    makes _cached_load_pending correct for ANY caller, not just one that's
    careful to only ever run inside a single do_GET/do_POST: two writes in
    a row from the same thread (the test suite calling add_personal_task
    twice, a script, a future change to how requests are dispatched) would
    otherwise keep serving the FIRST call's now-stale snapshot forever,
    since nothing else would ever clear it."""
    lock_file = _pending_lock()
    try:
        data = load_pending()
        mutate_fn(data)
        _atomic_write_json(PENDING_FILE, data)
    finally:
        lock_file.close()
    clear_request_cache()


# review_server.py's _post() (posting a public reply or internal comment)
# does a real Zendesk write (client.post_comment) that must never happen
# twice for the same ticket -- but like promote_personal_task/
# complete_personal_task above, it can't just check-then-mutate entirely
# under update_pending's own file lock, since that lock is only taken for
# the final "mark posted" write, not for the live post_comment call itself
# (holding a file lock across a network call would block every other
# unrelated pending-ticket mutation for however long Zendesk takes to
# respond). Two overlapping POSTs for the same ticket_id -- a double-click
# before the page's own button-disable JS fires, or a browser retrying a
# slow request -- are a real scenario on this ThreadingHTTPServer, not just
# a theoretical one: without this guard, both could pass the
# already-replied check and both call post_comment, posting the same reply
# twice. This in-process guard (reset on restart, irrelevant across two
# separate processes) closes that window: only one thread at a time may be
# mid-post for a given ticket_id. Deliberately a separate set from
# _personal_task_actions_in_progress above -- ticket ids and personal-task
# local ids are different id spaces, and keeping them in separate guards
# means a coincidental string collision between the two could never cause
# one action to block an unrelated other.
_pending_ticket_action_lock = threading.Lock()
_pending_ticket_actions_in_progress: set = set()


def _begin_pending_ticket_action(ticket_id: str) -> bool:
    """Returns True if the caller may proceed (and must call
    _end_pending_ticket_action(ticket_id) in a finally block once done),
    False if another thread is already mid-post for this same ticket_id."""
    with _pending_ticket_action_lock:
        if ticket_id in _pending_ticket_actions_in_progress:
            return False
        _pending_ticket_actions_in_progress.add(ticket_id)
        return True


def _end_pending_ticket_action(ticket_id: str) -> None:
    with _pending_ticket_action_lock:
        _pending_ticket_actions_in_progress.discard(ticket_id)


def _archive_terminal_entries(live_path: Path, archive_path: Path, lock_fn, is_archivable, entry_timestamp, after_days: int, log_fn=print) -> int:
    """Shared mechanics behind archive_old_pending()/
    archive_old_personal_tasks() below: pending_drafts.json and
    personal_tasks.json only ever grew before this -- nothing removed a
    resolved entry (posted/dismissed/already_handled, or a completed
    personal task) from the live file, so both crept larger the longer
    this tool stayed in daily use, and every single mutation's
    read-modify-write under fcntl had correspondingly more to read and
    write each time.

    Under the given file's own lock, splits its dict into entries to keep
    and entries that are both archivable (per is_archivable) and older
    than after_days (per entry_timestamp, parsed as an isoformat string --
    an entry with no usable timestamp is left alone rather than guessed
    at), moves the old ones into archive_path (a flat list -- nothing
    reads it back programmatically, it's a human-browsable history file,
    not a second live store this tool has to keep correct), then
    atomically rewrites the live file with only what's left. History is
    moved, never deleted outright.

    Returns how many entries were archived (0 if nothing qualified,
    including if live_path doesn't exist yet, or if it exists but can't be
    parsed -- that's left for load_pending()/load_personal_tasks()'s own
    fail-open handling to deal with, not this function's problem to fix)."""
    if not live_path.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=after_days)
    lock_file = lock_fn()
    try:
        try:
            data = json.loads(live_path.read_text())
        except Exception as exc:
            log_fn(f"_archive_terminal_entries({live_path.name}): couldn't read the live file, skipping this pass ({exc})")
            return 0

        to_archive = {}
        for key, entry in list(data.items()):
            if not is_archivable(entry):
                continue
            ts = entry_timestamp(entry)
            if not ts:
                continue
            try:
                when = datetime.fromisoformat(ts)
            except Exception:
                continue
            if when < cutoff:
                to_archive[key] = data.pop(key)

        if not to_archive:
            return 0

        existing_archive = []
        if archive_path.exists():
            try:
                existing_archive = json.loads(archive_path.read_text())
            except Exception as exc:
                log_fn(f"_archive_terminal_entries({live_path.name}): existing archive file was unreadable, starting a fresh one ({exc})")
                existing_archive = []
        existing_archive.extend(to_archive.values())

        _atomic_write_json(archive_path, existing_archive)
        _atomic_write_json(live_path, data)
        return len(to_archive)
    finally:
        lock_file.close()


def archive_old_pending(after_days: int = ARCHIVE_AFTER_DAYS, log_fn=print) -> int:
    """Moves pending_drafts.json entries in a terminal status (posted,
    dismissed, already_handled) whose resolving timestamp is older than
    after_days into pending_drafts_archive.json. Never touches a
    still-"pending" entry no matter how old -- "every candidate stays on
    the page until you act on it" (see the design principle of the same
    name) means archiving only ever applies to entries already resolved
    one way or another. Called from scan_and_merge(), so it runs on both
    the 2-minute cron tick and a manual Refresh -- the same two places
    everything else in this file's detection logic already runs from."""
    def is_archivable(entry):
        return entry.get("status") in ("posted", "dismissed", "already_handled")

    def entry_timestamp(entry):
        return entry.get("posted_at") or entry.get("dismissed_at") or entry.get("resolved_at") or entry.get("created_at")

    count = _archive_terminal_entries(
        PENDING_FILE, PENDING_ARCHIVE_FILE, _pending_lock, is_archivable, entry_timestamp, after_days, log_fn=log_fn,
    )
    if count:
        log_fn(f"Archived {count} resolved ticket(s) from pending_drafts.json (older than {after_days} days).")
    return count


def merge_candidates(drafted_candidates: list) -> list:
    """drafted_candidates: list of dicts with ticket_id, subject,
    requester_id, draft, url. Adds any ticket not already tracked. Existing
    entries -- whether still pending review, already posted, or already
    rejected -- are left untouched, so a human decision (or an in-progress
    edit sitting in the review page) is never silently overwritten. Returns
    the list of entries that were newly added, i.e. genuinely new tickets
    never seen before -- the caller uses these (not just a count) to build a
    notification with real ticket details.
    """
    added = []

    def _mutate(data):
        for c in drafted_candidates:
            key = str(c["ticket_id"])
            if key in data:
                continue
            entry = {
                **c,
                "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            data[key] = entry
            added.append(entry)

    update_pending(_mutate)
    return added


def pending_items() -> list:
    data = _cached_load_pending()
    return [v for v in data.values() if v.get("status") == "pending"]


def posted_today_items() -> list:
    """Every ticket actually posted through this tool (public reply or
    internal comment) today, most-recent first -- the data behind the
    review page's "Replied today" section. Deliberately scoped to `posted`
    only (not dismissed or already_handled elsewhere) since those weren't
    something you replied to; see summary_counts()'s posted_today count for
    the same underlying filter used elsewhere on the page."""
    data = _cached_load_pending()
    today = datetime.now().date().isoformat()
    items = [
        v for v in data.values()
        if v.get("status") == "posted" and (v.get("posted_at") or "").startswith(today)
    ]
    return sorted(items, key=lambda i: i.get("posted_at", ""), reverse=True)


# --- failure-notification throttling --------------------------------------
# The cron script runs every 2 minutes during business hours. If Zendesk auth
# breaks or a scan starts erroring, we want you to find out -- but not via a
# fresh notification every 2 minutes for as long as it stays broken. These
# helpers throttle "something's wrong" notifications to once per window, and
# reset the moment things start working again so a NEW failure still alerts
# promptly rather than waiting out a stale window from an old, unrelated one.

FAILURE_STATE_FILE = OUTPUT_DIR / "failure_notify_state.json"
FAILURE_NOTIFY_THROTTLE_MINUTES = 60


def should_notify_failure() -> bool:
    if not FAILURE_STATE_FILE.exists():
        return True
    try:
        state = json.loads(FAILURE_STATE_FILE.read_text())
        last = datetime.fromisoformat(state["last_notified"])
    except Exception:
        return True
    return datetime.now() - last >= timedelta(minutes=FAILURE_NOTIFY_THROTTLE_MINUTES)


def mark_failure_notified() -> None:
    FAILURE_STATE_FILE.write_text(
        json.dumps({"last_notified": datetime.now().isoformat(timespec="seconds")})
    )


def clear_failure_notified() -> None:
    """Call this once a check succeeds again, so the next real failure
    notifies right away instead of waiting out a throttle window left over
    from a problem that's already resolved."""
    if FAILURE_STATE_FILE.exists():
        try:
            FAILURE_STATE_FILE.unlink()
        except Exception:
            pass


# --- cached auth status ------------------------------------------------------
# GET / deliberately never constructs a live ZendeskClient (that would mean
# every page load risks a network call / token refresh just to render a list
# from a local file). Instead, whichever of the three consumers actually did
# construct a client recently (the cron tick, or a manual approve/refresh)
# records what it found here, so the page can show "Zendesk connection: needs
# reconnecting" from a cheap local read -- accurate as of the last real check,
# not a live probe.

AUTH_STATE_FILE = OUTPUT_DIR / "auth_state.json"


def mark_auth_ok() -> None:
    AUTH_STATE_FILE.write_text(
        json.dumps({"ok": True, "checked_at": datetime.now().isoformat(timespec="seconds")})
    )
    _harden_file_permissions(AUTH_STATE_FILE)


def mark_auth_broken(reason: str) -> None:
    AUTH_STATE_FILE.write_text(
        json.dumps(
            {"ok": False, "reason": reason, "checked_at": datetime.now().isoformat(timespec="seconds")}
        )
    )
    _harden_file_permissions(AUTH_STATE_FILE)


def auth_status() -> dict:
    """Returns {"ok": True} if nothing has ever been recorded -- don't show a
    scary banner before any check has actually run."""
    if not AUTH_STATE_FILE.exists():
        return {"ok": True}
    try:
        return json.loads(AUTH_STATE_FILE.read_text())
    except Exception:
        return {"ok": True}


# --- local OAuth reconnect flow ---------------------------------------------
# begin_oauth_authorization()/complete_oauth_authorization() (in
# zendesk_client.py) already do the real work -- they're the same methods
# used via a live Cowork session earlier. The catch: complete_oauth_authorization
# checks its `state` argument against self._pending_oauth_state, which lives
# in memory on whichever ZendeskClient instance called begin_oauth_authorization.
# Each HTTP request in review_server.py constructs a brand new client, so that
# in-memory state doesn't survive between the "start" request and the
# "complete" request (which happens after you've gone to Zendesk and back).
# This file bridges that gap: persist just the state string (and the
# authorization URL, for display) to disk, then re-inject it into the fresh
# client built for the "complete" step before calling complete_oauth_authorization.

OAUTH_PENDING_FILE = OUTPUT_DIR / "oauth_pending.json"
OAUTH_PENDING_TTL_MINUTES = 15  # Zendesk authorization codes are short-lived;
# don't let a flow you started and abandoned linger indefinitely as if valid.


def save_oauth_pending(authorization_url: str, state: str) -> None:
    OAUTH_PENDING_FILE.write_text(
        json.dumps(
            {
                "authorization_url": authorization_url,
                "state": state,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    )
    _harden_file_permissions(OAUTH_PENDING_FILE)


def load_oauth_pending() -> dict:
    """Returns {} if there's no pending flow, or the flow has aged out."""
    if not OAUTH_PENDING_FILE.exists():
        return {}
    try:
        data = json.loads(OAUTH_PENDING_FILE.read_text())
        started = datetime.fromisoformat(data["started_at"])
    except Exception:
        return {}
    if datetime.now() - started > timedelta(minutes=OAUTH_PENDING_TTL_MINUTES):
        return {}
    return data


def clear_oauth_pending() -> None:
    if OAUTH_PENDING_FILE.exists():
        try:
            OAUTH_PENDING_FILE.unlink()
        except Exception:
            pass


# --- shared scan-and-draft logic --------------------------------------------
# This is the actual "talk to Zendesk and figure out what needs a reply" logic.
# It lives here (not duplicated in first_reply_check.py AND review_server.py)
# so the 2-minute cron tick and the review page's manual Refresh button are
# always running the exact same detection logic -- two copies of this that
# could drift apart is exactly the kind of thing that caused the launchd
# OAuth-path bug earlier this session.

IT_OPERATIONS_GROUP_ID = 7783360594455

# The IT-Security queue -- a separate group from IT-Operations above. Only
# used for the review page's read-only "recent activity" column (see
# fetch_recent_tickets, below); unlike IT_OPERATIONS_GROUP_ID, nothing here
# feeds find_candidates/build_drafts, since that column doesn't draft or
# post replies, just shows what's recently come in.
IT_SECURITY_GROUP_ID = 4780342046231
IT_SECURITY_COLUMN_LIMIT = int(os.getenv("IT_SECURITY_COLUMN_LIMIT", "5"))  # how
# many recent IT-Security tickets the column shows -- overridable via .env
# rather than a hardcoded 5 at the review_server.py call site, for whoever
# wants a longer (or shorter) read-only glance at that queue.

# Tag applied to every personal-task ticket created via create_personal_task
# (below) -- checked by find_candidates() above to keep a self-created,
# self-assigned to-do from ever being mistaken for a customer ticket
# awaiting a first reply, regardless of which group Zendesk files it under
# (Zendesk auto-assigns a group at creation time; this tool doesn't control
# or need to know which one).
PERSONAL_TASK_TAG = "personal_task"

# Everything below mirrors this project's sibling "Zendesk Personal Task
# Tracker" Claude skill (a conversational skill that does the same job via
# chat) -- the /tasks page is meant to behave like that skill, not
# reinvent its own recipe. These three custom-field ids live on the
# "IT Requests" Zendesk ticket form; Department and Ticket Allocation never
# vary (always IT / CSA for this one user's personal tasks), so they're
# fixed values here rather than anything the page asks for. Estimated
# minutes is the one per-task value, entered on the "Make Zendesk Ticket
# from Task" form (not the earlier "Add task" draft form) since it's
# Zendesk-ticket metadata a quick local draft doesn't need yet -- matching
# the skill's own "ask for it when creating the ticket" framing.
PERSONAL_TASK_DEPARTMENT_FIELD_ID = 8510083340695
PERSONAL_TASK_EST_MINUTES_FIELD_ID = 29450755118359
PERSONAL_TASK_ALLOCATION_FIELD_ID = 42288679067287
PERSONAL_TASK_DEPARTMENT_VALUE = "it"
PERSONAL_TASK_PRIORITY = "low"
PERSONAL_TASK_EST_MINUTES_MAX = 1440  # 24 hours -- a personal task ticket is
# a quick to-do, not a multi-day project; this just catches an obvious
# fat-fingered value (e.g. an extra zero) before it becomes ticket metadata,
# not a claim that 24h is some meaningful policy limit.

# Ticket Allocation used to be a fixed "csa" value -- it's now a choice on
# the promote form, between the two allocations this Zendesk instance
# actually has configured for that field. NOTE: "csaif"'s exact value is
# an assumption (matching "csa"'s existing lowercase-tag convention, not
# independently confirmed against this Zendesk instance's real field
# configuration) -- if promoting with Allocation=CSAIF ever comes back
# with a Zendesk field-value error, this is the one constant to fix.
PERSONAL_TASK_ALLOCATION_CSA = "csa"
PERSONAL_TASK_ALLOCATION_CSAIF = "csaif"
PERSONAL_TASK_ALLOCATION_VALUE = PERSONAL_TASK_ALLOCATION_CSA  # default/legacy value
PERSONAL_TASK_ALLOCATION_CHOICES = (PERSONAL_TASK_ALLOCATION_CSA, PERSONAL_TASK_ALLOCATION_CSAIF)
PERSONAL_TASK_ALLOCATION_LABELS = {
    PERSONAL_TASK_ALLOCATION_CSA: "CSA",
    PERSONAL_TASK_ALLOCATION_CSAIF: "CSAIF",
}

# The two comments the skill posts automatically -- "no need to ask the
# user" -- immediately after every ticket it creates: an internal note
# first, then a public reply. promote_personal_task() below posts both,
# in this order, right after create_ticket succeeds.
PERSONAL_TASK_RECEIVED_NOTE = "Ticket has been received"
PERSONAL_TASK_RECEIVED_REPLY = "Ticket has been received for processing."

# Assignee/Requester on the promote form are now a choice too, between
# this tool's own account ("self" -- Catherine Vee, resolved via
# ZENDESK_EMAIL, exactly as before this choice existed) and a shared
# "IT-Operations" Zendesk account, resolved via a NEW env var,
# ZENDESK_IT_OPERATIONS_EMAIL (see resolve_it_operations_agent_id below).
# That env var isn't set by this tool -- add it to this project's own
# .env with the shared account's email for the "IT-Operations" choice to
# actually resolve to something; left unset, choosing it fails clearly
# (see resolve_personal_task_identity) rather than silently falling back
# to self.
PERSONAL_TASK_IDENTITY_SELF = "self"
PERSONAL_TASK_IDENTITY_IT_OPERATIONS = "it_operations"
PERSONAL_TASK_IDENTITY_CHOICES = (PERSONAL_TASK_IDENTITY_SELF, PERSONAL_TASK_IDENTITY_IT_OPERATIONS)
PERSONAL_TASK_IDENTITY_LABELS = {
    PERSONAL_TASK_IDENTITY_SELF: "Catherine Vee",
    PERSONAL_TASK_IDENTITY_IT_OPERATIONS: "IT-Operations",
}

# Category on a personal task used to be free text -- it's now a closed
# choice of two, since the whole point of picking one is that it gets
# auto-filled onto the subject box as a bracketed leading tag (see
# review_server.py's _category_select_html and its paired "change"
# listener in interaction_script). A fixed, known set of tags is what
# makes that auto-fill predictable; free text couldn't drive it the same
# way. Nothing here migrates an existing task's older free-typed category
# string -- add_personal_task/edit_personal_task still accept and store
# whatever string they're given, same as before.
PERSONAL_TASK_CATEGORY_CHOICES = ("[Documentation]", "[Configuration]")


def _personal_task_custom_fields(est_mins, allocation=None) -> list:
    """The three IT Requests custom fields Zendesk expects on every
    personal-task ticket -- required both when creating it and again when
    solving it (the skill notes Zendesk rejects a solve that omits any
    one of them). est_mins may be None for a task promoted before this
    field existed in this tool; in that case the field is left out of the
    list entirely rather than sent as a made-up value. allocation
    defaults to PERSONAL_TASK_ALLOCATION_VALUE (CSA) when not given --
    covers tasks promoted before Allocation was a per-task choice."""
    allocation = allocation or PERSONAL_TASK_ALLOCATION_VALUE
    fields = [
        {"id": PERSONAL_TASK_DEPARTMENT_FIELD_ID, "value": PERSONAL_TASK_DEPARTMENT_VALUE},
        {"id": PERSONAL_TASK_ALLOCATION_FIELD_ID, "value": allocation},
    ]
    if est_mins is not None:
        fields.append({"id": PERSONAL_TASK_EST_MINUTES_FIELD_ID, "value": est_mins})
    return fields


# Both resolve_own_agent_id and resolve_it_operations_agent_id (below) used
# to make a live search_users() call on every single invocation -- once per
# scan, once per post, once per /tasks page load, even once per
# fetch_recent_tickets() call for the IT-Security column -- with no caching
# at all, unlike fetch_recent_tickets' own _security_ticket_cache just below
# it. This shared cache/helper gives both the same treatment.
AGENT_ID_CACHE_TTL_SECONDS = 120  # matches SECURITY_CACHE_TTL_SECONDS's cadence
_agent_id_cache: dict = {}  # email -> {"id": int|None, "resolved_at": datetime}
# review_server.py serves every request on its own thread (ThreadingHTTPServer),
# so this cache's own read-check-then-write is a real concurrent-access path,
# not just a theoretical one -- two nearly-simultaneous requests for the same
# email could otherwise both see a cache miss and both call search_users.
# This lock guards only the dict read and the dict write below, never the
# search_users() call itself -- holding it across a live network call would
# serialize every unrelated email lookup for however long Zendesk takes to
# respond, the same "don't lock across the slow part" reasoning
# update_pending's own file lock already uses for post_comment (see its
# comment above _pending_ticket_action_lock). A plain dict assignment is a
# single atomic step in CPython, so once the lock is released, no reader can
# ever observe a torn/partial write -- this doesn't fully dedupe two threads
# racing to fill a cold cache (both can still independently call
# search_users), it just guarantees the cache itself is always either the old
# complete state or the new complete one, never something in between.
_agent_id_cache_lock = threading.Lock()


def _resolve_agent_id_by_email(client, email: str, log_fn=print):
    """Looks up which Zendesk user id `email` resolves to, via
    search_users-then-confirm-the-exact-email-matches (search_users can
    return near-matches, not just exact ones). Serves a cached id within
    AGENT_ID_CACHE_TTL_SECONDS of the last successful lookup for that
    email, with zero Zendesk API calls on a cache hit. A failed lookup
    (search_users raises) is NOT cached -- a transient API hiccup should
    be retried on the very next call, not stuck returning None for the
    rest of the cache window. Returns None if nothing matches (also
    cached, since that's a real answer -- "no such user" -- not a
    transient failure)."""
    now = datetime.now()
    with _agent_id_cache_lock:
        cached = _agent_id_cache.get(email)
        if cached and (now - cached["resolved_at"]).total_seconds() < AGENT_ID_CACHE_TTL_SECONDS:
            return cached["id"]

    try:
        matches = client.search_users(email)
    except Exception as exc:
        log_fn(f"_resolve_agent_id_by_email({email}): search_users failed ({exc})")
        return None

    resolved_id = None
    for user in matches:
        if (user.get("email") or "").lower() == email.lower():
            resolved_id = user.get("id")
            break
    with _agent_id_cache_lock:
        _agent_id_cache[email] = {"id": resolved_id, "resolved_at": now}
    return resolved_id


def resolve_it_operations_agent_id(client, log_fn=print):
    """Resolves the shared "IT-Operations" Zendesk account -- the
    Assignee/Requester identity choice that isn't "Catherine Vee" --
    from the ZENDESK_IT_OPERATIONS_EMAIL env var, via the same cached
    _resolve_agent_id_by_email lookup resolve_own_agent_id uses for
    ZENDESK_EMAIL. Returns None (not an exception) if that env var is
    unset, search_users is unreachable, or no matching user is found --
    resolve_personal_task_identity turns a None here into a clear
    "add this to your .env" error rather than silently falling back to
    self."""
    email = os.getenv("ZENDESK_IT_OPERATIONS_EMAIL", "")
    if not email:
        return None
    return _resolve_agent_id_by_email(client, email, log_fn=log_fn)


def resolve_personal_task_identity(client, choice: str, own_agent_id, log_fn=print):
    """Resolves a promote form's Assignee/Requester choice ("self" or
    "it_operations") to an actual Zendesk user id -- both requester_id
    and assignee_id must be real Zendesk users (not a group), so
    "IT-Operations" resolves to the shared account above rather than the
    IT-Operations Zendesk *group* this tool already tracks elsewhere
    (IT_OPERATIONS_GROUP_ID) -- that's a ticket's queue, not a person,
    and can't stand in for a requester or assignee id.

    Returns (user_id, error) -- error is None on success, or a
    human-readable string explaining what to fix (an unresolved
    ZENDESK_EMAIL for "self", or an unset/unresolved
    ZENDESK_IT_OPERATIONS_EMAIL for "it_operations") when user_id is
    None."""
    if choice == PERSONAL_TASK_IDENTITY_IT_OPERATIONS:
        it_ops_id = resolve_it_operations_agent_id(client, log_fn=log_fn)
        if it_ops_id is None:
            return None, "Could not resolve the IT-Operations account -- add ZENDESK_IT_OPERATIONS_EMAIL to your .env."
        return it_ops_id, None
    if own_agent_id is None:
        return None, "Could not resolve your own Zendesk account from ZENDESK_EMAIL -- check that .env value."
    return own_agent_id, None

# How far back each scan looks for candidate tickets. Widened from the
# original 24h: a ticket that arrives and ages past this window before this
# Mac ever wakes up to scan it (e.g. the lid stays closed over a long
# weekend) never gets recorded as a candidate at all -- find_candidates()
# stops paging once it hits a ticket older than the cutoff, so anything past
# it is silently invisible to that scan, not just delayed. 72h covers a
# closed-lid weekend with real margin, at the cost of a slightly larger
# Zendesk search per scan (negligible for one team's ticket volume).
# Overridable via .env if 72h ever isn't enough (a longer trip, etc.).
LOOKBACK_HOURS = int(os.getenv("ZENDESK_LOOKBACK_HOURS", "72"))

RUN_LOCK_FILE = OUTPUT_DIR / "run.lock"
LAST_SCAN_FILE = OUTPUT_DIR / "last_scan.json"


def acquire_run_lock():
    """Non-blocking exclusive lock shared by the cron script's scheduled tick
    and the review page's manual Refresh button, so a scheduled scan and a
    manual one (or two manual ones from an eager double-click) never hit
    Zendesk at the same time. Returns the open file handle to keep the lock
    alive for as long as the scan runs, or None if something else already
    holds it -- caller should back off rather than proceed."""
    lock_file = open(RUN_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def mark_scan_completed() -> None:
    LAST_SCAN_FILE.write_text(
        json.dumps({"completed_at": datetime.now().isoformat(timespec="seconds")})
    )


def last_scan_completed_at() -> str:
    if not LAST_SCAN_FILE.exists():
        return ""
    try:
        return json.loads(LAST_SCAN_FILE.read_text()).get("completed_at", "")
    except Exception:
        return ""


# --- business hours + "next automatic check" estimate -----------------------
# The source of truth for when first_reply_check.py actually runs its scan
# (as opposed to firing every CRON_INTERVAL_SECONDS and immediately no-oping).
# Lives here, not duplicated in first_reply_check.py, for the same reason
# everything else in this file does -- two copies of "what counts as business
# hours" is exactly the kind of drift that caused the launchd OAuth-path bug.

BUSINESS_HOURS = range(9, 17)  # 9am - 4pm local, last check starts at 4pm
BUSINESS_WEEKDAYS = range(0, 5)  # Mon=0 ... Fri=4

# Mirrors com.csa.zendesk-first-reply-check.plist's StartInterval. launchd
# reads that value from the plist itself, not from here -- this constant only
# feeds the "next automatic check" estimate below, so if the plist's interval
# is ever changed again, update this too or the estimate will quietly drift
# out of sync with what's actually scheduled.
CRON_INTERVAL_SECONDS = 120


def in_business_hours(when=None) -> bool:
    when = when or datetime.now()
    return when.weekday() in BUSINESS_WEEKDAYS and when.hour in BUSINESS_HOURS


def _next_business_hours_start(after: datetime) -> datetime:
    """First moment strictly after `after` that falls inside business hours
    -- i.e. when the automated check will next actually run if `after` is
    currently outside Mon-Fri 9am-4pm. Walks forward day by day (capped at a
    week, comfortably covering any weekend) rather than hardcoding "add 3
    days if it's Friday"-style special cases."""
    day = after.date()
    for offset in range(8):
        candidate_date = day + timedelta(days=offset)
        if candidate_date.weekday() not in BUSINESS_WEEKDAYS:
            continue
        candidate_start = datetime.combine(candidate_date, dtime(hour=BUSINESS_HOURS.start))
        if candidate_start > after:
            return candidate_start
    return after + timedelta(days=1)  # unreachable in practice; safe fallback


def next_scan_target(now=None) -> dict:
    """Machine-readable answer to "when does the next automatic check run" --
    the shared source of truth behind both next_scan_description() (the
    static human string) and the review page's live JS countdown. Returns:
      {"target_iso": <isoformat string, or None>, "precise": <bool>}
    "precise" means target_iso is an exact moment launchd will actually
    resume (outside business hours, business hours start is fixed); when
    False it's an estimate derived from the last completed scan, since
    launchd's StartInterval timer is phased from whenever the job was last
    loaded, not from a fixed clock boundary -- there's no way to know the
    exact second it'll next fire from outside that process. target_iso is
    None only when we're inside business hours but have no last-scan
    timestamp to estimate from yet (e.g. right after install)."""
    now = now or datetime.now()
    if not in_business_hours(now):
        next_start = _next_business_hours_start(now)
        return {"target_iso": next_start.isoformat(timespec="seconds"), "precise": True}

    last = last_scan_completed_at()
    if last:
        try:
            estimate = datetime.fromisoformat(last) + timedelta(seconds=CRON_INTERVAL_SECONDS)
            if estimate > now:
                return {"target_iso": estimate.isoformat(timespec="seconds"), "precise": False}
        except Exception:
            pass
    return {"target_iso": None, "precise": False}


def next_scan_description() -> str:
    """Human-readable answer to "when does the next automatic check run" --
    shown on the review page (as a fallback / no-JS text) so waiting for a
    ticket to appear doesn't feel like a black box. Built on top of
    next_scan_target() so the underlying calendar/estimate logic lives in
    exactly one place."""
    now = datetime.now()
    target = next_scan_target(now)
    if target["target_iso"] is None:
        return "within the next couple of minutes"

    target_dt = datetime.fromisoformat(target["target_iso"])
    if target["precise"]:
        return f"resumes {target_dt.strftime('%a %-I:%M %p')} (outside business hours)"
    return f"around {target_dt.strftime('%-I:%M:%S %p')}"


def guess_requester_name(client, requester_id: int, description: str, log_fn=print):
    """Look for a signature in the ticket text, then confirm it against
    requester_id via search_users before trusting it -- never trust a name
    match alone, since first-name collisions are real (see the skill's
    guardrails around this).

    Returns (name, confidence, reason):
      confidence is one of:
        "confirmed"   -- a candidate name was cross-checked against
                         requester_id via search_users and matched.
        "unconfirmed" -- a name-shaped line was found in the ticket text, but
                         search_users couldn't be reached to verify it's
                         actually this requester (not the same as a collision
                         -- just an unverified guess).
        "none"        -- no plausible name found in the text at all, or none
                         of the candidates found matched the real requester.
      reason gives the review page enough detail to explain *why* to a human,
      instead of just showing a bare warning flag:
        "matched"             -- paired with "confirmed".
        "search_unavailable"  -- paired with "unconfirmed"; search_users itself
                                 raised, so nothing could be checked.
        "no_lines"            -- paired with "none"; the description had no
                                 name-shaped line to even try (e.g. a one-line
                                 test ticket) -- there was nothing to confirm
                                 or refute in the first place.
        "no_match"            -- paired with "none"; a candidate name WAS
                                 found in the text, but it didn't match this
                                 requester_id via search_users. In this case
                                 `name` is still the attempted candidate (not
                                 the placeholder "there"), so the caller can
                                 show what was found even though it wasn't
                                 confirmed.
    The caller decides what to do with each tier (e.g. only using "confirmed"
    in the reply greeting, but still surfacing "unconfirmed"/"none" on the
    review page so a human can double-check it manually)."""
    candidates = set()
    for line in description.splitlines():
        line = line.strip().strip("-").strip()
        if not line or "@" in line or "unsubscribe" in line.lower():
            continue
        # A plausible person-name line: 2-4 title-case words, nothing else.
        if re.match(r"^([A-Z][a-zA-Z'.-]+)(\s+[A-Z][a-zA-Z'.-]+){1,3}$", line):
            candidates.add(line)

    if not candidates:
        return "there", "none", "no_lines"

    for name in candidates:
        try:
            matches = client.search_users(name)
        except Exception as exc:
            log_fn(f"search_users unavailable ({exc}); falling back without confirmation")
            return name, "unconfirmed", "search_unavailable"
        for user in matches:
            if user.get("id") == requester_id:
                return (user.get("name") or name), "confirmed", "matched"

    # Candidates existed but none matched this requester -- surface the
    # (unmatched) attempt rather than silently discarding it.
    attempted = sorted(candidates)[0]
    return attempted, "none", "no_match"


# resolve_requester_name and resolve_assignee_name (below) each used to make
# a live get_user() call on every single invocation, with no caching at all
# -- unlike resolve_own_agent_id/resolve_it_operations_agent_id just above,
# which already got this same treatment via _agent_id_cache once the
# identical problem was noticed there. In practice that meant: once per
# ticket in build_drafts() during every scan, once per matched ticket on
# every fetch_flagged_tickets()/fetch_recent_tickets()/
# fetch_recently_solved_tickets() cache refresh, AND once per OPEN TASK on
# literally every single /tasks page load (reconcile_personal_tasks() checks
# each open task's ticket status directly, but the /tasks page itself then
# calls resolve_requester_name for whichever of those same tickets appear
# elsewhere) -- with the same requester or assignee often showing up across
# several of those, each one paying its own fresh live lookup. This shared
# cache/helper gives get_user()-for-a-name the same treatment
# _agent_id_cache already gives get_user()-for-an-id.
USER_NAME_CACHE_TTL_SECONDS = AGENT_ID_CACHE_TTL_SECONDS  # same 120s cadence as the panel caches around it
_user_name_cache: dict = {}  # user_id -> {"name": str|None, "resolved_at": datetime}
# Same "lock only the dict access, never the network call" posture as
# _agent_id_cache_lock above, for the identical reason: review_server.py
# serves every request on its own thread, so two nearly-simultaneous
# lookups for the same user id are a real concurrent path, not just a
# theoretical one.
_user_name_cache_lock = threading.Lock()


def _cached_get_user_name(client, user_id, log_fn=print):
    """Shared get_user(user_id)->display name lookup, serving a cached name
    within USER_NAME_CACHE_TTL_SECONDS of the last successful lookup for
    that id, with zero Zendesk calls on a cache hit. A failed lookup (a
    404 for a deleted user, a transient API error) is NOT cached, so it
    gets retried on the very next call rather than being stuck returning
    "failed" for the rest of the window -- same posture as
    _resolve_agent_id_by_email above.

    Returns (name, ok). ok=False means the lookup itself failed -- callers
    need to tell that apart from "the lookup succeeded and this user
    genuinely has no name on file" to fall back correctly."""
    if not user_id:
        return None, True
    now = datetime.now()
    with _user_name_cache_lock:
        cached = _user_name_cache.get(user_id)
        if cached and (now - cached["resolved_at"]).total_seconds() < USER_NAME_CACHE_TTL_SECONDS:
            return cached["name"], True

    try:
        user = client.get_user(user_id)
    except Exception as exc:
        log_fn(f"get_user({user_id}) failed ({exc})")
        return None, False

    name = user.get("name")
    with _user_name_cache_lock:
        _user_name_cache[user_id] = {"name": name, "resolved_at": now}
    return name, True


def resolve_requester_name(client, requester_id, description: str, log_fn=print):
    """Resolve the requester's name the reliable way first: a direct
    get_user(requester_id) lookup (see _cached_get_user_name -- cached, so
    repeat lookups for the same requester across tickets/panels/scans
    within USER_NAME_CACHE_TTL_SECONDS cost nothing), which asks Zendesk for
    the exact user record behind this ticket's requester_id -- no
    text-parsing, no first-name collision risk, since we're matching on an
    exact numeric id rather than guessing from a name that might appear in
    the ticket body. This is why ticket #156312 (whose description was just
    "This is a test ticket." -- nothing name-shaped to parse) can still show
    a real name.

    Falls back to guess_requester_name()'s text-parsing heuristic only if the
    direct lookup itself fails (requester_id missing, the user record 404s,
    or the API call errors for any other reason) -- so a broken/missing
    lookup degrades gracefully instead of leaving the card with nothing.

    Returns (name, confidence, reason) -- same shape as guess_requester_name,
    plus one new reason value: "direct_lookup", paired with confidence
    "confirmed", for the normal/successful path."""
    if requester_id:
        name, ok = _cached_get_user_name(client, requester_id, log_fn=log_fn)
        if not ok:
            log_fn(f"resolve_requester_name({requester_id}): lookup failed; falling back to text-based guess")
        elif name:
            return name, "confirmed", "direct_lookup"

    return guess_requester_name(client, requester_id, description, log_fn=log_fn)


TEST_SUBJECT_TAGS = {"test", "tooling", "testing"}


def detect_test_ticket(subject: str):
    """Detect a bracketed subject prefix like '[Test]' or '[Tooling]' that
    signals a ticket exists to verify the pipeline itself (or some other
    internal tooling check) rather than a real request from someone needing
    help. Returns (is_test_ticket, tag) so the review page can badge these
    distinctly -- they don't need the same scrutiny as an external requester's
    issue, and shouldn't visually compete with tickets that do."""
    match = re.match(r"^\[([^\]]+)\]", subject or "")
    if not match:
        return False, ""
    tag = match.group(1).strip()
    if tag.lower() in TEST_SUBJECT_TAGS:
        return True, tag
    return False, ""


def build_description_excerpt(description: str) -> str:
    """Full original ticket text, shown as-is on the review page so nothing
    the requester wrote is ever silently cut off with "..." -- deciding
    whether a drafted reply is appropriate means seeing the whole request,
    not a preview of it. (Kept the name "excerpt" for the stored field --
    "description_excerpt" -- since that's the key already written into
    existing entries in pending_drafts.json; renaming it would just make
    already-pending tickets stop showing their text until re-posted.)

    Normalizes line endings and collapses long runs of blank lines (common
    in email-to-ticket descriptions with quoted signatures/disclaimers)
    down to a single blank line, but otherwise preserves the text and its
    paragraph breaks exactly as written -- the review page renders this
    with CSS white-space: pre-wrap so those breaks still show up."""
    text = (description or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def shorten_subject(subject: str) -> str:
    """Strip bracket tags, prefer a clean identifier (CVE number, etc.) over
    a restated description."""
    cve = re.search(r"CVE-\d{4}-\d+", subject)
    if cve:
        return cve.group(0)
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", subject).strip()
    return cleaned or subject


def resolve_assignee_name(client, assignee_id, log_fn=print):
    """Direct get_user(assignee_id) lookup for the "someone else is already
    working this" badge (see _cached_get_user_name -- cached, same as
    resolve_requester_name's use of it, so the same assignee showing up
    across several tickets doesn't pay a fresh live lookup each time) --
    mirrors resolve_requester_name's reliable path, but with no text-parsing
    fallback needed: assignee_id is an exact numeric id straight from the
    ticket (Zendesk's own "Assignee" field), so there's no
    first-name-collision risk to guard against the way there is for a name
    merely mentioned in ticket text.

    Returns the assignee's display name, or None on any failure (missing
    id, a 404, or an API error) -- callers fall back to showing the bare
    id in that case rather than leaving the badge out entirely."""
    if not assignee_id:
        return None
    name, ok = _cached_get_user_name(client, assignee_id, log_fn=log_fn)
    return name if ok else None


def resolve_own_agent_id(client, log_fn=print):
    """Resolves this tool's own Zendesk user id -- the account configured
    via ZENDESK_EMAIL -- so _has_public_reply_from_other (below) can
    recognize a reply posted by that account as a genuine agent reply even
    on a ticket where the requester happens to be the SAME account (e.g. a
    self-created test ticket, where you're both the requester and the one
    replying). Without this, "author_id != requester_id" is the only
    signal available, and it silently fails in exactly that self-
    referential case: our own reply looks identical to "the requester
    talking to themselves," so nothing ever recognizes the ticket as
    answered -- this was the root cause behind ticket #156314 getting
    re-flagged as a fresh candidate on every scan despite already having
    our reply on it.

    Returns None (not an exception) on any failure -- ZENDESK_EMAIL unset,
    search_users unreachable, or no matching user found -- so callers
    degrade to the plain author_id != requester_id check, which is exactly
    this tool's original (imperfect, but not new) behavior rather than a
    new failure mode. Resolved via the cached _resolve_agent_id_by_email
    lookup (see its docstring, above resolve_it_operations_agent_id) --
    this used to be a live search_users() call on every single
    invocation."""
    email = os.getenv("ZENDESK_EMAIL", "")
    if not email:
        return None
    return _resolve_agent_id_by_email(client, email, log_fn=log_fn)


def _first_public_reply_from_other(comments: list, requester_id, agent_user_id=None):
    """The single source of truth behind "has someone other than the
    requester already posted a public reply on this ticket" -- both
    _has_public_reply_from_other (the plain yes/no used at detection time)
    and check_already_replied (which also wants to name who, right before
    posting) go through this one predicate, rather than each independently
    re-deriving the same author_id comparison. That used to not be true:
    check_already_replied had its own inline copy of this exact condition,
    which only stayed correct by luck rather than by construction -- the
    kind of duplication this project has been burned by once already (see
    the launchd OAuth-path bug this README mentions).

    A public comment counts as an existing reply if it's from anyone other
    than the requester (the original signal), OR if it's from this tool's
    own configured agent account (agent_user_id, from
    resolve_own_agent_id) -- even when that account happens to equal
    requester_id. That second clause only ever matters for self-created
    test tickets where the same Zendesk account is both requester and
    replying agent; for every normal ticket (a real customer's account
    differs from the agent's) it changes nothing, since author_id !=
    requester_id already covers it.

    Returns the first matching comment dict, or None if there isn't one."""
    for c in comments:
        if c.get("public") and (
            c.get("author_id") != requester_id
            or (agent_user_id is not None and c.get("author_id") == agent_user_id)
        ):
            return c
    return None


def _has_public_reply_from_other(comments: list, requester_id, agent_user_id=None) -> bool:
    """Plain yes/no wrapper around _first_public_reply_from_other -- see
    that function's docstring for the full rationale. Used both at
    detection time (find_candidates, below) and again right before posting
    (check_already_replied, below), so the two checks can't quietly drift
    into checking slightly different things."""
    return _first_public_reply_from_other(comments, requester_id, agent_user_id=agent_user_id) is not None


def check_already_replied(client, ticket_id, requester_id, agent_user_id=None, log_fn=print) -> dict:
    """Re-checks a ticket's comments right before posting through it --
    guards against the exact race this tool's design invites: a candidate
    can sit in the pending queue indefinitely (there's no reject/dismiss
    action), and in the meantime someone else (another agent, or a
    teammate working the queue directly in Zendesk) may have already sent
    the actual first reply. Posting our drafted acknowledgment on top of
    that would be redundant at best and contradictory at worst.

    Returns {"already_replied": bool, "reason": str}. Fails OPEN (
    already_replied=False, with the reason explaining why it couldn't
    check) on any lookup error -- a transient API hiccup here shouldn't
    block every post; the worst case if it fails open is the same race
    this function exists to catch in the first place, not something new."""
    try:
        comments = client.get_ticket_comments(int(ticket_id))
    except Exception as exc:
        reason = f"could not verify -- comment lookup failed ({exc})"
        log_fn(f"check_already_replied(#{ticket_id}): {reason}")
        return {"already_replied": False, "reason": reason}

    match = _first_public_reply_from_other(comments, requester_id, agent_user_id=agent_user_id)
    if match:
        return {
            "already_replied": True,
            "reason": f"a public reply already exists on this ticket (from user id {match.get('author_id')})",
        }
    return {"already_replied": False, "reason": ""}


def find_candidates(client, log_fn=print, agent_user_id=None) -> list:
    # Ticket ids already tracked locally (pending, posted, dismissed, or
    # already_handled) are skipped outright, before even fetching their
    # comments. We already have ground truth for these from our own records
    # -- re-deriving "did we already reply" from live Zendesk comments via
    # _has_public_reply_from_other() is not just wasted work, it's actively
    # unreliable for them: that check only recognizes a reply as "already
    # handled" when it comes from a different author_id than the requester,
    # which breaks for any ticket where the requester and the replying agent
    # happen to share a Zendesk user id (e.g. self-created test tickets).
    # Without this, such a ticket gets re-flagged as a "candidate" on every
    # single scan forever, even though merge_candidates() correctly refuses
    # to re-add it -- producing a confusing "N candidate(s) found, 0 new"
    # with nothing to show for it.
    already_tracked = set(load_pending().keys())

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    candidates = []
    page = 1
    per_page = 25
    # Sorted by *updated_at* (not created_at) for the same reason
    # fetch_flagged_tickets() sorts that way instead of by created_at: a
    # ticket that's been sitting in some OTHER group for weeks, then gets
    # reassigned into IT-Operations today, is exactly as new to us as one
    # created five minutes ago -- but its created_at is still whatever it
    # was when Zendesk first made the ticket. Sorting/cutting off by
    # created_at would put that ticket wherever its original creation date
    # falls (almost certainly past the LOOKBACK_HOURS cutoff below), so the
    # scan would stop paging before ever reaching it, silently and
    # permanently excluding a ticket that genuinely needs a first reply.
    # updated_at gets bumped by the group change itself, so a just-
    # reassigned ticket sorts near the front like it should. For a ticket
    # that's genuinely just sitting untouched, updated_at and created_at
    # are the same value, so ordinary stale-ticket behavior (and the
    # LOOKBACK_HOURS margin sized for a closed-lid weekend, see above) is
    # unchanged.
    #
    # Stopped as soon as a ticket older than LOOKBACK_HOURS shows up, so in
    # ordinary use this never gets anywhere near Zendesk's own 1000-result
    # search cap the way an untargeted scan (fetch_flagged_tickets, which
    # has no time-window filter at all) can. Still worth a defensive
    # backstop rather than an unbounded `while True`, though, for the same
    # reason fetch_flagged_tickets has one: an IT-Operations group busy
    # enough to create/update over 1000 tickets within LOOKBACK_HOURS would
    # otherwise page straight into that cap and raise a 422 from inside the
    # try/except below anyway, just needlessly late.
    max_page = ZENDESK_SEARCH_RESULT_CAP // per_page
    while page <= max_page:
        try:
            result = client.get_tickets(
                group_id=IT_OPERATIONS_GROUP_ID,
                sort_by="updated_at",
                sort_order="desc",
                per_page=per_page,
                page=page,
            )
        except Exception as exc:
            # One page failing shouldn't discard every candidate already
            # found on the pages that DID work this run -- same fail-open
            # posture as a single ticket's comments failing to fetch below,
            # and the same one fetch_flagged_tickets uses for its own
            # per-page failures.
            log_fn(f"find_candidates(): page {page} failed ({exc}) -- "
                   f"stopping with {len(candidates)} candidate(s) already found this run.")
            break
        tickets = result.get("tickets", [])
        if not tickets:
            break

        stop_paging = False
        for ticket in tickets:
            updated = datetime.fromisoformat(ticket["updated_at"].replace("Z", "+00:00"))
            if updated < cutoff:
                stop_paging = True
                break
            if ticket.get("status") not in ("new", "open"):
                continue
            # Personal to-do tickets (see create_personal_task, below) are
            # self-requested and self-assigned, which would otherwise look
            # exactly like "a candidate with no reply yet" to the checks
            # below -- excluded by tag rather than by group, since this
            # tool doesn't control which group Zendesk files them under.
            if PERSONAL_TASK_TAG in (ticket.get("tags") or []):
                continue
            if str(ticket["id"]) in already_tracked:
                continue

            try:
                comments = client.get_ticket_comments(ticket["id"])
            except Exception as exc:
                # One ticket failing to fetch shouldn't discard every
                # candidate already found this run -- log it and move on.
                log_fn(f"Could not check comments for #{ticket['id']} ({exc}) -- skipping it this run.")
                continue

            if _has_public_reply_from_other(comments, ticket.get("requester_id"), agent_user_id=agent_user_id):
                continue

            candidates.append(ticket)

        if stop_paging or not result.get("has_more"):
            break
        page += 1
    else:
        # Loop condition (page <= max_page) went false rather than an
        # in-loop `break` -- every page we did fetch was still full of
        # tickets newer than the lookback cutoff, meaning this scan hit the
        # defensive cap without ever reaching the point it normally stops
        # at. Extremely unlikely (would need over max_page*per_page tickets
        # created in the group within LOOKBACK_HOURS) but worth a log line
        # rather than silently looking like a complete, cutoff-bounded scan.
        log_fn(f"find_candidates(): stopped at the {max_page}-page safety cap without reaching a ticket "
               f"older than the {LOOKBACK_HOURS}h lookback -- results may be incomplete this run.")

    return candidates


# Every draft opens with the same "Hey {name}!" line -- the requester's
# name called out directly is the whole point (that's what "tags" them into
# the reply, in the sense that matters here: it's addressed to them by name,
# not a generic "Hi there"), so unlike most of this file's other rotated
# templates (see e.g. the confirmation-toast wording elsewhere), there's
# nothing to rotate here -- varying the ONE line whose job is to name the
# requester would undercut that, not add variety worth having. Kept as its
# own function (rather than inlined into build_drafts) so its one job --
# "turn a display name into the fixed greeting line" -- stays independently
# testable and easy to find.
def _pick_greeting(display_name: str) -> str:
    return f"Hey {display_name}!"


def build_drafts(client, candidates: list, agent_user_id=None, log_fn=print) -> list:
    """Turn raw Zendesk ticket dicts (from find_candidates) into the
    drafted-reply dicts merge_candidates() expects."""
    drafted = []
    for ticket in candidates:
        description = ticket.get("description") or ""
        name, name_confidence, name_reason = resolve_requester_name(
            client, ticket.get("requester_id"), description, log_fn=log_fn
        )
        subject = shorten_subject(ticket["subject"])
        is_test_ticket, test_tag = detect_test_ticket(ticket["subject"])
        # "Someone else is already working this" badge: Zendesk's own
        # Assignee field, already included free-of-charge in every ticket
        # payload get_tickets/get_ticket return -- no extra API call needed
        # to detect it. Only counts as "other" when it's set to someone
        # other than this tool's own configured agent account (an
        # unassigned ticket, or one assigned to us, isn't "someone else").
        assignee_id = ticket.get("assignee_id")
        assigned_to_other = bool(assignee_id) and assignee_id != agent_user_id
        assignee_name = (
            resolve_assignee_name(client, assignee_id, log_fn=log_fn) if assigned_to_other else None
        )
        # Quoting the subject as a title ("...about “X”") rather than folding
        # it into the sentence ("...regarding X") avoids the grammatical
        # collision between the fixed template and a raw ticket title that
        # was never written to complete a sentence -- that mismatch was the
        # real source of the stiff, mismatched tone people were noticing.
        display_name = name if name_confidence == "confirmed" else "there"
        greeting = _pick_greeting(display_name)
        draft = (
            f"{greeting} Thanks for reaching out about “{subject}”. "
            f"We've received your request, and someone from our IT Operations "
            f"team will follow up with you shortly."
        )
        drafted.append(
            {
                "ticket_id": ticket["id"],
                "subject": ticket["subject"],
                "requester_id": ticket.get("requester_id"),
                "requester_name": name,
                "name_confidence": name_confidence,
                "name_confidence_reason": name_reason,
                "description_excerpt": build_description_excerpt(description),
                "is_test_ticket": is_test_ticket,
                "test_tag": test_tag,
                "assignee_id": assignee_id,
                "assignee_name": assignee_name,
                "assigned_to_other": assigned_to_other,
                # Already included free-of-charge in every ticket payload
                # get_tickets returns -- no extra API call needed, same as
                # assignee_id above -- just never surfaced on a card before.
                "priority": ticket.get("priority"),
                # This is Zendesk's own updated_at, not this tool's local
                # "first seen" created_at (set separately, in
                # merge_candidates) -- captured here as the initial value
                # for a brand-new candidate, then kept fresh
                # every scan by recheck_pending_for_assignment (which
                # already fetches this exact ticket via get_ticket for the
                # assignee badge, so refreshing this alongside it costs
                # nothing extra). Lets a ticket with genuine Zendesk-side
                # activity -- a note added, a field changed -- but no
                # reply yet, read differently from one that's been
                # completely silent since this tool first saw it.
                "updated_at": ticket.get("updated_at"),
                "draft": draft,
                "url": ticket_url(ticket["id"]),
            }
        )
    return drafted


def summary_counts() -> dict:
    """Quick status-bar numbers for the review page: how many are still
    waiting on you, and how many you already acted on today (posted or
    rejected), so opening the page after being away gives you situational
    awareness in one glance instead of counting cards."""
    data = _cached_load_pending()
    today = datetime.now().date().isoformat()
    counts = {
        "pending": 0,
        "posted_today": 0,
        "rejected_today": 0,
        "removed_today": 0,
        "already_handled_today": 0,
    }
    for v in data.values():
        status = v.get("status")
        if status == "pending":
            counts["pending"] += 1
        elif status == "posted" and (v.get("posted_at") or "").startswith(today):
            counts["posted_today"] += 1
        elif status == "rejected" and (v.get("rejected_at") or "").startswith(today):
            counts["rejected_today"] += 1
        elif status == "dismissed" and (v.get("dismissed_at") or "").startswith(today):
            counts["removed_today"] += 1
        elif status == "already_handled" and (v.get("resolved_at") or "").startswith(today):
            counts["already_handled_today"] += 1
    return counts


def recheck_pending_for_replies(client, log_fn=print, agent_user_id=None) -> list:
    """Catches the exact race this tool's design invites, proactively --
    not just at the moment you click Public Reply (see check_already_replied,
    used by review_server.py's _post() as a last-second safety net). A
    ticket can sit pending indefinitely (there's no time limit, only a
    human decision), and in that window someone else -- another agent, or a
    teammate working the queue directly in Zendesk -- may reply for real
    before you ever act on it here. Run on every scan (cron tick or manual
    refresh), this walks every currently-pending item and re-checks its
    comments directly, so the review page reflects reality without you
    needing to click anything first.

    Returns the list of pending entries that were just transitioned out of
    "pending" because of this, so the caller can log/notify with the actual
    ticket details -- not just a count."""
    newly_handled = []

    def _mutate(data):
        for ticket_id, entry in data.items():
            if entry.get("status") != "pending":
                continue
            result = check_already_replied(
                client, ticket_id, entry.get("requester_id"), agent_user_id=agent_user_id, log_fn=log_fn
            )
            if result["already_replied"]:
                entry["status"] = "already_handled"
                entry["resolved_at"] = datetime.now().isoformat(timespec="seconds")
                entry["resolution_note"] = result["reason"]
                newly_handled.append(dict(entry))

    update_pending(_mutate)
    return newly_handled


def recheck_pending_for_assignment(client, log_fn=print, agent_user_id=None) -> None:
    """Refreshes each pending ticket's "someone else is already working
    this" badge on every scan, the same way recheck_pending_for_replies
    refreshes reply state above -- a ticket can sit pending for a while,
    and whether a colleague has since picked it up (or dropped it) directly
    in Zendesk is exactly the kind of thing a stale card would miss.

    Also refreshes updated_at (see build_drafts' initial capture of it) off
    the exact same get_ticket call, at no extra cost -- so the ticket
    card's "updated Nh ago" text reflects genuine ongoing Zendesk-side
    activity (a note added, a field changed) for as long as the ticket
    sits pending, not just a one-time snapshot from whenever this tool
    first saw it.

    Purely cosmetic, unlike recheck_pending_for_replies: an assignee
    showing up doesn't change this ticket's pending status or pull it from
    the queue -- it's a heads-up for the human reviewing (so a duplicate
    reply isn't drafted on top of someone actively working it), not an
    automatic resolution the way an actual reply is. No return value --
    nothing here is worth a notification the way a newly-handled ticket is,
    this just keeps the badge (and updated_at) current for whenever the
    page is next loaded.

    Fails per-ticket, not per-scan: one ticket's get_ticket call erroring
    (a transient API hiccup, or the ticket having since been deleted)
    leaves that entry's existing assignee info (and updated_at) untouched
    and moves on, rather than discarding a refresh already in progress for
    every other pending ticket this scan."""
    def _mutate(data):
        for ticket_id, entry in data.items():
            if entry.get("status") != "pending":
                continue
            try:
                ticket = client.get_ticket(int(ticket_id))
            except Exception as exc:
                log_fn(f"recheck_pending_for_assignment(#{ticket_id}): get_ticket failed ({exc}) -- leaving assignee info as-is")
                continue
            assignee_id = ticket.get("assignee_id")
            assigned_to_other = bool(assignee_id) and assignee_id != agent_user_id
            entry["assignee_id"] = assignee_id
            entry["assigned_to_other"] = assigned_to_other
            entry["updated_at"] = ticket.get("updated_at")
            entry["assignee_name"] = (
                resolve_assignee_name(client, assignee_id, log_fn=log_fn) if assigned_to_other else None
            )

    update_pending(_mutate)


def scan_and_merge(client, log_fn=print):
    """One full detect-and-draft pass: find candidates, draft replies, merge
    into the pending store, re-check every already-pending ticket for
    someone else having replied first in the meantime, AND refresh whether
    each pending ticket now shows a Zendesk assignee other than us. Returns
    (candidates, new_items, handled_elsewhere) so the caller can
    log/notify/summarize however fits its own context -- this is the single
    shared implementation used by both the 2-minute cron tick and the
    review page's manual Refresh button.

    Resolves this tool's own agent user id once per scan (not once per
    ticket) and threads it through to find_candidates,
    recheck_pending_for_replies, AND recheck_pending_for_assignment, so a
    self-created test ticket's own reply is correctly recognized as
    "already answered" -- see resolve_own_agent_id's docstring for why
    that's not already implied by author_id != requester_id alone -- and so
    a ticket assigned to this tool's own account never mislabels itself as
    "assigned to someone else". Also archives resolved entries older than
    ARCHIVE_AFTER_DAYS out of pending_drafts.json on the way out (see
    archive_old_pending) -- piggybacking on the same cadence as everything
    else here rather than needing its own separate schedule."""
    agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
    candidates = find_candidates(client, log_fn=log_fn, agent_user_id=agent_user_id)
    new_items = []
    if candidates:
        drafted = build_drafts(client, candidates, agent_user_id=agent_user_id, log_fn=log_fn)
        new_items = merge_candidates(drafted)
    handled_elsewhere = recheck_pending_for_replies(client, log_fn=log_fn, agent_user_id=agent_user_id)
    recheck_pending_for_assignment(client, log_fn=log_fn, agent_user_id=agent_user_id)
    archive_old_pending(log_fn=log_fn)
    mark_scan_completed()
    return candidates, new_items, handled_elsewhere


def recent_replied_items(limit: "int | None" = 10) -> list:
    """The N most recently posted tickets (public reply or internal
    comment) through this tool, regardless of which calendar day they went
    out -- a longer-running, day-independent complement to
    posted_today_items() above (which resets every midnight and only
    covers today). Same 'posted' scope -- excludes dismissed and
    already-handled-elsewhere entries, since those aren't replies you
    actually sent -- and same most-recent-first order.

    limit=None returns every locally-tracked posted item, uncapped -- used
    by fetch_recently_worked_on (below) to merge this tool's own local
    history against a live Zendesk scan before applying the real display
    limit to the combined list, rather than to this function's local-only
    slice on its own."""
    data = _cached_load_pending()
    items = [v for v in data.values() if v.get("status") == "posted"]
    items.sort(key=lambda i: i.get("posted_at", ""), reverse=True)
    return items if limit is None else items[:limit]


# A get_ticket_comments() call is one extra Zendesk request per candidate
# ticket -- WORKED_ON_COMMENT_CHECK_LIMIT caps how many NOT-already-
# locally-known tickets get that extra check on a single live scan (see
# fetch_recently_worked_on), so a quiet stretch with dozens of recently-
# updated-for-other-reasons tickets in the group can't turn one page load
# into dozens of sequential API calls. WORKED_ON_SCAN_PAGES/
# WORKED_ON_TICKET_PER_PAGE bound the cheaper get_tickets() paging itself,
# same shape (and same reason -- get_tickets() can only filter by a single
# group_id) as FLAGGED_TICKET_SCAN_PAGES/SOLVED_TICKET_SCAN_PAGES above.
WORKED_ON_SCAN_PAGES = int(os.getenv("WORKED_ON_SCAN_PAGES", "10"))
WORKED_ON_TICKET_PER_PAGE = 100
WORKED_ON_COMMENT_CHECK_LIMIT = int(os.getenv("WORKED_ON_COMMENT_CHECK_LIMIT", "40"))
RECENTLY_WORKED_ON_LIMIT = 30  # matches the sidebar's own display limit -- a
# named constant rather than a bare literal so fetch_recently_worked_on and
# _render_worked_on_sidebar can't quietly drift apart the way the sidebar's
# limit and its seed data already once did (see run_server_worked_on.py).
_worked_on_cache: dict = {}
# Literal 120 rather than referencing SECURITY_CACHE_TTL_SECONDS directly --
# that constant is defined further down this file (in the IT-Security
# section, below), after this block, so referencing it here would be a
# forward reference that fails at import time. Same value, same reasoning:
# the same 2-minute cadence as every other read-only Zendesk-backed panel.
WORKED_ON_CACHE_TTL_SECONDS = 120
_worked_on_cache_lock = threading.Lock()


def _zendesk_ts_to_local_iso(ts: str) -> str:
    """Normalizes a Zendesk-sourced UTC timestamp to a naive LOCAL-time
    isoformat string at seconds precision -- exactly the format
    update_pending's own posted_at already uses
    (datetime.now().isoformat(timespec='seconds')) -- so a discovered
    Zendesk item and a locally-posted one sort and render identically once
    merged in fetch_recently_worked_on. Handles both shapes this codebase's
    two Zendesk timestamp sources actually produce: a raw ticket-level JSON
    string ending in 'Z' (as elsewhere in this file), and
    get_ticket_comments()'s own str(datetime) rendering of a comment's
    created_at ('2026-08-20 15:57:50+00:00' -- no 'Z', but still a valid
    fromisoformat input once present). Returns '' on anything unparseable
    rather than raising, the same fail-quiet posture _updated_age/
    _solved_age (in review_server.py) already use for the same reason."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return ""


def _most_recent_own_comment(comments: list, agent_user_id):
    """The latest comment (public OR internal) authored by this tool's own
    configured Zendesk account, or None. Deliberately not restricted to
    public-only -- an internal-note-only reply sent directly in Zendesk is
    still work done on the ticket, and excluding it here would make this
    look like a public-replies-only feature when nothing else on this
    panel makes that distinction either (recent_replied_items shows both
    kinds already)."""
    own = [c for c in comments if c.get("author_id") == agent_user_id]
    if not own:
        return None
    own.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return own[0]


def fetch_recently_worked_on(client, limit: int = RECENTLY_WORKED_ON_LIMIT, log_fn=print, force: bool = False) -> dict:
    """Returns {"items": [...], "capped": bool, "zendesk_unavailable": bool}
    -- the union of recent_replied_items() (replies sent THROUGH this tool)
    and a live scan of the IT-Operations group for tickets NOT already in
    that local set that carry a comment authored by this tool's own
    configured Zendesk account -- i.e. a reply sent directly in Zendesk,
    bypassing this tool entirely, that pending_drafts.json would otherwise
    have no record of at all. Merged and sorted most-recent-first, capped
    at `limit`.

    This is the one place on the main page ("/") that now touches Zendesk
    at all -- see the "GET / never touches Zendesk" design principle in
    the README, which this function deliberately narrows rather than
    removes: the local half of the result (recent_replied_items) is always
    computed first and returned on its own the moment anything about the
    live half goes wrong, so "/" still never actually BREAKS over Zendesk
    being unreachable, it just quietly stops finding outside-the-tool
    replies until the connection's back -- zendesk_unavailable=True is how
    the caller knows to say so.

    capped=True means the live scan hit its own page-depth
    (WORKED_ON_SCAN_PAGES) or per-scan comment-check
    (WORKED_ON_COMMENT_CHECK_LIMIT) budget before covering every recently-
    updated ticket in the group -- there could be an outside-the-tool reply
    further back this pass didn't reach yet.

    Known simplification: a ticket already tracked locally (you replied to
    it at least once through this tool) is never re-checked against
    Zendesk, so a LATER reply sent directly in Zendesk on that same ticket
    won't bump its position or move its timestamp forward here -- it'll
    still show at the tool-recorded time of your last reply through this
    tool specifically. Scoped to IT-Operations only, the same restriction
    fetch_flagged_tickets/fetch_recently_solved_tickets already live with
    and for the identical reason (get_tickets() takes one group_id or
    none)."""
    global _worked_on_cache
    now = datetime.now()
    with _worked_on_cache_lock:
        cached = _worked_on_cache or None
        if (
            not force and cached and cached.get("limit") == limit
            and (now - cached["fetched_at"]).total_seconds() < WORKED_ON_CACHE_TTL_SECONDS
        ):
            return {"items": cached["items"], "capped": cached["capped"], "zendesk_unavailable": cached["zendesk_unavailable"]}

    local_items = recent_replied_items(limit=None)
    local_ids = {str(i.get("ticket_id")) for i in local_items}

    if client.client is None:
        items = local_items[:limit]
        with _worked_on_cache_lock:
            _worked_on_cache = {"items": items, "fetched_at": now, "capped": False, "zendesk_unavailable": True, "limit": limit}
        return {"items": items, "capped": False, "zendesk_unavailable": True}

    try:
        agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
        if agent_user_id is None:
            raise RuntimeError(
                "could not resolve this tool's own Zendesk account (ZENDESK_EMAIL unset, or search_users unreachable)"
            )

        discovered = []
        capped = False
        checked = 0
        page = 1
        max_page = min(WORKED_ON_SCAN_PAGES, ZENDESK_SEARCH_RESULT_CAP // WORKED_ON_TICKET_PER_PAGE)
        while page <= max_page:
            try:
                result = client.get_tickets(
                    group_id=IT_OPERATIONS_GROUP_ID,
                    sort_by="updated_at",
                    sort_order="desc",
                    per_page=WORKED_ON_TICKET_PER_PAGE,
                    page=page,
                )
            except Exception as exc:
                log_fn(f"fetch_recently_worked_on() page {page} failed ({exc}) -- "
                       f"stopping with {len(discovered)} outside-the-tool repl(y/ies) already found")
                capped = True
                break
            tickets = result.get("tickets", [])
            if not tickets:
                break

            hit_check_limit = False
            for ticket in tickets:
                tid = str(ticket.get("id"))
                if tid in local_ids:
                    continue
                if checked >= WORKED_ON_COMMENT_CHECK_LIMIT:
                    hit_check_limit = True
                    break
                checked += 1
                try:
                    comments = client.get_ticket_comments(ticket.get("id"))
                except Exception as exc:
                    log_fn(f"fetch_recently_worked_on(): get_ticket_comments(#{tid}) failed ({exc}), skipping")
                    continue
                own_comment = _most_recent_own_comment(comments, agent_user_id)
                if not own_comment:
                    continue
                name, name_confidence, _ = resolve_requester_name(
                    client, ticket.get("requester_id"), ticket.get("description") or "", log_fn=log_fn
                )
                discovered.append(
                    {
                        "ticket_id": ticket.get("id"),
                        "subject": ticket.get("subject", ""),
                        "url": ticket_url(ticket.get("id")),
                        # Raw name + a separate name_confidence field, NOT
                        # the None-if-unconfirmed shape fetch_recent_tickets/
                        # fetch_flagged_tickets/fetch_recently_solved_tickets
                        # use above -- this item flows into
                        # _render_worked_on_sidebar's _requester_avatar_prefix,
                        # which (like build_drafts' own local "posted"
                        # entries) expects to do its own confirmed-only gate
                        # off name_confidence, not a pre-filtered value.
                        "requester_name": name,
                        "name_confidence": name_confidence,
                        "posted_at": _zendesk_ts_to_local_iso(own_comment.get("created_at", "")),
                        "posted_public": bool(own_comment.get("public")),
                    }
                )

            if hit_check_limit:
                capped = True
                break
            if not result.get("has_more"):
                break
            page += 1
        else:
            capped = True

        merged = local_items + discovered
        merged.sort(key=lambda i: i.get("posted_at", ""), reverse=True)
        items = merged[:limit]
        with _worked_on_cache_lock:
            _worked_on_cache = {"items": items, "fetched_at": now, "capped": capped, "zendesk_unavailable": False, "limit": limit}
        return {"items": items, "capped": capped, "zendesk_unavailable": False}
    except Exception as exc:
        log_fn(f"fetch_recently_worked_on() failed ({exc})")
        if cached:
            return {"items": cached["items"], "capped": cached["capped"], "zendesk_unavailable": cached["zendesk_unavailable"]}
        return {"items": local_items[:limit], "capped": False, "zendesk_unavailable": True}


# --- Previous-workday activity recap (/standup/yesterday) ------------------
# "What did I do on <day>?" for standup: every public reply and internal
# note this tool's own Zendesk account left on a given local calendar day,
# plus tickets assigned to it that sit solved with their last update on that
# day. Distinct from fetch_recently_worked_on above (last-N, no day window,
# one row per ticket): this one is bounded by a DAY, lists every event, and
# pages back only as far as that day requires.
ACTIVITY_SCAN_PAGES = int(os.getenv("ACTIVITY_SCAN_PAGES", "10"))
ACTIVITY_TICKET_PER_PAGE = 100
ACTIVITY_COMMENT_CHECK_LIMIT = int(os.getenv("ACTIVITY_COMMENT_CHECK_LIMIT", "150"))
ACTIVITY_CACHE_TTL_SECONDS = 120
_activity_cache: dict = {}
_activity_cache_lock = threading.Lock()


def previous_workday(today=None):
    """The most recent BUSINESS_WEEKDAYS date strictly before `today` (a
    date, default: today's local date) -- Monday gives the prior Friday,
    Tuesday through Friday give the day before."""
    day = today or datetime.now().date()
    day = day - timedelta(days=1)
    while day.weekday() not in BUSINESS_WEEKDAYS:
        day -= timedelta(days=1)
    return day


def adjacent_workday(day, step: int):
    """The business day `step` (+1 or -1) away from `day`."""
    delta = timedelta(days=1 if step > 0 else -1)
    day = day + delta
    while day.weekday() not in BUSINESS_WEEKDAYS:
        day += delta
    return day


def _activity_events_from_local(day_iso: str) -> dict:
    """{ticket_id: item} built from replies sent THROUGH this tool on
    `day_iso` (pending_drafts.json "posted" entries) -- the offline
    fallback, and a supplement for anything the live scan missed."""
    out = {}
    for it in recent_replied_items(limit=None):
        at = str(it.get("posted_at", ""))
        if at[:10] != day_iso:
            continue
        tid = str(it.get("ticket_id"))
        entry = out.setdefault(tid, {
            "ticket_id": it.get("ticket_id"),
            "subject": it.get("subject", ""),
            "url": it.get("url") or ticket_url(it.get("ticket_id")),
            "requester_name": it.get("requester_name") if it.get("name_confidence") == "confirmed" else None,
            "events": [],
        })
        entry["events"].append({"at": at, "kind": "public" if it.get("posted_public") else "internal"})
    return out


def _finish_activity_items(by_ticket: dict) -> list:
    items = []
    for entry in by_ticket.values():
        entry["events"].sort(key=lambda e: e["at"])
        entry["first_at"] = entry["events"][0]["at"]
        items.append(entry)
    items.sort(key=lambda i: i["first_at"])
    return items


def fetch_activity_for_day(client, day, log_fn=print, force: bool = False) -> dict:
    """Returns {"items": [...], "capped": bool, "zendesk_unavailable": bool}
    for the local calendar `day` (a date). Each item is one ticket with
    "events": [{"at": local-iso, "kind": "public"|"internal"|"solved"}],
    items ordered by each ticket's first event that day.

    Live path: pages IT-Operations tickets by updated_at desc and stops at
    the first ticket last updated BEFORE `day` (nothing older can carry
    activity on it). Every ticket updated on/after `day` has its comments
    checked for ones authored by this tool's own account dated `day`;
    a ticket assigned to that account with status solved and updated_at on
    `day` additionally gets a "solved" event (updated_at stands in for the
    solve time -- the same proxy fetch_recently_solved_tickets uses, so a
    ticket touched again after being solved will not show as solved on
    `day`). capped=True means the page or comment-check budget ran out
    before the scan reached the day boundary, so older-in-scan activity may
    be missing. Scoped to IT-Operations like the other group-bound scans.
    Offline (or on failure) it falls back to replies sent through this
    tool, flagged zendesk_unavailable=True."""
    global _activity_cache
    now = datetime.now()
    day_iso = day.isoformat()
    with _activity_cache_lock:
        cached = _activity_cache.get(day_iso)
        if (not force and cached and (now - cached["fetched_at"]).total_seconds() < ACTIVITY_CACHE_TTL_SECONDS):
            return {k: cached[k] for k in ("items", "capped", "zendesk_unavailable")}

    def _store(result):
        with _activity_cache_lock:
            _activity_cache[day_iso] = {**result, "fetched_at": now}
        return result

    local = _activity_events_from_local(day_iso)
    if client.client is None:
        return _store({"items": _finish_activity_items(local), "capped": False, "zendesk_unavailable": True})

    try:
        agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
        if agent_user_id is None:
            raise RuntimeError("could not resolve this tool's own Zendesk account")

        found = {}
        capped = False
        checked = 0
        page = 1
        reached_boundary = False
        max_page = min(ACTIVITY_SCAN_PAGES, ZENDESK_SEARCH_RESULT_CAP // ACTIVITY_TICKET_PER_PAGE)
        while page <= max_page and not reached_boundary:
            try:
                result = client.get_tickets(
                    group_id=IT_OPERATIONS_GROUP_ID, sort_by="updated_at", sort_order="desc",
                    per_page=ACTIVITY_TICKET_PER_PAGE, page=page,
                )
            except Exception as exc:
                log_fn(f"fetch_activity_for_day() page {page} failed ({exc}) -- stopping with {len(found)} ticket(s) found")
                capped = True
                break
            tickets = result.get("tickets", [])
            if not tickets:
                break
            stop_budget = False
            for ticket in tickets:
                updated_local = _zendesk_ts_to_local_iso(ticket.get("updated_at", ""))
                if updated_local and updated_local[:10] < day_iso:
                    reached_boundary = True
                    break
                if checked >= ACTIVITY_COMMENT_CHECK_LIMIT:
                    stop_budget = True
                    break
                checked += 1
                tid = ticket.get("id")
                events = []
                try:
                    comments = client.get_ticket_comments(tid)
                except Exception as exc:
                    log_fn(f"fetch_activity_for_day(): get_ticket_comments(#{tid}) failed ({exc}), skipping")
                    comments = []
                for c in comments:
                    if c.get("author_id") != agent_user_id:
                        continue
                    at = _zendesk_ts_to_local_iso(c.get("created_at", ""))
                    if at and at[:10] == day_iso:
                        events.append({"at": at, "kind": "public" if c.get("public") else "internal"})
                if (
                    ticket.get("status") == "solved" and ticket.get("assignee_id") == agent_user_id
                    and updated_local and updated_local[:10] == day_iso
                ):
                    events.append({"at": updated_local, "kind": "solved"})
                if not events:
                    continue
                name, name_confidence, _ = resolve_requester_name(
                    client, ticket.get("requester_id"), ticket.get("description") or "", log_fn=log_fn
                )
                found[str(tid)] = {
                    "ticket_id": tid,
                    "subject": ticket.get("subject", ""),
                    "url": ticket_url(tid),
                    "requester_name": name if name_confidence == "confirmed" else None,
                    "events": events,
                }
            if stop_budget:
                capped = True
                break
            if not result.get("has_more"):
                reached_boundary = True
            page += 1
        if not reached_boundary and not capped:
            capped = True

        # Replies sent through this tool for tickets the live scan did not
        # surface (outside the scanned group/window) still belong on the list.
        for tid, entry in local.items():
            if tid not in found:
                found[tid] = entry
        return _store({"items": _finish_activity_items(found), "capped": capped, "zendesk_unavailable": False})
    except Exception as exc:
        log_fn(f"fetch_activity_for_day() failed ({exc})")
        if cached:
            return {k: cached[k] for k in ("items", "capped", "zendesk_unavailable")}
        return {"items": _finish_activity_items(local), "capped": False, "zendesk_unavailable": True}


# --- IT-Security "recent activity" column -----------------------------
# A read-only companion panel, distinct from the IT-Operations working
# queue above: no candidate filtering, no already-replied check, no
# drafting, no persistence to pending_drafts.json. Just the N most
# recently created tickets in a group, however Zendesk has them right now
# (any status), enriched with resolved requester/assignee names so the
# review page can show something more useful than raw ids.

_security_ticket_cache: dict = {}
SECURITY_CACHE_TTL_SECONDS = 120  # matches the cron scan's own cadence --
# no point re-fetching more often than the underlying data actually
# changes upstream at that rate; a cache hit costs zero Zendesk API calls,
# which matters here because unlike the rest of this page, GET / previously
# never touched Zendesk at all just to render.
# Guards the dict read and the dict write only, same reasoning as
# _agent_id_cache_lock just above -- never held across the live get_tickets()
# call itself (or the per-ticket resolve_requester_name/resolve_assignee_name
# calls below it), so a slow Zendesk response never blocks an unrelated
# group_id's own cache read. The per-key assignment below was already a
# single atomic step even without this lock, so what this actually buys is
# consistency with the other caches plus a narrower (not fully eliminated)
# window for two threads to both see a stale/missing entry and both fetch.
_security_ticket_cache_lock = threading.Lock()


def fetch_recent_tickets(client, group_id, limit: int = 5, log_fn=print, force: bool = False) -> dict:
    """Returns {"items": [...], "error": str|None}. "items" entries have:
    ticket_id, subject, status, url, requester_name (or None if
    unconfirmed), requester_id, assignee_id, assignee_name (or None if
    unassigned/unresolved), already_ours (bool -- assignee_id matches this
    tool's own configured account).

    A dict rather than a bare list so the caller can distinguish
    "genuinely zero tickets" from "couldn't fetch, here's what we last
    had (or nothing at all)" -- collapsing those into an empty list either
    way would make a transient API hiccup look identical to "the queue is
    actually empty," and the review page shouldn't claim that.

    Serves a cached result within SECURITY_CACHE_TTL_SECONDS of the last
    successful fetch, with no client/network interaction at all on a cache
    hit. On a cache miss with auth broken, or a live fetch that errors,
    falls back to the last successful result if one exists (better than
    blanking an already-fetched, still-probably-accurate list just because
    a refresh happened to land at a bad moment) -- consistent with this
    tool's fail-open posture elsewhere (a failed post leaves the draft
    intact, a failed reply-check fails open rather than blocking a post)."""
    now = datetime.now()
    with _security_ticket_cache_lock:
        cached = _security_ticket_cache.get(group_id)
        if not force and cached and (now - cached["fetched_at"]).total_seconds() < SECURITY_CACHE_TTL_SECONDS:
            return {"items": cached["items"], "error": None}

    if client.client is None:
        if cached:
            return {"items": cached["items"], "error": None}
        return {"items": [], "error": "Zendesk isn't connected right now -- see the reconnect banner above."}

    try:
        agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
        result = client.get_tickets(
            group_id=group_id, sort_by="created_at", sort_order="desc", per_page=limit, page=1
        )
        tickets = (result.get("tickets") or [])[:limit]
        items = []
        for ticket in tickets:
            name, name_confidence, _ = resolve_requester_name(
                client, ticket.get("requester_id"), ticket.get("description") or "", log_fn=log_fn
            )
            assignee_id = ticket.get("assignee_id")
            already_ours = bool(assignee_id) and agent_user_id is not None and assignee_id == agent_user_id
            assignee_name = resolve_assignee_name(client, assignee_id, log_fn=log_fn) if assignee_id else None
            items.append(
                {
                    "ticket_id": ticket.get("id"),
                    "subject": ticket.get("subject", ""),
                    "status": ticket.get("status", ""),
                    "url": ticket_url(ticket.get("id")),
                    "requester_name": name if name_confidence == "confirmed" else None,
                    "requester_id": ticket.get("requester_id"),
                    "assignee_id": assignee_id,
                    "assignee_name": assignee_name,
                    "already_ours": already_ours,
                }
            )
        with _security_ticket_cache_lock:
            _security_ticket_cache[group_id] = {"items": items, "fetched_at": now}
        return {"items": items, "error": None}
    except Exception as exc:
        log_fn(f"fetch_recent_tickets(group={group_id}) failed ({exc})")
        if cached:
            return {"items": cached["items"], "error": None}
        return {"items": [], "error": f"Could not load recent tickets ({exc})."}


def cached_security_ticket_count(group_id) -> int:
    """Best-effort item count from whatever's already sitting in
    fetch_recent_tickets' own cache for this group, WITHOUT triggering a
    fetch of its own -- unlike that function, this never calls Zendesk (no
    client parameter, even). Returns 0 on a cold/expired cache, same as
    "genuinely nothing to badge" -- there's no separate "unknown" state
    worth distinguishing here the way fetch_recent_tickets' own {"error":
    ...} does, since a header badge that's merely absent reads fine either
    way (compare cached_flagged_ticket_count, which does the same
    "unavailable means no badge" collapse for the Discussion nav badge).

    This exists for review_server._top_nav_html's IT-Security nav badge,
    which renders on EVERY page via _page() -- calling fetch_recent_tickets
    itself from there would mean a page with nothing to do with
    IT-Security (say, /tasks) could still trigger a live Zendesk call (plus
    per-ticket requester/assignee name resolution) just because this
    group's 120-second cache happened to expire. Reading the cache instead
    keeps the badge free everywhere except the one real cost site
    (visiting /security itself, which already pays for the fetch it needs
    regardless): the badge shows the last real count for as long as that
    visit's cache stays warm, and quietly disappears once it goes stale,
    rather than forcing a fresh scan just to answer a header's badge."""
    with _security_ticket_cache_lock:
        cached = _security_ticket_cache.get(group_id)
        if not cached or (datetime.now() - cached["fetched_at"]).total_seconds() >= SECURITY_CACHE_TTL_SECONDS:
            return 0
        return len(cached["items"])


# "block"/"standup" tags mean something different from "needs a first
# reply": a ticket can carry either tag regardless of its status, its
# assignee, or whether this tool (or anyone) has already replied to it --
# none of which find_candidates()/the pending queue would surface, since
# that queue deliberately excludes anything already replied to. This is a
# second, independent read-only view over the same IT_OPERATIONS_GROUP_ID
# group, the same "no drafting, no persistence" posture as
# fetch_recent_tickets just above, just scoped by tag instead of by group
# and "most recent."
FLAGGED_TICKET_TAGS = ("block", "standup")
# ZendeskClient.get_tickets() has no "any ticket with this tag" filter --
# it only builds a plain "type:ticket group_id:N" search query -- so this
# has to page through the group's own tickets and check each one's own
# "tags" list in Python (already included free in every ticket payload).
FLAGGED_TICKET_PER_PAGE = 100
# Zendesk's own /search.json endpoint (which get_tickets(group_id=...) uses
# internally) hard-caps total results at 1000 regardless of page size --
# requesting a page whose offset would exceed that comes back as a real
# error (a 422 Unprocessable Entity, not an empty "no more results" page),
# discovered the hard way against the real IT-Operations queue, which
# apparently has 1000+ tickets matching "type:ticket group_id:N". This is a
# hard ceiling on the *Search* API specifically -- there's no cursor-based
# workaround for it the way Zendesk's incremental-export endpoints have --
# so FLAGGED_TICKET_SCAN_PAGES below is clamped to never even request a
# page past it, rather than relying on catching the resulting error.
ZENDESK_SEARCH_RESULT_CAP = 1000
# A further, independently configurable safety cap on top of the hard
# ceiling above (not a way to exceed it -- see the clamp in
# fetch_flagged_tickets) against turning a cache-miss refresh into an
# unnecessarily long scan for a smaller queue; see the "capped" field this
# returns for when either cap was actually hit.
FLAGGED_TICKET_SCAN_PAGES = int(os.getenv("FLAGGED_TICKET_SCAN_PAGES", "10"))
_flagged_ticket_cache: dict = {}
FLAGGED_CACHE_TTL_SECONDS = SECURITY_CACHE_TTL_SECONDS  # same cadence as the other read-only Zendesk-derived panel
# Same reasoning as _security_ticket_cache_lock -- guards the dict read and
# the dict replacement, never the multi-page get_tickets() scan itself. This
# one matters slightly more than the other two: the old refresh code did
# `_flagged_ticket_cache.clear()` followed by a separate `.update(...)` call,
# two visible steps rather than one -- a concurrent reader landing in that
# gap would see a genuinely empty dict and report "cold cache" even though a
# refresh was mid-flight and about to repopulate it a moment later (a header
# badge flickering to "no badge" mid-refresh, not just a wasted duplicate
# fetch). Replacing the whole cache with one `global`-reassignment under this
# lock, instead of clear()-then-update(), closes that window: any reader
# either sees the complete old cache or the complete new one, never a gap
# between them.
_flagged_ticket_cache_lock = threading.Lock()


def fetch_flagged_tickets(client, log_fn=print, force: bool = False) -> dict:
    """Returns {"items": [...], "error": str|None, "capped": bool} -- every
    non-terminal (not solved/closed) IT-Operations ticket tagged "block" or
    "standup" (FLAGGED_TICKET_TAGS), regardless of who it's assigned to.
    Each item has the same shape fetch_recent_tickets' items do, plus
    "matched_tags": a sorted list of which of FLAGGED_TICKET_TAGS this
    ticket actually carries (usually one, occasionally both).

    Sorted by *updated_at* rather than created_at (unlike find_candidates()
    and fetch_recent_tickets above) specifically so a ticket that's old but
    was just tagged today still sorts near the front of the scan instead of
    being buried under its original creation date -- created_at ordering
    would make an old ticket that just got flagged effectively invisible
    until the scan depth was raised.

    A single page failing partway through (Zendesk's own 1000-result search
    cap, or a transient API hiccup) stops the scan right there and returns
    whatever was already found from the pages that DID work, marked
    "capped": True -- it does NOT discard the whole result the way letting
    that error propagate up would. Cached the same way fetch_recent_tickets()
    is otherwise (FLAGGED_CACHE_TTL_SECONDS, falling back to the last
    successful result on a total failure rather than blanking an
    already-populated list)."""
    global _flagged_ticket_cache
    now = datetime.now()
    with _flagged_ticket_cache_lock:
        cached = _flagged_ticket_cache or None
        if not force and cached and (now - cached["fetched_at"]).total_seconds() < FLAGGED_CACHE_TTL_SECONDS:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}

    if client.client is None:
        if cached:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}
        return {"items": [], "error": "Zendesk isn't connected right now -- see the reconnect banner above.", "capped": False}

    try:
        agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
        tag_set = set(FLAGGED_TICKET_TAGS)
        items = []
        capped = False
        page = 1
        # Never actually requests a page whose offset would exceed Zendesk's
        # own ZENDESK_SEARCH_RESULT_CAP -- FLAGGED_TICKET_SCAN_PAGES can only
        # narrow the scan further than that, never widen it past the hard
        # ceiling.
        max_page = min(FLAGGED_TICKET_SCAN_PAGES, ZENDESK_SEARCH_RESULT_CAP // FLAGGED_TICKET_PER_PAGE)
        while page <= max_page:
            try:
                result = client.get_tickets(
                    group_id=IT_OPERATIONS_GROUP_ID,
                    sort_by="updated_at",
                    sort_order="desc",
                    per_page=FLAGGED_TICKET_PER_PAGE,
                    page=page,
                )
            except Exception as exc:
                # One page failing shouldn't discard everything already
                # gathered from the pages that DID work -- stop scanning
                # here and surface what we have, the same fail-open posture
                # find_candidates() uses for a single ticket's comments
                # failing to fetch (skip it, keep going with the rest).
                log_fn(f"fetch_flagged_tickets() page {page} failed ({exc}) -- "
                       f"stopping with {len(items)} item(s) already found")
                capped = True
                break
            tickets = result.get("tickets", [])
            if not tickets:
                break
            for ticket in tickets:
                if ticket.get("status") in ("solved", "closed"):
                    continue
                matched = sorted(tag_set & set(ticket.get("tags") or []))
                if not matched:
                    continue
                name, name_confidence, _ = resolve_requester_name(
                    client, ticket.get("requester_id"), ticket.get("description") or "", log_fn=log_fn
                )
                assignee_id = ticket.get("assignee_id")
                already_ours = bool(assignee_id) and agent_user_id is not None and assignee_id == agent_user_id
                assignee_name = resolve_assignee_name(client, assignee_id, log_fn=log_fn) if assignee_id else None
                items.append(
                    {
                        "ticket_id": ticket.get("id"),
                        "subject": ticket.get("subject", ""),
                        "status": ticket.get("status", ""),
                        "url": ticket_url(ticket.get("id")),
                        "requester_name": name if name_confidence == "confirmed" else None,
                        "requester_id": ticket.get("requester_id"),
                        "assignee_id": assignee_id,
                        "assignee_name": assignee_name,
                        "already_ours": already_ours,
                        "matched_tags": matched,
                    }
                )
            if not result.get("has_more"):
                break
            page += 1
        else:
            # Loop condition (page <= max_page) went false rather than an
            # in-loop `break` -- meaning Zendesk still had at least one
            # more page we didn't fetch (either FLAGGED_TICKET_SCAN_PAGES
            # or the hard ZENDESK_SEARCH_RESULT_CAP stopped us first).
            capped = True

        with _flagged_ticket_cache_lock:
            _flagged_ticket_cache = {"items": items, "fetched_at": now, "capped": capped}
        return {"items": items, "error": None, "capped": capped}
    except Exception as exc:
        log_fn(f"fetch_flagged_tickets() failed ({exc})")
        if cached:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}
        return {"items": [], "error": f"Could not load flagged tickets ({exc}).", "capped": False}


def cached_flagged_ticket_count() -> int:
    """Best-effort item count from whatever's already sitting in
    fetch_flagged_tickets' own cache, WITHOUT triggering a fetch of its
    own -- see cached_security_ticket_count just above fetch_recent_tickets
    for the identical reasoning, doubly so here: fetch_flagged_tickets can
    page through up to FLAGGED_TICKET_SCAN_PAGES pages (1000 tickets) of
    the IT-Operations group on a cold cache, which is not a cost any page
    render having nothing to do with Blocked/Standup should ever risk
    paying just to draw a header badge. Returns 0 on a cold/expired cache."""
    with _flagged_ticket_cache_lock:
        cached = _flagged_ticket_cache
        if not cached or (datetime.now() - cached["fetched_at"]).total_seconds() >= FLAGGED_CACHE_TTL_SECONDS:
            return 0
        return len(cached["items"])


# A read-only "what have I already closed out" look-back, the same "no
# drafting, no persistence" posture as fetch_recent_tickets/
# fetch_flagged_tickets above -- just filtered by status+assignee instead
# of by group ("recent activity") or tag ("block"/"standup").
RECENTLY_SOLVED_LIMIT = 40
SOLVED_TICKET_PER_PAGE = 100
# Same scan-depth safety cap fetch_flagged_tickets uses, and the same
# reason: ZendeskClient.get_tickets() can only filter by a single group_id
# via the Search API (see the comment above FLAGGED_TICKET_TAGS) -- there's
# no "status:solved assignee_id:N" query it can build directly, so this has
# to page through the IT-Operations group's own tickets (sorted by
# updated_at, most recent first) and check each one's status/assignee in
# Python. Overridable via .env for the same reason FLAGGED_TICKET_SCAN_PAGES
# is.
SOLVED_TICKET_SCAN_PAGES = int(os.getenv("SOLVED_TICKET_SCAN_PAGES", "10"))
_solved_ticket_cache: dict = {}
SOLVED_CACHE_TTL_SECONDS = SECURITY_CACHE_TTL_SECONDS  # same cadence as the other read-only Zendesk-derived panels
# Same reasoning as _flagged_ticket_cache_lock -- guards the dict read and
# the one-shot replacement, never the multi-page get_tickets() scan itself.
_solved_ticket_cache_lock = threading.Lock()


def fetch_recently_solved_tickets(client, limit: int = RECENTLY_SOLVED_LIMIT, log_fn=print, force: bool = False) -> dict:
    """Returns {"items": [...], "error": str|None, "capped": bool} -- the
    last `limit` tickets in the IT-Operations group that are BOTH
    status=="solved" AND assigned to this tool's own configured Zendesk
    account (resolve_own_agent_id), most recently touched first. Every
    item's "already_ours" is always True and "assignee_name" is always
    None (there's nothing to resolve -- it's definitionally this account),
    kept on each item only so _render_readonly_ticket_row (shared with
    /security and /flagged) doesn't need a special case for this page.

    Scoped to IT_OPERATIONS_GROUP_ID specifically, the same restriction
    fetch_flagged_tickets already lives with and for the identical reason:
    ZendeskClient.get_tickets() only supports filtering by a single
    group_id (via the Search API), not by assignee+status directly -- so
    this pages through that one group's tickets sorted by *updated_at*
    (a solved ticket's own last-touched time, doubling as a reasonable
    proxy for "when it was solved" -- there's no separate solved_at field
    in the payload) and filters in Python, the same shape
    fetch_flagged_tickets already uses for its own tag filtering. A ticket
    solved in some OTHER group this account might also have access to
    won't appear here -- if that's ever needed, it'd mean scanning every
    group (or the whole account with no group filter at all), a much
    bigger and slower scan than this page's use case has asked for so far.

    Stops paging as soon as `limit` matches are found (sorted most-recent-
    first, so anything past that point is guaranteed older) -- this is NOT
    the same as "capped": capped only becomes True if the scan hit
    SOLVED_TICKET_SCAN_PAGES/ZENDESK_SEARCH_RESULT_CAP (or a page fetch
    outright failed) before finding `limit` matches, meaning there could
    be more further back that this scan didn't reach. Cached the same way
    fetch_flagged_tickets is (SOLVED_CACHE_TTL_SECONDS, falling back to the
    last successful result on a total failure)."""
    global _solved_ticket_cache
    now = datetime.now()
    with _solved_ticket_cache_lock:
        cached = _solved_ticket_cache or None
        if (
            not force and cached and cached.get("limit") == limit
            and (now - cached["fetched_at"]).total_seconds() < SOLVED_CACHE_TTL_SECONDS
        ):
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}

    if client.client is None:
        if cached:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}
        return {"items": [], "error": "Zendesk isn't connected right now -- see the reconnect banner above.", "capped": False}

    try:
        agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
        if agent_user_id is None:
            raise RuntimeError(
                "could not resolve this tool's own Zendesk account (ZENDESK_EMAIL unset, or search_users unreachable)"
            )

        items = []
        capped = False
        page = 1
        # Never actually requests a page whose offset would exceed Zendesk's
        # own ZENDESK_SEARCH_RESULT_CAP -- SOLVED_TICKET_SCAN_PAGES can only
        # narrow the scan further than that, never widen it past the hard
        # ceiling (same clamp fetch_flagged_tickets uses).
        max_page = min(SOLVED_TICKET_SCAN_PAGES, ZENDESK_SEARCH_RESULT_CAP // SOLVED_TICKET_PER_PAGE)
        while page <= max_page:
            try:
                result = client.get_tickets(
                    group_id=IT_OPERATIONS_GROUP_ID,
                    sort_by="updated_at",
                    sort_order="desc",
                    per_page=SOLVED_TICKET_PER_PAGE,
                    page=page,
                )
            except Exception as exc:
                log_fn(f"fetch_recently_solved_tickets() page {page} failed ({exc}) -- "
                       f"stopping with {len(items)} item(s) already found")
                capped = True
                break
            tickets = result.get("tickets", [])
            if not tickets:
                break
            for ticket in tickets:
                if ticket.get("status") != "solved" or ticket.get("assignee_id") != agent_user_id:
                    continue
                name, name_confidence, _ = resolve_requester_name(
                    client, ticket.get("requester_id"), ticket.get("description") or "", log_fn=log_fn
                )
                items.append(
                    {
                        "ticket_id": ticket.get("id"),
                        "subject": ticket.get("subject", ""),
                        "status": ticket.get("status", ""),
                        "url": ticket_url(ticket.get("id")),
                        "requester_name": name if name_confidence == "confirmed" else None,
                        "requester_id": ticket.get("requester_id"),
                        "assignee_id": ticket.get("assignee_id"),
                        "assignee_name": None,
                        "already_ours": True,
                        "solved_at": ticket.get("updated_at", ""),
                    }
                )
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
            if not result.get("has_more"):
                break
            page += 1
        else:
            # Loop condition (page <= max_page) went false rather than an
            # in-loop `break` -- meaning Zendesk still had at least one more
            # page we didn't fetch, and we still hadn't found `limit`
            # matches yet.
            capped = True

        with _solved_ticket_cache_lock:
            _solved_ticket_cache = {"items": items, "fetched_at": now, "capped": capped, "limit": limit}
        return {"items": items, "error": None, "capped": capped}
    except Exception as exc:
        log_fn(f"fetch_recently_solved_tickets() failed ({exc})")
        if cached:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}
        return {"items": [], "error": f"Could not load recently solved tickets ({exc}).", "capped": False}



# ---------------------------------------------------------------------------
# SLA dashboard (/sla) -- live four-rule SLA evaluation over the
# IT-Operations queue, mirroring the sibling sla-report-slack project's
# own daily email report (zendesk_sla_slack_report.py), plus a read-only
# look at that project's own daily-snapshot history file for trend data.
# Deliberately built on ONLY client.get_tickets/client.get_ticket_comments
# -- the same Zendesk API surface every other read-only panel in this file
# already uses -- rather than that sibling project's ticket-metrics API
# call, so this stays consistent with the rest of this tool instead of
# introducing a second way of talking to Zendesk.
# ---------------------------------------------------------------------------

# Same four thresholds (business hours) as the standalone daily email
# report this mirrors, kept as the same numbers on purpose so a ticket
# this page calls "breached" matches what that report would say about it.
# SLA_RESOLUTION_HRS is overridable via env var for the same reason that
# report's own copy is.
SLA_INITIAL_RESPONSE_HRS = 2
SLA_REQUESTER_WAIT_HRS = 4
SLA_NO_UPDATE_HRS = 8
SLA_RESOLUTION_HRS = float(os.getenv("SLA_RESOLUTION_HRS", "40"))  # 5 business days x 8 hrs/day
SLA_DUE_SOON_PCT = 0.75  # flag when 75% of a threshold has elapsed

# Non-terminal statuses worth evaluating at all -- solved/closed tickets
# are excluded up front (rather than fetched, evaluated, and short-
# circuited inside evaluate_sla_ticket) so a comments fetch is never spent
# on a ticket whose SLA checks are guaranteed to come back on_track anyway.
SLA_TRACKED_STATUSES = ("new", "open", "pending", "hold")


def business_hours_between(start, end) -> float:
    """Elapsed BUSINESS_HOURS-only hours between two datetimes, in the
    system's own local time -- this tool runs locally on the machine whose
    business hours actually matter, unlike the GitHub-Actions-hosted
    sibling report, which has to convert to an explicitly configured
    TIMEZONE env var instead. Reuses BUSINESS_HOURS/BUSINESS_WEEKDAYS
    (defined above) rather than a second copy of "what counts as a
    business hour," the same reasoning next_scan_target already follows.

    Tolerant of naive or tz-aware datetimes for either argument -- ticket
    timestamps parsed from Zendesk arrive tz-aware (UTC); `now` is usually
    a plain datetime.now(). Tz-aware values are converted to local time via
    .astimezone() and then stripped back to naive (.replace(tzinfo=None))
    -- naive ones are assumed to already be local -- so both ends of the
    comparison always land in the same naive-local basis; astimezone()
    alone would leave a tz-aware result that can't be compared against a
    naive `now`. Returns 0.0 for missing input or a non-positive span,
    never raises."""
    if start is None or end is None:
        return 0.0
    if start.tzinfo is not None:
        start = start.astimezone().replace(tzinfo=None)
    if end.tzinfo is not None:
        end = end.astimezone().replace(tzinfo=None)
    if end <= start:
        return 0.0

    work_start, work_end = BUSINESS_HOURS.start, BUSINESS_HOURS.stop
    total = 0.0
    cursor = start
    for _ in range(370):  # comfortably covers a year of elapsed time; not unbounded
        if cursor.date() > end.date():
            break
        if cursor.weekday() in BUSINESS_WEEKDAYS:
            day_start = cursor.replace(hour=work_start, minute=0, second=0, microsecond=0)
            day_end = cursor.replace(hour=work_end, minute=0, second=0, microsecond=0)
            seg_start = max(cursor, day_start)
            seg_end = min(end, day_end)
            if seg_end > seg_start:
                total += (seg_end - seg_start).total_seconds() / 3600
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return round(total, 2)


def _sla_state(elapsed: float, threshold: float) -> str:
    if elapsed >= threshold:
        return "breached"
    if elapsed >= threshold * SLA_DUE_SOON_PCT:
        return "due_soon"
    return "on_track"


def _parse_zendesk_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_agent_comment(c: dict, requester_id, agent_user_id=None) -> bool:
    """True if a comment counts as "an agent touched this ticket" -- any
    comment (public OR internal; unlike _has_public_reply_from_other,
    which only cares about public ones) from someone other than the
    requester, or from this tool's own configured account even when that
    happens to equal requester_id (the same self-referential-test-ticket
    carve-out _first_public_reply_from_other uses)."""
    author = c.get("author_id")
    return author != requester_id or (agent_user_id is not None and author == agent_user_id)


def _first_agent_comment(comments: list, requester_id, agent_user_id=None):
    for c in comments:
        if _is_agent_comment(c, requester_id, agent_user_id):
            return c
    return None


def _last_agent_comment(comments: list, requester_id, agent_user_id=None):
    match = None
    for c in comments:
        if _is_agent_comment(c, requester_id, agent_user_id):
            match = c
    return match


def _last_public_comment(comments: list):
    match = None
    for c in comments:
        if c.get("public"):
            match = c
    return match


def evaluate_sla_ticket(ticket: dict, comments: list, now, assignee_name=None, requester_name=None, agent_user_id=None) -> dict:
    """Evaluates one ticket against the four SLA rules above, using only
    client.get_tickets/client.get_ticket_comments data (see this section's
    header comment for why). "First/last agent comment" is derived from
    comments directly rather than a metrics-endpoint timestamp, which is
    the one place this necessarily diverges in method from the sibling
    report -- the classification (breached/due_soon/on_track per rule) is
    intended to match, even though the exact elapsed-hours figure may
    differ by a few minutes from what that report would compute for the
    same ticket.

    Returns a dict shaped for both _sla_rule_cards_html/_sla_kpi_strip_html
    and (via url/subject/requester_name/assignee_name/already_ours)
    _render_readonly_ticket_row, so a ticket can be listed consistently
    wherever it needs to show up on the /sla page. breached_rules/
    due_soon_rules are lists of (rule_key, detail) tuples, not pre-
    formatted strings -- there's exactly one caller (this page), so there's
    no format-then-reparse indirection to keep in sync the way the sibling
    report's own string-embedded rule_key needs."""
    tid = ticket.get("id")
    status = ticket.get("status", "")
    requester_id = ticket.get("requester_id")
    assignee_id = ticket.get("assignee_id")
    created_at = _parse_zendesk_dt(ticket.get("created_at"))
    updated_at = _parse_zendesk_dt(ticket.get("updated_at"))

    base = {
        "ticket_id": tid,
        "subject": ticket.get("subject", ""),
        "status": status,
        "priority": ticket.get("priority") or "normal",
        "url": ticket_url(tid),
        "requester_id": requester_id,
        "requester_name": requester_name,
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "already_ours": bool(assignee_id) and agent_user_id is not None and assignee_id == agent_user_id,
        # Raw ISO string (not the parsed created_at above) -- kept only so
        # the /sla page's "awaiting first response" list can sort oldest-
        # first; ISO 8601 strings already sort correctly as plain text, so
        # there's no need to hand back a datetime object for it.
        "created_at": ticket.get("created_at"),
    }

    # Terminal tickets, and self-service personal tasks that slipped
    # through anyway (defensive -- fetch_sla_tickets already filters these
    # out by status/PERSONAL_TASK_TAG before this is ever called) -- SLA
    # checks don't apply to either.
    if status in ("solved", "closed") or (requester_id and assignee_id and requester_id == assignee_id):
        return {**base, "sla_status": "on_track", "breached_rules": [], "due_soon_rules": []}

    breached, due_soon = [], []

    # 1. Initial response -- any agent comment at all (public or internal).
    if _first_agent_comment(comments, requester_id, agent_user_id) is None and created_at:
        elapsed = business_hours_between(created_at, now)
        state = _sla_state(elapsed, SLA_INITIAL_RESPONSE_HRS)
        if state == "breached":
            breached.append(("initial_response", f"{elapsed:.1f}h elapsed, no reply yet / {SLA_INITIAL_RESPONSE_HRS}h limit"))
        elif state == "due_soon":
            due_soon.append(("initial_response", f"{round(SLA_INITIAL_RESPONSE_HRS - elapsed, 1)}h remaining, no reply yet"))

    # 2. Requester wait -- open, has comments, requester spoke last in public.
    if status == "open" and comments:
        last_public = _last_public_comment(comments)
        requester_spoke_last = last_public is None or last_public.get("author_id") == requester_id
        if requester_spoke_last:
            last_agent = _last_agent_comment(comments, requester_id, agent_user_id)
            anchor = _parse_zendesk_dt(last_agent.get("created_at")) if last_agent else created_at
            if anchor:
                elapsed = business_hours_between(anchor, now)
                state = _sla_state(elapsed, SLA_REQUESTER_WAIT_HRS)
                if state == "breached":
                    breached.append(("requester_wait", f"{elapsed:.1f}h since last agent action / {SLA_REQUESTER_WAIT_HRS}h limit"))
                elif state == "due_soon":
                    due_soon.append(("requester_wait", f"{round(SLA_REQUESTER_WAIT_HRS - elapsed, 1)}h remaining"))

    # 3. No-update window -- open only (pending means waiting on requester).
    if status == "open":
        last_agent = _last_agent_comment(comments, requester_id, agent_user_id)
        anchor = _parse_zendesk_dt(last_agent.get("created_at")) if last_agent else updated_at
        if anchor:
            elapsed = business_hours_between(anchor, now)
            state = _sla_state(elapsed, SLA_NO_UPDATE_HRS)
            if state == "breached":
                breached.append(("no_update", f"{elapsed:.1f}h since last agent action / {SLA_NO_UPDATE_HRS}h limit"))
            elif state == "due_soon":
                due_soon.append(("no_update", f"{round(SLA_NO_UPDATE_HRS - elapsed, 1)}h remaining"))

    # 4. Resolution age -- skip pending (waiting on requester) and hold (paused).
    if created_at and status not in ("pending", "hold"):
        elapsed = business_hours_between(created_at, now)
        state = _sla_state(elapsed, SLA_RESOLUTION_HRS)
        if state == "breached":
            breached.append(("resolution_age", f"{elapsed:.1f}h / {SLA_RESOLUTION_HRS:.0f}h limit"))
        elif state == "due_soon":
            due_soon.append(("resolution_age", f"{round(SLA_RESOLUTION_HRS - elapsed, 1)}h remaining"))

    sla_status = "breached" if breached else ("due_soon" if due_soon else "on_track")
    return {**base, "sla_status": sla_status, "breached_rules": breached, "due_soon_rules": due_soon}


SLA_CACHE_TTL_SECONDS = SECURITY_CACHE_TTL_SECONDS  # same cadence as the other read-only Zendesk-derived panels
SLA_TICKET_PER_PAGE = 100
SLA_TICKET_SCAN_PAGES = int(os.getenv("SLA_TICKET_SCAN_PAGES", "10"))
_sla_ticket_cache: dict = {}
# Same reasoning as _flagged_ticket_cache_lock -- guards the dict read and
# the one-shot replacement, never the multi-page get_tickets() scan (or its
# per-ticket get_ticket_comments calls) itself.
_sla_ticket_cache_lock = threading.Lock()


def fetch_sla_tickets(client, log_fn=print, force: bool = False) -> dict:
    """Returns {"items": [...evaluate_sla_ticket dicts...], "error": str|None,
    "capped": bool} -- every non-terminal IT-Operations ticket (excluding
    personal tasks, see PERSONAL_TASK_TAG), each evaluated against the four
    SLA rules above. Same scan/cache shape as fetch_flagged_tickets/
    fetch_recently_solved_tickets (paged via IT_OPERATIONS_GROUP_ID,
    SLA_CACHE_TTL_SECONDS cadence, fails open to the last good result on a
    total failure) -- the one cost specific to this one is a
    client.get_ticket_comments call per ticket (needed for the
    initial_response/requester_wait/no_update rules), the same per-ticket
    cost find_candidates already accepts for its own candidate scan. A
    single ticket's comments failing to fetch skips just that ticket
    (logged, not fatal) rather than discarding the whole scan."""
    global _sla_ticket_cache
    now_dt = datetime.now()
    with _sla_ticket_cache_lock:
        cached = _sla_ticket_cache or None
        if not force and cached and (now_dt - cached["fetched_at"]).total_seconds() < SLA_CACHE_TTL_SECONDS:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}

    if client.client is None:
        if cached:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}
        return {"items": [], "error": "Zendesk isn't connected right now -- see the reconnect banner above.", "capped": False}

    try:
        agent_user_id = resolve_own_agent_id(client, log_fn=log_fn)
        items = []
        capped = False
        page = 1
        max_page = min(SLA_TICKET_SCAN_PAGES, ZENDESK_SEARCH_RESULT_CAP // SLA_TICKET_PER_PAGE)
        while page <= max_page:
            try:
                result = client.get_tickets(
                    group_id=IT_OPERATIONS_GROUP_ID,
                    sort_by="updated_at",
                    sort_order="desc",
                    per_page=SLA_TICKET_PER_PAGE,
                    page=page,
                )
            except Exception as exc:
                log_fn(f"fetch_sla_tickets() page {page} failed ({exc}) -- "
                       f"stopping with {len(items)} item(s) already found")
                capped = True
                break
            tickets = result.get("tickets", [])
            if not tickets:
                break
            for ticket in tickets:
                if ticket.get("status") not in SLA_TRACKED_STATUSES:
                    continue
                if PERSONAL_TASK_TAG in (ticket.get("tags") or []):
                    continue
                try:
                    comments = client.get_ticket_comments(ticket["id"])
                except Exception as exc:
                    log_fn(f"fetch_sla_tickets(): comments fetch failed for #{ticket.get('id')} ({exc}) -- skipping")
                    continue
                name, name_confidence, _ = resolve_requester_name(
                    client, ticket.get("requester_id"), ticket.get("description") or "", log_fn=log_fn
                )
                assignee_id = ticket.get("assignee_id")
                assignee_name = resolve_assignee_name(client, assignee_id, log_fn=log_fn) if assignee_id else None
                items.append(evaluate_sla_ticket(
                    ticket, comments, now_dt,
                    assignee_name=assignee_name,
                    requester_name=name if name_confidence == "confirmed" else None,
                    agent_user_id=agent_user_id,
                ))
            if not result.get("has_more"):
                break
            page += 1
        else:
            capped = True

        with _sla_ticket_cache_lock:
            _sla_ticket_cache = {"items": items, "fetched_at": now_dt, "capped": capped}
        return {"items": items, "error": None, "capped": capped}
    except Exception as exc:
        log_fn(f"fetch_sla_tickets() failed ({exc})")
        if cached:
            return {"items": cached["items"], "error": None, "capped": cached["capped"]}
        return {"items": [], "error": f"Could not load SLA data ({exc}).", "capped": False}


def cached_sla_breach_count() -> int:
    """Best-effort breach count from fetch_sla_tickets' own cache, WITHOUT
    triggering a fetch of its own -- same reasoning as
    cached_flagged_ticket_count/cached_security_ticket_count above, for the
    nav badge. Returns 0 on a cold/expired cache."""
    with _sla_ticket_cache_lock:
        cached = _sla_ticket_cache
        if not cached or (datetime.now() - cached["fetched_at"]).total_seconds() >= SLA_CACHE_TTL_SECONDS:
            return 0
        return sum(1 for item in cached["items"] if item.get("sla_status") == "breached")


# Read-only look at the sibling sla-report-slack project's own daily-
# snapshot history file -- NEVER written to from here. That file is
# exclusively owned by that project's own GitHub Actions job, which
# commits it back to ITS OWN repo after every run; this tool only ever
# reads it, the same one-directional relationship _render_sla_page's own
# "history as of <date>" note makes explicit on the page itself.
# Overridable via env var for anyone whose clone of that repo doesn't sit
# at the default relative path -- the default assumes the two repos are
# cloned as sibling folders under ~/Desktop/git, which is how they exist
# on this machine today.
SLA_HISTORY_FILE = Path(os.getenv(
    "SLA_HISTORY_FILE",
    str(Path.home() / "Desktop" / "git" / "sla-report-slack" / "data" / "sla_history.json"),
))


def sla_history_snapshot() -> dict:
    """Read-only load of the sibling project's sla_history.json, for the
    /sla page's trend chart. Returns {"entries": [...], "error": str|None,
    "as_of": str|None} -- "as_of" is the last entry's own "date" field
    (falling back to the file's own mtime) so the page can honestly label
    how fresh this is: this file is only ever as current as the last
    `git pull` of that OTHER repo on this machine, since this tool never
    fetches or writes it. A missing file or unreadable JSON degrades to an
    empty "entries" list with an explanatory "error", never raises."""
    try:
        raw = SLA_HISTORY_FILE.read_text()
    except FileNotFoundError:
        return {"entries": [], "error": f"No history file found at {SLA_HISTORY_FILE} yet.", "as_of": None}
    except Exception as exc:
        return {"entries": [], "error": f"Could not read SLA history file: {exc}", "as_of": None}

    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError("expected a JSON list of daily snapshots")
    except Exception as exc:
        return {"entries": [], "error": f"Could not parse SLA history file: {exc}", "as_of": None}

    as_of = entries[-1].get("date") if entries else None
    if not as_of:
        try:
            as_of = datetime.fromtimestamp(SLA_HISTORY_FILE.stat().st_mtime).strftime("%Y-%m-%d")
        except Exception:
            as_of = None
    return {"entries": entries, "error": None, "as_of": as_of}


# Read-only look at the sibling meetingnotestool project's precomputed
# standup view -- NEVER written to from here, same one-directional
# relationship as SLA_HISTORY_FILE above. That project (a separate,
# private repo) parses the Operations Standup notes and owns all of the
# status logic; its build writes this one file, and /standup only renders
# it. Nothing from the standup notes is stored in this repo -- it's read
# at request time from that other checkout -- so none of it can end up
# in this repo's git history. Overridable via env var for a clone that
# doesn't sit at the default sibling path.
# Whose action items /standup/people opens on (any owner name in the notes
# can be picked from the page; this is only the default).
STANDUP_DEFAULT_PERSON = os.getenv("STANDUP_DEFAULT_PERSON", "Ryan").strip() or "Ryan"
STANDUP_DATA_FILE = Path(os.getenv(
    "STANDUP_DATA_FILE",
    str(Path.home() / "Desktop" / "git" / "meetingnotestool" / "dist" / "standup_data.json"),
))
STANDUP_SCHEMA_VERSION = 2

# (mtime_ns, size) -> parsed view. The nav badge reads this on every page
# render, so a re-parse only happens when meetingnotestool actually
# rebuilds the file, not on every request.
_standup_cache: dict = {}
_standup_cache_lock = threading.Lock()


def standup_snapshot() -> dict:
    """Read-only load of meetingnotestool's dist/standup_data.json for the
    /standup page. Returns {"view": dict|None, "error": str|None}. A missing
    file, unreadable JSON, or an unexpected schema version degrades to
    view=None with an explanatory error, never raises -- the rest of this
    tool keeps working whether or not that sibling project is set up."""
    try:
        st = STANDUP_DATA_FILE.stat()
    except FileNotFoundError:
        return {"view": None, "error": f"No standup data found at {STANDUP_DATA_FILE} yet -- run meetingnotestool's build.py."}
    except Exception as exc:
        return {"view": None, "error": f"Could not read standup data: {exc}"}
    key = (STANDUP_DATA_FILE, st.st_mtime_ns, st.st_size)
    with _standup_cache_lock:
        if key in _standup_cache:
            return _standup_cache[key]
    try:
        view = json.loads(STANDUP_DATA_FILE.read_text())
        if not isinstance(view, dict):
            raise ValueError("expected a JSON object")
        if view.get("schema") != STANDUP_SCHEMA_VERSION:
            raise ValueError(f"schema {view.get('schema')!r}, expected {STANDUP_SCHEMA_VERSION} -- this page and meetingnotestool's build.py are out of step")
        result = {"view": view, "error": None}
    except Exception as exc:
        result = {"view": None, "error": f"Could not parse standup data: {exc}"}
    with _standup_cache_lock:
        _standup_cache.clear()
        _standup_cache[key] = result
    return result


# The one file this tool writes in meetingnotestool: your own calls on
# standup action items and quiet topics. IDs, a decision and a timestamp
# only -- no free text -- so nothing from the notes is copied into it.
# meetingnotestool's build.py applies it permanently on its next build;
# apply_standup_decisions() below shows it on /standup immediately.
STANDUP_DECISIONS_FILE = Path(os.getenv(
    "STANDUP_DECISIONS_FILE",
    str(Path.home() / "Desktop" / "git" / "meetingnotestool" / "decisions.json"),
))
STANDUP_DECISIONS_LOCK_FILE = OUTPUT_DIR / "standup_decisions.json.lock"
STANDUP_ACTION_DECISIONS = {"done", "dropped", "open"}
STANDUP_TOPIC_DECISIONS = {"done", "dropped", "keep"}


def load_standup_decisions() -> dict:
    """Plain read of decisions.json; an empty store if it's missing or
    unreadable (same fail-open read posture as load_pending)."""
    empty = {"schema": 1, "actions": {}, "topics": {}}
    try:
        data = json.loads(STANDUP_DECISIONS_FILE.read_text())
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    return {"schema": 1, "actions": dict(data.get("actions") or {}), "topics": dict(data.get("topics") or {})}


def _standup_known_ids(view: dict) -> tuple:
    """(action ids, topic keys) a decision may be recorded for -- your own
    action items and any topic in the view. Anything else is refused rather
    than written."""
    me = view.get("me")
    actions = {aid for aid, a in (view.get("actions") or {}).items() if me and a.get("owner") == me}
    topics = set((view.get("topics") or {}).keys())
    return actions, topics


def record_standup_decision(kind: str, item_id: str, decision: str) -> tuple:
    """Sets (or with decision="clear", removes) your decision on one standup
    action item or quiet topic in decisions.json. Returns (ok, message,
    previous_decision_or_None) -- the previous value is what an Undo puts
    back. Refuses unknown kinds/decisions, IDs the current standup view
    doesn't contain, and a missing meetingnotestool checkout (it never
    creates that project's folder)."""
    allowed = {"action": STANDUP_ACTION_DECISIONS, "topic": STANDUP_TOPIC_DECISIONS}.get(kind)
    if allowed is None or (decision not in allowed and decision != "clear"):
        return False, "That standup decision isn't one this page knows how to record.", None
    view = standup_snapshot()["view"]
    if not view:
        return False, "Standup data isn't available, so nothing was recorded.", None
    known_actions, known_topics = _standup_known_ids(view)
    if item_id not in (known_actions if kind == "action" else known_topics):
        return False, "That standup item isn't in the current view (it may have changed since the page loaded). Nothing was recorded.", None
    if not STANDUP_DECISIONS_FILE.parent.is_dir():
        return False, f"No meetingnotestool folder at {STANDUP_DECISIONS_FILE.parent}, so nothing was recorded.", None
    section = "actions" if kind == "action" else "topics"
    field = "outcome" if kind == "action" else "decision"
    with open(STANDUP_DECISIONS_LOCK_FILE, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        data = load_standup_decisions()
        previous = (data[section].get(item_id) or {}).get(field)
        if decision == "clear":
            data[section].pop(item_id, None)
        else:
            data[section][item_id] = {field: decision, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        data[section] = dict(sorted(data[section].items()))
        _atomic_write_json(STANDUP_DECISIONS_FILE, data)
    return True, "", previous


def apply_standup_decisions(view: dict, decisions: dict) -> dict:
    """Returns a copy of the standup view with your decisions.json calls
    applied, so /standup reflects a click immediately instead of waiting for
    meetingnotestool's next build. Idempotent: once that build has applied a
    decision itself, applying it again here changes nothing.

    Action items: done/dropped resolves the item ("by you", marked pending
    until the next build); open clears a resolution the notes gave it. Your
    open and resolved lists are then rebuilt from the items themselves.
    Topics: the decision rides on the topic as "decision" -- and done/dropped
    becomes its status -- but only if made on or after the topic's latest
    mention; a later standup update wins, the same rule build.py uses."""
    v = copy.deepcopy(view)
    acts = decisions.get("actions") or {}
    tops = decisions.get("topics") or {}
    actions = v.get("actions") or {}
    topics = v.get("topics") or {}

    for aid, d in acts.items():
        a = actions.get(aid)
        outcome = (d or {}).get("outcome")
        if not a:
            continue
        r = a.get("resolution")
        if outcome in ("done", "dropped"):
            if not (r and r.get("by_you") and r.get("outcome") == outcome):
                a["resolution"] = {"date": str(d.get("at", ""))[:10], "outcome": outcome, "note": "Marked by you",
                                   "by_you": True, "pending": True}
        elif outcome == "open" and r:
            a["resolution"] = None

    me = v.get("me")
    mine = [a for a in actions.values() if me and a.get("owner") == me]
    v["my_open_actions"] = [a["id"] for a in sorted((a for a in mine if not a.get("resolution")),
                                                    key=lambda a: (a.get("date") or "", a.get("id") or ""))]
    v["my_resolved_actions"] = [a["id"] for a in sorted((a for a in mine if a.get("resolution")),
                                                        key=lambda a: (a["resolution"].get("date") or "", a.get("id") or ""),
                                                        reverse=True)[:20]]

    for key, d in tops.items():
        t = topics.get(key)
        decision = (d or {}).get("decision")
        if t and decision in STANDUP_TOPIC_DECISIONS and str(d.get("at", ""))[:10] >= (t.get("last_date") or ""):
            t["decision"] = decision
            if decision in ("done", "dropped"):
                t["status"] = decision
    return v


def standup_view() -> dict:
    """standup_snapshot() with your decisions applied -- what /standup and
    its nav badge actually show. {"view": dict|None, "error": str|None}."""
    snap = standup_snapshot()
    if not snap["view"]:
        return snap
    return {"view": apply_standup_decisions(snap["view"], load_standup_decisions()), "error": None}


def standup_open_action_count() -> int:
    """Nav-badge count: your open standup action items, after your own
    decisions. 0 (no badge) when the standup data isn't available, same
    fail-quiet posture as the other cache-backed badges."""
    view = standup_view()["view"]
    return len(view.get("my_open_actions") or []) if view else 0


def standup_tasks_by_action() -> dict:
    """{standup action item id: personal task} for every My tasks entry made
    from /standup's "Add to My tasks", live or archived, so the page can show
    "In My tasks" instead of offering the button again. Purely local."""
    out = {}
    try:
        archived = json.loads(PERSONAL_TASKS_ARCHIVE_FILE.read_text())
    except Exception:
        archived = []
    for v in list(archived if isinstance(archived, list) else []) + list(load_personal_tasks().values()):
        if isinstance(v, dict) and v.get("standup_action_id"):
            out[v["standup_action_id"]] = v
    return out


def add_standup_action_to_tasks(action_id: str, log_fn=print) -> dict:
    """/standup's "Add to My tasks": saves one of your open standup action
    items as a local My tasks draft (add_personal_task -- no Zendesk call;
    promoting it to a real ticket stays a separate click on /tasks).

    The draft's notes say only which standup it came from, deliberately
    without the Google Doc link: a promoted task's notes can end up in a
    Zendesk ticket, and the standup notes aren't meant to travel there.
    Refuses an action item that isn't one of your open ones in the current
    view, or one that already has a task (live or archived)."""
    view = standup_view()["view"]
    if not view:
        return {"ok": False, "error": "Standup data isn't available, so no task was added."}
    action = (view.get("actions") or {}).get(action_id) if action_id in (view.get("my_open_actions") or []) else None
    if action is None:
        return {"ok": False, "error": "That action item isn't one of your open standup items any more. No task was added."}
    if action_id in standup_tasks_by_action():
        return {"ok": False, "error": "That standup action item is already in My tasks."}
    try:
        raised = datetime.strptime(action.get("date", ""), "%Y-%m-%d")
        when = f"{raised.strftime('%b')} {raised.day}"
    except ValueError:
        when = action.get("date", "")
    return add_personal_task(action.get("task", ""), f"From the {when} Operations Standup.", log_fn=log_fn,
                             standup_action_id=action_id)


# Agents excluded from the /sla page's agent-activity chart entirely --
# not just hidden via that chart's own checkbox filter, but never listed
# or plotted at all, no matter the selected date range. Set by explicit
# request rather than any activity-based heuristic (e.g. former team
# members, or names that don't belong in this specific chart for reasons
# this file has no way to infer on its own).
SLA_AGENT_ACTIVITY_EXCLUDE = {"Illena Armstrong", "Jim Reavis", "Ryan Bergsma"}


def sla_chart_data(history: dict) -> dict:
    """Reshapes sla_history_snapshot()'s raw per-day entries into the
    parallel arrays the /sla page's Chart.js trend charts need client-side
    -- the same shape zendesk_sla_slack_report.py's own sla_dashboard.html
    embeds as its ALL_LABELS/ALL_ON_TRACK/ALL_DUE_SOON/ALL_BREACHED/
    ALL_COMPLIANCE_PCT/ALL_BREACHED_IDS/ALL_AGENT_HIST_PUB/ALL_AGENT_HIST_INT
    JS consts, so the two dashboards can render genuinely identical charts
    from the same underlying data. Kept here rather than in
    review_server.py since it's pure data reshaping, not HTML.

    Every top-level array is the same length as "labels" (one entry per
    tracked day, oldest first). Per-agent public/internal arrays are
    zero-filled for any day that agent has no recorded activity --
    including every day before this file started capturing
    "agent_activity" at all (older entries simply lack that key). Agents
    in SLA_AGENT_ACTIVITY_EXCLUDE are dropped here, before the data ever
    reaches the page -- excluded from "agent_names" entirely, not merely
    left out of some default-shown set."""
    entries = history.get("entries") or []
    labels = [e.get("date", "") for e in entries]
    on_track = [e.get("on_track", 0) or 0 for e in entries]
    due_soon = [e.get("due_soon", 0) or 0 for e in entries]
    breached = [e.get("breached", 0) or 0 for e in entries]
    totals = [e.get("total") or (on_track[i] + due_soon[i] + breached[i]) for i, e in enumerate(entries)]
    compliance_pct = [e.get("compliance_pct") for e in entries]
    breached_ids = [e.get("breached_ids") or [] for e in entries]

    agent_names = sorted({
        name for e in entries for name in (e.get("agent_activity") or {})
        if name not in SLA_AGENT_ACTIVITY_EXCLUDE
    })
    agents = {}
    for name in agent_names:
        public, internal = [], []
        for e in entries:
            activity = (e.get("agent_activity") or {}).get(name) or {}
            public.append(activity.get("public", 0) or 0)
            internal.append(activity.get("internal", 0) or 0)
        agents[name] = {"public": public, "internal": internal}

    return {
        "labels": labels,
        "on_track": on_track,
        "due_soon": due_soon,
        "breached": breached,
        "totals": totals,
        "compliance_pct": compliance_pct,
        "breached_ids": breached_ids,
        "agent_names": agent_names,
        "agents": agents,
    }


def claim_ticket(client, ticket_id, agent_user_id, log_fn=print) -> dict:
    """Self-assigns a ticket to this tool's own configured Zendesk account
    -- the direct complement to the "also being worked on" badge above:
    that badge flags someone else already having a ticket, this lets you
    take an unclaimed one (or knowingly take over an assigned one)
    without leaving this page. Just another Zendesk assignee change --
    indistinguishable from doing the same thing by hand in Zendesk's own
    UI, easily reversible, nothing posted or destroyed -- so this makes
    the API call directly rather than needing its own confirm-page
    interstitial the way an already-replied post does; the review page's
    own Claim button asks first when it's about to take over someone
    else's ticket (see _render_ticket).

    If this ticket is one of ours (tracked in pending_drafts.json), updates
    that entry's assignee fields in place so the badge and draft box
    reflect the claim immediately, without waiting for the next scan's
    recheck_pending_for_assignment to catch up -- mirrors the pattern
    _post() and _dismiss() already use for updating local state right
    after a Zendesk-side action succeeds. For an IT-Security column ticket
    (never tracked locally), there's nothing to update here -- the change
    exists only in Zendesk, and that column's own cache picks it up on its
    next refresh.

    Returns {"ok": True, "error": None} on success, or {"ok": False,
    "error": str} on failure -- the caller decides how to phrase the
    banner."""
    try:
        client.update_ticket(int(ticket_id), assignee_id=agent_user_id)
    except Exception as exc:
        log_fn(f"claim_ticket(#{ticket_id}) failed: {exc}")
        return {"ok": False, "error": str(exc)}

    def _mutate(data):
        entry = data.get(str(ticket_id))
        if entry is not None:
            entry["assignee_id"] = agent_user_id
            entry["assigned_to_other"] = False
            entry["assignee_name"] = None

    update_pending(_mutate)
    log_fn(f"Claimed #{ticket_id} -- now assigned to this tool's own account.")
    return {"ok": True, "error": None}


# --- Personal task tickets --------------------------------------------
# A lightweight personal to-do list, entirely separate from the
# IT-Operations pending-reply workflow above -- with a deliberate two-step
# lifecycle rather than every jotted task becoming a Zendesk ticket the
# instant it's typed:
#   draft -> just a local note, nothing in Zendesk yet (add_personal_task)
#   open  -> promoted into a real, self-requested/self-assigned Zendesk
#            ticket via the /tasks page's own "Make Zendesk Ticket from
#            Task" button (promote_personal_task) -- typed "task", one of
#            Zendesk's own native ticket types alongside
#            question/incident/problem, and tagged PERSONAL_TASK_TAG so
#            find_candidates() never mistakes it for a customer ticket
#            awaiting a first reply (see that function's own check)
#   done  -> the ticket was marked solved, either via this page's own
#            "Mark done" button or directly in Zendesk (complete_personal_
#            task / reconcile_personal_tasks)
# A draft can also be discarded outright (discard_personal_task) without
# ever touching Zendesk -- there's nothing there yet to lose track of.
#
# All three states share one local record, personal_tasks.json, mirroring
# pending_drafts.json's own load/update pattern (see
# _pending_lock/load_pending/update_pending above). Keyed by a locally-
# generated id (not the eventual ticket id -- a draft doesn't have one yet)
# so a task's identity is stable across its whole lifecycle. Once
# promoted, an "open" entry is deliberately NOT read back from Zendesk via
# a group-scoped query the way the IT-Security column is
# (fetch_recent_tickets): get_tickets()'s only filter is a single
# group_id, and there's no guarantee these tickets land in a group this
# tool already queries -- Zendesk auto-assigns a group at creation based
# on account defaults, not something this tool controls. Looking these up
# by the ticket ids already recorded here (via the existing single-ticket
# get_ticket()) sidesteps that entirely, and needs no changes to the
# shared zendesk_client.py library at all.
PERSONAL_TASKS_FILE = OUTPUT_DIR / "personal_tasks.json"
PERSONAL_TASKS_LOCK_FILE = OUTPUT_DIR / "personal_tasks.json.lock"
PERSONAL_TASKS_ARCHIVE_FILE = OUTPUT_DIR / "personal_tasks_archive.json"


def _personal_tasks_lock():
    lock_file = open(PERSONAL_TASKS_LOCK_FILE, "w")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def load_personal_tasks() -> dict:
    """Plain (unlocked) read, same tradeoff as load_pending() above -- fine
    for rendering the /tasks page. Mutate only through
    update_personal_tasks()."""
    if PERSONAL_TASKS_FILE.exists():
        try:
            return json.loads(PERSONAL_TASKS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _cached_load_personal_tasks() -> dict:
    """load_personal_tasks(), memoized for the lifetime of one request --
    see the comment above _request_cache (near load_pending(), above).
    Used only by draft_personal_tasks()/open_personal_tasks()/
    completed_personal_tasks() -- never by update_personal_tasks() or the
    pre-mutation freshness checks in promote/complete/quick-solve_personal_task
    or reconcile_personal_tasks(), which all still call
    load_personal_tasks() directly."""
    cached = getattr(_request_cache, "personal_tasks", None)
    if cached is None:
        cached = load_personal_tasks()
        _request_cache.personal_tasks = cached
    return cached


def update_personal_tasks(mutate_fn) -> None:
    """Read-modify-write personal_tasks.json under an exclusive lock --
    same reasoning as update_pending() above, in case two of these actions
    (an add, a promote, a mark-done, a reconcile pass) ever land close
    together. Also clears _request_cache's memoized copy right after
    writing -- see update_pending()'s docstring for why that matters
    beyond just the real HTTP request lifecycle."""
    lock_file = _personal_tasks_lock()
    try:
        data = load_personal_tasks()
        mutate_fn(data)
        _atomic_write_json(PERSONAL_TASKS_FILE, data)
    finally:
        lock_file.close()
    clear_request_cache()


def draft_personal_tasks() -> list:
    """The /tasks page's "not yet in Zendesk" list -- every locally-saved
    task that hasn't been promoted to a real ticket yet, in manual-reorder
    order (see move_personal_task_draft): highest `position` first. A
    fresh draft's position is one higher than any existing draft's (see
    add_personal_task), so with no reordering at all this still comes out
    newest-first, exactly as before manual reordering existed -- entries
    saved before `position` existed default to 0 and simply sort as if
    they were the oldest."""
    data = _cached_load_personal_tasks()
    items = [v for v in data.values() if v.get("status") == "draft"]
    return sorted(items, key=lambda i: i.get("position", 0), reverse=True)


def open_personal_tasks() -> list:
    """The /tasks page's "open" list -- every locally-tracked task that
    HAS been promoted to a real Zendesk ticket and not yet marked done,
    newest first. Purely a local-store read (like pending_items() above),
    no Zendesk call here -- see reconcile_personal_tasks() for the one
    place this store's status gets refreshed from Zendesk itself."""
    data = _cached_load_personal_tasks()
    items = [v for v in data.values() if v.get("status") == "open"]
    return sorted(items, key=lambda i: i.get("created_at", ""), reverse=True)


def completed_personal_tasks(limit: int = 10) -> list:
    """The /tasks page's "Recently completed" collapsible -- the most
    recently done tasks, most-recent first, capped at `limit` the same way
    recent_replied_items() caps the main queue's "Last N replies" section.
    Purely a local-store read; a task only ever lands here via
    complete_personal_task() or reconcile_personal_tasks() flipping its
    status to "done"."""
    data = _cached_load_personal_tasks()
    items = [v for v in data.values() if v.get("status") == "done"]
    items.sort(key=lambda i: i.get("completed_at", ""), reverse=True)
    return items[:limit]


def _parse_est_mins(est_mins) -> tuple:
    """Shared parsing/validation for the optional Estimated minutes value,
    used both when saving a draft (optional there) and when promoting one
    (required there). Blank/None is accepted as "not given" (returns
    (None, None)); anything else must be a whole number greater than 0 and
    no more than PERSONAL_TASK_EST_MINUTES_MAX -- catching an obvious
    fat-fingered value (e.g. 99999999) before it becomes ticket metadata,
    the same way the lower bound already catches 0/negative. Returns
    (parsed_int_or_None, error_or_None) -- never raises."""
    if est_mins in (None, ""):
        return None, None
    try:
        parsed = int(est_mins)
        if parsed <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return None, "Estimated minutes must be a whole number greater than 0 (or left blank)."
    if parsed > PERSONAL_TASK_EST_MINUTES_MAX:
        return None, f"Estimated minutes can't be more than {PERSONAL_TASK_EST_MINUTES_MAX} ({PERSONAL_TASK_EST_MINUTES_MAX // 60} hours)."
    return parsed, None


def add_personal_task(subject: str, notes: str, est_mins=None, category: str = "", log_fn=print, standup_action_id: str = "") -> dict:
    """Saves a personal task as a local-only draft -- no Zendesk call at
    all. This is deliberately the ONLY thing "Add task" does now: the
    first version of this feature created a real Zendesk ticket
    immediately on every add, which turned out to be too eager for
    quickly jotting down an idea that might not be worth tracking as a
    ticket. Creating the actual ticket is a separate, explicit action --
    see promote_personal_task().

    est_mins is OPTIONAL here (unlike at promote time, where it's
    required) -- if you already know roughly how long something will
    take when you jot it down, saving it now pre-fills the promote form's
    Estimated minutes input later instead of asking you to remember it a
    second time; leave it blank and the promote form just starts empty,
    exactly as before this existed. category is a short, purely local
    label (e.g. "ISP", "Reporting") for scanning a longer drafts list at a
    glance -- never sent to Zendesk.

    Every new draft gets a `position` one higher than any existing
    draft's, so draft_personal_tasks()'s default (unreordered) view is
    still newest-first, same as before manual reordering existed -- see
    move_personal_task_draft() for how that value changes afterward.

    standup_action_id (optional) links the draft back to the standup action
    item it came from (/standup's "Add to My tasks"). It's checked inside the
    same lock as the write, so a double-click can't create two drafts for
    one action item; see standup_tasks_by_action() for the archive check.

    Returns {"ok": True, "local_id": str, "error": None} on success, or
    {"ok": False, "error": str} if the subject is blank, est_mins is given
    but invalid, or that standup action item already has a task; never raises."""
    subject = (subject or "").strip()
    if not subject:
        return {"ok": False, "error": "A task needs a subject."}

    est_mins_int, est_mins_error = _parse_est_mins(est_mins)
    if est_mins_error:
        return {"ok": False, "error": est_mins_error}

    local_id = uuid.uuid4().hex[:12]
    duplicate = []

    def _mutate(data):
        if standup_action_id and any(v.get("standup_action_id") == standup_action_id for v in data.values()):
            duplicate.append(True)
            return
        existing_positions = [v.get("position", 0) for v in data.values() if v.get("status") == "draft"]
        position = (max(existing_positions) + 1) if existing_positions else 0
        data[local_id] = {
            "local_id": local_id,
            "subject": subject,
            "notes": (notes or "").strip(),
            "category": (category or "").strip(),
            "status": "draft",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "ticket_id": None,
            "ticket_created_at": None,
            "est_mins": est_mins_int,
            "position": position,
        }
        if standup_action_id:
            data[local_id]["standup_action_id"] = standup_action_id

    update_personal_tasks(_mutate)
    if duplicate:
        return {"ok": False, "error": "That standup action item is already in My tasks."}
    log_fn(f"Saved task draft {local_id}: {subject}")
    return {"ok": True, "local_id": local_id, "error": None}


def edit_personal_task(local_id: str, subject: str, notes: str, est_mins=None, category: str = "", log_fn=print) -> dict:
    """"Edit" on a draft row -- updates subject/notes/est_mins/category on
    an existing draft in place, so fixing a typo or adding a detail
    doesn't mean discarding and re-adding the whole task from scratch.
    Refuses to edit anything that isn't still a draft -- once a task is
    promoted, its subject/description already exist for real in Zendesk,
    so changing them here would just silently drift out of sync with the
    actual ticket; edit those directly in Zendesk instead.

    Returns {"ok": True, "error": None} on success, or {"ok": False,
    "error": str}; never raises.

    Guarded by _begin_personal_task_action/_end_personal_task_action, the
    same in-process guard promote_personal_task/complete_personal_task/
    quick_solve_personal_task use -- not because this function itself makes
    any network call (it doesn't), but because promote_personal_task's own
    "still a draft?" check happens on an unlocked read, well before its
    live Zendesk create_ticket call finishes and it writes "open" back.
    Without this guard, an edit landing in that window would rewrite
    subject/notes/est_mins on a draft that a moment later gets promoted
    with the OLD (pre-edit) values already captured into local variables --
    leaving the Zendesk ticket's actual subject/description permanently out
    of sync with what this tool shows. Taking the same guard here closes
    that window: the edit either finishes well before the promote starts,
    or waits until the promote (and its own guard) is done and re-reads the
    then-current status."""
    subject = (subject or "").strip()
    if not subject:
        return {"ok": False, "error": "A task needs a subject."}

    est_mins_int, est_mins_error = _parse_est_mins(est_mins)
    if est_mins_error:
        return {"ok": False, "error": est_mins_error}

    if not _begin_personal_task_action(local_id):
        return {"ok": False, "error": "This task is currently being promoted or solved -- please wait a moment and refresh."}

    try:
        result = {"ok": False, "error": "No such task."}

        def _mutate(data):
            nonlocal result
            entry = data.get(local_id)
            if entry is None:
                result = {"ok": False, "error": "No such task."}
                return
            if entry.get("status") != "draft":
                result = {"ok": False, "error": "This task already has a Zendesk ticket -- edit it directly in Zendesk instead."}
                return
            entry["subject"] = subject
            entry["notes"] = (notes or "").strip()
            entry["category"] = (category or "").strip()
            entry["est_mins"] = est_mins_int
            result = {"ok": True, "error": None}

        update_personal_tasks(_mutate)
        if result["ok"]:
            log_fn(f"Edited task draft {local_id}.")
        return result
    finally:
        _end_personal_task_action(local_id)


def move_personal_task_draft(local_id: str, direction: str, log_fn=print) -> dict:
    """"Move up"/"Move down" on a draft row -- manually reorders the
    "Not yet in Zendesk" list. Implemented as a plain swap of `position`
    with whichever neighboring draft is currently adjacent in the
    displayed (position-descending) order, rather than renumbering the
    whole list -- simpler, and every other draft's relative order is
    left untouched. A no-op (ok=True, nothing changes) if the draft is
    already at the top/bottom -- there's nothing to swap with in that
    direction. Refuses anything that isn't currently a draft.

    Returns {"ok": True, "error": None} on success (including the no-op
    case), or {"ok": False, "error": str}; never raises.

    Guarded by _begin_personal_task_action/_end_personal_task_action, same
    reasoning as edit_personal_task above -- swapping `position` with a
    neighbor while that neighbor (or this draft itself) is mid-promote in
    another thread could otherwise land in between promote's own unlocked
    "still a draft?" read and its later write, reordering a draft that's
    about to stop being one."""
    if direction not in ("up", "down"):
        return {"ok": False, "error": "Invalid direction."}

    if not _begin_personal_task_action(local_id):
        return {"ok": False, "error": "This task is currently being promoted or solved -- please wait a moment and refresh."}

    try:
        result = {"ok": False, "error": "No such task."}

        def _mutate(data):
            nonlocal result
            entry = data.get(local_id)
            if entry is None:
                result = {"ok": False, "error": "No such task."}
                return
            if entry.get("status") != "draft":
                result = {"ok": False, "error": "Only drafts can be reordered."}
                return
            drafts = sorted(
                (v for v in data.values() if v.get("status") == "draft"),
                key=lambda i: i.get("position", 0), reverse=True,
            )
            idx = next((i for i, d in enumerate(drafts) if d["local_id"] == local_id), None)
            if idx is None:
                result = {"ok": False, "error": "No such task."}
                return
            neighbor_idx = idx - 1 if direction == "up" else idx + 1
            if neighbor_idx < 0 or neighbor_idx >= len(drafts):
                result = {"ok": True, "error": None}
                return
            neighbor = drafts[neighbor_idx]
            entry["position"], neighbor["position"] = neighbor.get("position", 0), entry.get("position", 0)
            result = {"ok": True, "error": None}

        update_personal_tasks(_mutate)
        return result
    finally:
        _end_personal_task_action(local_id)


def discard_personal_task(local_id: str, log_fn=print) -> dict:
    """Removes a draft task that was never promoted to a real ticket --
    there's nothing in Zendesk to lose track of, unlike an "open" task
    (which always has a real ticket behind it, and can only be closed out
    via complete_personal_task, never silently untracked here). Refuses to
    discard anything that isn't still a draft, so this can never be used
    to make an already-created ticket quietly disappear from the list.

    Returns {"ok": True, "error": None} on success, or {"ok": False,
    "error": str}; never raises.

    Guarded by _begin_personal_task_action/_end_personal_task_action, same
    reasoning as edit_personal_task above -- this is the more serious case
    of the two: promote_personal_task reads a draft, then makes its live
    create_ticket/update_ticket calls, and only writes "open" back at the
    very end. A discard landing in that window would find the entry still
    "draft" (promote hasn't flipped it yet) and delete it outright -- and
    when promote's own final write later runs, it finds nothing left to
    update (its target-not-None guard just no-ops), leaving a real,
    already-created Zendesk ticket with no local record of it at all. This
    guard closes that window the same way it closes the double-promote one."""
    if not _begin_personal_task_action(local_id):
        return {"ok": False, "error": "This task is currently being promoted or solved -- please wait a moment and refresh."}

    try:
        result = {"ok": False, "error": "No such task."}

        def _mutate(data):
            nonlocal result
            entry = data.get(local_id)
            if entry is None:
                result = {"ok": False, "error": "No such task."}
                return
            if entry.get("status") != "draft":
                result = {
                    "ok": False,
                    "error": f"This task already has a Zendesk ticket (#{entry.get('ticket_id')}) -- use Mark done instead.",
                }
                return
            del data[local_id]
            result = {"ok": True, "error": None}

        update_personal_tasks(_mutate)
        if result["ok"]:
            log_fn(f"Discarded task draft {local_id}.")
        return result
    finally:
        _end_personal_task_action(local_id)


# promote_personal_task/complete_personal_task both do real Zendesk writes
# (create_ticket / update_ticket) that must never happen twice for the same
# local task -- but they can't just check-then-mutate under
# update_personal_tasks' file lock the way every other action above does,
# since holding that lock across a live network call would block every
# other unrelated task mutation for however long Zendesk takes to respond.
# review_server.py runs on a ThreadingHTTPServer (see main() there), so two
# overlapping requests for the same local_id are a real scenario -- a
# double-click before the page's own button-disable JS fires, or a browser
# retrying a slow request -- not just a theoretical race. This in-process
# guard (reset on restart, and irrelevant across two separate processes,
# neither of which is a real scenario for this single-instance,
# launchd-managed, localhost tool) closes that specific window: only one
# thread at a time may be mid-promote/mid-complete for a given local_id.
_personal_task_action_lock = threading.Lock()
_personal_task_actions_in_progress: set = set()


def _begin_personal_task_action(local_id: str) -> bool:
    """Returns True if the caller may proceed (and must call
    _end_personal_task_action(local_id) in a finally block once done),
    False if another thread is already mid-action on this same local_id."""
    with _personal_task_action_lock:
        if local_id in _personal_task_actions_in_progress:
            return False
        _personal_task_actions_in_progress.add(local_id)
        return True


def _end_personal_task_action(local_id: str) -> None:
    with _personal_task_action_lock:
        _personal_task_actions_in_progress.discard(local_id)


def promote_personal_task(
    client, local_id: str, agent_user_id, est_mins,
    identity_assignee=None, identity_requester=None, allocation=None,
    log_fn=print,
) -> dict:
    """"Make Zendesk Ticket from Task" -- takes an existing local draft
    (see add_personal_task) and creates the real Zendesk ticket for it,
    following the same recipe as the sibling "Zendesk Personal Task
    Tracker" Claude skill: priority "low", type="task", tagged
    PERSONAL_TASK_TAG so find_candidates() never mistakes it for a
    customer ticket awaiting a first reply, and the IT Requests form's
    three custom fields set via _personal_task_custom_fields(est_mins,
    allocation). Also always filed under the IT_OPERATIONS_GROUP_ID group
    (set via a follow-up update_ticket call, since create_ticket has no
    group_id parameter) regardless of which Assignee/Requester identity is
    chosen -- so every promoted task reads as "IT-Operations / <assignee>"
    in Zendesk, the same group/agent pairing its own UI shows.

    identity_assignee/identity_requester (each PERSONAL_TASK_IDENTITY_SELF
    or PERSONAL_TASK_IDENTITY_IT_OPERATIONS, defaulting to SELF -- the
    only option that existed before this choice did) are resolved
    independently via resolve_personal_task_identity, so Assignee and
    Requester don't have to match each other. allocation
    (PERSONAL_TASK_ALLOCATION_CSA or _CSAIF, defaulting to CSA) feeds
    straight into _personal_task_custom_fields.

    description falls back to the subject itself when notes is blank, so
    the ticket body is never empty -- Zendesk requires a non-empty
    description to create a ticket at all.

    Refuses to promote a task that already has a ticket (idempotency
    guard against a double-click creating two tickets for one draft) --
    the local entry's status is the source of truth for whether this has
    already happened, checked and flipped under the same lock as every
    other mutation here. Also validates est_mins/identity/allocation
    itself rather than trusting the web form's own required attributes
    and fixed <select> options, since those are only a UI nicety a raw
    request could still arrive without honoring.

    Deliberately does NOT post PERSONAL_TASK_RECEIVED_NOTE/
    PERSONAL_TASK_RECEIVED_REPLY at creation time -- unlike the sibling
    Claude skill, this tool defers both comments until the ticket is
    actually solved (see quick_solve_personal_task), so a freshly-created
    personal task ticket sits quietly with no public/internal activity
    until someone deliberately closes it out.

    On success, updates the local entry in place (status "open",
    ticket_id/ticket_created_at/est_mins/allocation/assignee_identity/
    requester_identity set -- the last three so complete_personal_task
    can resend the same allocation at solve time) so the /tasks page's
    open list picks it up immediately. Returns {"ok": True,
    "ticket_id": int, "url": str, "error": None, "warning": str|None} on
    success, or {"ok": False, "error": str} on failure (missing draft,
    already promoted, an unresolved identity, invalid est_mins/allocation,
    or the Zendesk create call itself failing); never raises.

    Guarded by _begin_personal_task_action/_end_personal_task_action (see
    those functions' comment above) against two overlapping requests for
    the same local_id both passing the "still a draft?" check below before
    either has written "open" back -- a real scenario on this server's
    ThreadingHTTPServer, not just a theoretical one. That guard is what
    actually makes "checked under the same lock as every other mutation
    here" true for the live-Zendesk-write step; update_personal_tasks'
    file lock alone only protects the final status flip, not the
    create_ticket call in between."""
    if not _begin_personal_task_action(local_id):
        return {"ok": False, "error": "This task is already being promoted -- please wait a moment and refresh."}

    try:
        identity_assignee = identity_assignee or PERSONAL_TASK_IDENTITY_SELF
        identity_requester = identity_requester or PERSONAL_TASK_IDENTITY_SELF
        allocation = allocation or PERSONAL_TASK_ALLOCATION_VALUE

        data = load_personal_tasks()
        entry = data.get(local_id)
        if entry is None:
            return {"ok": False, "error": "No such task."}
        if entry.get("status") != "draft":
            if entry.get("ticket_id"):
                return {"ok": False, "error": f"This task already has a Zendesk ticket (#{entry['ticket_id']})."}
            return {"ok": False, "error": "This task can't be promoted from its current state."}

        if identity_assignee not in PERSONAL_TASK_IDENTITY_CHOICES or identity_requester not in PERSONAL_TASK_IDENTITY_CHOICES:
            return {"ok": False, "error": "Invalid Assignee/Requester choice."}
        if allocation not in PERSONAL_TASK_ALLOCATION_CHOICES:
            return {"ok": False, "error": "Invalid Ticket Allocation choice."}

        est_mins_int, est_mins_error = _parse_est_mins(est_mins)
        if est_mins_error or est_mins_int is None:
            return {"ok": False, "error": est_mins_error or "Estimated minutes is required to create the ticket."}

        assignee_id, assignee_error = resolve_personal_task_identity(client, identity_assignee, agent_user_id, log_fn=log_fn)
        if assignee_error:
            return {"ok": False, "error": assignee_error}
        requester_id, requester_error = resolve_personal_task_identity(client, identity_requester, agent_user_id, log_fn=log_fn)
        if requester_error:
            return {"ok": False, "error": requester_error}

        subject = entry["subject"]
        description = (entry.get("notes") or "").strip() or subject

        try:
            created = client.create_ticket(
                subject=subject,
                description=description,
                requester_id=requester_id,
                assignee_id=assignee_id,
                priority=PERSONAL_TASK_PRIORITY,
                type="task",
                tags=[PERSONAL_TASK_TAG],
                custom_fields=_personal_task_custom_fields(est_mins_int, allocation),
            )
        except Exception as exc:
            log_fn(f"promote_personal_task({local_id}) failed: {exc}")
            return {"ok": False, "error": str(exc)}

        ticket_id = created.get("id")

        # Every personal task ticket is filed under the IT-Operations group by
        # default (so it shows up as "IT-Operations / <assignee>" the same way
        # Zendesk's own UI pairs a ticket's group and individual assignee),
        # regardless of which Assignee/Requester identity was chosen above --
        # these are IT-Operations work items either way. create_ticket (the
        # shared zendesk_client.py method, deliberately never modified by this
        # project) has no group_id parameter, so this is set via a follow-up
        # update_ticket call right after creation, the same "create, then a
        # couple of small follow-up writes" shape already used for the two
        # automatic comments below. A failure here doesn't undo the ticket or
        # block the promotion -- it's folded into the same best-effort
        # "warning" the comment posts already use.
        group_warning = None
        try:
            client.update_ticket(ticket_id, group_id=IT_OPERATIONS_GROUP_ID)
        except Exception as exc:
            log_fn(f"promote_personal_task({local_id}): setting group on #{ticket_id} failed: {exc}")
            group_warning = "the ticket was created, but assigning it to the IT-Operations group may not have worked -- check the ticket directly"

        def _mutate(data):
            target = data.get(local_id)
            if target is not None:
                target["status"] = "open"
                target["ticket_id"] = ticket_id
                target["ticket_created_at"] = datetime.now().isoformat(timespec="seconds")
                target["est_mins"] = est_mins_int
                target["allocation"] = allocation
                target["assignee_identity"] = identity_assignee
                target["requester_identity"] = identity_requester

        update_personal_tasks(_mutate)
        log_fn(f"Promoted task draft {local_id} to Zendesk ticket #{ticket_id}.")

        return {"ok": True, "ticket_id": ticket_id, "url": ticket_url(ticket_id), "error": None, "warning": group_warning}
    finally:
        _end_personal_task_action(local_id)


def _resolve_est_mins_for_solve(entry: dict, est_mins_override) -> tuple:
    """A task promoted before Estimated minutes existed (see
    _personal_task_custom_fields' docstring) has no est_mins recorded at
    all -- and unlike at creation time, where the field can just be left
    out of custom_fields entirely, the skill's own experience is that
    Zendesk rejects a SOLVE that omits any of the three IT Requests
    fields. So a legacy task like that can't actually be solved without
    finally getting an Estimated minutes value from somewhere. This
    resolves that: the entry's own recorded est_mins always wins when
    present (a routine solve click never silently overwrites it), and
    only when there's truly none on file does est_mins_override -- an
    estimate collected right on the solve form itself, the same
    required-at-this-point pattern the promote form already uses -- get
    validated and used instead.

    Returns (resolved_est_mins, error_or_None). resolved_est_mins is
    always a valid int when error_or_None is None."""
    if entry.get("est_mins") is not None:
        return entry["est_mins"], None
    if est_mins_override is None or str(est_mins_override).strip() == "":
        return None, "This task was created before Estimated minutes was required -- enter an estimate to solve it."
    parsed, err = _parse_est_mins(est_mins_override)
    if err or parsed is None:
        return None, err or "Estimated minutes is required to solve this task."
    return parsed, None


def _solve_personal_task_ticket(client, local_id: str, entry: dict, log_fn=print) -> dict:
    """Shared solve+local-mutate core used by both complete_personal_task
    ("Mark done") and quick_solve_personal_task ("Quick Solve") -- pulled
    out on its own precisely so neither of those two has to call the
    other directly. Both callers already hold the single non-reentrant
    _begin_personal_task_action(local_id) guard for the duration of their
    own call; if one called the other internally, the second guard
    acquisition for the same local_id would immediately fail. This helper
    does the actual Zendesk write and local-state flip with NO guard of
    its own -- callers are required to already hold it.

    Resends the same three IT Requests custom fields as at creation time
    (via _personal_task_custom_fields, reusing the est_mins AND
    allocation recorded back in promote_personal_task, or freshly resolved
    by _resolve_est_mins_for_solve for a legacy task that had none) --
    the skill notes Zendesk rejects a solve that omits any of them.
    entry["est_mins"] is also written back into the stored record here
    (not just used for this one Zendesk call), so a legacy task's
    just-collected estimate is remembered going forward rather than
    needing to be re-entered on every future retry. Returns
    {"ok": True, "error": None} or {"ok": False, "error": str}; never
    raises. Does not itself check entry["status"] -- callers do that
    first, since the exact wording of "not open" differs slightly between
    them."""
    ticket_id = entry["ticket_id"]
    est_mins = entry.get("est_mins")
    allocation = entry.get("allocation")
    try:
        client.update_ticket(
            int(ticket_id),
            status="solved",
            custom_fields=_personal_task_custom_fields(est_mins, allocation),
        )
    except Exception as exc:
        log_fn(f"_solve_personal_task_ticket({local_id}, #{ticket_id}) failed: {exc}")
        return {"ok": False, "error": str(exc)}

    def _mutate(data):
        target = data.get(local_id)
        if target is not None:
            target["status"] = "done"
            target["completed_at"] = datetime.now().isoformat(timespec="seconds")
            if est_mins is not None:
                target["est_mins"] = est_mins

    update_personal_tasks(_mutate)
    log_fn(f"Marked task {local_id} (Zendesk #{ticket_id}) done.")
    return {"ok": True, "error": None}


def complete_personal_task(client, local_id: str, est_mins=None, log_fn=print) -> dict:
    """Marks a promoted task's Zendesk ticket solved and its local record
    "done" in the same call -- mirrors claim_ticket()'s pattern above (a
    real Zendesk write, immediately followed by an in-place local-state
    update so the /tasks page reflects it right away rather than waiting
    on the next reconcile pass). Refuses to act on anything that isn't
    currently "open" (a draft has no ticket to mark solved; an
    already-done task gets its own distinct message rather than the
    misleading "doesn't have a ticket yet", which is really about drafts).
    This is the "solve only" path -- it does NOT post any comments; see
    quick_solve_personal_task for the variant that also posts the
    intake note/reply first.

    est_mins is only used for the rare legacy task with no est_mins
    already on file -- see _resolve_est_mins_for_solve; for every normal
    task (the vast majority, which already have one recorded from promote
    time) it's ignored entirely and the stored value is used as before.

    Returns {"ok": True, "error": None} or {"ok": False, "error": str};
    never raises. Guarded by _begin_personal_task_action (see that
    function's comment above) -- lower stakes here since Zendesk's own
    status="solved" update is idempotent, but two overlapping Mark Done
    clicks would otherwise still both pass the "still open?" check and
    both hit Zendesk for no reason."""
    if not _begin_personal_task_action(local_id):
        return {"ok": False, "error": "This task is already being marked done -- please wait a moment and refresh."}

    try:
        data = load_personal_tasks()
        entry = data.get(local_id)
        if entry is None:
            return {"ok": False, "error": "No such task."}
        if entry.get("status") != "open":
            if entry.get("status") == "done":
                return {"ok": False, "error": "This task has already been marked done."}
            return {"ok": False, "error": "This task doesn't have a Zendesk ticket yet -- create one first."}

        resolved_est_mins, est_mins_error = _resolve_est_mins_for_solve(entry, est_mins)
        if est_mins_error:
            return {"ok": False, "error": est_mins_error}

        return _solve_personal_task_ticket(client, local_id, dict(entry, est_mins=resolved_est_mins), log_fn=log_fn)
    finally:
        _end_personal_task_action(local_id)


def quick_solve_personal_task(client, local_id: str, est_mins=None, log_fn=print) -> dict:
    """"Quick Solve" -- the one-click alternative to "Mark done" for a
    promoted task whose ticket has had no public reply or internal
    comment posted on it yet (see promote_personal_task, which
    deliberately no longer posts these two at creation time). Posts both
    PERSONAL_TASK_RECEIVED_NOTE (internal) and PERSONAL_TASK_RECEIVED_REPLY
    (public) -- the same fixed pair the skill posts, just deferred to
    this later point -- and then solves the ticket via the same shared
    _solve_personal_task_ticket helper complete_personal_task uses.

    est_mins is only used for the rare legacy task with none on file
    already -- see _resolve_est_mins_for_solve and complete_personal_task's
    own docstring, which explains this the same way.

    The comment posts are best-effort: if either fails, this still goes
    on to solve the ticket (a missing intake note/reply isn't worth
    blocking the actual solve over) and the returned dict's "warning" key
    explains what to double-check manually -- the same pattern
    promote_personal_task previously used for its own follow-up posts.
    If the solve itself fails, that failure IS returned as an error (the
    comments may already be posted, but the ticket is not marked done).

    Each of the two comments' posted-or-not state is recorded on the
    entry itself (intake_note_posted / intake_reply_posted) the moment it
    succeeds -- BEFORE attempting the solve, and independently of whether
    the solve then succeeds or fails. Without this, a solve failure after
    both comments already posted would leave the task still "open", and
    clicking Quick Solve again to retry would post both comments a SECOND
    time before retrying the solve -- this makes that retry skip whichever
    comment(s) already went out on an earlier attempt, posting only
    whatever didn't.

    Refuses to act on anything that isn't currently "open", the same
    guard complete_personal_task applies (including the same distinct
    "already done" message). Returns {"ok": True, "error": None,
    "warning": str|None, "comments_posted_this_call": bool} on success, or
    {"ok": False, "error": str} on failure; never raises.

    Guarded by _begin_personal_task_action/_end_personal_task_action --
    the same single non-reentrant guard complete_personal_task and
    promote_personal_task use, which is exactly why this function calls
    _solve_personal_task_ticket directly rather than complete_personal_task
    itself (see that helper's docstring)."""
    if not _begin_personal_task_action(local_id):
        return {"ok": False, "error": "This task is already being solved -- please wait a moment and refresh."}

    try:
        data = load_personal_tasks()
        entry = data.get(local_id)
        if entry is None:
            return {"ok": False, "error": "No such task."}
        if entry.get("status") != "open":
            if entry.get("status") == "done":
                return {"ok": False, "error": "This task has already been marked done."}
            return {"ok": False, "error": "This task doesn't have a Zendesk ticket yet -- create one first."}

        resolved_est_mins, est_mins_error = _resolve_est_mins_for_solve(entry, est_mins)
        if est_mins_error:
            return {"ok": False, "error": est_mins_error}

        ticket_id = entry["ticket_id"]
        note_already_posted = bool(entry.get("intake_note_posted"))
        reply_already_posted = bool(entry.get("intake_reply_posted"))
        note_posted_now = False
        reply_posted_now = False
        comment_warning = None
        try:
            if not note_already_posted:
                client.post_comment(ticket_id, PERSONAL_TASK_RECEIVED_NOTE, public=False)
                note_posted_now = True
            if not reply_already_posted:
                client.post_comment(ticket_id, PERSONAL_TASK_RECEIVED_REPLY, public=True)
                reply_posted_now = True
        except Exception as exc:
            log_fn(f"quick_solve_personal_task({local_id}): intake note/reply on #{ticket_id} failed: {exc}")
            comment_warning = "the ticket was solved, but the automatic internal note/reply may not have posted -- check the ticket directly"

        if note_posted_now or reply_posted_now:
            # Recorded right away, before the solve call below even runs --
            # if update_ticket then fails, a retry must not re-post
            # whichever of these just succeeded.
            def _mark_posted(data):
                target = data.get(local_id)
                if target is not None:
                    if note_posted_now:
                        target["intake_note_posted"] = True
                    if reply_posted_now:
                        target["intake_reply_posted"] = True

            update_personal_tasks(_mark_posted)

        result = _solve_personal_task_ticket(client, local_id, dict(entry, est_mins=resolved_est_mins), log_fn=log_fn)
        if not result["ok"]:
            return result

        result["warning"] = comment_warning
        result["comments_posted_this_call"] = note_posted_now or reply_posted_now
        return result
    finally:
        _end_personal_task_action(local_id)


def reconcile_personal_tasks(client, log_fn=print) -> None:
    """Catches the one case complete_personal_task() above can't: a
    promoted task's ticket solved or closed directly in Zendesk (or by any
    other means) rather than via this page's own "Mark done"/"Quick Solve"
    buttons, which would otherwise leave a stale "open" entry sitting in
    the /tasks list forever. Called once per /tasks page load -- a handful
    of open tasks at most (this is a personal to-do list, not a queue), so
    a get_ticket() per entry stays cheap. Fails per-ticket, not per-pass,
    mirroring recheck_pending_for_assignment()'s posture above: one
    ticket's lookup erroring leaves that entry untouched rather than
    discarding the whole reconcile pass. Drafts (no ticket_id yet) are
    skipped entirely -- there's nothing in Zendesk to check. Also archives
    old completed tasks (see archive_old_personal_tasks) at the end of the
    same pass, since this already runs on every /tasks page load and needs
    no Zendesk call to do it.

    Deliberately does its get_ticket() calls on a plain unlocked read
    first, and only takes update_personal_tasks' exclusive file lock
    afterward for the (fast, no-network) write -- NOT the other way
    around. An earlier version ran every get_ticket() call from inside the
    update_personal_tasks _mutate callback itself, which meant the file
    lock stayed held for the full duration of every one of those live
    Zendesk round trips. Since this runs on every single /tasks page load
    (unlike the 2-minute cron cadence recheck_pending_for_assignment()
    above has the luxury of), that meant simply loading /tasks with a slow
    Zendesk connection could block someone else's add/edit/discard/
    promote/complete/quick-solve click for however long every open task's
    lookup took, combined. Splitting the network reads and the write into
    two passes (re-checking each entry's status is STILL "open" right
    before flipping it, in case something else already moved it in the
    meantime) keeps the exclusive lock held only for the quick in-memory
    part, same posture as every other action in this file that touches
    live Zendesk."""
    data = load_personal_tasks()
    newly_solved_local_ids = []
    for local_id, entry in data.items():
        if entry.get("status") != "open":
            continue
        ticket_id = entry.get("ticket_id")
        try:
            ticket = client.get_ticket(int(ticket_id))
        except Exception as exc:
            log_fn(f"reconcile_personal_tasks(#{ticket_id}): get_ticket failed ({exc}) -- leaving as-is")
            continue
        if ticket.get("status") in ("solved", "closed"):
            newly_solved_local_ids.append(local_id)

    if newly_solved_local_ids:
        def _mutate(data):
            for local_id in newly_solved_local_ids:
                entry = data.get(local_id)
                # Re-checked fresh, under the lock, rather than trusting
                # the unlocked read above -- a Quick Solve/Mark done click
                # (or a second reconcile pass) could have already moved
                # this entry off "open" in the meantime; only flip it if
                # it's STILL exactly the state this decision was based on.
                if entry is not None and entry.get("status") == "open":
                    entry["status"] = "done"
                    entry["completed_at"] = datetime.now().isoformat(timespec="seconds")

        update_personal_tasks(_mutate)
    archive_old_personal_tasks(log_fn=log_fn)


def archive_old_personal_tasks(after_days: int = ARCHIVE_AFTER_DAYS, log_fn=print) -> int:
    """Moves personal_tasks.json entries with status "done" whose
    completed_at is older than after_days into
    personal_tasks_archive.json (see _archive_terminal_entries for the
    shared mechanics). Never touches a "draft" or "open" entry regardless
    of age -- only a task that's actually finished is eligible."""
    def is_archivable(entry):
        return entry.get("status") == "done"

    def entry_timestamp(entry):
        return entry.get("completed_at") or entry.get("created_at")

    count = _archive_terminal_entries(
        PERSONAL_TASKS_FILE, PERSONAL_TASKS_ARCHIVE_FILE, _personal_tasks_lock, is_archivable, entry_timestamp, after_days, log_fn=log_fn,
    )
    if count:
        log_fn(f"Archived {count} completed personal task(s) from personal_tasks.json (older than {after_days} days).")
    return count


# --- Personal activity snapshot for /overview ------------------------------
# Replaces the old /sla page's team-wide SLA-compliance metrics (and its
# "Your activity" per-agent charts), both of which read the sibling
# sla-report-slack repo's precomputed sla_history.json -- removed entirely
# along with that page, rather than kept around unused. This is a much
# smaller thing: a purely local "how did last week go" figure for the
# Overview page, computed from data this tool already has on hand
# (pending_drafts.json) and nothing else -- no sibling repo, no GitHub
# Actions, no separate history file to keep fresh.


def _previous_business_week_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The most recently completed Monday-through-Friday business week, as
    a half-open [start, end) range -- start is last Monday at midnight, end
    is last Saturday at midnight (i.e. the instant last week's business
    days ended; whatever happened over that weekend is deliberately
    excluded, same as it would be from a "last week at the office" figure).
    A fixed calendar range rather than a trailing "last N days" window on
    purpose: it reads the same all week long (doesn't creep forward a day
    at a time the way a rolling window would) and always means the one
    full business week just before whichever week `now` falls in, in a
    "here's how last week went" sense. `now` is a parameter (defaulting to
    datetime.now()) so tests can pin it instead of depending on the real
    clock."""
    now = now or datetime.now()
    start_of_this_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_of_last_week = start_of_this_week - timedelta(days=7)
    end_of_last_business_week = start_of_last_week + timedelta(days=5)  # last Saturday, 00:00
    return start_of_last_week, end_of_last_business_week


def personal_activity_summary(now: datetime | None = None) -> dict:
    """How many replies (public or internal) this tool posted during last
    business week (the most recently completed Monday-through-Friday
    business week -- see _previous_business_week_bounds; whatever happened
    over that weekend doesn't count), and how long they sat waiting on
    average -- from posted_at minus created_at on each pending_drafts.json
    entry with status "posted". created_at here is this tool's own "first
    seen" timestamp (set in merge_candidates when a candidate is first
    drafted), not the ticket's original Zendesk creation time, so this
    measures "how long did it sit in MY queue," a genuinely different
    (and, for a personal pace check, more useful) number than the SLA
    page's old ticket-age-based compliance figures. Don't present
    avg_hours as an SLA or compliance measurement -- it isn't one.

    Returns {"count": int, "public_count": int, "internal_count": int,
    "avg_hours": float|None, "week_start": date, "week_end": date}.
    week_start/week_end are date objects for last Monday and last Friday
    (inclusive on both ends), handy for labeling the figure with the
    actual date range it covers. avg_hours is None when there's nothing
    to average (zero posted items in the window, or every one of them is
    missing a parseable created_at/posted_at pair -- old enough entries
    predating one of those fields, in practice)."""
    week_start, week_end_exclusive = _previous_business_week_bounds(now)
    data = _cached_load_pending()
    public_count = 0
    internal_count = 0
    wait_hours = []
    for entry in data.values():
        if entry.get("status") != "posted":
            continue
        try:
            posted_dt = datetime.fromisoformat(entry.get("posted_at") or "")
        except ValueError:
            continue
        if not (week_start <= posted_dt < week_end_exclusive):
            continue
        if entry.get("posted_public"):
            public_count += 1
        else:
            internal_count += 1
        try:
            created_dt = datetime.fromisoformat(entry.get("created_at") or "")
        except ValueError:
            continue
        wait_hours.append((posted_dt - created_dt).total_seconds() / 3600)

    return {
        "count": public_count + internal_count,
        "public_count": public_count,
        "internal_count": internal_count,
        "avg_hours": (sum(wait_hours) / len(wait_hours)) if wait_hours else None,
        "week_start": week_start.date(),
        "week_end": (week_end_exclusive - timedelta(days=1)).date(),
    }
