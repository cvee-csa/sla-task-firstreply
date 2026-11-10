"""Real page-load progress: _common.progress_* / _ProgressClient and the
review_server wiring (/__progress, the csa_pid cookie, redirect continuation).

Every fixture here is made up. This repo is public, so real ticket data must
never appear in it -- not even as test data.
"""
import json
import threading
import time
import urllib.request
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

import _common
import review_server

PID = "0123456789abcdef"
ME = 111


@pytest.fixture(autouse=True)
def fresh_progress_state(monkeypatch):
    monkeypatch.setattr(_common, "_progress_registry", {})
    monkeypatch.setattr(_common, "_progress_history", {})
    monkeypatch.setattr(_common, "_activity_cache", {})
    _common._progress_tls.current = None
    yield
    _common._progress_tls.current = None


class Inner:
    def __init__(self):
        self.client = object()
        self.calls = []
        self.flag = "x"

    def get_tickets(self, **kw):
        self.calls.append("get_tickets")
        return {"tickets": [], "has_more": False}

    def get_ticket_comments(self, tid):
        self.calls.append("get_ticket_comments")
        return []

    def boom(self):  # not counted
        return "boom"

    def failing(self):
        raise RuntimeError("nope")


def test_cookie_parsing_validates_the_id():
    assert _common.progress_id_from_cookie(f"a=1; csa_pid={PID}; b=2") == PID
    assert _common.progress_id_from_cookie("csa_pid=NOTHEX") is None
    assert _common.progress_id_from_cookie("csa_pid=" + "a" * 17) is None
    assert _common.progress_id_from_cookie("other=1") is None
    assert _common.progress_id_from_cookie("") is None
    assert _common.progress_id_from_cookie("\x00;;==") is None


def test_unknown_id_snapshot():
    snap = _common.progress_snapshot(PID)
    assert snap == {"known": False, "done": 0, "expected": None, "pct": None, "finished": False}


def test_first_load_has_no_percentage_then_second_load_does():
    token = _common.progress_begin(PID, "GET /x")
    client = _common.progress_wrap(Inner())
    for _ in range(4):
        client.get_ticket_comments(1)
    snap = _common.progress_snapshot(PID)
    assert snap["known"] and snap["done"] == 4 and snap["expected"] is None and snap["pct"] is None
    _common.progress_end(token)
    snap = _common.progress_snapshot(PID)
    assert snap["finished"] and snap["pct"] == 1.0

    # Same route again, new navigation: expectation learned from the first load.
    pid2 = "fedcba9876543210"
    token = _common.progress_begin(pid2, "GET /x")
    client = _common.progress_wrap(Inner())
    client.get_ticket_comments(1)
    snap = _common.progress_snapshot(pid2)
    assert snap["expected"] == 4 and snap["pct"] == pytest.approx(0.25)
    for _ in range(10):  # more calls than last time: capped, never claims done
        client.get_ticket_comments(1)
    assert _common.progress_snapshot(pid2)["pct"] == pytest.approx(0.95)
    _common.progress_end(token)
    assert _common.progress_snapshot(pid2)["pct"] == 1.0


def test_zero_call_load_does_not_erase_the_baseline():
    token = _common.progress_begin(PID, "GET /y")
    _common.progress_wrap(Inner()).get_tickets()
    _common.progress_end(token)
    token = _common.progress_begin("aaaaaaaaaaaaaaaa", "GET /y")  # cache-warm load: no calls
    _common.progress_end(token)
    assert _common._progress_history["GET /y"] == 1


def test_redirect_continues_the_same_record():
    token = _common.progress_begin(PID, "POST /refresh")
    _common.progress_wrap(Inner()).get_tickets()
    _common.progress_end(token, redirected=True)
    assert _common.progress_snapshot(PID)["finished"] is False
    token = _common.progress_begin(PID, "GET /firstreply")
    snap = _common.progress_snapshot(PID)
    assert snap["done"] == 1                       # POST's call still counted
    _common.progress_wrap(Inner()).get_tickets()
    _common.progress_end(token)
    snap = _common.progress_snapshot(PID)
    assert snap["finished"] and snap["done"] == 2
    assert _common._progress_history["POST /refresh"] == 1 and _common._progress_history["GET /firstreply"] == 1


def test_wrapper_is_transparent():
    inner = Inner()
    token = _common.progress_begin(PID, "GET /z")
    client = _common.progress_wrap(inner)
    assert client.client is inner.client           # attribute read passes through
    client.flag = "changed"                        # attribute write lands on the real client
    assert inner.flag == "changed"
    assert client.boom() == "boom"                 # uncounted method works
    assert _common.progress_snapshot(PID)["done"] == 0
    with pytest.raises(RuntimeError):
        _common._ProgressClient(inner, _common._progress_tls.current).failing()
    client.get_tickets()
    assert _common.progress_snapshot(PID)["done"] == 1 and inner.calls == ["get_tickets"]
    _common.progress_end(token)


def test_wrap_is_a_no_op_outside_a_tracked_request():
    inner = Inner()
    assert _common.progress_wrap(inner) is inner


def test_failed_calls_still_count():
    token = _common.progress_begin(PID, "GET /f")

    class Failing(Inner):
        def get_tickets(self, **kw):
            raise ValueError("x")

    client = _common.progress_wrap(Failing())
    with pytest.raises(ValueError):
        client.get_tickets()
    assert _common.progress_snapshot(PID)["done"] == 1
    _common.progress_end(token)


