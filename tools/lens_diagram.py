#!/usr/bin/env python3
"""Draw the hardware guide's lens drawings: which lenses fit a box in an enclosure.

Two examples, each written to docs/images/:
  choosing-lenses.svg          a 45 x 30 cm box in an enclosure 100 x 80 cm, 90 cm high inside
  choosing-lenses-cubicle.svg  a mouse operant box in a sound-attenuating cubicle

Every number in the drawings is worked out here, for the FLIR Blackfly S
BFS-U3-13Y3M (1280 x 1024 pixels of 4.8 um, a 1/2" sensor) and a pinhole lens
(no distortion). A lens "fits" from a spot when the whole box, from its floor to
the top of its walls (and at least 10 cm up, where a mouse rears), lies inside
the middle 80% of the frame, both across and up and down, with the camera aimed
at the middle of the floor and its picture level. The margin is for tracking: an
animal climbing or rearing at a wall stays wholly in view, the calibration board
can be seen near the box's edges, and the frame's corners, where a lens distorts
most, stay empty. (On the rigs LUC3D was tested on, the arena's floor took about
half to three quarters of each frame's width.) Each camera's spot is 10 cm in
from the wall or ceiling behind it (room for the camera, the lens and a mount),
along the line it looks.

    python3 tools/lens_diagram.py            # writes both drawings
    python3 tools/lens_diagram.py --table    # prints the distances only

To check your own rig, change a scenario's `box` and `enclosure` below.
Standard library only.
"""
import math
import os
import sys

SENSOR_W, SENSOR_H = 1280 * 0.0048, 1024 * 0.0048  # mm
FILL = 0.8  # the whole box sits in the middle 80% of the frame
REAR = 10.0  # and the camera sees at least this far above the floor (a rearing mouse)
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


def corner_azimuth(enclosure, sx, sy):
    """Azimuth from the floor's middle to one of the enclosure's vertical corner edges."""
    return math.degrees(math.atan2(sy * enclosure[1] / 2, sx * enclosure[0] / 2))


TARGET = (0.0, 0.0, 0.0)  # every camera aims at the middle of the box's floor


def camera_axes(position, target=TARGET, right_hint=(1.0, 0.0, 0.0)):
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


# --- the two examples --------------------------------------------------------
# box: floor length, floor width, wall height (cm); enclosure, inside: width,
# depth, height (cm). Cameras: one looking down and two looking in from opposite
# corners at 40 degrees, as docs/calibration.md recommends, and a corner where an
# 8 mm lens is tried ("lens" given). Callouts are label offsets in the drawing.

ENCLOSURE = dict(
    file="choosing-lenses.svg",
    name="enclosure", box_name="Box",
    box=(45.0, 30.0, 20.0), enclosure=(100.0, 80.0, 90.0),
    view=dict(azimuth=-58, elevation=24, scale=4.1, origin=(560, 404)),
    size=(900, 790), thumbs_y=612, camera_scale=1.8,
    enclosure_label=(-20, 34), box_label=(8, 16),
    cameras=[
        dict(key="top", where="looking down", title="Top", u=(0.0, 0.0, 1.0), cls="c1", callout=(70, -18, "start")),
        dict(key="back", where="in a back corner", title="Back corner", az=("corner", -1, 1), el=40, cls="c2",
             callout=(-40, -46, "end")),
        dict(key="front", where="in a front corner", title="Front corner", az=("corner", 1, -1), el=40, cls="c3",
             callout=(40, 40, "start")),
        dict(key="ghost", where="in the other front corner", title="Front left", az=("corner", -1, -1), el=40,
             cls="warn", lens=8, callout=(-10, 64, "start")),
    ],
)

