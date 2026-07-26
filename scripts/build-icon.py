#!/usr/bin/env python3
"""Generate Vivieen's abstract, portrait-free macOS app icon."""

from pathlib import Path
import subprocess

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICONSET = ASSETS / "icon.iconset"
SIZE = 1024


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((20, 20, size - 20, size - 20), radius, fill=255)
    return mask


def build() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (SIZE, SIZE))
    pixels = gradient.load()
    for y in range(SIZE):
        for x in range(SIZE):
            blend = (x * 0.62 + (SIZE - y) * 0.38) / SIZE
            glow = max(0.0, 1.0 - (((x - 620) / 650) ** 2 + ((y - 340) / 650) ** 2))
            pixels[x, y] = (
                int(10 + 22 * glow + 8 * blend),
                int(12 + 6 * glow),
                int(18 + 28 * blend),
                255,
            )
    image.alpha_composite(Image.composite(gradient, Image.new("RGBA", gradient.size), rounded_mask(SIZE, 220)))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((22, 22, 1002, 1002), 218, outline=(255, 255, 255, 42), width=10)
    draw.rounded_rectangle((210, 224, 814, 782), 180, fill=(245, 242, 236, 244))
    draw.polygon(((390, 762), (330, 874), (500, 786)), fill=(245, 242, 236, 244))

    bars = [150, 244, 332, 220, 390, 282, 172]
    width = 48
    gap = 26
    total = len(bars) * width + (len(bars) - 1) * gap
    left = (SIZE - total) // 2
    for index, height in enumerate(bars):
        x0 = left + index * (width + gap)
        y0 = 506 - height // 2
        y1 = 506 + height // 2
        mix = index / (len(bars) - 1)
        color = (int(239 - 16 * mix), int(85 - 50 * mix), int(73 + 75 * mix), 255)
        draw.rounded_rectangle((x0, y0, x0 + width, y1), width // 2, fill=color)
    return image


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    ICONSET.mkdir(exist_ok=True)
    image = build()
    image.save(ASSETS / "icon.png", optimize=True)
    sizes = (16, 32, 128, 256, 512)
    for size in sizes:
        image.resize((size, size), Image.Resampling.LANCZOS).save(
            ICONSET / f"icon_{size}x{size}.png")
        image.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(
            ICONSET / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ASSETS / "icon.icns")],
                   check=True)
    print("Generated portrait-free Vivieen icon.")


if __name__ == "__main__":
    main()
