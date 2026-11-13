"""Bulk resolve on /standup: age buckets, the confirmation page, the batch
write with its decision log, backups and one-step Undo, plus the Needs
attention page. Every fixture is made up (this repo is public); dates are
relative to today so the age buckets don't drift.
"""
import io
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlparse

import _common
import review_server
from test_standup_page import _action, _entry, _topic, _view, _write


def _ago(days):
    return (datetime.now().date() - timedelta(days=days)).isoformat()


def _bulk_view():
    live = _topic("Widgets|Live", "Live topic", "active", [_entry("e1", _ago(100), "Going.")])
    done = _topic("Widgets|Finished", "Finished topic", "done", [_entry("e2", _ago(110), "Shipped.", status="done")])
    quiet = _topic("Widgets|Dusty", "Dusty topic", "quiet", [_entry("e3", _ago(80), "Paused.", status="quiet")],
                   triage={"suggest": "likely_done", "why": "Looks finished."})
    acts = {
        "a-week": _action("a-week", _ago(3), "Mine, new", "Widgets|Live", owner="Alex"),
        "a-month": _action("a-month", _ago(20), "Sam, a few weeks", "Widgets|Live", owner="Sam"),
        "a-quarter": _action("a-quarter", _ago(45), "Sam, over a month", "Widgets|Live", owner="Sam"),
        "a-old1": _action("a-old1", _ago(90), "Sam, under finished", "Widgets|Finished", owner="Sam"),
        "a-old2": _action("a-old2", _ago(100), "Nobody, under finished", "Widgets|Finished", owner="Unassigned",
                          check="Owner was a guess"),
        "a-group": _action("a-group", _ago(5), "Everybody", "Widgets|Live", owner="Group"),
        "a-resolved": _action("a-resolved", _ago(50), "Already closed", "Widgets|Live", owner="Sam",
                              resolution={"date": _ago(40), "outcome": "done", "note": "ok", "by_you": False}),
    }
    for a in acts.values():
        a["topic"] = {"Widgets|Live": "Live topic", "Widgets|Finished": "Finished topic"}[a["topic_key"]]
    return _view(topics={t["key"]: t for t in (live, done, quiet)}, actions=acts, quiet_topics=["Widgets|Dusty"],
                 my_open_actions=["a-week"], my_resolved_actions=[], my_topics=[], meetings=[], waiting_on=[])


def _setup():
    _write(_bulk_view())
    _common.STANDUP_DECISIONS_FILE.parent.mkdir(exist_ok=True)


def _log():
    path = _common.standup_decisions_log_file()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _decisions():
    return json.loads(_common.STANDUP_DECISIONS_FILE.read_text())


# --- age buckets ------------------------------------------------------------

def test_age_bucket_boundaries():
    b = _common.standup_age_bucket
    assert [b(d) for d in (0, 7, 8, 30, 31, 60, 61, 400)] == ["week", "week", "month", "month", "quarter", "quarter", "old", "old"]
    assert b(None) is None


def test_age_days_handles_bad_dates_and_the_future():
    assert _common.standup_action_age_days({"date": "nonsense"}) is None
    assert _common.standup_action_age_days({}) is None
    assert _common.standup_action_age_days({"date": _ago(-3)}) == 0


def test_age_counts_cover_open_items_only():
    view = _bulk_view()
    counts = _common.standup_age_counts(_common.standup_open_actions(view))
    assert counts == {"week": 2, "month": 1, "quarter": 1, "old": 2}


def test_candidates_filter_by_owner_age_text_topic_and_closed_topics():
    view = _bulk_view()
    ids = lambda **kw: [a["id"] for a in _common.standup_bulk_candidates(view, **kw)]  # noqa: E731
    assert ids(owner="Sam") == ["a-old1", "a-quarter", "a-month"]
    assert ids(age="old") == ["a-old1", "a-old2"]
    assert ids(owner="Sam", age="old") == ["a-old1"]
    assert ids(q="finished") == ["a-old1", "a-old2"]
    assert sorted(ids(closed_topics=True)) == ["a-old1", "a-old2"]
    assert sorted(ids(topic_key="Widgets|Finished")) == ["a-old1", "a-old2"]
    assert ids(age="bogus") == []                      # a bad link never widens the set
    assert "a-resolved" not in ids()


def test_candidates_are_grouped_oldest_first_within_owner():
    view = _bulk_view()
    sam = [a["id"] for a in _common.standup_bulk_candidates(view, owner="Sam")]
    assert sam == ["a-old1", "a-quarter", "a-month"]


