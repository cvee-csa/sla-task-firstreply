"""/standup and its sub-pages: the view of the sibling meetingnotestool
project's standup data (_common.standup_snapshot / standup_view and the
review_server._render_standup_* functions), your decisions, and the My tasks
and Zendesk links.

Every fixture here is made up. This repo is public, so real standup notes
must never appear in it -- not even as test data.
"""
import json
from urllib.parse import parse_qs, quote, unquote

import _common
import review_server

NOOP = lambda *_: None  # noqa: E731


def _entry(eid, date, update, status="active", owner="Alex", **extra):
    e = {"id": eid, "date": date, "status": status, "owner": owner, "owner_inferred": False, "update": update,
         "waiting_on": None, "check": None, "doc_url": f"https://docs.example.com/{date}"}
    e.update(extra)
    return e


def _topic(key, name, status, entries, owners=("Alex",), **extra):
    last = entries[-1]
    t = {"key": key, "name": name, "workstream": key.split("|")[0], "status": status, "decision": None,
         "owners": list(owners), "first_date": entries[0]["date"], "last_date": last["date"],
         "last_update": last["update"], "waiting_on": last.get("waiting_on"), "check": last.get("check"),
         "doc_url": last["doc_url"], "triage": None, "entries": entries, "action_ids": []}
    t.update(extra)
    return t


def _action(aid, date, task, topic_key, owner="Alex", item=None, resolution=None, **extra):
    a = {"id": aid, "date": date, "owner": owner, "owner_inferred": False, "task": task, "topic_key": topic_key,
         "topic": None, "item": item, "check": None, "doc_url": f"https://docs.example.com/{date}", "resolution": resolution}
    a.update(extra)
    return a


def _view(**overrides):
    rollout = _topic("Widgets|Widget rollout", "Widget rollout", "blocked", [
        _entry("2026-01-13-i1", "2026-01-13", "Rollout started."),
        _entry("2026-01-15-i1", "2026-01-15", "Waiting on the vendor's <b>patch</b>, see ticket 111111.", status="blocked",
               waiting_on="Vendor"),
    ], action_ids=["2026-01-13-a1"])
    old = _topic("Widgets|Old widget", "Old widget", "quiet", [
        _entry("2026-01-01-i1", "2026-01-01", "Paused.", owner="Sam"),
    ], owners=("Sam",), triage={"suggest": "possibly_stuck", "why": "No progress since it paused."})
    certs = _topic("Security|Certificates", "Certificates", "done", [
        _entry("2026-01-12-i1", "2026-01-12", "Renewed.", status="done"),
    ], action_ids=["2026-01-12-a2"])
    actions = {
        "2026-01-13-a1": _action("2026-01-13-a1", "2026-01-13", "Email the vendor", "Widgets|Widget rollout",
                                 item="2026-01-13-i1", owner_inferred=True, check="Name was a guess"),
        "2026-01-12-a2": _action("2026-01-12-a2", "2026-01-12", "Renew the certificate", "Security|Certificates",
                                 item="2026-01-12-i1",
                                 resolution={"date": "2026-01-14", "outcome": "done", "note": "Renewed", "by_you": False}),
        "2026-01-15-a1": _action("2026-01-15-a1", "2026-01-15", "Chase the vendor", "Widgets|Widget rollout", owner="Sam"),
    }
    view = {
        "schema": _common.STANDUP_SCHEMA_VERSION,
        "generated_at": "2026-01-15T12:00:00-08:00",
        "me": "Alex",
        "latest_standup": "2026-01-15",
        "standup_count": 3,
        "latest_doc_url": "https://docs.example.com/2026-01-15",
        "workstreams": ["Widgets", "Security"],
        "topics": {t["key"]: t for t in (rollout, old, certs)},
        "actions": actions,
        "meetings": [
            {"date": "2026-01-15", "doc_url": "https://docs.example.com/2026-01-15", "summary": "Vendor delay.",
             "items": [{"topic_key": "Widgets|Widget rollout", "entry_id": "2026-01-15-i1"}],
             "action_ids": ["2026-01-15-a1"], "resolved_ids": []},
            {"date": "2026-01-14", "doc_url": "https://docs.example.com/2026-01-14", "summary": "Quiet week.",
             "items": [], "action_ids": [], "resolved_ids": ["2026-01-12-a2"]},
        ],
        "since_standups": ["2026-01-14", "2026-01-13"],
        "since": {
            "2026-01-14": {"newly_blocked": [{"key": "Widgets|Widget rollout", "before": "active"}], "newly_done": [],
                           "new_topics": [], "other_updates": [], "new_actions": ["2026-01-15-a1"], "resolved_actions": []},
            "2026-01-13": {"newly_blocked": [{"key": "Widgets|Widget rollout", "before": "active"}], "newly_done": [],
                           "new_topics": [], "other_updates": [], "new_actions": ["2026-01-15-a1"],
                           "resolved_actions": ["2026-01-12-a2"]},
        },
        "my_open_actions": ["2026-01-13-a1"],
        "my_topics": ["Widgets|Widget rollout"],
        "my_resolved_actions": ["2026-01-12-a2"],
        "quiet_topics": ["Widgets|Old widget"],
        "waiting_on": [{"on": "Vendor", "topics": ["Widgets|Widget rollout"]}],
        "ticket_mentions": {},
    }
    view.update(overrides)
    return view