def test_registry_is_bounded_and_purges_old_records(monkeypatch):
    monkeypatch.setattr(_common, "_PROGRESS_MAX_RECORDS", 5)
    for n in range(12):
        _common.progress_end(_common.progress_begin(f"{n:016x}", "GET /b"))
    assert len(_common._progress_registry) <= 5
    for rec in _common._progress_registry.values():
        rec.updated -= _common._PROGRESS_KEEP_SECONDS + 1
    _common.progress_end(_common.progress_begin(PID, "GET /b"))
    assert list(_common._progress_registry) == [PID]


# ---------------------------------------------------------------------------
# HTTP level: a real server, a slow fake Zendesk client, /standup/yesterday.
# ---------------------------------------------------------------------------

DAY1 = date(2026, 10, 28)
DAY2 = date(2026, 10, 27)


def _ts(day, hour):
    local = datetime(day.year, day.month, day.day, hour).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SlowClient:
    def __init__(self, delay=0.04, tickets=12):
        self.client = object()
        self.delay = delay
        self.total_calls = 0
        self._tickets = [
            {"id": n, "subject": f"S{n}", "status": "open", "assignee_id": None, "requester_id": 9,
             "description": "", "updated_at": _ts(DAY1, 12)} for n in range(1, tickets + 1)
        ]

    def get_tickets(self, **kw):
        self.total_calls += 1
        return {"tickets": self._tickets, "has_more": False}

    def get_ticket_comments(self, tid):
        time.sleep(self.delay)
        self.total_calls += 1
        return []


@pytest.fixture
def server(monkeypatch):
    clients = []

    def make():
        c = SlowClient()
        clients.append(c)
        return _common.progress_wrap(c)

    monkeypatch.setattr(_common, "make_client", make)
    monkeypatch.setattr(_common, "resolve_own_agent_id", lambda client, log_fn=print: ME)
    monkeypatch.setattr(review_server, "_log", lambda msg: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, clients
    httpd.shutdown()
    httpd.server_close()


def _get(port, path, cookie=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()


def _load_and_watch(port, day, pid):
    """GET /standup/yesterday with a progress cookie in a thread while
    polling /__progress from here; returns every snapshot seen."""
    result = {}

    def load():
        result["status"], _ = _get(port, f"/standup/yesterday?date={day.isoformat()}", cookie=f"csa_pid={pid}")

    thread = threading.Thread(target=load)
    thread.start()
    snaps = []
    while thread.is_alive():
        _, body = _get(port, f"/__progress?id={pid}")
        snaps.append(json.loads(body))
        time.sleep(0.03)
    thread.join()
    _, body = _get(port, f"/__progress?id={pid}")
    snaps.append(json.loads(body))
    assert result["status"] == 200
    return snaps


def test_http_progress_unknown_then_measured(server):
    httpd, clients = server
    port = httpd.server_address[1]

    first = _load_and_watch(port, DAY1, PID)
    assert all(s["pct"] is None for s in first[:-1] if not s["finished"])   # no baseline yet: no made-up number
    assert first[-1]["finished"] and first[-1]["pct"] == 1.0
    assert _common._progress_history["GET /standup/yesterday"] == clients[0].total_calls == 13

    second = _load_and_watch(port, DAY2, "fedcba9876543210")
    pcts = [s["pct"] for s in second if s["known"] and not s["finished"] and s["pct"] is not None]
    assert len(set(pcts)) >= 3, pcts                     # real, advancing values
    assert pcts == sorted(pcts) and max(pcts) <= 0.95    # monotone, never claims done early
    assert second[-1]["finished"] and second[-1]["pct"] == 1.0
    assert second[-1]["done"] == clients[1].total_calls == 13


def test_http_untracked_requests_and_bad_ids(server):
    httpd, _ = server
    port = httpd.server_address[1]
    _get(port, f"/standup/yesterday?date={DAY1.isoformat()}")            # no cookie
    assert _common._progress_registry == {}
    for bad in ("", "nothex", "g" * 16, "a" * 40):
        _, body = _get(port, f"/__progress?id={bad}")
        assert json.loads(body)["known"] is False
    status, _ = _get(port, "/healthz")
    assert status == 200 and _common._progress_registry == {}


def test_progress_poll_is_not_logged_but_pages_are(monkeypatch, server):
    httpd, _ = server
    logged = []
    monkeypatch.setattr(review_server, "_log", logged.append)
    port = httpd.server_address[1]
    _get(port, f"/__progress?id={PID}")
    assert not any("__progress" in m for m in logged)
    _get(port, "/healthz")
    assert any("/healthz" in m for m in logged)


def test_loading_bar_markup_uses_real_progress():
    html_out = review_server._page("<p>x</p>").decode()
    assert "/__progress?id=" in html_out and "csa_pid" in html_out
    assert "width 5s cubic-bezier" not in html_out                         # the old fake 5s crawl is gone
    assert 'id="page-loading-bar"' in html_out


def test_detail_card_markup_and_script_hooks():
    html_out = review_server._page("<p>x</p>").decode()
    for needle in ('id="page-loading-card"', 'id="page-loading-dim"', 'id="page-loading-detail"',
                   "CARD_DELAY_MS = 1500", "of ' + s.expected + ' requests", "indeterminate"):
        assert needle in html_out, needle
    # Never blocks clicks; the card is a live region for screen readers.
    assert "pointer-events: none" in html_out and 'role="status"' in html_out
