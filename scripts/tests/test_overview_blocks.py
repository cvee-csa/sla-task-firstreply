"""The Standup and Discussion blocks on /overview (they used to be nav
tabs). Fixtures are made up (see test_standup_page.py)."""
import review_server
import _common
from test_standup_page import _view, _write

SLA = {"items": [], "error": None, "capped": False}


def _ticket(n, tag):
    return {"ticket_id": n, "subject": f"Ticket {n}", "status": "open", "requester": "Pat", "assignee": "Sam",
            "assignee_id": 5, "matched_tags": [tag], "url": f"https://example.zendesk.com/agent/tickets/{n}",
            "created_at": "2026-01-10T10:00:00Z", "updated_at": "2026-01-11T10:00:00Z"}


def _render(flagged=None, standup=True, **kw):
    snap = _common.standup_view() if standup else None
    return review_server._render_overview_page(SLA, flagged=flagged, standup_snapshot=snap,
                                               tasks_by_action=_common.standup_tasks_by_action(), **kw).decode()


def test_blocks_are_left_out_when_their_data_is_not_passed_in():
    out = review_server._render_overview_page(SLA).decode()
    assert "overview-block-standup" not in out and "overview-block-discussion" not in out


def test_standup_block_shows_your_open_items_oldest_first_and_links_to_the_lists():
    _write()
    out = _render()
    block = out.split("overview-block-standup")[1]
    assert "Email the vendor" in block and "Chase the vendor" not in block            # yours only (Alex); Sam's is not
    assert 'href="/standup" class="overview-block-link"' in block and "(1)" in block
    for href in ("/standup", "/standup/topics", "/standup/tracked"):
        assert f'href="{href}"' in block
    assert 'name="return_to" value="/overview"' in block and 'action="/standup/add-task"' in block


def test_standup_block_caps_rows_and_links_to_the_rest():
    view = _view()
    base = view["actions"]["2026-01-13-a1"]
    for n in range(2, 9):
        view["actions"][f"2026-01-1{n}-x"] = dict(base, id=f"2026-01-1{n}-x", date=f"2026-01-1{n}", task=f"Extra {n}")
    _write(view)
    block = _render().split("overview-block-standup")[1]
    assert block.count('class="mini-ticket"') == review_server._OVERVIEW_BLOCK_ROWS
    assert "of your open items" in block and 'href="/standup?owner=Alex"' in block


def test_standup_block_says_so_when_data_is_missing():
    block = _render().split("overview-block-standup")[1]
    assert "No standup data found" in block


def test_discussion_block_splits_blocked_and_standup_tickets_and_caps_rows():
    flagged = {"items": [_ticket(n, "block") for n in range(1, 8)] + [_ticket(20, "standup")], "error": None, "capped": False}
    block = _render(flagged=flagged).split("overview-block-discussion")[1]
    assert "Blocked (7)" in block and "Standup (1)" in block
    assert block.count("Ticket ") == review_server._OVERVIEW_BLOCK_ROWS + 1
    assert 'href="/flagged">All 7' in block and 'href="/flagged" class="overview-block-link"' in block and "(8)" in block


def test_discussion_block_empty_and_error_states():
    empty = _render(flagged={"items": [], "error": None, "capped": False}).split("overview-block-discussion")[1]
    assert 'No tickets tagged &quot;block&quot;' in empty and 'No tickets tagged &quot;standup&quot;' in empty
    err = _render(flagged={"items": [], "error": "Zendesk isn't connected", "capped": False}).split("overview-block-discussion")[1]
    assert "Zendesk isn&#x27;t connected" in err


def test_standup_return_path_allows_overview_only_as_the_bare_page():
    assert review_server._standup_return_path("/overview") == "/overview"
    assert review_server._standup_return_path("/overview?x=1") == "/standup"
    assert review_server._standup_return_path("/tasks") == "/standup"