def _write(view=None):
    _common.STANDUP_DATA_FILE.write_text(json.dumps(view or _view()))


def _page(render, *args, **kwargs):
    return render(_common.standup_view(), *args, **kwargs).decode()


# --- loading -----------------------------------------------------------------

def test_snapshot_reports_a_missing_file_without_raising():
    snap = _common.standup_snapshot()
    assert snap["view"] is None and "No standup data found" in snap["error"]


def test_snapshot_rejects_invalid_json():
    _common.STANDUP_DATA_FILE.write_text("{not json")
    snap = _common.standup_snapshot()
    assert snap["view"] is None and "Could not parse" in snap["error"]


def test_snapshot_rejects_an_unexpected_schema_version():
    _write(_view(schema=1))
    snap = _common.standup_snapshot()
    assert snap["view"] is None and "schema" in snap["error"]


def test_snapshot_rereads_after_the_file_changes():
    _write()
    assert _common.standup_open_action_count() == 1
    _write(_view(my_open_actions=[], actions={}, extra_padding="x" * 10))
    assert _common.standup_open_action_count() == 0


def test_badge_count_is_zero_when_data_is_unavailable():
    assert _common.standup_open_action_count() == 0


def test_nav_shows_standup_badge_from_open_action_items():
    _write()
    nav = review_server._top_nav_html("/standup")
    assert 'href="/standup"' in nav and 'aria-current="page"' in nav
    assert '<span class="nav-badge">1</span>' in nav


# --- overview ----------------------------------------------------------------

def test_overview_renders_sections_and_escapes_note_text():
    _write()
    page = _page(review_server._render_standup_page)
    assert "Since the Jan 14 standup" in page
    assert "Your open action items, oldest first (1)" in page
    assert "Newly blocked (1)" in page and "(was active)" in page
    assert "&lt;b&gt;patch&lt;/b&gt;" in page and "<b>patch</b>" not in page
    assert "Waiting on Vendor" in page
    assert 'title="Name was a guess"' in page
    assert 'href="/standup/topic/' in page


def test_overview_since_picker_switches_the_comparison():
    _write()
    page = _page(review_server._render_standup_page, since="2026-01-13")
    assert "Since the Jan 13 standup" in page and "Action items resolved (1)" in page
    assert 'href="/standup?since=2026-01-13"' in page
    unknown = _page(review_server._render_standup_page, since="1999-01-01")
    assert "Since the Jan 14 standup" in unknown


def test_error_page_replaces_sections_when_data_is_missing():
    page = review_server._render_standup_page(_common.standup_snapshot()).decode()
    assert "No standup data found" in page and "Your open action items" not in page


def test_inferred_owner_is_marked_on_team_rows_but_your_rows_show_no_owner():
    _write()
    ctx = review_server._StandupCtx(_common.standup_view()["view"], "/standup")
    action = ctx.action("2026-01-13-a1")
    assert 'class="standup-inferred"' in ctx.action_row(action, show_owner=True)
    assert "Alex" not in ctx.action_row(action)


def test_buttons_only_appear_on_your_own_action_items():
    _write()
    ctx = review_server._StandupCtx(_common.standup_view()["view"], "/standup")
    assert 'value="done"' in ctx.action_row(ctx.action("2026-01-13-a1"))
    assert "/standup/decide" not in ctx.action_row(ctx.action("2026-01-15-a1"), show_owner=True)


def test_resolved_action_shows_outcome_instead_of_age():
    _write()
    ctx = review_server._StandupCtx(_common.standup_view()["view"], "/standup")
    row = ctx.action_row(ctx.action("2026-01-12-a2"))
    assert "Done Jan 14" in row and "Renewed" in row and "open " not in row and 'value="open"' in row


# --- sub-pages ---------------------------------------------------------------

def test_topic_page_shows_history_newest_first_with_action_items_under_their_update():
    _write()
    page = _page(review_server._render_standup_topic_page, "Widgets|Widget rollout")
    assert page.index("Waiting on the vendor") < page.index("Rollout started.")
    assert page.index("Rollout started.") < page.index("Email the vendor")
    assert "raised at 2 standups, Jan 13 to Jan 15" in page


