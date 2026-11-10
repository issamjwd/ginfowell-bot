"""Timeline renderer: storyboard -> frames -> ffmpeg (H.264 + AAC), plus a contact sheet for review.

Animated scenes are drawn "on twos": character drawings, camera moves and caption boil change every 2nd frame at
24 fps. Footage plays on ones (it is real gameplay). Effects: impact (a hard flash frame, a zoom punch and real camera
shake; the overlay adds the particle burst), flash, shake, zoom punch, freeze, the drawn filter, the "vhs" look,
fade / whip / cut transitions on the beat. (The old manga speed lines and 3-tone ink frames are retired: off-brand.)
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from . import bank, config, fx, grade, look, motion, music, rig, sfx, sprites, voice
from .footage import ClipReader

log = logging.getLogger(__name__)
FIRST_SKIP_S = 1.5  # at most this much of a black lead-in is skipped on the first shot (frame 0 is never black)
TWOS = 12  # drawings per second
LUFS = -14  # YouTube plays everything at about -14 LUFS: master there so nothing is turned down (or up, noisily)


def ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


@dataclass
class Actorset:
    images: list[Image.Image]
    anchor: tuple[float, float]   # source px (feet centre) shared by all drawings
    ref_h: float                  # source px height of the character standing (idle)
    consistent: bool


class SpriteBook:
    """Loads and caches drawings per (character, action, flip) with one shared anchor per character."""

    def __init__(self, folder: Path | None):
        self.folder = folder

    @lru_cache(maxsize=64)
    def get(self, character: str, action: str, flip: bool) -> Actorset:
        imgs = [look.tint_shadow(im) for im in sprites.drawings(character, action, self.folder)]
        consistent = self.folder is not None and (self.folder / sprites.sprite_file(character, "idle", 1.0)).exists()
        if consistent:
            idle = look.tint_shadow(sprites.drawings(character, "idle", self.folder)[0])
            box = _body_box(idle)
        else:
            box = _body_box(imgs[0])
        anchor = ((box[0] + box[2]) / 2, box[3])
        ref_h = box[3] - box[1]
        if flip:
            imgs = [im.transpose(Image.FLIP_LEFT_RIGHT) for im in imgs]
            anchor = (imgs[0].width - anchor[0], anchor[1])
        return Actorset(imgs, anchor, ref_h, consistent)


def _body_box(im: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of the opaque body (ignores the translucent shadow)."""
    a = im.getchannel("A").point(lambda v: 255 if v > 200 else 0)
    return a.getbbox() or (0, 0, im.width, im.height)


