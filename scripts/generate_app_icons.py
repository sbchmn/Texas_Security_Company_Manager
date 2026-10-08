"""Generate opaque mobile icons from the geometry/colors of static/icon.svg."""
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent


def icon(size, maskable=False):
    scale = 4
    image = Image.new("RGB", (size * scale, size * scale), "#16324f")
    drawing = ImageDraw.Draw(image)
    factor = size * scale / 128
    inset = 0.8 if maskable else 1

    def point(x, y):
        return ((64 + (x - 64) * inset) * factor, (64 + (y - 64) * inset) * factor)

    shield = [(64, 18), (103, 34), (103, 62)]
    for index in range(1, 33):
        t = index / 32
        shield.append(((1-t)**3*103 + 3*(1-t)**2*t*103 + 3*(1-t)*t*t*87 + t**3*64,
                       (1-t)**3*62 + 3*(1-t)**2*t*87 + 3*(1-t)*t*t*102 + t**3*111))
    for index in range(1, 33):
        t = index / 32
        shield.append(((1-t)**3*64 + 3*(1-t)**2*t*41 + 3*(1-t)*t*t*25 + t**3*25,
                       (1-t)**3*111 + 3*(1-t)**2*t*102 + 3*(1-t)*t*t*87 + t**3*62))
    shield.append((25, 34))
    drawing.polygon([point(*xy) for xy in shield], fill="#14b8a6")
    drawing.polygon([point(*xy) for xy in [(43, 47), (85, 47), (85, 59), (70, 59),
                    (70, 93), (58, 93), (58, 59), (43, 59)]], fill="white")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    output = ROOT / "static" / "icons"
    output.mkdir(exist_ok=True)
    for size in (180, 192, 512, 1024):
        icon(size).save(output / f"icon-{size}.png")
    icon(512, maskable=True).save(output / "icon-maskable-512.png")
    android = ROOT / "mobile" / "android" / "app" / "src" / "main" / "res"
    for density, size in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        directory = android / f"mipmap-{density}"
        directory.mkdir(parents=True, exist_ok=True)
        icon(size).save(directory / "ic_launcher.png")
        icon(int(size * 108 / 48), maskable=True).save(directory / "ic_launcher_foreground.png")
    assets = ROOT / "mobile" / "ios" / "TSCM" / "Assets.xcassets"
    app_icon = assets / "AppIcon.appiconset"
    app_icon.mkdir(parents=True, exist_ok=True)
    icon(1024).save(app_icon / "AppIcon.png")
    (assets / "Contents.json").write_text(json.dumps({"info": {"author": "xcode", "version": 1}}, indent=2) + "\n")
    (app_icon / "Contents.json").write_text(json.dumps({
        "images": [{"filename": "AppIcon.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}],
        "info": {"author": "xcode", "version": 1},
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
