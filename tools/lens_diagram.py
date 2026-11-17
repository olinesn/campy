#!/usr/bin/env python3
"""Draw docs/images/choosing-lenses.svg: which lenses fit a behaviour box in an enclosure.

Every number in the drawing is worked out here, for the FLIR Blackfly S
BFS-U3-13Y3M (1280 x 1024 pixels of 4.8 um, a 1/2" sensor) and a pinhole lens
(no distortion). A lens "fits" from a spot when the box's floor, and the space
10 cm above it where a mouse rears, lie inside the middle 90% of the frame, both
across and up and down, with the camera aimed at the middle of the floor and its
picture level.

    python3 tools/lens_diagram.py            # writes docs/images/choosing-lenses.svg
    python3 tools/lens_diagram.py --table    # prints the distances only

Standard library only.
"""
import math
import os
import sys

SENSOR_W, SENSOR_H = 1280 * 0.0048, 1024 * 0.0048  # mm
FILL = 0.9  # what the camera must see sits in the middle 90% of the frame
BOX = (45.0, 30.0, 20.0)  # box: floor length, floor width, wall height (cm)
REAR = 10.0  # the camera must see this far above the floor (a rearing mouse)
ENC = (100.0, 80.0, 90.0)  # enclosure, inside: width, depth, height (cm)
MOUNT = 10.0  # the lens's front sits this far in from a wall: camera, lens, mount
LENSES = [3.5, 4, 5, 6, 8, 12, 16]


# --- vectors ---------------------------------------------------------------

def add(a, b): return tuple(x + y for x, y in zip(a, b))
def sub(a, b): return tuple(x - y for x, y in zip(a, b))
def mul(a, k): return tuple(x * k for x in a)
def dot(a, b): return sum(x * y for x, y in zip(a, b))
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def unit(a):
    n = math.sqrt(dot(a, a))
    return mul(a, 1 / n)


def direction(azimuth, elevation):
    a, e = math.radians(azimuth), math.radians(elevation)
    return (math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))


def camera_axes(position, target, right_hint=(1.0, 0.0, 0.0)):
    """Right, up and forward unit vectors of a level camera aimed at target."""
    w = unit(sub(target, position))
    r = cross(w, (0.0, 0.0, 1.0))
    if dot(r, r) < 1e-12:  # looking straight down
        r = right_hint
    r = unit(r)
    return r, cross(r, w), w


def to_sensor(point, position, axes, f):
    """Sensor coordinates (mm) of a world point, and its depth (cm)."""
    r, v, w = axes
    d = sub(point, position)
    z = dot(d, w)
    return f * dot(d, r) / z, f * dot(d, v) / z, z


