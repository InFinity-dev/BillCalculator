#!/usr/bin/env python3
"""Electron Builder용 PNG/ICO/ICNS 아이콘을 동일 원본으로 생성한다."""

from pathlib import Path
import shutil

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"


def make_master(size=1024):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    blue = "#2856A3"
    white = "#FFFFFF"
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=220, fill=blue)

    width = 52
    building = (285, 175, 739, 874)
    draw.rounded_rectangle(building, radius=55, outline=white, width=width)
    for y in (345, 500, 655):
        for x in (405, 595):
            draw.line((x - 28, y, x + 28, y), fill=white, width=width, joint="curve")
    draw.rectangle((445, 720, 579, 874), fill=blue, outline=white, width=width)
    return image


def save_png_and_ico(master):
    BUILD.mkdir(parents=True, exist_ok=True)
    master.save(BUILD / "icon.png")
    master.save(
        BUILD / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def save_icns(master):
    iconset = BUILD / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    master.save(
        BUILD / "icon.icns",
        format="ICNS",
        sizes=[(16, 16), (32, 32), (128, 128), (256, 256), (512, 512), (1024, 1024)],
    )


def main():
    master = make_master()
    save_png_and_ico(master)
    save_icns(master)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
