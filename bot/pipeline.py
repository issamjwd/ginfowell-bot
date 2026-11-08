"""Making an episode: plan -> storyboard -> sprites -> render/edit -> QA -> thumbnail, into out/<episode>/.

Never touches YouTube and never needs YouTube secrets. Claiming and uploading live in bot/publish.py (stdlib only),
which runs in separate workflow jobs.
"""
from __future__ import annotations

import json
import logging
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import (ai_video, config, footage, longform, music, overlay, palette, planner, qa, sprites, storyboard, thumbs, topic,
               world, writer_claude)
from .render import Renderer
from .state import Ledger

log = logging.getLogger(__name__)
CURRENT = config.OUT / "current.json"


@dataclass
class Made:
    episode_id: str
    folder: Path
    video: Path
    thumb: Path
    storyboard: dict
    qa: qa.Report


def make(settings: config.Settings, ledger: Ledger, seed: int | None = None, out_root: Path | None = None) -> Made:
    out_root = out_root or config.OUT
    clips = footage.available()
    history, used = ledger.history(), ledger.used_fingerprints()
    long_ = settings.kind == "long"
    if not long_ and topic.choose(history):
        made = _world(settings, history, used, seed, out_root)
        if made:
            return made
    if long_:
        sb = longform.plan_long(history, used, clips, seed=seed)
    else:
        pick, sb = planner.plan(history, used, settings.fmt, clips, seed=seed, template=settings.template or None)
        sb = writer_claude.write(pick, sb, random.Random(pick.seed))
    log.info("planned %s: %s %s %s", sb["episode_id"], sb["template"], sb["dream_ids"], sb.get("monster"))
    music.assign(sb, history, int(sb["music"]["seed"]), n=6 if long_ else 1)   # a real track, not a recent one
    sb["palette"] = palette.pick(history, int(sb["music"]["seed"]))             # a new accent colour each video
    footage.save_profiles()   # clips measured while planning are kept for the next runs (BOT_PROFILE_CACHE)

    if long_:
        w, h = (960, 540) if settings.low_res else (config.LONG_W, config.LONG_H)
    else:
        w, h = (540, 960) if settings.low_res else (config.W, config.H)
    sprite_dir = None
    if sb["format"] == "animated" or long_:  # the long video's drawn scenes use the cast too
        sprite_dir = sprites.ensure_sprites(settings.blender, res=512 if settings.low_res else 1024,
                                            samples=16 if settings.low_res else 24)
    folder = out_root / sb["episode_id"]
    folder.mkdir(parents=True, exist_ok=True)

    ai_clips = {}
    if sb["format"] == "animated" and not long_ and ai_video.enabled():
        first = Renderer(sb, w, h, config.FPS, sprite_dir)
        img = Image.fromarray(first.frame(0))
        d = sb["dream_ids"][0]
        clip = ai_video.generate(img, f"hand-drawn anime style, soft pastel dreamcore, {d} from the game liminality, "
                                      "a small child in pink pajamas, gentle camera drift, no text",
                                 sb["shots"][0]["seconds"], folder / "ai_shot0.mp4")
        if clip:
            ai_clips[0] = clip
            sb["synthetic"] = True
            sb["description"] += "\nsome shots in this video were made with AI video tools."
            storyboard.validate(sb)

    video, report = render_video(sb, folder, w, h, sprite_dir, ai_clips, settings.overlay)
    (folder / "qa.txt").write_text(report.as_text() + f"\nINFO  text: {report.info['text']}\n"
                                   + json.dumps(report.info) + "\n", encoding="utf-8")
    thumb = thumbs.make(sb, folder / "thumb.jpg", sprite_dir)
    (folder / "storyboard.json").write_text(json.dumps(sb, indent=2), encoding="utf-8")
    made = Made(sb["episode_id"], folder, video, thumb, sb, report)
    if not report.ok:
        raise RuntimeError(f"QA failed for {sb['episode_id']}:\n{report.as_text()}")
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "current.json").write_text(json.dumps({"episode_id": made.episode_id}), encoding="utf-8")
    return made


def _world(settings: config.Settings, history: list[dict], used: set[str], seed: int | None,
           out_root: Path) -> Made | None:
    """A world Short (bot/world.py), or None (logged) so the run makes a liminality Short instead."""
    try:
        sb, folder, video, thumb, report = world.make(settings, history, used, seed, out_root)
    except world.WorldError as e:
        log.warning("world Short not made (%s); making a liminality Short instead", e)
        return None
    if not report.ok:
        log.warning("world Short failed QA (%s); making a liminality Short instead", report.as_text())
        return None
    (out_root / "current.json").write_text(json.dumps({"episode_id": sb["episode_id"]}), encoding="utf-8")
    return Made(sb["episode_id"], folder, video, thumb, sb, report)


def _qa(video: Path, sb: dict, w: int, h: int) -> qa.Report:
    if sb.get("kind") == "long":
        return qa.check(video, w, h, max_seconds=config.LONG_MAX_SECONDS, min_seconds=config.LONG_MIN_SECONDS)
    return qa.check(video, w, h)


