"""Shared helpers for the Zendesk local automation scripts (first_reply_check.py
and review_server.py). Kept in one place so both scripts resolve paths, load
config, and touch the shared pending-drafts store the same way -- the launchd
OAuth bug earlier this session was caused by exactly this kind of logic being
duplicated (and drifting) across files, so this time there's one source of
truth both scripts import from.
"""

import fcntl
import json
import os
import re
import sys
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


def make_client() -> ZendeskClient:
    return ZendeskClient(
        subdomain=os.getenv("ZENDESK_SUBDOMAIN"),
        email=os.getenv("ZENDESK_EMAIL"),
        token=os.getenv("ZENDESK_API_KEY"),
        client_id=os.getenv("ZENDESK_CLIENT_ID"),
        client_secret=os.getenv("ZENDESK_CLIENT_SECRET"),
        redirect_uri=os.getenv("ZENDESK_REDIRECT_URI", "https://localhost/callback"),
        oauth_scopes=os.getenv("ZENDESK_OAUTH_SCOPES", "read write"),
        oauth_token_path=resolve_token_path(),
        timeout=int(os.getenv("ZENDESK_TIMEOUT_SECONDS", "30")),
    )


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


def update_pending(mutate_fn) -> None:
    """Read-modify-write the pending store under an exclusive lock, so the
    cron script (adding new candidates every 2 minutes) and the review server
    (marking approve/reject whenever you click) can never clobber each
    other's concurrent writes. mutate_fn receives the current dict and
    mutates it in place."""
    lock_file = _pending_lock()
    try:
        data = load_pending()
        mutate_fn(data)
        PENDING_FILE.write_text(json.dumps(data, indent=2))
    finally:
        lock_file.close()


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
    data = load_pending()
    return [v for v in data.values() if v.get("status") == "pending"]


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


def mark_auth_broken(reason: str) -> None:
    AUTH_STATE_FILE.write_text(
        json.dumps(
            {"ok": False, "reason": reason, "checked_at": datetime.now().isoformat(timespec="seconds")}
        )
    )


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


def resolve_requester_name(client, requester_id, description: str, log_fn=print):
    """Resolve the requester's name the reliable way first: a direct
    get_user(requester_id) lookup, which asks Zendesk for the exact user
    record behind this ticket's requester_id -- no text-parsing, no
    first-name collision risk, since we're matching on an exact numeric id
    rather than guessing from a name that might appear in the ticket body.
    This is why ticket #156312 (whose description was just "This is a test
    ticket." -- nothing name-shaped to parse) can still show a real name.

    Falls back to guess_requester_name()'s text-parsing heuristic only if the
    direct lookup itself fails (requester_id missing, the user record 404s,
    or the API call errors for any other reason) -- so a broken/missing
    lookup degrades gracefully instead of leaving the card with nothing.

    Returns (name, confidence, reason) -- same shape as guess_requester_name,
    plus one new reason value: "direct_lookup", paired with confidence
    "confirmed", for the normal/successful path."""
    if requester_id:
        try:
            user = client.get_user(requester_id)
        except Exception as exc:
            log_fn(f"get_user({requester_id}) failed ({exc}); falling back to text-based guess")
        else:
            name = user.get("name")
            if name:
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


def build_description_excerpt(description: str, max_len: int = 240) -> str:
    """Collapse whitespace/newlines into a single readable preview line, so
    the review page can show what the person actually asked without you
    needing to click through to Zendesk to find out."""
    text = " ".join((description or "").split())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "..."
    return text


def shorten_subject(subject: str) -> str:
    """Strip bracket tags, prefer a clean identifier (CVE number, etc.) over
    a restated description."""
    cve = re.search(r"CVE-\d{4}-\d+", subject)
    if cve:
        return cve.group(0)
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", subject).strip()
    return cleaned or subject


def find_candidates(client, log_fn=print) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    candidates = []
    page = 1
    while True:
        result = client.get_tickets(
            group_id=IT_OPERATIONS_GROUP_ID,
            sort_by="created_at",
            sort_order="desc",
            per_page=25,
            page=page,
        )
        tickets = result.get("tickets", [])
        if not tickets:
            break

        stop_paging = False
        for ticket in tickets:
            created = datetime.fromisoformat(ticket["created_at"].replace("Z", "+00:00"))
            if created < cutoff:
                stop_paging = True
                break
            if ticket.get("status") not in ("new", "open"):
                continue
            if "personal_task" in (ticket.get("tags") or []):
                continue

            try:
                comments = client.get_ticket_comments(ticket["id"])
            except Exception as exc:
                # One ticket failing to fetch shouldn't discard every
                # candidate already found this run -- log it and move on.
                log_fn(f"Could not check comments for #{ticket['id']} ({exc}) -- skipping it this run.")
                continue

            already_replied = any(
                c.get("public") and c.get("author_id") != ticket.get("requester_id")
                for c in comments
            )
            if already_replied:
                continue

            candidates.append(ticket)

        if stop_paging or not result.get("has_more"):
            break
        page += 1

    return candidates


# Rotated across drafts so replies don't all open with the identical line --
# keyed off the ticket id (below) rather than a persisted counter, so the
# rotation needs no shared state file/lock between the cron script and a
# manual refresh, and survives restarts with no bookkeeping at all.
GREETING_TEMPLATES = ["Hey {name}", "Hey howdy {name}", "Hi {name}"]


def _pick_greeting(ticket_id: int, display_name: str) -> str:
    template = GREETING_TEMPLATES[ticket_id % len(GREETING_TEMPLATES)]
    return f"{template.format(name=display_name)},"


def build_drafts(client, candidates: list, log_fn=print) -> list:
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
        # Quoting the subject as a title ("...about “X”") rather than folding
        # it into the sentence ("...regarding X") avoids the grammatical
        # collision between the fixed template and a raw ticket title that
        # was never written to complete a sentence -- that mismatch was the
        # real source of the stiff, mismatched tone people were noticing.
        display_name = name if name_confidence == "confirmed" else "there"
        greeting = _pick_greeting(ticket["id"], display_name)
        draft = (
            f"{greeting} thank you for reaching out about “{subject}”. "
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
    data = load_pending()
    today = datetime.now().date().isoformat()
    counts = {"pending": 0, "posted_today": 0, "rejected_today": 0}
    for v in data.values():
        status = v.get("status")
        if status == "pending":
            counts["pending"] += 1
        elif status == "posted" and (v.get("posted_at") or "").startswith(today):
            counts["posted_today"] += 1
        elif status == "rejected" and (v.get("rejected_at") or "").startswith(today):
            counts["rejected_today"] += 1
    return counts


def scan_and_merge(client, log_fn=print):
    """One full detect-and-draft pass: find candidates, draft replies, merge
    into the pending store. Returns (candidates, new_items) so the caller
    can log/notify/summarize however fits its own context -- this is the
    single shared implementation used by both the 2-minute cron tick and the
    review page's manual Refresh button."""
    candidates = find_candidates(client, log_fn=log_fn)
    new_items = []
    if candidates:
        drafted = build_drafts(client, candidates, log_fn=log_fn)
        new_items = merge_candidates(drafted)
    mark_scan_completed()
    return candidates, new_items
