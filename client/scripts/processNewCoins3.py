"""Process JPY, LINK, XLM with clean edges; used for orbit coins."""
import math
import os
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SRC = Path(
    r"C:\Users\Abeera Zainab\.cursor\projects\c-Users-Abeera-Zainab-OneDrive-Desktop-Cryp\assets"
)
OUT = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public\coins")
OUT.mkdir(parents=True, exist_ok=True)

PAIRS = [
    ("jpy", ["6386e678", "127648970683446532"]),
    ("link", ["chainlink_or_link", "4d852676", "chainlink"]),
    ("xlm", ["stellar_or_xlm", "a0713aef", "stellar"]),
]


def long_path(path: Path) -> str:
    p = str(path.resolve())
    if not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + p
    return p


def list_pngs():
    root = long_path(SRC)
    return [SRC / n for n in os.listdir(root) if n.lower().endswith(".png")]


def find_file(files, substrs):
    for s in substrs:
        for f in files:
            if s in f.name:
                return f
    return None


def is_bg(r, g, b, a, thresh=225):
    if a < 10:
        return True
    mx, mn = max(r, g, b), min(r, g, b)
    if mx >= thresh and (mx - mn) < 28:
        return True
    if r > 210 and g > 210 and b > 210 and (mx - mn) < 22:
        return True
    return False


def flood_remove(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    visited = [[False] * w for _ in range(h)]
    q = deque(
        [
            (0, 0),
            (w - 1, 0),
            (0, h - 1),
            (w - 1, h - 1),
            (w // 2, 0),
            (0, h // 2),
            (w - 1, h // 2),
            (w // 2, h - 1),
            (w // 4, 0),
            (3 * w // 4, 0),
            (w // 4, h - 1),
            (3 * w // 4, h - 1),
        ]
    )
    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or visited[y][x]:
            continue
        visited[y][x] = True
        r, g, b, a = px[x, y]
        if is_bg(r, g, b, a):
            px[x, y] = (r, g, b, 0)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                q.append((nx, ny))
    return img


def clean_fringe(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()

    for _ in range(2):
        clear = []
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a == 0:
                    continue
                neigh = []
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        neigh.append(px[nx, ny][3])
                if not neigh or min(neigh) > 0:
                    continue
                mx, mn = max(r, g, b), min(r, g, b)
                # pale / gray fringe only — keep white coin bodies (LINK/XLM)
                # fringe is usually lower alpha OR very light AND near transparent
                if a < 170 and mx > 170:
                    clear.append((x, y))
                elif mx > 230 and mn > 210 and (mx - mn) < 25 and a < 240:
                    clear.append((x, y))
                elif mx > 200 and (mx - mn) < 20 and a < 200:
                    clear.append((x, y))
        for x, y in clear:
            r, g, b, _ = px[x, y]
            px[x, y] = (r, g, b, 0)

    # Circular soft mask from opaque mass
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if px[x, y][3] > 180:
                xs.append(x)
                ys.append(y)
    if xs:
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        rad = 0.0
        for x, y in zip(xs, ys):
            rad = max(rad, math.hypot(x - cx, y - cy))
        rad *= 0.998
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(0.7))
        out_a = Image.new("L", (w, h), 0)
        oa = out_a.load()
        ma = mask.load()
        for y in range(h):
            for x in range(w):
                oa[x, y] = min(px[x, y][3], ma[x, y])
        img.putalpha(out_a)

    return img


def to_square(cleaned: Image.Image) -> Image.Image:
    bbox = cleaned.getbbox()
    if not bbox:
        return cleaned
    cleaned = cleaned.crop(bbox)
    side = max(cleaned.size) + 6
    pad = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ox = (side - cleaned.size[0]) // 2
    oy = (side - cleaned.size[1]) // 2
    pad.paste(cleaned, (ox, oy), cleaned)
    return pad.resize((1024, 1024), Image.Resampling.LANCZOS)


def main():
    files = list_pngs()
    print(f"Found {len(files)} pngs")
    for name, substrs in PAIRS:
        f = find_file(files, substrs)
        if not f:
            print("MISSING", name)
            continue
        with Image.open(long_path(f)) as img:
            cleaned = flood_remove(img.copy())
            cleaned = clean_fringe(cleaned)
        cleaned = to_square(cleaned)
        out = OUT / f"{name}.png"
        cleaned.save(out, "PNG")
        print("saved", out.name, cleaned.size)
    print("done")


if __name__ == "__main__":
    main()
