"""Sound design: real CC0 samples (assets/sfx/, credits in assets/sfx/CREDITS.txt) placed on the rig's cues, with the
original synthesised sounds as the fallback for any sample that is missing (and for the end-card ticks).

Edit v4 cues (bot/rig.py edit_cues / cue_sheet; each one lands on the frame the picture does it) and their samples:
  whoosh  dir "left" (a whip cut): whoosh_deep, its crest on the cut, travelling right to left;
          any other whoosh (hook, caption, chapter, outro, zoom-through): whoosh_short, its crest on the landing
  impact  the impact frame: impact_cinematic, its transient on the frame, layered with the synth sub (a sine dropping
          62 -> 38 Hz + its 2nd/3rd harmonics, which carry the weight on phone speakers)
  boom    the reveal's freeze frame: impact_big
  hit     a punch-in, flash or zoom cut: impact_punch
  subdrop a dip to black: impact_downer or sub_drop
  riser   a slow-motion ramp into the reveal: riser_scary or riser_tension, cut on the crest of its build so the build
          ENDS exactly on the landing frame (the cut / impact comes out of silence)
  swell   a light-leak cut: reverse_swell, its peak on the cut
  glitch  a tape-glitch cut: glitch_vhs or glitch_corrupt
  tick    synthesised: a tiny bright pluck per end-card / chapter letter, stepping up a pentatonic scale
  room    not a cue: room_tone looped quietly under the whole video from a random point, level per style
Each sample is aligned by its own detected anchor (onset, crest or end of build), not by the file start. Per video
(the episode seed) the alternatives, a pitch shift of up to +-2 semitones and a gain jitter of +-1.5 dB vary, so two
videos never sound identical; the same storyboard always sounds the same.

The score is ducked under the effects (a sidechain envelope built from the cue windows, so it is exact and
deterministic), then the mix is peak-limited. bot/render.py loudness-normalises the result (two-pass loudnorm,
-14 LUFS, -2 dBTP before AAC).

The synth fallback, one per kind: a layered band-passed noise whoosh; a thump (sub + harmonics + body + crack + dark
tail); a tight kick; a noise + detuned-tone riser; a reversed reverb swell; a falling sine sub drop; a sample-and-hold
glitch; brown-noise + mains-hum room tone.
"""
from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

from . import config
from .music import SR, lowpass

log = logging.getLogger(__name__)

LEVEL = 0.55          # the effects bus under the score (the score itself peaks at 0.85)
PEAK = 0.89           # about -1 dBFS before loudnorm
DUCK = {"whoosh": (0.30, 0.22, 0.12), "impact": (0.55, 0.005, 0.35), "tick": (0.10, 0.004, 0.06),
        "hit": (0.35, 0.004, 0.18), "boom": (0.55, 0.005, 0.5), "riser": (0.25, 0.4, 0.05),
        "swell": (0.3, 0.3, 0.1), "subdrop": (0.4, 0.01, 0.5), "glitch": (0.3, 0.003, 0.1)}
#        kind: (depth, attack s, release s); depth 0.55 is about -7 dB
WHOOSH_PRE, WHOOSH_POST = 0.30, 0.16  # seconds before / after the landing frame
PENTA = (0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24, 26)
START_RAMP = 0.03     # seconds the whole mix ramps in from silence (see mix)
ROOM = {"slow_dread": 0.05, "found_footage": 0.06, "monster_reveal": 0.045, "chase_cut": 0.025,
        "rules_list": 0.025, "pov_walk": 0.035, "long": 0.03}   # room-tone level per grade / style

SAMPLE_DIR = config.ASSETS / "sfx"
# cue role -> the samples it may use; a role whose files are all missing keeps its synth sound
SAMPLES = {"whip": ("whoosh_deep",), "rise": ("whoosh_short",), "impact": ("impact_cinematic",),
           "boom": ("impact_big",), "hit": ("impact_punch",), "subdrop": ("impact_downer", "sub_drop"),
           "riser": ("riser_scary", "riser_tension"), "swell": ("reverse_swell",),
           "glitch": ("glitch_vhs", "glitch_corrupt"), "room": ("room_tone",)}