# --- the batch write ---------------------------------------------------------

def test_batch_marks_anyones_open_items_and_logs_each_one():
    _setup()
    out = _common.record_standup_batch(["a-old1", "a-old2", "a-week"], "dropped")
    assert out["ok"] and out["applied"] == 3 and out["skipped"] == 0 and out["batch"]
    data = _decisions()
    assert {k: v["outcome"] for k, v in data["actions"].items()} == {"a-old1": "dropped", "a-old2": "dropped", "a-week": "dropped"}
    log = _log()
    assert len(log) == 3 and {e["batch"] for e in log} == {out["batch"]} and {e["source"] for e in log} == {"batch"}
    assert all(e["from"] is None and e["to"] == "dropped" for e in log)
    # applied immediately in the view, marked as pending
    view = _common.standup_view()["view"]
    assert view["actions"]["a-old1"]["resolution"]["outcome"] == "dropped" and view["actions"]["a-old1"]["resolution"]["pending"]


def test_batch_skips_ids_that_are_not_open_actions_without_writing_them():
    _setup()
    out = _common.record_standup_batch(["a-month", "a-resolved", "no-such-id", "a-month"], "done")
    assert out["ok"] and out["applied"] == 1 and out["skipped"] == 2     # duplicate collapsed, two unusable
    assert list(_decisions()["actions"]) == ["a-month"]


def test_batch_refuses_bad_input_and_writes_nothing():
    _setup()
    assert not _common.record_standup_batch(["a-month"], "open")["ok"]
    assert not _common.record_standup_batch(["a-month"], "clear")["ok"]
    assert not _common.record_standup_batch([], "done")["ok"]
    assert not _common.record_standup_batch(["no-such-id", "a-resolved"], "done")["ok"]
    assert not _common.record_standup_batch([f"x{i}" for i in range(_common.STANDUP_BATCH_MAX_ITEMS + 1)], "done")["ok"]
    assert not _common.STANDUP_DECISIONS_FILE.exists() and not _common.standup_decisions_log_file().exists()


def test_batch_refuses_when_standup_data_or_folder_is_missing():
    assert "isn't available" in _common.record_standup_batch(["a-month"], "done")["message"]
    _write(_bulk_view())
    _common.STANDUP_DECISIONS_FILE = _common.STANDUP_DECISIONS_FILE.parent / "missing_dir" / "decisions.json"
    assert "No meetingnotestool folder" in _common.record_standup_batch(["a-month"], "done")["message"]


def test_batch_that_changes_nothing_writes_nothing():
    _setup()
    assert _common.record_standup_decision("action", "a-week", "done")[0]
    # a-week now has a decision, so it isn't open in the view; nothing eligible
    out = _common.record_standup_batch(["a-week"], "done")
    assert not out["ok"]
    assert len(_log()) == 1


def test_log_and_decisions_hold_ids_and_decisions_only():
    _setup()
    _common.record_standup_batch(["a-old1"], "done")
    for entry in _log():
        assert set(entry) == {"at", "source", "batch", "undo_of", "kind", "id", "from", "to"}
    raw = _common.standup_decisions_log_file().read_text() + _common.STANDUP_DECISIONS_FILE.read_text()
    assert "Sam, under finished" not in raw and "Finished topic" not in raw


def test_decisions_json_stays_in_the_shape_build_py_reads():
    _setup()
    _common.record_standup_batch(["a-old1"], "done")
    data = _decisions()
    assert set(data) == {"schema", "actions", "topics"} and data["schema"] == 1
    assert set(data["actions"]["a-old1"]) == {"outcome", "at"}


def test_single_decisions_are_logged_too():
    _setup()
    ok, _, prev = _common.record_standup_decision("action", "a-week", "done")
    assert ok and prev is None
    ok, _, prev = _common.record_standup_decision("action", "a-week", "clear")
    assert ok and prev == "done"
    log = _log()
    assert [(e["source"], e["from"], e["to"]) for e in log] == [("single", None, "done"), ("single", "done", None)]
    assert all(e["batch"] is None for e in log)


def test_unwritable_log_blocks_the_change():
    _setup()
    _common.standup_decisions_log_file().mkdir()          # appending to a directory fails
    ok, msg, _ = _common.record_standup_decision("action", "a-week", "done")
    assert not ok and "Nothing was recorded" in msg and not _common.STANDUP_DECISIONS_FILE.exists()
    out = _common.record_standup_batch(["a-month"], "done")
    assert not out["ok"] and not _common.STANDUP_DECISIONS_FILE.exists()


