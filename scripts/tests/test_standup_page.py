"""/standup: the read-only view of the sibling meetingnotestool project's
standup data (_common.standup_snapshot / review_server._render_standup_page).

Every fixture here is made up. This repo is public, so real standup notes
must never appear in it -- not even as test data.
"""
import json

import _common
import review_server


def _view(**overrides):
    view = {
        "schema": _common.STANDUP_SCHEMA_VERSION,
        "generated_at": "2026-01-15T12:00:00-08:00",
        "me": "Alex",
        "latest_standup": "2026-01-15",
        "previous_standup": "2026-01-14",
        "standup_count": 3,
        "latest_doc_url": "https://docs.example.com/latest",
        "tracker_url": None,
        "since": {
            "newly_blocked": [{
                "key": "Widgets|Widget rollout", "name": "Widget rollout", "workstream": "Widgets",
                "status": "blocked", "before": "active", "owners": ["Alex"], "last_date": "2026-01-15",
                "last_update": "Waiting on the vendor's <b>patch</b>.", "waiting_on": "Vendor",
                "check": None, "doc_url": "https://docs.example.com/latest",
            }],
            "newly_done": [], "new_topics": [], "other_updates": [],
            "new_actions": [], "resolved_actions": [],
        },
        "my_open_actions": [
            {"id": "2026-01-13-a1", "date": "2026-01-13", "owner": "Alex", "owner_inferred": True,
             "task": "Email the vendor", "topic": "Widget rollout", "check": "Name was a guess",
             "doc_url": "https://docs.example.com/a", "resolution": None},
        ],
        "my_topics": [],
        "waiting_on": [],
        "counts": {"live_topics": 1, "quiet_topics": 0, "open_actions": 1},
    }
    view.update(overrides)
    return view


def _write(view):
    _common.STANDUP_DATA_FILE.write_text(json.dumps(view))


def test_snapshot_reports_a_missing_file_without_raising():
    snap = _common.standup_snapshot()
    assert snap["view"] is None
    assert "No standup data found" in snap["error"]


def test_snapshot_rejects_invalid_json():
    _common.STANDUP_DATA_FILE.write_text("{not json")
    snap = _common.standup_snapshot()
    assert snap["view"] is None and "Could not parse" in snap["error"]


def test_snapshot_rejects_an_unexpected_schema_version():
    _write(_view(schema=999))
    snap = _common.standup_snapshot()
    assert snap["view"] is None and "schema" in snap["error"]


def test_snapshot_rereads_after_the_file_changes():
    _write(_view())
    assert _common.standup_open_action_count() == 1
    _write(_view(my_open_actions=[], extra_padding="x" * 10))
    assert _common.standup_open_action_count() == 0


def test_badge_count_is_zero_when_data_is_unavailable():
    assert _common.standup_open_action_count() == 0


def test_nav_shows_standup_badge_from_open_action_items():
    _write(_view())
    nav = review_server._top_nav_html("/standup")
    assert 'href="/standup"' in nav and 'aria-current="page"' in nav
    assert '<span class="nav-badge">1</span>' in nav


def test_page_renders_sections_and_escapes_note_text():
    _write(_view())
    page = review_server._render_standup_page(_common.standup_snapshot()).decode()
    assert "Since the Jan 14 standup" in page
    assert "Your open action items, oldest first (1)" in page
    assert "Newly blocked (1)" in page
    assert "(was active)" in page
    assert "&lt;b&gt;patch&lt;/b&gt;" in page and "<b>patch</b>" not in page
    assert "Waiting on Vendor" in page
    assert 'title="Name was a guess"' in page


def test_inferred_owner_is_marked_on_team_rows_but_your_rows_show_no_owner():
    action = _view()["my_open_actions"][0]
    assert 'class="standup-inferred"' in review_server._render_standup_action_row(action, show_owner=True)
    assert "Alex" not in review_server._render_standup_action_row(action)


def test_page_shows_the_error_instead_of_sections_when_data_is_missing():
    page = review_server._render_standup_page(_common.standup_snapshot()).decode()
    assert "No standup data found" in page
    assert "Your open action items" not in page


def test_resolved_action_shows_outcome_instead_of_age():
    row = review_server._render_standup_action_row({
        "date": "2026-01-10", "task": "Rotate the key", "topic": None, "owner": "Alex",
        "resolution": {"date": "2026-01-14", "outcome": "superseded", "note": "Replaced by SSO"},
    })
    assert "Superseded Jan 14" in row and "Replaced by SSO" in row and "open " not in row


# --- decisions (/standup/decide) ---------------------------------------------

