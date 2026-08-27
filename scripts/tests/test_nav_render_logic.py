"""Pure string-output checks on review_server.py's header nav rendering --
_top_nav_html and _category_select_html -- run without a browser or a live
server, just calling the functions directly and asserting on the HTML they
return. Complements tests/test_nav_badge_caches.py (which covers the cache
layer these badges read) by covering the rendering layer on top of it: does
the right badge show up on the right link, does the active-page class land
on the right one, does a category selection round-trip correctly.

JS-driven behavior (the auto-fill-onto-subject 'change' listener, the
loading-bar, theme switching) isn't covered here -- that needs a real
browser and remains the ad hoc Playwright scripts' territory for now (see
the README's "Running tests" section).
"""
import review_server
import _common


def test_top_nav_has_no_badge_on_any_link_with_a_cold_cache():
    html = review_server._top_nav_html(current_path="/")
    assert 'href="/flagged"' in html
    assert 'href="/security"' in html
    # No draft/open tasks, no SLA data, healthy auth, cold flagged/security
    # caches -- nothing here should be flagged as needing attention.
    assert "nav-badge" not in html


def test_top_nav_marks_the_current_page_active():
    html = review_server._top_nav_html(current_path="/tasks")
    assert 'href="/tasks"' in html
    tasks_link_start = html.index('href="/tasks"')
    # The "active" class lives on the same <a> tag as the href -- check the
    # tag text immediately around it rather than the whole page.
    tag_slice = html[max(0, tasks_link_start - 80):tasks_link_start + 80]
    assert "active" in tag_slice


def _link_tag(html: str, href: str) -> str:
    """The full <a ...>...</a> for a given href -- badges/labels can sit
    well past a fixed-size slice once the icon's own inline <svg> markup
    (itself well over 200 characters) is counted, so this slices up to the
    link's actual closing tag instead of guessing a fixed window size."""
    start = html.index(f'href="{href}"')
    end = html.index("</a>", start)
    return html[start:end]


def test_top_nav_shows_the_flagged_badge_once_its_cache_is_warm():
    _common._flagged_ticket_cache["items"] = [{"ticket_id": 1}, {"ticket_id": 2}]
    import datetime
    _common._flagged_ticket_cache["fetched_at"] = datetime.datetime.now()
    _common._flagged_ticket_cache["capped"] = False

    html = review_server._top_nav_html(current_path="/")
    tag = _link_tag(html, "/flagged")
    assert "nav-badge" in tag
    assert ">2<" in tag


def test_top_nav_shows_the_security_badge_once_its_cache_is_warm():
    import datetime
    _common._security_ticket_cache[_common.IT_SECURITY_GROUP_ID] = {
        "items": [{"ticket_id": 1}, {"ticket_id": 2}, {"ticket_id": 3}],
        "fetched_at": datetime.datetime.now(),
    }

    html = review_server._top_nav_html(current_path="/")
    tag = _link_tag(html, "/security")
    assert "nav-badge" in tag
    assert ">3<" in tag


def test_top_nav_badges_are_neutral_not_warn():
    """Blocked/Standup and IT-Security counts aren't inherently a problem
    the way an SLA breach or a disconnected Zendesk OAuth token is -- their
    badge should be the plain neutral style, not nav-badge-warn."""
    import datetime
    now = datetime.datetime.now()
    _common._flagged_ticket_cache["items"] = [{"ticket_id": 1}]
    _common._flagged_ticket_cache["fetched_at"] = now
    _common._flagged_ticket_cache["capped"] = False
    _common._security_ticket_cache[_common.IT_SECURITY_GROUP_ID] = {"items": [{"ticket_id": 1}], "fetched_at": now}

    html = review_server._top_nav_html(current_path="/")
    assert "nav-badge-warn" not in _link_tag(html, "/flagged")
    assert "nav-badge-warn" not in _link_tag(html, "/security")


# --- _category_select_html ----------------------------------------------------

def test_category_select_html_lists_the_fixed_choices():
    html = review_server._category_select_html()
    assert "[Documentation]" in html
    assert "[Configuration]" in html
    assert "No category" in html


def test_category_select_html_marks_the_selected_choice():
    html = review_server._category_select_html("[Configuration]")
    option_start = html.index('value="[Configuration]"')
    tag_slice = html[option_start:option_start + 60]
    assert "selected" in tag_slice
    # The other option must NOT be marked selected.
    other_start = html.index('value="[Documentation]"')
    other_slice = html[other_start:other_start + 60]
    assert "selected" not in other_slice


def test_category_select_html_preserves_an_old_free_typed_category():
    """A draft saved before Category became a fixed dropdown may still carry
    an old free-typed string -- editing it must not silently discard or
    overwrite that value just because it isn't one of the two fixed
    choices (see the function's own docstring)."""
    html = review_server._category_select_html("Old Free Text Category")
    assert "Old Free Text Category" in html
    option_start = html.index('value="Old Free Text Category"')
    tag_slice = html[option_start:option_start + 80]
    assert "selected" in tag_slice