CUBICLE = dict(
    file="choosing-lenses-cubicle.svg",
    name="cubicle", box_name="Operant box",
    box=(22.0, 18.0, 13.0), enclosure=(55.0, 40.0, 45.0),
    view=dict(azimuth=-58, elevation=24, scale=7.0, origin=(570, 452)),
    size=(900, 830), thumbs_y=652, camera_scale=1.0,
    enclosure_label=(-30, 34), box_label=(8, 16),
    cameras=[
        dict(key="top", where="looking down", title="Top", u=(0.0, 0.0, 1.0), cls="c1", callout=(64, -22, "start")),
        dict(key="back", where="in a back corner", title="Back corner", az=("corner", -1, 1), el=40, cls="c2",
             callout=(-34, -52, "end")),
        dict(key="front", where="in a front corner", title="Front corner", az=("corner", 1, -1), el=40, cls="c3",
             callout=(40, 40, "start")),
        dict(key="ghost", where="in the other front corner", title="Front left", az=("corner", -1, -1), el=40,
             cls="warn", lens=8, callout=(-6, 118, "start")),
    ],
)

SCENARIOS = [ENCLOSURE, CUBICLE]


def must_see(sc):
    L, W = sc["box"][0] / 2, sc["box"][1] / 2
    floor = [(sx * L, sy * W, 0.0) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    top = max(sc["box"][2], REAR)
    return floor, floor + [(x, y, top) for x, y, _ in floor]


def fits(sc, position, f):
    axes = camera_axes(position)
    for p in must_see(sc)[1]:
        x, y, z = to_sensor(p, position, axes, f)
        if z <= 0 or abs(x) > FILL * SENSOR_W / 2 or abs(y) > FILL * SENSOR_H / 2:
            return False
    return True


def distance_needed(sc, u, f):
    """Shortest distance from the floor's middle, along u, from which f fits."""
    lo, hi = 1.0, 5000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if fits(sc, mul(u, mid), f):
            hi = mid
        else:
            lo = mid
    return hi


def wall_distance(sc, u):
    """Distance from the floor's middle, along u, to the enclosure's inside."""
    hx, hy, h = sc["enclosure"][0] / 2, sc["enclosure"][1] / 2, sc["enclosure"][2]
    t = [hx / abs(u[0])] if abs(u[0]) > 1e-9 else []
    t += [hy / abs(u[1])] if abs(u[1]) > 1e-9 else []
    t += [h / u[2]] if u[2] > 1e-9 else []
    return min(t)


def place_cameras(sc):
    for cam in sc["cameras"]:
        if "u" not in cam:
            az = cam["az"]
            if isinstance(az, tuple):  # aimed along a vertical corner edge
                az = corner_azimuth(sc["enclosure"], az[1], az[2])
            cam["u"] = direction(az, cam["el"])
        cam["wall"] = wall_distance(sc, cam["u"])
        cam["spot"] = cam["wall"] - MOUNT
        cam["needs"] = {f: distance_needed(sc, cam["u"], f) for f in LENSES}
        cam["too_long"] = "lens" in cam
        if cam["too_long"]:  # drawn where it would have to be
            cam["dist"] = cam["needs"][cam["lens"]]
        else:
            cam["lens"] = max(f for f in LENSES if cam["needs"][f] <= cam["spot"])
            cam["dist"] = cam["spot"]
        cam["pos"] = mul(cam["u"], cam["dist"])


for _sc in SCENARIOS:
    place_cameras(_sc)


def fmt_mm(f):
    return f"{f:g} mm"


def table():
    for sc in SCENARIOS:
        b, e = sc["box"], sc["enclosure"]
        print(f"{sc['box_name']} {b[0]:g} x {b[1]:g} cm, {sc['name']} {e[0]:g} x {e[1]:g} x {e[2]:g} cm inside")
        print("cm from the floor's middle each lens needs:")
        print("            " + "".join(f"{fmt_mm(f):>8}" for f in LENSES) + "    wall  spot  lens")
        for cam in sc["cameras"]:
            print(f"{cam['key']:<12}" + "".join(f"{cam['needs'][f]:8.0f}" for f in LENSES)
                  + f"  {cam['wall']:6.0f}{cam['spot']:6.0f}  {fmt_mm(cam['lens'])}")
        print()


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


def line(a, b, klass, extra=""):
    return f'<line class="{klass}" x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}"{extra}/>'


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


def floor_grid(sc, step=5.0):
    """Lines every 5 cm on the box's floor, from its middle out."""
    L, W = sc["box"][0] / 2, sc["box"][1] / 2
    lines = []
    for half, other, along_x in ((L, W, True), (W, L, False)):
        k = 0.0
        while k < half - 1e-6:
            for c in ({k, -k} if k else {0.0}):
                lines.append(((c, -other, 0.0), (c, other, 0.0)) if along_x else ((-other, c, 0.0), (other, c, 0.0)))
            k += step
    return lines


def box_walls(sc):
    floor = must_see(sc)[0]
    top = [(x, y, sc["box"][2]) for x, y, _ in floor]
    return floor, [[floor[i], floor[(i + 1) % 4], top[(i + 1) % 4], top[i]] for i in range(4)]


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


def camera_parts(cam, scale):
    """A Blackfly S (about a 3 cm cube) and its lens, aimed at the floor's middle."""
    w = camera_axes(cam["pos"])[2]
    lens_len, body_len = 3.4 * scale, 3.0 * scale
    lens_c = sub(cam["pos"], mul(w, lens_len / 2))
    body_c = sub(cam["pos"], mul(w, lens_len + body_len / 2))
    back = sub(cam["pos"], mul(w, lens_len + body_len))
    return [(body_c, (1.5 * scale, 1.5 * scale, body_len / 2), "body"),
            (lens_c, (1.15 * scale, 1.15 * scale, lens_len / 2), "lensc")], back


def draw_camera(out, view, cam, scale):
    axes = camera_axes(cam["pos"])
    parts, _ = camera_parts(cam, scale)
    polys = []
    for center, half, cls in parts:
        for quad, normal, front in cuboid_faces(center, axes, half):
            if dot(normal, view.V) <= 0:
                continue
            fc = cam["cls"] if cls == "lensc" and front else cls
            polys.append((view.depth(center) + dot(normal, view.V), quad, fc))
    for _, quad, fc in sorted(polys, key=lambda t: t[0]):
        p2 = pts([view(q) for q in quad])
        if cam["too_long"]:
            out.append(f'<polygon class="ghost" points="{p2}"/>')
        else:
            out.append(f'<polygon class="{fc}" points="{p2}" stroke="none"/>')


def main_view(sc, out, view):
    W, D, H = sc["enclosure"]
    hx, hy = W / 2, D / 2
    P = lambda x, y, z: view((x, y, z))
    out.append(f'<polygon class="wall1" points="{pts([P(-hx, hy, 0), P(hx, hy, 0), P(hx, hy, H), P(-hx, hy, H)])}"/>')
    out.append(f'<polygon class="wall2" points="{pts([P(-hx, -hy, 0), P(-hx, hy, 0), P(-hx, hy, H), P(-hx, -hy, H)])}"/>')
    out.append(f'<polygon class="floor" points="{pts([P(-hx, -hy, 0), P(hx, -hy, 0), P(hx, hy, 0), P(-hx, hy, 0)])}"/>')

    # lines of sight from each camera to the box's floor corners, then the box
    floor, walls = box_walls(sc)
    for cam in sc["cameras"]:
        shape = hull([view(cam["pos"])] + [view(p) for p in floor])
        extra = ' stroke-dasharray="5 4"' if cam["too_long"] else ""
        out.append(f'<polygon class="beam {cam["cls"]}" points="{pts(shape)}"{extra}/>')
    out.append(f'<polygon class="boxfloor" points="{pts([view(p) for p in floor])}"/>')
    for a, b in floor_grid(sc):
        out.append(line(view(a), view(b), "grid"))
    for quad in sorted(walls, key=lambda q: view.depth(mul(add(q[0], q[2]), 0.5))):
        out.append(f'<polygon class="glass" points="{pts([view(p) for p in quad])}"/>')

    # the cut-away front and right walls and the ceiling, as outlines
    for a, b in [((hx, -hy, 0), (hx, -hy, H)), ((-hx, -hy, H), (hx, -hy, H)), ((hx, -hy, H), (hx, hy, H)),
                 ((hx, hy, 0), (hx, -hy, 0)), ((-hx, -hy, 0), (hx, -hy, 0))]:
        out.append(line(view(a), view(b), "cut"))

    # cameras, back to front, each on a mount to the wall, corner or ceiling behind it
    anchors = {}
    for cam in sorted(sc["cameras"], key=lambda c: view.depth(c["pos"])):
        if not cam["too_long"]:
            _, back = camera_parts(cam, sc["camera_scale"])
            out.append(line(view(back), view(mul(cam["u"], cam["wall"])), "mount"))
        draw_camera(out, view, cam, sc["camera_scale"])
        anchors[cam["key"]] = view(cam["pos"])
    return anchors


def thumbnail(sc, out, x0, y0, width, cam, f, dist, title, note):
    height = width * SENSOR_H / SENSOR_W
    pos = mul(cam["u"], dist)
    axes = camera_axes(pos)
    clip = f"clip-{sc['name']}-{cam['key']}"
    out.append(f'<clipPath id="{clip}"><rect x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}"/></clipPath>')
    frame_cls = "frame warn" if cam["too_long"] else "frame"
    out.append(f'<rect class="{frame_cls}" x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}"/>')

    def to_px(p):
        x, y, _ = to_sensor(p, pos, axes, f)
        return (x0 + (x / SENSOR_W + 0.5) * width, y0 + (0.5 - y / SENSOR_H) * height)

    def poly(points3d, klass):
        # keep only what is in front of the camera
        near, kept = 1.0, []
        for i in range(len(points3d)):
            a, b = points3d[i], points3d[(i + 1) % len(points3d)]
            za, zb = to_sensor(a, pos, axes, f)[2], to_sensor(b, pos, axes, f)[2]
            if za > near:
                kept.append(a)
            if (za > near) != (zb > near):
                kept.append(add(a, mul(sub(b, a), (near - za) / (zb - za))))
        if len(kept) >= 3:
            out.append(f'<polygon class="{klass}" points="{pts([to_px(p) for p in kept])}" clip-path="url(#{clip})"/>')

    W, D, H = sc["enclosure"]
    hx, hy = W / 2, D / 2
    poly([(-hx, hy, 0), (hx, hy, 0), (hx, hy, H), (-hx, hy, H)], "wall1")
    poly([(-hx, -hy, 0), (-hx, hy, 0), (-hx, hy, H), (-hx, -hy, H)], "wall2")
    poly([(hx, -hy, 0), (hx, hy, 0), (hx, hy, H), (hx, -hy, H)], "wall2")
    poly([(-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0)], "floor")
    floor, walls = box_walls(sc)
    poly(floor, "boxfloor")
    for a, b in floor_grid(sc):
        out.append(line(to_px(a), to_px(b), "grid", f' clip-path="url(#{clip})"'))
    for quad in sorted(walls, key=lambda q: -dot(sub(mul(add(q[0], q[2]), 0.5), pos), axes[2])):
        poly(quad, "glass")
    mx, my = width * (1 - FILL) / 2, height * (1 - FILL) / 2
    out.append(f'<rect class="margin" x="{x0 + mx:.1f}" y="{y0 + my:.1f}" width="{width * FILL:.1f}" height="{height * FILL:.1f}"/>')
    out.append(f'<rect class="{frame_cls}" x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}" style="fill:none"/>')
    out.append(f'<text x="{x0}" y="{y0 - 22}" class="t-b t-{cam["cls"]}">{title}</text>')
    out.append(f'<text x="{x0}" y="{y0 - 7}" class="t-small t-muted">{note}</text>')


def a_lens(f):
    return ("an " if str(f).startswith(("8", "11", "18")) else "a ") + fmt_mm(f) + " lens"


def svg(sc):
    Wd, Hd = sc["size"]
    view = View(**sc["view"])
    out = [f'<rect class="bg" width="{Wd}" height="{Hd}"/>']
    anchors = main_view(sc, out, view)
    W, D, H = sc["enclosure"]
    name = sc["name"]

    for cam in sc["cameras"]:
        ax, ay = anchors[cam["key"]]
        dx, dy, anchor = cam["callout"]
        tx, ty = ax + dx, ay + dy
        out.append(line((ax, ay), (tx, ty - 4), "lead"))
        if cam["too_long"]:
            lines = [(f"✕ {fmt_mm(cam['lens'])} lens, {cam['where']}", "head"),
                     (f"it fits only from {cam['dist']:.0f} cm: outside the", "body"),
                     (f"{name}, whose wall is {cam['wall']:.0f} cm away", "body")]
        else:
            where = "above the floor" if cam["key"] == "top" else "from the floor's middle"
            lines = [(f"{fmt_mm(cam['lens'])} lens, {cam['where']}", "head"),
                     (f"{cam['dist']:.0f} cm {where}", "body"),
                     (f"it fits from {cam['needs'][cam['lens']]:.0f} cm", "muted")]
        for i, (text, kind) in enumerate(lines):
            k = {"head": f"t-b t-{cam['cls']}", "body": "t-small", "muted": "t-small t-muted"}[kind]
            out.append(f'<text x="{tx:.1f}" y="{ty + i * 16:.1f}" class="{k}" text-anchor="{anchor}">{text}</text>')

    notes = ["To scale." if sc["camera_scale"] == 1 else "To scale, but for the cameras (drawn larger).",
             "Camera: FLIR Blackfly S, 1/2\" sensor, 1280 × 1024.",
             "Each lens is the longest that fits from its spot.",
             "Distances: from the lens to the middle of the box&#8217;s floor."]
    for i, text in enumerate(notes):
        out.append(f'<text x="24" y="{28 + 16 * i}" class="t-small t-muted">{text}</text>')

    # labels for the enclosure and the box
    a, b = view((-W / 2, -D / 2, 0)), view((W / 2, -D / 2, 0))
    dx, dy = sc["enclosure_label"]
    out.append(f'<text x="{(a[0] + b[0]) / 2 + dx:.1f}" y="{(a[1] + b[1]) / 2 + dy:.1f}" class="t-small t-muted" '
               f'text-anchor="middle">{name.capitalize()}, inside: {W:g} × {D:g} cm, {H:g} cm high</text>')
    bx = view((sc["box"][0] / 2, -sc["box"][1] / 2, 0))
    dx, dy = sc["box_label"]
    out.append(f'<text x="{bx[0] + dx:.1f}" y="{bx[1] + dy:.1f}" class="t-small t-muted">'
               f'{sc["box_name"]}: {sc["box"][0]:g} × {sc["box"][1]:g} cm floor, {sc["box"][2]:g} cm high</text>')

    # what each camera sees
    y0, wt, gap, x = sc["thumbs_y"], 192, 24, 24
    out.append(f'<text x="24" y="{y0 - 52}" class="t-b">What each camera sees (without lens distortion). '
               'Dashed: the middle 80% of the frame, where the whole box should sit</text>')
    for cam in sc["cameras"]:
        title = f"{cam['title']}, {fmt_mm(cam['lens'])}"
        if cam["too_long"]:
            note = f"from {cam['spot']:.0f} cm, inside: the box is cut off"
            thumbnail(sc, out, x, y0, wt, cam, cam["lens"], cam["spot"], title, note)
        else:
            thumbnail(sc, out, x, y0, wt, cam, cam["lens"], cam["dist"], title, f"from {cam['dist']:.0f} cm")
        x += wt + gap

    fit = [c for c in sc["cameras"] if not c["too_long"]]
    ghost = [c for c in sc["cameras"] if c["too_long"]][0]
    fit_text = ", ".join(f"{a_lens(c['lens'])} {c['where']}" for c in fit[:-1]) + f" and {a_lens(fit[-1]['lens'])} {fit[-1]['where']}"
    aria = (f"{sc['box_name']}, {sc['box'][0]:g} by {sc['box'][1]:g} cm, inside a{'n' if name[0] in 'aeiou' else ''} {name} "
            f"{W:g} by {D:g} cm and {H:g} cm high, seen from the front right. Lenses that fit: {fit_text}. "
            f"{a_lens(ghost['lens']).capitalize()} {ghost['where']} would have to be {ghost['dist']:.0f} cm away, "
            f"outside the {name}; from inside it, the box is cut off.")
    body = "\n".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {Wd} {Hd}" width="{Wd}" height="{Hd}" '
            f'role="img" aria-label="{aria}"><style>{STYLE}</style>\n{body}\n</svg>\n')


if __name__ == "__main__":
    table()
    if "--table" not in sys.argv:
        here = os.path.dirname(os.path.abspath(__file__))
        for sc in SCENARIOS:
            path = os.path.normpath(os.path.join(here, "..", "docs", "images", sc["file"]))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(svg(sc))
            print("wrote", path)
