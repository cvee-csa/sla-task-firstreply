"""Recolorable topic badges: a theme-colored square with the CSA glyph masked over it.

The glyph PNGs live in assets/topic-glyphs/ (made by make_topic_glyphs.py from the CSA badges). The
page draws each badge as <span class="... tb tbg-<name>"> whose colors come from two custom properties
every theme sets, so these tests check the files, the markup, that the data is defined once per page,
and that every theme's pair of colors is readable."""
import re
import struct

import pytest

import review_server

KEYS_WITH_GLYPHS = [k for k, f in review_server._TOPIC_ICON_FILES.items() if f.endswith("-csa-blue.png")]


def _css() -> str:
    page = review_server._page("<p>x</p>").decode()
    return page.split("</style>")[0]


def _hex_to_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(rgb):
    def chan(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_every_badge_has_a_small_square_glyph_png_with_alpha():
    names = {review_server._glyph_name(k) for k in KEYS_WITH_GLYPHS}
    assert len(names) >= 15
    for name in names:
        data = (review_server.TOPIC_GLYPHS_DIR / f"{name}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 8_000, name
        width, height = struct.unpack(">II", data[16:24])
        assert width == height, name
        assert data[25] in (4, 6), f"{name} must carry an alpha channel (grey+alpha or RGBA)"
        assert name in review_server.TOPIC_GLYPH_DATA_URIS


def test_glyphs_are_mostly_transparent_white_shapes():
    pil = pytest.importorskip("PIL.Image")
    for name in set(review_server._glyph_name(k) for k in KEYS_WITH_GLYPHS):
        im = pil.open(review_server.TOPIC_GLYPHS_DIR / f"{name}.png").convert("RGBA")
        alpha = list(im.getchannel("A").tobytes())
        covered = sum(1 for a in alpha if a > 127) / len(alpha)
        assert 0.02 < covered < 0.5, (name, covered)
        assert im.getpixel((0, 0))[3] == 0                              # corners are empty, not blue


def test_conversion_keeps_white_and_drops_the_blue():
    pil = pytest.importorskip("PIL.Image")
    import tempfile
    from pathlib import Path
    import make_topic_glyphs
    src = pil.new("RGBA", (20, 20), (0, 84, 159, 255))
    for x in range(5, 15):
        for y in range(5, 15):
            src.putpixel((x, y), (255, 255, 255, 255))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x-csa-blue.png"
        src.save(path)
        out = make_topic_glyphs.convert(path)
    alpha = out.getchannel("A") if "A" in out.getbands() else out
    center = alpha.getpixel((out.width // 2, out.height // 2))
    assert center > 245 and alpha.getpixel((1, 1)) == 0


def test_badge_markup_is_a_decorative_span_not_an_embedded_image():
    html_out = review_server._topic_icon("projects", "topic-icon-nav")
    assert html_out == '<span class="topic-icon-nav tb tbg-folder" aria-hidden="true"></span>'
    assert "data:image" not in html_out


def test_a_labelled_icon_keeps_a_real_image_with_alt_text():
    out = review_server._topic_icon("projects", "topic-icon", alt="Projects")
    assert out.startswith("<img ") and 'alt="Projects"' in out


def test_a_key_without_a_glyph_twin_keeps_its_image():
    assert review_server._topic_icon("priority-alert", "topic-icon-inline").startswith("<img ")      # error-white.png


def test_a_missing_glyph_file_falls_back_to_the_badge_image(monkeypatch):
    monkeypatch.setattr(review_server, "TOPIC_GLYPH_DATA_URIS", {})
    assert review_server._topic_icon("projects", "topic-icon").startswith("<img ")


def test_unknown_key_renders_nothing():
    assert review_server._topic_icon("no-such-key") == ""


def test_glyph_data_is_defined_once_per_page_not_once_per_use():
    css = _css()
    names = set(review_server._glyph_name(k) for k in KEYS_WITH_GLYPHS)
    for name in names:
        assert css.count(f".tbg-{name} {{") == 1, name
    assert css.count('--glyph: url("data:image/png;base64,') == len(review_server.TOPIC_GLYPH_DATA_URIS)
    nav = review_server._top_nav_html("/projects")
    assert "data:image" not in nav                                       # seven badges, zero embedded images


def test_badge_css_masks_the_glyph_and_inverts_on_the_active_link():
    css = _css()
    assert "mask: var(--glyph)" in css and "-webkit-mask: var(--glyph)" in css
    assert re.search(r"\.top-nav-link\.active \.tb \{ --topic-badge-bg: var\(--topic-badge-ink\); --topic-badge-fg: var\(--accent\); \}", css)


THEMES = ("dark", "abyss", "tomorrow", "lesbian", "halloween", "christmas", "galaxy")


def _theme_vars(css: str, theme: str) -> dict:
    out = {}
    for body in re.findall(r"html\.theme-%s \{([^}]*)\}" % theme, css):
        out.update(dict(re.findall(r"(--[a-z-]+):\s*([^;]+);", body)))
    return out


def _resolve(value: str, vars_: dict) -> str:
    match = re.fullmatch(r"var\((--[a-z-]+)\)", value.strip())
    return vars_[match.group(1)].strip() if match else value.strip()


@pytest.mark.parametrize("theme", THEMES)
def test_every_theme_sets_a_readable_badge_color_pair(theme):
    vars_ = _theme_vars(_css(), theme)
    assert "--topic-badge-ink" in vars_, theme
    bg = _hex_to_rgb(_resolve("var(--accent)", vars_))
    fg = _hex_to_rgb(_resolve(vars_["--topic-badge-ink"], vars_))
    assert _contrast(bg, fg) >= 4.5, (theme, _contrast(bg, fg))


def test_default_light_and_system_dark_badge_pairs_are_readable():
    css = _css()
    root = re.search(r":root \{\s*color-scheme[^}]*\}", css, flags=re.S).group(0)
    accent = re.search(r"--accent:\s*(#[0-9a-f]{6})", root).group(1)
    fg = re.search(r"--topic-badge-ink:\s*(#[0-9a-f]{6})", root).group(1)
    assert _contrast(_hex_to_rgb(accent), _hex_to_rgb(fg)) >= 4.5
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root \{([^}]*)\}", css).group(1)
    accent = re.search(r"--accent:\s*(#[0-9a-f]{6})", dark).group(1)
    fg = re.search(r"--topic-badge-ink:\s*(#[0-9a-f]{6})", dark).group(1)
    assert _contrast(_hex_to_rgb(accent), _hex_to_rgb(fg)) >= 4.5


def test_the_active_link_swaps_the_pair_so_it_stays_readable():
    """The pill is the accent, so the badge takes the ink as its square and the accent as its glyph:
    the same two colors as the normal badge, swapped, hence the same contrast."""
    css = _css()
    assert ".top-nav-link.active .tb { --topic-badge-bg: var(--topic-badge-ink); --topic-badge-fg: var(--accent); }" in css
    assert "--topic-badge-fg: var(--topic-badge-ink);" in css                 # the normal badge: ink glyph on accent square
