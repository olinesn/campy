"""One input of the BNC breakout's input board, drawn in full (docs/images/bnc-breakout-inputs.svg).

    python3 tools/bnc_breakout/inputs.py docs/images/bnc-breakout-inputs.svg
"""
import sys

IN, GND, PWR, BUF, NOTE = "#15803d", "#111111", "#dc2626", "#0f6e74", "#475569"
FONT = "Helvetica, Arial, sans-serif"
W, H = 1000, 580
o = []
a = o.append


def text(x, y, t, size=13, color="#111", anchor="middle", weight="normal"):
    a(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{t}</text>')


def line(x1, y1, x2, y2, color=IN, w=2.2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    a(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}" stroke-linecap="round"{d}/>')


def poly(pts, color=IN, w=2.2):
    a(f'<polyline points="{" ".join(f"{x},{y}" for x, y in pts)}" fill="none" stroke="{color}" stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"/>')


def dot(x, y, color=IN):
    a(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}"/>')


def res_h(x1, x2, y, label, color=IN, below=False):
    m = (x1 + x2) / 2
    line(x1, y, m - 22, y, color)
    a(f'<rect x="{m-22}" y="{y-8}" width="44" height="16" rx="2" fill="#fff" stroke="{color}" stroke-width="2"/>')
    line(m + 22, y, x2, y, color)
    text(m, y + 28 if below else y - 14, label, 13, "#111", weight="bold")


def res_v(x, y1, y2, label, color=IN, left=False):
    m = (y1 + y2) / 2
    line(x, y1, x, m - 22, color)
    a(f'<rect x="{x-8}" y="{m-22}" width="16" height="44" rx="2" fill="#fff" stroke="{color}" stroke-width="2"/>')
    line(x, m + 22, x, y2, color)
    text(x - 16 if left else x + 16, m + 5, label, 13, "#111", "end" if left else "start", "bold")


def ground(x, y):
    line(x, y, x, y + 8, GND)
    for i, w in enumerate((14, 9, 4)):
        line(x - w, y + 8 + i * 5, x + w, y + 8 + i * 5, GND, 2)


def rail(x, y, label, color=PWR):
    line(x, y, x, y - 10, color)
    line(x - 10, y - 10, x + 10, y - 10, color, 2.4)
    text(x, y - 18, label, 12.5, color, weight="bold")


a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" font-size="13">')
a(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
text(24, 32, "One input of the input board, drawn in full: there are eight (IN 0–7, each to its own A pin)", 15, "#334155", "start", "bold")

y = 170   # the signal line
# BNC input jack
bx = 80
a(f'<circle cx="{bx}" cy="{y}" r="22" fill="#e2e8f0" stroke="#334155" stroke-width="2.5"/>')
a(f'<circle cx="{bx}" cy="{y}" r="8" fill="#fff" stroke="#334155" stroke-width="1.5"/>')
a(f'<circle cx="{bx}" cy="{y}" r="3" fill="{IN}"/>')
text(bx, y - 34, "IN n (BNC)", 13, IN, weight="bold")
poly([(bx, y + 22), (bx, y + 60)], GND, 2)
ground(bx, y + 60)
text(bx, y + 108, "shell to the", 11.5, NOTE)
text(bx, y + 122, "ground bus", 11.5, NOTE)
# 1 kOhm in, node, 100 kOhm down
res_h(bx + 8, 270, y, "1 kΩ")
dot(270, y)
res_v(270, y, y + 120, "100 kΩ", left=True)
ground(270, y + 120)
text(270, y + 165, "an unplugged jack reads low", 11.5, NOTE)
# buffer triangle
tx0, tx1 = 340, 420
line(270, y, tx0, y)
a(f'<polygon points="{tx0},{y-34} {tx1},{y} {tx0},{y+34}" fill="#ecfeff" stroke="{BUF}" stroke-width="2.4"/>')
text(tx0 + 8, y + 5, "A", 12, BUF, "start", "bold")
text(tx1 + 6, y - 8, "Y", 12, BUF, "start", "bold")
text((tx0 + tx1) / 2, y - 48, "¼ SN74AHC125", 13, BUF, weight="bold")
text((tx0 + tx1) / 2, y - 64, "a buffer with its own enable", 11.5, NOTE)
# OE: bubble at the triangle's lower side, down to the DIP switch and the pull-up
ox, oy = (tx0 + tx1) / 2, y + 17
a(f'<circle cx="{ox}" cy="{oy + 6}" r="5" fill="#fff" stroke="{BUF}" stroke-width="2"/>')
text(ox + 10, oy + 30, "OE (active low)", 11.5, BUF, "start", "bold")
line(ox, oy + 11, ox, 330, BUF)
dot(ox, 330, BUF)
poly([(ox, 330), (ox + 90, 330)], BUF)
res_v(ox + 90, 270, 330, "10 kΩ", BUF)
rail(ox + 90, 270, "3.3 V")
# DIP switch from OE to ground
sy0, sy1 = 360, 410
line(ox, 330, ox, sy0, BUF)
a(f'<circle cx="{ox}" cy="{sy0}" r="3.5" fill="#fff" stroke="{GND}" stroke-width="2"/>')
a(f'<circle cx="{ox}" cy="{sy1}" r="3.5" fill="#fff" stroke="{GND}" stroke-width="2"/>')
line(ox, sy0 + 4, ox + 22, sy1 - 14, GND, 2.2)
line(ox, sy1 + 4, ox, sy1 + 16, GND)
ground(ox, sy1 + 16)
text(ox - 16, (sy0 + sy1) / 2 + 5, "DIP switch n", 13, "#111", "end", "bold")
# switch meaning
a(f'<rect x="20" y="452" width="440" height="104" rx="8" fill="#f8fafc" stroke="#cbd5e1"/>')
text(36, 478, "ON: OE is low, the buffer drives the pin: the input is its BNC.", 12.5, "#111", "start")
text(36, 498, "OFF: the 10 kΩ holds OE high, the buffer lets go of the pin", 12.5, "#111", "start")
text(36, 518, "(high impedance): the input is its screw terminal, as on a", 12.5, "#111", "start")
text(36, 538, "board with no input board.", 12.5, "#111", "start")
# 1 kOhm out to the node
res_h(tx1, 600, y, "1 kΩ")
dot(600, y)
line(600, y, 860, y)
a(f'<rect x="860" y="{y-16}" width="70" height="32" rx="4" fill="#fff" stroke="{IN}" stroke-width="2"/>')
text(895, y + 5, "A n", 13, IN, weight="bold")
text(895, y - 28, "the adapter's", 11.5, NOTE)
text(895, y + 38, "terminal (one wire)", 11.5, NOTE)
# sensor terminal block off the node
line(600, y, 600, 300)
tbx, tby = 600, 300
for i, (lab, col) in enumerate((("S", IN), ("+", PWR), ("−", GND))):
    a(f'<rect x="{tbx + i*40 - 16}" y="{tby}" width="32" height="30" rx="3" fill="#fff" stroke="{col}" stroke-width="2"/>')
    text(tbx + i * 40, tby + 21, lab, 14, col, weight="bold")
line(640, tby + 30, 640, tby + 56, PWR)
text(640, tby + 72, "5 V", 12.5, PWR, weight="bold")
line(680, tby + 30, 680, tby + 50, GND)
ground(680, tby + 50)
text(640, tby + 112, "sensor n's terminal block (signal, power, ground):", 12, NOTE)
text(640, tby + 128, "beam breaks, nose pokes, lick sensors, with DIP switch n OFF", 12, NOTE)
# levels box
a(f'<rect x="480" y="452" width="500" height="104" rx="8" fill="#f0fdf4" stroke="#86efac"/>')
text(496, 478, "BNC inputs take logic levels from 0 to 5.5 V: 3.3 V or 5 V TTL.", 12.5, "#14532d", "start")
text(496, 498, "High above 2.1 V, low below 0.9 V; an edge must change faster", 12.5, "#14532d", "start")
text(496, 518, "than 100 ns per volt (any logic output does). Delay under", 12.5, "#14532d", "start")
text(496, 538, "10 ns. Each buffer chip: 3.3 V on pin 14, ground on 7, 100 nF.", 12.5, "#14532d", "start")
a('</svg>')
open(sys.argv[1], "w").write("\n".join(o))