BOX_FLOOR = [(sx * BOX[0] / 2, sy * BOX[1] / 2, 0.0) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
MUST_SEE = BOX_FLOOR + [(x, y, REAR) for x, y, _ in BOX_FLOOR]
TARGET = (0.0, 0.0, 0.0)


def fits(position, f):
    axes = camera_axes(position, TARGET)
    for p in MUST_SEE:
        x, y, z = to_sensor(p, position, axes, f)
        if z <= 0 or abs(x) > FILL * SENSOR_W / 2 or abs(y) > FILL * SENSOR_H / 2:
            return False
    return True


def distance_needed(u, f):
    """Shortest distance from the floor's middle, along u, from which f fits."""
    lo, hi = 1.0, 5000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if fits(mul(u, mid), f):
            hi = mid
        else:
            lo = mid
    return hi


def wall_distance(u):
    """Distance from the floor's middle, along u, to the enclosure's inside."""
    hx, hy, h = ENC[0] / 2, ENC[1] / 2, ENC[2]
    t = [hx / abs(u[0])] if abs(u[0]) > 1e-9 else []
    t += [hy / abs(u[1])] if abs(u[1]) > 1e-9 else []
    t += [h / u[2]] if u[2] > 1e-9 else []
    return min(t)


# The cameras: one looking down and two looking in at an angle, as
# docs/calibration.md recommends, and one spot where a 12 mm lens is tried.
CAMERAS = [
    dict(key="top", u=(0.0, 0.0, 1.0), cls="c1"),
    dict(key="corner", u=direction(141.34, 40), cls="c2"),
    dict(key="side", u=direction(-20, 30), cls="c3"),
    dict(key="ghost", u=direction(-128, 35), cls="warn", lens=12),
]
for cam in CAMERAS:
    cam["wall"] = wall_distance(cam["u"])
    cam["spot"] = cam["wall"] - MOUNT
    cam["needs"] = {f: distance_needed(cam["u"], f) for f in LENSES}
    if "lens" not in cam:
        cam["lens"] = max(f for f in LENSES if cam["needs"][f] <= cam["spot"])
        cam["dist"] = cam["spot"]
    else:  # drawn where it would have to be
        cam["dist"] = cam["needs"][cam["lens"]]
    cam["pos"] = mul(cam["u"], cam["dist"])


def fmt_mm(f):
    return f"{f:g} mm"


def table():
    print(f"Box {BOX[0]:g} x {BOX[1]:g} cm, enclosure {ENC[0]:g} x {ENC[1]:g} x {ENC[2]:g} cm inside")
    print("cm from the floor's middle each lens needs:")
    print("            " + "".join(f"{fmt_mm(f):>8}" for f in LENSES) + "    wall  spot  lens")
    for cam in CAMERAS:
        print(f"{cam['key']:<12}" + "".join(f"{cam['needs'][f]:8.0f}" for f in LENSES)
              + f"  {cam['wall']:6.0f}{cam['spot']:6.0f}  {fmt_mm(cam['lens'])}")


# --- drawing ---------------------------------------------------------------

STYLE = """
svg{--bg:#fbfaf7;--fg:#1f2328;--muted:#5f666e;--line:#b9b4a9;--floor:#ebe7de;--wall1:#e1ddd3;--wall2:#d6d1c6;
--boxfloor:#fbfaf6;--grid:#cdc7ba;--glass:#9fc4d8;--body:#3b3f46;--lens:#16181b;--c1:#1f6feb;--c2:#0b8a63;--c3:#8250df;--warn:#c2410c;
color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
@media (prefers-color-scheme: dark){svg{--bg:#16181c;--fg:#e6e4df;--muted:#a3a9b1;--line:#555a63;--floor:#24272d;
--wall1:#2b2e35;--wall2:#32353d;--boxfloor:#1a1d22;--grid:#40444c;--glass:#5d8aa3;--body:#a5abb4;--lens:#6d737c;--c1:#6ea8ff;--c2:#3fcf9f;
--c3:#b69cff;--warn:#ff9a66}}
.bg{fill:var(--bg)}
.floor{fill:var(--floor);stroke:var(--line);stroke-width:1}
.wall1{fill:var(--wall1);stroke:var(--line);stroke-width:1}
.wall2{fill:var(--wall2);stroke:var(--line);stroke-width:1}
.cut{fill:none;stroke:var(--line);stroke-width:1.2;stroke-dasharray:5 4}
.boxfloor{fill:var(--boxfloor);stroke:var(--fg);stroke-width:1.1}
.grid{stroke:var(--grid);stroke-width:.7;fill:none}
.glass{fill:var(--glass);fill-opacity:.16;stroke:var(--fg);stroke-opacity:.7;stroke-width:1}
.body{fill:var(--body)}
.lensc{fill:var(--lens)}
.mount{stroke:var(--muted);stroke-width:2.2;stroke-linecap:round}
.beam{stroke-width:1;stroke-opacity:.85;fill-opacity:.13}
.c1{fill:var(--c1);stroke:var(--c1)}.c2{fill:var(--c2);stroke:var(--c2)}.c3{fill:var(--c3);stroke:var(--c3)}
.warn{fill:var(--warn);stroke:var(--warn)}
.ghost{fill:none;stroke:var(--warn);stroke-width:1.3;stroke-dasharray:3 2}
text{fill:var(--fg);font-size:13px}
.t-muted{fill:var(--muted)}
.t-small{font-size:12px}
.t-b{font-weight:600}
.t-c1{fill:var(--c1)}.t-c2{fill:var(--c2)}.t-c3{fill:var(--c3)}.t-warn{fill:var(--warn)}
.frame{fill:var(--bg);stroke:var(--fg);stroke-width:1.2}
.frame.warn{stroke:var(--warn);stroke-width:1.6;fill:var(--bg)}
.margin{fill:none;stroke:var(--muted);stroke-width:.8;stroke-dasharray:3 3}
.lead{fill:none;stroke:var(--muted);stroke-width:.8}
"""


def pts(points):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def hull(points):
    p = sorted(set((round(x, 2), round(y, 2)) for x, y in points))
    if len(p) < 3:
        return p
    def turn(o, a, b): return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for q in p:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(p):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]


