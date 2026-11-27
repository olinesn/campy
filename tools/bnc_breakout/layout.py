"""The BNC breakout's box and jack layout, shared by its drilling template and its drill jig (docs/bnc-breakout.md).

Millimetres, from the lid's centre: x toward the USB-C side (right), y toward the back.
"""
BOX = "Hammond 1590D"
LID_W, LID_D, LID_R = 187.75, 119.5, 5.0   # Hammond's 1590D drawing; the corner screws sit about 5 mm in from both edges
SCREWS = [(x, y) for x in (-89.0, 0.0, 89.0) for y in (-55.0, 55.0)]   # six 6-32 cover screws, 4.0 mm holes in the lid
HOLE = 9.5          # each jack's hole
PITCH = 22.0        # between jacks in a row
IN_Y, OUT1_Y, OUT2_Y = 34.0, 1.0, -32.0   # the rows: inputs at the back, then the outputs in two rows of five

# (x, y, pin, role, kind): kind sets the colour, "in", "out", "cam" or "sync"
JACKS = [((i - 3.5) * PITCH, IN_Y, f"A{i}", f"IN {i}", "in") for i in range(8)]
JACKS += [((i - 2) * PITCH, OUT1_Y, f"D{2 + i}", f"OUT {i}", "out") for i in range(5)]
JACKS += [((i - 2) * PITCH, OUT2_Y, pin, role, kind) for i, (pin, role, kind) in enumerate(
    [("D7", "OUT 5", "out"), ("D8", "OUT 6", "out"), ("D9", "OUT 7", "out"), ("D10", "CAM", "cam"), ("D13", "SYNC", "sync")])]
