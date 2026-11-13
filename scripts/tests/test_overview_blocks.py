"""The Discussion and Security columns on /overview (they used to be nav
tabs) and the sideways-scrolling board they sit in. Fixtures are made up."""
import review_server

SLA = {"items": [], "error": None, "capped": False}


def _ticket(n, tag=None):
    t = {"ticket_id": n, "subject": f"Ticket {n}", "status": "open", "requester": "Pat", "assignee": "Sam",
         "assignee_id": 5, "url": f"https://example.zendesk.com/agent/tickets/{n}",
         "created_at": "2026-01-10T10:00:00Z", "updated_at": "2026-01-11T10:00:00Z"}
    if tag:
        t["matched_tags"] = [tag]
    return t


def _full(**kw):
    return review_server._render_overview_page(SLA, **kw).decode()


def _render(**kw):
    """Just the markup after the page's stylesheet (the CSS names the same classes)."""
    return _full(**kw).split("</style>")[-1]


def test_blocks_are_left_out_when_their_data_is_not_passed_in():
    out = _render()
    assert "overview-block-discussion" not in out and "overview-block-security" not in out
    assert "overview-block-standup" not in out


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


def test_security_block_shows_recent_tickets_caps_rows_and_links_to_the_page():
    security = {"items": [_ticket(n) for n in range(1, 9)], "error": None}
    block = _render(security=security).split("overview-block-security")[1]
    assert block.count("Ticket ") == review_server._OVERVIEW_BLOCK_ROWS
    assert 'href="/security" class="overview-block-link"' in block and "(8)" in block and 'href="/security">All 8' in block
    assert 'name="return_to" value="/overview"' in block                          # Claim returns to Overview


def test_security_block_empty_and_error_states():
    empty = _render(security={"items": [], "error": None}).split("overview-block-security")[1]
    assert "No recent tickets." in empty
    err = _render(security={"items": [], "error": "Zendesk isn't connected"}).split("overview-block-security")[1]
    assert "Zendesk isn&#x27;t connected" in err


def test_security_and_discussion_pages_keep_overview_highlighted():
    for page in (review_server._render_security_page([_ticket(1)]),
                 review_server._render_flagged_page([_ticket(1, "block")])):
        text = page.decode()
        top = text.split('class="top-nav"')[1].split("</div>")[0]
        assert 'class="top-nav-link active" href="/overview"' in top
        assert '<a href="/overview">&larr; back to Overview</a>' in text


def test_board_puts_people_then_unassigned_then_discussion_then_security_in_one_scrolling_row():
    out = _render(flagged={"items": [], "error": None, "capped": False}, security={"items": [], "error": None})
    assert out.count('class="overview-board"') == 1 and "person-grid" not in out
    board = out.split('class="overview-board"')[1]
    order = [board.index(x) for x in ("person-card-unassigned", "overview-block-discussion", "overview-block-security")]
    assert order == sorted(order)
    css = _full().split("</style>")[0].split(".overview-board {")[1].split("}")[0]
    assert "overflow-x: auto" in css


def test_board_still_shows_the_columns_when_the_ticket_scan_failed():
    out = review_server._render_overview_page({"items": [], "error": "Zendesk isn't connected", "capped": False},
                                              flagged={"items": [], "error": None, "capped": False}).decode().split("</style>")[-1]
    assert "Zendesk isn&#x27;t connected" in out and "overview-block-discussion" in out
    assert "person-card-unassigned" not in out
