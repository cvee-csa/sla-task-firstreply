"""The countdown in the sidebar's status line is labelled "Pending Refresh"."""
import review_server


def test_countdown_is_labelled_pending_refresh():
    out = review_server._header_meta_html()
    assert "Pending Refresh: <span" in out
    assert "Next automatic check" not in out
    assert 'id="next-check"' in out          # the ticking script still finds it
    assert "Last checked:" in out


def test_status_line_sits_below_the_background_control():
    out = review_server._page("<p>x</p>", header_meta=review_server._header_meta_html()).decode()
    assert out.count('id="next-check"') == 1 and out.count('id="last-checked"') == 1
    status = out.index('id="last-checked"')
    assert out.index('class="bg-picker"') < status
    assert status < out.index('<div id="bg-toast"')           # still inside the sidebar block
    assert out.index('class="brand-nav-row"') < out.index('class="side-sky"') < out.index('class="brand-footer"')
    # the nav row no longer carries it
    nav_row = out[out.index('class="brand-nav-row"'):out.index('class="side-sky"')]
    assert "last-checked" not in nav_row


def test_timing_items_are_separate_spans_for_the_sidebar():
    out = review_server._header_meta_html()
    assert out.count('class="meta-item"') == 2 and 'class="meta-sep"' in out
    css = review_server._page("<p>x</p>").decode().split("</style>")[0]
    assert ".brand .meta-item { display: block; }" in css and ".brand .meta-sep { display: none; }" in css