TRIM_DB = {"whoosh_deep": -2.0, "whoosh_short": -2.0, "impact_punch": -2.5, "sub_drop": -1.0, "riser_tension": -1.0,
           "reverse_swell": -1.0, "glitch_vhs": 4.0, "glitch_corrupt": 3.0}
#          per-sample balance (the files are all peak-normalised), measured against the synth sounds they replace
PITCH_SPAN = 2.0      # semitones: a video's pitch shift (and each cue's on top of it) stays inside +-this
JITTER_DB = 1.5       # each cue's gain varies by up to +-this
SUB_MIX = 0.55        # the synth sub under impact_cinematic (phone weight)
DRIVE = {"impact": 1.8, "boom": 2.0, "hit": 1.6, "subdrop": 1.5}   # saturation per role (the synth hits are saturated)
ROOM_XFADE = 0.5      # seconds of crossfade where the room tone loops
PAN = {"whip": (0.55, -0.45), "rise": (-0.18, 0.18), "riser": (-0.3, 0.3), "swell": (0.4, -0.1)}  # start, end


# ------------------------------------------------------------------------------------------------ building blocks
def _svf_band(x: np.ndarray, fc: np.ndarray, q: float = 0.7) -> np.ndarray:
    """Chamberlin state-variable band-pass with a per-sample cutoff (fc < SR / 6 keeps it stable)."""
    f = 2 * np.sin(np.pi * np.clip(fc, 20, SR / 7) / SR)
    low = band = 0.0
    out = []
    for xi, fi in zip(x.tolist(), f.tolist()):  # plain floats: ~10x faster than numpy scalar indexing
        low += fi * band
        band += fi * (xi - low - q * band)
        out.append(band)
    return np.asarray(out)


