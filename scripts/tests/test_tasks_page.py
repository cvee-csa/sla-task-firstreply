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


def test_nav_badge_counts_drafts_only(monkeypatch):
    monkeypatch.setattr(_common, "draft_personal_tasks", lambda: [_draft(1), _draft(2)])
    monkeypatch.setattr(_common, "open_personal_tasks", lambda: [{"local_id": "o1"}] * 5)
    nav = review_server._top_nav_html(current_path="/tasks")
    tag = nav[nav.index('href="/tasks"'):]
    tag = tag[:tag.index("</a>")]
    assert '<span class="nav-badge">2</span>' in tag      # not 7: the open (promoted) tasks aren't counted


def test_nav_badge_hidden_with_no_drafts(monkeypatch):
    monkeypatch.setattr(_common, "draft_personal_tasks", lambda: [])
    monkeypatch.setattr(_common, "open_personal_tasks", lambda: [{"local_id": "o1"}])
    nav = review_server._top_nav_html(current_path="/tasks")
    tag = nav[nav.index('href="/tasks"'):]
    assert "nav-badge" not in tag[:tag.index("</a>")]
