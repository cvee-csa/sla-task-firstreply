"""The sidebar's page-background picker is client-side only: the markup, the
apply-before-paint script and the CSS must be on every page, and nothing about
the image may reach the server. (Picking, persisting and removing an image is
checked in a real browser, like the other JS behavior; see the README.)"""
import review_server


def _page_html():
    return review_server._page("<p>x</p>").decode()


def test_picker_markup_is_on_the_page_below_the_refresh_button():
    out = _page_html()
    assert 'class="bg-picker"' in out and 'id="bg-file"' in out and 'accept="image/*"' in out
    assert 'id="bg-veil"' in out and 'id="bg-remove"' in out
    assert out.index('class="brand-nav-row"') < out.index('Refresh data') < out.index('class="bg-picker"')
    assert out.index('class="bg-picker"') < out.index("</main>") if "</main>" in out else True


def test_saved_background_is_applied_in_head_before_the_stylesheet():
    out = _page_html()
    head = out.split("<style>")[0]
    assert "csa-bg-image" in head and "has-bg-image" in head
    assert "data:image/" in head                       # only accepts data URLs, nothing remote


def test_css_veils_the_image_with_the_theme_color_and_keeps_it_fixed():
    css = _page_html().split("<style>")[1].split("</style>")[0]
    rule = css.split("html.has-bg-image {")[1].split("}")[0]
    assert "var(--csa-bg-image)" in rule and "color-mix(in srgb, var(--bg)" in rule and "background-attachment: fixed" in rule
    assert "html.has-bg-image body { background: transparent; }" in css


def test_the_picker_script_stores_only_in_this_browser():
    out = _page_html()
    script = out.split("Background-image picker")[1].split("</script>")[0]
    assert "localStorage.setItem('csa-bg-image'" in script and "fetch(" not in script and "XMLHttpRequest" not in script
    assert "toDataURL('image/jpeg'" in script and "MAX_EDGE" in script         # downscaled before saving


def _css():
    return _page_html().split("<style>")[1].split("</style>")[0]


def test_glass_styling_only_applies_with_an_image_and_backdrop_filter_support():
    css = _css()
    block = css.split("Liquid glass:")[1].split("html.has-bg-image .sla-range-btn.active")[0]
    assert "@supports (backdrop-filter: blur(1px))" in block
    assert block.count("html.has-bg-image") >= 4 and "backdrop-filter: var(--lg-blur)" in block
    # every glass rule hangs off the has-bg-image class, so without an image nothing changes
    selectors = [r.rsplit("}", 1)[-1].strip() for r in block.split("{")[:-1]]
    for sel in selectors[1:]:
        assert sel.startswith("html.has-bg-image"), sel


def test_glass_panels_use_theme_tokens_and_keep_the_columns_accent_edge():
    block = _css().split("Liquid glass:")[1].split("html.has-bg-image .sla-range-btn.active")[0]
    for token in ("var(--card-bg)", "var(--input-bg)", "var(--glass-shine)", "var(--glass-border)", "var(--glass-shadow)"):
        assert token in block
    assert "border-left" not in block and "border: " not in block         # the accent's left edge is not overwritten
    for sel in (".security-panel", ".ticket", ".filter-box"):
        assert sel in block
