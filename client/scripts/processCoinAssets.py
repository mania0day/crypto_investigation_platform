"""Strip only connected edge backgrounds; keep white logos intact."""
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
    ("xrp", "Solana_sol_3d_coin_icon"),
    ("usdt", "3d_Gold_Dollar_Coin"),
    ("ilv", "Illuvium__ILV"),
    ("btc", "Bitcoin__BTC_Glass"),
    ("ton", "Premium_Photo-ea39f6ef"),
    ("sol", "Solana__SOL_Glass"),
    ("avax", "Avalanche__AVAX"),
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


def is_bg_pixel(r, g, b, a, mode="light"):
    if a < 8:
        return True
    if mode == "light":
        # light / white / light-gray studio backgrounds
        return r > 232 and g > 232 and b > 232 and (max(r, g, b) - min(r, g, b)) < 30
    # checker / near-transparent leftovers
    return a < 40 and r > 200 and g > 200 and b > 200


def remove_bg_edge_flood(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()

    # Detect if image already mostly transparent at corners
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    already_alpha = sum(1 for c in corners if c[3] < 20) >= 2

    visited = [[False] * w for _ in range(h)]
    q = deque()
    seeds = [
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
    for s in seeds:
        q.append(s)

    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or visited[y][x]:
            continue
        visited[y][x] = True
        r, g, b, a = px[x, y]

        if already_alpha:
            # Only clear leftover opaque white fringe connected to edge
            if a > 0 and r > 245 and g > 245 and b > 245 and (max(r, g, b) - min(r, g, b)) < 12:
                px[x, y] = (r, g, b, 0)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    q.append((nx, ny))
            elif a < 20:
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    q.append((nx, ny))
            continue

        if is_bg_pixel(r, g, b, a, "light"):
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
