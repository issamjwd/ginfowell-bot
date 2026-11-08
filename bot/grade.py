"""Cinematic colour grade per edit style, as ONE ffmpeg filter chain run inside the clean video's encode (edit v4).

The Python renderer used to grade every frame in numpy (bloom, contrast, vignette, grain: four full-frame float passes
per frame). The look now lives here, in the encoder's own filter graph, which is both cheaper and better:

  curves        a filmic S-curve per channel: a soft toe (blacks crushed but not clipped), a rolled-off shoulder
                (highlights never hard-clip), the contrast in the mid-tones where faces and monsters are
  split-toning  shadows pushed one way (teal / green / cold blue), highlights the other (warm / rose), folded into
                the same per-channel curves (look-up tables: cheap)
  eq            saturation and gamma per style
  halation      the highlights alone (a threshold curve) at quarter size, blurred, tinted red-orange like film
                halation, screen-blended back: lamps and screens glow, the rest of the frame stays sharp
  vignette      ffmpeg's optical cos^4 vignette (a lens falloff, not a drawn gradient blob), computed once
  grain         fine temporal luma grain on the final yuv planes (no colour noise), re-seeded every frame

Each style is a Look (numbers only); chain() turns it into the filter string. Everything is deterministic and needs
no new packages. bot/render.py applies it when the storyboard carries "grade" (edit v4 Shorts and the long video).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Look:
    curve: str              # curves "all" points (x/y pairs), the filmic S
    shadows: tuple[float, float, float]    # split-tone push on the shadows (r, g, b), -1..1
    highlights: tuple[float, float, float]  # split-tone push on the highlights (r, g, b)
    mids: tuple[float, float, float] = (0.0, 0.0, 0.0)
    saturation: float = 1.0
    gamma: float = 1.0
    halation: float = 0.25  # screen-blend opacity of the highlight glow (0 = none)
    threshold: float = 0.72  # luma above which a pixel glows
    glow_tint: tuple[float, float, float] = (1.0, 0.55, 0.42)  # film halation: red-orange
    vignette: float = 0.42  # vignette angle in radians (bigger = darker corners); 0 = none
    grain: int = 7          # luma grain strength (ffmpeg noise c0s)


# Filmic S: (in/out) pairs. Toe lifts true black a hair (0.02) so it is never digital-dead, shoulder rolls off at 0.96.
S_SOFT = "0/0.02 0.2/0.13 0.5/0.47 0.85/0.87 1/0.96"
S_HARD = "0/0 0.15/0.09 0.5/0.47 0.8/0.85 1/0.96"
S_CRUSH = "0/0 0.12/0.06 0.5/0.46 0.85/0.86 1/0.95"
S_MILK = "0/0.06 0.2/0.17 0.5/0.48 0.85/0.84 1/0.93"   # lifted, milky blacks (dread, fog)

LOOKS: dict[str, Look] = {
    # fast and punchy: hard S, teal shadows / warm highlights (the classic action split), strong glow
    "chase_cut": Look(S_HARD, (-0.08, 0.02, 0.06), (0.07, 0.02, -0.08), mids=(-0.01, 0.01, -0.01), saturation=1.0,
                      halation=0.34, threshold=0.84, vignette=0.45, grain=7),
    # slow and cold: milky lifted blacks, blue-cyan shadows, pale highlights, low colour, a soft wide bloom
    "slow_dread": Look(S_MILK, (-0.06, 0.03, 0.08), (0.02, 0.02, 0.04), mids=(-0.04, 0.0, 0.05), saturation=0.62,
                       gamma=0.96, halation=0.22, threshold=0.82, glow_tint=(0.8, 0.85, 1.0), vignette=0.55, grain=9),
    # a camcorder tape: green shadows, flat sickly mids, crushed toe, no halation (tape has none), heavy grain
    "found_footage": Look(S_CRUSH, (-0.04, 0.08, -0.02), (0.04, 0.05, -0.06), mids=(0.0, 0.03, -0.03),
                          saturation=0.78, halation=0.0, vignette=0.5, grain=13),
    # the brand look: neutral-warm, rose highlights (the game's pink accent), clean and readable for the rule cards
    "rules_list": Look(S_SOFT, (-0.03, 0.02, 0.03), (0.06, 0.02, 0.02), mids=(0.0, 0.02, 0.0), saturation=0.98,
                       halation=0.26, threshold=0.84, glow_tint=(1.0, 0.6, 0.7), vignette=0.38, grain=6),
    # horror: deep crushed blacks, sickly green-yellow mids, red halation around every light
    "monster_reveal": Look(S_CRUSH, (-0.03, 0.06, 0.0), (0.06, 0.03, -0.06), mids=(0.0, 0.045, -0.04),
                           saturation=0.8, gamma=1.0, halation=0.38, threshold=0.84, glow_tint=(1.0, 0.35, 0.3),
                           vignette=0.52, grain=8),
    # walking: natural, slightly warm, gentle glow, light vignette (it should feel like your own eyes)
    "pov_walk": Look(S_SOFT, (-0.03, 0.01, 0.03), (0.05, 0.02, -0.03), mids=(0.0, 0.01, -0.01),
                     saturation=0.95, halation=0.2, threshold=0.85, vignette=0.32, grain=6),
    # the long video: a filmic neutral version of the Shorts' looks (16:9, 2 minutes: easy on the eyes)
    # (no halation: its full-size RGB blend costs ~2 min of runner time over 2,900 1080p frames)
    "long": Look(S_SOFT, (-0.05, 0.03, 0.04), (0.06, 0.02, -0.05), mids=(0.0, 0.03, -0.01),
                 saturation=0.96, halation=0.0, threshold=0.84,
                 vignette=0.4, grain=6),
}


def names() -> tuple[str, ...]:
    return tuple(LOOKS)


def _f(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") if x else "0"


TONE_X = tuple(i / 12 for i in range(13))
SPLIT = 1.2   # how far a split-tone value moves its tone band (0.1 -> 0.12 of full scale at the band centre)


def _curve_y(points: str, x: float) -> float:
    """Linear interpolation of a curves point list "x/y x/y ..." at x."""
    pts = [tuple(float(v) for v in p.split("/")) for p in points.split()]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-9, x1 - x0)
    return pts[-1][1]


def _tone_points(lk: Look, ch: int) -> str:
    """One channel's curve: the filmic S plus the split-tone pushes, weighted by tone band (shadows under 0.5,
    highlights over it, mids in between). The whole grade is then three 1-D look-up tables: ffmpeg's colorbalance
    does the same per pixel in float and cost ~10x more (2943 1080p frames a long video)."""
    out = []
    for x in TONE_X:
        ws = max(0.0, 1 - x / 0.5) ** 1.5
        wh = max(0.0, (x - 0.5) / 0.5) ** 1.5
        wm = max(0.0, 1 - ws - wh) * 4 * x * (1 - x)
        y = _curve_y(lk.curve, x) + SPLIT * (lk.shadows[ch] * ws + lk.mids[ch] * wm + lk.highlights[ch] * wh)
        out.append(f"{x:.3f}/{min(1.0, max(0.0, y)):.3f}")
    return " ".join(out)


def chain(name: str, width: int, height: int, bt709: bool = False) -> str:
    """The ffmpeg -vf chain for one look, from rgb24 input to yuv420p output (the caller adds its own colour-matrix
    scale before the format when it needs BT.709). Unknown names raise KeyError (the storyboard validator stops them
    first)."""
    lk = LOOKS[name]
    r_pts, g_pts, b_pts = (_tone_points(lk, ch) for ch in range(3))
    base = f"curves=r='{r_pts}':g='{g_pts}':b='{b_pts}'"
    if lk.saturation != 1.0 or lk.gamma != 1.0:
        base += f",eq=saturation={_f(lk.saturation)}:gamma={_f(lk.gamma)}"
    out = base
    if lk.halation > 0:
        qw, qh = max(2, width // 4 // 2 * 2), max(2, height // 4 // 2 * 2)
        t = lk.threshold
        r, g, b = lk.glow_tint
        sigma = max(2.0, width / 1080 * 9)
        # gbrp: the screen blend must run on R, G, B (on YUV it adds to the chroma planes and turns everything magenta)
        out = (f"{base},format=gbrp,split[hb][hs];"
               f"[hs]scale={qw}:{qh}:flags=bilinear,"
               f"curves=all='0/0 {_f(t)}/0 1/1',"                       # keep only what is above the threshold
               f"colorchannelmixer=rr={_f(r)}:gg={_f(g)}:bb={_f(b)},"
               f"gblur=sigma={sigma:.1f}:steps=2,"
               f"scale={width}:{height}:flags=bilinear[hg];"
               f"[hb][hg]blend=all_mode=screen:all_opacity={_f(lk.halation)}")
    if lk.vignette > 0:
        out += f",vignette=angle={_f(lk.vignette)}:mode=forward"
    # BT.709 (the long video): convert with that matrix here, before the grain goes onto the luma plane
    out += ",scale=out_color_matrix=bt709:out_range=tv,format=yuv420p" if bt709 else ",format=yuv420p"
    if lk.grain > 0:
        out += f",noise=c0s={lk.grain}:c0f=t+u"
    return out
