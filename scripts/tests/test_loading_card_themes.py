"""The loading detail card is a progress ring (plain arc, no stripes) in every theme, with theme looks for
Halloween (bat), Lesbian pride (heart) and Christmas (snow cap, red arc, snowflake) only. Markup/CSS/script-level
checks; the drawing is checked in a real browser."""
import re
import shutil
import subprocess

import pytest

import review_server

THEMES = ("halloween", "lesbian", "christmas")


def _page():
    return review_server._page("<p>x</p>").decode()


def _css():
    return _page().split("</style>")[0]


def test_card_is_a_ring_with_a_track_circle_an_arc_and_a_percent():
    out = _page()
    assert 'id="page-loading-ring"' in out and 'class="ring-track"' in out
    assert 'id="page-loading-arc"' in out and 'pathLength="100"' in out and 'id="page-loading-pct"' in out
    assert 'id="page-loading-track"' not in out and 'id="page-loading-fill"' not in out


def test_ring_is_plain_not_striped():
    css = _css()
    assert "repeating-linear-gradient(45deg" not in css.split("#page-loading-card {")[1].split("@media (min-width: 861px)")[0]
    assert "page-loading-stripes" not in css


def test_indeterminate_ring_spins_a_short_arc_and_hides_the_percent():
    css = _css()
    assert "#page-loading-card.indeterminate .ring-arc { stroke-dasharray: 25 100 !important; }" in css
    assert "#page-loading-card.indeterminate #page-loading-pct { visibility: hidden; }" in css
    assert "#page-loading-card.indeterminate #page-loading-ring svg { animation: page-loading-ring-spin" in css


def test_script_drives_arc_percent_and_rider_from_the_real_progress():
    out = _page()
    assert "cardArc.style.strokeDasharray = shown + ' 100'" in out
    assert "cardPct.textContent = Math.round(shown) + '%'" in out
    assert "card.style.setProperty('--load-rot', String(shown * 3.6))" in out
    assert "cardArc.style.strokeDasharray = '0 100'" in out        # reset when the card hides


def test_each_of_the_three_themes_has_a_rider_and_a_card_look():
    css = _css()
    for theme in THEMES:
        assert f"html.theme-{theme} #page-loading-card {{" in css
        assert f"html.theme-{theme} #page-loading-rider" in css
        assert f"html.theme-{theme} .ring-arc" in css
    assert "html.theme-halloween .ring-arc { stroke: url(#ring-grad-halloween); }" in css
    assert "html.theme-lesbian .ring-arc { stroke: url(#ring-grad-lesbian); }" in css
    out = _page()
    assert 'id="ring-grad-halloween"' in out and 'id="ring-grad-lesbian"' in out


def test_riders_are_hidden_in_every_other_theme():
    css = _css()
    assert "#page-loading-rider { display: none;" in css
    assert css.count("{ display: block; }") >= 1
    for theme in ("abyss", "tomorrow", "dark", "galaxy"):
        assert f"html.theme-{theme} #page-loading" not in css
        assert f"html.theme-{theme} .ring" not in css


def test_christmas_adds_a_snow_cap():
    assert "html.theme-christmas #page-loading-card::before {" in _css()


def test_reduced_motion_stops_the_spin_and_the_riders():
    css = _css()
    block = css[css.index("@media (prefers-reduced-motion: reduce) {\n    #page-loading-card.indeterminate #page-loading-ring svg"):]
    block = block.split("\n  }\n")[0]
    assert "animation: none" in block
    for theme in THEMES:
        assert f"html.theme-{theme} #page-loading-rider > span::after" in block


def test_braces_are_single_in_the_rendered_card_css():
    css = _css()
    start = css.index("The card is a progress ring")
    seg = css[start:start + 9000]
    assert "{{" not in seg and "}}" not in seg


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_scripts_are_valid_javascript(tmp_path):
    for i, source in enumerate(re.findall(r"<script>(.*?)</script>", _page(), re.S)):
        path = tmp_path / f"s{i}.js"
        path.write_text(source)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