class View:
    """Parallel projection of the enclosure seen from the front right, above."""

    def __init__(self, azimuth, elevation, scale, origin):
        self.V = direction(azimuth, elevation)
        self.ex = unit(cross((0.0, 0.0, 1.0), self.V))
        self.ey = cross(self.V, self.ex)
        self.s, self.o = scale, origin

    def __call__(self, p):
        return (self.o[0] + self.s * dot(p, self.ex), self.o[1] - self.s * dot(p, self.ey))

    def depth(self, p):
        return dot(p, self.V)


def floor_grid(step=5.0):
    """Lines every 5 cm on the box's floor."""
    L, W = BOX[0] / 2, BOX[1] / 2
    lines = []
    x = -L + step
    while x < L - 1e-6:
        lines.append(((x, -W, 0.0), (x, W, 0.0)))
        x += step
    y = -W + step
    while y < W - 1e-6:
        lines.append(((-L, y, 0.0), (L, y, 0.0)))
        y += step
    return lines


def cuboid_faces(center, axes, half):
    """The 6 faces of a box with the given half-sizes along axes (r, v, w)."""
    r, v, w = axes
    corner = lambda a, b, c: add(center, add(mul(r, a * half[0]), add(mul(v, b * half[1]), mul(w, c * half[2]))))
    faces = []
    for axis in range(3):
        for sign in (-1, 1):
            quad = []
            for s1, s2 in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                c = [0, 0, 0]
                c[axis] = sign
                c[(axis + 1) % 3], c[(axis + 2) % 3] = s1, s2
                quad.append(corner(*c))
            normal = mul((r, v, w)[axis], sign)
            faces.append((quad, normal, axis == 2 and sign == 1))
    return faces


CAMERA_SCALE = 1.8  # cameras are drawn this much larger than life
LENS_LEN, BODY_LEN = 3.4 * CAMERA_SCALE, 3.0 * CAMERA_SCALE


def camera_back(cam):
    """The middle of the back of a camera's body."""
    return sub(cam["pos"], mul(camera_axes(cam["pos"], TARGET)[2], LENS_LEN + BODY_LEN))


def draw_camera(out, view, cam):
    """A Blackfly S (about a 3 cm cube) with its lens, aimed at the floor's middle."""
    axes = camera_axes(cam["pos"], TARGET)
    r, v, w = axes
    scale, lens_len, body_len = CAMERA_SCALE, LENS_LEN, BODY_LEN
    lens_c = sub(cam["pos"], mul(w, lens_len / 2))
    body_c = sub(cam["pos"], mul(w, lens_len + body_len / 2))
    parts = [(body_c, (1.5 * scale, 1.5 * scale, body_len / 2), "body"),
             (lens_c, (1.15 * scale, 1.15 * scale, lens_len / 2), "lensc")]
    ghost = cam["cls"] == "warn"
    polys = []
    for center, half, cls in parts:
        for quad, normal, front in cuboid_faces(center, axes, half):
            if dot(normal, view.V) <= 0:
                continue
            fc = cls
            if cls == "lensc" and front:
                fc = cam["cls"]
            polys.append((view.depth(center) + dot(normal, view.V), quad, fc))
    for _, quad, fc in sorted(polys, key=lambda t: t[0]):
        p2 = [view(q) for q in quad]
        if ghost:
            out.append(f'<polygon class="ghost" points="{pts(p2)}"/>')
        else:
            out.append(f'<polygon class="{fc}" points="{pts(p2)}" stroke="none"/>')


