# /// script
# requires-python = ">=3.9"
# dependencies = ["manifold3d>=3.0", "numpy", "matplotlib"]
# ///
"""A 3D-printable drill jig for the BNC breakout's lid (docs/bnc-breakout.md).

The jig is a plate the size of the Hammond 1590C lid (119.5 x 94 mm). It sits on the lid, held by the lid's own four
cover screws (6-32, through the jig and the lid into the box), so it lines up on the screw holes rather than on the
cast outline. Each of the ten jack positions has a guide for a 3 mm pilot drill; the step drill then opens each pilot
to 9.5 mm without the jig. The hole layout is the one in docs/images/bnc-breakout-panel.svg: two rows of five, 22 mm
apart, the back row 19 mm behind the lid's centre and the front row 17 mm in front of it.

    uv run tools/bnc_breakout_jig.py docs/cad/bnc-breakout-drill-jig.stl

Coordinates: millimetres, the lid's centre at the origin, x toward the USB-C side (right), y toward the back,
z up from the lid's top surface.
"""
import struct
import sys

import numpy as np
from manifold3d import CrossSection, FillRule, JoinType, Manifold, OpType

# The Hammond 1590C lid (Hammond's drawing for the 1590C)
LID_W, LID_D, LID_R = 119.5, 94.0, 5.0   # outline and corner radius (the corner screws sit 5 mm in from both edges)
SCREW_DX, SCREW_DY = 109.5 / 2, 84.0 / 2  # the four cover screws, 6-32, through 4.0 mm holes in the lid

# The jacks: the drilling template's layout (docs/images/bnc-breakout-panel.svg)
PITCH = 22.0
ROWS = [(19.0, [("D2", "OUT 0"), ("D3", "OUT 1"), ("D4", "OUT 2"), ("D5", "OUT 3"), ("D6", "OUT 4")]),
        (-17.0, [("D7", "OUT 5"), ("D8", "OUT 6"), ("D9", "OUT 7"), ("D10", "CAM"), ("D13", "SYNC")])]

# The jig
PLATE = 3.0            # plate thickness
GUIDE_H = 12.0         # guide height above the lid: the length that keeps the drill straight
GUIDE_OD = 10.0
PILOT = 3.0            # the pilot drill
PILOT_HOLE = 3.2       # printed holes come out about 0.1-0.2 mm small: run the drill through each guide by hand first
SCREW_HOLE = 3.8       # clearance for a 6-32 screw (3.5 mm)
HEAD_D, HEAD_DEPTH = 7.5, 1.0   # counterbore for the pan heads: 2 mm left under each head, so the original 1/2"
                                # screws still reach about 5.7 mm of thread in the box
ENGRAVE = 0.6
SEG = 64


def rounded_rect(w, d, r):
    return CrossSection.square([w - 2 * r, d - 2 * r], center=True).offset(r, JoinType.Round, circular_segments=SEG)


def cyl(r, z0, z1, x=0.0, y=0.0, r_top=None):
    c = Manifold.cylinder(z1 - z0, r, r if r_top is None else r_top, circular_segments=SEG)
    return c.translate([x, y, z0])