# --- batch undo --------------------------------------------------------------

def test_undo_restores_what_each_item_was_before():
    _setup()
    assert _common.record_standup_decision("action", "a-week", "open")[0]        # an explicit "still open" first
    out = _common.record_standup_batch(["a-week", "a-month", "a-old1"], "done")
    # a-week has decision "open" (not resolved), so it is eligible and its previous value is "open"
    assert out["applied"] == 3
    undo = _common.undo_standup_batch(out["batch"])
    assert undo["ok"] and undo["restored"] == 3 and undo["skipped"] == 0
    data = _decisions()
    assert data["actions"]["a-week"]["outcome"] == "open"
    assert "a-month" not in data["actions"] and "a-old1" not in data["actions"]
    view = _common.standup_view()["view"]
    assert view["actions"]["a-month"]["resolution"] is None
    assert [e["undo_of"] for e in _log() if e["source"] == "undo"] == [out["batch"]] * 3


def test_undo_leaves_items_changed_since_alone_and_is_idempotent():
    _setup()
    out = _common.record_standup_batch(["a-month", "a-quarter"], "done")
    # a later call on one of them (re-opened by hand)
    assert _common._apply_standup_changes([{"kind": "action", "id": "a-month", "to": "open"}], "single")
    undo = _common.undo_standup_batch(out["batch"])
    assert undo["restored"] == 1 and undo["skipped"] == 1
    assert _decisions()["actions"]["a-month"]["outcome"] == "open"
    assert "a-quarter" not in _decisions()["actions"]
    again = _common.undo_standup_batch(out["batch"])
    assert again["ok"] and again["restored"] == 0 and again["skipped"] == 2


def test_undo_rejects_unknown_or_malformed_batch_ids():
    _setup()
    assert not _common.undo_standup_batch("../../etc")["ok"]
    assert not _common.undo_standup_batch("")["ok"]
    assert "isn't in the decision log" in _common.undo_standup_batch("20260101T000000-abcdef")["message"]


def test_undo_does_not_touch_other_batches():
    _setup()
    first = _common.record_standup_batch(["a-month"], "done")
    second = _common.record_standup_batch(["a-quarter"], "dropped")
    assert _common.undo_standup_batch(first["batch"])["restored"] == 1
    assert list(_decisions()["actions"]) == ["a-quarter"]
    assert second["batch"] != first["batch"]


def test_batch_ids_match_the_pattern_the_undo_route_accepts():
    _setup()
    out = _common.record_standup_batch(["a-month"], "done")
    assert _common._STANDUP_BATCH_ID_RE.match(out["batch"])


# --- backups -----------------------------------------------------------------

def test_first_write_of_the_day_backs_up_the_file_it_replaces():
    _setup()
    _common.record_standup_decision("action", "a-week", "done")         # no file yet: nothing to copy
    folder = _common.standup_decisions_backup_dir()
    assert not folder.exists() or not list(folder.glob("decisions-*.json"))
    before = _common.STANDUP_DECISIONS_FILE.read_text()
    _common.record_standup_decision("action", "a-week", "clear")
    daily = folder / f"decisions-{datetime.now():%Y-%m-%d}.json"
    assert daily.read_text() == before
    _common.record_standup_decision("action", "a-week", "dropped")       # same day: daily copy not replaced
    assert daily.read_text() == before


def test_bulk_change_takes_its_own_pre_change_snapshot():
    _setup()
    _common.record_standup_decision("action", "a-week", "done")
    snapshot_state = _common.STANDUP_DECISIONS_FILE.read_text()
    out = _common.record_standup_batch(["a-month"], "done")
    snaps = list(_common.standup_decisions_backup_dir().glob("decisions-*-before-*.json"))
    assert len(snaps) == 1 and out["batch"] in snaps[0].name
    assert snaps[0].read_text() == snapshot_state


def test_backups_are_rotated():
    _setup()
    folder = _common.standup_decisions_backup_dir()
    folder.mkdir()
    start = datetime(2025, 1, 1)
    for i in range(40):
        (folder / f"decisions-{(start + timedelta(days=i)):%Y-%m-%d}.json").write_text("{}")
    _common.record_standup_decision("action", "a-week", "done")
    _common.record_standup_decision("action", "a-week", "clear")
    dailies = sorted(folder.glob("decisions-????-??-??.json"))
    assert len(dailies) == _common.STANDUP_BACKUPS_KEEP
    assert dailies[0].name == "decisions-2025-01-12.json"
    assert dailies[-1].name == f"decisions-{datetime.now():%Y-%m-%d}.json"


