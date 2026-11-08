"""liminality cast for Blender (5.x): the dreamer + three monsters, rigged, toon-shaded ("drawn" look).

Each character is ONE mesh skinned to an R15-style armature (Root, LowerTorso, UpperTorso, Head, Left/Right
UpperArm/LowerArm/Hand, Left/Right UpperLeg/LowerLeg/Foot). Parts are rigid (weight 1.0 to one bone) except the
places that must bend smoothly: torsos blend Lower->UpperTorso over the waist, the Neighbour's long coat blends into
the thighs so the skirt follows the legs. Limb ends are spheres centred on the joint pivot, so rotations never gap.

Shapes are made procedurally (no primitives-with-bevels): swept profiles along spline paths, superellipsoid blobs,
hairline-masked hair caps, and surface-conforming decals/brush strokes (eyes, mouths, stitches) projected by ray cast.

Shading (engine-agnostic, works in EEVEE *and* Cycles CPU for headless CI):
  one shared node group "LiminalityToon": world normal . key-light vector -> hard step -> base / shadow-tint colour
  -> Emission. No Shader-to-RGB, so Cycles renders the same picture. Trade-off: characters get no cast self-shadows
  (the floor still catches them: EEVEE via a Shader-to-RGB floor, Cycles via is_shadow_catcher). Freestyle ink on top.
Roblox export: every material also maps (via UVs) into one shared 128x4 palette PNG embedded in the FBX, and the mesh
carries a "Col" vertex-colour attribute, so colours survive the Roblox importer (it ignores emission node trees).
FBX: Y-up, face toward -Z (Roblox LookVector), 1 Blender unit = 1 FBX unit (cm) = 1 stud.

usage (no UI):
  blender -b -P tools/blender/liminality_cast.py -- <out folder> [--engine CYCLES|BLENDER_EEVEE] [--fast]
writes <out>/liminality_cast.blend, <out>/<Name>.fbx, <out>/preview_<Name>.png, <out>/poses/<Name>_<pose>.png,
<out>/cast_sheet.png and <out>/liminality_palette.png

pose library (for the cloud video pipeline):
  apply_pose(rig, "wave", weight=1.0)   # idle, walk_contact, wave, peek, reach, sit
  key_pose(rig, "walk_contact", frame=12)
Pose angles are written in world axes of the rest pose (character faces -Y, +X is the character's left), so the
same numbers work for every rig regardless of bone roll.
"""
import math
import os
import sys
import tempfile

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = next((a for a in ARGS if not a.startswith("--") and a not in ("CYCLES", "BLENDER_EEVEE")),
           os.path.join(os.path.expanduser("~"), "Desktop", "liminality Models"))
ENGINE = ARGS[ARGS.index("--engine") + 1] if "--engine" in ARGS else "BLENDER_EEVEE"
FAST = "--fast" in ARGS
os.makedirs(OUT, exist_ok=True)


# --- colour helpers ----------------------------------------------------------------------------------------------
def _lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def C(r, g, b):
    """sRGB 0-255 as it should APPEAR on screen (Standard view transform) -> linear RGBA for nodes."""
    return (_lin(r / 255), _lin(g / 255), _lin(b / 255), 1.0)


def smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def spow(x, e):
    return math.copysign(abs(x) ** e, x)


def _herm(ts, vs, t):
    """Cubic Hermite through (ts, vs) with finite-difference tangents. vs may be floats or Vectors."""
    n = len(ts)
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    i = 0
    while i < n - 2 and ts[i + 1] < t:
        i += 1

    def tan(k):
        if k == 0:
            return (vs[1] - vs[0]) / (ts[1] - ts[0])
        if k == n - 1:
            return (vs[-1] - vs[-2]) / (ts[-1] - ts[-2])
        return (vs[k + 1] - vs[k - 1]) / (ts[k + 1] - ts[k - 1])

    h = ts[i + 1] - ts[i]
    s = (t - ts[i]) / h
    return ((2 * s ** 3 - 3 * s ** 2 + 1) * vs[i] + (s ** 3 - 2 * s ** 2 + s) * h * tan(i)
            + (-2 * s ** 3 + 3 * s ** 2) * vs[i + 1] + (s ** 3 - s ** 2) * h * tan(i + 1))


# --- scene -------------------------------------------------------------------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = ENGINE
scene.render.resolution_x = scene.render.resolution_y = 1080
scene.render.film_transparent = True  # previews are cut-outs; the video pipeline paints its own backdrop
scene.render.use_freestyle = True
scene.render.line_thickness_mode = "ABSOLUTE"
scene.render.line_thickness = 2.4
try:
    scene.view_settings.view_transform = "Standard"
except TypeError:
    pass
if ENGINE == "CYCLES":
    scene.cycles.device = "CPU"
    scene.cycles.samples = 24 if FAST else 48
    scene.cycles.use_denoising = False
else:
    scene.eevee.taa_render_samples = 16 if FAST else 48
world = bpy.data.worlds.new("Dream")
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = C(255, 236, 246)
world.node_tree.nodes["Background"].inputs[1].default_value = 0.0  # toon look is self-lit; world adds nothing

NOINK = bpy.data.collections.new("NoInk")  # floor etc.: never outlined
scene.collection.children.link(NOINK)
scene.view_layers[0].use_freestyle = True
fs = scene.view_layers[0].freestyle_settings
fs.crease_angle = math.radians(128)
if not fs.linesets:
    fs.linesets.new("Outline")
INKSTYLE = bpy.data.linestyles.new("Ink")
INKSTYLE.color = (0.2, 0.12, 0.28)
INKSTYLE.thickness = 2.4
INKSTYLE.chaining = "PLAIN"
for ls in fs.linesets:  # turning freestyle on can make a default set with no style; that breaks the line pass
    ls.select_by_visibility = True
    ls.select_by_edge_types = True
    ls.select_silhouette, ls.select_border, ls.select_crease = True, False, True  # border off: decals stay line-free
    ls.select_by_collection = True
    ls.collection = NOINK
    ls.collection_negation = "EXCLUSIVE"
    ls.linestyle = INKSTYLE

# key light: a sun. Its direction also drives the toon group (normal . light), so move it with set_key_light().
sun = bpy.data.lights.new("Key", "SUN")
sun.energy = 3.0
sun.angle = math.radians(4)
sun_o = bpy.data.objects.new("Key", sun)
scene.collection.objects.link(sun_o)

# --- toon material -----------------------------------------------------------------------------------------------
PALETTE_SLOTS = 32
MATS = {}  # name -> (material, srgb tuple 0-1, palette index)


def _toon_group():
    g = bpy.data.node_groups.get("LiminalityToon")
    if g:
        return g
    g = bpy.data.node_groups.new("LiminalityToon", "ShaderNodeTree")
    g.interface.new_socket("Color", in_out="INPUT", socket_type="NodeSocketColor")
    g.interface.new_socket("Shadow", in_out="INPUT", socket_type="NodeSocketColor")
    g.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    n, ln = g.nodes, g.links
    gi, go = n.new("NodeGroupInput"), n.new("NodeGroupOutput")
    geo = n.new("ShaderNodeNewGeometry")
    key = n.new("ShaderNodeCombineXYZ")
    key.name = "KeyDir"
    dot = n.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    step = n.new("ShaderNodeMapRange")  # hard cel step with a hair of softness so edges anti-alias
    step.inputs["From Min"].default_value = 0.02
    step.inputs["From Max"].default_value = 0.06
    step.clamp = True
    shade = n.new("ShaderNodeMix")
    shade.data_type = "RGBA"
    shade.blend_type = "MULTIPLY"
    shade.inputs[0].default_value = 1.0
    mix = n.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    ln.new(geo.outputs["Normal"], dot.inputs[0])
    ln.new(key.outputs[0], dot.inputs[1])
    ln.new(dot.outputs["Value"], step.inputs["Value"])
    ln.new(gi.outputs[0], shade.inputs[6])
    ln.new(gi.outputs[1], shade.inputs[7])
    ln.new(step.outputs[0], mix.inputs[0])
    ln.new(shade.outputs[2], mix.inputs[6])
    ln.new(gi.outputs[0], mix.inputs[7])
    ln.new(mix.outputs[2], go.inputs[0])
    return g


def set_key_light(rot_deg):
    """Point the sun and the toon ramp the same way (rotation in degrees, XYZ)."""
    sun_o.rotation_euler = Euler([math.radians(a) for a in rot_deg])
    d = sun_o.rotation_euler.to_matrix() @ Vector((0, 0, 1))  # direction TOWARD the light
    k = _toon_group().nodes["KeyDir"]
    k.inputs[0].default_value, k.inputs[1].default_value, k.inputs[2].default_value = d


def _build_toon_nodes(m, color, shadow):
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    grp = nt.nodes.new("ShaderNodeGroup")
    grp.node_tree = _toon_group()
    grp.inputs[0].default_value = color
    grp.inputs[1].default_value = shadow
    emit = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(grp.outputs[0], emit.inputs[0])
    nt.links.new(emit.outputs[0], out.inputs[0])


def toon(name, rgb, shadow=(176, 158, 214)):
    """rgb: 0-255 sRGB as it should look lit. shadow: multiply tint for the dark side (lavender like the game ink)."""
    if name in MATS:
        return MATS[name][0]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    col, sh = C(*rgb), C(*shadow)
    _build_toon_nodes(m, col, sh)
    m["toon_color"], m["toon_shadow"] = list(col), list(sh)
    m.diffuse_color = col
    MATS[name] = (m, tuple(v / 255 for v in rgb), len(MATS))
    return m


