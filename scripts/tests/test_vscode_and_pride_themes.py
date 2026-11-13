"""Abyss and Tomorrow Blue (VS Code palettes) and the soft lesbian-pride theme:
they replaced the Auto and B68FBF buttons. Markup/CSS-level checks."""
import re

import review_server

NEW = ("abyss", "tomorrow", "lesbian")


def _page():
    return review_server._page("<p>x</p>").decode()


def _css():
    return _page().split("<style>")[1].split("</style>")[0]


def _block(theme):
    m = re.search(r"html\.theme-%s \{\s*(.*?)\n  \}" % theme, _css(), re.S)
    assert m, theme
    return m.group(1)


def _vars(theme):
    return set(re.findall(r"(--[a-z0-9-]+):", _block(theme)))


def _hex(theme):
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-f]{6})", _block(theme)))


def _lum(h):
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_buttons_replaced_auto_and_b68fbf():
    out = _page()
    for t in NEW:
        assert f'data-theme="{t}"' in out and f"setTheme('{t}')" in out
    assert 'data-theme="system"' not in out and "setTheme('system')" not in out
    assert 'data-theme="b68fbf"' not in out and "setTheme('b68fbf')" not in out and "html.theme-b68fbf {" not in out
    assert "'b68fbf'" not in out


def test_toggle_order_and_count():
    out = _page()
    order = re.findall(r'data-theme="([a-z0-9]+)"', out.split('class="theme-toggle"')[1].split("</div>")[0])
    assert order == ["abyss", "galaxy", "dark", "tomorrow", "lesbian", "halloween", "christmas"]


def test_palettes_define_the_dark_variable_set_plus_on_accent_where_needed():
    dark = _vars("dark")
    assert _vars("abyss") == dark | {"--on-accent"}
    assert _vars("tomorrow") == dark | {"--on-accent"}
    assert _vars("lesbian") == dark


def test_scripts_list_every_mode_and_only_lesbian_is_light():
    out = _page()
    for t in NEW:
        assert f"t === '{t}'" in out and f"mode === '{t}'" in out
        assert f"'theme-{t}'" in out
    assert "t === 'lesbian' ? 'light' : 'dark'" in out and "mode === 'lesbian' ? 'light' : 'dark'" in out
    assert "'#000c18'" in out and "'#002451'" in out


def test_vscode_palettes_use_the_source_backgrounds():
    assert _hex("abyss")["--bg"] == "#000c18" and _hex("abyss")["--card-bg"] == "#060621"
    assert _hex("tomorrow")["--bg"] == "#002451" and _hex("tomorrow")["--accent"] == "#bbdaff"


def test_contrast_in_the_new_themes():
    for t in NEW:
        v = _hex(t)
        assert _ratio(v["--text"], v["--bg"]) >= 7, t
        assert _ratio(v["--text"], v["--card-bg"]) >= 7, t
        assert _ratio(v["--muted"], v["--card-bg"]) >= 4.5, t
        assert _ratio(v["--link"], v["--bg"]) >= 4.5, t
        for k in ("approve", "internal", "remove", "reject", "refresh", "claim"):
            assert _ratio("#ffffff", v[f"--btn-{k}"]) >= 4.5, (t, k)
    for t in ("abyss", "tomorrow"):
        v = _hex(t)
        assert _ratio(v["--accent"], v["--on-accent"]) >= 7, t
    v = _hex("lesbian")                      # no --on-accent: the accent takes white text and is also used as text
    assert _ratio("#ffffff", v["--accent"]) >= 4.5 and _ratio(v["--accent"], v["--bg"]) >= 4.5


def test_dark_accent_pills_get_dark_text_but_lesbian_does_not():
    css = _css()
    for sel in (".sla-range-btn.active", ".theme-toggle button.active", ".top-nav-link.active", ".top-nav-link.active:hover"):
        assert f"html.theme-abyss {sel}" in css and f"html.theme-tomorrow {sel}" in css
        assert f"html.theme-lesbian {sel}" not in css


def test_lesbian_theme_has_the_soft_flag_wash():
    css = _css()
    assert "html.theme-lesbian body { background: transparent; }" in css
    assert "linear-gradient(180deg, #ffe0cc" in css


def test_each_theme_button_has_its_line_icon():
    out = _page().split('class="theme-toggle"')[1].split("</div>")[0]
    expected = {"abyss": "anchor", "galaxy": "orbit", "dark": "moon-star", "tomorrow": "terminal",
                "lesbian": "cloud-moon", "halloween": "ghost", "christmas": "snowflake"}
    buttons = dict(re.findall(r'data-theme="([a-z0-9]+)"[^>]*>(.*?)</button>', out, re.S))
    assert set(buttons) == set(expected)
    for theme, icon in expected.items():
        assert buttons[theme].strip() == review_server._icon(icon, "icon-sm"), theme
        assert icon in review_server._ICONS and review_server._ICONS[icon]
    assert "theme-swatch" not in out


def test_avatar_hue_rules_are_valid_single_brace_css():
    """The B68FBF cleanup rewrote the generator; a doubled brace silently
    dropped every avatar bubble's color."""
    css = _css()
    for i, hue in enumerate(review_server._AVATAR_HUES):
        assert f".avatar-hue-{i} {{ background: {hue}; }}" in css
    assert "{{" not in css.split(".avatar-hue-0")[1][:60]
    assert "theme-b68fbf" not in css and "logo-b68fbf" not in css and "topic-icon-b68fbf" not in css


def test_logo_and_topic_icons_render_once():
    out = _page()
    assert "logo-b68fbf" not in out and "topic-icon-default" not in out