def _bp(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return sosfilt(butter(2, [lo, min(hi, SR / 2 - 100)], btype="band", fs=SR, output="sos"), x)


def _hp(x: np.ndarray, fc: float) -> np.ndarray:
    return sosfilt(butter(2, fc, btype="high", fs=SR, output="sos"), x)


def _lp(x: np.ndarray, fc: float) -> np.ndarray:
    return sosfilt(butter(2, fc, btype="low", fs=SR, output="sos"), x)


def _norm(x: np.ndarray) -> np.ndarray:
    return x / (np.max(np.abs(x)) + 1e-9)


def _tail(x: np.ndarray, rng: np.random.Generator, seconds: float, bright: float = 2500.0) -> np.ndarray:
    """A small dark room: x convolved with a decaying, low-passed noise impulse (normalised, its length added)."""
    n = int(seconds * SR)
    ir = _lp(rng.standard_normal(n), bright) * np.exp(-np.linspace(0, 7, n))
    ir[0] = 0
    return _norm(fftconvolve(x, ir))


def _sweep(f0: float, f1: float, n: int, curve: float = 1.0) -> np.ndarray:
    """Phase of a tone gliding f0 -> f1 (exponential in pitch) over n samples."""
    k = np.linspace(0, 1, n) ** curve
    return 2 * np.pi * np.cumsum(f0 * (f1 / f0) ** k) / SR


def _soft_start(x: np.ndarray, ms: float = 2.0) -> np.ndarray:
    k = int(ms / 1000 * SR)
    x[:k] *= np.linspace(0, 1, k)
    return x


# ------------------------------------------------------------------------------------------------ the sounds
def whoosh(rng: np.random.Generator, strength: float = 1.0, long: bool = False) -> tuple[np.ndarray, int]:
    """(mono samples, index of the landing sample). The swept band of noise, a high air layer that opens on the
    landing, and (long: a whip) a low doppler tone under it."""
    pre = WHOOSH_PRE * (1.4 if long else 1.0)
    n_pre, n_post = int(pre * SR), int(WHOOSH_POST * SR)
    n = n_pre + n_post
    t = np.arange(n) / SR
    rise = np.clip(t / pre, 0, 1)
    fall = np.clip((t - pre) / WHOOSH_POST, 0, 1)
    lo, hi = (260.0, 3200.0) if long else (340.0, 2300.0)
    fc = lo + (hi - lo) * rise ** 2 * (1 - 0.6 * fall)
    body = _svf_band(rng.standard_normal(n), fc, q=0.55 if long else 0.75)
    env = np.where(t < pre, rise ** 2.4, np.exp(-(t - pre) / (WHOOSH_POST / 3.5)))
    air = _hp(rng.standard_normal(n), 4500.0) * np.where(t < pre, rise ** 4, np.exp(-(t - pre) / 0.03))
    out = _norm(body) * env + 0.3 * _norm(air)
    if long:
        out += 0.4 * np.sin(_sweep(95.0, 260.0, n, 1.6)) * env
    return _norm(out) * strength, n_pre


def thump(rng: np.random.Generator, strength: float = 1.0, big: bool = False, sub_only: bool = False) -> np.ndarray:
    """The impact: sub + harmonics + body + crack, saturated, with a short dark tail. Starts on its first sample.
    sub_only: just the sub and its harmonics, softly saturated (the weight layer under a sampled impact)."""
    n = int((1.6 if big else 0.9) * SR)
    t = np.arange(n) / SR
    f = 38 + 24 * np.exp(-t * (10 if big else 18)) + rng.uniform(-1.5, 1.5)
    ph = 2 * np.pi * np.cumsum(f) / SR
    sub = np.sin(ph) * np.exp(-t * (2.8 if big else 5.5))
    harm = (0.45 * np.sin(2 * ph) + 0.25 * np.sin(3 * ph + 0.3)) * np.exp(-t * (4.5 if big else 8))
    if sub_only:
        return _norm(_soft_start(np.tanh(1.4 * (sub + harm)))) * strength
    body = _norm(_bp(rng.standard_normal(n), 90, 420)) * np.exp(-t * 22) * 0.9
    crack = _norm(_hp(rng.standard_normal(n), 2500)) * np.exp(-t * 160) * 0.8
    click = _norm(lowpass(rng.standard_normal(n), 2800)) * np.exp(-t * 140) * 0.6
    dry = np.tanh(1.8 * (sub + harm + body + crack + click)) / np.tanh(1.8)
    wet = _tail(dry, rng, 1.4 if big else 0.6, 1800.0)[:n]
    out = dry + (0.3 if big else 0.18) * wet
    return _norm(_soft_start(out)) * strength


def kick(rng: np.random.Generator, strength: float = 1.0) -> np.ndarray:
    """A punch-in / flash-cut hit: a tight pitched kick and a snap."""
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * np.cumsum(48 + 75 * np.exp(-t * 40)) / SR
    body = np.sin(ph) * np.exp(-t * 11) + 0.3 * np.sin(2 * ph) * np.exp(-t * 16)
    snap = _norm(_bp(rng.standard_normal(n), 1800, 7000)) * np.exp(-t * 90) * 0.5
    return _norm(_soft_start(np.tanh(1.5 * (body + snap)))) * strength


def riser(rng: np.random.Generator, seconds: float, strength: float = 1.0) -> np.ndarray:
    """Ends ON its last sample (it is placed so that is the landing frame): a crescendo, then 6 ms to silence."""
    n = max(int(0.2 * SR), int(seconds * SR))
    k = np.linspace(0, 1, n)
    noise = _svf_band(rng.standard_normal(n), 300 * 20 ** (k ** 1.4), q=0.35)
    ph = _sweep(110, 880, n, 1.3)
    tone = np.sin(ph) + 0.8 * np.sin(ph * 1.012) + 0.25 * np.sin(2 * ph)
    out = (_norm(noise) * 0.8 + _norm(tone) * 0.45) * k ** 2.5
    m = int(0.006 * SR)
    out[-m:] *= np.linspace(1, 0, m)
    return _norm(out) * strength


def swell(rng: np.random.Generator, seconds: float = 0.9, strength: float = 1.0) -> np.ndarray:
    """A reversed reverb tail: swells up and stops on its last sample (the cut)."""
    seconds = max(0.1, seconds)
    m = int(0.05 * SR)
    tt = np.arange(m) / SR
    hit = (_norm(_bp(rng.standard_normal(m), 300, 5000)) + np.sin(2 * np.pi * 330 * tt)) * np.exp(-tt * 60)
    out = _tail(hit, rng, seconds, 4000.0)[: int(seconds * SR)][::-1].copy()
    k = int(0.004 * SR)
    out[-k:] *= np.linspace(1, 0, k)
    return _norm(out) * strength


def subdrop(rng: np.random.Generator, strength: float = 1.0) -> np.ndarray:
    n = int(1.5 * SR)
    t = np.arange(n) / SR
    ph = _sweep(72, 30, n, 0.6)
    env = np.clip(t / 0.008, 0, 1) * np.exp(-t * 2.2)
    out = (np.sin(ph) + 0.4 * np.sin(2 * ph) + 0.18 * np.sin(3 * ph)) * env
    out += _norm(_lp(rng.standard_normal(n), 300)) * np.exp(-t * 9) * 0.3
    return _norm(np.tanh(1.3 * out)) * strength


def glitch_burst(rng: np.random.Generator, strength: float = 1.0) -> np.ndarray:
    n = int(0.22 * SR)
    hold = int(rng.integers(6, 30))
    sh = np.repeat(rng.uniform(-1, 1, n // hold + 1), hold)[:n]          # sample-and-hold: digital, crunchy
    chop = int(0.022 * SR)
    gate = np.repeat(rng.random(n // chop + 1) < 0.7, chop)[:n]
    gate[:chop] = True                                                  # the first stutter is always ON the cut
    tone = np.sign(np.sin(2 * np.pi * rng.uniform(600, 1400) * np.arange(n) / SR)) * 0.3
    out = _bp((sh + tone) * gate * np.exp(-np.arange(n) / SR * 6), 200, 9000)
    return _norm(_soft_start(out)) * strength


def tick(rng: np.random.Generator, step: int, strength: float = 1.0) -> np.ndarray:
    n = int(0.06 * SR)
    t = np.arange(n) / SR
    freq = 1320 * 2 ** (PENTA[step % len(PENTA)] / 12) * (1 + rng.uniform(-0.004, 0.004))
    tone = (np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2.01 * t)) * np.exp(-t * 85)
    click = rng.standard_normal(n) * np.exp(-t * 900) * 0.25
    out = tone + click
    out[: int(0.001 * SR)] *= np.linspace(0, 1, int(0.001 * SR))
    return out * strength


def room_tone(n: int, seed: int, level: float) -> np.ndarray:
    """(n, 2) peaking at `level`, faded in and out: the room_tone sample looped (crossfaded at the seam) from a random
    point per episode, else the synth. The silence between sounds is never digital silence."""
    if level <= 0 or n <= 0:
        return np.zeros((n, 2))
    x = _sample("room_tone")
    xf = int(ROOM_XFADE * SR)
    if x is None or len(x) <= 4 * xf:
        return _synth_room(n, seed, level)
    k = np.sqrt(np.linspace(0, 1, xf))[:, None]          # equal power: the recording is noise, uncorrelated at the seam
    loop = x[:-xf].copy()
    loop[:xf] = x[:xf] * k + x[-xf:] * k[::-1]           # the tail fades into the head: tiling `loop` has no seam
    start = int(np.random.default_rng([seed & 0xFFFFFFFF, 99, 1]).integers(len(loop)))
    reps = -(-(start + n) // len(loop))
    out = np.tile(loop, (reps, 1))[start:start + n].copy()
    out *= level / (np.max(np.abs(loop)) + 1e-9)
    return _fade_ends(out)


def _synth_room(n: int, seed: int, level: float) -> np.ndarray:
    """(n, 2): brown noise under 220 Hz and a faint 50 Hz mains hum with harmonics, breathing slowly in level."""
    rng = np.random.default_rng([seed & 0xFFFFFFFF, 99])
    t = np.arange(n) / SR
    out = np.zeros((n, 2))
    for ch in range(2):
        brown = _hp(_lp(np.cumsum(rng.standard_normal(n)), 220.0), 30.0)
        hum = sum(a * np.sin(2 * np.pi * 50 * h * t + rng.uniform(0, 6.28)) for h, a in ((1, 0.25), (2, 0.15), (3, 0.08)))
        out[:, ch] = (_norm(brown) + hum * 0.6) * (1 + 0.25 * np.sin(2 * np.pi * 0.07 * t + ch))
    out *= level / (np.max(np.abs(out)) + 1e-9)
    return _fade_ends(out)


def _fade_ends(out: np.ndarray, seconds: float = 0.5) -> np.ndarray:
    fade = int(min(len(out) // 2, seconds * SR))
    if fade:
        out[:fade] *= np.linspace(0, 1, fade)[:, None]
        out[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return out


# ------------------------------------------------------------------------------------------------ the samples
@lru_cache(maxsize=32)
def _load(path: str) -> np.ndarray | None:
    """(n, 2) float64 at SR, peak 1, read-only; None when the file is missing or cannot be decoded."""
    if not Path(path).is_file():
        return None
    try:
        raw = subprocess.run([config.ffmpeg_exe(), "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-ar",
                              str(SR), "-"], capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError, config.ConfigError) as e:
        log.warning("sound effect %s unreadable (%s); using the synth", Path(path).name, e)
        return None
    x = np.frombuffer(raw, np.float32).reshape(-1, 2).astype(np.float64)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if len(x) < int(0.02 * SR) or not np.isfinite(peak) or peak <= 0:
        log.warning("sound effect %s is empty or broken; using the synth", Path(path).name)
        return None
    x /= peak
    x.flags.writeable = False
    return x


def _sample(name: str) -> np.ndarray | None:
    return _load(str(SAMPLE_DIR / f"{name}.ogg"))


def _smooth(v: np.ndarray, k: int) -> np.ndarray:
    """Centred moving average over k samples (same length)."""
    c = np.concatenate(([0.0], np.cumsum(v)))
    i = np.arange(len(v))
    lo, hi = np.clip(i - k // 2, 0, len(v)), np.clip(i - k // 2 + k, 0, len(v))
    return (c[hi] - c[lo]) / np.maximum(1, hi - lo)


def _env(x: np.ndarray, ms: float) -> np.ndarray:
    """Amplitude envelope of a stereo sound: the louder channel, averaged over `ms` (centred)."""
    return _smooth(np.abs(x).max(axis=1), max(1, int(ms / 1000 * SR)))


def onset(x: np.ndarray) -> int:
    """The transient: where the level jumps fastest (5 ms RMS, rise over 10 ms) at or before the loudest point,
    among the parts within 20 dB of it; then the first sample of that jump within 3 dB of where it lands."""
    d = int(0.01 * SR)
    pad = np.concatenate((np.zeros(d), (x ** 2).mean(axis=1)))     # silence before it: a sound may start on sample 0
    e = np.sqrt(_smooth(pad, int(0.005 * SR)))
    lev = 20 * np.log10(e + 1e-9)
    top = int(np.argmax(e))
    rise = np.full(len(e), -np.inf)
    rise[d:] = lev[d:] - lev[:-d]
    rise[top + 1:] = -np.inf
    rise[e < 0.1 * e[top]] = -np.inf
    i = int(np.argmax(rise))
    a = max(0, i - d)
    return max(0, a + int(np.argmax(lev[a:i + 1] >= lev[i] - 3.0)) - d)


def crest(x: np.ndarray) -> int:
    """Where a swell or whoosh is loudest (30 ms envelope)."""
    return int(np.argmax(_env(x, 30.0)))


def build_end(x: np.ndarray) -> int:
    """Where a riser's build tops out: the first sample whose 50 ms envelope is within 3 dB of its loudest."""
    env = _env(x, 50.0)
    return int(np.argmax(env >= 0.7 * env.max()))


def varispeed(x: np.ndarray, semitones: float) -> np.ndarray:
    """Pitch (and length) shifted by `semitones`, like a tape played faster or slower."""
    r = 2 ** (semitones / 12)
    if abs(r - 1) < 1e-4:
        return x
    n = max(2, int(len(x) / r))
    src, pos = np.arange(len(x)), np.arange(n) * r
    return np.stack([np.interp(pos, src, x[:, ch]) for ch in range(x.shape[1])], axis=1)


def role(cue: dict) -> str | None:
    """Which sample set a cue uses (None: always the synth)."""
    kind = cue["kind"]
    if kind == "whoosh":
        return "whip" if cue.get("dir") == "left" else "rise"
    return kind if kind in SAMPLES else None


def video_pitch(seed: int) -> float:
    """The episode's own pitch offset in semitones (every sampled cue of the video starts from it)."""
    return float(np.random.default_rng([seed & 0xFFFFFFFF, 0x5F]).uniform(-1.0, 1.0))


def sampled(cue: dict, rng: np.random.Generator, pitch: float) -> tuple[np.ndarray, int] | None:
    """(stereo sound at the cue's gain, index of the sample that lands ON the cue frame), or None when the cue's samples
    are missing (the caller uses the synth). Impacts, hits, booms, drops and glitches land their detected onset there,
    a whoosh its crest; a riser or swell is cut on its crest and placed so it ends on the frame (index = its length)."""
    r = role(cue)
    names = [nm for nm in SAMPLES.get(r or "", ()) if _sample(nm) is not None]
    if not names:
        return None
    name = names[int(rng.integers(len(names)))]
    semis = float(np.clip(pitch + rng.uniform(-0.8, 0.8), -PITCH_SPAN, PITCH_SPAN))
    x = varispeed(_sample(name), semis)
    gain = float(cue.get("gain", 1.0)) * 10 ** ((TRIM_DB.get(name, 0.0) + rng.uniform(-JITTER_DB, JITTER_DB)) / 20)
    if r in ("riser", "swell"):
        end = build_end(x) if r == "riser" else crest(x)
        a = max(0, end - int(float(cue.get("len", 1.0 if r == "riser" else 0.8)) * SR))
        y = x[a:end].copy()
        if len(y) < int(0.05 * SR):
            return None
        if a > 0:                                         # cut into the build: no click where it starts
            k = min(len(y) // 4, int(0.03 * SR))
            y[:k] *= np.linspace(0, 1, k)[:, None]
        m = int(0.006 * SR)
        y[-m:] *= np.linspace(1, 0, m)[:, None]           # stops dead on the frame, without a click
        return y / (np.abs(y).max() + 1e-12) * gain, len(y)
    anchor = crest(x) if r in ("whip", "rise") else onset(x)
    if r == "impact":                                     # the synth sub under it, from the same transient
        sub = thump(rng, SUB_MIX, sub_only=True)
        y = np.zeros((max(len(x), anchor + len(sub)), 2))
        y[:len(x)] = x
        y[anchor:anchor + len(sub)] += sub[:, None]
        x = y
    if r in DRIVE:                                        # tape-style saturation: body and weight, same peak
        x = np.tanh(DRIVE[r] * x / (np.abs(x).max() + 1e-12))
    else:
        x = x.copy()
    k = min(len(x) // 4, int(0.003 * SR))                 # a 3 ms ramp in: a hard step makes the limiter / AAC overshoot
    x[:k] *= np.linspace(0, 1, k)[:, None]
    return x / (np.abs(x).max() + 1e-12) * gain, anchor


# ------------------------------------------------------------------------------------------------ placing + mixing
def _place(bus: np.ndarray, snd: np.ndarray, at: int, pan: tuple[float, float]) -> None:
    """Add a mono or stereo sound into the stereo bus starting at sample `at` (clipped to the bus), with a start/end
    pan (-1 left .. +1 right). Mono is panned at equal power; stereo keeps its image and is balanced, at the same
    level as a centred mono sound."""
    a, b = max(0, at), min(len(bus), at + len(snd))
    if a >= b:
        return
    seg = snd[a - at: b - at]
    p = np.linspace(pan[0], pan[1], len(snd))[a - at: b - at]
    if seg.ndim == 1:
        bus[a:b, 0] += seg * np.sqrt((1 - p) / 2)
        bus[a:b, 1] += seg * np.sqrt((1 + p) / 2)
    else:
        bus[a:b, 0] += seg[:, 0] * np.sqrt(0.5) * np.clip(1 - p, 0, 1)
        bus[a:b, 1] += seg[:, 1] * np.sqrt(0.5) * np.clip(1 + p, 0, 1)


def cue_sample(cue: dict, fps: int) -> int:
    return int(round(cue["f"] / fps * SR))


def effects(cues: list[dict], n: int, fps: int, seed: int) -> np.ndarray:
    """The effects bus alone, (n, 2) float. Each sound's landing sample is exactly its cue frame (risers and swells
    END there; a whoosh crests there; everything else has its transient there)."""
    bus = np.zeros((n, 2))
    pitch = video_pitch(seed)
    for idx, c in enumerate(cues):
        rng = np.random.default_rng([seed & 0xFFFFFFFF, idx, int(c["f"])])
        at, g, kind = cue_sample(c, fps), float(c.get("gain", 1.0)), c["kind"]
        hit = sampled(c, np.random.default_rng([seed & 0xFFFFFFFF, idx, int(c["f"]), 7]), pitch)
        if hit is not None:
            snd, anchor = hit
            if kind == "glitch":
                p = float(rng.uniform(-0.5, 0.5))
                pan = (p, -p)
            else:
                pan = PAN.get(role(c) or "", (0.0, 0.0))
            _place(bus, snd, at - anchor, pan)
            continue
        if kind == "whoosh":
            long = c.get("dir") == "left"
            mono, land = whoosh(rng, g, long)
            pan = (0.55, -0.45) if long else (-0.18, 0.18)  # a whip travels right to left; a rise sits centred
            _place(bus, mono, at - land, pan)
        elif kind == "impact":
            _place(bus, thump(rng, g), at, (0.0, 0.0))
        elif kind == "tick":
            _place(bus, tick(rng, int(c.get("i", 0)), g), at, (-0.25 + 0.05 * (int(c.get("i", 0)) % 10),) * 2)
        elif kind == "hit":
            _place(bus, kick(rng, g), at, (0.0, 0.0))
        elif kind == "boom":
            _place(bus, thump(rng, g, big=True), at, (0.0, 0.0))
        elif kind == "riser":
            mono = riser(rng, float(c.get("len", 1.0)), g)
            _place(bus, mono, at - len(mono), (-0.3, 0.3))
        elif kind == "swell":
            mono = swell(rng, float(c.get("len", 0.9)), g)
            _place(bus, mono, at - len(mono), (0.4, -0.1))
        elif kind == "subdrop":
            _place(bus, subdrop(rng, g), at, (0.0, 0.0))
        elif kind == "glitch":
            p = float(rng.uniform(-0.5, 0.5))
            _place(bus, glitch_burst(rng, g), at, (p, -p))
    return bus


def duck_curve(cues: list[dict], n: int, fps: int) -> np.ndarray:
    """Gain (n,) applied to the score: dips under each effect, never stacks deeper than the deepest one."""
    duck = np.zeros(n)
    for c in cues:
        depth, att, rel = DUCK[c["kind"]]
        depth *= min(1.0, float(c.get("gain", 1.0)) / 0.5)
        at = cue_sample(c, fps)
        if c["kind"] == "whoosh":
            pre = WHOOSH_PRE
        elif c["kind"] in ("riser", "swell"):
            pre = float(c.get("len", 1.0))      # the music backs off for the whole build
        else:
            pre = att
        start = at - int(pre * SR)
        hold = at + int((WHOOSH_POST * 0.5 if c["kind"] == "whoosh" else att) * SR)
        stop = hold + int(rel * 4 * SR)
        a, b = max(0, start), min(n, stop)
        if a >= b:
            continue
        i = np.arange(a, b)
        shape = np.where(i < at, np.clip((i - start) / max(1, at - start), 0, 1),
                         np.where(i < hold, 1.0, np.exp(-(i - hold) / (rel * SR))))
        duck[a:b] = np.maximum(duck[a:b], depth * shape)
    return 1.0 - duck


def mix(score: np.ndarray, cues: list[dict], fps: int, seed: int, room: float = 0.0) -> np.ndarray:
    """Score (n, 2) + effects on the cues, the score ducked under them, a room tone under it all, peak-limited.
    float32 (n, 2)."""
    if not cues and room <= 0:
        return score
    n = len(score)
    out = score.astype(np.float64) * duck_curve(cues, n, fps)[:, None] + effects(cues, n, fps, seed) * LEVEL
    out += room_tone(n, seed, room)
    # the cold open's impact starts on sample 0: a hard full-scale onset there makes the AAC encoder (no look-ahead
    # before the first block) overshoot ~3 dB past 0 dBFS. A 30 ms ramp keeps the hit and the file under -1 dBTP.
    k = min(n, int(START_RAMP * SR))
    out[:k] *= np.linspace(0.0, 1.0, k)[:, None]
    peak = np.max(np.abs(out))
    if peak > PEAK:
        out *= PEAK / peak
    return out.astype(np.float32)
