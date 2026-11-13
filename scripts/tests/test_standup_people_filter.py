"""Only _common.STANDUP_PEOPLE are offered as standup owner filters.
Fixtures are made up (see test_standup_page.py)."""
import _common
import review_server
from test_standup_page import _action, _page, _view, _write


def _setup(monkeypatch, people):
    monkeypatch.setattr(_common, "STANDUP_PEOPLE", people)
    view = _view()
    view["actions"].update({
        "2026-01-16-a5": _action("2026-01-16-a5", "2026-01-16", "Outsider task", "Widgets|Widget rollout", owner="Casey"),
        "2026-01-16-a6": _action("2026-01-16-a6", "2026-01-16", "Group task", "Widgets|Widget rollout", owner="Group"),
    })
    _write(view)
    return view


def _options(html_text, select_name):
    import re
    block = re.search(rf'<select name="{select_name}"[^>]*>(.*?)</select>', html_text, re.S).group(1)
    return re.findall(r'value="([^"]*)"', block)


def test_owner_options_are_only_the_configured_people_in_order(monkeypatch):
    view = _setup(monkeypatch, ["Sam", "Alex"])
    assert review_server._standup_owner_options(view) == ["Sam", "Alex"]


def test_matching_is_case_insensitive_and_uses_the_notes_spelling(monkeypatch):
    view = _setup(monkeypatch, ["sam", "ALEX"])
    assert review_server._standup_owner_options(view) == ["Sam", "Alex"]


def test_a_configured_person_missing_from_the_notes_is_still_offered(monkeypatch):
    view = _setup(monkeypatch, ["Alex", "Zed"])
    assert review_server._standup_owner_options(view) == ["Alex", "Zed"]


def test_filter_pages_do_not_offer_other_owners(monkeypatch):
    _setup(monkeypatch, ["Alex", "Sam"])
    out = _page(review_server._render_standup_actions_page)
    assert _options(out, "owner") == ["", "Alex", "Sam"]
    assert "Casey" not in _options(out, "owner")
    for render in (review_server._render_standup_topics_page, review_server._render_standup_tracked_page):
        assert 'name="owner"' not in _page(render)          # no owner filter on those lists


def test_everyone_still_shows_items_owned_by_other_people(monkeypatch):
    _setup(monkeypatch, ["Alex", "Sam"])
    out = _page(review_server._render_standup_actions_page, show="all")
    assert "Outsider task" in out


def test_owner_param_outside_the_choices_is_ignored(monkeypatch):
    _setup(monkeypatch, ["Alex", "Sam"])
    out = _page(review_server._render_standup_actions_page, show="all", owner="Casey")
    assert "Outsider task" in out and "Email the vendor" in out        # Everyone, not filtered to Casey
    mine = _page(review_server._render_standup_actions_page, show="all", owner="Sam")
    assert "Chase the vendor" in mine and "Outsider task" not in mine


def test_owner_filter_for_one_person_never_offers_outsiders(monkeypatch):
    _setup(monkeypatch, ["Sam", "Alex"])
    out = _page(review_server._render_standup_actions_page, owner="Sam")
    assert _options(out, "owner") == ["", "Sam", "Alex"]
    hostile = _page(review_server._render_standup_actions_page, owner="Casey")
    assert "Casey" not in _options(hostile, "owner")


def test_group_and_unassigned_are_never_filter_options_and_cannot_become_tasks(monkeypatch):
    _setup(monkeypatch, ["Alex", "Sam"])
    out = _page(review_server._render_standup_actions_page, show="all")
    assert "Group" not in _options(out, "owner") and "Unassigned" not in _options(out, "owner")
    assert "Group task" in out                                                    # still listed under Everyone
    assert out.count('name="id" value="2026-01-16-a6"') == 0                       # but no Add to My tasks for it
    assert out.count('name="id" value="2026-01-16-a5"') == 0                       # nor for someone outside the four
    assert _common.add_standup_action_to_tasks("2026-01-16-a6", log_fn=lambda *a, **k: None)["ok"] is False
    assert _common.add_standup_action_to_tasks("2026-01-16-a5", log_fn=lambda *a, **k: None)["ok"] is False
    assert _common.load_personal_tasks() == {}
    assert _common.add_standup_action_to_tasks("2026-01-15-a1", log_fn=lambda *a, **k: None)["ok"] is True   # Sam's