# --- pages -------------------------------------------------------------------

def _render(fn, **kw):
    return fn(_common.standup_view(), **kw).decode()


def test_actions_page_shows_age_pills_with_counts_and_filters_by_age():
    _setup()
    page = _render(review_server._render_standup_actions_page, age="old")
    assert "All ages (6)" in page and "This week (2)" in page and "8–30 days (1)" in page and "60+ days (2)" in page
    assert "Sam, under finished" in page and "Nobody, under finished" in page
    assert "Mine, new" not in page and "Sam, a few weeks" not in page
    assert 'name="age" value="old"' in page                      # the filter form keeps the age
    assert 'href="/standup/actions?age=month"' in page


def test_bulk_bar_needs_an_age_or_search_and_carries_the_scope():
    _setup()
    plain = _render(review_server._render_standup_actions_page)
    assert 'action="/standup/bulk"' not in plain
    page = _render(review_server._render_standup_actions_page, age="old", owner="Sam")
    assert 'action="/standup/bulk"' in page and 'name="decision" value="done"' in page and 'name="decision" value="dropped"' in page
    assert 'name="age" value="old"' in page and 'name="owner" value="Sam"' in page
    assert 'name="return_to" value="/standup/actions?owner=Sam&amp;age=old"' in page
    assert "1 open item " in page
    by_search = _render(review_server._render_standup_actions_page, q="finished")
    assert 'action="/standup/bulk"' in by_search


def test_resolved_view_has_no_age_pills_or_bulk_bar():
    _setup()
    page = _render(review_server._render_standup_actions_page, show="resolved", age="old")
    assert "Open for:" not in page and 'action="/standup/bulk"' not in page and "Already closed" in page


def test_people_page_has_age_pills_and_bulk_bar_for_that_person():
    _setup()
    page = _render(review_server._render_standup_people_page, person="Sam", age="quarter")
    assert "Open for:" in page and "31–60 days (1)" in page
    assert "Sam, over a month" in page and "Sam, a few weeks" not in page
    assert 'name="owner" value="Sam"' in page and 'action="/standup/bulk"' in page
    assert "3 open" in page                                         # the summary still counts every open item
    assert 'name="age" value="quarter"' in page


def test_bulk_page_lists_every_item_grouped_by_owner_with_ticked_boxes():
    _setup()
    page = _render(review_server._render_standup_bulk_page, decision="dropped", age="old", return_to="/standup/actions?age=old")
    assert "Mark 2 open action items as dropped?" in page
    assert page.count('type="checkbox" name="id"') == 2 and page.count(" checked>") == 2
    assert 'value="a-old1"' in page and 'value="a-old2"' in page
    assert "Sam (1)" in page and "Unassigned (1)" in page
    assert "2 of these belong to someone other than you" in page
    assert 'method="post" action="/standup/bulk"' in page
    assert 'class="trigger" id="standup-bulk-confirm"' in page            # the one filled button on the page
    assert 'name="return_to" value="/standup/actions?age=old"' in page
    assert "open 60+ days" in page


def test_bulk_page_scopes_by_topic_closed_topics_and_text():
    _setup()
    topic = review_server._b64url("Widgets|Finished")
    page = _render(review_server._render_standup_bulk_page, decision="done", topic=topic)
    assert "Mark 2 open action items as done?" in page and "under the topic Finished topic" in page
    page = _render(review_server._render_standup_bulk_page, decision="done", closed="1")
    assert "under done or dropped topics" in page and page.count('name="id"') == 2
    page = _render(review_server._render_standup_bulk_page, decision="done", q="everybody")
    assert page.count('name="id"') == 1 and 'value="a-group"' in page and "Group (1)" in page


def test_bulk_page_refuses_bad_scope_without_listing_anything():
    _setup()
    bad_decision = _render(review_server._render_standup_bulk_page, decision="open")
    assert 'name="id"' not in bad_decision and "only mark items done or dropped" in bad_decision
    bad_owner = _render(review_server._render_standup_bulk_page, decision="done", owner="Nobody Named This")
    assert 'name="id"' not in bad_owner and "Nothing to resolve" in bad_owner
    bad_age = _render(review_server._render_standup_bulk_page, decision="done", age="forever")
    assert 'name="id"' not in bad_age