def palette_image():
    """One shared swatch strip; every material points its UVs at its own swatch (Roblox-safe colour)."""
    img = bpy.data.images.get("liminality_palette") or bpy.data.images.new("liminality_palette", PALETTE_SLOTS * 4, 4)
    px = [0.0] * (PALETTE_SLOTS * 4 * 4 * 4)
    for _, srgb, idx in MATS.values():
        for y in range(4):
            for x in range(idx * 4, idx * 4 + 4):
                o = (y * PALETTE_SLOTS * 4 + x) * 4
                px[o:o + 4] = [srgb[0], srgb[1], srgb[2], 1.0]
    img.pixels = px
    img.filepath_raw = os.path.join(OUT, "liminality_palette.png")
    img.file_format = "PNG"
    img.save()
    return img


def materials_for_export(on):
    """Swap every material between the toon tree (render) and a palette-textured Principled BSDF (FBX/Roblox)."""
    img = palette_image() if on else None
    for m, srgb, _ in MATS.values():
        if on:
            nt = m.node_tree
            nt.nodes.clear()
            out = nt.nodes.new("ShaderNodeOutputMaterial")
            bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = img
            tex.interpolation = "Closest"
            bsdf.inputs["Roughness"].default_value = 1.0
            for k in ("Specular IOR Level", "Specular"):
                if k in bsdf.inputs:
                    bsdf.inputs[k].default_value = 0.0
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            nt.links.new(bsdf.outputs[0], out.inputs[0])
        else:
            _build_toon_nodes(m, tuple(m["toon_color"]), tuple(m["toon_shadow"]))


# --- geometry generators (return (verts, faces)) -------------------------------------------------------------------
def ring_mesh(rings, pole0=None, pole1=None):
    seg = len(rings[0])
    verts, faces = [v for r in rings for v in r], []
    for i in range(len(rings) - 1):
        a, b = i * seg, (i + 1) * seg
        for j in range(seg):
            j2 = (j + 1) % seg
            faces.append((a + j, a + j2, b + j2, b + j))
    if pole0 is not None:
        verts.append(pole0)
        p = len(verts) - 1
        faces += [(p, (j + 1) % seg, j) for j in range(seg)]
    if pole1 is not None:
        verts.append(pole1)
        p, base = len(verts) - 1, (len(rings) - 1) * seg
        faces += [(p, base + j, base + (j + 1) % seg) for j in range(seg)]
    return verts, faces


def sweep(ctrl, prof, seg=16, rows=8, sx=1.0, sy=1.0, ends=("round", "round"), endk=(1.0, 1.0), endrows=4,
          shape=None, ref=(0, -1, 0)):
    """Sweep an elliptical section along a spline through ctrl. prof: [(t, radius)] smooth radius profile.
    sx scales the section sideways, sy front/back. ends: round (dome, depth radius*endk), flat (cap) or none.
    shape(t, angle) -> radius multiplier (angle 90deg = the front side)."""
    ctrl = [Vector(c) for c in ctrl]
    d = [0.0]
    for a, b in zip(ctrl, ctrl[1:]):
        d.append(d[-1] + (b - a).length)
    ts = [x / d[-1] for x in d]
    pt, pr = [p[0] for p in prof], [float(p[1]) for p in prof]

    def P(t):
        return _herm(ts, ctrl, t)

    def T(t):
        e = 1e-3
        return (P(min(1.0, t + e)) - P(max(0.0, t - e))).normalized()

    refv, frames, vprev = Vector(ref), [], None
    for i in range(rows + 1):
        t = i / rows
        tt = T(t)
        v = (refv if vprev is None else vprev)
        v = v - tt * v.dot(tt)
        if v.length < 1e-4:
            v = Vector((0, 0, 1)) - tt * tt.z
        vv = v.normalized()
        frames.append((P(t), tt, tt.cross(vv), vv, max(_herm(pt, pr, t), 0.0), t))
        vprev = vv

    def ring(c, u, v, r, t):
        out = []
        for j in range(seg):
            a = 2 * math.pi * j / seg
            k = shape(t, a) if shape else 1.0
            out.append(c + u * (math.cos(a) * r * sx * k) + v * (math.sin(a) * r * sy * k))
        return out

    rings = [ring(c, u, v, r, t) for c, tt, u, v, r, t in frames]
    poles = [None, None]
    for e, (fr, sgn) in enumerate(((frames[0], -1), (frames[-1], 1))):
        c, tt, u, v, r, t = fr
        if ends[e] == "round":
            extra = [ring(c + tt * (sgn * r * endk[e] * math.sin(k / endrows * math.pi / 2)), u, v,
                          r * math.cos(k / endrows * math.pi / 2), t) for k in range(1, endrows)]
            rings = (extra[::-1] + rings) if e == 0 else (rings + extra)
            poles[e] = c + tt * (sgn * r * endk[e])
        elif ends[e] == "flat":
            poles[e] = c
    return ring_mesh(rings, poles[0], poles[1])


def _rotm(rot):
    if rot is None:
        return None
    if isinstance(rot, Matrix):
        return rot
    return Euler([math.radians(a) for a in rot]).to_matrix()


def blob(c, r, seg=24, rings=14, e1=1.0, e2=1.0, deform=None, rot=None):
    """Superellipsoid (e<1 boxier, e=1 sphere). deform(p) shapes it in its own frame before rot/translate."""
    c, m = Vector(c), _rotm(rot)

    def tf(p):
        if deform:
            p = deform(p)
        return c + (m @ p if m is not None else p)

    rr = []
    for i in range(1, rings):
        phi = -math.pi / 2 + math.pi * i / rings
        cp, sp = spow(math.cos(phi), e1), spow(math.sin(phi), e1)
        rr.append([tf(Vector((r[0] * cp * spow(math.cos(2 * math.pi * j / seg), e2),
                              r[1] * cp * spow(math.sin(2 * math.pi * j / seg), e2), r[2] * sp))) for j in range(seg)])
    return ring_mesh(rr, tf(Vector((0, 0, -r[2]))), tf(Vector((0, 0, r[2]))))


def torus(c, R, r, seg=28, mseg=8, sy=1.0, rot=None):
    c, m = Vector(c), _rotm(rot)
    verts, faces = [], []
    for i in range(seg):
        a = 2 * math.pi * i / seg
        for j in range(mseg):
            b = 2 * math.pi * j / mseg
            p = Vector(((R + r * math.cos(b)) * math.cos(a), (R + r * math.cos(b)) * math.sin(a) * sy, r * math.sin(b)))
            verts.append(c + (m @ p if m is not None else p))
    for i in range(seg):
        for j in range(mseg):
            i2, j2 = (i + 1) % seg, (j + 1) % mseg
            faces.append((i * mseg + j, i2 * mseg + j, i2 * mseg + j2, i * mseg + j2))
    return verts, faces


def orient(d, xhint=(1, 0, 0)):
    """Rotation whose local Z follows d and local X stays as close to xhint as possible."""
    z = Vector(d).normalized()
    x = Vector(xhint)
    x = (x - z * x.dot(z)).normalized()
    return Matrix((x, z.cross(x), z)).transposed()


def hairline(keys, fringe=0.0, locks=7, band=0.12, inner=0.84, puff=1.0):
    """Deform for a hair blob: below the hairline height h(angle from the front) the shell sinks into the head.
    keys: [(abs angle deg, height relative to hair centre)]."""
    ka, kh = [k[0] for k in keys], [k[1] for k in keys]

    def f(p):
        th = math.degrees(math.atan2(p.x, -p.y))
        a = abs(th)
        h = _herm(ka, kh, a)
        if fringe and a < 62:
            h -= fringe * (0.5 + 0.5 * math.cos(math.radians(th) * locks)) ** 3 * (1 - a / 62)
        q = p * (inner + (1 - inner) * smooth((p.z - h) / band + 0.5))
        if q.z > 0:
            q.z *= puff
        return q
    return f


