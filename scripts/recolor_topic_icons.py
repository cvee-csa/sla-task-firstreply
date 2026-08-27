"""One-off recolor pass: takes each existing '-csa-blue' topic-icon badge
and produces a '-b68fbf' sibling by rotating hue to B68FBF's own hue
(288.75 degrees) while preserving each pixel's original saturation/value --
the same fixed-hue-varying-shade approach the B68FBF theme's CSS custom
properties already use for button colors, just applied per-pixel to a
raster badge instead of to a handful of named CSS variables. White/near-
gray glyph pixels (S ~ 0) are unaffected by a hue change, so the icon
glyph itself stays exactly as before -- only the colored badge background
shifts from CSA blue to the B68FBF purple family."""
import colorsys
from pathlib import Path
from PIL import Image

SRC_DIR = Path(__file__).resolve().parent / "assets" / "topic-icons"
TARGET_HUE = 288.75 / 360.0

# Re-run this after adding a new "-csa-blue" badge to assets/topic-icons/ and
# wiring it into _TOPIC_ICON_FILES in review_server.py -- it regenerates
# every "-b68fbf" sibling listed below (add the new filename to FILES too),
# and is safe to re-run over existing output since it always reads from the
# "-csa-blue" source, never from a previously-generated "-b68fbf" file.

FILES = [
    "stopwatch-csa-blue.png",
    "checklist-csa-blue.png",
    "shield-checkmark-csa-blue.png",
    "conversation-csa-blue.png",
    "key-csa-blue.png",
    "error-csa-blue.png",
    "checkmark-csa-blue.png",
]

for filename in FILES:
    src_path = SRC_DIR / filename
    im = Image.open(src_path).convert("RGBA")
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            hh, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if s < 0.08:
                continue  # near-white/gray glyph pixels stay untouched
            nr, ng, nb = colorsys.hsv_to_rgb(TARGET_HUE, s, v)
            px[x, y] = (round(nr * 255), round(ng * 255), round(nb * 255), a)
    out_name = filename.replace("-csa-blue.png", "-b68fbf.png")
    out_path = SRC_DIR / out_name
    im.save(out_path, optimize=True)
    print(f"{filename} -> {out_name} ({out_path.stat().st_size} bytes)")