def main_view(out, view):
    W, D, H = ENC
    hx, hy = W / 2, D / 2
    P = lambda x, y, z: view((x, y, z))
    floor = [P(-hx, -hy, 0), P(hx, -hy, 0), P(hx, hy, 0), P(-hx, hy, 0)]
    back = [P(-hx, hy, 0), P(hx, hy, 0), P(hx, hy, H), P(-hx, hy, H)]
    left = [P(-hx, -hy, 0), P(-hx, hy, 0), P(-hx, hy, H), P(-hx, -hy, H)]
    out.append(f'<polygon class="wall1" points="{pts(back)}"/>')
    out.append(f'<polygon class="wall2" points="{pts(left)}"/>')
    out.append(f'<polygon class="floor" points="{pts(floor)}"/>')

    # the box: floor, then its clear walls
    bf = [(x, y, 0.0) for x, y, _ in BOX_FLOOR]
    bt = [(x, y, BOX[2]) for x, y, _ in BOX_FLOOR]
    # lines of sight from each camera to the box's floor corners
    for cam in CAMERAS:
        c2 = view(cam["pos"])
        shape = hull([c2] + [view(p) for p in bf])
        extra = ' stroke-dasharray="5 4"' if cam["cls"] == "warn" else ""
        out.append(f'<polygon class="beam {cam["cls"]}" points="{pts(shape)}"{extra}/>')
    out.append(f'<polygon class="boxfloor" points="{pts([view(p) for p in bf])}"/>')
    for a, b in floor_grid():
        a, b = view(a), view(b)
        out.append(f'<line class="grid" x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}"/>')

    walls = []
    for i in range(4):
        a, b = i, (i + 1) % 4
        quad = [bf[a], bf[b], bt[b], bt[a]]
        mid = mul(add(add(quad[0], quad[1]), add(quad[2], quad[3])), 0.25)
        walls.append((view.depth(mid), quad))
    for _, quad in sorted(walls):
        out.append(f'<polygon class="glass" points="{pts([view(p) for p in quad])}"/>')

    # the cut-away walls and ceiling, as outlines
    for seg in [((hx, -hy, 0), (hx, -hy, H)), ((-hx, -hy, H), (hx, -hy, H)), ((hx, -hy, H), (hx, hy, H)),
                ((hx, hy, 0), (hx, -hy, 0)), ((-hx, -hy, 0), (hx, -hy, 0))]:
        a, b = view(seg[0]), view(seg[1])
        out.append(f'<line class="cut" x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}"/>')

    # cameras, back to front, each with its mount to the wall or ceiling
    anchors = {}
    for cam in sorted(CAMERAS, key=lambda c: view.depth(c["pos"])):
        back_pt = camera_back(cam)
        mount_to = {"top": (back_pt[0], back_pt[1], H), "corner": (-hx, hy, back_pt[2]),
                    "side": (hx, back_pt[1], back_pt[2])}.get(cam["key"])
        if mount_to:
            a, b = view(back_pt), view(mount_to)
            out.append(f'<line class="mount" x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}"/>')
        draw_camera(out, view, cam)
        anchors[cam["key"]] = view(cam["pos"])
    return anchors


