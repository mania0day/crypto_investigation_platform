"""Glass panels GIF — horizontal left-to-right pan only (no vertical drift)."""
import os
from pathlib import Path

from PIL import Image

SRC_DIR = Path(
    r"C:\Users\Abeera Zainab\.cursor\projects\c-Users-Abeera-Zainab-OneDrive-Desktop-Cryp\assets"
)
OUT_DIR = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public")

W, H = 288, 512
FRAMES = 24
DURATION_MS = 100


def long_path(p: Path) -> str:
    s = str(p.resolve())
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


def find_src():
    root = long_path(SRC_DIR)
    for n in os.listdir(root):
        if "7edd43ed" in n or "956592777188034364" in n:
            return SRC_DIR / n
    raise FileNotFoundError("source image not found")


def crop_pan_x(img: Image.Image, t: float) -> Image.Image:
    """t 0..1 — pan left → right at fixed zoom/vertical position."""
    k = t * t * (3 - 2 * t)
    zoom = 1.18  # fixed — no zoom bounce that feels like up/down
    cy = 0.5  # locked vertical
    cx = 0.38 + 0.24 * k  # left → right

    iw, ih = img.size
    cw = iw / zoom
    ch = ih / zoom
    left = max(0, min(iw - cw, cx * iw - cw / 2))
    top = max(0, min(ih - ch, cy * ih - ch / 2))
    box = (int(left), int(top), int(left + cw), int(top + ch))
    return img.crop(box).resize((W, H), Image.Resampling.LANCZOS)


def main():
    src = find_src()
    base = Image.open(long_path(src)).convert("RGB")
    base.resize((W, H), Image.Resampling.LANCZOS).save(
        OUT_DIR / "cipher-bg.jpg", "JPEG", quality=90
    )

    frames = []
    for i in range(FRAMES):
        t = i / (FRAMES - 1)
        frame = crop_pan_x(base, t).convert("P", palette=Image.Palette.ADAPTIVE, colors=72)
        frames.append(frame)

    # Ping-pong: L→R then R→L
    loop = frames + frames[-2:0:-1]
    out = OUT_DIR / "cipher-bg.gif"
    loop[0].save(
        out,
        save_all=True,
        append_images=loop[1:],
        duration=DURATION_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"saved {out.name} ({len(loop)} frames, {out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
