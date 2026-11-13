"""The concise /standup Overview: the five-item cap on "Yours", the
blocked-or-updated topic filter, who-you're-waiting-on, and the age flag. Fixtures are made up (see
test_standup_page.py); this repo is public, so no real notes appear here."""
import _common
import review_server
from test_standup_page import _action, _entry, _page, _topic, _view, _write


def _body(**kw):
    return _page(review_server._render_standup_page, **kw).split("</style>")[-1]


def _yours(body):
    return body.split('id="standup-yours"')[1].split("standup-fold")[0]


# --- yours: capped items, hot topics, waiting ---------------------------------------------

def _crowded_view():
    view = _view()
    extra = {}
    for n in range(1, 8):
        aid = f"2026-01-0{n}-x{n}"
        extra[aid] = _action(aid, f"2026-01-0{n}", f"Extra {n}", "Widgets|Widget rollout", owner="Alex")
    view["actions"].update(extra)
    view["my_open_actions"] = sorted(extra) + ["2026-01-13-a1"]
    return view


def test_yours_shows_the_five_oldest_open_items_and_links_to_the_rest():
    _write(_crowded_view())
    yours = _yours(_body())
    assert "Open action items, oldest first (8)" in yours
    for n in range(1, 6):
        assert f"Extra {n}" in yours
    assert "Extra 6" not in yours and "Extra 7" not in yours and "Email the vendor" not in yours
    assert "Showing the 5 oldest of 8." in yours
    assert 'href="/standup/actions?owner=Alex">Show all 8</a>' in yours


def test_yours_shows_everything_without_a_link_when_there_are_five_or_fewer():
    _write()
    yours = _yours(_body())
    assert "Email the vendor" in yours and "Showing the" not in yours and "Show all" not in yours


def _topics_view():
    view = _view()
    calm = _topic("Widgets|Calm", "Calm topic", "active", [_entry("c1", "2026-01-10", "Steady.")])
    fresh = _topic("Widgets|Fresh", "Fresh topic", "active", [_entry("f1", "2026-01-15", "New this week.")])
    view["topics"].update({"Widgets|Calm": calm, "Widgets|Fresh": fresh})
    view["my_topics"] = ["Widgets|Widget rollout", "Widgets|Calm", "Widgets|Fresh"]
    return view


def test_yours_lists_only_blocked_or_recently_updated_topics():
    _write(_topics_view())
    yours = _yours(_body())
    assert "Blocked or recently updated topics (2)" in yours
    assert "Widget rollout" in yours and "Fresh topic" in yours and "Calm topic" not in yours
    assert '1 more active topic on <a href="/standup/topics?owner=Alex">Topics</a>.' in yours


def test_yours_says_so_when_no_topic_is_blocked_or_recent():
    view = _topics_view()
    view["my_topics"] = ["Widgets|Calm"]
    _write(view)
    assert "No blocked or recently updated topics." in _yours(_body())


def _waiting_view():
    view = _view()
    theirs = _topic("Widgets|Theirs", "Their topic", "active", [_entry("t1", "2026-01-15", "Pending.", owner="Sam")], owners=("Sam",))
    view["topics"]["Widgets|Theirs"] = theirs
    view["waiting_on"] = [{"on": "Vendor", "topics": ["Widgets|Widget rollout"]}, {"on": "Jesstin", "topics": ["Widgets|Theirs"]}]
    return view


def test_waiting_is_counts_per_party_and_yours_covers_only_your_topics():
    _write(_waiting_view())
    body = _body()
    yours = _yours(body)
    assert "Waiting on others (1)" in yours and "<summary>Vendor (1)</summary>" in yours
    assert "Jesstin" not in yours and "Their topic" not in yours
    team = body.split('csa-standup-fold-waiting')[1].split("standup-fold")[0]
    assert "Waiting on others, whole team (2)" in body
    assert "<summary>Vendor (1)</summary>" in team and "<summary>Jesstin (1)</summary>" in team and "Their topic" in team


# --- age flag ------------------------------------------------------------------------------

def test_old_open_items_carry_the_age_flag_and_recent_ones_do_not():
    from datetime import datetime, timedelta
    _write()
    ctx = review_server._StandupCtx(_common.standup_view()["view"], "/standup")
    day = lambda n: (datetime.now().date() - timedelta(days=n)).isoformat()  # noqa: E731
    row = lambda n: ctx.action_row(_action(f"x{n}", day(n), "T", "Widgets|Widget rollout"))  # noqa: E731
    assert 'class="standup-age-old"' in row(31) and 'class="standup-age-old"' in row(90)
    assert 'class="standup-age-old"' not in row(30) and 'class="standup-age-old"' not in row(2)
