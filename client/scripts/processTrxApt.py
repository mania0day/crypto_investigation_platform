"""Process TRX + APT with clean edge background removal (no fringe)."""
import os
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

SRC = Path(
    r"C:\Users\Abeera Zainab\.cursor\projects\c-Users-Abeera-Zainab-OneDrive-Desktop-Cryp\assets"
)
OUT = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public\coins")
OUT.mkdir(parents=True, exist_ok=True)

PAIRS = [
    ("trx", ["trx_or_tron", "Premium_Photo-b6f212cf", "tron_custom"]),
    ("apt", ["Aptos__APT_Glass", "5caac2cc", "Aptos"]),
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


def is_light_bg(r, g, b, a, thresh=228):
    if a < 10:
        return True
    mx, mn = max(r, g, b), min(r, g, b)
    # near-white / light gray studio bg
    return mx >= thresh and (mx - mn) < 28


def flood_remove(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
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
        if is_light_bg(r, g, b, a) or a < 20:
            if a > 0:
                px[x, y] = (r, g, b, 0)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                q.append((nx, ny))
    return img


def clean_edge_fringe(img: Image.Image) -> Image.Image:
    """Strip light halos and semi-opaque fringe around the subject."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()

    # Pass 1: kill near-white low-alpha fringe anywhere
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            # light fringe with partial alpha
            if a < 200 and mx > 210 and (mx - mn) < 35:
                px[x, y] = (r, g, b, 0)
                continue
            # despill: pull light edge pixels toward opaque coin colors
            if a < 255 and mx > 200 and (mx - mn) < 40:
                # fade remaining light contamination
                fade = max(0, min(1, (245 - mx) / 40))
                px[x, y] = (r, g, b, int(a * fade * 0.35))

    # Pass 2: erode 1px of weak alpha border connected to transparent
    to_clear = []
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            r, g, b, a = px[x, y]
            if a == 0 or a > 230:
                continue
            neighbors = [
                px[x + 1, y][3],
                px[x - 1, y][3],
                px[x, y + 1][3],
                px[x, y - 1][3],
            ]
            if any(n == 0 for n in neighbors):
                mx = max(r, g, b)
                # only clear pale / weak fringe, keep dark coin edges
                if a < 180 or mx > 190:
                    to_clear.append((x, y))
    for x, y in to_clear:
        r, g, b, _ = px[x, y]
        px[x, y] = (r, g, b, 0)

    # Pass 3: slight alpha blur then hard threshold for smooth circular edge
    alpha = img.getchannel("A")
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=0.6))
    # harden: anything below 40 → 0, above 210 → 255, else keep
    a_px = alpha.load()
    for y in range(h):
        for x in range(w):
            v = a_px[x, y]
            if v < 40:
                a_px[x, y] = 0
            elif v > 210:
                a_px[x, y] = 255
    img.putalpha(alpha)

    # Pass 4: final light-spill kill on remaining edge
    px = img.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            neighbors_a = []
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    neighbors_a.append(px[nx, ny][3])
            if neighbors_a and min(neighbors_a) == 0:
                mx, mn = max(r, g, b), min(r, g, b)
                if mx > 215 and (mx - mn) < 30:
                    px[x, y] = (r, g, b, 0)

    return img


def to_square(cleaned: Image.Image) -> Image.Image:
    bbox = cleaned.getbbox()
    if not bbox:
        return cleaned
    # tight crop with tiny pad so edge doesn't clip
    cleaned = cleaned.crop(bbox)
    side = max(cleaned.size) + 8
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
            cleaned = clean_edge_fringe(cleaned)
        cleaned = to_square(cleaned)
        out = OUT / f"{name}.png"
        cleaned.save(out, "PNG")
        print("saved", out.name, cleaned.size)
    print("done")


if __name__ == "__main__":
    main()
