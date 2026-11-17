# /// script
# requires-python = ">=3.9"
# dependencies = ["manifold3d>=3.0", "numpy", "matplotlib"]
# ///
"""A 3D-printable drill jig for the BNC breakout's lid (docs/bnc-breakout.md).

The jig is a plate the size of the lid. It sits on the lid, held by the lid's own cover screws (6-32, through the
jig and the lid into the box), so it lines up on the screw holes rather than on the cast outline. Each jack position
has a guide for a 3 mm pilot drill; the step drill then opens each pilot to 9.5 mm without the jig. The box, the
screws and the jacks come from layout.py, as for the drilling template (panel.py).

    uv run tools/bnc_breakout/jig.py docs/cad/bnc-breakout-drill-jig.stl

Coordinates: millimetres, the lid's centre at the origin, x toward the USB-C side (right), y toward the back,
z up from the lid's top surface.
"""
import struct
import sys

import numpy as np
from manifold3d import CrossSection, FillRule, JoinType, Manifold, OpType

from layout import BOX, JACKS, LID_D, LID_R, LID_W, SCREWS

PLATE = 3.0            # plate thickness
GUIDE_H = 12.0         # guide height above the lid: the length that keeps the drill straight
GUIDE_OD = 10.0
PILOT_HOLE = 3.2       # for a 3 mm drill: printed holes come out about 0.1-0.2 mm small, so the drill is run
                       # through each guide by hand first
SCREW_HOLE = 3.8       # clearance for a 6-32 screw (3.5 mm)
HEAD_D, HEAD_DEPTH = 7.5, 1.0   # counterbore for the pan heads: 2 mm of jig left under each head, so the lid's
                                # own 1/2" screws still reach the box's thread
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


def build():
    jig = rounded_rect(LID_W, LID_D, LID_R).extrude(PLATE)
    for x, y, *_ in JACKS:
        jig += cyl(GUIDE_OD / 2, 0, GUIDE_H, x, y)

    cut = []
    for x, y, *_ in JACKS:
        cut.append(cyl(PILOT_HOLE / 2, -1, GUIDE_H + 1, x, y))
        cut.append(cyl(PILOT_HOLE / 2 + 0.6, GUIDE_H - 0.6, GUIDE_H + 0.01, x, y, r_top=PILOT_HOLE / 2 + 1.2))  # lead-in
        cut.append(cyl(PILOT_HOLE / 2 + 0.4, -0.01, 0.4, x, y, r_top=PILOT_HOLE / 2))  # against the first layer's flare
    for x, y in SCREWS:
        cut.append(cyl(SCREW_HOLE / 2, -1, PLATE + 1, x, y))
        cut.append(cyl(HEAD_D / 2, PLATE - HEAD_DEPTH, PLATE + 1, x, y))

    # Engraved labels on the plate's top: each jack's names, which side is the front, which the USB-C's
    labels = []
    for x, y, pin, role, kind in JACKS:
        big, small = (role, pin) if kind == "in" else (pin, role)
        labels.append(text(big, x, y - 8.3, 3.0))
        labels.append(text(small, x, y - 12.4, 2.4))
    labels.append(text("FRONT", -LID_W / 4, -LID_D / 2 + 5.0, 3.6))
    labels.append(text("3 mm pilots, then 9.5 mm", LID_W / 4, -LID_D / 2 + 5.0, 2.4, bold=False))
    labels.append(text("USB-C side", LID_W / 2 - 4.6, 0, 2.6, angle=90))
    labels.append(text(f"campy BNC breakout: {BOX} lid", 0, LID_D / 2 - 13.5, 2.6))
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
        f.write(f"campy BNC breakout drill jig, {BOX} lid, mm".encode().ljust(80, b" "))
        f.write(struct.pack("<I", len(t)))
        f.write(rec.tobytes())
    return len(t)


def check(m):
    """The jig is one closed solid, every pilot and screw hole is open all the way through, and nothing overlaps."""
    assert m.status().name == "NoError", m.status()
    assert len(m.decompose()) == 1, "the jig came out in more than one piece"
    probes = [(x, y, GUIDE_H) for x, y, *_ in JACKS] + [(x, y, PLATE) for x, y in SCREWS]
    for x, y, top in probes:
        rod = cyl(0.5, -0.5, top + 0.5, x, y)
        assert (m ^ rod).volume() < 1e-6, f"hole at ({x}, {y}) is blocked"
    for x, y, *_ in JACKS:   # the guide's wall is whole
        ring = cyl(PILOT_HOLE / 2 + 1.0, 4, 8, x, y) - cyl(PILOT_HOLE / 2 + 0.5, 3, 9, x, y)
        assert abs((m ^ ring).volume() - ring.volume()) < 1e-3, f"guide wall at ({x}, {y}) is missing"
    pts = [(x, y, GUIDE_OD / 2) for x, y, *_ in JACKS] + [(x, y, HEAD_D / 2) for x, y in SCREWS]
    for i, (x1, y1, r1) in enumerate(pts):
        for x2, y2, r2 in pts[i + 1:]:
            assert (x1 - x2) ** 2 + (y1 - y2) ** 2 > (r1 + r2 + 1.0) ** 2, f"({x1}, {y1}) and ({x2}, {y2}) overlap"
        assert abs(x1) + r1 < LID_W / 2 and abs(y1) + r1 < LID_D / 2, f"({x1}, {y1}) runs off the lid"


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "bnc-breakout-drill-jig.stl"
    jig = build()
    check(jig)
    n = write_stl(jig, out)
    bb = jig.bounding_box()
    print(f"{out}: {n} triangles, {jig.volume() / 1000:.1f} cm3, "
          f"{bb[3] - bb[0]:.1f} x {bb[4] - bb[1]:.1f} x {bb[5] - bb[2]:.1f} mm")
