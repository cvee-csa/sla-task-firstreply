"""The /sla page's "First response time" and "Breaches by rule" charts: the
series reshaping in _common.sla_chart_data and the markup/JS wiring in
review_server._sla_charts_html. Fixtures are made up."""

import json
import re

import _common
import review_server


def _entry(date, **kw):
    base = {"date": date, "total": 10, "breached": 0, "due_soon": 0, "on_track": 10}
    base.update(kw)
    return base


def _history(entries):
    return {"entries": entries, "error": None, "as_of": entries[-1]["date"] if entries else None}


def test_response_series_keep_none_for_missing_days():
    data = _common.sla_chart_data(_history([
        _entry("2026-01-01"),
        _entry("2026-01-02", avg_resp_hrs=0.4, med_resp_hrs=0.0, resp_p90_hrs=1.5, resp_sla_pct=90),
    ]))
    assert data["resp_avg"] == [None, 0.4]
    # a real 0.0 h median is kept as 0.0, distinct from the missing day
    assert data["resp_med"] == [None, 0.0]
    assert data["resp_p90"] == [None, 1.5]
    assert data["resp_sla_pct"] == [None, 90]
    assert data["resp_threshold_hrs"] == _common.SLA_INITIAL_RESPONSE_HRS


def test_rules_gap_before_recording_zero_after():
    data = _common.sla_chart_data(_history([
        _entry("2026-01-01", breached=2),                                    # breaches, rules not recorded yet
        _entry("2026-01-02"),                                                # no breaches at all
        _entry("2026-01-03", breached=2, rules={"no_update": 2}),            # recorded, only one rule
        _entry("2026-01-04", breached=3, rules={"no_update": 2, "initial_response": 1}),
    ]))
    rules = data["rules"]
    assert set(rules) == set(_common.SLA_RULE_KEYS)
    assert rules["no_update"] == [None, 0, 2, 2]
    assert rules["initial_response"] == [None, 0, 0, 1]
    assert rules["requester_wait"] == [None, 0, 0, 0]
    assert rules["resolution_age"] == [None, 0, 0, 0]
    assert data["rules_first_date"] == "2026-01-03"
    assert all(len(v) == 4 for v in rules.values())


def test_rules_first_date_none_when_never_recorded():
    data = _common.sla_chart_data(_history([_entry("2026-01-01", breached=1)]))
    assert data["rules_first_date"] is None
    assert data["rules"]["no_update"] == [None]


def test_chart_markup_has_both_new_charts_and_controls():
    html = review_server._sla_charts_html(_history([
        _entry("2026-01-01", avg_resp_hrs=0.2, med_resp_hrs=0.0, resp_p90_hrs=0.9, resp_sla_pct=100),
    ]))
    for canvas_id in ("sla-resp-chart", "sla-rule-chart"):
        assert f'<canvas id="{canvas_id}">' in html
        assert f"_slaDownloadChart('{canvas_id}'" in html
    assert "window._slaRespChart" in html and "window._slaRuleChart" in html
    assert "First response time" in html and "Breaches by rule" in html
    # chart order: queue health, response time, rules, agent activity
    order = [html.index(x) for x in ("sla-area-chart", "sla-resp-chart", "sla-rule-chart", "sla-agent-chart")]
    assert order == sorted(order)


def test_embedded_json_carries_new_series_and_is_script_safe():
    html = review_server._sla_charts_html(_history([
        _entry("2026-01-01", breached=1, rules={"no_update": 1}, avg_resp_hrs=1.0, med_resp_hrs=0.5, resp_p90_hrs=3.0,
               resp_sla_pct=80, agent_activity={"A </script> Agent": {"public": 1, "internal": 0}}),
    ]))
    m = re.search(r'<script type="application/json" id="sla-chart-data">(.*?)</script>', html, re.S)
    payload = json.loads(m.group(1))
    assert payload["resp_p90"] == [3.0]
    assert payload["rules"]["no_update"] == [1]
    assert payload["rules_first_label"]
    assert "</script> Agent" not in m.group(1)


def test_js_has_no_stray_python_escapes():
    html = review_server._sla_charts_html(_history([_entry("2026-01-01")]))
    # the apostrophe in the rule note is written as a JS ’ escape
    assert "day\\u2019s bars" in html


def test_page_renders_with_empty_history():
    out = review_server._sla_charts_html({"entries": [], "error": "none yet", "as_of": None})
    assert "none yet" in out and "sla-resp-chart" not in out
