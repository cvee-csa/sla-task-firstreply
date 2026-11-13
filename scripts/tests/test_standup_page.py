"""/standup: the three read-only lists (action items, topics A to Z, one
person's updates by date) built from the sibling meetingnotestool project's
standup data (_common.standup_snapshot and the review_server._render_standup_*
functions), plus Add to My tasks.

Every fixture here is made up. This repo is public, so real standup notes
must never appear in it -- not even as test data.
"""
import json

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
    if render is review_server._render_standup_actions_page:
        kwargs.setdefault("owner", "")      # these tests look at everyone's items; the default (you) is tested separately
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
    assert len(_common.standup_snapshot()["view"]["my_open_actions"]) == 1
    _write(_view(my_open_actions=[], actions={}, extra_padding="x" * 10))
    assert len(_common.standup_snapshot()["view"]["my_open_actions"]) == 0


def test_nav_shows_standup_tab_without_a_count_badge():
    _write()
    nav = review_server._top_nav_html("/standup")
    assert 'href="/standup"' in nav and 'aria-current="page"' in nav
    assert "nav-badge" not in nav
    assert 'href="/flagged"' not in nav and 'href="/security"' not in nav        # blocks on Overview, not tabs


# --- action items list (/standup) ------------------------------------------------

def test_actions_page_lists_open_items_newest_first_with_owner_and_topic():
    _write()
    page = _page(review_server._render_standup_actions_page)
    assert "Action items (2)" in page
    assert page.index("Chase the vendor") < page.index("Email the vendor")            # newest raised first
    assert "<strong>Sam</strong> &middot; Raised" in page                              # who it belongs to
    assert 'href="/standup/topic/' in page and "Widget rollout" in page
    assert "Renew the certificate" not in page                                         # resolved items stay out of the open view


def test_actions_page_filters_by_state_owner_and_text():
    _write()
    assert "Action items (1)" in _page(review_server._render_standup_actions_page, show="resolved")
    assert "Action items (3)" in _page(review_server._render_standup_actions_page, show="all")
    assert "Action items (1)" in _page(review_server._render_standup_actions_page, owner="Sam")
    assert "Chase the vendor" in _page(review_server._render_standup_actions_page, q="chase")
    assert "Action items (2)" in _page(review_server._render_standup_actions_page, show="bogus")     # falls back to open


def test_actions_page_has_no_decision_or_bulk_controls():
    _write()
    page = _page(review_server._render_standup_actions_page, show="all")
    for gone in ("/standup/decide", "/standup/bulk", "Still open", "Review and mark", 'value="dropped"'):
        assert gone not in page


def test_actions_page_error_when_data_is_missing():
    assert "No standup data found" in review_server._render_standup_actions_page(_common.standup_snapshot()).decode()


def test_actions_page_escapes_note_text_and_marks_guesses():
    view = _view()
    view["actions"]["2026-01-15-a1"]["task"] = "<b>bold</b> task"
    _write(view)
    page = _page(review_server._render_standup_actions_page)
    assert "&lt;b&gt;bold&lt;/b&gt; task" in page and "<b>bold</b>" not in page
    assert 'title="Name was a guess"' in page and 'class="standup-inferred"' in page


# --- Add to My tasks -------------------------------------------------------------

def test_open_items_offer_add_to_my_tasks_and_resolved_ones_do_not():
    _write()
    open_page = _page(review_server._render_standup_actions_page)
    assert open_page.count('action="/standup/add-task"') == 2
    assert 'name="return_to" value="/standup"' in open_page
    resolved = _page(review_server._render_standup_actions_page, show="resolved")
    assert 'action="/standup/add-task"' not in resolved


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


def test_add_to_my_tasks_works_for_anyones_open_item_and_names_the_owner():
    _write()
    result = _common.add_standup_action_to_tasks("2026-01-15-a1", log_fn=NOOP)           # Sam's
    assert result["ok"]
    assert _common.load_personal_tasks()[result["local_id"]]["notes"] == "From the Jan 15 Operations Standup (Sam's item)."


