"""/standup/yesterday: the previous-workday ticket-activity recap
(_common.previous_workday / adjacent_workday / fetch_activity_for_day and
review_server._render_standup_yesterday_page).

Every fixture here is made up. This repo is public, so real ticket data must
never appear in it -- not even as test data.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import _common
import review_server

NOOP = lambda *_: None  # noqa: E731
ME = 111
DAY = date(2026, 10, 28)  # a Wednesday


def _ts(day, hour, minute=0):
    """Zendesk-style UTC 'Z' timestamp for a LOCAL wall-clock time, so the
    tests behave the same in any timezone."""
    local = datetime(day.year, day.month, day.day, hour, minute).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ticket(tid, updated, status="open", assignee=None, subject=None):
    return {"id": tid, "subject": subject or f"Subject {tid}", "status": status, "assignee_id": assignee,
            "requester_id": 9, "description": "", "updated_at": updated}


def _comment(author, created, public=True):
    return {"author_id": author, "created_at": created, "public": public}


class FakeClient:
    def __init__(self, tickets, comments, per_page=100):
        self.client = object()
        self._tickets = tickets
        self._comments = comments
        self.comment_calls = []
        self.pages_requested = []
        self._per_page = per_page

    def get_tickets(self, group_id=None, sort_by=None, sort_order=None, per_page=100, page=1):
        self.pages_requested.append(page)
        start = (page - 1) * self._per_page
        chunk = self._tickets[start:start + self._per_page]
        return {"tickets": chunk, "has_more": start + self._per_page < len(self._tickets)}

    def get_ticket_comments(self, tid):
        self.comment_calls.append(tid)
        return self._comments.get(tid, [])


def _patch(monkeypatch):
    monkeypatch.setattr(_common, "resolve_own_agent_id", lambda client, log_fn=print: ME)
    monkeypatch.setattr(_common, "resolve_requester_name", lambda client, rid, desc, log_fn=print: ("Pat Example", "confirmed", None))
    monkeypatch.setattr(_common, "_activity_cache", {})


def test_previous_workday_skips_weekends():
    assert _common.previous_workday(date(2026, 10, 26)) == date(2026, 10, 23)  # Monday -> Friday
    assert _common.previous_workday(date(2026, 10, 27)) == date(2026, 10, 26)  # Tuesday -> Monday
    assert _common.previous_workday(date(2026, 10, 25)) == date(2026, 10, 23)  # Sunday -> Friday
    assert _common.previous_workday(date(2026, 10, 24)) == date(2026, 10, 23)  # Saturday -> Friday


def test_adjacent_workday_both_directions():
    assert _common.adjacent_workday(date(2026, 10, 23), 1) == date(2026, 10, 26)
    assert _common.adjacent_workday(date(2026, 10, 26), -1) == date(2026, 10, 23)
    assert _common.adjacent_workday(DAY, 1) == date(2026, 10, 29)


def test_live_scan_collects_own_events_for_the_day_only(monkeypatch):
    _patch(monkeypatch)
    tickets = [
        _ticket(1, _ts(DAY + timedelta(days=1), 9)),                                   # touched after the day, own reply on the day
        _ticket(2, _ts(DAY, 16), status="solved", assignee=ME),                        # solved that day
        _ticket(3, _ts(DAY, 10)),                                                      # only someone else's comment
        _ticket(4, _ts(DAY - timedelta(days=1), 15)),                                  # older than the day: scan must stop here
        _ticket(5, _ts(DAY - timedelta(days=2), 15)),
    ]
    comments = {
        1: [_comment(ME, _ts(DAY, 9, 15)), _comment(ME, _ts(DAY, 9, 40), public=False), _comment(ME, _ts(DAY - timedelta(days=1), 9))],
        2: [_comment(ME, _ts(DAY, 15, 30))],
        3: [_comment(999, _ts(DAY, 10))],
        4: [_comment(ME, _ts(DAY - timedelta(days=1), 15))],
    }
    client = FakeClient(tickets, comments)
    result = _common.fetch_activity_for_day(client, DAY, log_fn=NOOP, force=True)

    assert [i["ticket_id"] for i in result["items"]] == [1, 2]
    kinds = {i["ticket_id"]: [e["kind"] for e in i["events"]] for i in result["items"]}
    assert kinds[1] == ["public", "internal"]          # previous-day comment excluded, chronological order
    assert kinds[2] == ["public", "solved"]
    assert result["items"][0]["requester_name"] == "Pat Example"
    assert 4 not in client.comment_calls and 5 not in client.comment_calls
    assert result["capped"] is False and result["zendesk_unavailable"] is False


def test_solved_requires_assignee_and_same_day(monkeypatch):
    _patch(monkeypatch)
    tickets = [
        _ticket(10, _ts(DAY, 12), status="solved", assignee=999),                      # someone else's solve
        _ticket(11, _ts(DAY + timedelta(days=1), 12), status="solved", assignee=ME),   # solved, but touched after the day
    ]
    client = FakeClient(tickets, {})
    result = _common.fetch_activity_for_day(client, DAY, log_fn=NOOP, force=True)
    assert result["items"] == []


def test_comment_budget_sets_capped(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(_common, "ACTIVITY_COMMENT_CHECK_LIMIT", 2)
    tickets = [_ticket(n, _ts(DAY, 12)) for n in range(20, 25)]
    client = FakeClient(tickets, {})
    result = _common.fetch_activity_for_day(client, DAY, log_fn=NOOP, force=True)
    assert result["capped"] is True
    assert len(client.comment_calls) == 2


def test_offline_falls_back_to_local_posted_replies(monkeypatch):
    _patch(monkeypatch)
    posted = [
        {"ticket_id": 77, "subject": "Local one", "url": "https://example.test/77", "posted_at": f"{DAY.isoformat()}T09:05:00",
         "posted_public": True, "requester_name": "Pat Example", "name_confidence": "confirmed"},
        {"ticket_id": 78, "subject": "Other day", "url": "https://example.test/78", "posted_at": "2026-10-27T09:05:00",
         "posted_public": True},
    ]
    monkeypatch.setattr(_common, "recent_replied_items", lambda limit=None: posted)
    result = _common.fetch_activity_for_day(SimpleNamespace(client=None), DAY, log_fn=NOOP, force=True)
    assert result["zendesk_unavailable"] is True
    assert [i["ticket_id"] for i in result["items"]] == [77]
    assert result["items"][0]["events"] == [{"at": f"{DAY.isoformat()}T09:05:00", "kind": "public"}]


def test_result_is_cached_per_day(monkeypatch):
    _patch(monkeypatch)
    client = FakeClient([_ticket(1, _ts(DAY, 9))], {1: [_comment(ME, _ts(DAY, 9))]})
    _common.fetch_activity_for_day(client, DAY, log_fn=NOOP)
    calls_after_first = len(client.comment_calls)
    _common.fetch_activity_for_day(client, DAY, log_fn=NOOP)
    assert len(client.comment_calls) == calls_after_first


def _page(items, **kw):
    result = {"items": items, "capped": kw.pop("capped", False), "zendesk_unavailable": kw.pop("unavailable", False)}
    today = kw.pop("today", date(2026, 10, 29))
    default_day = kw.pop("default_day", DAY)
    day = kw.pop("day", DAY)
    return review_server._render_standup_yesterday_page(result, day, default_day, today).decode()


def _item(tid, subject, events, name="Pat Example"):
    return {"ticket_id": tid, "subject": subject, "url": f"https://example.test/{tid}", "requester_name": name,
            "events": [{"at": f"{DAY.isoformat()}T{h}", "kind": k} for h, k in events],
            "first_at": f"{DAY.isoformat()}T{events[0][0]}"}


def test_page_summary_rows_and_escaping():
    html_out = _page([
        _item(1, "VPN <b>down</b>", [("09:15:00", "public"), ("09:40:00", "internal")]),
        _item(2, "Laptop", [("16:00:00", "solved")], name=None),
    ])
    assert "2 tickets: 1 public reply, 1 internal note, 1 solved." in html_out
    assert "VPN &lt;b&gt;down&lt;/b&gt;" in html_out and "<b>down</b>" not in html_out
    assert "Public reply" in html_out and "Internal note" in html_out and "Solved" in html_out
    assert "Copy as text" in html_out and "#1 VPN" in html_out
    assert "(previous workday)" in html_out
    assert 'aria-current="page"' in html_out and "Workday recap" in html_out


def test_page_empty_and_warnings():
    html_out = _page([], capped=True, unavailable=True)
    assert "No ticket activity found for Wednesday, Oct 28." in html_out
    assert "Zendesk was unreachable" in html_out and "page or comment-check limit" in html_out


def test_page_date_navigation_links():
    html_out = _page([], day=date(2026, 10, 23), default_day=DAY, today=date(2026, 10, 29))
    assert "date=2026-10-22" in html_out        # previous workday link
    assert "date=2026-10-26" in html_out        # next workday link (not in the future)
    assert 'href="/standup/yesterday">Previous workday' in html_out
    assert 'max="2026-10-29"' in html_out
    # no "next" link past today
    html_out = _page([], day=date(2026, 10, 29), default_day=DAY, today=date(2026, 10, 29))
    assert "date=2026-10-30" not in html_out
