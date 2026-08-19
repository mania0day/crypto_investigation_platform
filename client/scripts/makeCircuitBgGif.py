"""Circuit hex network GIF — slow pan into glowing hexagons (ping-pong)."""
from pathlib import Path

from PIL import Image

SRC = Path(
    r"C:\Users\Abeera Zainab\.cursor\projects\c-Users-Abeera-Zainab-OneDrive-Desktop-Cryp"
    r"\assets\c__Users_Abeera_Zainab_AppData_Roaming_Cursor_User_workspaceStorage_"
    r"empty-window_images_955537245970508455-b209718f-98da-45e1-8018-132cb8fec477.png"
)
OUT_DIR = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public")

W, H = 420, 236  # 16:9 — keep file size reasonable
FRAMES = 22
DURATION_MS = 100


def long_path(p: Path) -> str:
    s = str(p.resolve())
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


def crop_pan(img: Image.Image, t: float) -> Image.Image:
    """t 0..1 — ease from open dark field toward circuit cluster (bottom-right)."""
    k = t * t * (3 - 2 * t)
    zoom = 1.12 + 0.18 * k  # gentle zoom into hexagons
    cx = 0.42 + 0.28 * k  # left/center → right
    cy = 0.48 + 0.18 * k  # mid → lower (where hexes live)

    iw, ih = img.size
    cw = iw / zoom
    ch = ih / zoom
    left = max(0, min(iw - cw, cx * iw - cw / 2))
    top = max(0, min(ih - ch, cy * ih - ch / 2))
    box = (int(left), int(top), int(left + cw), int(top + ch))
    return img.crop(box).resize((W, H), Image.Resampling.LANCZOS)


def main():
    base = Image.open(long_path(SRC)).convert("RGB")
    base.resize((W, H), Image.Resampling.LANCZOS).save(
        OUT_DIR / "cipher-bg.jpg", "JPEG", quality=90
    )

    frames = []
    for i in range(FRAMES):
        t = i / (FRAMES - 1)
        frame = crop_pan(base, t).convert("P", palette=Image.Palette.ADAPTIVE, colors=64)
        frames.append(frame)

    # Ping-pong: drift in, then ease back out
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