class Renderer:
    """text=False renders a CLEAN video: no burned captions, no card or end-card lettering (the Remotion overlay
    adds all text afterwards). text=True is the original burned-in path, also the automatic fallback."""

    def __init__(self, sb: dict, w: int, h: int, fps: int = config.FPS, sprite_dir: Path | None = None,
                 ai_clips: dict[int, Path] | None = None, text: bool = True):
        self.sb, self.w, self.h, self.fps = sb, w, h, fps
        self.text = text
        self.s = min(w, h) / 1080  # 1.0 for both the 1080x1920 Short and the 1920x1080 long video
        self.book = SpriteBook(sprite_dir)
        # edit v4: the colour grade, glow, vignette and grain are one ffmpeg filter chain in the encode (bot/grade.py);
        # the numpy grade only runs for storyboards made before it (no "grade" field)
        g = sb.get("grade")
        self.look = g if isinstance(g, str) and g in grade.LOOKS else None
        self.grade = _identity if self.look else look.Grade(w, h, seed=sb["music"]["seed"])
        self.seed = int(sb["music"]["seed"])
        self.starts, t = [], 0.0
        for shot in sb["shots"]:
            self.starts.append(t)
            t += float(shot["seconds"])
        self.total = t
        self.n = int(round(t * fps))
        self.readers: dict[int, ClipReader] = {}
        self.prev_last: np.ndarray | None = None
        self.last_frame: np.ndarray | None = None
        self.last_k = -1
        self.ai_clips = ai_clips or {}
        self._twos: tuple | None = None   # (key, frame) of the last animated-scene frame (drawn on twos)
        self._backdrops: dict[int, np.ndarray] = {}

    # ------------------------------------------------------------------ shots
    def panel_width(self) -> int:
        """The long video's footage panel: the clips' own 8:9 shape at full height, on the right of the frame."""
        return min(self.w, int(round(self.h * 8 / 9 / 2)) * 2)

    def _backdrop(self, k: int, shot: dict) -> np.ndarray:
        """The panel layout's left side: the dream's drawn plate, a solid step darker so the text column reads."""
        if k not in self._backdrops:
            d = bank.dream(shot.get("backdrop") or self.sb["dream_ids"][0])
            plate = look.plate(d.still, False, self.w, self.h)
            x0, y0 = (plate.width - self.w) // 2, (plate.height - self.h) // 2
            arr = np.asarray(plate.crop((x0, y0, x0 + self.w, y0 + self.h)), np.float32) * 0.78
            self._backdrops[k] = arr.astype(np.uint8)
        return self._backdrops[k]

    def _clip(self, k: int, shot: dict, tl: float, fi: int) -> np.ndarray:
        panel = shot.get("layout") == "panel"
        cw = self.panel_width() if panel else self.w
        ramp = shot.get("ramp")
        if k not in self.readers:
            reader = ClipReader(shot["clip"], shot.get("from", 0.0), 1.0 if ramp else shot.get("speed", 1.0), cw,
                                self.h, self.fps, shot.get("pan", 0.0), freeze="freeze" in shot.get("fx", []))
            if k == 0 and not ramp:
                # frame 0 of a video is its first impression (and the Short's loop point): never black, even when the
                # recording opens on black frames. Starts on the first frame with a picture (up to FIRST_SKIP_S in).
                dropped = reader.skip_black(int(FIRST_SKIP_S * self.fps))
                if dropped:
                    log.info("shot 0: skipped %d black frame(s) at the start of %s", dropped, shot["clip"])
            self.readers[k] = motion.RampReader(reader, self.fps) if ramp else reader
        if ramp:
            f = self.readers[k].at(motion.source_time(tl, ramp, float(shot.get("speed", 1.0))))
        else:
            f = self.readers[k].next()
        f = self._camera(k, shot, tl, f, cw)
        if panel:
            out = self._backdrop(k, shot).copy()
            x0 = self.w - cw
            out[:, x0:] = f
            out[:, max(0, x0 - int(6 * self.s)):x0] = look.INK  # a solid ink edge between the column and the clip
            f = out
        f = self.grade(f, fi)
        if "drawn" in shot.get("fx", []):
            f = self._drawn_twos(k, f, tl)
        return f

    def _camera(self, k: int, shot: dict, tl: float, f: np.ndarray, cw: int) -> np.ndarray:
        """The shot's camera: the eased push / pull, snap-zoom punch-ins (zoom-blurred while they snap), handheld
        drift and a walking bob, the freeze frame's still-photo look. One crop + resize per frame."""
        z0, z1 = shot.get("zoom", [1.0, 1.0])
        dur = float(shot["seconds"])
        z = lerp(z0, z1, motion.ease_in_out_sine(tl / dur) if self.look else ease(tl / dur))
        punches, depth = shot.get("punches") or [], float(shot.get("punch", 0.1))
        env = motion.punch_env(tl, punches)
        z *= 1 + depth * env
        side = min(cw, self.h)
        amp = float(shot.get("hand") or 0.0) * side
        bob_amp = float(shot.get("bob") or 0.0) * side
        dx, dy, _ = motion.handheld(self.starts[k] + tl, self.seed + k, amp)
        dy += motion.bob(self.starts[k] + tl, bob_amp)
        if amp or bob_amp:
            z = max(z, motion.headroom(max(amp, bob_amp), cw, self.h))
        f = motion.camera(f, z, dx, dy)
        if punches:
            prev = motion.punch_env(tl - 1 / self.fps, punches)
            if env - prev > 0.15:                       # the snap itself: real zoom blur on that frame
                f = motion.radial_blur(f, depth * (env - prev) * 0.9)
        if shot.get("ramp") and motion.speed_at(tl, shot["ramp"]) == 0:
            f = motion.freeze_look(f, 0.8)
        return f

    def _drawn_twos(self, k: int, f: np.ndarray, tl: float) -> np.ndarray:
        key = (k, int(tl * TWOS))
        if getattr(self, "_drawn_key", None) != key:
            self._drawn_val = np.asarray(look.drawn(Image.fromarray(f), colours=36, ink=0.75, soften=1))
            self._drawn_key = key
        return self._drawn_val

    def _zoom(self, f: np.ndarray, z: float) -> np.ndarray:
        if z <= 1.001:
            return f
        h, w = f.shape[:2]
        cw, ch = int(w / z), int(h / z)
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        return np.asarray(Image.fromarray(f[y0:y0 + ch, x0:x0 + cw]).resize((w, h), Image.BILINEAR))

    def _scene(self, k: int, shot: dict, tl: float, fi: int) -> np.ndarray:
        d = bank.dream(shot["dream"])
        night = bool(shot.get("night"))
        plate = look.plate(d.still, night, self.w, self.h)
        dur = float(shot["seconds"])
        tq = math.floor(tl * TWOS) / TWOS                       # camera on twos
        cam = shot.get("camera", {})
        z = lerp(*cam.get("zoom", [1.0, 1.0]), ease(tq / dur))
        pan = lerp(*cam.get("pan", [0.0, 0.0]), ease(tq / dur))
        pw, ph = plate.size
        ww, wh = self.w / z, self.h / z
        cx = pw / 2 + pan * (pw - ww) / 2 * 0.9
        cy = ph / 2 + (ph - wh) / 2 * 0.2
        view = plate.crop((int(cx - ww / 2), int(cy - wh / 2), int(cx + ww / 2), int(cy + wh / 2)))
        frame = view.resize((self.w, self.h), Image.BILINEAR).convert("RGBA")
        if shot.get("orb"):
            frame = fx.orb(frame, tq)
        actors = sorted(shot.get("actors", []), key=lambda a: -a.get("depth", 0.0))
        for a in actors:
            self._actor(frame, a, tl, dur, z, pan, night)
        return self.grade(np.asarray(frame.convert("RGB")), fi, night=night)

    def _actor(self, frame: Image.Image, a: dict, tl: float, dur: float, z: float, pan: float, night: bool) -> None:
        start = float(a.get("start", 0.0))
        if tl < start:
            return
        x0, x1 = a.get("x", [0.5, 0.5])
        moving_right = x1 > x0 + 0.02
        flip = bool(a.get("flip")) ^ moving_right
        aset = self.book.get(a["character"], a["action"], flip)
        n = int((tl - start) * TWOS * float(a.get("speed", 1.0)))
        imgs = aset.images
        im = imgs[n % len(imgs)] if a["action"] in sprites.LOOPING else imgs[min(n, len(imgs) - 1)]
        k = (tl - start) / max(0.01, dur - start)
        k = k if a["action"] == "walk" else ease(k)
        tq = math.floor(lerp(x0, x1, k) * 200) / 200
        depth = float(a.get("depth", 0.0))
        target_h = float(a.get("height", 0.42)) * self.h * z
        if aset.consistent:
            scale = target_h / aset.ref_h
            ax, ay = aset.anchor
        else:
            box = _body_box(im)
            scale = target_h / max(1, box[3] - box[1])
            ax, ay = (box[0] + box[2]) / 2, box[3]
        sw, sh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
        spr = im.resize((sw, sh), Image.LANCZOS)
        if depth > 0 or night:
            arr = np.asarray(spr).astype(np.float32)
            haze = np.array((236, 222, 240) if not night else (40, 40, 80), np.float32)
            kd = 0.28 * depth if not night else 0.35 + 0.2 * depth
            arr[..., :3] = arr[..., :3] * (1 - kd) + haze * kd
            spr = Image.fromarray(arr.astype(np.uint8), "RGBA")
        ground = self.h * float(a.get("ground", 0.86 - 0.16 * depth))
        sx = self.w / 2 + (tq * self.w - self.w / 2) * z - pan * self.w * 0.08
        frame.alpha_composite(spr, (int(sx - ax * scale), int(ground - ay * scale)))

    def _card(self, k: int, shot: dict, tl: float) -> np.ndarray:
        dur = float(shot["seconds"])
        if shot.get("end"):
            img = fx.end_card(self.w, self.h, tl, dur, text=self.text)
        elif self.text:
            big = shot.get("big") or (shot.get("chapter") or {}).get("name")  # a long video's chapter title card
            img = fx.card(self.w, self.h, big, shot.get("caption"), tl, dur, k)
        else:
            img = fx.card(self.w, self.h, None, None, tl, dur, k)
        return self.grade(np.asarray(img.convert("RGB")), int(tl * self.fps))

    # ------------------------------------------------------------------ frame
    def frame(self, fi: int) -> np.ndarray:
        t = fi / self.fps
        k = max(i for i, s0 in enumerate(self.starts) if s0 <= t + 1e-9)
        shot = self.sb["shots"][k]
        tl = t - self.starts[k]
        if k != self.last_k:
            self.prev_last, self.last_k = self.last_frame, k
            # a finished shot's decoder is never read again: stop it now (a 2-minute 1080p video has ~46 shots, and
            # 46 live ffmpeg decoders starved the 2-core runner until it lost contact, dry run 41909434990)
            for j in [j for j in self.readers if j < k]:
                self.readers.pop(j).close()
        kind = shot["kind"]
        if kind == "clip":
            f = self._clip(k, shot, tl, fi)
        elif kind == "scene":
            f = self._ai_or_scene(k, shot, tl, fi)
        else:
            f = self._card(k, shot, tl)
        fxs = set(shot.get("fx", []))
        local_frame = fi - rig.first_frame(self.starts[k], self.fps)   # 0 on the shot's first frame
        if "zoom_punch" in fxs or "impact" in fxs:
            f = self._zoom(f, 1 + (0.16 if "impact" in fxs else 0.2) * math.exp(-10 * tl))
        if "impact" in fxs and local_frame < 4:
            # one hard flash frame, then it falls away. The cold open (shot 0) keeps frame 0 clean (its first frame IS
            # the big, sharp monster: thumbnail, first impression) and flashes lightly on frames 1-2.
            f = fx.flash(f, ((0.0, 0.22, 0.1, 0.0) if k == 0 else (0.92, 0.6, 0.32, 0.12))[local_frame])
        if "flash" in fxs and local_frame < 5:
            f = fx.flash(f, 0.7 * (1 - local_frame / 5))
        if "impact" in fxs or "shake" in fxs:
            amp = (34 if "impact" in fxs else 12) * self.s
            dx, dy = fx.shake_offset(tl, k, amp, decay=4.0 if "impact" in fxs else 0.8)
            f = fx.apply_shake(f, dx, dy)
        f = self._transition(shot, local_frame, f)
        f = self._transition_out(k, fi, f)
        if self.sb.get("look") == "vhs":
            f = look.vhs(f, fi)
        if self.text:
            f = self._burn_text(shot, kind, tl, t, f)
        self.last_frame = f
        return f

    def _burn_text(self, shot: dict, kind: str, tl: float, t: float, f: np.ndarray) -> np.ndarray:
        """Burned-caption fallback: the storyboard's caption track (Shorts v2) or the per-shot captions (long video)."""
        track = self.sb.get("captions")
        img = None
        if track is not None:
            hook, tag = self.sb.get("hook") or {}, self.sb.get("end_tag") or {}
            passes = []
            if hook.get("title") and t < float(hook.get("seconds") or 2.0):
                passes.append((hook["title"], t, 0.17))                       # the hook, upper band
            ch = shot.get("chapter")
            if ch and tl < 1.5:
                passes.append((ch["name"], tl, 0.22))                         # a long video's chapter title
            cap = next((c for c in track if c["start"] <= t < c["end"]), None)
            if cap:
                passes.append((cap["text"], t - cap["start"], None))         # the caption, its usual band
            t_tag = self.total - float(tag.get("seconds") or 0)
            if tag and t >= t_tag:
                passes.append((tag.get("text") or "liminality: free on roblox", t - t_tag, 0.17))
            for text, tc, y in passes:
                img = img or Image.fromarray(f).convert("RGBA")
                img = fx.put_caption(img, text, tc, int(tc * TWOS), y=y)
            return f if img is None else np.asarray(img.convert("RGB"))
        text, tc = (shot.get("caption"), tl) if kind != "card" else (None, 0.0)
        if not text:
            return f
        img = Image.fromarray(f).convert("RGBA")
        return np.asarray(fx.put_caption(img, text, tc, int(tc * TWOS)).convert("RGB"))

    def _ai_or_scene(self, k: int, shot: dict, tl: float, fi: int) -> np.ndarray:
        if k in self.ai_clips:
            if k not in self.readers:
                self.readers[k] = _PathReader(self.ai_clips[k], self.w, self.h, self.fps)
            return self.grade(self.readers[k].next(), fi)
        # Scenes are drawn on twos: camera, drawings and grain all step every 2nd frame, so a pair of frames with the
        # same drawing, camera step and grain step is the same picture. Reuse it instead of painting it twice.
        # Long video only: the Shorts keep painting every frame (walkers there glide on ones and actors may start
        # between drawings), so their look is unchanged. A fast actor (speed > 1) is always painted every frame.
        if self.sb.get("kind") != "long" or any(float(a.get("speed", 1.0)) > 1.0 for a in shot.get("actors", [])):
            return self._scene(k, shot, tl, fi)
        key = (k, int(tl * TWOS), fi // 2)
        if self._twos is not None and self._twos[0] == key:
            return self._twos[1]
        f = self._scene(k, shot, tl, fi)
        self._twos = (key, f)
        return f

    def _transition(self, shot: dict, i: int, f: np.ndarray) -> np.ndarray:
        """How this shot comes in (bot/motion.py): fade, whip with real motion blur, flash cut, light-leak burn, dip
        from black, zoom-through, tape glitch. Uses the last frame of the shot before."""
        tr = shot.get("transition", "cut")
        if tr == "cut" or self.prev_last is None:
            # the first shot has nothing to come in from: no dip up out of black, no flash or burn on frame 0
            return f
        return motion.transition_in(tr, f, self.prev_last, i, self.seed + self.last_k)

    def _transition_out(self, k: int, fi: int, f: np.ndarray) -> np.ndarray:
        """The outgoing half of the NEXT cut over this shot's last frames (the whip's pan starting, the leak rising,
        the dip to black, the push into the zoom-through), so each cut is edited on both sides."""
        shots = self.sb["shots"]
        if k + 1 >= len(shots):
            return f
        nxt = shots[k + 1].get("transition", "cut")
        if motion.OUT_FRAMES.get(nxt, 0) == 0:
            return f
        left = rig.first_frame(self.starts[k + 1], self.fps) - fi
        return motion.transition_out(nxt, f, left, self.seed + k + 1)

    def close(self) -> None:
        for r in self.readers.values():
            r.close()

    # ------------------------------------------------------------------ output
    def cues(self) -> list[dict]:
        """The rig's cue sheet for this render: the overlay's (text=False: Remotion draws the text on these frames)
        or the burned-caption fallback's."""
        if self.text:
            return rig.burned_cues(self.sb, self.fps)
        from .overlay import props_from_storyboard  # local: overlay imports this module lazily too
        try:
            return props_from_storyboard(self.sb, self.w, self.h, self.fps)["cues"]
        except (KeyError, IndexError, TypeError, ValueError) as e:  # never lose the audio over the overlay's props
            log.warning("overlay cues unavailable (%s); using the burned-caption cues", e)
            return rig.burned_cues(self.sb, self.fps)

    def audio(self, wav: Path) -> Path:
        drop, resume, t = self.sb["music"].get("drop_at"), self.sb["music"].get("resume_at"), 0.0
        for shot in self.sb["shots"]:
            if drop is not None and resume is None and shot["kind"] == "card" and t > drop:
                resume = t
            t += float(shot["seconds"])
        cues = self.cues()
        hits = [c["f"] / self.fps for c in cues if c["kind"] == "impact"]  # the score's hit lands on the impact frame
        m = self.sb["music"]
        st = None
        if m.get("tracks"):   # real tracks (bot/music.py library_bed); the synth score is the fallback
            try:
                st = music.library_bed(self.total + 0.05, list(m["tracks"]), int(m["seed"]), drop, resume)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log.warning("music track unusable (%s); synth score instead", e)
        if st is None:
            st = music.score(self.total + 0.05, int(m["bpm"]), int(m["seed"]), m.get("mood", "soft"), hits, drop,
                             resume, bed=m.get("bed", "full"))
        if os.environ.get("BOT_SFX", "on") != "off":
            st = sfx.mix(st, cues, self.fps, int(m["seed"]), room=sfx.ROOM.get(self.look or "", 0.0))
        st = voice.mix(st, self.sb, music.SR)   # the narrator (bot/voice.py); unchanged when it is off
        return music.write_wav(wav, st)

    def render(self, out_mp4: Path, preset: str = "medium", crf: int = 18, bt709: bool | None = None,
               text_frames=None, x264: list[str] | None = None) -> Path:
        """Encode the whole timeline. The clean intermediate under the overlay may use a faster preset at a lower crf
        (it is encoded again by the overlay), so it costs fewer runner minutes without losing detail.

        text_frames (bot/overlay.py TextFrames, the long video): the overlay's transparent text frames are laid over
        the graded picture inside THIS encode, so the picture is encoded once (no clean intermediate, no compose
        pass). x264 replaces the -c:v / -preset / -crf arguments (the final encode's own settings)."""
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        # The long video is BT.709 from the start: its untouched frames sit next to the overlay's BT.709 text ranges,
        # so both must use the same matrix (no colour step at a range edge). Shorts keep their original untagged
        # encode (every frame of a Short goes through the overlay anyway).
        if bt709 is None:
            bt709 = self.sb.get("kind") == "long"
        tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
        if self.look:
            vf = grade.chain(self.look, self.w, self.h, bt709=bt709)
        else:
            vf = "scale=out_color_matrix=bt709:out_range=tv" if bt709 else ""
        video_map, text_inputs = ["-map", "0:v:0"], []
        if text_frames is not None:
            # the grade (grain included) first, then the text over it: the same order as clean video + compose had
            text_inputs, chain, last = text_frames.graph(2, "base")
            colour = ["-filter_complex", f"[0:v]{vf or 'null'},format=yuv420p[base];" + ";".join(chain)]
            colour += tags if bt709 else []
            video_map = ["-map", f"[{last}]"]
        else:
            colour = (["-vf", vf] if vf else []) + (tags if bt709 and vf else [])
        enc_args = x264 or ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
        wav = self.audio(out_mp4.with_suffix(".wav"))
        aac = encode_audio(wav, out_mp4.with_suffix(".m4a"))
        cmd = [config.ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{self.w}x{self.h}", "-r", str(self.fps), "-i", "-", "-i", str(aac), *text_inputs,
               *colour, *enc_args, "-pix_fmt", "yuv420p",
               *video_map, "-map", "1:a:0", "-c:a", "copy",
               "-shortest", "-movflags", "+faststart", str(out_mp4)]
        err_log = tempfile.TemporaryFile()  # a file, not a pipe: ffmpeg's stderr can never fill up and block
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err_log)
        sheet: list[Image.Image] = []
        mids = {int((s0 + float(sh["seconds"]) * 0.6) * self.fps) for s0, sh in zip(self.starts, self.sb["shots"])}
        broken = False
        try:
            for fi in range(self.n):
                f = self.frame(fi)
                try:
                    enc.stdin.write(np.ascontiguousarray(f, np.uint8).tobytes())
                except (BrokenPipeError, OSError):
                    broken = True  # ffmpeg died: stop feeding it and report ITS error below
                    break
                if fi in mids:
                    sheet.append(Image.fromarray(f).resize((self.w // 3, self.h // 3), Image.LANCZOS))
                if fi % (self.fps * 4) == 0:
                    log.info("  frame %d/%d", fi, self.n)
        finally:
            self.close()
            try:
                enc.stdin.close()
            except (BrokenPipeError, OSError):
                broken = True
            rc = enc.wait()
            err_log.seek(0)
            err = err_log.read().decode(errors="replace").strip()[-1500:]
            err_log.close()
        if rc != 0 or broken:
            raise RuntimeError(f"ffmpeg encode failed (rc={rc}): {err or 'no error output'}")
        wav.unlink(missing_ok=True)
        aac.unlink(missing_ok=True)
        _contact(sheet, out_mp4.with_name(out_mp4.stem + "_sheet.jpg"))
        return out_mp4


TRUE_PEAK = -2.0  # dBTP before AAC: the encoder overshoots ~0.5-1 dB, so the file stays under -1 dBTP
MAX_TRUE_PEAK = -1.2  # dBTP the encoded AAC may reach (YouTube / the owner's bar: <= -1); over it, encode again lower


def encode_audio(wav: Path, out: Path, tries: int = 3) -> Path:
    """The mix -> loudness-normalised AAC (192k, 48 kHz), measured after encoding: an AAC (or limiter) overshoot past
    MAX_TRUE_PEAK is encoded again with the limiter that much lower (loudnorm keeps -14 LUFS), so the file itself
    stays under -1 dBTP whatever ffmpeg version made it."""
    tp = TRUE_PEAK
    for _ in range(tries):
        p = subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(wav), "-af",
                            loudnorm_filter(wav, tp), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)],
                           capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise RuntimeError(f"audio encode failed: {p.stderr.strip()[-500:]}")
        peak = true_peak(out)
        if peak is None or peak <= MAX_TRUE_PEAK:
            return out
        log.info("audio true peak %.2f dBTP after AAC; encoding again with the limiter at %.2f", peak,
                 tp - (peak - MAX_TRUE_PEAK) - 0.2)
        tp -= (peak - MAX_TRUE_PEAK) + 0.2
    log.warning("audio true peak still %.2f dBTP after %d encodes", peak, tries)
    return out


def true_peak(path: Path) -> float | None:
    """The file's true peak in dBTP (ffmpeg ebur128), None when it cannot be read."""
    p = subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af",
                        "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    try:
        summary = p.stderr[p.stderr.rindex("Summary:"):]
        v = float(summary.split("True peak:")[1].split("Peak:")[1].split("dBFS")[0])
    except (ValueError, IndexError):
        return None
    return v if math.isfinite(v) else None


def loudnorm_filter(wav: Path, tp: float = TRUE_PEAK) -> str:
    """Two-pass loudnorm: measure the mix (audio only, about a second), then normalise LINEARLY to LUFS (one gain,
    no pumping; the true-peak limiter only touches the loudest hits). Falls back to the one-pass dynamic filter
    when the measurement cannot be read. (loudnorm itself switches to its dynamic mode when the mix's peaks sit too
    far above its loudness for one gain to reach -14 LUFS under `tp`: measured on the Shorts and the long video.)"""
    base = f"loudnorm=I={LUFS}:TP={tp:.1f}:LRA=11"
    p = subprocess.run([config.ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(wav), "-af",
                        base + ":print_format=json", "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    try:
        js = p.stderr[p.stderr.rindex("{"):p.stderr.rindex("}") + 1]
        m = json.loads(js)
        vals = {k: float(m[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError):
        log.warning("loudnorm measurement unreadable; one-pass loudnorm")
        return base
    if not all(math.isfinite(v) for v in vals.values()):
        return base
    return (f"{base}:measured_I={vals['input_i']:.2f}:measured_TP={vals['input_tp']:.2f}"
            f":measured_LRA={vals['input_lra']:.2f}:measured_thresh={vals['input_thresh']:.2f}"
            f":offset={vals['target_offset']:.2f}:linear=true")


def _identity(f: np.ndarray, i: int = 0, night: bool = False) -> np.ndarray:
    return f


class _PathReader:
    """Reads an arbitrary mp4 (the optional AI clip) scaled to cover the vertical frame."""

    def __init__(self, path: Path, w: int, h: int, fps: int):
        vf = f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        self.size, self.shape, self.last = w * h * 3, (h, w, 3), None
        # a downloaded file: force the mp4 demuxer and local-file protocol only (no network via crafted files)
        self.p = subprocess.Popen([config.ffmpeg_exe(), "-loglevel", "error", "-protocol_whitelist", "file",
                                   "-f", "mp4", "-i", str(Path(path).resolve()), "-vf", vf, "-an",
                                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)

    def next(self) -> np.ndarray:
        buf = self.p.stdout.read(self.size)
        if len(buf) == self.size:
            self.last = np.frombuffer(buf, np.uint8).reshape(self.shape)
        if self.last is None:
            raise RuntimeError("AI clip produced no frames")
        return self.last

    def close(self) -> None:
        self.p.kill()
        self.p.wait()


def _contact(frames: list[Image.Image], path: Path) -> None:
    if not frames:
        return
    cols = min(4, len(frames))
    rows = math.ceil(len(frames) / cols)
    fw, fh = frames[0].size
    sheet = Image.new("RGB", (cols * fw, rows * fh), (30, 22, 40))
    for i, f in enumerate(frames):
        sheet.paste(f, ((i % cols) * fw, (i // cols) * fh))
    sheet.save(path, quality=88)



AV_SLACK = 0.04   # audio may outlast the picture by this much (the QA gates allow 0.05 s)


def _stream_seconds(path: Path) -> dict[str, float]:
    out = subprocess.run([config.ffprobe_exe(), "-v", "error", "-show_entries", "stream=codec_type,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[-300:]}")
    streams = json.loads(out.stdout).get("streams", [])
    return {s["codec_type"]: float(s["duration"]) for s in streams if s.get("duration") not in (None, "N/A")}


def fit_audio_to_video(path: Path) -> Path:
    """End the audio with the picture. `-shortest` with stream copy can leave the AAC running past the last frame
    (measured 2026-11-10: 115 ms on a Short). A stream-copy re-mux with the picture's exact length fixes it without
    re-encoding anything. Leaves a file that already fits untouched."""
    try:
        d = _stream_seconds(path)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        return path                                  # unreadable: QA rejects it with the real reason
    if "video" not in d or "audio" not in d or d["audio"] - d["video"] <= AV_SLACK:
        return path
    tmp = path.with_name(path.stem + ".fit.tmp.mp4")
    try:
        p = subprocess.run([config.ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(path), "-map", "0", "-c", "copy",
                            "-t", f"{d['video']:.6f}", "-movflags", "+faststart", str(tmp)],
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(f"audio trim failed: {p.stderr.strip()[-300:]}")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("audio trimmed to the picture: %.3fs -> %.3fs", d["audio"], d["video"])
    return path
