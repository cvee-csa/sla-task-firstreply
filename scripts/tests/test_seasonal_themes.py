"""Halloween / Christmas theme modes: toggle buttons, palette blocks, and the
JS that persists and applies them. Markup/CSS-level checks only (a real
browser pass covers the visual result)."""
import re

import review_server

SEASONAL = ("halloween", "christmas")
# Every variable the dark palette defines must also be defined by each new
# palette, or the mode would silently inherit the wrong :root values.
_BLOCK = r"html\.theme-%s \{\s*(.*?)\n  \}"


def _page():
    return review_server._page("<p>x</p>").decode()


def _vars(css, theme):
    m = re.search(_BLOCK % theme, css, re.S)
    assert m, f"missing html.theme-{theme} block"
    return set(re.findall(r"(--[a-z0-9-]+):", m.group(1)))


def test_toggle_has_a_button_per_seasonal_theme():
    out = _page()
    for t in SEASONAL:
        assert f'data-theme="{t}"' in out and f"setTheme('{t}')" in out


def test_palettes_define_the_full_variable_set():
    out = _page()
    reference = _vars(out, "dark")
    for t in SEASONAL:
        assert _vars(out, t) == reference | {"--on-accent"}, t


def test_scripts_know_the_new_modes():
    out = _page()
    assert out.count("'halloween'") >= 4 and out.count("'christmas'") >= 4
    assert "theme-halloween" in out and "theme-christmas" in out   # class removal list + CSS


def test_seasonal_themes_force_dark_color_scheme():
    out = _page()
    # every named theme is a dark scheme except the soft lesbian-pride one
    assert "colorScheme = t === 'lesbian' ? 'light' : 'dark'" in out
    assert "colorScheme = mode === 'lesbian' ? 'light' : 'dark'" in out


def test_no_seasonal_glyph_beside_the_header_title():
    out = _page()
    assert "<h1>Workflow Asset</h1>" in out
    assert "theme-deco" not in out and "pumpkin" not in review_server._ICONS and "tree" not in review_server._ICONS


def test_christmas_snowfall_is_wired_and_guarded():
    out = _page()
    assert "snowStart()" in out and "snowStop()" in out
    assert "if (mode === 'christmas') { snowStart(); } else { snowStop(); }" in out
    assert "prefers-reduced-motion: reduce" in out and "document.hidden" in out and "visibilitychange" in out
    assert "requestAnimationFrame(snowFrame)" in out and "cancelAnimationFrame" in out


def test_snow_canvas_never_blocks_clicks_and_stays_under_the_toast():
    css = _page().split("<style>")[1].split("</style>")[0]
    rule = re.search(r"#snow-canvas \{ (.*?) \}", css).group(1)
    assert "position: fixed" in rule and "pointer-events: none" in rule
    z = int(re.search(r"z-index: (\d+)", rule).group(1))
    toast = int(re.search(r"\.bg-toast \{\s*[^}]*?z-index: (\d+)", css).group(1))
    assert z < toast
