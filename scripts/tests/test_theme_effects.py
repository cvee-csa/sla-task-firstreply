"""Lesbian-pride clouds/hearts (#side-sky + prideStart), sharing the sidebar sky with the bats.

Markup/CSS/script-level checks; the drawing itself needs canvas and
requestAnimationFrame and is checked manually in a real browser."""
import re
import shutil
import subprocess

import pytest

import review_server


def _page():
    return review_server._page("<p>x</p>").decode()


def _css():
    return _page().split("</style>")[0]


def test_sky_is_shared_by_bats_and_pride_but_each_effect_owns_its_canvas():
    out = _page()
    assert out.count('id="side-sky"') == 1
    # One canvas class per effect, so stopping one theme's effect never removes the other's canvas.
    for cls, stop in (("fx-bats", "batsStop"), ("fx-pride", "prideStop")):
        body = out[out.index(f"function {stop}()"):]
        assert f"canvas.{cls}" in body.split("\n  }\n")[0]
    assert "sky.querySelector('canvas');" not in out


def test_pride_sky_is_wide_layout_only():
    css = _css()
    wide = css[css.index("@media (min-width: 861px) {\n    html.theme-halloween .side-sky"):]
    assert "html.theme-lesbian .side-sky" in wide.split("}")[0]
    assert ".side-sky { display: none; }" in css


def test_theme_switch_starts_each_effect_only_in_its_own_theme():
    out = _page()
    assert "if (mode === 'lesbian') { prideStart(); } else { prideStop(); }" in out
    assert "if (mode === 'christmas') { snowStart(); } else { snowStop(); }" in out


def test_pride_respects_reduced_motion_hidden_tabs_and_tiny_sidebars():
    out = _page()
    body = out[out.index("function prideStart()"):out.index("function prideStop()")]
    assert "snowReduced()" in body and "prideStill()" in body
    assert "document.hidden" in body
    assert "pride.h < 50 || pride.w < 60" in out
    vis = out[out.index("document.addEventListener('visibilitychange', function() {\n    var cls"):]
    assert "theme-lesbian" in vis.split("});\n  function applyTheme")[0]


def test_christmas_has_no_string_lights():
    out = _page()
    assert "xmas-lights" not in out and "lightsStart" not in out


def test_effect_canvases_never_take_clicks():
    css = _css()
    assert ".side-sky canvas { position: absolute;" in css
    assert "pointer-events: none" in css.split(".side-sky canvas {")[1].split("}")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_scripts_with_the_new_effects_are_valid_javascript(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", _page(), re.S)
    assert any("prideStart" in s for s in scripts)
    for i, source in enumerate(scripts):
        path = tmp_path / f"s{i}.js"
        path.write_text(source)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
