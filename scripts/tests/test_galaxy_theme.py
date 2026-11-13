"""Galaxy theme: replaces the old forced-Light option; its photo background is
served from a fixed route. Markup/CSS/route-level checks (a browser pass covers
the visual result)."""
import re
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest
import review_server


def _page():
    return review_server._page("<p>x</p>").decode()


def _css():
    return _page().split("<style>")[1].split("</style>")[0]


def _vars(css, theme):
    m = re.search(r"html\.theme-%s \{\s*(.*?)\n  \}" % theme, css, re.S)
    assert m, theme
    return set(re.findall(r"(--[a-z0-9-]+):", m.group(1)))


def test_galaxy_button_replaces_light_button():
    out = _page()
    assert 'data-theme="galaxy"' in out and "setTheme('galaxy')" in out
    assert 'data-theme="light"' not in out and "setTheme('light')" not in out
    assert 'title="Galaxy"' in out


def test_light_is_gone_from_scripts_and_css():
    out = _page()
    assert "theme-light" not in out
    assert "'light' ||" not in out and "=== 'light'" not in out.replace("? 'dark' : 'light'", "")


def test_galaxy_palette_matches_the_dark_variable_set_plus_on_accent():
    css = _css()
    assert _vars(css, "galaxy") == _vars(css, "dark") | {"--on-accent"}


def test_galaxy_is_a_dark_scheme_with_a_themed_meta_color():
    out = _page()
    assert out.count("'galaxy'") >= 3
    assert "'#f09a7a'" in out


def test_galaxy_background_uses_the_route_and_yields_to_a_custom_image():
    css = _css()
    assert "html.theme-galaxy:not(.has-bg-image)" in css
    assert "url(/assets/galaxy-bg.jpg)" in css
    assert "html.theme-galaxy body { background: transparent; }" in css


def test_galaxy_gets_the_glass_panels_and_dark_active_pill_text():
    css = _css()
    assert ":is(html.has-bg-image, html.theme-galaxy) :is(.security-panel" in css
    assert "html.theme-galaxy .top-nav-link.active" in css and "html.theme-galaxy .theme-toggle button.active" in css


def test_galaxy_accent_text_is_readable():
    def lum(h):
        r, g, b = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def ratio(a, b):
        la, lb = sorted((lum(a), lum(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)
    block = re.search(r"html\.theme-galaxy \{\s*(.*?)\n  \}", _css(), re.S).group(1)
    v = dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-f]{6})", block))
    assert ratio(v["--accent"], v["--on-accent"]) >= 7
    assert ratio(v["--text"], v["--bg"]) >= 12
    for k in ("approve", "internal", "remove", "reject", "refresh", "claim"):
        assert ratio("#ffffff", v[f"--btn-{k}"]) >= 4.5, k


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_background_route_serves_the_jpeg(server):
    with urlopen(f"http://127.0.0.1:{server}/assets/galaxy-bg.jpg") as r:
        body = r.read()
        assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
        assert "max-age" in r.headers["Cache-Control"]
    assert body[:3] == b"\xff\xd8\xff" and len(body) == int(r.headers["Content-Length"])


def test_background_route_is_a_single_fixed_file(server, monkeypatch, tmp_path):
    monkeypatch.setattr(review_server, "GALAXY_BG_PATH", tmp_path / "missing.jpg")
    with pytest.raises(HTTPError) as e:
        urlopen(f"http://127.0.0.1:{server}/assets/galaxy-bg.jpg")
    assert e.value.code == 404
    with pytest.raises(HTTPError) as e:
        urlopen(f"http://127.0.0.1:{server}/assets/../review_server.py")
    assert e.value.code != 200


def test_credit_is_in_the_markup_and_only_shown_for_the_photo():
    out = _page()
    assert "International Gemini Observatory / NOIRLab / NSF / AURA" in out and "CC BY 4.0" in out
    css = _css()
    assert re.search(r"\.galaxy-credit \{\s*display: none;", css)
    assert "html.theme-galaxy:not(.has-bg-image) .galaxy-credit { display: block; }" in css