def thumbnail(out, x0, y0, width, cam, f, dist, title, note, cls):
    height = width * SENSOR_H / SENSOR_W
    pos = mul(cam["u"], dist)
    axes = camera_axes(pos, TARGET)
    clip = f"clip-{cam['key']}-{str(f).replace('.', '_')}"
    out.append(f'<clipPath id="{clip}"><rect x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}"/></clipPath>')
    frame_cls = "frame warn" if cls == "warn" else "frame"
    out.append(f'<rect class="{frame_cls}" x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}"/>')

    def to_px(p):
        x, y, _ = to_sensor(p, pos, axes, f)
        return (x0 + (x / SENSOR_W + 0.5) * width, y0 + (0.5 - y / SENSOR_H) * height)

    def poly(points3d, klass):
        # keep only what is in front of the camera
        near = 1.0
        kept = []
        n = len(points3d)
        for i in range(n):
            a, b = points3d[i], points3d[(i + 1) % n]
            za, zb = to_sensor(a, pos, axes, f)[2], to_sensor(b, pos, axes, f)[2]
            if za > near:
                kept.append(a)
            if (za > near) != (zb > near):
                t = (near - za) / (zb - za)
                kept.append(add(a, mul(sub(b, a), t)))
        if len(kept) >= 3:
            out.append(f'<polygon class="{klass}" points="{pts([to_px(p) for p in kept])}" clip-path="url(#{clip})"/>')

    W, D, H = ENC
    hx, hy = W / 2, D / 2
    poly([(-hx, hy, 0), (hx, hy, 0), (hx, hy, H), (-hx, hy, H)], "wall1")
    poly([(-hx, -hy, 0), (-hx, hy, 0), (-hx, hy, H), (-hx, -hy, H)], "wall2")
    poly([(hx, -hy, 0), (hx, hy, 0), (hx, hy, H), (hx, -hy, H)], "wall2")
    poly([(-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0)], "floor")
    bf = [(x, y, 0.0) for x, y, _ in BOX_FLOOR]
    bt = [(x, y, BOX[2]) for x, y, _ in BOX_FLOOR]
    poly(bf, "boxfloor")
    for a, b in floor_grid():
        a, b = to_px(a), to_px(b)
        out.append(f'<line class="grid" x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" clip-path="url(#{clip})"/>')
    walls = []
    for i in range(4):
        a, b = i, (i + 1) % 4
        quad = [bf[a], bf[b], bt[b], bt[a]]
        mid = mul(add(add(quad[0], quad[1]), add(quad[2], quad[3])), 0.25)
        walls.append((-dot(sub(mid, pos), axes[2]), quad))
    for _, quad in sorted(walls):
        poly(quad, "glass")
    mx, my = width * (1 - FILL) / 2, height * (1 - FILL) / 2
    out.append(f'<rect class="margin" x="{x0 + mx:.1f}" y="{y0 + my:.1f}" width="{width * FILL:.1f}" height="{height * FILL:.1f}"/>')
    out.append(f'<rect class="{frame_cls}" x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}" fill="none" style="fill:none"/>')
    tcls = {"c1": "t-c1", "c2": "t-c2", "c3": "t-c3", "warn": "t-warn"}[cls]
    out.append(f'<text x="{x0}" y="{y0 - 22}" class="t-b {tcls}">{title}</text>')
    out.append(f'<text x="{x0}" y="{y0 - 7}" class="t-small t-muted">{note}</text>')
    return height