def test_topic_page_offers_decisions_only_while_quiet():
    _write()
    quiet = _page(review_server._render_standup_topic_page, "Widgets|Old widget")
    assert 'value="keep"' in quiet and "Suggested: possibly stuck." in quiet
    blocked = _page(review_server._render_standup_topic_page, "Widgets|Widget rollout")
    assert 'value="keep"' not in blocked


def test_topic_page_for_an_unknown_topic_says_so():
    _write()
    assert "in the standup data" in _page(review_server._render_standup_topic_page, "Nope|Nope")


def test_workstreams_page_lists_only_live_topics_and_filters():
    _write()
    page = _page(review_server._render_standup_workstreams_page)
    assert "Widget rollout" in page and "Old widget" not in page and "Certificates" not in page
    assert "Widget rollout" not in _page(review_server._render_standup_workstreams_page, q="nothing matches")
    assert "Widget rollout" not in _page(review_server._render_standup_workstreams_page, owner="Sam")


def test_closed_page_lists_done_and_dropped_topics():
    _write()
    page = _page(review_server._render_standup_closed_page)
    assert "Closed topics (1)" in page and "Certificates" in page


def test_actions_page_filters_by_state_owner_and_text():
    _write()
    assert "Action items (2)" in _page(review_server._render_standup_actions_page)
    assert "Action items (1)" in _page(review_server._render_standup_actions_page, show="resolved")
    assert "Action items (3)" in _page(review_server._render_standup_actions_page, show="all")
    assert "Action items (1)" in _page(review_server._render_standup_actions_page, owner="Sam")
    assert "Chase the vendor" in _page(review_server._render_standup_actions_page, q="chase")


def test_meeting_pages_list_standups_and_show_one_standups_notes():
    _write()
    listing = _page(review_server._render_standup_meetings_page)
    assert "Standups (2)" in listing and 'href="/standup/meetings/2026-01-15"' in listing
    one = _page(review_server._render_standup_meeting_page, "2026-01-14")
    assert "Earlier action items resolved here (1)" in one and "Renew the certificate" in one
    assert "in the standup data" in _page(review_server._render_standup_meeting_page, "1999-01-01")


def test_return_path_only_allows_standup_pages():
    ok = review_server._standup_return_path
    assert ok("/standup/actions?show=all&owner=Sam") == "/standup/actions?show=all&owner=Sam"
    assert ok(review_server._standup_topic_href("A, B & C|D")).startswith("/standup/topic/")
    for bad in ("https://evil.example", "//evil.example", "/tasks", "/standup//evil.example", "/standup?x=<b>", ""):
        assert ok(bad) == "/standup"


# --- decisions (/standup/decide) ---------------------------------------------

def test_record_refuses_an_unknown_decision_or_id():
    _write()
    assert _common.record_standup_decision("action", "2026-01-13-a1", "delete")[0] is False
    assert _common.record_standup_decision("action", "2099-01-01-a9", "done")[0] is False
    assert _common.record_standup_decision("action", "2026-01-15-a1", "done")[0] is False  # someone else's
    assert _common.record_standup_decision("topic", "Nope|Nope", "keep")[0] is False
    assert not _common.STANDUP_DECISIONS_FILE.exists()


def test_record_refuses_when_standup_data_is_missing():
    ok, msg, _ = _common.record_standup_decision("action", "2026-01-13-a1", "done")
    assert ok is False and "isn't available" in msg


def test_marking_an_action_done_moves_it_out_of_open_and_undo_restores_it():
    _write()
    ok, _, previous = _common.record_standup_decision("action", "2026-01-13-a1", "done")
    assert ok and previous is None
    view = _common.standup_view()["view"]
    assert view["my_open_actions"] == []
    assert view["my_resolved_actions"][0] == "2026-01-13-a1"
    r = view["actions"]["2026-01-13-a1"]["resolution"]
    assert r["by_you"] and r["pending"] and r["outcome"] == "done"
    assert _common.standup_open_action_count() == 0
    assert _common.record_standup_decision("action", "2026-01-13-a1", previous or "clear")[0]
    assert _common.standup_view()["view"]["my_open_actions"] == ["2026-01-13-a1"]


def test_still_open_moves_a_resolved_action_back_to_open():
    _write()
    assert _common.record_standup_decision("action", "2026-01-12-a2", "open")[0]
    view = _common.standup_view()["view"]
    assert view["my_open_actions"] == ["2026-01-12-a2", "2026-01-13-a1"]
    assert view["my_resolved_actions"] == []