def ellipse(rx, ry, n=20):
    return [(rx * math.cos(2 * math.pi * i / n), ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def star(r_out, r_in, n=5):
    return [((r_out if i % 2 == 0 else r_in) * math.cos(math.pi / 2 + math.pi * i / n),
             (r_out if i % 2 == 0 else r_in) * math.sin(math.pi / 2 + math.pi * i / n)) for i in range(2 * n)]


def heart(s, n=28):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        pts.append((s * 16 * math.sin(t) ** 3 / 17, s * (13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t)
                                                          - math.cos(4 * t)) / 17))
    return pts[::-1]


FRONT = (Vector((0, 1, 0)), Vector((1, 0, 0)), Vector((0, 0, 1)))  # ray dir, screen-u, screen-v
TOP = (Vector((0, 0, -1)), Vector((1, 0, 0)), Vector((0, 1, 0)))
BOTTOM = (Vector((0, 0, 1)), Vector((1, 0, 0)), Vector((0, -1, 0)))


# --- part builder --------------------------------------------------------------------------------------------------
class Body:
    """Collects parts (each one material + bone weights), then joins them into one skinned mesh."""

    def __init__(self, name):
        self.name, self.objs = name, []

    def add(self, geo, mat, bone, recalc=True, smooth_shade=True):
        verts, faces = geo
        me = bpy.data.meshes.new(self.name + "_part")
        me.from_pydata([tuple(v) for v in verts], [], faces)
        me.validate()
        if recalc:
            bm = bmesh.new()
            bm.from_mesh(me)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(me)
            bm.free()
        me.polygons.foreach_set("use_smooth", [smooth_shade] * len(me.polygons))
        me.materials.append(mat)
        # palette UVs + vertex colour: what Roblox sees
        _, srgb, idx = MATS[mat.name]
        uv = me.uv_layers.new(name="UVMap")
        uv.data.foreach_set("uv", [(idx * 4 + 2) / (PALETTE_SLOTS * 4), 0.5] * len(me.loops))
        col = me.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
        col.data.foreach_set("color_srgb", [srgb[0], srgb[1], srgb[2], 1.0] * len(me.loops))
        ob = bpy.data.objects.new(self.name + "_part", me)
        scene.collection.objects.link(ob)
        if isinstance(bone, str):
            ob.vertex_groups.new(name=bone).add(list(range(len(me.vertices))), 1.0, "REPLACE")
        else:  # callable(p) -> {bone: weight}
            groups = {}
            for v in me.vertices:
                for bn, w in bone(v.co).items():
                    if w > 1e-4:
                        if bn not in groups:
                            groups[bn] = ob.vertex_groups.new(name=bn)
                        groups[bn].add([v.index], w, "REPLACE")
        self.objs.append(ob)
        return ob

    def cast(self, target, a, b, proj=FRONT):
        d, u, v = proj
        ok, loc, nor, _ = target.ray_cast(u * a + v * b - d * 30, d)
        return (loc, nor.normalized()) if ok else (None, None)

    def stick(self, target, a, b, proj=FRONT, lift=0.0):
        """Point on target's surface (plus lift along the normal) and its normal."""
        bpy.context.view_layer.update()
        p, n = self.cast(target, a, b, proj)
        if p is None:
            raise RuntimeError(f"{self.name}: stick missed at {a},{b}")
        return p + n * lift, n

    def decal(self, target, mat, bone, a, b, outline, proj=FRONT, eps=0.01, rings=2, rot=0.0):
        """Flat shape conforming to target's surface (eyes, blush, patches). No outline (open mesh)."""
        bpy.context.view_layer.update()
        p0, n0 = self.cast(target, a, b, proj)
        if p0 is None:
            print("decal miss", self.name, a, b)
            return None
        u = (proj[1] - n0 * proj[1].dot(n0)).normalized()
        v = n0.cross(u)
        cr, sr = math.cos(math.radians(rot)), math.sin(math.radians(rot))

        def place(x, y):
            x, y = x * cr - y * sr, x * sr + y * cr
            q = p0 + u * x + v * y + n0 * 0.6
            ok, loc, nor, _ = target.ray_cast(q, -n0)
            return (loc + nor.normalized() * eps) if ok else (p0 + u * x + v * y + n0 * eps)

        verts = [place(0, 0)]
        n = len(outline)
        for k in range(1, rings + 1):
            verts += [place(x * k / rings, y * k / rings) for x, y in outline]
        faces = [(0, 1 + j, 1 + (j + 1) % n) for j in range(n)]
        for k in range(rings - 1):
            a0, b0 = 1 + k * n, 1 + (k + 1) * n
            faces += [(a0 + j, b0 + j, b0 + (j + 1) % n, a0 + (j + 1) % n) for j in range(n)]
        f = faces[0]
        if (verts[f[1]] - verts[f[0]]).cross(verts[f[2]] - verts[f[0]]).dot(n0) < 0:
            faces = [tuple(reversed(f)) for f in faces]
        return self.add((verts, faces), mat, bone, recalc=False)

    def stroke(self, target, mat, bone, pts, width, proj=FRONT, eps=0.012, taper=True, n=None):
        """A brush stroke (tapered ribbon) drawn on target's surface through 2D points in projection space."""
        bpy.context.view_layer.update()
        P = [Vector(p) for p in pts]
        d = [0.0]
        for a, b in zip(P, P[1:]):
            d.append(d[-1] + (b - a).length)
        ts = [x / d[-1] for x in d]
        n = n or max(4, min(28, int(d[-1] / max(width, 0.02) * 1.5)))
        hits = []
        for i in range(n + 1):
            q = _herm(ts, P, i / n)
            h = self.cast(target, q.x, q.y, proj)
            if h[0] is not None:
                hits.append(h)
        if len(hits) < 2:
            print("stroke miss", self.name, pts)
            return None
        verts, m = [], len(hits)
        for i, (hp, hn) in enumerate(hits):
            dv = (hits[min(i + 1, m - 1)][0] - hits[max(i - 1, 0)][0]).normalized()
            s = hn.cross(dv).normalized()
            t = i / (m - 1)
            w = width * 0.5 * ((0.3 + 0.7 * math.sin(math.pi * t) ** 0.5) if taper else 1.0)
            base = hp + hn * eps
            verts += [base + s * w, base - s * w]
        faces = [(2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1) for i in range(m - 1)]
        f = faces[0]
        if (verts[f[1]] - verts[f[0]]).cross(verts[f[2]] - verts[f[0]]).dot(hits[0][1]) < 0:
            faces = [tuple(reversed(f)) for f in faces]
        return self.add((verts, faces), mat, bone, recalc=False)

    def stitches(self, target, mat, bone, pts, proj=FRONT, step=0.13, tick=0.1, width=0.035, seam=True):
        """Plush seam: optional thin seam line plus little cross ticks along it."""
        P = [Vector(p) for p in pts]
        if seam:
            self.stroke(target, mat, bone, pts, width * 0.45, proj, eps=0.008, taper=False)
        d = [0.0]
        for a, b in zip(P, P[1:]):
            d.append(d[-1] + (b - a).length)
        ts = [x / d[-1] for x in d]
        k = max(2, int(d[-1] / step))
        for i in range(k + 1):
            t = i / k
            q = _herm(ts, P, t)
            tg = (_herm(ts, P, min(1, t + 0.01)) - _herm(ts, P, max(0, t - 0.01))).normalized()
            nn = Vector((-tg.y, tg.x)) * (tick / 2)
            self.stroke(target, mat, bone, [q - nn, q + nn], width, proj, eps=0.014, n=3)

    def finish(self, rig):
        bpy.ops.object.select_all(action="DESELECT")
        for o in self.objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = self.objs[0]
        bpy.ops.object.join()
        body = bpy.context.object
        body.name, body.data.name = self.name, self.name + "_Mesh"
        bm = bmesh.new()  # weld coincident seams only within tiny distance (keeps parts separate otherwise)
        bm.from_mesh(body.data)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
        bm.to_mesh(body.data)
        bm.free()
        mod = body.modifiers.new("Rig", "ARMATURE")
        mod.object = rig
        body.parent = rig
        rig["character"] = self.name
        return body


def wz(z0, z1, lo, hi):
    """Smooth height blend from bone lo (below z0) to bone hi (above z1)."""
    def f(p):
        t = smooth((p.z - z0) / (z1 - z0))
        return {lo: 1 - t, hi: t}
    return f


# --- rig -----------------------------------------------------------------------------------------------------------
def make_rig(name, J):
    """J: hips, waist, neck, top (z heights) + left-side joint points sh, el, wr, hd, hp, kn, an, toe."""
    arm = bpy.data.armatures.new(name + "_Rig")
    rig = bpy.data.objects.new(name + "_Rig", arm)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones

    def bone(n, h, t, parent=None):
        b = eb.new(n)
        b.head, b.tail = Vector(h), Vector(t)
        if parent:
            b.parent = eb[parent]
        return b

    bone("Root", (0, 0, 0), (0, 0.6, 0))
    bone("LowerTorso", (0, 0, J["hips"]), (0, 0, J["waist"]), "Root")
    bone("UpperTorso", (0, 0, J["waist"]), (0, 0, J["neck"]), "LowerTorso")
    bone("Head", (0, 0, J["neck"]), (0, 0, J["top"]), "UpperTorso")
    for side, s in (("Left", 1), ("Right", -1)):
        def m(k):
            p = J[k]
            return (s * p[0], p[1], p[2])
        bone(side + "UpperArm", m("sh"), m("el"), "UpperTorso")
        bone(side + "LowerArm", m("el"), m("wr"), side + "UpperArm")
        bone(side + "Hand", m("wr"), m("hd"), side + "LowerArm")
        bone(side + "UpperLeg", m("hp"), m("kn"), "LowerTorso")
        bone(side + "LowerLeg", m("kn"), m("an"), side + "UpperLeg")
        bone(side + "Foot", m("an"), m("toe"), side + "LowerLeg")
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


def mirror_x(p):
    return Vector((-p[0], p[1], p[2]))


def sides(J):
    """Yield (side, sign, joint getter) for Left (+X) and Right (-X)."""
    for side, s in (("Left", 1), ("Right", -1)):
        yield side, s, (lambda k, s=s: Vector((s * J[k][0], J[k][1], J[k][2])))


# =====================================================================================================================
# THE DREAMER: a small kid in pastel pajamas and white socks, hospital wristband, worried eyebrows.
# =====================================================================================================================
PJ = toon("Pajamas", (250, 182, 206), (196, 160, 214))
TRIM = toon("PajamaTrim", (196, 180, 240), (160, 150, 214))
SKIN = toon("Skin", (255, 224, 204), (232, 178, 190))
HAIR = toon("Hair", (104, 74, 84), (150, 120, 170))
INK = toon("Ink", (60, 38, 78), (60, 38, 78))
WHITE = toon("White", (255, 252, 250), (206, 196, 230))


def build_dreamer():
    J = dict(hips=1.6, waist=2.2, neck=3.02, top=5.0,
             sh=(0.6, 0, 2.86), el=(0.76, -0.04, 2.32), wr=(0.84, -0.08, 1.84), hd=(0.87, -0.1, 1.5),
             hp=(0.31, 0, 1.6), kn=(0.33, -0.03, 0.96), an=(0.34, 0, 0.33), toe=(0.34, -0.42, 0.08))
    rig = make_rig("Dreamer", J)
    B = Body("Dreamer")
    torso_w = wz(1.95, 2.45, "LowerTorso", "UpperTorso")

    # pajama trousers (mostly hidden) + top with a soft tummy and a step at the hem
    B.add(sweep([(0, 0, 1.36), (0, 0, 2.05)], [(0, 0.6), (1, 0.62)], seg=20, rows=4, sy=0.74,
                ends=("round", "flat"), endk=(0.45, 1)), PJ, "LowerTorso")

    def tummy(t, a):
        return 1 + 0.07 * max(0.0, math.sin(a)) * math.exp(-((t - 0.3) / 0.22) ** 2)
    top = B.add(sweep([(0, 0, 1.5), (0, 0, 3.08)], [(0, 0.73), (0.12, 0.72), (0.45, 0.65), (0.82, 0.58), (1, 0.4)],
                      seg=24, rows=10, sy=0.74, ends=("flat", "round"), endk=(1, 0.4), shape=tummy), PJ, torso_w)
    B.add(sweep([(0, 0, 2.92), (0, 0, 3.3)], [(0, 0.2), (1, 0.19)], seg=12, rows=2, ends=("none", "none")),
          SKIN, "Head")  # neck
    # peter-pan collar lobes, buttons, a printed star
    for s in (1, -1):
        p, n = B.stick(top, s * 0.17, 2.96, lift=0.02)
        B.add(blob(p, (0.21, 0.13, 0.045), seg=16, rings=8, rot=orient(n, (s * 1, 0, -0.3))), TRIM, "UpperTorso")
    for z in (2.68, 2.38, 2.08):
        p, n = B.stick(top, 0.0, z, lift=0.02)
        B.add(blob(p, (0.065, 0.065, 0.03), seg=12, rings=6, rot=orient(n)), WHITE, torso_w)
    B.decal(top, TRIM, "UpperTorso", 0.32, 2.6, star(0.12, 0.055), rings=1)

    # head: soft squircle, fuller cheeks (baby proportions), slightly flattened face
    def head_shape(p):
        q = p.copy()
        if q.z < 0:
            q.x *= 1 + 0.08 * (-q.z / 0.96)
        if q.y < 0:
            q.y *= 0.93
        return q
    head = B.add(blob((0, 0, 3.96), (0.95, 0.86, 0.96), seg=32, rings=20, e1=0.82, e2=0.86, deform=head_shape),
                 SKIN, "Head")
    for s in (1, -1):
        B.add(blob((s * 0.93, 0.05, 3.9), (0.1, 0.15, 0.19), seg=12, rings=8), SKIN, "Head")
    hair_def = hairline([(0, 0.36), (35, 0.33), (75, 0.06), (100, -0.12), (140, -0.4), (180, -0.5)],
                        fringe=0.13, locks=7, band=0.1, inner=0.84, puff=1.05)
    hair = B.add(blob((0, 0.05, 4.02), (1.03, 0.95, 1.02), seg=32, rings=22, e1=0.82, e2=0.86, deform=hair_def),
                 HAIR, "Head")
    B.add(sweep([(0.04, 0.12, 4.95), (0.12, 0.08, 5.28), (0.34, 0.0, 5.34)], [(0, 0.12), (0.55, 0.07), (1, 0.015)],
                seg=10, rows=7, ends=("round", "round"), endk=(1, 1)), HAIR, "Head")  # cowlick
    for s in (1, -1):  # little flicks over the ears
        B.add(sweep([(s * 0.88, -0.1, 4.12), (s * 1.02, -0.12, 3.9), (s * 1.1, -0.2, 3.78)],
                    [(0, 0.13), (1, 0.02)], seg=10, rows=6, endrows=3), HAIR, "Head")
    del hair
    # face: big soft eyes with two glints, worried brows, tiny mouth, blush
    for s in (1, -1):
        B.decal(head, INK, "Head", s * 0.33, 3.86, ellipse(0.11, 0.16), rings=2)
        B.decal(head, WHITE, "Head", s * 0.33 + 0.04, 3.93, ellipse(0.048, 0.052), eps=0.02, rings=1)
        B.decal(head, WHITE, "Head", s * 0.33 - 0.035, 3.79, ellipse(0.022, 0.022), eps=0.02, rings=1)
        B.stroke(head, HAIR, "Head", [(s * 0.2, 4.2), (s * 0.32, 4.18), (s * 0.45, 4.12)], 0.05)
        B.decal(head, PJ, "Head", s * 0.6, 3.66, ellipse(0.13, 0.075), rings=1)
    B.stroke(head, INK, "Head", [(-0.07, 3.6), (0, 3.575), (0.07, 3.6)], 0.035)

    for side, s, g in sides(J):
        sh, el, wr, hd = g("sh"), g("el"), g("wr"), g("hd")
        d = (wr - el).normalized()
        B.add(sweep([sh, el], [(0, 0.25), (1, 0.23)], seg=14, rows=4, endrows=3), PJ, side + "UpperArm")
        B.add(sweep([el, wr + d * 0.02], [(0, 0.23), (1, 0.255)], seg=14, rows=4, endrows=3,
                    ends=("round", "flat")), PJ, side + "LowerArm")
        B.add(sweep([wr - d * 0.1, wr + d * 0.03], [(0, 0.275), (1, 0.275)], seg=14, rows=1, ends=("flat", "flat")),
              TRIM, side + "LowerArm")
        hm = orient(hd - wr, (s, 0, 0))
        B.add(blob(wr + (hd - wr).normalized() * 0.2, (0.13, 0.17, 0.21), seg=14, rings=10, rot=hm), SKIN, side + "Hand")
        B.add(blob(wr + (hd - wr).normalized() * 0.12 + Vector((-s * 0.06, -0.12, 0)), (0.06, 0.06, 0.1), seg=10,
                   rings=6, rot=orient(Vector((0, -0.6, -1)), (1, 0, 0))), SKIN, side + "Hand")
        if side == "Left":  # hospital wristband
            B.add(sweep([wr + d * 0.04, wr + d * 0.1], [(0, 0.17), (1, 0.17)], seg=14, rows=1, ends=("flat", "flat")),
                  WHITE, side + "Hand")
        hp, kn, an = g("hp"), g("kn"), g("an")
        B.add(sweep([hp, kn], [(0, 0.31), (1, 0.27)], seg=14, rows=4, endrows=3), PJ, side + "UpperLeg")
        B.add(sweep([kn, an + Vector((0, 0, 0.06))], [(0, 0.27), (1, 0.295)], seg=14, rows=4, endrows=3,
                    ends=("round", "flat")), PJ, side + "LowerLeg")
        B.add(sweep([an + Vector((0, 0, 0.17)), an + Vector((0, 0, 0.03))], [(0, 0.31), (1, 0.31)], seg=14, rows=1,
                    ends=("flat", "flat")), TRIM, side + "LowerLeg")

        def sock(p):
            q = p.copy()
            q.z = max(q.z, -0.15)
            if q.y < 0:
                q.z *= 1 - 0.25 * (-q.y / 0.38)
            return q
        B.add(blob((s * 0.34, -0.11, 0.16), (0.23, 0.37, 0.17), seg=16, rings=10, e1=0.9, deform=sock), WHITE,
              side + "Foot")
    return rig, B.finish(rig)


# =====================================================================================================================
# THE MANNEQUIN (yellow halls): an artist's wooden mannequin grown too tall, faceless. A child drew a heart on it.
# =====================================================================================================================
WOOD = toon("Wood", (246, 232, 214), (204, 178, 196))
JOINT = toon("Joint", (236, 184, 192), (196, 150, 186))


def build_mannequin():
    J = dict(hips=3.6, waist=4.36, neck=5.92, top=7.15,
             sh=(0.8, 0, 5.56), el=(0.94, 0.02, 4.36), wr=(1.0, -0.02, 3.26), hd=(1.02, -0.05, 2.66),
             hp=(0.36, 0, 3.55), kn=(0.38, -0.04, 1.96), an=(0.38, 0, 0.4), toe=(0.38, -0.8, 0.1))
    rig = make_rig("Mannequin", J)
    B = Body("Mannequin")

    def pelvis(p):
        q = p.copy()
        if q.z > 0:
            q.x *= 1 - 0.28 * (q.z / 0.44)
        return q
    B.add(blob((0, 0, 3.8), (0.62, 0.42, 0.44), seg=24, rings=12, e1=0.7, e2=0.78, deform=pelvis), WOOD, "LowerTorso")
    B.add(blob((0, 0, 4.36), (0.36, 0.31, 0.32), seg=20, rings=10), WOOD, "LowerTorso")

    def chest_shape(t, a):
        return 1 + 0.05 * max(0.0, math.sin(a)) * smooth((t - 0.3) / 0.4)
    chest = B.add(sweep([(0, 0, 4.58), (0, 0, 5.84)], [(0, 0.42), (0.35, 0.62), (0.72, 0.72), (1, 0.5)],
                        seg=24, rows=8, sy=0.6, ends=("round", "round"), endk=(0.5, 0.42), shape=chest_shape),
                  WOOD, "UpperTorso")
    B.add(blob((0, 0, 5.95), (0.2, 0.2, 0.2), seg=14, rings=8), JOINT, "Head")
    B.add(sweep([(0, 0, 5.95), (0, -0.02, 6.32)], [(0, 0.16), (1, 0.15)], seg=12, rows=2, ends=("none", "none")),
          WOOD, "Head")

    def egg(p):
        q = p.copy()
        if q.z < 0:
            k = -q.z / 0.53
            q.x *= 1 - 0.2 * k
            q.y *= 1 - 0.1 * k
        return q
    B.add(blob((0, -0.04, 6.66), (0.37, 0.43, 0.53), seg=28, rings=18, deform=egg, rot=(-8, 0, 0)), WOOD, "Head")
    B.decal(chest, JOINT, "UpperTorso", 0.27, 5.3, heart(0.13), rings=2, rot=-12)  # crayon heart

    for side, s, g in sides(J):
        sh, el, wr, hd = g("sh"), g("el"), g("wr"), g("hd")
        hp, kn, an, toe = g("hp"), g("kn"), g("an"), g("toe")
        for p, r, bone in ((sh, 0.27, "UpperArm"), (el, 0.2, "LowerArm"), (wr, 0.15, "Hand"),
                           (hp, 0.28, "UpperLeg"), (kn, 0.22, "LowerLeg"), (an, 0.16, "Foot")):
            B.add(blob(p, (r, r, r), seg=16, rings=8), JOINT, side + bone)
        d1, d2 = (el - sh).normalized(), (wr - el).normalized()
        B.add(sweep([sh + d1 * 0.16, el - d1 * 0.12], [(0, 0.23), (0.3, 0.25), (1, 0.17)], seg=14, rows=5, endrows=3,
                    endk=(0.6, 0.6)), WOOD, side + "UpperArm")
        B.add(sweep([el + d2 * 0.12, wr - d2 * 0.08], [(0, 0.18), (0.3, 0.19), (1, 0.125)], seg=14, rows=5,
                    endrows=3, endk=(0.6, 0.6)), WOOD, side + "LowerArm")
        dh = (hd - wr).normalized()
        hm = orient(dh, (s, 0, 0))
        B.add(blob(wr + dh * 0.32, (0.1, 0.2, 0.34), seg=14, rings=10, e1=0.85, rot=hm), WOOD, side + "Hand")
        B.add(sweep([wr + dh * 0.14 + Vector((-s * 0.05, -0.14, 0)), wr + dh * 0.32 + Vector((-s * 0.09, -0.2, 0))],
                    [(0, 0.055), (1, 0.045)], seg=8, rows=2, endrows=2), WOOD, side + "Hand")
        d3, d4 = (kn - hp).normalized(), (an - kn).normalized()
        B.add(sweep([hp + d3 * 0.2, kn - d3 * 0.16], [(0, 0.33), (0.25, 0.345), (1, 0.22)], seg=16, rows=5, endrows=3,
                    endk=(0.6, 0.6)), WOOD, side + "UpperLeg")
        B.add(sweep([kn + d4 * 0.16, an - d4 * 0.12], [(0, 0.22), (0.28, 0.25), (1, 0.15)], seg=14, rows=5, endrows=3,
                    endk=(0.6, 0.6)), WOOD, side + "LowerLeg")

        def foot(p):
            q = p.copy()
            q.z = max(q.z, -0.16)
            if q.y < 0:
                q.z *= 1 - 0.35 * (-q.y / 0.44)
            return q
        B.add(blob((s * 0.38, -0.3, 0.18), (0.2, 0.44, 0.18), seg=16, rings=10, e1=0.8, deform=foot), WOOD,
              side + "Foot")
    return rig, B.finish(rig)


# =====================================================================================================================
# THE BEAR (playplace): a pink hospital-gift plush, one button eye, one stitched X, a ribbon, seams.
# =====================================================================================================================
FUR = toon("Fur", (246, 168, 196), (206, 140, 196))
BELLY = toon("Belly", (255, 236, 238), (226, 196, 222))
BUTTON = toon("Button", (78, 44, 78), (60, 34, 64))
RIBBON = toon("Ribbon", (176, 196, 246), (140, 150, 220))


def build_bear():
    J = dict(hips=1.95, waist=2.7, neck=3.95, top=6.0,
             sh=(0.84, 0, 3.55), el=(1.14, -0.08, 2.98), wr=(1.3, -0.16, 2.42), hd=(1.36, -0.2, 2.02),
             hp=(0.5, 0, 1.92), kn=(0.54, -0.08, 1.16), an=(0.55, -0.04, 0.5), toe=(0.55, -0.6, 0.22))
    rig = make_rig("Bear", J)
    B = Body("Bear")
    tw = wz(2.45, 2.95, "LowerTorso", "UpperTorso")
    body = B.add(sweep([(0, 0, 1.32), (0, 0, 4.1)], [(0, 0.96), (0.22, 1.08), (0.5, 1.0), (0.82, 0.76), (1, 0.6)],
                       seg=28, rows=10, sy=0.88, ends=("round", "round"), endk=(0.6, 0.3)), FUR, tw)
    B.decal(body, BELLY, tw, 0, 2.9, ellipse(0.6, 0.54, 28), rings=4, eps=0.02)
    ring = [(0.52 * math.cos(a / 18 * 2 * math.pi), 2.9 + 0.46 * math.sin(a / 18 * 2 * math.pi)) for a in range(19)]
    B.stitches(body, BUTTON, tw, ring, step=0.2, tick=0.09, width=0.03, seam=False)  # applique stitches

    def head_shape(p):
        q = p.copy()
        if q.z < 0:
            q.x *= 1 + 0.07 * (-q.z / 1.0)
        return q
    head = B.add(blob((0, 0, 4.95), (1.2, 1.0, 0.98), seg=30, rings=18, e1=0.92, e2=0.9, deform=head_shape), FUR, "Head")
    muzzle = B.add(blob((0, -0.82, 4.62), (0.46, 0.3, 0.33), seg=20, rings=12), BELLY, "Head")
    B.add(blob((0, -1.1, 4.78), (0.17, 0.09, 0.11), seg=14, rings=8, e2=0.8, rot=(-14, 0, 0)), BUTTON, "Head")
    B.stroke(muzzle, BUTTON, "Head", [(0, 4.7), (0, 4.52)], 0.04, taper=False)
    B.stitches(muzzle, BUTTON, "Head", [(-0.24, 4.58), (-0.1, 4.47), (0.04, 4.5), (0.2, 4.55)], step=0.09, tick=0.07,
               width=0.03)  # crooked stitched smile
    # button eye (its left), X-stitched eye (its right)
    p, n = B.stick(head, 0.44, 5.12, lift=0.03)
    btn = B.add(blob(p, (0.19, 0.19, 0.06), seg=20, rings=8, e1=0.6, rot=orient(n)), BUTTON, "Head")
    for dx, dz in ((-0.05, 0.05), (0.05, 0.05), (-0.05, -0.05), (0.05, -0.05)):
        B.decal(btn, BELLY, "Head", 0.44 + dx, 5.12 + dz, ellipse(0.024, 0.024, 10), eps=0.006, rings=1)
    for a in (1, -1):
        B.stroke(head, BUTTON, "Head", [(-0.44 - 0.14, 5.12 + a * 0.14), (-0.44 + 0.14, 5.12 - a * 0.14)], 0.07)
    B.stitches(head, BUTTON, "Head", [(0, -0.75), (0, 0.0), (0, 0.8)], proj=TOP, step=0.14, tick=0.12)  # head seam
    for s in (1, -1):
        ear = B.add(blob((s * 0.84, 0.06, 5.74), (0.36, 0.17, 0.34), seg=20, rings=10, rot=(0, -s * 22, 0)), FUR, "Head")
        B.decal(ear, BELLY, "Head", s * 0.86, 5.72, ellipse(0.2, 0.18), rings=2)
    # ribbon with a bow
    B.add(torus((0, 0, 3.98), 0.66, 0.085, seg=28, mseg=8, sy=0.9), RIBBON, "UpperTorso")
    for s in (1, -1):
        B.add(blob((0.28 + s * 0.2, -0.66, 3.98), (0.2, 0.08, 0.13), seg=14, rings=8, rot=(0, s * 18, -s * 20)),
              RIBBON, "UpperTorso")
        B.add(sweep([(0.28, -0.66, 3.95), (0.28 + s * 0.08, -0.72, 3.72), (0.28 + s * 0.14, -0.7, 3.55)],
                    [(0, 0.06), (1, 0.05)], seg=8, rows=4, sy=0.5, endrows=2), RIBBON, "UpperTorso")
    B.add(blob((0.28, -0.7, 3.98), (0.07, 0.06, 0.08), seg=10, rings=6), RIBBON, "UpperTorso")

    for side, s, g in sides(J):
        sh, el, wr, hd = g("sh"), g("el"), g("wr"), g("hd")
        B.add(sweep([sh, el], [(0, 0.38), (1, 0.33)], seg=16, rows=4, endrows=3), FUR, side + "UpperArm")
        B.add(sweep([el, wr], [(0, 0.33), (1, 0.34)], seg=16, rows=4, endrows=3), FUR, side + "LowerArm")
        dh = (hd - wr).normalized()
        paw = B.add(blob(wr + dh * 0.12, (0.37, 0.36, 0.38), seg=16, rings=10, rot=orient(dh, (s, 0, 0))),
                    FUR, side + "Hand")
        pc = wr + dh * 0.12 + Vector((0, -0.34, 0))
        B.decal(paw, BELLY, side + "Hand", pc.x, pc.z, ellipse(0.17, 0.14), rings=2)
        hp, kn, an = g("hp"), g("kn"), g("an")
        B.add(sweep([hp, kn], [(0, 0.47), (1, 0.42)], seg=16, rows=4, endrows=3), FUR, side + "UpperLeg")
        B.add(sweep([kn, an], [(0, 0.42), (1, 0.38)], seg=16, rows=4, endrows=3), FUR, side + "LowerLeg")

        def foot(p):
            q = p.copy()
            q.z = max(q.z, -0.26)
            return q
        f = B.add(blob((s * 0.55, -0.24, 0.3), (0.4, 0.55, 0.3), seg=18, rings=10, deform=foot), FUR, side + "Foot")
        B.decal(f, BELLY, side + "Foot", s * 0.55, 0.24, ellipse(0.26, 0.17), proj=BOTTOM, rings=2, eps=0.02)
        B.decal(f, BELLY, side + "Foot", s * 0.55, 0.3, ellipse(0.25, 0.17), rings=3, eps=0.02)  # toe pad on the front
    return rig, B.finish(rig)


# =====================================================================================================================
# THE NEIGHBOUR (hills): too tall, too thin, long dark coat, neat hair, a polite smile that is a little too wide.
# =====================================================================================================================
COAT = toon("Coat", (92, 68, 86), (62, 44, 70))
PALE = toon("Pale", (240, 232, 226), (200, 186, 212))
DARK = toon("Dark", (44, 30, 44), (34, 22, 38))
TROUSER = toon("Trousers", (112, 112, 136), (82, 80, 112))
SHIRT = toon("Shirt", (236, 236, 244), (190, 186, 222))


def build_neighbour():
    J = dict(hips=4.08, waist=5.0, neck=7.05, top=8.7,
             sh=(0.56, 0, 6.72), el=(0.66, 0.04, 5.2), wr=(0.7, 0.0, 3.74), hd=(0.72, -0.03, 3.1),
             hp=(0.27, 0, 4.05), kn=(0.29, -0.03, 2.22), an=(0.29, 0, 0.36), toe=(0.29, -0.64, 0.08))
    rig = make_rig("Neighbour", J)
    B = Body("Neighbour")

    def coat_w(p):
        up = smooth((p.z - 4.7) / 0.6)
        leg = 0.9 * smooth((4.0 - p.z) / 2.1)
        side = max(-1.0, min(1.0, p.x / 0.45))
        wl, wr_ = leg * (0.5 + 0.5 * side), leg * (0.5 - 0.5 * side)
        return {"UpperTorso": up, "LowerTorso": max(0.0, 1 - up - wl - wr_), "LeftUpperLeg": wl, "RightUpperLeg": wr_}

    def coat_shape(t, a):  # slightly deeper chest, flat back
        return 1 + (0.06 * smooth((t - 0.6) / 0.25) if math.sin(a) > 0 else -0.03)
    coat = B.add(sweep([(0, 0, 1.72), (0, 0, 6.98)],
                       [(0, 0.74), (0.16, 0.66), (0.43, 0.52), (0.62, 0.47), (0.82, 0.56), (0.95, 0.55), (1, 0.3)],
                       seg=26, rows=18, sy=0.58, ends=("flat", "round"), endk=(1, 0.4), shape=coat_shape), COAT, coat_w)
    B.stroke(coat, DARK, coat_w, [(0.03, 6.25), (0.03, 4.2), (0.03, 1.74)], 0.03, taper=False)
    for z in (5.8, 5.3, 4.8):
        p, n = B.stick(coat, -0.08, z, lift=0.02)
        B.add(blob(p, (0.055, 0.055, 0.03), seg=12, rings=6, rot=orient(n)), DARK, coat_w)
    for s in (1, -1):  # pocket flaps + lapels
        B.stroke(coat, DARK, coat_w, [(s * 0.2, 4.15), (s * 0.44, 4.1)], 0.035, taper=False)
        p, n = B.stick(coat, s * 0.17, 6.35, lift=0.025)
        B.add(blob(p, (0.12, 0.42, 0.03), seg=12, rings=8, e1=0.6, e2=0.6,
                   rot=orient(n, (0, 0, 1)) @ Euler((0, 0, math.radians(-s * 70))).to_matrix()), COAT, "UpperTorso")
    B.decal(coat, SHIRT, "UpperTorso", 0, 6.62, [(-0.13, 0.32), (0.13, 0.32), (0, -0.3)], rings=2)  # shirt V
    B.stroke(coat, DARK, "UpperTorso", [(0, 6.8), (0, 6.38)], 0.06)  # tie
    B.add(sweep([(0, 0, 6.88), (0, 0, 7.12)], [(0, 0.2), (1, 0.19)], seg=14, rows=1, ends=("flat", "flat")),
          SHIRT, "UpperTorso")
    B.add(sweep([(0, 0, 6.95), (0, -0.04, 7.6)], [(0, 0.15), (1, 0.14)], seg=12, rows=3, ends=("none", "none")),
          PALE, "Head")

    def head_shape(p):
        q = p.copy()
        if q.z < 0:
            k = -q.z / 0.6
            q.x *= 1 - 0.24 * k
            q.y *= 1 - 0.08 * k
        if q.y < 0:
            q.y *= 0.92
        return q
    head = B.add(blob((0, -0.04, 7.98), (0.44, 0.48, 0.64), seg=28, rings=18, deform=head_shape), PALE, "Head")
    hair = B.add(blob((0, -0.02, 8.05), (0.47, 0.5, 0.64), seg=28, rings=18,
                      deform=hairline([(0, 0.34), (50, 0.26), (85, 0.02), (130, -0.2), (180, -0.28)],
                                      band=0.06, inner=0.86, puff=1.03)), DARK, "Head")
    B.stroke(hair, PALE, "Head", [(0.13, -0.3), (0.13, 0.05), (0.12, 0.3)], 0.025, proj=TOP, taper=False)  # side part
    for s in (1, -1):
        B.add(blob((s * 0.43, 0.0, 7.96), (0.06, 0.1, 0.14), seg=10, rings=6), PALE, "Head")
        B.decal(head, DARK, "Head", s * 0.155, 8.0, ellipse(0.045, 0.06, 14), rings=1)
        B.stroke(head, DARK, "Head", [(s * 0.08, 8.2), (s * 0.16, 8.25), (s * 0.24, 8.21)], 0.025)  # raised brows
        B.stroke(head, DARK, "Head", [(s * 0.235, 7.77), (s * 0.255, 7.7)], 0.022)  # dimples
    B.stroke(head, DARK, "Head", [(-0.24, 7.73), (-0.13, 7.665), (0, 7.65), (0.13, 7.665), (0.24, 7.73)], 0.03)

    for side, s, g in sides(J):
        sh, el, wr, hd = g("sh"), g("el"), g("wr"), g("hd")
        d2 = (wr - el).normalized()
        B.add(sweep([sh, el], [(0, 0.2), (1, 0.165)], seg=14, rows=5, endrows=3), COAT, side + "UpperArm")
        B.add(sweep([el, wr - d2 * 0.02], [(0, 0.165), (0.8, 0.17), (1, 0.19)], seg=14, rows=5, endrows=3,
                    ends=("round", "flat")), COAT, side + "LowerArm")
        B.add(sweep([wr - d2 * 0.06, wr + d2 * 0.06], [(0, 0.125), (1, 0.12)], seg=12, rows=1, ends=("none", "flat")),
              SHIRT, side + "LowerArm")
        dh = (hd - wr).normalized()
        hm = orient(dh, (s, 0, 0))
        hx, hy = hm.col[0], hm.col[1]  # hand thickness axis (toward body), width axis (front/back)
        palm_c = wr + dh * 0.22
        B.add(blob(palm_c, (0.07, 0.15, 0.2), seg=14, rings=10, rot=hm), PALE, side + "Hand")
        for i, (off, ln) in enumerate(((-0.11, 0.42), (-0.04, 0.52), (0.035, 0.5), (0.105, 0.4))):
            base = palm_c + dh * 0.14 + hy * off
            B.add(sweep([base, base + dh * ln * 0.55 + hy * off * 0.3 - hx * 0.015,
                         base + dh * ln + hy * off * 0.5 - hx * 0.08],
                        [(0, 0.045), (1, 0.032)], seg=8, rows=4, endrows=2), PALE, side + "Hand")
        tb = palm_c - hy * 0.12 - dh * 0.05
        B.add(sweep([tb, tb - hy * 0.09 + dh * 0.14, tb - hy * 0.08 + dh * 0.28 - hx * 0.04], [(0, 0.05), (1, 0.035)],
                    seg=8, rows=3, endrows=2), PALE, side + "Hand")
        hp, kn, an = g("hp"), g("kn"), g("an")
        B.add(sweep([hp, kn], [(0, 0.19), (1, 0.16)], seg=12, rows=5, endrows=3), TROUSER, side + "UpperLeg")
        B.add(sweep([kn, an + Vector((0, 0, 0.04))], [(0, 0.16), (1, 0.145)], seg=12, rows=5, endrows=3,
                    ends=("round", "flat")), TROUSER, side + "LowerLeg")

        def shoe(p):
            q = p.copy()
            q.z = max(q.z, -0.14)
            if q.y < 0:
                q.z *= 1 - 0.3 * (-q.y / 0.42)
                q.x *= 1 - 0.25 * (-q.y / 0.42)
            return q
        B.add(blob((s * 0.29, -0.2, 0.15), (0.15, 0.42, 0.15), seg=14, rings=10, e1=0.85, deform=shoe), DARK,
              side + "Foot")
    return rig, B.finish(rig)


# =====================================================================================================================
# POSE LIBRARY
# Angles in degrees, world axes of the rest pose (XYZ order). The character faces -Y; +X is ITS left.
#   arms/legs (pointing down): -X swings forward, +X back. Left limb -Y swings out to its side, right limb +Y.
#   head/torso (pointing up): +X nods/leans forward, +Y tilts toward its left, +Z turns toward its left.
# "_seat": the pose wants something to sit on (render_shot adds a stool).
# =====================================================================================================================
def _lr(bone, rot):
    """Left rotation + mirrored right rotation."""
    x, y, z = rot
    return {"Left" + bone: (x, y, z), "Right" + bone: (x, -y, -z)}


POSES = {
    "idle": {**_lr("UpperArm", (0, -3, 0)), **_lr("LowerArm", (-12, 0, 0)), **_lr("Hand", (-6, 0, 0)),
             "UpperTorso": (2, 0, 0), "Head": (5, 4, 0), "LeftUpperLeg": (0, -1, 0), "RightUpperLeg": (0, 1, 0)},
    "walk_contact": {"LeftUpperLeg": (-26, 0, 0), "LeftLowerLeg": (6, 0, 0), "LeftFoot": (-12, 0, 0),
                     "RightUpperLeg": (16, 0, 0), "RightLowerLeg": (24, 0, 0), "RightFoot": (-8, 0, 0),
                     "RightUpperArm": (-24, 3, 0), "RightLowerArm": (-22, 0, 0),
                     "LeftUpperArm": (22, -3, 0), "LeftLowerArm": (-10, 0, 0),
                     "LowerTorso": (0, 0, -4), "UpperTorso": (3, 0, 7), "Head": (2, 0, -4)},
    "wave": {"RightUpperArm": (-12, 128, 0), "RightLowerArm": (0, 38, 0), "RightHand": (0, 8, -90),
             "LeftUpperArm": (0, -3, 0), "LeftLowerArm": (-12, 0, 0),
             "UpperTorso": (0, -4, 0), "Head": (2, -12, 0)},
    "peek": {"LowerTorso": (0, 6, 0), "UpperTorso": (4, 12, 0), "Head": (6, 16, 10),
             "LeftUpperArm": (-58, -24, 0), "LeftLowerArm": (-62, 0, 0), "LeftHand": (0, 0, 0),
             "RightUpperArm": (-14, 4, 0), "RightLowerArm": (-48, 0, 0),
             "LeftUpperLeg": (0, -7, 0), "RightUpperLeg": (-4, -3, 0), "RightLowerLeg": (8, 0, 0)},
    "reach": {**_lr("UpperArm", (-92, 4, 0)), **_lr("LowerArm", (-10, 0, 0)), **_lr("Hand", (10, 0, 0)),
              "UpperTorso": (9, 0, 0), "LowerTorso": (3, 0, 0), "Head": (-8, 0, 0),
              "LeftUpperLeg": (-8, 0, 0), "LeftLowerLeg": (6, 0, 0),
              "RightUpperLeg": (14, 0, 0), "RightLowerLeg": (10, 0, 0), "RightFoot": (14, 0, 0)},
    "sit": {**_lr("UpperLeg", (-88, -3, 0)), **_lr("LowerLeg", (86, 0, 0)), **_lr("Foot", (2, 0, 0)),
            **_lr("UpperArm", (-26, 7, 0)), **_lr("LowerArm", (-50, 0, 0)), **_lr("Hand", (14, 0, 0)),
            "UpperTorso": (7, 0, 0), "Head": (9, 6, 0), "_seat": True},
}
# per-character flavour on top of the shared poses
POSE_OVERRIDES = {
    ("Mannequin", "reach"): {"LeftHand": (40, 0, 0), "RightHand": (40, 0, 0), "Head": (6, 0, 14)},
    ("Mannequin", "idle"): {"Head": (6, 0, -14)},
    ("Neighbour", "idle"): {"Head": (6, -9, 0), "UpperTorso": (5, 0, 0)},
    ("Neighbour", "wave"): {"Head": (4, -14, 0), "UpperTorso": (4, -3, 0), "RightUpperArm": (-14, 124, 0)},
    ("Bear", "idle"): {**_lr("UpperArm", (-6, -8, 0)), "Head": (2, 10, 0)},
    ("Bear", "sit"): {**_lr("UpperArm", (-30, -26, 0)), **_lr("LowerArm", (-20, 0, 0)), "Head": (10, 16, 0),
                      "UpperTorso": (10, 4, 0)},
    ("Dreamer", "peek"): {"Head": (8, 18, 8)},
}


def reset_pose(rig):
    for pb in rig.pose.bones:
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.location = (0, 0, 0)


def _body_of(rig):
    return next(o for o in rig.children if o.type == "MESH")


def world_bounds(ob):
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    eo = ob.evaluated_get(dg)
    me = eo.to_mesh()
    mw = eo.matrix_world
    pts = [mw @ v.co for v in me.vertices]
    eo.to_mesh_clear()
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return mn, mx


def apply_pose(rig, name, weight=1.0, ground=True):
    """Pose a liminality rig from the library. weight 0..1 scales the angles (cheap in-betweens).
    ground=True slides Root so the lowest vertex touches z=0 (no floating, no floor sinking)."""
    reset_pose(rig)
    spec = dict(POSES[name])
    spec.update(POSE_OVERRIDES.get((rig.get("character"), name), {}))
    for bn, rot in spec.items():
        pb = rig.pose.bones.get(bn)
        if bn.startswith("_") or pb is None:
            continue
        r = Euler([math.radians(a * weight) for a in rot], "XYZ").to_matrix()
        m = pb.bone.matrix_local.to_3x3()
        pb.rotation_quaternion = (m.inverted() @ r @ m).to_quaternion()
    if ground:
        mn, _ = world_bounds(_body_of(rig))
        root = rig.pose.bones["Root"]
        root.location = root.bone.matrix_local.to_3x3().inverted() @ Vector((0, 0, rig.location.z - mn.z))
    return spec


def key_pose(rig, name, frame, weight=1.0):
    """apply_pose + keyframe every bone at frame (for headless animation)."""
    apply_pose(rig, name, weight)
    for pb in rig.pose.bones:
        pb.keyframe_insert("rotation_quaternion", frame=frame)
        pb.keyframe_insert("location", frame=frame)


# =====================================================================================================================
# build everything
# =====================================================================================================================
CAST = []
for i, fn in enumerate((build_dreamer, build_mannequin, build_bear, build_neighbour)):
    rig, body = fn()
    CAST.append((body.name, rig, body))
for i, (_, rig, _) in enumerate(CAST):
    rig.location.x = i * 5.0

# floor that only shows shadows (EEVEE: shader-to-rgb mask; Cycles: shadow catcher)
bpy.ops.mesh.primitive_plane_add(size=80, location=(7.5, 0, 0))
floor = bpy.context.object
floor.name = "Floor"
for c in list(floor.users_collection):
    c.objects.unlink(floor)
NOINK.objects.link(floor)
fm = bpy.data.materials.new("ShadowFloor")
fm.use_nodes = True
nt = fm.node_tree
nt.nodes.clear()
fo = nt.nodes.new("ShaderNodeOutputMaterial")
if ENGINE == "CYCLES":
    floor.is_shadow_catcher = True
    sun.angle = math.radians(2)
    nt.links.new(nt.nodes.new("ShaderNodeBsdfDiffuse").outputs[0], fo.inputs[0])
else:
    fd, s2r, bw = nt.nodes.new("ShaderNodeBsdfDiffuse"), nt.nodes.new("ShaderNodeShaderToRGB"), nt.nodes.new("ShaderNodeRGBToBW")
    lt = nt.nodes.new("ShaderNodeMath")
    lt.operation = "LESS_THAN"
    lt.inputs[1].default_value = 0.15  # lit floor ~0.45, shadowed ~0.0
    k = nt.nodes.new("ShaderNodeMath")
    k.operation = "MULTIPLY"
    k.inputs[1].default_value = 0.38
    mixs, tr, em = nt.nodes.new("ShaderNodeMixShader"), nt.nodes.new("ShaderNodeBsdfTransparent"), nt.nodes.new("ShaderNodeEmission")
    em.inputs[0].default_value = C(120, 90, 150)
    for a, b in ((fd.outputs[0], s2r.inputs[0]), (s2r.outputs[0], bw.inputs[0]), (bw.outputs[0], lt.inputs[0]),
                 (lt.outputs[0], k.inputs[0]), (k.outputs[0], mixs.inputs[0]), (tr.outputs[0], mixs.inputs[1]),
                 (em.outputs[0], mixs.inputs[2]), (mixs.outputs[0], fo.inputs[0])):
        nt.links.new(a, b)
    if hasattr(fm, "surface_render_method"):
        fm.surface_render_method = "BLENDED"
floor.data.materials.append(fm)
floor.visible_shadow = False

STOOL = None


def make_stool():
    global STOOL
    m = toon("Stool", (214, 206, 236), (170, 160, 210))
    v, f = blob((0, 0, 0.5), (0.55, 0.5, 0.5), seg=20, rings=12, e1=0.25, e2=0.3)
    me = bpy.data.meshes.new("Stool")
    me.from_pydata([tuple(x) for x in v], [], f)
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    me.materials.append(m)
    STOOL = bpy.data.objects.new("Stool", me)
    scene.collection.objects.link(STOOL)
    STOOL.hide_render = True


make_stool()

cam = bpy.data.cameras.new("Cam")
cam.lens = 60
cam.clip_start = 0.5
cam_o = bpy.data.objects.new("Cam", cam)
scene.collection.objects.link(cam_o)
scene.camera = cam_o
set_key_light((50, 4, 34))  # from the camera side, high: faces read lit


def look_at(obj, target):
    d = Vector(target) - obj.location
    obj.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


VIEW = {"Dreamer": (0.55, -1.0, 0.16), "Mannequin": (0.55, -1.0, 0.05), "Bear": (0.6, -1.0, 0.12),
        "Neighbour": (0.5, -1.0, 0.0)}


def render_shot(name, pose, path, res=1080, view=None):
    """Pose one character, frame it, render a transparent PNG."""
    rig, body = next((r, b) for n, r, b in CAST if n == name)
    spec = apply_pose(rig, pose)
    for n, r, b in CAST:
        b.hide_render = n != name
    mn, mx = world_bounds(body)
    STOOL.hide_render = True
    if spec.get("_seat"):
        bpy.context.view_layer.update()
        hip = rig.matrix_world @ rig.pose.bones["LeftUpperLeg"].head
        seat_top = max(0.3, hip.z - 0.22)
        half = abs(hip.x - rig.location.x) + 0.45
        STOOL.scale = (half / 0.55, 0.9 / 0.5, seat_top)
        STOOL.location = (rig.location.x, hip.y + 0.35, 0)
        STOOL.hide_render = False
    c = (mn + mx) / 2
    h, w = mx.z - mn.z, max(mx.x - mn.x, mx.y - mn.y)
    d = Vector(view or VIEW[name]).normalized()
    radius = max(h, w * 1.25) * 0.5 * 1.16
    cam_o.location = c + d * (radius / math.tan(cam.angle / 2))
    look_at(cam_o, c)
    scene.render.resolution_x = scene.render.resolution_y = res
    scene.render.line_thickness = 2.4 * res / 1080
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    if ENGINE == "CYCLES":
        tint_catcher_shadow(path)
    for _, _, b in CAST:
        b.hide_render = False
    STOOL.hide_render = True
    return mn, mx


def tint_catcher_shadow(path, rgb=(120, 90, 150), strength=0.38):
    """Cycles shadow catchers write black; repaint them in the EEVEE floor's soft lavender so both engines match.
    Safe because no character colour is near-black (darkest is ~0.17)."""
    import numpy as np
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    a = a.reshape(-1, 4)
    sh = a[:, :3].max(axis=1) < 0.06
    a[sh, :3] = np.array(rgb, np.float32) / 255
    a[sh, 3] *= strength
    img.pixels.foreach_set(a.ravel())
    img.filepath_raw = path
    img.save()
    bpy.data.images.remove(img)


def report(name, rig, body):
    me = body.data
    tris = sum(len(p.vertices) - 2 for p in me.polygons)
    mats = [m.name for m in me.materials]
    groups = {g.index: g.name for g in body.vertex_groups}
    bad = sum(1 for v in me.vertices if abs(sum(g.weight for g in v.groups) - 1) > 0.01)
    unknown = [n for n in groups.values() if n not in rig.data.bones]
    print(f"[{name}] tris={tris} mats={len(mats)} {mats} bones={len(rig.data.bones)} "
          f"weight_sum_off={bad} unknown_groups={unknown}")
    assert tris < 10000 and len(mats) <= 6 and not unknown, name
    return tris


SHEET_POSES = {"Dreamer": ["idle", "peek", "sit"], "Mannequin": ["idle", "walk_contact", "reach"],
               "Bear": ["idle", "sit", "peek"], "Neighbour": ["idle", "wave", "walk_contact"]}
SHEET_VIEW = {("Mannequin", "reach"): (1.0, -0.55, 0.05), ("Bear", "sit"): (0.45, -1.0, 0.2)}
SHEET_BG = {"Dreamer": (232, 224, 250), "Mannequin": (246, 234, 196), "Bear": (214, 238, 228),
            "Neighbour": (186, 190, 226)}


def contact_sheet(cells, path, cell=720, gap=24):
    """cells: rows of (png path) -> one PNG with a solid pastel panel per character row (numpy, no PIL needed)."""
    import numpy as np
    rows, cols = len(cells), max(len(r[1]) for r in cells)
    W, H = cols * cell + (cols + 1) * gap, rows * cell + (rows + 1) * gap
    sheet = np.ones((H, W, 4), np.float32)
    sheet[:, :, :3] = np.array((250, 246, 250), np.float32) / 255
    for ri, (name, paths) in enumerate(cells):
        bg = np.array(SHEET_BG[name], np.float32) / 255
        y0 = H - (gap + ri * (cell + gap)) - cell  # numpy rows run bottom-up like Blender pixels
        sheet[y0 - gap // 2:y0 + cell + gap // 2, gap // 2:W - gap // 2, :3] = bg
        for ci, p in enumerate(paths):
            img = bpy.data.images.load(p, check_existing=False)
            w, h = img.size
            buf = np.empty(w * h * 4, np.float32)
            img.pixels.foreach_get(buf)
            bpy.data.images.remove(img)
            a = buf.reshape(h, w, 4)
            x0 = gap + ci * (cell + gap)
            dst = sheet[y0:y0 + h, x0:x0 + w, :3]
            sheet[y0:y0 + h, x0:x0 + w, :3] = dst * (1 - a[:, :, 3:4]) + a[:, :, :3] * a[:, :, 3:4]
    out = bpy.data.images.new("cast_sheet", W, H, alpha=False)
    out.pixels.foreach_set(sheet.ravel())
    out.filepath_raw = path
    out.file_format = "PNG"
    out.save()


def export_fbx(name, rig, body):
    """Rest pose, palette materials, Y-up, face -> -Z, 1 unit = 1 cm = 1 stud."""
    reset_pose(rig)
    loc = rig.location.copy()
    rig.location = (0, 0, 0)
    bpy.ops.object.select_all(action="DESELECT")
    rig.select_set(True)
    body.select_set(True)
    bpy.context.view_layer.objects.active = rig
    us = scene.unit_settings
    old = (us.system, us.scale_length)
    us.system, us.scale_length = "METRIC", 0.01  # Blender->FBX factor becomes 1.0, UnitScaleFactor = 1 (cm)
    bpy.ops.export_scene.fbx(filepath=os.path.join(OUT, name + ".fbx"), use_selection=True, add_leaf_bones=False,
                             object_types={"ARMATURE", "MESH"}, apply_unit_scale=True,
                             apply_scale_options="FBX_SCALE_UNITS", axis_forward="Z", axis_up="Y",
                             bake_anim=False, mesh_smooth_type="FACE", path_mode="COPY", embed_textures=True,
                             use_armature_deform_only=False)
    us.system, us.scale_length = old
    rig.location = loc


if __name__ == "__main__":
    for name, rig, body in CAST:
        report(name, rig, body)
    materials_for_export(True)
    for name, rig, body in CAST:
        export_fbx(name, rig, body)
    materials_for_export(False)
    tmp = os.path.join(OUT, "poses")
    os.makedirs(tmp, exist_ok=True)
    cells = []
    for name, rig, body in CAST:
        render_shot(name, "idle", os.path.join(OUT, "preview_" + name + ".png"), 1080)
        paths = []
        for pose in SHEET_POSES[name]:
            p = os.path.join(tmp, f"{name}_{pose}.png")
            mn, mx = render_shot(name, pose, p, 720, SHEET_VIEW.get((name, pose)))
            print(f"[{name}:{pose}] min_z={mn.z:.3f} height={mx.z - mn.z:.2f} width={mx.x - mn.x:.2f}")
            paths.append(p)
        cells.append((name, paths))
        reset_pose(rig)
    contact_sheet(cells, os.path.join(OUT, "cast_sheet.png"))
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "liminality_cast.blend"))
    print("CAST DONE ->", OUT)
