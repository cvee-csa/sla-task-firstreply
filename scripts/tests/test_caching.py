"""Caching: the (ticket id, updated_at) comment cache and the
stale-while-revalidate main-queue scan (_common.cached_ticket_comments,
_common.fetch_recently_worked_on). Every fixture is made up."""
import threading
import time
from datetime import datetime, timedelta

import pytest

import _common

NOOP = lambda *_: None  # noqa: E731
ME = 111


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    _common.clear_comment_cache()
    monkeypatch.setattr(_common, "_worked_on_cache", {})
    monkeypatch.setattr(_common, "_worked_on_refreshing", False)
    monkeypatch.setattr(_common, "_worked_on_next_attempt", None)
    monkeypatch.setattr(_common, "_activity_cache", {})
    monkeypatch.setattr(_common, "resolve_own_agent_id", lambda client, log_fn=print: ME)
    monkeypatch.setattr(_common, "resolve_requester_name", lambda client, rid, desc, log_fn=print: ("Pat Example", "confirmed", None))
    monkeypatch.setattr(_common, "recent_replied_items", lambda limit=None: list(LOCAL))
    LOCAL.clear()
    yield
    _common.clear_comment_cache()


LOCAL: list = []


class FakeClient:
    def __init__(self, tickets=None, comments=None, gate=None, fail=False):
        self.client = object()
        self.tickets = tickets if tickets is not None else []
        self.comments = comments or {}
        self.gate = gate
        self.fail = fail
        self.comment_calls = []
        self.ticket_calls = 0

    def get_tickets(self, **kw):
        self.ticket_calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("zendesk down")
        return {"tickets": self.tickets, "has_more": False}

    def get_ticket_comments(self, tid):
        self.comment_calls.append(tid)
        return self.comments.get(tid, [])


def _ticket(tid, updated="2026-01-01T10:00:00Z"):
    return {"id": tid, "subject": f"S{tid}", "requester_id": 9, "description": "", "updated_at": updated}


def _own_comment(public=True):
    return [{"author_id": ME, "created_at": "2026-01-01T09:00:00Z", "public": public}]


# --- comment cache -------------------------------------------------------------

def test_same_updated_at_is_served_from_cache():
    c = FakeClient(comments={1: _own_comment()})
    first = _common.cached_ticket_comments(c, 1, "t1")
    second = _common.cached_ticket_comments(c, 1, "t1")
    assert first is second and c.comment_calls == [1]


def test_changed_updated_at_refetches():
    c = FakeClient(comments={1: _own_comment()})
    _common.cached_ticket_comments(c, 1, "t1")
    _common.cached_ticket_comments(c, 1, "t2")
    assert c.comment_calls == [1, 1]


def test_missing_updated_at_is_never_cached():
    c = FakeClient()
    _common.cached_ticket_comments(c, 1, None)
    _common.cached_ticket_comments(c, 1, "")
    assert c.comment_calls == [1, 1]


def test_errors_propagate_and_are_not_cached():
    class Boom(FakeClient):
        def get_ticket_comments(self, tid):
            self.comment_calls.append(tid)
            raise RuntimeError("nope")
    c = Boom()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            _common.cached_ticket_comments(c, 1, "t1")
    assert c.comment_calls == [1, 1]


def test_cache_is_bounded_least_recently_used_first(monkeypatch):
    monkeypatch.setattr(_common, "COMMENT_CACHE_MAX_ENTRIES", 2)
    c = FakeClient()
    _common.cached_ticket_comments(c, 1, "t")
    _common.cached_ticket_comments(c, 2, "t")
    _common.cached_ticket_comments(c, 1, "t")      # touch 1, so 2 is now the oldest
    _common.cached_ticket_comments(c, 3, "t")      # evicts 2
    c.comment_calls.clear()
    _common.cached_ticket_comments(c, 1, "t")
    _common.cached_ticket_comments(c, 2, "t")
    assert c.comment_calls == [2]


def test_entries_expire_after_the_max_age(monkeypatch):
    monkeypatch.setattr(_common, "COMMENT_CACHE_MAX_AGE_SECONDS", 0)
    c = FakeClient()
    _common.cached_ticket_comments(c, 1, "t")
    _common.cached_ticket_comments(c, 1, "t")
    assert c.comment_calls == [1, 1]


def test_activity_scan_reuses_comments_for_unchanged_tickets():
    day = datetime(2026, 1, 1).date()
    c = FakeClient(tickets=[_ticket(1, "2026-01-01T12:00:00Z")], comments={1: _own_comment()})
    _common.fetch_activity_for_day(c, day, log_fn=NOOP, force=True)
    _common.fetch_activity_for_day(c, day, log_fn=NOOP, force=True)
    assert c.comment_calls == [1]


def test_click_time_check_always_reads_live():
    c = FakeClient(comments={1: []})
    for _ in range(2):
        _common.check_already_replied(c, 1, requester_id=9, agent_user_id=ME, log_fn=NOOP)
    assert c.comment_calls == [1, 1]