def _single_pass(sb: dict, folder: Path, video: Path, w: int, h: int, sprite_dir: Path | None,
                 ai_clips: dict) -> qa.Report:
    """The long video in ONE encode: Remotion renders the text ranges as transparent frames first (no picture
    needed), then the renderer's frames are graded, get the text laid over them and are encoded once, straight to
    video.mp4. The old way encoded a clean video (crf 15, film grain: the slowest step), decoded it again and
    encoded it a second time; measured on the 2-core runner that was ~9 of the job's ~18 minutes. Raises on any
    overlay problem (the caller falls back to burned captions)."""
    t0 = time.perf_counter()
    frames = overlay.render_text_frames(sb, w, h, config.FPS)
    try:
        t1 = time.perf_counter()
        Renderer(sb, w, h, config.FPS, sprite_dir, ai_clips, text=False).render(
            video, text_frames=frames, x264=overlay.FINAL_X264)
        t2 = time.perf_counter()
        log.info("timing: single-pass render + encode %.0fs (text frames %.0fs)", t2 - t1, t1 - t0)
    finally:
        frames.close()
    want = sum(float(s["seconds"]) for s in sb["shots"])
    got = qa._probe(video)["seconds"]
    if abs(want - got) > overlay.LENGTH_SLACK + 1 / config.FPS:
        raise overlay.OverlayError(f"single-pass video is {got:.2f}s, the storyboard {want:.2f}s")
    # QA and the review contact sheet share ONE decode of the finished video (each used to decode it on its own)
    want_frames = overlay.sheet_frames(sb, config.FPS)
    with tempfile.TemporaryDirectory(prefix="sheet-") as td:
        report = qa.check(video, w, h, max_seconds=config.LONG_MAX_SECONDS, min_seconds=config.LONG_MIN_SECONDS,
                          grab=(want_frames, w // 3, Path(td)))
        log.info("timing: QA + contact sheet frames %.0fs", time.perf_counter() - t2)
        if report.ok:
            try:
                overlay.sheet_from_folder(want_frames, Path(td), folder / "video_sheet.jpg")
            except Exception as e:  # noqa: BLE001 - the sheet is a preview; never lose a good video over it
                log.warning("contact sheet skipped: %s", e)
        report.info["text"] = "remotion overlay"
    return report


def render_video(sb: dict, folder: Path, w: int, h: int, sprite_dir: Path | None, ai_clips: dict,
                 overlay_mode: str = "auto") -> tuple[Path, qa.Report]:
    """video.mp4 + its QA report. Remotion overlay on a clean render when possible; on ANY failure (or a failed QA)
    the original burned-caption render instead, so the episode still ships. report.info['text'] says which."""
    video = folder / "video.mp4"
    ok, why = overlay.available(overlay_mode)
    if ok and overlay.single_pass_ok(sb, w, h, config.FPS):
        try:
            report = _single_pass(sb, folder, video, w, h, sprite_dir, ai_clips)
            if report.ok:
                return video, report
            why = "single-pass video failed QA: " + "; ".join(f"{k} {v}" for k, v in report.checks.items() if v != "ok")
        except Exception as e:  # noqa: BLE001 - any overlay problem must fall back, never lose the episode
            why = f"{type(e).__name__}: {e}"
        log.warning("Remotion overlay not used (%s); falling back to burned captions", why[:600])
        video.unlink(missing_ok=True)
        ok = False
    if ok:
        clean = folder / "clean.mp4"
        try:
            # the clean video is an intermediate (encoded again by the overlay): a faster preset at a lower crf keeps
            # its detail and saves runner minutes
            t0 = time.perf_counter()
            Renderer(sb, w, h, config.FPS, sprite_dir, ai_clips, text=False).render(clean, preset="veryfast", crf=15)
            t1 = time.perf_counter()
            log.info("timing: clean render %.0fs", t1 - t0)
            overlay.render(clean, sb, video, w, h, config.FPS)
            t2 = time.perf_counter()
            log.info("timing: overlay (frames + compose) %.0fs", t2 - t1)
            report = _qa(video, sb, w, h)
            log.info("timing: QA %.0fs", time.perf_counter() - t2)
            if report.ok:
                try:
                    t3 = time.perf_counter()
                    overlay.contact_sheet(video, sb, folder / "video_sheet.jpg", w)
                    log.info("timing: contact sheet %.0fs", time.perf_counter() - t3)
                except Exception as e:  # noqa: BLE001 - the sheet is a preview; never lose a good video over it
                    log.warning("contact sheet skipped: %s", e)
                report.info["text"] = "remotion overlay"
                return video, report
            why = "overlay video failed QA: " + "; ".join(f"{k} {v}" for k, v in report.checks.items() if v != "ok")
        except Exception as e:  # noqa: BLE001 - any overlay problem must fall back, never lose the episode
            why = f"{type(e).__name__}: {e}"
        finally:
            clean.unlink(missing_ok=True)
            (folder / "clean_sheet.jpg").unlink(missing_ok=True)
        log.warning("Remotion overlay not used (%s); falling back to burned captions", why[:600])
        video.unlink(missing_ok=True)
    video = Renderer(sb, w, h, config.FPS, sprite_dir, ai_clips, text=True).render(
        video, preset="faster" if sb.get("kind") == "long" else "medium")
    report = _qa(video, sb, w, h)
    report.info["text"] = f"burned captions (overlay: {why[:300]})"
    return video, report