def test_add_to_my_tasks_sees_archived_tasks_and_refuses_resolved_or_unknown_items():
    _write()
    _common.PERSONAL_TASKS_ARCHIVE_FILE.write_text(json.dumps([{"local_id": "old1", "status": "done", "standup_action_id": "2026-01-13-a1"}]))
    assert _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=NOOP)["ok"] is False
    assert _common.add_standup_action_to_tasks("2026-01-12-a2", log_fn=NOOP)["ok"] is False  # already resolved
    assert _common.add_standup_action_to_tasks("no-such-id", log_fn=NOOP)["ok"] is False
    assert _common.load_personal_tasks() == {}


def test_add_to_my_tasks_refuses_when_standup_data_is_missing():
    assert "isn't available" in _common.add_standup_action_to_tasks("x", log_fn=NOOP)["error"]


def test_page_swaps_the_button_for_a_chip_once_the_task_exists():
    _write()
    assert 'action="/standup/add-task"' in _page(review_server._render_standup_actions_page)
    _common.add_standup_action_to_tasks("2026-01-13-a1", log_fn=NOOP)
    page = _page(review_server._render_standup_actions_page, tasks_by_action=_common.standup_tasks_by_action())
    assert page.count('action="/standup/add-task"') == 1 and "In My tasks (draft)" in page


def test_ticket_numbers_link_only_when_a_subdomain_is_configured(monkeypatch):
    monkeypatch.setattr(_common, "ZENDESK_SUBDOMAIN", "")
    assert "<a " not in review_server._standup_text_html("See ticket 123456.")
    monkeypatch.setattr(_common, "ZENDESK_SUBDOMAIN", "example")
    out = review_server._standup_text_html("See ticket 123456 and #234567 <b>now</b>.")
    assert 'href="https://example.zendesk.com/agent/tickets/123456"' in out
    assert 'href="https://example.zendesk.com/agent/tickets/234567"' in out
    assert "&lt;b&gt;now&lt;/b&gt;" in out


# --- topics, A to Z ----------------------------------------------------------------

def _topics_view():
    view = _view()
    for key, name in (("Widgets|zebra", "zebra crossing"), ("Widgets|Apple", "Apple pickup"), ("Widgets|1099", "1099 forms")):
        view["topics"][key] = _topic(key, name, "active", [_entry(f"{name}-1", "2026-01-10", f"About {name}.")])
    return view


def test_topics_are_alphabetical_case_insensitive_with_letter_headings():
    _write(_topics_view())
    page = _page(review_server._render_standup_topics_page)
    names = ["Apple pickup", "Certificates", "Old widget", "Widget rollout", "zebra crossing"]
    positions = [page.index(n) for n in names]
    assert positions == sorted(positions)
    assert page.index("1099 forms") < page.index("Apple pickup") or page.index("# (1)") < page.index("A (1)")
    for heading in ("A (1)", "C (1)", "O (1)", "W (1)", "Z (1)"):
        assert heading in page
    assert "6 topics, alphabetical." in page


def test_topics_page_shows_every_status_and_links_each_topic_to_its_history():
    _write()
    page = _page(review_server._render_standup_topics_page)
    for label in ("Blocked", "Quiet", "Done"):
        assert f">{label}</span>" in page
    assert page.count('href="/standup/topic/') == 3
    assert "raised at 2 standups, Jan 13 to" in page and "1 open action item" in page


def test_topics_page_search_and_error():
    _write()
    assert "Old widget" in _page(review_server._render_standup_topics_page, q="paused")        # matches an update
    out = _page(review_server._render_standup_topics_page, q="zzz")
    assert "No topics match this search." in out and "0 topics, alphabetical." in out


def test_topics_and_tracked_pages_error_when_data_is_missing():
    for render in (review_server._render_standup_topics_page, review_server._render_standup_tracked_page):
        assert "No standup data found" in render(_common.standup_snapshot()).decode()


