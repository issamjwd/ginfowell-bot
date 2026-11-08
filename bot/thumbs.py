"""Custom thumbnail (1280x720 JPEG, < 2 MB): the dream repainted, one character, a short lowercase line."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import bank, config, fonts, look, sprites

W, H = 1280, 720


def make(sb: dict, out: Path, sprite_dir: Path | None) -> Path:
    th = sb["thumbnail"]
    d = bank.dream(th["dream"])
    night = sb.get("template") == "it_waves_back"
    src = Image.open(config.STILLS / d.still).convert("RGB")
    bg = look.drawn(look.cover(src, W, H), night=night).convert("RGBA")
    # darken the left for the text
    shade = np.zeros((H, W, 4), np.uint8)
    shade[..., :3] = (40, 16, 50)
    shade[..., 3] = (np.clip(1 - np.linspace(0, 1, W) / 0.62, 0, 1) ** 1.4 * 170).astype(np.uint8)[None, :]
    bg.alpha_composite(Image.fromarray(shade, "RGBA"))
    who = sb.get("monster") or "Dreamer"
    act = "reach" if sb.get("monster") else "idle"
    im = look.tint_shadow(sprites.drawings(who, act, sprite_dir)[-1])
    box = im.getchannel("A").point(lambda v: 255 if v > 200 else 0).getbbox() or (0, 0, im.width, im.height)
    im = im.crop(box)
    s = H * 0.92 / im.height
    im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gx = W - im.width - 40
    glow.paste(Image.new("RGBA", im.size, (255, 170, 210, 150)), (gx, H - im.height), im.getchannel("A"))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))
    bg.alpha_composite(im, (gx, H - im.height))
    dr = ImageDraw.Draw(bg)
    big = fonts.serif(132)
    small = fonts.serif(84, italic=True)
    for dx, dy, col in ((4, 6, (30, 10, 40, 200)), (0, 0, look.CREAM + (255,))):
        dr.text((64 + dx, 250 + dy), "liminality", font=big, fill=col, anchor="ls")
        dr.text((70 + dx, 360 + dy), th["text"], font=small, fill=col if dx else (255, 190, 220, 255), anchor="ls")
    dr.text((70, 660), "P I X E L H E L M", font=fonts.sans(30, "bold"), fill=look.PINK + (255,), anchor="ls")
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out, quality=90, optimize=True)
    if out.stat().st_size > 2 * 1024 * 1024:
        bg.convert("RGB").save(out, quality=78, optimize=True)
    return out
