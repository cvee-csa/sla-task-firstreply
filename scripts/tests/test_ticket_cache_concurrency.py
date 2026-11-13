"""Concurrency smoke test for the lock around the read-through ticket caches
(_agent_id_cache, _security_ticket_cache, _flagged_ticket_cache). Added
because review_server.py serves every request on its own thread
(ThreadingHTTPServer), so two nearly-simultaneous requests hitting a cold
cache is a real scenario, not a theoretical one.
"""
import threading
import time

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

# --- concurrency ---------------------------------------------------------------

def test_concurrent_fetch_flagged_tickets_never_exposes_a_torn_cache():
    """Several threads call fetch_flagged_tickets() at close to the same
    moment against a client whose get_tickets() sleeps briefly (so the
    calls genuinely overlap instead of serializing by accident). Before the
    lock/atomic-reassignment fix, the old clear()-then-update() sequence
    could leave a concurrent reader observing an empty dict mid-refresh --
    a reader seeing "0 items" even though a fetch was already in flight. This asserts every thread gets a fully-formed, correct
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
    assert len(_common._flagged_ticket_cache["items"]) == 2
