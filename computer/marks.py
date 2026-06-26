"""Set-of-marks overlay for Layer-3 vision (brief §3, §6, §10).

Frontier-grade VLMs can regress raw pixel coordinates; smaller free-tier
multimodal models (the only ones available without a paid API) cannot —
they return round, generic guesses. The fix the field settled on is
set-of-marks: draw numbered marks over candidate regions and ask the
model to PICK A NUMBER. Classification is reliable where regression is
not. The picked number maps back to a known coordinate we then click.

Here the candidate regions are a uniform grid over the window screenshot,
so the technique needs zero knowledge of where the target is — the model
does the perception (which number sits on the red circle), we do the
arithmetic (number → pixel center).
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def _font(size: int):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:                                  # noqa: BLE001
            continue
    return ImageFont.load_default()


def draw_grid_marks(src_png: str, dst_png: str, *,
                    cols: int = 5, rows: int = 4) -> tuple[dict[int, tuple[int, int]],
                                                           tuple[int, int]]:
    """Overlay a numbered grid on `src_png`, write to `dst_png`, and return
    ({mark_number: (x, y)}, (width, height)) in image pixel space."""
    im = Image.open(src_png).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im, "RGBA")
    fs = max(16, min(W, H) // 36)
    font = _font(fs)
    centers: dict[int, tuple[int, int]] = {}
    n = 1
    for r in range(rows):
        for c in range(cols):
            cx = int((c + 0.5) * W / cols)
            cy = int((r + 0.5) * H / rows)
            centers[n] = (cx, cy)
            label = str(n)
            tb = d.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            pad = fs // 3
            box = [cx - tw // 2 - pad, cy - th // 2 - pad,
                   cx + tw // 2 + pad, cy + th // 2 + pad]
            d.rectangle(box, fill=(255, 235, 59, 235), outline=(0, 0, 0, 255), width=2)
            d.text((cx - tw // 2, cy - th // 2 - tb[1]), label,
                   fill=(0, 0, 0, 255), font=font)
            n += 1
    # faint grid lines to make the cells legible
    for c in range(1, cols):
        x = int(c * W / cols)
        d.line([(x, 0), (x, H)], fill=(0, 0, 0, 60), width=1)
    for r in range(1, rows):
        y = int(r * H / rows)
        d.line([(0, y), (W, y)], fill=(0, 0, 0, 60), width=1)
    im.save(dst_png)
    return centers, (W, H)
