"""Render liminality cast pose sprites headless (Blender 5.x, Cycles CPU) for the animated-short compositor.

    blender -b --factory-startup -P blender/render_sprites.py -- <request.json> <out dir>

request.json (written by bot/sprites.py):
    {"res": 720, "samples": 24,
     "frames": [{"character": "Dreamer", "pose": "walk_contact", "weight": -1.0, "file": "Dreamer_walk_contact_-1.00.png"}]}

Why import the cast *script* instead of only opening assets/liminality_cast.blend:
the pose library (POSES, apply_pose, the toon node group helpers) lives in Python, not in the .blend. Importing
blender/liminality_cast.py rebuilds the identical cast procedurally (same code that wrote the .blend), so the pose
API, rig names and toon look can never drift from each other. The .blend ships alongside as the reference asset.

Camera: one fixed framing per character, computed from the union of all its requested poses, so a walk cycle does
not "breathe" from per-frame re-framing. Background is transparent (film_transparent); the compositor paints the
dream behind it. EEVEE needs OpenGL, which GitHub runners don't have, so we force Cycles CPU.
"""
import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(ARGS) < 2:
    raise SystemExit("usage: blender -b -P render_sprites.py -- <request.json> <out dir>")
REQ_PATH, OUT_DIR = ARGS[0], ARGS[1]
with open(REQ_PATH, encoding="utf-8") as fh:
    REQ = json.load(fh)
os.makedirs(OUT_DIR, exist_ok=True)

# liminality_cast reads sys.argv at import: give it a scratch output folder, Cycles, fast samples.
_scratch = tempfile.mkdtemp(prefix="cast_")
sys.argv = [sys.argv[0], "--", _scratch, "--engine", "CYCLES", "--fast"]
sys.path.insert(0, HERE)
import bpy  # noqa: E402
import liminality_cast as cast  # noqa: E402  (builds the cast on import)
from mathutils import Vector  # noqa: E402

scene = bpy.context.scene
res = int(REQ.get("res", 720))
scene.cycles.samples = int(REQ.get("samples", 24))
scene.cycles.use_denoising = False
scene.render.resolution_x = scene.render.resolution_y = res
scene.render.line_thickness = 2.4 * res / 1080
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
try:
    scene.cycles.threads_mode = "AUTO"
except AttributeError:
    pass

by_char = {}
for fr in REQ["frames"]:
    by_char.setdefault(fr["character"], []).append(fr)

done = 0
for name, frames in by_char.items():
    rig, body = next((r, b) for n, r, b in cast.CAST if n == name)
    for n, _, b in cast.CAST:
        b.hide_render = n != name
    cast.STOOL.hide_render = True
    # union of bounds over every requested pose -> one stable camera
    lo, hi = Vector((1e9, 1e9, 1e9)), Vector((-1e9, -1e9, -1e9))
    for fr in frames:
        cast.apply_pose(rig, fr["pose"], fr.get("weight", 1.0))
        mn, mx = cast.world_bounds(body)
        lo = Vector((min(lo.x, mn.x), min(lo.y, mn.y), min(lo.z, mn.z)))
        hi = Vector((max(hi.x, mx.x), max(hi.y, mx.y), max(hi.z, mx.z)))
    c = (lo + hi) / 2
    h, w = hi.z - lo.z, max(hi.x - lo.x, hi.y - lo.y)
    view = Vector(cast.VIEW.get(name, (0.55, -1.0, 0.1))).normalized()
    radius = max(h, w * 1.1) * 0.5 * 1.10
    cast.cam_o.location = c + view * (radius / math.tan(cast.cam.angle / 2))
    cast.look_at(cast.cam_o, c)
    for fr in frames:
        spec = cast.apply_pose(rig, fr["pose"], fr.get("weight", 1.0))
        cast.STOOL.hide_render = True
        if spec.get("_seat"):
            bpy.context.view_layer.update()
            hip = rig.matrix_world @ rig.pose.bones["LeftUpperLeg"].head
            seat_top = max(0.3, hip.z - 0.22)
            cast.STOOL.scale = ((abs(hip.x - rig.location.x) * 2 + 0.6) / 1.1, 1.2, seat_top)
            cast.STOOL.location = (rig.location.x, hip.y + 0.25, 0)
            cast.STOOL.hide_render = False
        scene.render.filepath = os.path.join(OUT_DIR, fr["file"])
        bpy.ops.render.render(write_still=True)
        done += 1
        print(f"[sprites] {done}/{len(REQ['frames'])} {fr['file']}", flush=True)
    cast.reset_pose(rig)

print("SPRITES DONE", done)
