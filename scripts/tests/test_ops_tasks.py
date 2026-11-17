"""The read-only /ops-tasks page: the Operations group of the Recurring IT Tasks table. Airtable is replaced by a
fake `_request`; all records are made up."""
import http.client
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

import _diagnostics
import _ops_tasks
import _projects
import review_server


def _rec(rid, name, group="Operations", status="Enabled", **fields):
    return {"id": rid, "fields": {"Name": name, "Group": group, "Status": status, **fields}}


RECORDS = [
    _rec("recAAAAAAAAAAAAAA", "Zeta check", Frequency="Monthly", Method="API", Assignee="IT-Operations",
         **{"Last Run": "2026-10-01T14:02:00.000Z", "Notes": "Runs on the 1st", "Subject": "Zeta subject",
            "Body": "<p>Hello <strong>Ops</strong></p><ul><li>one</li><li>two &amp; three</li></ul><p>Bye<br>now</p>"}),
    _rec("recBBBBBBBBBBBBBB", "alpha audit", status="Disabled", Frequency="Quarterly", Method="Email"),
    _rec("recCCCCCCCCCCCCCC", "Beta backup", Frequency="Annually", Method="API"),
    _rec("recDDDDDDDDDDDDDD", "A support report", group="Support"),
]


class FakeAirtable:
    def __init__(self, records, pages=1):
        self.records, self.pages, self.calls = records, pages, []

    def __call__(self, method, path, params=None, body=None):
        self.calls.append((method, path, list(params or []), body))
        p = dict(params or [])
        if "offset" in p and self.pages == 1:
            return {"records": []}
        if self.pages > 1 and "offset" not in p:
            return {"records": self.records[:2], "offset": "next"}
        if self.pages > 1:
            return {"records": self.records[2:]}
        return {"records": self.records}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "pat-fake")
    monkeypatch.setenv("OPS_TASKS_BASE_ID", "appBASEBASEBASE1")
    monkeypatch.setenv("OPS_TASKS_TABLE_ID", "tblTABLETABLE001")
    fake = FakeAirtable(RECORDS)
    monkeypatch.setattr(_projects, "_request", fake)
    return fake


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


# --- reading --------------------------------------------------------------------------------

def test_only_the_operations_group_is_listed_and_sorted_enabled_first(configured):
    result = _ops_tasks.fetch_tasks()
    assert [i["name"] for i in result["items"]] == ["Beta backup", "Zeta check", "alpha audit"]
    assert [i["status_key"] for i in result["items"]] == ["Enabled", "Enabled", "Disabled"]


def test_the_request_is_a_get_with_the_group_formula_and_never_writes(configured, monkeypatch):
    monkeypatch.setenv("OPS_TASKS_VIEW_ID", "viwVIEWVIEWVIEW1")
    _ops_tasks.fetch_tasks()
    method, path, params, body = configured.calls[0]
    assert method == "GET" and body is None
    assert path == "appBASEBASEBASE1/tblTABLETABLE001"
    assert ("filterByFormula", "{Group}='Operations'") in params and ("view", "viwVIEWVIEWVIEW1") in params
    assert {c[0] for c in configured.calls} == {"GET"}


def test_group_formula_escapes_quotes_and_backslashes():
    assert _ops_tasks.group_formula("Ops'; DROP") == "{Group}='Ops\\'; DROP'"
    assert _ops_tasks.group_formula("a\\b") == "{Group}='a\\\\b'"


def test_group_can_be_changed_and_is_rechecked_client_side(configured, monkeypatch):
    monkeypatch.setenv("OPS_TASKS_GROUP", "support")
    assert [i["name"] for i in _ops_tasks.fetch_tasks()["items"]] == ["A support report"]


def test_fields_are_normalized(configured):
    zeta = [i for i in _ops_tasks.fetch_tasks()["items"] if i["name"] == "Zeta check"][0]
    assert (zeta["frequency"], zeta["method"], zeta["assignee"], zeta["subject"]) == ("Monthly", "API", "IT-Operations", "Zeta subject")
    assert zeta["last_run"].startswith("2026-10-0") and len(zeta["last_run"]) == 16
    assert zeta["url"] == "https://airtable.com/appBASEBASEBASE1/tblTABLETABLE001/recAAAAAAAAAAAAAA"


def test_single_select_objects_and_missing_values_are_handled():
    task = _ops_tasks.normalize({"id": "recX", "fields": {"Status": {"name": "Enabled"}, "Assignee": {"name": "Pat"}}},
                                {"base": "", "table": "", "view": ""})
    assert task["status_key"] == "Enabled" and task["assignee"] == "Pat" and task["name"] == "(untitled task)"
    assert task["url"] == "" and task["last_run"] == ""


def test_body_html_becomes_plain_text():
    text = _ops_tasks.html_to_text("<p>Hello <strong>Ops</strong></p><ul><li>one</li><li>two &amp; three</li></ul><p>Bye<br>now</p>")
    assert text == "Hello Ops\n- one\n- two & three\nBye\nnow"
    assert "<" not in _ops_tasks.html_to_text("<div onclick=x>a</div><span>b</span>")
    assert _ops_tasks.html_to_text("") == ""


def test_paging_follows_offsets(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "t")
    monkeypatch.setenv("OPS_TASKS_BASE_ID", "appBASEBASEBASE1")
    monkeypatch.setenv("OPS_TASKS_TABLE_ID", "tblTABLETABLE001")
    fake = FakeAirtable(RECORDS, pages=2)
    monkeypatch.setattr(_projects, "_request", fake)
    assert len(_ops_tasks.fetch_tasks()["items"]) == 3 and len(fake.calls) == 2


