"""Draw the app icon (teal tile, white shield, check) at 1024 px; write .png and .ico."""
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def draw(size=1024):
    s = size / 32.0
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(7 * s), fill=(14, 140, 140, 255))
    shield = [(16, 5.5), (24.5, 8.6), (24.5, 15), (23.6, 19.2), (21.2, 22.8), (16, 26.5),
              (10.8, 22.8), (8.4, 19.2), (7.5, 15), (7.5, 8.6)]
    d.polygon([(x * s, y * s) for x, y in shield], fill=(255, 255, 255, 255))
    d.line([(12.2 * s, 16 * s), (15 * s, 18.8 * s), (20.2 * s, 13.2 * s)],
           fill=(14, 140, 140, 255), width=int(2.4 * s), joint="curve")
    return im


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    im = draw()
    im.save(os.path.join(OUT, "icon.png"))
    im.save(os.path.join(OUT, "icon.ico"),
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icons written to", OUT)
