"""Create a smaller animated GIF for faster page loads."""
from pathlib import Path

from PIL import Image

SRC = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public\cipher-bg.jpg")
OUT = Path(r"C:\Users\Abeera Zainab\OneDrive\Desktop\Cryp\cryp\client\public\cipher-bg.gif")

W, H = 320, 568
FRAMES = 24
DURATION_MS = 110


def crop_zoom(img: Image.Image, t: float) -> Image.Image:
    k = t * t * (3 - 2 * t)
    zoom = 1.2 - 0.16 * k
    cx = 0.62 + 0.05 * k
    cy = 0.52 - 0.03 * k
    iw, ih = img.size
    cw = iw / zoom
    ch = ih / zoom
    left = max(0, min(iw - cw, cx * iw - cw / 2))
    top = max(0, min(ih - ch, cy * ih - ch / 2))
    box = (int(left), int(top), int(left + cw), int(top + ch))
    return img.crop(box).resize((W, H), Image.Resampling.LANCZOS)


def main():
    base = Image.open(SRC).convert("RGB")
    frames = []
    for i in range(FRAMES):
        t = i / (FRAMES - 1)
        frame = crop_zoom(base, t).convert("P", palette=Image.Palette.ADAPTIVE, colors=64)
        frames.append(frame)

    loop = frames + frames[-2:0:-1]
    loop[0].save(
        OUT,
        save_all=True,
        append_images=loop[1:],
        duration=DURATION_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"saved {OUT.name} ({len(loop)} frames, {OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
