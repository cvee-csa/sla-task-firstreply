"""/standup/people: one person's action items from the standup notes
(review_server._render_standup_people_page). Every fixture is made up (see
test_standup_page.py); this repo is public, so no real notes appear here."""
import _common
import review_server
from test_standup_page import _action, _page, _view, _write

render = review_server._render_standup_people_page


def _setup(monkeypatch, default="Sam", extra_actions=None):
    monkeypatch.setattr(_common, "STANDUP_DEFAULT_PERSON", default)
    view = _view()
    view["actions"].update(extra_actions or {})
    _write(view)


def test_opens_on_the_default_person(monkeypatch):
    _setup(monkeypatch, default="Sam")
    out = _page(render)
    assert "Chase the vendor" in out and "Email the vendor" not in out
    assert "Sam: 1 open, 0 done, 0 dropped across 1 standup." in out
    assert 'value="Sam" selected' in out


def test_unknown_default_falls_back_to_the_first_person(monkeypatch):
    _setup(monkeypatch, default="Nobody")
    out = _page(render)
    assert 'value="Alex" selected' in out and "Alex: 1 open, 1 done" in out


def test_picker_lists_people_but_not_group_or_unassigned(monkeypatch):
    _setup(monkeypatch, extra_actions={
        "2026-01-16-a9": _action("2026-01-16-a9", "2026-01-16", "Group thing", "Widgets|Widget rollout", owner="Group"),
        "2026-01-16-a8": _action("2026-01-16-a8", "2026-01-16", "Orphan thing", "Widgets|Widget rollout", owner="Unassigned"),
    })
    out = _page(render)
    assert 'value="Alex"' in out and 'value="Sam"' in out
    assert 'value="Group"' not in out and 'value="Unassigned"' not in out


def test_open_items_first_then_resolved_with_counts_and_inferred_marker(monkeypatch):
    _setup(monkeypatch)
    out = _page(render, person="Alex")
    assert out.index("Open (1)") < out.index("Done and dropped (1)")
    assert out.index("Email the vendor") < out.index("Renew the certificate")
    assert "Alex: 1 open, 1 done, 0 dropped across 2 standups; 1 with an inferred owner." in out
    assert 'class="standup-inferred"' in out          # the one inferred-owner item is marked
    assert "Name was a guess" in out                  # its check note rides along


def test_open_items_are_oldest_first_and_resolved_newest_first(monkeypatch):
    _setup(monkeypatch, extra_actions={
        "2026-01-05-a1": _action("2026-01-05-a1", "2026-01-05", "Older open task", "Widgets|Widget rollout", owner="Alex"),
        "2026-01-06-a1": _action("2026-01-06-a1", "2026-01-06", "Older done task", "Widgets|Widget rollout", owner="Alex",
                                 resolution={"date": "2026-01-07", "outcome": "dropped", "note": None, "by_you": False}),
    })
    out = _page(render, person="Alex")
    assert out.index("Older open task") < out.index("Email the vendor")                 # oldest open first
    assert out.index("Renew the certificate") < out.index("Older done task")            # newest resolved first
    assert "1 done, 1 dropped across 4 standups" in out


def test_search_filters_by_task_and_topic(monkeypatch):
    _setup(monkeypatch)
    assert "Renew the certificate" in _page(render, person="Alex", q="certificates")   # topic name
    out = _page(render, person="Alex", q="renew")
    assert "Renew the certificate" in out and "Email the vendor" not in out
    assert "No action items" in _page(render, person="Alex", q="zzz")


def test_unknown_or_hostile_person_param_falls_back_safely(monkeypatch):
    _setup(monkeypatch, default="Sam")
    out = _page(render, person="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in out and 'value="Sam" selected' in out


def test_subnav_links_the_page_and_marks_it_current(monkeypatch):
    _setup(monkeypatch)
    out = _page(render, person="Alex")
    assert 'href="/standup/people"' in out and "By person" in out
    assert 'class="sla-range-btn active" href="/standup/people" aria-current="page"' in out


def test_missing_data_shows_the_error_page():
    snap = _common.standup_snapshot()
    out = render(snap).decode()
    assert "No standup data found" in out
