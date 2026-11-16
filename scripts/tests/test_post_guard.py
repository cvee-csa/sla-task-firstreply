"""Every POST route refuses a cross-site or non-local request before doing anything.

The guard sits at the top of Handler.do_POST, so no route can be added without it. Each case
sends a forged Origin (and, separately, a foreign Host) and expects HTTP 403 with no side
effect; a request from the tool's own origin must still get past the guard."""
import http.client
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

import review_server

POST_ROUTES = [
    "/refresh",
    "/standup/add-task",
    "/projects/recAAAAAAAAAAAAAA/edit",
    "/projects/new",
    "/diagnostics/airtable-test",
    "/diagnostics/smoke-test",
    "/projects/undo/0123456789ab",
    "/sla/update",
    "/tasks/new",
    "/tasks/new/quick-solve",
    "/tasks/abc123/edit",
    "/tasks/abc123/move/up",
    "/tasks/abc123/promote",
    "/tasks/abc123/discard",
    "/tasks/abc123/done",
    "/tasks/abc123/quick-solve",
    "/reconnect/start",
    "/reconnect/complete",
    "/post_public/12345",
    "/post_internal/12345",
    "/dismiss/12345",
    "/undo_dismiss/12345",
    "/claim/12345",
]


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _post(port, path, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = urlencode({"id": "x", "text": "hello"})
    hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    hdrs.update(headers)
    conn.request("POST", path, body=body, headers=hdrs)
    resp = conn.getresponse()
    resp.read()
    status = resp.status
    conn.close()
    return status


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Anything a route would do after the guard fails the test instead of touching Zendesk or disk."""
    import _common
    for name in ("add_standup_action_to_tasks", "update_sla_history", "post_comment"):
        if hasattr(_common, name):
            monkeypatch.setattr(_common, name, lambda *a, **k: pytest.fail(f"{name} reached past the guard"))
    monkeypatch.setattr(review_server.Handler, "_refresh", lambda self: pytest.fail("refresh reached past the guard"))


@pytest.mark.parametrize("path", POST_ROUTES)
def test_forged_origin_is_refused(server, path):
    assert _post(server, path, {"Origin": "https://evil.example"}) == 403


@pytest.mark.parametrize("path", POST_ROUTES)
def test_forged_referer_is_refused(server, path):
    assert _post(server, path, {"Referer": "https://evil.example/page"}) == 403


@pytest.mark.parametrize("path", POST_ROUTES)
def test_foreign_host_is_refused(server, path):
    host = "attacker.example:8765"
    assert _post(server, path, {"Host": host, "Origin": f"http://{host}"}) == 403


def test_keep_alive_survives_a_refusal(server):
    """The refused request's body is read, so the next request on the connection parses cleanly."""
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    body = urlencode({"a": "b" * 500})
    conn.request("POST", "/claim/1", body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example"})
    first = conn.getresponse()
    first.read()
    conn.request("GET", "/healthz")
    second = conn.getresponse()
    second.read()
    conn.close()
    assert first.status == 403 and second.status == 200


def test_own_origin_gets_past_the_guard(server):
    host = f"127.0.0.1:{server}"
    status = _post(server, "/no-such-route/x/y", {"Origin": f"http://{host}"})
    assert status == 404
