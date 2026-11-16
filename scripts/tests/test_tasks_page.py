"""/tasks lists only not-yet-promoted drafts, in a two-column grid; there is no
Open panel, and the Tasks nav badge counts drafts only. Fixtures are made up."""
import _common
import review_server


def _draft(i):
    return {"local_id": f"d{i}", "subject": f"Draft {i}", "notes": "", "category": "", "est_mins": None,
            "created_at": "2026-10-20T10:00:00", "status": "draft"}


def _page(drafts):
    return review_server._render_tasks_page(drafts).decode()


def test_no_open_panel_jump_link_or_solve_buttons():
    page = _page([_draft(1)]).split("</style>")[-1]   # markup only: CSS comments may still mention the old buttons
    for gone in ('id="tasks-open"', "Open (", "#tasks-open", "Nothing open", "Mark done"):
        assert gone not in page
    # the new-task form's own "Add + Quick Solve" button is a different thing and stays
    assert "Add + Quick Solve" in page


def test_drafts_fill_the_page_in_a_two_column_grid():
    page = _page([_draft(1), _draft(2), _draft(3)])
    assert 'id="tasks-drafts"' in page and "Not yet in Zendesk (3)" in page
    assert 'class="mini-ticket-grid mini-ticket-grid-2"' in page
    assert "two-panel" not in page.split("</style>")[-1]
    assert "Draft 1" in page and "Draft 3" in page


def test_empty_state():
    page = _page([])
    assert "Not yet in Zendesk (0)" in page and "Nothing here yet." in page


def test_nav_tabs_carry_no_count_badges(monkeypatch):
    monkeypatch.setattr(_common, "draft_personal_tasks", lambda: [_draft(1), _draft(2)])
    monkeypatch.setattr(_common, "open_personal_tasks", lambda: [{"local_id": "o1"}] * 5)
    nav = review_server._top_nav_html(current_path="/tasks")
    assert "nav-badge" not in nav
    tag = nav[nav.index('href="/tasks"'):]
    assert tag[:tag.index("</a>")].endswith('<span class="top-nav-label">Tasks</span>')


def test_nav_badge_hidden_with_no_drafts(monkeypatch):
    monkeypatch.setattr(_common, "draft_personal_tasks", lambda: [])
    monkeypatch.setattr(_common, "open_personal_tasks", lambda: [{"local_id": "o1"}])
    nav = review_server._top_nav_html(current_path="/tasks")
    tag = nav[nav.index('href="/tasks"'):]
    assert "nav-badge" not in tag[:tag.index("</a>")]


def _completed(n):
    return [{"ticket_id": 100 + i, "subject": f"Done <b>{i}</b>", "completed_at": f"2026-10-2{i}T14:05:00"} for i in range(1, n + 1)]


def test_recently_completed_is_a_column_to_the_right_of_the_main_body(monkeypatch):
    monkeypatch.setattr(_common, "completed_personal_tasks", lambda limit=10: _completed(2))
    page = _page([_draft(1)])
    body = page.split("</style>")[-1]
    assert "Scrolls sideways: Recently completed is to the right." in body
    board = body[body.index('<div class="tasks-board">'):]
    assert board.index('class="tasks-main"') < board.index('id="tasks-drafts"') < board.index('id="tasks-completed"')
    assert board.index('class="ticket"') < board.index('id="tasks-completed"')            # the new-task form is in the main body
    # the completed column is a sibling of the main body, not nested inside it
    main = board[board.index('class="tasks-main"'):board.index('id="tasks-completed"')]
    # (the slice starts after the main div's own opening tag and ends inside the completed column's)
    assert main.count("<div") == main.count("</div>")
    assert "Recently completed (2)" in board and "Done &lt;b&gt;1&lt;/b&gt;" in board and "<b>1</b>" not in board
    assert 'class="recently-handled"' not in body


def test_completed_column_empty_state_and_css(monkeypatch):
    monkeypatch.setattr(_common, "completed_personal_tasks", lambda limit=10: [])
    page = _page([])
    assert "Recently completed (0)" in page and "Nothing completed yet." in page
    css = page.split("</style>")[0]
    assert ".tasks-board {" in css and "overflow-x: auto" in css.split(".tasks-board {")[1].split("}")[0]
    assert "flex: 0 0 calc(100% - 5rem)" in css          # the main body leaves the next column's edge showing
