#!/usr/bin/env python3
"""Draw the hardware guide's lens drawings: which lenses fit around the box the animals are in.

Two examples, each written to docs/images/:
  choosing-lenses.svg          a 45 x 30 cm box in a cabinet 100 x 80 cm, 90 cm high inside
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

Two mice stand in each box, one rearing up to 10 cm and one lying behind it,
sniffing it. In each camera's view the script works out how much of the farther
mouse the nearer one hides, and outlines what is hidden: the cabinet's two side
cameras look along the line of the two mice from opposite ends, so the rearing
mouse hides the other from one and not from the other.

    python3 tools/lens_diagram.py            # writes both drawings
    python3 tools/lens_diagram.py --table    # prints the distances only

To check your own rig, change a scenario's `box` and `outer` below.
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


def corner_azimuth(outer, sx, sy):
    """Azimuth from the floor's middle to one of the outer cabinet's vertical corner edges."""
    return math.degrees(math.atan2(sy * outer[1] / 2, sx * outer[0] / 2))


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


# --- mice -------------------------------------------------------------------
MOUSE_BODY = (3.8, 1.6, 1.4)  # half-length, half-width, half-height (cm): about 10 cm nose to rump
MOUSE_HEAD = (1.6, 1.05, 1.0)
MOUSE_EAR = (0.3, 0.55, 0.6)
TAIL_LEN = 8.0


def ellipsoid(center, axes, semi, n_around=16, n_along=9):
    """Points on an ellipsoid's surface; axes are three unit vectors."""
    a, b, c = axes
    pts = [add(center, mul(a, semi[0])), add(center, mul(a, -semi[0]))]
    for j in range(1, n_along):
        ph = math.pi * j / n_along
        for i in range(n_around):
            th = 2 * math.pi * i / n_around
            pts.append(add(center, add(mul(a, semi[0] * math.cos(ph)),
                                       add(mul(b, semi[1] * math.sin(ph) * math.cos(th)),
                                           mul(c, semi[2] * math.sin(ph) * math.sin(th))))))
    return pts


def mouse_shapes(m):
    """A mouse as ellipsoids (body, head, two ears) and a tail on the floor."""
    h, t = math.radians(m["heading"]), math.radians(m["pitch"])
    fwd = (math.cos(h) * math.cos(t), math.sin(h) * math.cos(t), math.sin(t))
    lat = (-math.sin(h), math.cos(h), 0.0)
    up = cross(fwd, lat)
    rear = (m["rear"][0], m["rear"][1], MOUSE_BODY[2] + (0.3 if m["pitch"] else 0.05))
    body_c = add(rear, mul(fwd, MOUSE_BODY[0]))
    head_c = add(body_c, mul(fwd, MOUSE_BODY[0] + 0.5))
    parts = {"body": (ellipsoid(body_c, (fwd, lat, up), MOUSE_BODY), body_c),
             "head": (ellipsoid(head_c, (fwd, lat, up), MOUSE_HEAD), head_c)}
    for side in (-1, 1):
        ear_c = add(head_c, add(mul(fwd, -0.5), add(mul(lat, 0.8 * side), mul(up, 0.9))))
        parts[f"ear{side}"] = (ellipsoid(ear_c, (fwd, lat, up), MOUSE_EAR, 10, 5), ear_c)
    turn, curl = m["tail"]
    ang = h + math.pi + math.radians(turn)
    p = (m["rear"][0], m["rear"][1], 0.3)
    tail = [p]
    for _ in range(16):
        p = add(p, (math.cos(ang) * TAIL_LEN / 16, math.sin(ang) * TAIL_LEN / 16, 0.0))
        tail.append(p)
        ang += math.radians(curl) * TAIL_LEN / 16
    return parts, tail


def mouse_outline(m):
    """Body and head points: what hides, or is hidden."""
    parts, _ = mouse_shapes(m)
    return parts["body"][0] + parts["head"][0], parts["body"][1]