def test_bulk_page_escapes_task_text_and_return_path():
    view = _bulk_view()
    view["actions"]["a-old1"]["task"] = "<img src=x onerror=1>"
    _write(view)
    page = _render(review_server._render_standup_bulk_page, decision="done", age="old", return_to="https://evil.example/")
    assert "<img src=x" not in page and "&lt;img src=x" in page
    assert 'name="return_to" value="/standup"' in page            # an off-site return path falls back


def test_attention_page_sections_and_counts():
    _setup()
    page = _render(review_server._render_standup_attention_page)
    assert "Open items under finished topics (2)" in page
    assert "Quiet topics awaiting a decision (1)" in page and "Likely done (1)" in page
    assert "No clear owner (2)" in page                              # Unassigned + Group
    assert "Marked “check” in the notes (1)" in page
    assert "Open more than 30 days (3)" in page                      # quarter 1 + old 2
    assert 'href="/standup/actions?age=old"' in page and 'href="/standup/actions?age=quarter"' in page
    assert 'name="closed" value="1"' in page
    assert 'name="owner" value="Unassigned"' in page and 'name="owner" value="Group"' in page


def test_attention_page_says_so_when_nothing_needs_attention():
    quiet_view = _view()
    quiet_view["actions"] = {}
    quiet_view["quiet_topics"] = []
    quiet_view["my_open_actions"] = []
    _write(quiet_view)
    page = _render(review_server._render_standup_attention_page)
    assert "Nothing needs attention." in page


def test_attention_page_errors_cleanly_without_standup_data():
    page = review_server._render_standup_attention_page({"view": None, "error": "No standup data found"}).decode()
    assert "No standup data found" in page


def test_topic_page_offers_bulk_only_for_a_finished_topic_with_open_items():
    _setup()
    done = _render(review_server._render_standup_topic_page, key="Widgets|Finished")
    assert 'action="/standup/bulk"' in done and f'name="topic" value="{review_server._b64url("Widgets|Finished")}"' in done
    live = _render(review_server._render_standup_topic_page, key="Widgets|Live")
    assert 'action="/standup/bulk"' not in live


def test_subnav_lists_needs_attention_and_marks_it_current():
    _setup()
    page = _render(review_server._render_standup_attention_page)
    assert 'href="/standup/attention" aria-current="page"' in page


# --- POST routes ---------------------------------------------------------------

class _Headers:
    def __init__(self, length):
        self._length = length

    def get(self, key, default=None):
        return str(self._length) if key == "Content-Length" else default


def _post(path, fields):
    body = "&".join(f"{k}={quote(v)}" for k, v in fields).encode()
    handler = review_server.Handler.__new__(review_server.Handler)
    handler.path = path
    handler.headers = _Headers(len(body))
    handler.rfile = io.BytesIO(body)
    seen = []
    handler._redirect = lambda location, *a, **k: seen.append(location)
    handler.do_POST()
    assert len(seen) == 1
    parsed = urlparse(seen[0])
    return parsed.path, parse_qs(parsed.query)


def test_post_bulk_records_the_batch_and_offers_one_undo():
    _setup()
    path, q = _post("/standup/bulk", [("decision", "done"), ("return_to", "/standup/actions?age=old"),
                                       ("id", "a-old1"), ("id", "a-old2"), ("id", "ghost")])
    assert path == "/standup/actions" and q["age"] == ["old"]
    assert q["msg"] == ["Marked 2 action items done. 1 weren't open any more and were left alone."]
    undo = unquote(q["undo"][0])
    parts = undo.strip("/").split("/")
    assert parts[:2] == ["standup", "undo-batch"] and review_server._b64url_decode(parts[3]) == "/standup/actions?age=old"
    assert len(_log()) == 2

    path, q = _post(undo, [])
    assert path == "/standup/actions" and q["msg"] == ["Undone: 2 action items restored."]
    assert _decisions()["actions"] == {}


def test_post_bulk_reports_a_refusal_and_returns_to_the_page():
    _setup()
    path, q = _post("/standup/bulk", [("decision", "done"), ("return_to", "/standup/attention")])
    assert path == "/standup/attention" and q["msg"] == ["Nothing was selected."] and "undo" not in q
    path, q = _post("/standup/bulk", [("decision", "open"), ("return_to", "https://evil.example/x"), ("id", "a-month")])
    assert path == "/standup" and "only mark items done or dropped" in q["msg"][0]


def test_post_undo_batch_with_a_bad_id_changes_nothing():
    _setup()
    path, q = _post("/standup/undo-batch/not-a-batch/" + review_server._b64url("/standup"), [])
    assert path == "/standup" and "isn't a bulk change" in q["msg"][0]