def _view_with_resolved_and_quiet():
    return _view(
        my_resolved_actions=[
            {"id": "2026-01-12-a2", "date": "2026-01-12", "owner": "Alex", "owner_inferred": False,
             "task": "Renew the certificate", "topic": "Certificates", "check": None,
             "doc_url": "https://docs.example.com/b",
             "resolution": {"date": "2026-01-14", "outcome": "done", "note": "Renewed", "by_you": False}},
        ],
        quiet_topics=[
            {"key": "Widgets|Old widget", "name": "Old widget", "workstream": "Widgets", "status": "quiet",
             "before": None, "decision": None, "owners": ["Sam"], "last_date": "2026-01-01",
             "last_update": "Paused.", "waiting_on": None, "check": None, "doc_url": "https://docs.example.com/c",
             "triage": {"suggest": "possibly_stuck", "why": "No progress since it paused."}},
        ],
        counts={"live_topics": 1, "quiet_topics": 1, "open_actions": 1},
    )


def test_record_refuses_an_unknown_decision_or_id():
    _write(_view_with_resolved_and_quiet())
    assert _common.record_standup_decision("action", "2026-01-13-a1", "delete")[0] is False
    assert _common.record_standup_decision("action", "2099-01-01-a9", "done")[0] is False
    assert _common.record_standup_decision("topic", "Nope|Nope", "keep")[0] is False
    assert not _common.STANDUP_DECISIONS_FILE.exists()


def test_record_refuses_when_standup_data_is_missing():
    ok, msg, _ = _common.record_standup_decision("action", "2026-01-13-a1", "done")
    assert ok is False and "isn't available" in msg


def test_marking_an_action_done_moves_it_out_of_open_and_undo_restores_it():
    _write(_view_with_resolved_and_quiet())
    ok, _, previous = _common.record_standup_decision("action", "2026-01-13-a1", "done")
    assert ok and previous is None
    view = _common.standup_view()["view"]
    assert view["my_open_actions"] == []
    moved = view["my_resolved_actions"][0]
    assert moved["id"] == "2026-01-13-a1" and moved["resolution"]["by_you"] and moved["resolution"]["pending"]
    assert _common.standup_open_action_count() == 0
    assert _common.record_standup_decision("action", "2026-01-13-a1", previous or "clear")[0]
    assert [a["id"] for a in _common.standup_view()["view"]["my_open_actions"]] == ["2026-01-13-a1"]


def test_still_open_moves_a_resolved_action_back_to_open():
    _write(_view_with_resolved_and_quiet())
    assert _common.record_standup_decision("action", "2026-01-12-a2", "open")[0]
    view = _common.standup_view()["view"]
    assert [a["id"] for a in view["my_open_actions"]] == ["2026-01-12-a2", "2026-01-13-a1"]
    assert view["my_resolved_actions"] == []


def test_topic_decision_applies_and_a_later_mention_wins():
    _write(_view_with_resolved_and_quiet())
    assert _common.record_standup_decision("topic", "Widgets|Old widget", "dropped")[0]
    view = _common.standup_view()["view"]
    assert view["quiet_topics"][0]["decision"] == "dropped"
    assert view["counts"]["quiet_topics"] == 0
    raised_again = _view_with_resolved_and_quiet()
    raised_again["quiet_topics"][0]["last_date"] = "2099-01-01"
    applied = _common.apply_standup_decisions(raised_again, _common.load_standup_decisions())
    assert applied["quiet_topics"][0].get("decision") is None


def test_decisions_file_holds_only_ids_decisions_and_timestamps():
    _write(_view_with_resolved_and_quiet())
    _common.record_standup_decision("action", "2026-01-13-a1", "done")
    _common.record_standup_decision("topic", "Widgets|Old widget", "keep")
    data = json.loads(_common.STANDUP_DECISIONS_FILE.read_text())
    assert set(data["actions"]["2026-01-13-a1"]) == {"outcome", "at"}
    assert set(data["topics"]["Widgets|Old widget"]) == {"decision", "at"}


def test_apply_is_idempotent():
    view = _view_with_resolved_and_quiet()
    decisions = {"actions": {"2026-01-13-a1": {"outcome": "done", "at": "2026-01-15T10:00:00"}},
                 "topics": {"Widgets|Old widget": {"decision": "keep", "at": "2026-01-15T10:00:00"}}}
    once = _common.apply_standup_decisions(view, decisions)
    assert _common.apply_standup_decisions(once, decisions) == once


