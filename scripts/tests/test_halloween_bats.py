"""Halloween bats in the empty part of the sidebar (#side-sky + batsStart).

Markup/CSS/script-level checks; the drawing itself is checked manually in a
real browser (it needs canvas and requestAnimationFrame)."""
import re
import shutil
import subprocess

import pytest

import review_server


def _page():
    return review_server._page("<p>x</p>").decode()


def test_bat_sky_sits_between_the_nav_and_the_footer_once():
    out = _page()
    assert out.count('id="side-sky"') == 1
    assert out.index("brand-nav-row") < out.index('id="side-sky"') < out.index('class="brand-footer"')
    assert 'id="side-sky" aria-hidden="true"' in out


def test_bats_are_hidden_by_default_and_shown_only_for_halloween_on_wide_screens():
    css = _page().split("</style>")[0]
    assert ".side-sky { display: none; }" in css
    wide = css[css.index("@media (min-width: 861px) {\n    html.theme-halloween .side-sky"):]
    assert "html.theme-halloween .side-sky, html.theme-lesbian .side-sky {" in wide.split("}")[0] + "}"
    assert "flex: 1 1 0" in wide.split("}")[0] and "min-height: 0" in wide.split("}")[0]
    assert "{{" not in css.split(".side-sky")[1][:400]


def test_theme_switch_starts_bats_only_for_halloween():
    out = _page()
    assert "if (mode === 'halloween') { batsStart(); } else { batsStop(); }" in out


def test_bats_respect_reduced_motion_and_hidden_tabs():
    out = _page()
    body = out[out.index("function batsStart()"):out.index("function batsStop()")]
    assert "snowReduced()" in body and "batsStill()" in body
    vis = out[out.index("document.addEventListener('visibilitychange', function() {\n    var cls"):]
    assert "theme-halloween" in vis.split("});")[0]


def test_bats_hide_when_the_sidebar_is_too_small():
    out = _page()
    assert "bats.h < 50 || bats.w < 60 ? 0" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_scripts_are_valid_javascript(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", _page(), re.S)
    assert any("batsStart" in s for s in scripts)
    for i, source in enumerate(scripts):
        path = tmp_path / f"s{i}.js"
        path.write_text(source)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
