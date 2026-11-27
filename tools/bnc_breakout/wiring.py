"""The BNC breakout's wiring diagram (docs/images/bnc-breakout-wiring.svg).

    python3 tools/bnc_breakout/wiring.py docs/images/bnc-breakout-wiring.svg
"""
import sys

OUT, CAM, SYNC = "#1d6fb8", "#c2410c", "#7c3aed"
GND, IN, PWR, SERVO, NC = "#111111", "#15803d", "#dc2626", "#0f766e", "#9ca3af"
FONT = "Helvetica, Arial, sans-serif"

x0, s = 75, 58
def X(i): return x0 + s * i

# The adapter turned so its D2-D12 row faces the BNC panel (front, bottom of the drawing).
front = ["TX1", "RX0", "RST", "GND", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12", "GND"]
back = ["VIN", "GND", "RST", "5V", "A7", "A6", "A5", "A4", "A3", "A2", "A1", "A0", "REF", "3.3V", "D13", "GND"]

bncs = [("D%d" % (2 + c), "OUT %d" % c, OUT, X(4 + c)) for c in range(8)]
bncs.append(("D10", "CAM", CAM, X(12)))
bncs.append(("D13", "SYNC", SYNC, 856))

DY = 310   # the adapter and the outputs, drawn below the inputs
W, H = 1090, 800 + DY
o = []
a = o.append
a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" font-size="13">')
a(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
a('<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#555"/></marker></defs>')

def text(x, y, t, size=13, color="#111", anchor="middle", weight="normal", extra=""):
    a(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}" {extra}>{t}</text>')
def line(x1, y1, x2, y2, color, w=2.2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    a(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}" stroke-linecap="round"{d}/>')
def poly(pts, color, w=2.2):
    p = " ".join(f"{x},{y}" for x, y in pts)
    a(f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"/>')
def resistor(x, y1, y2, color):  # vertical, body in the middle
    m = (y1 + y2) / 2
    line(x, y1, x, m - 15, color)
    a(f'<rect x="{x-6}" y="{m-15}" width="12" height="30" rx="2" fill="#fff" stroke="{color}" stroke-width="2"/>')
    line(x, m + 15, x, y2, color)
def dot(x, y, color): a(f'<circle cx="{x}" cy="{y}" r="3.5" fill="{color}"/>')
def nc(x, y):
    line(x - 7, y - 7, x + 7, y + 7, NC, 2.4); line(x - 7, y + 7, x + 7, y - 7, NC, 2.4)

# Headings
INPUTS_TOP = None  # (drawn after the group below)
a(f'<g transform="translate(0,{DY})">')
text(24, 788, "FRONT: one BNC jack per output, on the lid (drawn in a row here; on the lid, two rows of five read D2 to D13)", 14, "#444", "start", "bold")

# Adapter outline
ay1, ay2 = 145, 488
a(f'<rect x="38" y="{ay1}" width="950" height="{ay2-ay1}" rx="10" fill="#f3f6f8" stroke="#64748b" stroke-width="2"/>')
text(52, 246, "Arduino Nano Screw Terminal Adapter (ASX00037), turned so its D2–D12 row faces the BNC panel", 12.5, "#475569", "start", "bold")

# Proto area (left, not connected)
for r in range(8):
    for c in range(9):
        a(f'<circle cx="{74 + c*24}" cy="{268 + r*20}" r="3" fill="none" stroke="#94a3b8" stroke-width="1.3"/>')
text(170, 425, "9 × 8 prototyping area", 12, "#64748b")
text(170, 439, "(holes only, not connected)", 12, "#64748b")

# Nano ESP32 in its socket
a('<rect x="380" y="270" width="548" height="104" rx="6" fill="#0f6e74" stroke="#0b4f53" stroke-width="2"/>')
text(654, 310, "Arduino Nano ESP32", 16, "#ffffff", weight="bold")
text(654, 332, "plugged into the adapter's socket", 12, "#d1fae5")
text(654, 350, "(its reset button and LEDs are here)", 12, "#d1fae5")
a('<rect x="928" y="304" width="38" height="36" rx="5" fill="#cbd5e1" stroke="#475569" stroke-width="2"/>')
text(947, 326, "USB-C", 10, "#111")
poly([(966, 322), (1030, 322)], "#475569", 5)
text(1040, 300, "USB-C cable", 12, "#475569", "end", "bold")
text(1040, 352, "to the computer", 12, "#475569", "end")

# Terminals
def terminal(x, y, label, color, sub=None):
    a(f'<rect x="{x-22}" y="{y}" width="44" height="30" rx="3" fill="#ffffff" stroke="{color}" stroke-width="2"/>')
    text(x, y + 20, label, 12, color, weight="bold")
    if sub: text(x, y + 44 if y > 300 else y - 8, sub, 10.5, "#64748b")
# Back row (A row) at top
ty = 158
for i, lab in enumerate(back):
    col = {"GND": GND, "5V": PWR, "3.3V": PWR, "D13": SYNC, "VIN": NC, "RST": NC, "REF": NC}.get(lab, IN)
    terminal(X(i), ty, lab, col)
# Front row (D row) at bottom
fy = 444
for i, lab in enumerate(front):
    col = {"GND": GND, "TX1": NC, "RX0": NC, "RST": "#475569", "D10": CAM, "D11": SERVO, "D12": SERVO}.get(lab, OUT)
    terminal(X(i), fy, lab, col)

# Back-row wiring: inputs, power, not-connected
BOARD_BOTTOM = 62   # the input board's lower edge, in this group's coordinates
for i in range(4, 12):
    line(X(i), ty, X(i), BOARD_BOTTOM, IN)
line(X(3), ty, X(3), BOARD_BOTTOM, PWR); text(X(3) + 6, 90, "5 V", 12, PWR, "start", "bold")
line(X(13), ty, X(13), BOARD_BOTTOM, PWR); text(X(13) + 6, 90, "3.3 V", 12, PWR, "start", "bold")
line(X(1), ty, X(1), BOARD_BOTTOM, GND); text(X(1) + 6, 90, "GND", 12, GND, "start", "bold")
for i in (0, 2, 12):
    nc(X(i), 142) if i else nc(X(i), 135)
text(X(2), 128, "B1", 11, NC, weight="bold")
text(X(12), 128, "B0", 11, NC, weight="bold")
text(52, 222, "On a Nano ESP32 this row’s “REF” is boot pin B0 and its “RST” is boot pin B1: leave both empty, and VIN (USB powers the board).", 12, "#64748b", "start")

# Front-row specials
for i in (0, 1):
    nc(X(i), 505)
text(X(0) + 29, 528, "TX1, RX0: empty", 11, NC)
text(X(0) + 29, 542, "(D1 sends boot text at reset)", 11, NC)
poly([(X(2), 474), (X(2), 585)], "#475569", 1.8)
a(f'<circle cx="{X(2)}" cy="{592}" r="7" fill="#fff" stroke="#475569" stroke-width="1.8"/>')
text(X(2) - 4, 618, "optional recessed", 10.5, "#475569")
text(X(2) - 4, 631, "reset button to GND", 10.5, "#475569")
for i, n in ((13, 0), (14, 1)):
    line(X(i), 474, X(i), 498, SERVO)
    text(X(i), 512, f"servo {n}", 11, SERVO, weight="bold")
text((X(13) + X(14)) / 2, 526, "(optional, wires out the back", 10.5, SERVO)
text((X(13) + X(14)) / 2, 539, "with the servos' own supply)", 10.5, SERVO)

# Channel circuits and BNCs
by, bus = 664, 718
pd = 24  # the pull-down's offset from the signal line
for (pin, role, col, bx) in bncs:
    if pin == "D13":
        # from the back row, around the right side
        poly([(X(14), ty), (X(14), 136), (1050, 136), (1050, 560), (bx, 560), (bx, 574)], SYNC)
        top = 574
    else:
        tx = bx
        top = fy + 30
    resistor(bx, top if pin == "D13" else top, 620, col)
    dot(bx, 620, col)
    poly([(bx, 620), (bx + pd, 620), (bx + pd, 640)], col, 2)
    resistor(bx + pd, 640, bus, GND)
    line(bx, 620, bx, by, col)
    a(f'<circle cx="{bx}" cy="{by}" r="17" fill="#e2e8f0" stroke="#334155" stroke-width="2.5"/>')
    a(f'<circle cx="{bx}" cy="{by}" r="6" fill="#fff" stroke="#334155" stroke-width="1.5"/>')
    a(f'<circle cx="{bx}" cy="{by}" r="2.6" fill="{col}"/>')
    line(bx, by + 17, bx, bus, GND, 2)
    dot(bx, bus, GND); dot(bx + pd, bus, GND)
    text(bx, bus + 24, pin, 14, col, weight="bold")
    text(bx, bus + 41, role, 12, col, weight="bold")
# ground bus to the GND terminal next to D2
line(X(3), fy + 30, X(3), bus, GND)
dot(X(3), bus, GND)
line(X(3), bus, bncs[-1][3] + pd, bus, GND, 3)
text(X(3) - 8, bus + 24, "ground", 11.5, GND, "end", "bold")
text(X(3) - 8, bus + 39, "bus", 11.5, GND, "end", "bold")
# Labels on the first channel
bx = bncs[-1][3]
text(bx + 12, 602, "47 Ω", 12.5, "#111", "start", "bold")
text(bx + pd + 12, 684, "10 kΩ", 12.5, "#111", "start", "bold")
# callout
a('<rect x="30" y="652" width="220" height="40" rx="6" fill="#fff7ed" stroke="#fdba74"/>')
text(140, 668, "Every output BNC: 47 Ω in series,", 12, "#7c2d12")
text(140, 684, "10 kΩ from centre to ground", 12, "#7c2d12")
a('</g>')

# The inputs: eight BNCs on the lid's back row, through the input board to A0-A7
text(24, 24, "BACK row of the lid: eight input BNCs (drawn here above the terminals they feed; on the lid they read IN 0 to IN 7 from left to right)", 14, "#444", "start", "bold")
iby, bus_in = 82, 46
bt, bb = 150, BOARD_BOTTOM + DY
for k in range(8):
    x, n = X(4 + k), 7 - k
    line(x, iby, x, bt, IN)
    poly([(x - 12, iby - 12), (x - 12, bus_in)], GND, 2)
    dot(x - 12, bus_in, GND)
    a(f'<circle cx="{x}" cy="{iby}" r="16" fill="#e2e8f0" stroke="#334155" stroke-width="2.5"/>')
    a(f'<circle cx="{x}" cy="{iby}" r="6" fill="#fff" stroke="#334155" stroke-width="1.5"/>')
    a(f'<circle cx="{x}" cy="{iby}" r="2.6" fill="{IN}"/>')
    text(x + 6, 122, f"IN {n}", 12.5, IN, "start", "bold")
line(X(4) - 12, bus_in, X(11) - 12, bus_in, GND, 3)
poly([(X(4) - 12, bus_in), (X(3) + 18, bus_in), (X(3) + 18, bt)], GND, 2.4)
text(X(3) + 10, bus_in - 6, "shells: ground", 11, GND, "end", "bold")
# the board
a(f'<rect x="40" y="{bt}" width="980" height="{bb - bt}" rx="10" fill="#f0fdf4" stroke="#16a34a" stroke-width="2"/>')
text(56, bt + 24, "Input board (perfboard)", 14, "#14532d", "start", "bold")
text(56, bt + 42, "per input: 1 kΩ in, 100 kΩ to ground,", 11.5, "#14532d", "start")
text(56, bt + 57, "a buffer (¼ SN74AHC125), 1 kΩ out", 11.5, "#14532d", "start")
text(56, bt + 72, "(one input in full: the next drawing)", 11.5, "#14532d", "start")
# DIP switch and its enable lines
oe_y = bt + 92
dx0 = 60
a(f'<rect x="{dx0}" y="{oe_y - 16}" width="180" height="32" rx="4" fill="#fff" stroke="#334155" stroke-width="2"/>')
for i in range(8):
    sx = dx0 + 12 + i * 21
    a(f'<rect x="{sx}" y="{oe_y - 9}" width="12" height="18" rx="2" fill="#e2e8f0" stroke="#334155" stroke-width="1.2"/>')
    a(f'<rect x="{sx + 2}" y="{oe_y - 7}" width="8" height="7" fill="#334155"/>')
text(dx0, oe_y + 34, "DIP switch, one per input:", 11.5, "#111", "start", "bold")
text(dx0, oe_y + 49, "ON = its BNC, OFF = its screw terminal", 11.5, "#111", "start")
line(dx0 + 180, oe_y, X(11), oe_y, "#0f6e74", 1.6, "5 4")
text(dx0 + 186, oe_y - 6, "OE", 10.5, "#0f6e74", "start", "bold")
for k in range(8):
    x = X(4 + k)
    a(f'<rect x="{x - 5}" y="{bt + 8}" width="10" height="24" rx="2" fill="#fff" stroke="{IN}" stroke-width="1.8"/>')
    a(f'<polygon points="{x - 15},{oe_y - 18} {x + 15},{oe_y - 18} {x},{oe_y + 14}" fill="#ecfeff" stroke="#0f6e74" stroke-width="2"/>')
    a(f'<rect x="{x - 5}" y="{oe_y + 24}" width="10" height="24" rx="2" fill="#fff" stroke="{IN}" stroke-width="1.8"/>')
    ny = oe_y + 64
    dot(x, ny, IN)
    line(x, ny, x + 13, ny, IN, 2)
    a(f'<rect x="{x + 13}" y="{ny - 9}" width="18" height="18" rx="2" fill="#fff" stroke="{IN}" stroke-width="1.8"/>')
    text(x + 22, ny + 4.5, "S", 11, IN, weight="bold")
text(X(4) - 4, bb - 10, "S: each sensor's signal terminal, with a + (5 V) and a − (GND) terminal beside it; sensor cables come in through a gland in the back wall", 11.5, "#14532d", "start")
text(X(11) + 30, bt + 22, "to A7 … A0", 11.5, IN, "start", "bold")
a('</svg>')
open(sys.argv[1], "w").write("\n".join(o))
