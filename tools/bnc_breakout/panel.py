"""The BNC breakout lid's drilling template and labels, at 1:1 in millimetres (docs/images/bnc-breakout-panel.svg).

    python3 tools/bnc_breakout/panel.py docs/images/bnc-breakout-panel.svg
"""
import sys

from layout import BOX, HOLE, JACKS, LID_D, LID_R, LID_W, SCREWS

COLOR = {"in": "#15803d", "out": "#1d6fb8", "cam": "#c2410c", "sync": "#7c3aed"}
FONT = "Helvetica, Arial, sans-serif"
W, H = 212.0, 158.0
lx, ly = (W - LID_W) / 2, 20.0
cx, cy = lx + LID_W / 2, ly + LID_D / 2
o = []
a = o.append


def text(x, y, t, size, color="#111", anchor="middle", weight="normal", extra=""):
    a(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}" {extra}>{t}</text>')


a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}" font-family="{FONT}">')
a(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
text(cx, 7.5, f"campy BNC breakout: {BOX} lid, 1:1", 3.6, "#334155", weight="bold")
text(cx, 14.5, "BACK of the box: cable gland for the sensors", 3.2, "#475569")
a(f'<rect x="{lx:.3f}" y="{ly}" width="{LID_W}" height="{LID_D}" rx="{LID_R}" fill="#f8fafc" stroke="#64748b" stroke-width="0.5"/>')
for sx, sy in SCREWS:   # the cover screws: line the template up on them
    x, y = cx + sx, cy - sy
    a(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="none" stroke="#64748b" stroke-width="0.35"/>')
    a(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="0.4" fill="#64748b"/>')
for jx, jy, pin, role, kind in JACKS:
    x, y, col = cx + jx, cy - jy, COLOR[kind]
    a(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="8" fill="none" stroke="#cbd5e1" stroke-width="0.35" stroke-dasharray="1 1"/>')
    a(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{HOLE / 2}" fill="#ffffff" stroke="#111" stroke-width="0.4"/>')
    a(f'<line x1="{x-6.5:.2f}" y1="{y:.2f}" x2="{x+6.5:.2f}" y2="{y:.2f}" stroke="#111" stroke-width="0.2"/>')
    a(f'<line x1="{x:.2f}" y1="{y-6.5:.2f}" x2="{x:.2f}" y2="{y+6.5:.2f}" stroke="#111" stroke-width="0.2"/>')
    text(x, y + 12.2, role if kind == "in" else pin, 4.0, col, weight="bold")
    text(x, y + 16.2, pin if kind == "in" else role, 3.0, col, weight="bold")
text(cx, ly + LID_D + 6.5, "FRONT of the box (the adapter's D2–D12 terminals face this side)", 3.2, "#475569")
a(f'<text x="{W-4:.2f}" y="{cy:.2f}" font-size="3.2" fill="#475569" text-anchor="middle" transform="rotate(90 {W-4:.2f} {cy:.2f})">USB-C side</text>')
# scale bar
a(f'<line x1="{lx:.2f}" y1="{H-5}" x2="{lx+50:.2f}" y2="{H-5}" stroke="#111" stroke-width="0.5"/>')
for xx in (lx, lx + 50):
    a(f'<line x1="{xx:.2f}" y1="{H-6.5}" x2="{xx:.2f}" y2="{H-3.5}" stroke="#111" stroke-width="0.5"/>')
text(lx + 53, H - 3.9, f"50 mm: check the print before drilling. Holes {HOLE} mm, 22 mm apart; the six small circles are the lid's screws.", 2.8, "#111", "start")
a('</svg>')
open(sys.argv[1], "w").write("\n".join(o))
