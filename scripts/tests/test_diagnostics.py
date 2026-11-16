"""/diagnostics: local health checks, the Airtable connection test and the write test.

Everything is made up and local: Airtable is FakeAirtable, launchctl is faked, and no check
touches the network."""
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

import _common
import _diagnostics
import _projects
import review_server
from test_projects import PEOPLE, FakeAirtable, _http


@pytest.fixture
def airtable(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "pat-test-token")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTESTBASE")
    monkeypatch.setenv("AIRTABLE_TABLE_ID", "tblTESTTABLE")
    monkeypatch.setenv("AIRTABLE_VIEW_ID", "viwTESTVIEW")
    monkeypatch.setenv("PROJECTS_ASSIGNEES", PEOPLE)
    fake = FakeAirtable()
    monkeypatch.setattr(_projects, "_request", fake)
    return fake


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _by_name(checks, name):
    return next(c for c in checks if c["name"] == name)


# --- individual checks -----------------------------------------------------------------

def test_env_file_missing_loose_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "REPO_ROOT", tmp_path)
    assert _diagnostics.check_env_file()["status"] == "fail"
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    env.chmod(0o644)
    loose = _diagnostics.check_env_file()
    assert loose["status"] == "warn" and "chmod 600" in loose["detail"]
    env.chmod(0o600)
    assert _diagnostics.check_env_file()["status"] == "ok"


def test_zendesk_checks_report_names_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
    monkeypatch.delenv("ZENDESK_CLIENT_ID", raising=False)
    monkeypatch.setenv("ZENDESK_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setattr(_common, "AUTH_STATE_FILE", tmp_path / "auth.json")
    checks = _diagnostics.check_zendesk()
    settings = _by_name(checks, "Zendesk settings")
    assert settings["status"] == "fail" and "ZENDESK_CLIENT_ID" in settings["detail"]
    assert "super-secret-value" not in repr(checks)


def test_zendesk_broken_auth_is_a_problem_with_its_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "AUTH_STATE_FILE", tmp_path / "auth.json")
    _common.mark_auth_broken("refresh token expired")
    check = _by_name(_diagnostics.check_zendesk(), "Zendesk connection (last check)")
    assert check["status"] == "fail" and "refresh token expired" in check["detail"]


def test_airtable_settings_missing(monkeypatch):
    for name in ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    checks = _diagnostics.check_airtable()
    assert len(checks) == 1 and checks[0]["status"] == "fail" and "AIRTABLE_TOKEN" in checks[0]["detail"]


def test_airtable_checks_when_configured(airtable, monkeypatch):
    monkeypatch.delenv("PROJECTS_ASSIGNEES", raising=False)
    checks = _diagnostics.check_airtable()
    assert _by_name(checks, "Assignee list")["status"] == "warn"
    assert _by_name(checks, "Projects data")["status"] == "info"             # nothing loaded yet in this process
    _projects.fetch_projects(force=True)
    assert "3 projects loaded" in _by_name(_diagnostics.check_airtable(), "Projects data")["detail"]
    assert _by_name(_diagnostics.check_airtable(), "Local snapshots")["status"] == "info"
    _projects.take_snapshot(force=True)
    assert _by_name(_diagnostics.check_airtable(), "Local snapshots")["status"] == "ok"


def test_sla_history_states(monkeypatch):
    snap = {"entries": [{"date": "2026-01-01"}], "error": None, "as_of": "2026-01-01", "source": "local", "fetched_at": ""}
    monkeypatch.setattr(_common, "sla_history_snapshot", lambda: snap)
    assert _diagnostics.check_sla_history()[0]["status"] == "warn"           # years old
    monkeypatch.setattr(_common, "sla_history_age_days", lambda as_of, today=None: 0)
    assert _diagnostics.check_sla_history()[0]["status"] == "ok"
    monkeypatch.setattr(_common, "sla_history_snapshot", lambda: {**snap, "entries": [], "error": "No history file found"})
    assert _diagnostics.check_sla_history()[0]["status"] == "fail"


def test_sla_checkout_present_or_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "SLA_HISTORY_FILE", tmp_path / "sla-report-slack" / "data" / "sla_history.json")
    assert _diagnostics.check_sla_checkout()["status"] == "warn"
    (tmp_path / "sla-report-slack" / ".git").mkdir(parents=True)
    assert _diagnostics.check_sla_checkout()["status"] == "ok"