def test_page_offers_the_right_buttons():
    _write(_view_with_resolved_and_quiet())
    page = review_server._render_standup_page(_common.standup_view()).decode()
    assert 'action="/standup/decide"' in page
    assert 'value="done"' in page and 'value="dropped"' in page and 'value="keep"' in page and 'value="open"' in page
    assert "Possibly stuck (1)" in page and "No progress since it paused." in page
    assert "Recently resolved for you (1)" in page and "Needs a decision (1)" in page


def test_undo_path_survives_the_double_decoding_of_the_undo_query_param():
    """do_GET runs unquote() on top of parse_qs's own decoding of ?undo=, so
    the undo target must be made of characters neither step changes."""
    import base64
    from urllib.parse import parse_qs, quote, unquote
    key = "Research, Ops & CMS|Topic / 50% done"
    path = f"/standup/undo/topic/{review_server._b64url(key)}/clear"
    assert unquote(parse_qs("undo=" + quote(path))["undo"][0]) == path
    token = path.split("/")[4]
    assert base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode() == key


# --- Add to My tasks and Zendesk ticket links ------------------------------

def test_add_to_my_tasks_creates_one_linked_local_draft():
    _write(_view())
    result = _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=lambda *_: None)
    assert result["ok"]
    task = _common.load_personal_tasks()[result["local_id"]]
    assert task["status"] == "draft" and task["subject"] == "Email the vendor"
    assert task["standup_action_id"] == "2026-01-13-a1"
    assert task["notes"] == "From the Jan 13 Operations Standup." and "http" not in task["notes"]
    again = _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=lambda *_: None)
    assert again["ok"] is False and "already in My tasks" in again["error"]
    assert len(_common.load_personal_tasks()) == 1


def test_add_to_my_tasks_sees_archived_tasks_and_refuses_unknown_items():
    _write(_view())
    _common.PERSONAL_TASKS_ARCHIVE_FILE.write_text(json.dumps([{"local_id": "old1", "status": "done", "standup_action_id": "2026-01-13-a1"}]))
    assert _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=lambda *_: None)["ok"] is False
    assert _common.add_standup_action_to_tasks("2099-01-01-a1", log_fn=lambda *_: None)["ok"] is False
    assert _common.load_personal_tasks() == {}


def test_page_swaps_the_button_for_a_chip_once_the_task_exists():
    _write(_view())
    page = review_server._render_standup_page(_common.standup_view()).decode()
    assert 'action="/standup/add-task"' in page
    _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=lambda *_: None)
    page = review_server._render_standup_page(_common.standup_view(), tasks_by_action=_common.standup_tasks_by_action()).decode()
    assert 'action="/standup/add-task"' not in page and "In My tasks (draft)" in page


def test_ticket_numbers_link_only_when_a_subdomain_is_configured(monkeypatch):
    monkeypatch.setattr(_common, "ZENDESK_SUBDOMAIN", "")
    assert "<a " not in review_server._standup_text_html("See ticket 123456.")
    monkeypatch.setattr(_common, "ZENDESK_SUBDOMAIN", "example")
    out = review_server._standup_text_html("See ticket 123456 and #234567 <b>now</b>.")
    assert 'href="https://example.zendesk.com/agent/tickets/123456"' in out
    assert 'href="https://example.zendesk.com/agent/tickets/234567"' in out
    assert "&lt;b&gt;now&lt;/b&gt;" in out


def test_tickets_section_shows_tagged_tickets_and_other_mentions(monkeypatch):
    monkeypatch.setattr(_common, "ZENDESK_SUBDOMAIN", "example")
    mention = {"date": "2026-01-14", "topic": "Widget rollout", "doc_url": "https://docs.example.com/a"}
    _write(_view(ticket_mentions={"111111": [mention], "222222": [mention]}))
    tagged = [{"ticket_id": 111111, "subject": "Widget outage", "url": "https://example.zendesk.com/agent/tickets/111111",
               "status": "open", "requester_name": "Sam", "already_ours": True, "matched_tags": ["standup"]}]
    page = review_server._render_standup_page(_common.standup_view(), tagged_tickets=tagged).decode()
    assert 'Tagged "standup" in Zendesk (1)' in page and "Widget outage" in page
    assert "Mentioned at the" in page and "Jan 14 standup" in page
    assert "Mentioned in the notes (1)" in page and "#222222" in page


def test_tickets_section_shows_a_zendesk_error_instead_of_an_empty_list():
    _write(_view())
    page = review_server._render_standup_page(_common.standup_view(), tickets_error="Not connected to Zendesk.").decode()
    assert "Not connected to Zendesk." in page