def test_topic_page_shows_history_newest_first_with_task_buttons_and_no_decisions():
    _write()
    page = _page(review_server._render_standup_topic_page, "Widgets|Widget rollout")
    assert page.index("Jan 15") < page.index("Jan 13")
    assert "&lt;b&gt;patch&lt;/b&gt;" in page and "<b>patch</b>" not in page
    assert "Email the vendor" in page and 'action="/standup/add-task"' in page
    assert 'href="/standup/topics">&larr; all topics</a>' in page
    assert "/standup/decide" not in page and 'value="keep"' not in page
    assert "raised at 2 standups, Jan 13 to Jan 15" in page


def test_topic_page_for_an_unknown_topic_says_so():
    _write()
    assert "in the standup data" in _page(review_server._render_standup_topic_page, "Nope|Nope")


# --- the tracked person's items, by date ----------------------------------------------

def _tracked_view():
    view = _view()
    a = _topic("Widgets|Alpha", "Alpha topic", "active", [
        _entry("a1", "2026-01-05", "Sam started alpha.", owner="Sam"),
        _entry("a2", "2026-01-14", "Sam <i>finished</i> alpha.", owner="Sam", waiting_on="Vendor"),
        _entry("a3", "2026-01-14", "Alex commented.", owner="Alex"),
    ], owners=("Sam", "Alex"))
    b = _topic("Widgets|Beta", "Beta topic", "blocked", [
        _entry("b1", "2026-01-14", "Sam is stuck on beta.", owner="sam", status="blocked", owner_inferred=True, check="Guessed owner"),
    ], owners=("Sam",))
    view["topics"].update({"Widgets|Alpha": a, "Widgets|Beta": b})
    return view


def test_tracked_page_lists_only_that_persons_updates_grouped_by_date_newest_first():
    _write(_tracked_view())
    page = _page(review_server._render_standup_tracked_page)
    assert "Sam started alpha." in page and "Sam is stuck on beta." in page and "Alex commented." not in page
    assert page.index(">Jan 14</a> (2)") < page.index(">Jan 5</a> (1)") < page.index(">Jan 1</a> (1)")   # newest standup first
    assert page.index("Alpha topic") < page.index("Beta topic")                      # same day: by topic name
    assert "Paused." in page                                                          # Sam's earlier update on another topic
    assert "4 items Sam raised across 3 standups, newest first." in page


def test_tracked_page_escapes_text_and_shows_waiting_check_and_inferred_marks():
    _write(_tracked_view())
    page = _page(review_server._render_standup_tracked_page)
    assert "&lt;i&gt;finished&lt;/i&gt;" in page and "<i>finished</i>" not in page
    assert "Waiting on Vendor" in page and 'title="Guessed owner"' in page and "owner inferred" in page
    assert ">Blocked</span>" in page and 'href="/standup/topic/' in page


def test_tracked_page_label_follows_the_configured_person(monkeypatch):
    monkeypatch.setattr(_common, "STANDUP_TRACKED_PERSON", "alex")
    _write(_tracked_view())
    page = _page(review_server._render_standup_tracked_page)
    assert "Alex commented." in page and "Sam started alpha." not in page
    assert ">Alex&#x27;s items</a>" in page or ">Alex's items</a>" in page
    assert 'class="sla-range-btn active" href="/standup/tracked" aria-current="page"' in page


def test_tracked_page_search_and_empty_states(monkeypatch):
    _write(_tracked_view())
    assert "Sam is stuck on beta." not in _page(review_server._render_standup_tracked_page, q="alpha")
    assert "match this search." in _page(review_server._render_standup_tracked_page, q="zzz")
    monkeypatch.setattr(_common, "STANDUP_TRACKED_PERSON", "Nobody")
    assert "No standup items for Nobody in the notes." in _page(review_server._render_standup_tracked_page)


def test_person_entries_helper_is_case_insensitive_and_sorted():
    view = _tracked_view()
    got = _common.standup_person_entries(view, "SAM")
    assert [e["id"] for e in got] == ["a2", "b1", "a1", "2026-01-01-i1"]
    assert all(e["topic_key"] and e["topic_name"] for e in got)
    assert _common.standup_person_entries(view, "Sam", q="stuck")[0]["id"] == "b1"


