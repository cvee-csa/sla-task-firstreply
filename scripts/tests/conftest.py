"""Shared pytest setup for scripts/tests/.

_common.py imports `from zendesk_mcp_server.zendesk_client import ZendeskClient`
at module load time -- that's a real, separate sibling project on the
machine this tool normally runs on (see the README's setup instructions),
not something published to PyPI, so it won't exist in CI or on a fresh
checkout with no other setup. stub_pkg/ is a trivial stand-in (just enough
of a ZendeskClient shape for _common.py to import cleanly and for
`client.client is None` checks to behave like "not connected") so these
tests can run anywhere with nothing more than `pip install -r
requirements-dev.txt` -- no real Zendesk credentials, no network, no sibling
checkout required. Every test here is pure local logic exercised through a
FakeClient it builds itself when it needs one; none of them talk to the
genuine zendesk_mcp_server package even when it happens to be installed.
"""
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
STUB_PKG_DIR = TESTS_DIR / "stub_pkg"

# stub_pkg first, so `import zendesk_mcp_server` resolves to the stub even on
# a machine that also has the real package installed elsewhere on the path --
# these tests should behave identically everywhere, not depend on whichever
# happens to win a sys.path race on any given machine.
for _p in (str(STUB_PKG_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import _common  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_output_dir(tmp_path, monkeypatch):
    """Runs before EVERY test in this directory, no opt-in required.

    Without this, importing/exercising _common's file-backed helpers
    (add_personal_task, update_pending, ...) would read and write this
    machine's ACTUAL scripts/output/pending_drafts.json and
    personal_tasks.json -- the live data this tool tracks day to day, not
    test fixtures. Every OUTPUT_DIR-derived path constant gets repointed at
    a fresh pytest tmp_path for the duration of each individual test, and
    every in-process cache/lock this session touches gets reset too, so
    nothing leaks from one test into the next regardless of run order."""
    monkeypatch.setattr(_common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(_common, "PENDING_FILE", tmp_path / "pending_drafts.json")
    monkeypatch.setattr(_common, "PENDING_LOCK_FILE", tmp_path / "pending_drafts.json.lock")
    monkeypatch.setattr(_common, "PENDING_ARCHIVE_FILE", tmp_path / "pending_drafts_archive.json")
    monkeypatch.setattr(_common, "FAILURE_STATE_FILE", tmp_path / "failure_notify_state.json")
    monkeypatch.setattr(_common, "AUTH_STATE_FILE", tmp_path / "auth_state.json")
    monkeypatch.setattr(_common, "OAUTH_PENDING_FILE", tmp_path / "oauth_pending.json")
    monkeypatch.setattr(_common, "RUN_LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(_common, "LAST_SCAN_FILE", tmp_path / "last_scan.json")
    monkeypatch.setattr(_common, "PERSONAL_TASKS_FILE", tmp_path / "personal_tasks.json")
    monkeypatch.setattr(_common, "PERSONAL_TASKS_LOCK_FILE", tmp_path / "personal_tasks.json.lock")
    monkeypatch.setattr(_common, "PERSONAL_TASKS_ARCHIVE_FILE", tmp_path / "personal_tasks_archive.json")
    # In-memory caches -- fresh dict per test, not just a fresh file on disk.
    monkeypatch.setattr(_common, "_agent_id_cache", {})
    monkeypatch.setattr(_common, "_security_ticket_cache", {})
    monkeypatch.setattr(_common, "_flagged_ticket_cache", {})
    monkeypatch.setattr(_common, "_pending_ticket_actions_in_progress", set())
    # /standup reads a sibling project's file that exists on this Mac --
    # point it at a nonexistent tmp path so no test ever reads real
    # standup notes (and the Standup nav badge starts cold like the rest).
    monkeypatch.setattr(_common, "STANDUP_DATA_FILE", tmp_path / "standup_data.json")
    monkeypatch.setattr(_common, "_standup_cache", {})
    monkeypatch.setattr(_common, "STANDUP_DECISIONS_FILE", tmp_path / "decisions.json")
    monkeypatch.setattr(_common, "STANDUP_DECISIONS_LOCK_FILE", tmp_path / "standup_decisions.json.lock")
    yield
