"""Halloween / Christmas theme modes: toggle buttons, palette blocks, and the
JS that persists and applies them. Markup/CSS-level checks only (a real
browser pass covers the visual result)."""
import re

import review_server

SEASONAL = ("halloween", "christmas")
# Every variable the b68fbf palette defines must also be defined by each new
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
    reference = _vars(out, "b68fbf")
    for t in SEASONAL:
        assert _vars(out, t) == reference | {"--on-accent"}, t


def test_scripts_know_the_new_modes():
    out = _page()
    assert out.count("'halloween'") >= 4 and out.count("'christmas'") >= 4
    assert "theme-halloween" in out and "theme-christmas" in out   # class removal list + CSS


def test_seasonal_themes_force_dark_color_scheme():
    out = _page()
    assert "colorScheme = (t === 'dark' || t === 'halloween' || t === 'christmas') ? 'dark'" in out
    assert "colorScheme = (mode === 'dark' || mode === 'halloween' || mode === 'christmas') ? 'dark'" in out


def test_header_glyphs_present_and_hidden_by_default():
    out = _page()
    assert "theme-deco-halloween" in out and "theme-deco-christmas" in out
    assert re.search(r"\.theme-deco \{\s*display: none;", out)
    assert "html.theme-halloween .theme-deco-halloween" in out