def svg():
    Wd, Hd = 900, 790
    view = View(azimuth=-58, elevation=24, scale=4.1, origin=(560, 404))
    out = [f'<rect class="bg" width="{Wd}" height="{Hd}"/>']
    anchors = main_view(out, view)

    cams = {c["key"]: c for c in CAMERAS}

    def callout(key, dx, dy, lines, anchor="start"):
        ax, ay = anchors[key]
        tx, ty = ax + dx, ay + dy
        out.append(f'<line class="lead" x1="{ax:.1f}" y1="{ay:.1f}" x2="{tx:.1f}" y2="{ty - 4:.1f}"/>')
        cls = {"c1": "t-c1", "c2": "t-c2", "c3": "t-c3", "warn": "t-warn"}[cams[key]["cls"]]
        for i, (text, kind) in enumerate(lines):
            k = {"head": f"t-b {cls}", "body": "t-small", "muted": "t-small t-muted"}[kind]
            out.append(f'<text x="{tx:.1f}" y="{ty + i * 16:.1f}" class="{k}" text-anchor="{anchor}">{text}</text>')

    t, c, s, g = cams["top"], cams["corner"], cams["side"], cams["ghost"]
    callout("top", 70, -18, [(f"{fmt_mm(t['lens'])} lens, looking down", "head"),
                             (f"{t['dist']:.0f} cm above the floor", "body"),
                             (f"it fits from {t['needs'][t['lens']]:.0f} cm", "muted")])
    callout("corner", -40, -46, [(f"{fmt_mm(c['lens'])} lens, in a back corner", "head"),
                                 (f"{c['dist']:.0f} cm from the floor's middle", "body"),
                                 (f"it fits from {c['needs'][c['lens']]:.0f} cm", "muted")], anchor="end")
    callout("side", 46, 36, [(f"{fmt_mm(s['lens'])} lens, on the side wall", "head"),
                             (f"{s['dist']:.0f} cm from the floor's middle", "body"),
                             (f"it fits from {s['needs'][s['lens']]:.0f} cm", "muted")])
    callout("ghost", -10, 64, [(f"✕ {fmt_mm(g['lens'])} lens, in a front corner", "head"),
                               (f"it fits only from {g['dist']:.0f} cm: outside the", "body"),
                               (f"enclosure, whose wall is {g['wall']:.0f} cm away", "body")])

    notes = ["To scale, but for the cameras (drawn larger).",
             "Camera: FLIR Blackfly S, 1/2\" sensor, 1280 × 1024.",
             "Each lens is the longest that fits from its spot.",
             "Distances: from the lens to the middle of the box&#8217;s floor."]
    for i, line in enumerate(notes):
        out.append(f'<text x="24" y="{28 + 16 * i}" class="t-small t-muted">{line}</text>')

    # labels for the enclosure and the box
    W, D, H = ENC
    a = view((-W / 2, -D / 2, 0))
    b = view((W / 2, -D / 2, 0))
    out.append(f'<text x="{(a[0] + b[0]) / 2 - 20:.1f}" y="{(a[1] + b[1]) / 2 + 34:.1f}" class="t-small t-muted" text-anchor="middle">'
               f'Enclosure, inside: {W:g} × {D:g} cm, {H:g} cm high</text>')
    bx = view((BOX[0] / 2, -BOX[1] / 2, 0))
    out.append(f'<text x="{bx[0] + 8:.1f}" y="{bx[1] + 16:.1f}" class="t-small t-muted">Box: {BOX[0]:g} × {BOX[1]:g} cm floor</text>')

    # what each camera sees
    y0 = 612
    wt, gap = 192, 24
    x = 24
    out.append(f'<text x="24" y="{y0 - 52}" class="t-b">What each camera sees (without lens distortion). Dashed: the middle 90% of the frame, where the box should sit</text>')
    for cam, f, dist, title, note in [
        (t, t["lens"], t["dist"], f"Top, {fmt_mm(t['lens'])}", f"from {t['dist']:.0f} cm"),
        (c, c["lens"], c["dist"], f"Back corner, {fmt_mm(c['lens'])}", f"from {c['dist']:.0f} cm"),
        (s, s["lens"], s["dist"], f"Side wall, {fmt_mm(s['lens'])}", f"from {s['dist']:.0f} cm"),
        (g, g["lens"], g["spot"], f"Front corner, {fmt_mm(g['lens'])}", f"from {g['spot']:.0f} cm, inside: the box is cut off"),
    ]:
        thumbnail(out, x, y0, wt, cam, f, dist, title, note, cam["cls"])
        x += wt + gap

    def a_lens(f):
        return ("an " if str(f).startswith(("8", "11", "18")) else "a ") + fmt_mm(f) + " lens"
    aria = (f"A behaviour box, {BOX[0]:g} by {BOX[1]:g} cm, inside an enclosure {W:g} by {D:g} cm and {H:g} cm high, "
            f"seen from the front right. {a_lens(t['lens']).capitalize()} fits looking down from the ceiling, "
            f"{a_lens(c['lens'])} from a back corner, and {a_lens(s['lens'])} from the side wall. "
            f"{a_lens(g['lens']).capitalize()} would have to be {g['dist']:.0f} cm away, outside the enclosure; "
            f"from inside it, the box is cut off.")
    body = "\n".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Wd} {Hd}" width="{Wd}" height="{Hd}" '
            f'role="img" aria-label="{aria}"><style>{STYLE}</style>\n{body}\n</svg>\n')


if __name__ == "__main__":
    if "--table" in sys.argv:
        table()
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "..", "docs", "images", "choosing-lenses.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg())
        table()
        print("wrote", os.path.normpath(path))