def test_cache_is_used_and_force_refetches(configured):
    _ops_tasks.fetch_tasks()
    _ops_tasks.fetch_tasks()
    assert len(configured.calls) == 1
    _ops_tasks.fetch_tasks(force=True)
    assert len(configured.calls) == 2


def test_failure_falls_back_to_the_last_copy_and_first_failure_is_reported(configured, monkeypatch):
    _ops_tasks.fetch_tasks()
    def boom(*a, **k):
        raise _projects.AirtableError("Airtable returned 403 (INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND)")
    monkeypatch.setattr(_projects, "_request", boom)
    stale = _ops_tasks.fetch_tasks(force=True)
    assert stale["stale"] and len(stale["items"]) == 3 and "403" in stale["error"]
    _ops_tasks.reset_cache()
    fresh = _ops_tasks.fetch_tasks(force=True)
    assert fresh["items"] == [] and "403" in fresh["error"] and not fresh["stale"]


def test_unconfigured_makes_no_request(monkeypatch):
    monkeypatch.setattr(_projects, "_request", lambda *a, **k: pytest.fail("called"))
    assert _ops_tasks.fetch_tasks()["configured"] is False
    assert _ops_tasks.check_connection()["ok"] is False


def test_token_is_not_in_any_result(configured):
    assert "pat-fake" not in repr(_ops_tasks.fetch_tasks())


def test_check_connection_reads_one_record(configured):
    assert _ops_tasks.check_connection()["ok"] is True
    assert ("pageSize", "1") in configured.calls[0][2]


# --- page -----------------------------------------------------------------------------------

def test_setup_page_when_not_configured(server):
    status, body = _get(server, "/ops-tasks")
    assert status == 200 and "Ops tasks are not set up" in body and "OPS_TASKS_BASE_ID=" in body
    assert "pat-" not in body


def test_page_lists_sections_tasks_details_and_links(configured, server):
    status, body = _get(server, "/ops-tasks")
    assert status == 200
    assert "Enabled (2)" in body and "Disabled (1)" in body
    assert body.index("Beta backup") < body.index("Zeta check") < body.index("alpha audit")
    assert "A support report" not in body
    assert body.count('class="ops-task ') == 3
    assert '<span class="ops-chip">Monthly</span>' in body and "IT-Operations" in body
    assert "Runs on the 1st" in body and "Zeta subject" in body and "- two &amp; three" in body
    assert 'href="https://airtable.com/appBASEBASEBASE1/tblTABLETABLE001/recAAAAAAAAAAAAAA"' in body
    assert 'data-filter-target=".ops-task"' in body
    assert 'class="top-nav-link active" href="/ops-tasks"' in body
    assert "read-only" in body


def test_page_escapes_airtable_text(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "t")
    monkeypatch.setenv("OPS_TASKS_BASE_ID", "appBASEBASEBASE1")
    monkeypatch.setenv("OPS_TASKS_TABLE_ID", "tblTABLETABLE001")
    evil = [_rec("recEEEEEEEEEEEEEE", "<script>x</script>", Notes="<img src=x onerror=1>", Body="<p>&lt;b&gt;</p>")]
    monkeypatch.setattr(_projects, "_request", FakeAirtable(evil))
    page = review_server._render_ops_tasks_page(_ops_tasks.fetch_tasks()).decode()
    assert "<script>x</script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page and "&lt;img src=x onerror=1&gt;" in page


def test_error_banner_and_empty_state(configured, monkeypatch):
    monkeypatch.setattr(_projects, "_request", FakeAirtable([]))
    page = review_server._render_ops_tasks_page(_ops_tasks.fetch_tasks()).decode()
    assert "No tasks in the Operations group." in page and "Filter by task" not in page
    _ops_tasks.reset_cache()
    def boom(*a, **k):
        raise _projects.AirtableError("Airtable returned 403")
    monkeypatch.setattr(_projects, "_request", boom)
    page = review_server._render_ops_tasks_page(_ops_tasks.fetch_tasks()).decode()
    assert 'class="flag-alert"' in page and "403" in page


def test_refresh_parameter_forces_a_reload(configured, server):
    _get(server, "/ops-tasks")
    _get(server, "/ops-tasks")
    assert len(configured.calls) == 1
    _get(server, "/ops-tasks?refresh=1")
    assert len(configured.calls) == 2


def test_the_page_has_no_post_route(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=15)
    conn.request("POST", "/ops-tasks", body=urllib.parse.urlencode({"x": "1"}),
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 404


# --- nav and diagnostics ----------------------------------------------------------------------

def test_nav_link_sits_after_projects_with_a_badge():
    nav = review_server._top_nav_html("/ops-tasks")
    assert nav.index('href="/projects"') < nav.index('href="/ops-tasks"') < nav.index('href="/solved"')
    link = nav.split('href="/ops-tasks"')[1].split("</a>")[0]
    assert "Ops Tasks" in link and "tbg-calendar" in link


def test_diagnostics_section_reports_setup_state(configured):
    assert _diagnostics.check_ops_tasks()[0]["status"] == "ok"
    _ops_tasks.fetch_tasks()
    assert _diagnostics.check_ops_tasks()[1]["status"] == "ok" and "3 tasks" in _diagnostics.check_ops_tasks()[1]["detail"]


def test_diagnostics_section_is_informational_when_unset():
    checks = _diagnostics.check_ops_tasks()
    assert len(checks) == 1 and checks[0]["status"] == "info"
    assert "Ops tasks" in [title for title, _ in _diagnostics.run_checks()]
