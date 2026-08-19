"""Process INR + USDT coin PNGs into public/coins."""
import os
from collections import deque
from pathlib import Path

from PIL import Image

SRC = Path(
    r"C:\Users\Abeera Zainab\.cursor\projects\c-Users-Abeera-Zainab-OneDrive-Desktop-Cryp\assets"
)
OUT = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public\coins")
OUT.mkdir(parents=True, exist_ok=True)

PAIRS = [
    ("inr", "606015693653139716"),
    ("usdt", "Tether__USDT_Glass"),
]


def long_path(path: Path) -> str:
    p = str(path.resolve())
    if not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + p
    return p


def list_pngs():
    root = long_path(SRC)
    names = os.listdir(root)
    return [SRC / n for n in names if n.lower().endswith(".png")]


def find_file(files, substr):
    for f in files:
        if substr in f.name:
            return f
    return None


def is_bg_pixel(r, g, b, a):
    if a < 8:
        return True
    return r > 232 and g > 232 and b > 232 and (max(r, g, b) - min(r, g, b)) < 30


def remove_bg_edge_flood(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()

    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    already_alpha = sum(1 for c in corners if c[3] < 20) >= 2

    visited = [[False] * w for _ in range(h)]
    q = deque()
    for s in [
        (0, 0),
        (w - 1, 0),
        (0, h - 1),
        (w - 1, h - 1),
        (w // 2, 0),
        (0, h // 2),
        (w - 1, h // 2),
        (w // 2, h - 1),
    ]:
        q.append(s)

    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or visited[y][x]:
            continue
        visited[y][x] = True
        r, g, b, a = px[x, y]

        if already_alpha:
            if a > 0 and r > 245 and g > 245 and b > 245 and (max(r, g, b) - min(r, g, b)) < 12:
                px[x, y] = (r, g, b, 0)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    q.append((nx, ny))
            elif a < 20:
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    q.append((nx, ny))
            continue

        if is_bg_pixel(r, g, b, a):
            px[x, y] = (r, g, b, 0)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                q.append((nx, ny))

    return img


def to_square(cleaned: Image.Image) -> Image.Image:
    bbox = cleaned.getbbox()
    if not bbox:
        return cleaned
    cleaned = cleaned.crop(bbox)
    side = max(cleaned.size)
    pad = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ox = (side - cleaned.size[0]) // 2
    oy = (side - cleaned.size[1]) // 2
    pad.paste(cleaned, (ox, oy), cleaned)
    return pad.resize((1024, 1024), Image.Resampling.LANCZOS)


def main():
    files = list_pngs()
    print(f"Found {len(files)} pngs")
    for name, substr in PAIRS:
        f = find_file(files, substr)
        if not f:
            print("MISSING", name, substr)
            continue
        with Image.open(long_path(f)) as img:
            cleaned = remove_bg_edge_flood(img.copy())
        cleaned = to_square(cleaned)
        out = OUT / f"{name}.png"
        cleaned.save(out, "PNG")
        print("saved", out.name, cleaned.size)
    print("done")


if __name__ == "__main__":
    main()