# --- stale-while-revalidate ---------------------------------------------------------

def _worked_on(c, **kw):
    return _common.fetch_recently_worked_on(c, log_fn=NOOP, **kw)


def _wait_refresh_done(timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if not _common._worked_on_refreshing:
            return
        time.sleep(0.01)
    raise AssertionError("background refresh did not finish")


def test_first_call_scans_synchronously_then_serves_from_cache():
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    out = _worked_on(c)
    assert [i["ticket_id"] for i in out["items"]] == [1] and c.ticket_calls == 1
    again = _worked_on(c)
    assert again["items"] == out["items"] and c.ticket_calls == 1


def test_stale_cache_is_served_instantly_while_one_refresh_runs(monkeypatch):
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    _worked_on(c)
    monkeypatch.setattr(_common, "WORKED_ON_CACHE_TTL_SECONDS", 0)
    gate = threading.Event()
    slow = FakeClient(tickets=[_ticket(1), _ticket(2)], comments={1: _own_comment(), 2: _own_comment()}, gate=gate)
    monkeypatch.setattr(_common, "make_client", lambda: slow)

    started = time.time()
    first = _worked_on(c)
    second = _worked_on(c)                       # while the refresh is still blocked
    assert time.time() - started < 1.0           # neither call waited for the scan
    assert [i["ticket_id"] for i in first["items"]] == [1] == [i["ticket_id"] for i in second["items"]]
    assert _common._worked_on_refreshing is True

    gate.set()
    _wait_refresh_done()
    assert slow.ticket_calls == 1                # single flight: two stale calls, one refresh
    monkeypatch.setattr(_common, "WORKED_ON_CACHE_TTL_SECONDS", 120)
    assert sorted(i["ticket_id"] for i in _worked_on(c)["items"]) == [1, 2]


def test_local_replies_show_immediately_even_from_a_cached_scan():
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    _worked_on(c)
    LOCAL.append({"ticket_id": 77, "subject": "Just sent", "posted_at": "2099-01-01T00:00:00", "posted_public": True})
    out = _worked_on(c)
    assert [i["ticket_id"] for i in out["items"]][0] == 77 and c.ticket_calls == 1


def test_a_ticket_replied_to_through_the_tool_is_not_duplicated():
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    _worked_on(c)
    LOCAL.append({"ticket_id": 1, "subject": "Now local", "posted_at": "2099-01-01T00:00:00", "posted_public": True})
    out = _worked_on(c)
    assert [i["ticket_id"] for i in out["items"]] == [1] and out["items"][0]["subject"] == "Now local"


def test_failed_background_refresh_keeps_the_last_good_result_and_backs_off(monkeypatch):
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    _worked_on(c)
    monkeypatch.setattr(_common, "WORKED_ON_CACHE_TTL_SECONDS", 0)
    broken = FakeClient(fail=True)
    monkeypatch.setattr(_common, "make_client", lambda: broken)
    _worked_on(c)
    _wait_refresh_done()
    out = _worked_on(c)                          # inside the backoff: no new refresh attempt
    assert [i["ticket_id"] for i in out["items"]] == [1] and out["zendesk_unavailable"] is False
    assert broken.ticket_calls == 1 and _common._worked_on_next_attempt > datetime.now() - timedelta(seconds=1)


def test_forced_scan_that_fails_does_not_replace_a_good_cache():
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    _worked_on(c)
    out = _worked_on(FakeClient(fail=True), force=True)
    assert [i["ticket_id"] for i in out["items"]] == [1] and out["zendesk_unavailable"] is False


def test_unreachable_zendesk_falls_back_to_local_history():
    LOCAL.append({"ticket_id": 5, "subject": "Local", "posted_at": "2026-01-01T00:00:00", "posted_public": True})
    c = FakeClient()
    c.client = None
    out = _worked_on(c)
    assert out["zendesk_unavailable"] is True and [i["ticket_id"] for i in out["items"]] == [5]


def test_very_stale_cache_makes_the_request_wait_for_a_fresh_scan(monkeypatch):
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    _worked_on(c)
    monkeypatch.setattr(_common, "WORKED_ON_MAX_STALE_SECONDS", 0)
    fresh = FakeClient(tickets=[_ticket(2)], comments={2: _own_comment()})
    assert [i["ticket_id"] for i in _worked_on(fresh)["items"]] == [2] and fresh.ticket_calls == 1


def test_warm_caches_fills_the_cache_and_never_raises(monkeypatch):
    c = FakeClient(tickets=[_ticket(1)], comments={1: _own_comment()})
    monkeypatch.setattr(_common, "make_client", lambda: c)
    _common.warm_caches(log_fn=NOOP)
    assert c.ticket_calls == 1 and _common._worked_on_cache
    monkeypatch.setattr(_common, "make_client", lambda: (_ for _ in ()).throw(RuntimeError("no creds")))
    _common.warm_caches(log_fn=NOOP)