def test_topic_decision_applies_and_a_later_mention_wins():
    _write()
    assert _common.record_standup_decision("topic", "Widgets|Old widget", "dropped")[0]
    t = _common.standup_view()["view"]["topics"]["Widgets|Old widget"]
    assert t["decision"] == "dropped" and t["status"] == "dropped"
    raised_again = _view()
    raised_again["topics"]["Widgets|Old widget"]["last_date"] = "2099-01-01"
    applied = _common.apply_standup_decisions(raised_again, _common.load_standup_decisions())
    assert applied["topics"]["Widgets|Old widget"]["decision"] is None


def test_decisions_file_holds_only_ids_decisions_and_timestamps():
    _write()
    _common.record_standup_decision("action", "2026-01-13-a1", "done")
    _common.record_standup_decision("topic", "Widgets|Old widget", "keep")
    data = json.loads(_common.STANDUP_DECISIONS_FILE.read_text())
    assert set(data["actions"]["2026-01-13-a1"]) == {"outcome", "at"}
    assert set(data["topics"]["Widgets|Old widget"]) == {"decision", "at"}


def test_apply_is_idempotent():
    decisions = {"actions": {"2026-01-13-a1": {"outcome": "done", "at": "2026-01-15T10:00:00"}},
                 "topics": {"Widgets|Old widget": {"decision": "keep", "at": "2026-01-15T10:00:00"}}}
    once = _common.apply_standup_decisions(_view(), decisions)
    assert _common.apply_standup_decisions(once, decisions) == once


def test_overview_offers_the_right_buttons_and_carries_return_to():
    _write()
    page = _page(review_server._render_standup_page, since="2026-01-13")
    assert 'action="/standup/decide"' in page
    assert 'value="done"' in page and 'value="dropped"' in page and 'value="keep"' in page and 'value="open"' in page
    assert 'name="return_to" value="/standup?since=2026-01-13"' in page
    assert "Possibly stuck (1)" in page and "No progress since it paused." in page


def test_undo_path_survives_the_double_decoding_of_the_undo_query_param():
    """do_GET runs unquote() on top of parse_qs's own decoding of ?undo=, so
    the undo target must be made of characters neither step changes."""
    key = "Research, Ops & CMS|Topic / 50% done"
    ret = "/standup/actions?show=all&owner=Sam"
    path = f"/standup/undo/topic/{review_server._b64url(key)}/clear/{review_server._b64url(ret)}"
    assert unquote(parse_qs("undo=" + quote(path))["undo"][0]) == path
    parts = path.strip("/").split("/")
    assert review_server._b64url_decode(parts[3]) == key and review_server._b64url_decode(parts[5]) == ret


# --- Add to My tasks and Zendesk ticket links ------------------------------

def test_add_to_my_tasks_creates_one_linked_local_draft():
    _write()
    result = _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=NOOP)
    assert result["ok"]
    task = _common.load_personal_tasks()[result["local_id"]]
    assert task["status"] == "draft" and task["subject"] == "Email the vendor"
    assert task["standup_action_id"] == "2026-01-13-a1"
    assert task["notes"] == "From the Jan 13 Operations Standup." and "http" not in task["notes"]
    again = _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=NOOP)
    assert again["ok"] is False and "already in My tasks" in again["error"]
    assert len(_common.load_personal_tasks()) == 1


def test_add_to_my_tasks_sees_archived_tasks_and_refuses_other_items():
    _write()
    _common.PERSONAL_TASKS_ARCHIVE_FILE.write_text(json.dumps([{"local_id": "old1", "status": "done", "standup_action_id": "2026-01-13-a1"}]))
    assert _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=NOOP)["ok"] is False
    assert _common.add_standup_action_to_tasks("2026-01-15-a1", log_fn=NOOP)["ok"] is False  # someone else's
    assert _common.add_standup_action_to_tasks("2026-01-12-a2", log_fn=NOOP)["ok"] is False  # already resolved
    assert _common.load_personal_tasks() == {}


def test_page_swaps_the_button_for_a_chip_once_the_task_exists():
    _write()
    assert 'action="/standup/add-task"' in _page(review_server._render_standup_page)
    _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=NOOP)
    page = _page(review_server._render_standup_page, tasks_by_action=_common.standup_tasks_by_action())
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
    page = _page(review_server._render_standup_page, tagged_tickets=tagged)
    assert 'Tagged "standup" in Zendesk (1)' in page and "Widget outage" in page
    assert "Mentioned at the" in page and "Jan 14 standup" in page
    assert "Mentioned in the notes (1)" in page and "#222222" in page


def test_tickets_section_shows_a_zendesk_error_instead_of_an_empty_list():
    _write()
    assert "Not connected to Zendesk." in _page(review_server._render_standup_page, tickets_error="Not connected to Zendesk.")
