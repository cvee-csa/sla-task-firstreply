"""Only _common.STANDUP_PEOPLE are offered as standup owner filters and in the
By person picker. Fixtures are made up (see test_standup_page.py)."""
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
    assert review_server._standup_owner_options(view) == ["Sam", "Alex", "Group"]


def test_matching_is_case_insensitive_and_uses_the_notes_spelling(monkeypatch):
    view = _setup(monkeypatch, ["sam", "ALEX"])
    assert review_server._standup_owner_options(view) == ["Sam", "Alex", "Group"]


def test_a_configured_person_missing_from_the_notes_is_still_offered(monkeypatch):
    view = _setup(monkeypatch, ["Alex", "Zed"])
    assert review_server._standup_owner_options(view) == ["Alex", "Zed", "Group"]


def test_filter_pages_do_not_offer_other_owners(monkeypatch):
    _setup(monkeypatch, ["Alex", "Sam"])
    for render in (review_server._render_standup_workstreams_page, review_server._render_standup_closed_page,
                   review_server._render_standup_actions_page):
        out = _page(render)
        assert _options(out, "owner") == ["", "Alex", "Sam", "Group"]
        assert "Casey" not in _options(out, "owner")


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


def test_by_person_picker_offers_only_the_configured_people(monkeypatch):
    _setup(monkeypatch, ["Sam", "Alex"])
    out = _page(review_server._render_standup_people_page)
    assert _options(out, "person") == ["Sam", "Alex"]
    hostile = _page(review_server._render_standup_people_page, person="Casey")
    assert "Casey" not in _options(hostile, "person") and "Outsider task" not in hostile