def polygon_area(poly):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def clip_convex(subject, clipper):
    """The part of polygon subject inside the convex polygon clipper."""
    sign = 1 if sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(clipper, clipper[1:] + clipper[:1])) > 0 else -1
    out = list(subject)
    for a, b in zip(clipper, clipper[1:] + clipper[:1]):
        inside = lambda p: sign * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) >= 0
        src, out = out, []
        for i, p in enumerate(src):
            q = src[i - 1]
            if inside(p):
                if not inside(q):
                    out.append(intersect(q, p, a, b))
                out.append(p)
            elif inside(q):
                out.append(intersect(q, p, a, b))
        if not out:
            break
    return out


def intersect(p, q, a, b):
    d1, d2 = (q[0] - p[0], q[1] - p[1]), (b[0] - a[0], b[1] - a[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    t = ((a[0] - p[0]) * d2[1] - (a[1] - p[1]) * d2[0]) / den
    return (p[0] + t * d1[0], p[1] + t * d1[1])


# --- the two examples --------------------------------------------------------
# box: the box the animals are in: floor length, floor width, wall height (cm);
# outer: the cabinet or cubicle around it, inside: width,
# depth, height (cm). Cameras: one looking down and two looking in from opposite
# corners at 40 degrees, as docs/calibration.md recommends, two side cameras on
# the end walls (in the cubicle only 50 degrees up or more leaves room for a lens),
# and a corner where an 8 mm lens is tried ("lens" given). Callouts are label
# offsets in the drawing. Mice: where the rear end is (cm from the floor's
# middle), which way the mouse faces (heading, degrees), how far its body tilts
# up (pitch: 0 lying, about 75 rearing), and its tail (turn from straight back,
# curl in degrees per cm).

CABINET = dict(
    file="choosing-lenses.svg",
    name="cabinet", box_name="Box",
    box=(45.0, 30.0, 20.0), outer=(100.0, 80.0, 90.0),
    view=dict(azimuth=-58, elevation=24, scale=4.1, origin=(560, 404)),
    width=900, notes_y=532, camera_scale=1.8,
    outer_label=(-20, 34), box_label=(8, 16),
    cameras=[
        dict(key="top", where="looking down", title="Top", u=(0.0, 0.0, 1.0), cls="c1", callout=(70, -18, "start")),
        dict(key="back", where="in a back corner", title="Back corner", az=("corner", -1, 1), el=40, cls="c2",
             callout=(-40, -46, "end")),
        dict(key="front", where="in a front corner", title="Front corner", az=("corner", 1, -1), el=40, cls="c3",
             callout=(40, 40, "start")),
        dict(key="right", where="on the right wall", title="Right wall", az=0, el=40, cls="c4",
             callout=(185, -44, "end"), closeup=True),
        dict(key="left", where="on the left wall", title="Left wall", az=180, el=40, cls="c5",
             callout=(-20, 40, "end"), closeup=True),
        dict(key="ghost", where="in the other front corner", title="Front left", az=("corner", -1, -1), el=40,
             cls="warn", lens=8, callout=(-10, 64, "start")),
    ],
    mice=[
        dict(cls="mouseA", rear=(3.0, 0.0), heading=0, pitch=75, tail=(70, 6)),
        dict(cls="mouseB", rear=(-12.0, 0.0), heading=0, pitch=0, tail=(15, 4)),
    ],
)

CUBICLE = dict(
    file="choosing-lenses-cubicle.svg",
    name="cubicle", box_name="Operant box",
    box=(22.0, 18.0, 13.0), outer=(55.0, 40.0, 45.0),
    view=dict(azimuth=-58, elevation=24, scale=7.0, origin=(570, 410)),
    width=900, notes_y=528, camera_scale=1.0,
    outer_label=(-30, 34), box_label=(8, 16),
    cameras=[
        dict(key="top", where="looking down", title="Top", u=(0.0, 0.0, 1.0), cls="c1", callout=(64, -22, "start")),
        dict(key="back", where="in a back corner", title="Back corner", az=("corner", -1, 1), el=40, cls="c2",
             callout=(-34, -72, "end")),
        dict(key="front", where="in a front corner", title="Front corner", az=("corner", 1, -1), el=40, cls="c3",
             callout=(40, 40, "start")),
        dict(key="right", where="on the right wall", title="Right wall", az=0, el=52, cls="c4",
             callout=(185, 34, "end"), closeup=True),
        dict(key="left", where="on the left wall", title="Left wall", az=180, el=52, cls="c5",
             callout=(-20, 112, "end"), closeup=True),
        dict(key="ghost", where="in the other front corner", title="Front left", az=("corner", -1, -1), el=40,
             cls="warn", lens=8, callout=(-6, 118, "start")),
    ],
    mice=[
        dict(cls="mouseA", rear=(3.5, 0.0), heading=0, pitch=75, tail=(70, 6)),
        dict(cls="mouseB", rear=(-8.0, 0.0), heading=0, pitch=0, tail=(95, 9)),
    ],
)

SCENARIOS = [CABINET, CUBICLE]


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
    """Distance from the floor's middle, along u, to the inside of the cabinet or cubicle."""
    hx, hy, h = sc["outer"][0] / 2, sc["outer"][1] / 2, sc["outer"][2]
    t = [hx / abs(u[0])] if abs(u[0]) > 1e-9 else []
    t += [hy / abs(u[1])] if abs(u[1]) > 1e-9 else []
    t += [h / u[2]] if u[2] > 1e-9 else []
    return min(t)


def place_cameras(sc):
    for cam in sc["cameras"]:
        if "u" not in cam:
            az = cam["az"]
            if isinstance(az, tuple):  # aimed along a vertical corner edge
                az = corner_azimuth(sc["outer"], az[1], az[2])
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
        # every camera drawn as fitting holds the whole box in the middle of its frame
        assert cam["too_long"] or fits(sc, cam["pos"], cam["lens"]), cam["key"]


for _sc in SCENARIOS:
    place_cameras(_sc)


def fmt_mm(f):
    return f"{f:g} mm"


def table():
    for sc in SCENARIOS:
        b, e = sc["box"], sc["outer"]
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
--c4:#bf3989;--c5:#9a6700;--mA:#5e5650;--mAs:#2f2b28;--mB:#efe8dd;--mBs:#7d746a;
color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
@media (prefers-color-scheme: dark){svg{--bg:#16181c;--fg:#e6e4df;--muted:#a3a9b1;--line:#555a63;--floor:#24272d;
--wall1:#2b2e35;--wall2:#32353d;--boxfloor:#1a1d22;--grid:#40444c;--glass:#5d8aa3;--body:#a5abb4;--lens:#6d737c;--c1:#6ea8ff;--c2:#3fcf9f;
--c3:#b69cff;--warn:#ff9a66;--c4:#f778ba;--c5:#d29922;--mA:#6e665f;--mAs:#d3cabe;--mB:#e3dbcf;--mBs:#a39a8f}}
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
.c4{fill:var(--c4);stroke:var(--c4)}.c5{fill:var(--c5);stroke:var(--c5)}
.mouseA{fill:var(--mA);stroke:var(--mAs);stroke-width:.8;stroke-linejoin:round}
.mouseB{fill:var(--mB);stroke:var(--mBs);stroke-width:.8;stroke-linejoin:round}
.tail-mouseA{fill:none;stroke:var(--mAs);stroke-width:1.1;stroke-linecap:round}
.tail-mouseB{fill:none;stroke:var(--mBs);stroke-width:1.1;stroke-linecap:round}
.hidden{fill:none;stroke:var(--warn);stroke-width:1.5;stroke-dasharray:4 3}
.warn{fill:var(--warn);stroke:var(--warn)}
.ghost{fill:none;stroke:var(--warn);stroke-width:1.3;stroke-dasharray:3 2}
text{fill:var(--fg);font-size:13px}
.t-muted{fill:var(--muted)}
.t-small{font-size:12px}
.t-b{font-weight:600}
.t-c1{fill:var(--c1)}.t-c2{fill:var(--c2)}.t-c3{fill:var(--c3)}.t-c4{fill:var(--c4)}.t-c5{fill:var(--c5)}.t-warn{fill:var(--warn)}
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
    """Parallel projection of the cabinet or cubicle seen from the front right, above."""

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


def draw_mice(sc, out, project, nearness, extra=""):
    """Tails first (they lie on the floor), then every body part, farthest first."""
    parts = []
    for m in sc["mice"]:
        shapes, tail = mouse_shapes(m)
        out.append(f'<polyline class="tail-{m["cls"]}" points="{pts([project(p) for p in tail])}"{extra}/>')
        for pts3, centre in shapes.values():
            parts.append((nearness(centre), m["cls"], hull([project(p) for p in pts3])))
    for _, cls, poly in sorted(parts, key=lambda t: t[0]):
        out.append(f'<polygon class="{cls}" points="{pts(poly)}"{extra}/>')


def main_view(sc, out, view):
    W, D, H = sc["outer"]
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
    draw_mice(sc, out, view, view.depth)
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


def hidden_share(sc, pos, axes, f):
    """How much of the farther mouse the nearer one hides in this view, and its outline."""
    seen = []
    for m in sc["mice"]:
        points, centre = mouse_outline(m)
        sil = hull([to_sensor(p, pos, axes, f)[:2] for p in points])
        seen.append((to_sensor(centre, pos, axes, f)[2], sil))
    (_, near), (_, far) = sorted(seen)
    covered = clip_convex(far, near)
    return (polygon_area(covered) / polygon_area(far) if len(covered) >= 3 else 0.0), far


def mice_extent(sc, cam):
    """Middle and size (sensor mm) of the mice's bodies in a camera's frame."""
    pos = mul(cam["u"], cam["dist"])
    axes = camera_axes(pos)
    xy = []
    for m in sc["mice"]:
        xy += [to_sensor(p, pos, axes, cam["lens"])[:2] for p in mouse_outline(m)[0]]
    x0, x1 = min(q[0] for q in xy), max(q[0] for q in xy)
    y0, y1 = min(q[1] for q in xy), max(q[1] for q in xy)
    return ((x0 + x1) / 2, (y0 + y1) / 2), (x1 - x0, y1 - y0)


def mice_windows(sc, cams, aspect, room=1.4):
    """One part of the frame per camera, around its mice, all at the same zoom."""
    extents = [mice_extent(sc, c) for c in cams]
    w = max(max(size[0], size[1] * aspect) for _, size in extents) * room
    h = w / aspect
    return [(cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2) for (cx, cy), _ in extents]


def thumbnail(sc, out, x0, y0, width, cam, f, dist, title, note, height=None, window=None, tag="frame"):
    full = window is None
    height = height or width * SENSOR_H / SENSOR_W
    wx0, wx1, wy0, wy1 = window or (-SENSOR_W / 2, SENSOR_W / 2, -SENSOR_H / 2, SENSOR_H / 2)
    pos = mul(cam["u"], dist)
    axes = camera_axes(pos)
    clip = f"clip-{sc['name']}-{cam['key']}-{tag}"
    out.append(f'<clipPath id="{clip}"><rect x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}"/></clipPath>')
    frame_cls = "frame warn" if cam["too_long"] else "frame"
    out.append(f'<rect class="{frame_cls}" x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}"/>')

    def sensor_px(q):
        return (x0 + (q[0] - wx0) / (wx1 - wx0) * width, y0 + (wy1 - q[1]) / (wy1 - wy0) * height)

    def to_px(p):
        return sensor_px(to_sensor(p, pos, axes, f))

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

    W, D, H = sc["outer"]
    hx, hy = W / 2, D / 2
    poly([(-hx, hy, 0), (hx, hy, 0), (hx, hy, H), (-hx, hy, H)], "wall1")
    poly([(-hx, -hy, 0), (-hx, hy, 0), (-hx, hy, H), (-hx, -hy, H)], "wall2")
    poly([(hx, -hy, 0), (hx, hy, 0), (hx, hy, H), (hx, -hy, H)], "wall2")
    poly([(-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0)], "floor")
    floor, walls = box_walls(sc)
    poly(floor, "boxfloor")
    for a, b in floor_grid(sc):
        out.append(line(to_px(a), to_px(b), "grid", f' clip-path="url(#{clip})"'))
    draw_mice(sc, out, to_px, lambda c: -to_sensor(c, pos, axes, f)[2], f' clip-path="url(#{clip})"')
    for quad in sorted(walls, key=lambda q: -dot(sub(mul(add(q[0], q[2]), 0.5), pos), axes[2])):
        poly(quad, "glass")
    share, far = hidden_share(sc, pos, axes, f)
    if share >= 0.15 and not cam["too_long"]:
        out.append(f'<polygon class="hidden" points="{pts([sensor_px(q) for q in far])}" clip-path="url(#{clip})"/>')
    if full and not cam["too_long"]:
        note += f" · {share:.0%} of a mouse hidden" if share >= 0.15 else " · both mice in view"
    cam["hidden"] = share
    if full:
        mx, my = width * (1 - FILL) / 2, height * (1 - FILL) / 2
        out.append(f'<rect class="margin" x="{x0 + mx:.1f}" y="{y0 + my:.1f}" width="{width * FILL:.1f}" height="{height * FILL:.1f}"/>')
    out.append(f'<rect class="{frame_cls}" x="{x0}" y="{y0}" width="{width:.1f}" height="{height:.1f}" style="fill:none"/>')
    out.append(f'<text x="{x0}" y="{y0 - 22}" class="t-b t-{cam["cls"]}">{title}</text>')
    out.append(f'<text x="{x0}" y="{y0 - 7}" class="t-small t-muted">{note}</text>')


def a_lens(f):
    return ("an " if str(f).startswith(("8", "11", "18")) else "a ") + fmt_mm(f) + " lens"


THUMB_COLS, THUMB_W, THUMB_GAP, THUMB_ROW = 3, 272, 18, 284
CLOSE_W, CLOSE_H = 417, 260


def svg(sc):
    rows = -(-len(sc["cameras"]) // THUMB_COLS)
    closeups = [c for c in sc["cameras"] if c.get("closeup")]
    header_y = sc["notes_y"] + 52
    thumbs_y = header_y + 62
    close_y = thumbs_y + rows * THUMB_ROW - 66 + 52
    Wd = sc["width"]
    Hd = (close_y + 62 + CLOSE_H if closeups else thumbs_y + rows * THUMB_ROW - 66) + 24
    view = View(**sc["view"])
    out = [f'<rect class="bg" width="{Wd}" height="{Hd}"/>']
    anchors = main_view(sc, out, view)
    W, D, H = sc["outer"]
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

    notes = [("To scale." if sc["camera_scale"] == 1 else "To scale, but for the cameras (drawn larger).")
             + " Camera: FLIR Blackfly S, 1/2\" sensor, 1280 × 1024. Mice: one rearing to 10 cm, one lying behind it.",
             "Each lens is the longest that keeps the whole box in the middle 80% of its frame. "
             "Distances: from the lens to the middle of the box&#8217;s floor."]
    for i, text in enumerate(notes):
        out.append(f'<text x="24" y="{sc["notes_y"] + 16 * i}" class="t-small t-muted">{text}</text>')

    # labels for the cabinet or cubicle and the box
    a, b = view((-W / 2, -D / 2, 0)), view((W / 2, -D / 2, 0))
    dx, dy = sc["outer_label"]
    out.append(f'<text x="{(a[0] + b[0]) / 2 + dx:.1f}" y="{(a[1] + b[1]) / 2 + dy:.1f}" class="t-small t-muted" '
               f'text-anchor="middle">{name.capitalize()}, inside: {W:g} × {D:g} cm, {H:g} cm high</text>')
    bx = view((sc["box"][0] / 2, -sc["box"][1] / 2, 0))
    dx, dy = sc["box_label"]
    out.append(f'<text x="{bx[0] + dx:.1f}" y="{bx[1] + dy:.1f}" class="t-small t-muted">'
               f'{sc["box_name"]}: {sc["box"][0]:g} × {sc["box"][1]:g} cm floor, {sc["box"][2]:g} cm high</text>')

    # what each camera sees
    out.append(f'<text x="24" y="{header_y}" class="t-b">What each camera sees (without lens distortion)</text>')
    out.append(f'<text x="24" y="{header_y + 16}" class="t-small t-muted">Grey dashes: the middle 80% of the frame, where the '
               'whole box must sit. Orange dashes: the part of a mouse another one hides.</text>')
    for i, cam in enumerate(sc["cameras"]):
        x, y = 24 + (i % THUMB_COLS) * (THUMB_W + THUMB_GAP), thumbs_y + (i // THUMB_COLS) * THUMB_ROW
        title = f"{cam['title']}, {fmt_mm(cam['lens'])}"
        if cam["too_long"]:
            note = f"from {cam['spot']:.0f} cm, inside: the box is cut off"
            thumbnail(sc, out, x, y, THUMB_W, cam, cam["lens"], cam["spot"], title, note)
        else:
            thumbnail(sc, out, x, y, THUMB_W, cam, cam["lens"], cam["dist"], title, f"from {cam['dist']:.0f} cm")

    # the side cameras' views of the mice, enlarged: one sees a mouse hidden, the other doesn't
    if closeups:
        out.append(f'<text x="24" y="{close_y}" class="t-b">The mice, enlarged, from the two side cameras</text>')
        out.append(f'<text x="24" y="{close_y + 16}" class="t-small t-muted">The same two views, enlarged equally: from one end the rearing mouse hides the other, '
                   'from the other end both are in view.</text>')
        windows = mice_windows(sc, closeups, CLOSE_W / CLOSE_H)
        for i, (cam, window) in enumerate(zip(closeups, windows)):
            x = 24 + i * (CLOSE_W + THUMB_GAP)
            hidden = cam["hidden"]
            note = (f"the rearing mouse hides {hidden:.0%} of the other" if hidden >= 0.15
                    else "the lying mouse is in front; both in view")
            thumbnail(sc, out, x, close_y + 62, CLOSE_W, cam, cam["lens"], cam["dist"],
                      f"{cam['title']}, {fmt_mm(cam['lens'])}, enlarged", note,
                      height=CLOSE_H, window=window, tag="closeup")

    fit = [c for c in sc["cameras"] if not c["too_long"]]
    ghost = [c for c in sc["cameras"] if c["too_long"]][0]
    fit_text = ", ".join(f"{a_lens(c['lens'])} {c['where']}" for c in fit[:-1]) + f" and {a_lens(fit[-1]['lens'])} {fit[-1]['where']}"
    aria = (f"{sc['box_name']}, {sc['box'][0]:g} by {sc['box'][1]:g} cm, inside a{'n' if name[0] in 'aeiou' else ''} {name} "
            f"{W:g} by {D:g} cm and {H:g} cm high, seen from the front right. Lenses that fit: {fit_text}. "
            f"{a_lens(ghost['lens']).capitalize()} {ghost['where']} would have to be {ghost['dist']:.0f} cm away, "
            f"outside the {name}; from inside it, the box is cut off. Two mice, one rearing and one lying behind it, "
            "are drawn in the box; below, what each camera sees"
            + "".join(f", and from {c['where'].replace('on the ', 'the ')} the rearing mouse hides {c['hidden']:.0%} of the other"
                      for c in fit if c["hidden"] >= 0.15) + ".")
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
