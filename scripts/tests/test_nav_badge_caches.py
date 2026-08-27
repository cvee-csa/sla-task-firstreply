"""The header nav badges for Blocked/Standup and IT-Security read
cached_flagged_ticket_count()/cached_security_ticket_count() -- helpers that
deliberately never call Zendesk themselves (see their own docstrings), only
ever reading whatever fetch_flagged_tickets()/fetch_recent_tickets() already
cached. This covers three things: the cold/warm/stale lifecycle of that
cache, that the two functions truly never touch a client, and a concurrency
smoke test on the lock added around all three read-through caches
(_agent_id_cache, _security_ticket_cache, _flagged_ticket_cache) -- added
specifically because review_server.py serves every request on its own
thread (ThreadingHTTPServer), so two nearly-simultaneous requests hitting a
cold cache is a real scenario, not just a theoretical one.
"""
import inspect
import threading
import time
from datetime import datetime, timedelta

import _common


class _FakeClient:
    """Minimal stand-in exercising only what fetch_recent_tickets/
    fetch_flagged_tickets actually call -- search_users (for
    resolve_own_agent_id), get_user (for requester/assignee name
    resolution), and get_tickets. A small sleep in get_tickets is what lets
    the concurrency test below actually create an overlapping window
    between threads instead of finishing too fast to ever race."""

    def __init__(self, tickets, sleep_seconds=0.0):
        self.client = object()
        self._tickets = tickets
        self._sleep_seconds = sleep_seconds
        self.get_tickets_call_count = 0

    def search_users(self, query):
        return [{"id": 1, "email": "agent@example.com", "name": "Test Agent"}]

    def get_user(self, user_id):
        return {"id": user_id, "name": f"User {user_id}"}

    def get_tickets(self, group_id=None, **kwargs):
        self.get_tickets_call_count += 1
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        return {"tickets": self._tickets, "has_more": False}


FLAGGED_FIXTURE_TICKETS = [
    {"id": 201, "subject": "VPN blocked", "tags": ["block"], "status": "open",
     "requester_id": 1, "assignee_id": None, "description": ""},
    {"id": 202, "subject": "Weekly sync", "tags": ["standup"], "status": "open",
     "requester_id": 1, "assignee_id": None, "description": ""},
]
SECURITY_FIXTURE_TICKETS = [
    {"id": 301, "subject": "Ticket A", "status": "open", "requester_id": 1, "assignee_id": None},
    {"id": 302, "subject": "Ticket B", "status": "open", "requester_id": 1, "assignee_id": None},
    {"id": 303, "subject": "Ticket C", "status": "open", "requester_id": 1, "assignee_id": None},
]


# --- cold / warm / stale lifecycle -------------------------------------------

def test_cached_counts_are_zero_before_anything_is_fetched():
    assert _common.cached_flagged_ticket_count() == 0
    assert _common.cached_security_ticket_count(_common.IT_SECURITY_GROUP_ID) == 0


def test_cached_flagged_ticket_count_reflects_a_warm_cache():
    client = _FakeClient(FLAGGED_FIXTURE_TICKETS)
    _common.fetch_flagged_tickets(client)
    assert _common.cached_flagged_ticket_count() == 2


def test_cached_security_ticket_count_reflects_a_warm_cache():
    client = _FakeClient(SECURITY_FIXTURE_TICKETS)
    _common.fetch_recent_tickets(client, _common.IT_SECURITY_GROUP_ID, limit=5)
    assert _common.cached_security_ticket_count(_common.IT_SECURITY_GROUP_ID) == 3


def test_cached_flagged_ticket_count_drops_to_zero_once_stale():
    client = _FakeClient(FLAGGED_FIXTURE_TICKETS)
    _common.fetch_flagged_tickets(client)
    assert _common.cached_flagged_ticket_count() == 2

    # Backdate the cache's own timestamp past the TTL rather than sleeping
    # for real -- same effective test, no slow test suite.
    _common._flagged_ticket_cache["fetched_at"] = (
        datetime.now() - timedelta(seconds=_common.FLAGGED_CACHE_TTL_SECONDS + 5)
    )
    assert _common.cached_flagged_ticket_count() == 0


def test_cached_security_ticket_count_drops_to_zero_once_stale():
    client = _FakeClient(SECURITY_FIXTURE_TICKETS)
    _common.fetch_recent_tickets(client, _common.IT_SECURITY_GROUP_ID, limit=5)
    assert _common.cached_security_ticket_count(_common.IT_SECURITY_GROUP_ID) == 3

    _common._security_ticket_cache[_common.IT_SECURITY_GROUP_ID]["fetched_at"] = (
        datetime.now() - timedelta(seconds=_common.SECURITY_CACHE_TTL_SECONDS + 5)
    )
    assert _common.cached_security_ticket_count(_common.IT_SECURITY_GROUP_ID) == 0


def test_cached_count_helpers_never_take_a_client_param():
    """The whole point of these two helpers is that the header nav (which
    renders on every single page) can call them without ever risking a live
    Zendesk call -- enforced structurally here, not just by convention."""
    assert "client" not in inspect.signature(_common.cached_flagged_ticket_count).parameters
    assert "client" not in inspect.signature(_common.cached_security_ticket_count).parameters


# --- concurrency ---------------------------------------------------------------

def test_concurrent_fetch_flagged_tickets_never_exposes_a_torn_cache():
    """Several threads call fetch_flagged_tickets() at close to the same
    moment against a client whose get_tickets() sleeps briefly (so the
    calls genuinely overlap instead of serializing by accident). Before the
    lock/atomic-reassignment fix, the old clear()-then-update() sequence
    could leave a concurrent reader observing an empty dict mid-refresh --
    a header badge flickering to "0 items" even though a fetch was already
    in flight. This asserts every thread gets a fully-formed, correct
    result and nothing ever raises or returns a half-written cache."""
    client = _FakeClient(FLAGGED_FIXTURE_TICKETS, sleep_seconds=0.05)
    results = []
    errors = []

    def _fetch():
        try:
            results.append(_common.fetch_flagged_tickets(client))
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=_fetch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 8
    for result in results:
        assert result["error"] is None
        assert len(result["items"]) == 2
    # The cache itself must land in a fully consistent final state too.
    assert _common.cached_flagged_ticket_count() == 2