def _fake_launchctl(monkeypatch, stdout):
    monkeypatch.setattr(_diagnostics.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(_diagnostics.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=""))


def test_launch_agents_running_idle_failed_and_missing(monkeypatch):
    _fake_launchctl(monkeypatch, "PID\tStatus\tLabel\n"
                                 "4242\t0\tcom.csa.zendesk-review-server\n"
                                 "-\t0\tcom.csa.zendesk-first-reply-check\n"
                                 "-\t78\tcom.csa.zendesk-watchdog\n")
    got = {c["name"]: c for c in _diagnostics.check_launch_agents()}
    assert got["com.csa.zendesk-review-server"]["status"] == "ok" and "4242" in got["com.csa.zendesk-review-server"]["detail"]
    assert got["com.csa.zendesk-first-reply-check"]["status"] == "ok"
    assert got["com.csa.zendesk-watchdog"]["status"] == "warn" and "78" in got["com.csa.zendesk-watchdog"]["detail"]
    _fake_launchctl(monkeypatch, "PID\tStatus\tLabel\n")
    assert {c["status"] for c in _diagnostics.check_launch_agents()} == {"warn"}


def test_launch_agents_without_launchctl_is_info(monkeypatch):
    monkeypatch.setattr(_diagnostics.shutil, "which", lambda name: None)
    assert _diagnostics.check_launch_agents()[0]["status"] == "info"


def test_launchctl_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(_diagnostics.shutil, "which", lambda name: "/bin/launchctl")
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("launchctl", 5)
    monkeypatch.setattr(_diagnostics.subprocess, "run", boom)
    assert _diagnostics.check_launch_agents()[0]["status"] == "warn"


# --- the page and its actions ------------------------------------------------------------

def test_diagnostics_page_renders_without_secrets_or_network(airtable, server, monkeypatch):
    monkeypatch.setenv("ZENDESK_CLIENT_SECRET", "super-secret-value")
    _fake_launchctl(monkeypatch, "PID\tStatus\tLabel\n")
    status, _loc, body = _http(server, "GET", "/diagnostics")
    assert status == 200 and "Diagnostics" in body and "Test Airtable connection" in body and "Run write test" in body
    assert "pat-test-token" not in body and "super-secret-value" not in body
    assert airtable.calls == []                                               # the page itself never calls Airtable


def test_diagnostics_page_without_airtable_has_no_airtable_buttons(server, monkeypatch):
    for name in ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    _fake_launchctl(monkeypatch, "PID\tStatus\tLabel\n")
    status, _loc, body = _http(server, "GET", "/diagnostics")
    assert status == 200 and "Test Airtable connection" not in body and "AIRTABLE_TOKEN" in body


def test_connection_test_route(airtable, server):
    status, loc, _ = _http(server, "POST", "/diagnostics/airtable-test", {})
    assert status == 303 and loc.startswith("/diagnostics?msg=") and "worked" in loc
    assert airtable.calls and all(c[0] == "GET" for c in airtable.calls)


def test_write_test_needs_a_confirmation_step(airtable, server):
    status, _loc, body = _http(server, "POST", "/diagnostics/smoke-test", {})
    assert status == 200 and "Run the Airtable write test?" in body and 'name="confirm" value="yes"' in body
    assert airtable.calls == []


def test_confirmed_write_test_runs_and_shows_the_steps(airtable, server):
    before = set(airtable.records)
    status, _loc, body = _http(server, "POST", "/diagnostics/smoke-test", {"confirm": ["yes"]})
    assert status == 200 and "Write test passed" in body and body.count("diag-ok") >= 3
    assert set(airtable.records) == before


def test_write_test_when_unconfigured_redirects(server, monkeypatch):
    fake = FakeAirtable()
    monkeypatch.setattr(_projects, "_request", fake)
    for name in ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    status, loc, _ = _http(server, "POST", "/diagnostics/smoke-test", {"confirm": ["yes"]})
    assert status == 303 and "not%20set%20up" in loc and fake.calls == []


def test_about_panel_links_to_diagnostics():
    assert 'href="/diagnostics"' in review_server._ABOUT_TOOL_HTML