# --- chrome and routing ---------------------------------------------------------------

def test_subnav_has_exactly_three_tabs_and_marks_the_current_one():
    _write()
    for render, path in ((review_server._render_standup_actions_page, "/standup"),
                         (review_server._render_standup_topics_page, "/standup/topics"),
                         (review_server._render_standup_tracked_page, "/standup/tracked")):
        page = _page(render)
        nav = page.split('class="standup-subnav"')[1].split("</nav>")[0]
        assert nav.count("<a ") == 3
        for label in ("Action items", "Topics", "Sam"):
            assert label in nav
        assert f'class="sla-range-btn active" href="{path}" aria-current="page"' in nav


def test_return_path_only_allows_standup_pages():
    ok = review_server._standup_return_path
    assert ok("/standup?show=all&owner=Sam") == "/standup?show=all&owner=Sam"
    assert ok(review_server._standup_topic_href("A, B & C|D")).startswith("/standup/topic/")
    for bad in ("https://evil.example", "//evil.example", "/tasks", "/standup//evil.example", "/standup?x=<b>", ""):
        assert ok(bad) == "/standup"


# --- the action items list defaults to you ----------------------------------------------

def _people(monkeypatch, people):
    monkeypatch.setattr(_common, "STANDUP_PEOPLE", people)


def test_actions_default_to_the_notes_me_when_they_are_a_filter_choice(monkeypatch):
    _people(monkeypatch, ["Sam", "Alex"])                  # view["me"] is "Alex", the second name
    _write()
    page = review_server._render_standup_actions_page(_common.standup_view()).decode()
    assert "Action items (1)" in page and "Email the vendor" in page and "Chase the vendor" not in page
    assert '<option value="Alex" selected>' in page and '<option value="all">' in page


def test_default_falls_back_to_the_first_configured_person(monkeypatch):
    _people(monkeypatch, ["Sam", "Zed"])                    # "me" (Alex) is not offered
    _write()
    page = review_server._render_standup_actions_page(_common.standup_view()).decode()
    assert "Chase the vendor" in page and "Email the vendor" not in page
    assert '<option value="Sam" selected>' in page


def test_everyone_is_still_selectable_and_is_remembered_in_links(monkeypatch):
    _people(monkeypatch, ["Sam", "Alex"])
    _write()
    for value in ("all", ""):
        page = review_server._render_standup_actions_page(_common.standup_view(), owner=value).decode()
        assert "Action items (2)" in page
        assert '<option value="all" selected>' in page
        assert "owner=all" in page                           # a form or redirect keeps Everyone instead of snapping back to you


def test_clear_link_shows_only_when_the_view_differs_from_the_default(monkeypatch):
    _people(monkeypatch, ["Sam", "Alex"])
    _write()
    view = _common.standup_view()
    default = review_server._render_standup_actions_page(view).decode()
    everyone = review_server._render_standup_actions_page(view, owner="all").decode()
    assert 'class="meta">Clear</a>' not in default
    assert 'class="meta">Clear</a>' in everyone


def test_unknown_owner_still_means_everyone(monkeypatch):
    _people(monkeypatch, ["Sam", "Alex"])
    _write()
    page = review_server._render_standup_actions_page(_common.standup_view(), owner="Mallory").decode()
    assert "Action items (2)" in page


def test_empty_default_view_explains_how_to_see_everyone(monkeypatch):
    _people(monkeypatch, ["Zed"])
    _write()
    page = review_server._render_standup_actions_page(_common.standup_view()).decode()
    assert "No action items for Zed match these filters. Choose Everyone" in page


def test_http_link_without_owner_shows_you_and_owner_all_shows_everyone(monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    _people(monkeypatch, ["Sam", "Alex"])
    _write()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        mine = urlopen(base + "/standup").read().decode()
        everyone = urlopen(base + "/standup?owner=all").read().decode()
    finally:
        srv.shutdown()
    assert "Action items (1)" in mine and "Email the vendor" in mine
    assert "Action items (2)" in everyone
