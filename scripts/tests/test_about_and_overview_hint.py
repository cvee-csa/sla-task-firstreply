"""The About This Tool control (top-right of every page) and the Overview's sideways-scroll hint."""
import review_server


def _page():
    return review_server._page("<p>x</p>").decode()


def test_about_control_is_on_every_page_before_the_sidebar():
    out = _page()
    assert out.count('id="about-tool"') == 1
    assert out.index('id="about-tool"') < out.index('<div class="brand">')
    assert "About This Tool" in out.split('class="about-panel"')[1].split("</h2>")[0]


def test_about_text_names_every_data_source_and_what_it_writes():
    panel = _page().split('class="about-panel"')[1].split("</details>")[0]
    for needle in ("Zendesk API", "pending_drafts.json", "every 2 minutes", "72 hours", "sla_history.json",
                   "sla-report-slack", "standup_data.json", "meetingnotestool", "personal_tasks.json",
                   "local storage", "Public Reply", "Internal Comment", "Claim", "Pending Refresh", "Refresh data"):
        assert needle in panel, needle


def test_about_text_matches_the_code_it_describes(monkeypatch):
    """The numbers quoted in the text come from the same settings the code uses."""
    import _common
    assert _common.LOOKBACK_HOURS == 72
    assert "Pending Refresh" in review_server._header_meta_html()


def test_about_css_is_single_braced_and_the_panel_scrolls_when_tall():
    css = _page().split("</style>")[0]
    assert ".about-tool { position: absolute;" in css and ".about-panel {" in css
    assert "overflow-y: auto" in css.split(".about-panel {")[1].split("}")[0]
    assert ".about-tool {{" not in css


def test_overview_says_the_board_scrolls_sideways():
    out = review_server._render_overview_page({"items": []}).decode()
    assert "This board scrolls sideways." in out
    assert "hold Shift and use the mouse wheel" in out
    assert 'aria-label="Overview board, scrolls sideways"' in out


def test_about_intro_covers_airtable_and_github_and_names_the_repositories():
    panel = _page().split('class="about-panel"')[1].split("</details>")[0]
    intro = panel.split("<h3>")[0]
    assert "Airtable" in intro and "GitHub" in intro
    assert "IT-Security group" in panel and "ZENDESK_IT_SECURITY_GROUP_ID" in panel
    assert "open or closed" in panel
    section = panel.split("<h3>GitHub repositories</h3>")[1]
    for repo in ("cvee-csa/sla-task-firstreply", "cvee-csa/sla-report-slack",
                 "cvee-csa/meetingnotestool", "cvee-csa/zendeskmcp"):
        assert f"<code>{repo}</code>" in section, repo
    # only sla-report-slack is reached over the network by the running app
    assert "Update history" in section.split("cvee-csa/sla-report-slack")[1].split("<li>")[0]
