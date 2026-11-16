#!/usr/bin/env python3
"""Turns the CSA badge PNGs in assets/topic-icons/ into recolorable glyphs in assets/topic-glyphs/.

Each "<name>-csa-blue.png" is a flat CSA-blue rounded square (0, 84, 159) with a white glyph. The
glyph's coverage is the red channel (0 on the blue, 255 on the white, anything between on anti-aliased
edges), so the output is a white-on-transparent PNG whose alpha is that coverage. The page uses it as a
CSS mask over a square drawn in the active theme's color (see _TOPIC_BADGE_CSS in review_server.py).

Run from the repo root after adding or replacing a badge (needs Pillow, a development-only tool):

    python3 scripts/make_topic_glyphs.py
"""
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "assets" / "topic-icons"
TARGET = HERE / "assets" / "topic-glyphs"
SUFFIX = "-csa-blue.png"
SIZE = 96


def convert(path: Path) -> Image.Image:
    rgba = Image.open(path).convert("RGBA")
    coverage = Image.new("L", rgba.size, 0)
    src, dst = rgba.load(), coverage.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, _g, _b, a = src[x, y]
            dst[x, y] = r if a else 0
    coverage = coverage.resize((SIZE, SIZE), Image.LANCZOS)
    glyph = Image.new("LA", (SIZE, SIZE), (255, 0))
    glyph.putalpha(coverage)
    return glyph


def main() -> None:
    TARGET.mkdir(exist_ok=True)
    for path in sorted(SOURCE.glob(f"*{SUFFIX}")):
        name = path.name[: -len(SUFFIX)]
        out = TARGET / f"{name}.png"
        convert(path).save(out, optimize=True)
        print(f"{out.name}: {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