def text(s, x, y, cap, bold=True, angle=0.0):
    """Text as a cross-section, centred on (x, y), with capitals `cap` mm tall."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    size = cap / 0.73   # DejaVu Sans: capital height is about 0.73 of the font size
    path = TextPath((0, 0), s, size=size, prop=FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal"))
    polys = [np.asarray(p, dtype=float) for p in path.to_polygons(closed_only=True) if len(p) >= 3]
    cs = CrossSection(polys, FillRule.EvenOdd)
    (x0, y0, x1, y1) = cs.bounds()
    cs = cs.translate([-(x0 + x1) / 2, -(y0 + y1) / 2])
    if angle:
        cs = cs.rotate(angle)
    return cs.translate([x, y])


def jacks():
    for y, row in ROWS:
        for i, (pin, role) in enumerate(row):
            yield (i - 2) * PITCH, y, pin, role


def build():
    jig = rounded_rect(LID_W, LID_D, LID_R).extrude(PLATE)
    for x, y, _, _ in jacks():
        jig += cyl(GUIDE_OD / 2, 0, GUIDE_H, x, y)

    cut = []
    for x, y, _, _ in jacks():
        cut.append(cyl(PILOT_HOLE / 2, -1, GUIDE_H + 1, x, y))
        cut.append(cyl(PILOT_HOLE / 2 + 0.6, GUIDE_H - 0.6, GUIDE_H + 0.01, x, y, r_top=PILOT_HOLE / 2 + 1.2))  # lead-in
        cut.append(cyl(PILOT_HOLE / 2 + 0.4, -0.01, 0.4, x, y, r_top=PILOT_HOLE / 2))  # against the first layer's flare
    for sx in (-1, 1):
        for sy in (-1, 1):
            cut.append(cyl(SCREW_HOLE / 2, -1, PLATE + 1, sx * SCREW_DX, sy * SCREW_DY))
            cut.append(cyl(HEAD_D / 2, PLATE - HEAD_DEPTH, PLATE + 1, sx * SCREW_DX, sy * SCREW_DY))

    # Engraved labels on the plate's top: each jack's pin and role, which side is the front, which the USB-C's
    labels = []
    for x, y, pin, role in jacks():
        labels.append(text(pin, x, y - 8.3, 3.0))
        labels.append(text(role, x, y - 12.4, 2.4))
    labels.append(text("FRONT", 0, -LID_D / 2 + 5.5, 3.6))
    labels.append(text("USB-C side", LID_W / 2 - 5.2, 0, 2.6, angle=90))
    labels.append(text("campy BNC breakout: Hammond 1590C lid", 0, LID_D / 2 - 6.0, 2.6))
    labels.append(text("3 mm pilots through the guides, then 9.5 mm", 0, LID_D / 2 - 10.6, 2.2, bold=False))
    for cs in labels:
        cut.append(cs.extrude(ENGRAVE + 1).translate([0, 0, PLATE - ENGRAVE]))

    return jig - Manifold.batch_boolean(cut, OpType.Add)


def write_stl(m, path):
    mesh = m.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
    t = np.asarray(mesh.tri_verts, dtype=np.int64)
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    n = np.cross(b - a, c - a)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(t), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3), ("c", "<f4", 3), ("attr", "<u2")])
    rec["n"], rec["a"], rec["b"], rec["c"] = n, a, b, c
    with open(path, "wb") as f:
        f.write(b"campy BNC breakout drill jig, Hammond 1590C lid, mm".ljust(80, b" "))
        f.write(struct.pack("<I", len(t)))
        f.write(rec.tobytes())
    return len(t)


def check(m):
    """The jig is one closed solid, and every pilot hole and screw hole is open all the way through."""
    assert m.status().name == "NoError", m.status()
    assert len(m.decompose()) == 1, "the jig came out in more than one piece"
    probes = [(x, y, 0, GUIDE_H) for x, y, _, _ in jacks()]
    probes += [(sx * SCREW_DX, sy * SCREW_DY, 0, PLATE) for sx in (-1, 1) for sy in (-1, 1)]
    for x, y, z0, z1 in probes:
        rod = cyl(0.5, z0 - 0.5, z1 + 0.5, x, y)
        assert (m ^ rod).volume() < 1e-6, f"hole at ({x}, {y}) is blocked"
    for x, y, _, _ in jacks():   # and the guide's wall is there around it
        ring = cyl(PILOT_HOLE / 2 + 1.0, 4, 8, x, y) - cyl(PILOT_HOLE / 2 + 0.5, 3, 9, x, y)
        assert abs((m ^ ring).volume() - ring.volume()) < 1e-3, f"guide wall at ({x}, {y}) is missing"


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "bnc-breakout-drill-jig.stl"
    jig = build()
    check(jig)
    n = write_stl(jig, out)
    bb = jig.bounding_box()
    print(f"{out}: {n} triangles, {jig.volume() / 1000:.1f} cm3, "
          f"{bb[3] - bb[0]:.1f} x {bb[4] - bb[1]:.1f} x {bb[5] - bb[2]:.1f} mm")
