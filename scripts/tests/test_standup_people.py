"""One person's action items on /standup/actions?owner=. (The separate By person
page was merged into Action items; /standup/people now redirects here.) Every
fixture is made up (see test_standup_page.py); this repo is public, so no real
notes appear here."""
import io
from urllib.parse import parse_qs, urlparse

import _common
import review_server
from test_standup_page import _action, _page, _view, _write

render = review_server._render_standup_actions_page


def _setup(extra_actions=None):
    view = _view()
    view["actions"].update(extra_actions or {})
    for a in view["actions"].values():                 # the real data carries each item's topic name
        a["topic"] = view["topics"][a["topic_key"]]["name"]
    _write(view)


def test_owner_filter_shows_that_persons_items_with_a_summary():
    _setup()
    out = _page(render, owner="Sam")
    assert "Chase the vendor" in out and "Email the vendor" not in out
    assert "Sam: 1 open, 0 done, 0 dropped across 1 standup." in out
    assert 'value="Sam" selected' in out


def test_summary_counts_resolved_items_and_inferred_owners_even_in_the_open_view():
    _setup()
    out = _page(render, owner="Alex")
    assert "Alex: 1 open, 1 done, 0 dropped across 2 standups; 1 with an inferred owner." in out
    assert 'class="standup-inferred"' in out          # the one inferred-owner item is marked
    assert "Name was a guess" in out                  # its check note rides along
    assert "Renew the certificate" not in out         # resolved items stay out of the open view
    assert "Renew the certificate" in _page(render, owner="Alex", show="all")


def test_summary_counts_dropped_and_other_standups():
    _setup(extra_actions={
        "2026-01-05-a1": _action("2026-01-05-a1", "2026-01-05", "Older open task", "Widgets|Widget rollout", owner="Alex"),
        "2026-01-06-a1": _action("2026-01-06-a1", "2026-01-06", "Older done task", "Widgets|Widget rollout", owner="Alex",
                                 resolution={"date": "2026-01-07", "outcome": "dropped", "note": None, "by_you": False}),
    })
    out = _page(render, owner="Alex")
    assert "1 done, 1 dropped across 4 standups" in out


def test_search_filters_by_task_and_topic():
    _setup()
    assert "Renew the certificate" in _page(render, owner="Alex", show="all", q="certificates")   # topic name
    out = _page(render, owner="Alex", show="all", q="renew")
    assert "Renew the certificate" in out and "Email the vendor" not in out
    assert "No action items" in _page(render, owner="Alex", q="zzz")


def test_no_owner_means_no_summary_line():
    _setup()
    assert " dropped across " not in _page(render)


def test_unknown_or_hostile_owner_param_is_ignored():
    _setup()
    out = _page(render, owner="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in out and "Chase the vendor" in out and "Email the vendor" in out


def test_subnav_has_the_five_tabs_and_marks_the_current_one():
    _setup()
    out = _page(render)
    for label in ("Overview", "Needs attention", "Action items", "Topics", "Meetings"):
        assert f">{label}</a>" in out
    for gone in ("By person", "Workstreams", ">Closed</a>", "Workday recap"):
        assert gone not in out.split("</nav>")[0].split("standup-subnav")[-1]
    assert 'class="sla-range-btn active" href="/standup/actions" aria-current="page"' in out


def test_missing_data_shows_the_error_page():
    out = render(_common.standup_snapshot()).decode()
    assert "No standup data found" in out


# --- the pages that were merged still answer, as redirects ----------------------

class _Headers:
    def get(self, key, default=None):
        return default


def _get(url):
    handler = review_server.Handler.__new__(review_server.Handler)
    handler.path = url
    handler.headers = _Headers()
    handler.rfile = io.BytesIO(b"")
    seen = []
    handler._redirect = lambda location, *a, **k: seen.append(location)
    handler.do_GET()
    assert len(seen) == 1, seen
    parsed = urlparse(seen[0])
    return parsed.path, parse_qs(parsed.query)


def test_old_by_person_url_redirects_to_action_items_with_the_owner():
    path, q = _get("/standup/people?person=Sam&q=vendor&age=old")
    assert path == "/standup/actions" and q == {"owner": ["Sam"], "q": ["vendor"], "age": ["old"]}
    assert _get("/standup/people")[0] == "/standup/actions"


def test_old_workstreams_and_closed_urls_redirect_to_topics():
    path, q = _get("/standup/workstreams?q=x&owner=Sam")
    assert path == "/standup/topics" and q == {"q": ["x"], "owner": ["Sam"]}
    path, q = _get("/standup/closed")
    assert path == "/standup/topics" and q == {"status": ["closed"]}
