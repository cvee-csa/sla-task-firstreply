"""/sla: the Update history button and the live "today" point.

The update is exercised against real git repositories built in a temp folder (a bare "remote",
the checkout this tool reads, and a second clone that plays the GitHub Action pushing a new
snapshot), so the claims that matter are checked for real: the checkout's working tree is never
touched, a later `git pull` still works, and a failed fetch changes nothing. Dates, names and
ticket ids are made up."""
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

import _common
import review_server

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_TERMINAL_PROMPT": "0"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=GIT_ENV, check=True).stdout


def snap(day, breached=0, **extra):
    return {"date": day, "total": 10, "breached": breached, "due_soon": 1, "on_track": 9 - breached,
            "breached_ids": list(range(breached)), **extra}


def write_history(repo, entries):
    path = repo / "data" / "sla_history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


@pytest.fixture
def repos(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    checkout = tmp_path / "checkout"
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
    git(checkout, "remote", "add", "origin", str(remote))
    write_history(checkout, [snap("2026-10-28"), snap("2026-10-29")])
    (checkout / "report.py").write_text("print('committed')\n")
    git(checkout, "add", "-A"); git(checkout, "commit", "-m", "start"); git(checkout, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
    (checkout / "report.py").write_text("print('uncommitted local work')\n")           # the user's unsaved edit
    (checkout / "notes.txt").write_text("untracked\n")
    monkeypatch.setattr(_common, "SLA_HISTORY_FILE", checkout / "data" / "sla_history.json")
    return type("R", (), {"checkout": checkout, "other": other, "remote": remote})


def push_new_days(repos, *days):
    entries = json.loads((repos.other / "data" / "sla_history.json").read_text())
    entries += [snap(d, breached=1) for d in days]
    write_history(repos.other, entries)
    git(repos.other, "add", "-A"); git(repos.other, "commit", "-m", "chore: SLA history"); git(repos.other, "push", "origin", "main")


def tree_fingerprint(checkout):
    return {p.relative_to(checkout).as_posix(): hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(checkout.rglob("*")) if p.is_file() and ".git" not in p.relative_to(checkout).parts}


# --- the update itself -------------------------------------------------------------------

def test_update_saves_the_newer_remote_copy_and_the_page_data_uses_it(repos):
    push_new_days(repos, "2026-11-12", "2026-11-13")
    assert _common.sla_history_snapshot()["as_of"] == "2026-10-29"
    result = _common.update_sla_history()
    assert result["ok"] and "2026-11-13" in result["message"] and "was 2026-10-29" in result["message"] and "2 new day" in result["message"]
    snapshot = _common.sla_history_snapshot()
    assert snapshot["source"] == "github" and snapshot["as_of"] == "2026-11-13" and len(snapshot["entries"]) == 4
    assert snapshot["fetched_at"]


def test_update_never_touches_the_checkouts_working_tree_and_pull_still_works(repos):
    push_new_days(repos, "2026-11-12")
    before = tree_fingerprint(repos.checkout)
    head_before = git(repos.checkout, "rev-parse", "HEAD")
    status_before = git(repos.checkout, "status", "--porcelain")
    assert _common.update_sla_history()["ok"]
    assert tree_fingerprint(repos.checkout) == before                       # not one file changed
    assert git(repos.checkout, "rev-parse", "HEAD") == head_before
    assert git(repos.checkout, "status", "--porcelain") == status_before    # still just the user's own edits
    git(repos.checkout, "pull", "--ff-only")                                  # the user's normal workflow still works
    assert _common.sla_history_snapshot()["source"] == "local"               # the pulled file now ties, and the checkout wins
    assert _common.sla_history_snapshot()["as_of"] == "2026-11-12"


def test_already_current_writes_nothing(repos):
    result = _common.update_sla_history()
    assert result["ok"] and result["message"].startswith("Already current")
    assert not _common._sla_remote_cache_path().exists()


def test_a_remote_older_than_the_checkout_is_ignored(repos):
    write_history(repos.checkout, [snap("2026-10-28"), snap("2026-10-29"), snap("2026-11-02")])
    result = _common.update_sla_history()
    assert result["message"].startswith("Already current") and not _common._sla_remote_cache_path().exists()


def test_fetch_failure_changes_nothing_and_says_why(repos):
    git(repos.checkout, "remote", "set-url", "origin", str(repos.remote) + "-gone")
    result = _common.update_sla_history()
    assert result["ok"] is False and "Could not fetch from GitHub" in result["message"] and "not changed" in result["message"]
    assert not _common._sla_remote_cache_path().exists()
    assert _common.sla_history_snapshot()["source"] == "local"


def test_missing_file_on_the_remote_branch(repos):
    git(repos.other, "rm", "-q", "data/sla_history.json"); git(repos.other, "commit", "-qm", "drop"); git(repos.other, "push", "-q", "origin", "main")
    result = _common.update_sla_history()
    assert result["ok"] is False and "has no data/sla_history.json" in result["message"]


@pytest.mark.parametrize("bad", ["not json", '{"a": 1}', "[1, 2]", "[]", '[{"total": 1}]'])
def test_unusable_remote_copies_are_rejected(repos, bad):
    (repos.other / "data" / "sla_history.json").write_text(bad)
    git(repos.other, "add", "-A"); git(repos.other, "commit", "-qm", "bad"); git(repos.other, "push", "-q", "origin", "main")
    result = _common.update_sla_history()
    assert result["ok"] is False and "not changed" in result["message"]
    assert not _common._sla_remote_cache_path().exists()


def test_not_a_git_checkout(tmp_path, monkeypatch):
    path = tmp_path / "plain" / "data" / "sla_history.json"
    write_history(tmp_path / "plain", [snap("2026-10-29")])
    monkeypatch.setattr(_common, "SLA_HISTORY_FILE", path)
    result = _common.update_sla_history()
    assert result["ok"] is False and "not a git checkout" in result["message"]


def test_git_missing_and_timeout_are_reported_not_raised(repos, monkeypatch):
    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(_common, "_sla_git", no_git)
    assert "git is not installed" in _common.update_sla_history()["message"]

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)
    monkeypatch.setattr(_common, "_sla_git", slow)
    assert "did not answer in time" in _common.update_sla_history()["message"]


def test_git_runs_without_a_shell_prompt_and_with_timeouts(repos, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, **kw):
        seen.append((cmd, kw))
        return real(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    _common.update_sla_history()
    assert [c[0][3:5] for c in seen] == [["fetch", "--quiet"], ["show", "origin/main:data/sla_history.json"]]
    assert all(isinstance(c[0], list) and not c[1].get("shell") and c[1]["timeout"] for c in seen)
    assert all(c[1]["env"]["GIT_TERMINAL_PROMPT"] == "0" for c in seen)


def test_only_one_update_runs_at_a_time(repos):
    assert _common._sla_update_lock.acquire(blocking=False)
    try:
        assert _common.update_sla_history() == {"ok": False, "message": "An update is already running."}
    finally:
        _common._sla_update_lock.release()
    assert _common.update_sla_history()["ok"]


def test_credentials_in_a_git_error_are_never_shown():
    proc = subprocess.CompletedProcess([], 128, stderr="fatal: unable to access 'https://user:ghp_SECRET@github.com/x/y/': 403\n")
    text = _common._git_error_text(proc)
    assert "ghp_SECRET" not in text and "user:" not in text and "github.com" in text
    assert len(_common._git_error_text(subprocess.CompletedProcess([], 1, stderr="x" * 900))) == 200


def test_a_missing_checkout_file_falls_back_to_the_saved_copy(repos):
    push_new_days(repos, "2026-11-12")
    _common.update_sla_history()
    (repos.checkout / "data" / "sla_history.json").unlink()
    snapshot = _common.sla_history_snapshot()
    assert snapshot["error"] is None and snapshot["source"] == "github" and snapshot["as_of"] == "2026-11-12"


def test_a_corrupt_saved_copy_is_ignored(repos):
    _common._sla_remote_cache_path().write_text("{broken")
    assert _common.sla_history_snapshot()["source"] == "local"
    _common._sla_remote_cache_path().write_text(json.dumps({"entries": "nope"}))
    assert _common.sla_history_snapshot()["source"] == "local"


def test_age_in_days():
    today = date(2026, 11, 16)
    assert _common.sla_history_age_days("2026-10-29", today) == 18
    assert _common.sla_history_age_days("2026-11-16", today) == 0
    assert _common.sla_history_age_days(None, today) is None and _common.sla_history_age_days("soon", today) is None


# --- the live "today" point -----------------------------------------------------------------

def _item(ticket_id, status, rules=()):
    return {"ticket_id": ticket_id, "sla_status": status, "breached_rules": [(r, 50.0) for r in rules], "due_soon_rules": []}


def _history(*days):
    return {"entries": [snap(d) for d in days], "error": None, "as_of": days[-1], "source": "local", "fetched_at": ""}


TODAY = date(2026, 11, 16)


def test_live_entry_counts_the_open_tickets():
    items = [_item(1, "breached", ["resolution_age", "no_update"]), _item(2, "breached", ["resolution_age"]),
             _item(3, "due_soon"), _item(4, "on_track"), _item(5, "on_track")]
    entry = _common.sla_live_entry(items, "2026-11-16")
    assert entry == {"date": "2026-11-16", "live": True, "total": 5, "on_track": 2, "due_soon": 1, "breached": 2,
                     "breached_ids": [1, 2], "rules": {"resolution_age": 2, "no_update": 1}}


def test_a_live_point_is_added_when_history_ends_before_today():
    out = _common.sla_history_with_live_point(_history("2026-10-28", "2026-10-29"), {"items": [_item(1, "on_track")]}, TODAY)
    assert out["live_date"] == "2026-11-16" and out["entries"][-1]["live"] is True and len(out["entries"]) == 3
    assert out["as_of"] == "2026-10-29"                                            # still the newest SAVED snapshot


@pytest.mark.parametrize("sla_result,history_days", [
    ({"items": [], "error": "Zendesk is down"}, ("2026-10-29",)),                      # failed scan
    ({"items": [_item(1, "on_track")], "error": None, "capped": True}, ("2026-10-29",)),   # partial count
    ({"items": [], "error": None}, ("2026-11-16",)),                                     # today's saved snapshot wins
    ({"items": [], "error": None}, ("2026-11-20",)),
])
def test_no_live_point_when_it_would_mislead(sla_result, history_days):
    history = _history(*history_days)
    assert _common.sla_history_with_live_point(history, sla_result, TODAY) is history


def test_no_live_point_without_any_history():
    history = {"entries": [], "error": "No history file", "as_of": None}
    assert _common.sla_history_with_live_point(history, {"items": []}, TODAY) is history


def test_chart_data_carries_the_live_point_and_labels_it():
    history = _common.sla_history_with_live_point(_history("2026-10-29"), {"items": [_item(1, "breached", ["no_update"])]}, TODAY)
    data = _common.sla_chart_data(history)
    assert data["labels"][-1] == "2026-11-16" and data["breached"][-1] == 1 and data["breached_ids"][-1] == [1]
    assert data["compliance_pct"][-1] is None and data["resp_med"][-1] is None
    html = review_server._sla_charts_html(history)
    embedded = json.loads(re.search(r'<script type="application/json" id="sla-chart-data">(.*?)</script>', html, re.S).group(1))
    assert embedded["labels"][-1] == "Nov 16 (live)" and embedded["labels"][0] == "Oct 29"


# --- the page and the routes -----------------------------------------------------------------

def _page(history, sla_result=None):
    return review_server._render_sla_page(sla_result or {}, history).decode().split("</style>")[-1]


def test_page_has_the_update_button_and_names_the_source():
    page = _page(_history("2026-11-15"))
    assert 'action="/sla/update"' in page and "Update history" in page
    assert "From the local checkout." in page and "days old" not in page
    github = {**_history("2026-11-15"), "source": "github", "fetched_at": "2026-11-16T09:30:00"}
    assert "Fetched from GitHub at 2026-11-16 09:30:00." in _page(github)


def test_page_warns_when_the_newest_snapshot_is_old():
    page = _page(_history("2000-01-01"))
    assert re.search(r"\d+ days old\.", page) and "Use Update history" in page


def test_page_explains_the_live_point():
    history = _common.sla_history_with_live_point(_history("2026-10-29"), {"items": []}, TODAY)
    assert "The last point (Nov 16, live) is counted from the open tickets just now" in _page(history)
    assert "live)" not in _page(_history("2026-10-29")).split("sla-history-note")[1].split("</p>")[0]


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _post(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    hdrs.update(headers or {})
    conn.request("POST", path, body=urlencode({"x": "1"}), headers=hdrs)
    resp = conn.getresponse()
    resp.read()
    out = (resp.status, resp.getheader("Location") or "")
    conn.close()
    return out


def test_post_runs_the_update_and_redirects_with_its_message(server, monkeypatch):
    monkeypatch.setattr(_common, "update_sla_history", lambda: {"ok": True, "message": "Updated: the history now runs to 2026-11-13."})
    status, loc = _post(server, "/sla/update")
    assert status == 303 and loc.startswith("/sla?msg=Updated") and "2026-11-13" in loc


def test_cross_site_update_is_refused_and_runs_nothing(server, monkeypatch):
    calls = []
    monkeypatch.setattr(_common, "update_sla_history", lambda: calls.append(1) or {"ok": True, "message": "x"})
    for headers in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/x"}, {"Host": "evil.example"}):
        assert _post(server, "/sla/update", headers)[0] == 403
    assert calls == []


def test_get_sla_shows_the_live_point_when_history_is_old(server, monkeypatch):
    monkeypatch.setattr(_common, "make_client", lambda: type("C", (), {"client": None})())
    monkeypatch.setattr(_common, "fetch_sla_tickets", lambda client, log_fn=print, force=False: {"items": [_item(7, "breached", ["no_update"])], "error": None, "capped": False})
    _common_snapshot = _history("2020-01-01")
    monkeypatch.setattr(_common, "sla_history_snapshot", lambda: _common_snapshot)
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.request("GET", "/sla")
    body = conn.getresponse().read().decode()
    conn.close()
    embedded = json.loads(re.search(r'<script type="application/json" id="sla-chart-data">(.*?)</script>', body, re.S).group(1))
    assert embedded["labels"][-1].endswith("(live)") and embedded["breached_ids"][-1] == [7]
    assert "days old" in body


def test_about_text_mentions_the_update():
    assert "Update history" in review_server._ABOUT_TOOL_HTML
