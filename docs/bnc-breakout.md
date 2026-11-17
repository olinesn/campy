# BNC breakout for the Nano ESP32

In short: a box that gives an Arduino Nano ESP32 on the **Arduino Nano Screw Terminal Adapter** (ASX00037) eighteen BNC jacks, **ten outputs and eight inputs**, in a Hammond 1590D.

- **The outputs:** the outputs board's channels (OUT 0–7), the camera line (CAM) and the sync output for other recorders (SYNC). Each has a **47 Ω resistor in series** and a **10 kΩ resistor from the centre pin to ground**.
- **The inputs** (IN 0–7, on A0–A7) go through a small **input board**: a 5 V-tolerant buffer per input, which takes 3.3 V or 5 V logic. A **DIP switch** sets, for each input, whether it comes from its BNC jack or from its screw terminal. Beam breaks, nose pokes and lick sensors use the screw terminals, because they need power and ground as well as a signal.
- **Servos** stay on the adapter's screw terminals.
- **One box suits all three ways campy uses the board:** the camera trigger (`trigger.ino`), the outputs or operant-box board (`campy_outputs`), and the one board that does both ([the design](design/single-board-box.md)).

> **Status: a design, agreed on 17 November 2026; the inputs and the larger box were added the same day. Not built or tested yet.** Its numbers come from the parts' datasheets and from measurements on a 4-camera rig, each cited where it is used.

![Wiring of the BNC breakout. At the top, the eight input BNCs (IN 0 to 7) feed the input board, whose eight buffers each have a 1 kΩ resistor in, a 100 kΩ resistor to ground and a 1 kΩ resistor out; a DIP switch enables each buffer, and each input has a sensor terminal (S) on the board. The input board's outputs go to the adapter's A0 to A7 terminals, and it takes 5 V, 3.3 V and GND from the adapter. Below, the screw terminal adapter is turned so its D2–D12 row faces the output jacks: D2 to D9 (OUT 0 to 7) and D10 (CAM) each run through a 47 Ω resistor to a BNC jack's centre pin, with a 10 kΩ resistor from that pin to a ground bus; D13 (SYNC) comes round from the adapter's other row the same way. The ground bus goes to the GND terminal next to D2. REF, the RST on the analog row, VIN, TX1 and RX0 stay empty; D11 and D12 are servo signals; the RST next to TX1 can take a recessed reset button.](images/bnc-breakout-wiring.svg)

## The jacks

The labels name the Nano's pin and the role campy's sketches give it by default. The computer sets every pin when it starts, so a rig's config or task file must use these pins for the labels to hold.

| Jack | Label | Outputs or box board (`campy_outputs`) | One board for both (`campy-box --camera-fps`) | Trigger board (`trigger.ino`) |
|---|---|---|---|---|
| A0–A7 | IN 0–7 | inputs 0–7, when their DIP switch is ON (`in` commands, or `inputs:` in a task or rules file) | the same | not used |
| D2–D9 | OUT 0–7 | channels 0–7, the sketch's defaults (`campy-rules` outputs, `campy-box` outputs) | the same | free (an OUT jack can take `blinkPin`, the `campy-synctest` LED) |
| D10 | CAM | camera trigger **in**: the trigger board's camera line, through a BNC T | camera pulses **out**: `--camera-pins 10` | camera pulses out: `digitalPins: [10]` |
| D13 | SYNC | (none) | sync output: `--sync-pin 13` | sync output: `syncPin: 13` |

- **The board has eight inputs in all.** IN 0–7 and the sensor screw terminals share them: each input is its BNC or its screw terminal, as its DIP switch says, never both at once.
- **D13 also lights the Nano's own LED**, so the LED blinks with each sync pulse while the sync output runs.
- **Configs that use other pins** need changing if their rig moves to this box: `configs/campy_config_evox2.yaml` puts the camera trigger on D6 (`digitalPins: [6]`), which this box labels OUT 4.
- **More than 4 cameras** need a second camera line. With one board, `campy-box` takes up to 4 camera pins: put the second on an OUT jack you don't use (not D8 or D9, see below) and relabel it.
- **D11 and D12** are the suggested servo pins ([Operant boxes](operant-boxes.md#wiring-arduino-nano-esp32)), so they stay on screw terminals.

## The two resistors on every output jack

**10 kΩ from the centre pin to ground (a pull-down).** It holds the jack at 0 V whenever the board isn't driving it.

- At power-up, at a reset, while a sketch is uploaded and in the bootloader, the ESP32-S3's pins behind D2–D13 drive nothing and have no pull-up or pull-down (ESP32-S3 datasheet, table 2-1). They float until the sketch sets them. A floating line on a few metres of BNC cable can read as "on" to a laser, LED or valve driver.
- `campy_outputs` drives D2–D9 low as soon as it starts and holds D10 down with the chip's weak internal pull-down; D13 floats until the computer sets it up. `trigger.ino` drives its pins only once campy-acquire sends its settings, so they float from power-up until a recording starts.
- It costs 0.33 mA while the output is high, against the pin's 40 mA.
- A device that pulls its own input up can still lift a floating line. The cameras' non-isolated inputs (Line3) probably do: on the 4-camera rig below, a small current flowed back into the driving pin while it was low. That is harmless, because the cameras only take frames during a recording, and a board reset ends the recording anyway.

**47 Ω in series.** Between the pin and the jack.

- **It stops the cable ringing.** On the 4-camera rig, a camera cable driven straight from a Raspberry Pi Pico rang as each pulse fell, and the Pico's own input counted a second rise on every pulse. A 50 Ω resistor in series stopped it: 0 extra rises in 34,800 pulses ([ChronicRecord timer](../chronicrecord/README.md)). 47 Ω is the nearest common value. `campy_outputs` counts the camera trigger's rises on D10, so a clean edge matters here too.
- **It limits the current** if a jack is shorted or plugged into something that drives it. Don't leave a short on: the pin's rating is 40 mA (Nano ESP32 datasheet).
- **It costs little.** Four cameras' Line3 inputs drew about 2.6 mA together on the same rig (the 50 Ω resistor lowered their line from 3.10 to 2.97 V). Through 47 Ω that is about 0.12 V.

**Why not 10 kΩ in series.** It would starve most of the things on the other end:

- A Blackfly S opto-isolated input (Line0) needs 3.5–7 mA. Through 10 kΩ, a 3.3 V pin gives at most 0.33 mA.
- A Blackfly S non-isolated input (Line3) counts 2.6 V or more as high and 1.4 V or less as low, and four of them drew about 2.6 mA. Through 10 kΩ the line would sit in neither range.
- Driver modules with an opto-isolated TTL input need a few mA too.
- 10 kΩ with a 3 m cable's capacitance (about 300 pF) slows every edge by microseconds, which moves the camera's trigger time.

## What an output jack can drive

The outputs are 3.3 V logic: high is 3.3 V, and the pin can give up to 40 mA (sink 28 mA).

| On the other end | Works? | Notes |
|---|---|---|
| FLIR Blackfly S, Line3 (non-isolated input) | Yes, up to 4 cameras per jack | campy's rigs trigger on Line3. With a Pico, a weaker driver than the ESP32, 4 cameras through 50 Ω read 2.97 V high; they need 2.6 V. 8 cameras on one jack is not tested. |
| FLIR Blackfly S, Line0 (opto-isolated input) | 1–2 cameras per jack | Each needs 3.5–7 mA. Prefer Line3. |
| Recording systems' digital or analog inputs (an ephys acquisition box, a photometry console) | Yes | High-impedance inputs. Check that the device counts 3.3 V as high. |
| LED and laser drivers' TTL inputs | Yes, if high-impedance | An input terminated in 50 Ω (some laser modulation inputs) would need 66 mA: use a line driver. |
| Solenoid valves, pumps | Through a driver only | A logic-level MOSFET module or valve driver, with a flyback diode across the coil and its own supply. Never a coil on a pin. |
| A cue LED | Yes | With its own 270–330 Ω resistor at the LED, under 10 mA. |
| A device that pulls its input up to 5 V | No | It would push current into the 3.3 V pin. Use a level shifter. |

**D8 and D9 drive more weakly.** The ESP32-S3's default drive strength is 10 mA on the pins behind D8 and D9 (GPIO17 and GPIO18), and 20 mA on the others (ESP32-S3 datasheet). That matters only for a jack that drives several cameras: put camera lines on other jacks.

## The inputs: BNC or screw terminal

![One input of the input board: the IN n BNC jack, its shell to ground, then 1 kΩ to the buffer's input, with 100 kΩ from there to ground; the buffer is a quarter of an SN74AHC125; its output enable (OE, active low) has a 10 kΩ pull-up to 3.3 V and DIP switch n to ground; the buffer's output goes through 1 kΩ to a node that runs to the adapter's A n terminal and to sensor n's terminal block (S, + at 5 V, − at ground). ON: the buffer drives the pin from the BNC. OFF: it lets go, and the screw terminal has the pin.](images/bnc-breakout-inputs.svg)

`campy_outputs` reads up to eight inputs, and this box puts them on A0–A7. Each comes from its BNC jack or from its screw terminal on the input board, and the input board's DIP switch picks which. Position 1 is IN 0, and position 8 is IN 7.

- **ON: the BNC.** The buffer drives the A pin from the jack.
- **OFF: the screw terminal.** The buffer lets go of the pin, so its output is high-impedance. The pin then belongs to the terminal, as on a board without the input board.

Per input, the input board has:

- **1 kΩ from the jack to the buffer.** It limits the current if a jack gets more than the buffer takes, or less than 0 V.
- **100 kΩ from the buffer's input to ground,** so an unplugged jack reads low: inactive, for an input that is active high.
- **A quarter of an SN74AHC125.** The chip has four buffers, each with its own enable (OE, active low), so two chips cover the eight inputs.
  - TI's datasheet: powered at 3.3 V, its inputs still take 0–5.5 V, so it reads 3.3 V and 5 V logic alike. It counts above 2.1 V as high and below 0.9 V as low.
  - Its delay is under 10 ns. The board stamps input edges to a few microseconds, so the buffer adds nothing measurable.
- **1 kΩ from the buffer to the A pin.** If a sensor is also wired to that input's terminal while its switch is ON, the buffer and the sensor fight through this resistor, at no more than 3.3 mA, which harms neither. The input then reads whichever of the two wins.
- **The enable:** a 10 kΩ pull-up to 3.3 V holds OE high, so the buffer is off. The DIP switch, when ON, connects OE to ground, turning the buffer on.

What a BNC input takes:

| On the other end | Works? | Notes |
|---|---|---|
| A 3.3 V or 5 V logic output: a TTL out from an ephys or photometry system, another Arduino, a stimulus computer | Yes | The input reads high while the signal is high. |
| A signal above 5.5 V (12 or 24 V, as from some industrial controllers) | No | It would damage the buffer. Bring it down to 5 V first, with a divider or an optocoupler. |
| A slow edge: an RC-filtered output, or an optocoupler's output with a weak pull-up | Check | The buffer needs an edge faster than 100 ns per volt, about 0.3 µs from 0 to 3.3 V. A slower edge can make extra edges; the input's lockout (`debounce_ms`) hides a few. |
| An open-collector output or a contact (a relay, a switch) | Not on a BNC | It needs a pull-up, and the BNC input pulls down. Use the screw terminal, with `pull: up`. |

In a task or rules file, an input on its BNC takes `pull: down`. The buffer drives the pin either way, so the pull only matters for which level is active. `active` defaults to high, for a signal that goes high on the event:

```yaml
inputs:
  laser_on: {input: 5, pin: A5, pull: down}   # DIP switch 6 ON: from the IN 5 jack
```

**Grounds.** Each BNC input's shell joins another instrument's ground to the box's, and through the Nano's USB cable to the computer's. With several instruments, that can make ground loops, which can add noise to analog recordings such as ephys or photometry. If it does, an opto-isolated input for that instrument breaks the loop. That isn't part of this design.

## Sensors and servos (screw terminals)

Beam breaks, nose pokes and lick sensors need power, ground and a signal wire, so they go on the input board's terminal blocks. Each input has three terminals: **S** (signal), **+** (5 V) and **−** (GND). Set that input's DIP switch OFF. The sensor cables come in through a cable gland in the back wall.

- **Signals:** input n's S terminal is A n (`in` commands, or `inputs:` in a task or rules file).
- **Power:** the + terminals carry the adapter's 5 V, which on a Nano ESP32 is the USB cable's 5 V. A 3.3 V sensor takes its power from the adapter's 3.3V terminal instead. A short in a sensor cable can reset the board. With one board for both, a reset also stops the cameras, so on that board power the sensors from a supply of their own and join its ground to the board's.
- **Long sensor cables:** beam breaks with an open-collector output (most IR receivers) use the input's own pull-up (`pull: up`, about 45 kΩ). For a long cable, solder a 10 kΩ resistor on the input board from that input's S terminal to 3.3 V.
- **Servos:** signal wires on the adapter's D11 and D12 terminals. Power them from their own 5–6 V supply, with its ground joined to the board's at one GND terminal ([Operant boxes](operant-boxes.md#wiring-arduino-nano-esp32)).

## Pins left empty

| Terminal on the adapter | On a Nano ESP32 | Why it stays empty |
|---|---|---|
| REF (analog row) | boot pin B0 (GPIO46) | The adapter's labels are for the classic Nano. On the Nano ESP32 this is a boot pin. |
| RST on the analog row (between 5V and GND) | boot pin B1 (GPIO0) | Also a boot pin: held at ground during a reset, it starts the chip's own bootloader instead of the sketch. The real reset is the RST on the other row. |
| TX1, RX0 (D1, D0) | the chip's serial port 0 | D1 may carry the chip's boot messages at every reset, which a device would see as pulses. |
| VIN | 6–21 V power input | The USB cable powers the board. |

The **RST next to TX1** is the board's reset. A push button from it to GND, recessed so that nobody presses it by accident, saves opening the box. A reset stops everything the board does, the camera pulses included. Two quick presses start the Nano's bootloader, for a board that no longer answers over USB.

## The box: a Hammond 1590D, jacks on the lid

![Drilling template for the Hammond 1590D lid at 1:1: eighteen 9.5 mm holes, 22 mm apart. The back row has the eight inputs, IN 0 to IN 7 (A0 to A7). In front of it, two rows of five outputs: D2 OUT 0, D3 OUT 1, D4 OUT 2, D5 OUT 3, D6 OUT 4; then D7 OUT 5, D8 OUT 6, D9 OUT 7, D10 CAM, D13 SYNC. Six small circles mark the lid's cover screws. The front of the box is where the adapter's D2–D12 terminals face, the USB-C side is on the right, and the cable gland is in the back wall. A 50 mm scale bar checks the print.](images/bnc-breakout-panel.svg)

The Hammond 1590D is a diecast aluminium box of 187.75 × 119.5 × 56 mm, 51 mm deep inside (Hammond's drawing). The jacks go on its lid, 22 mm apart: the eight inputs in a row at the back, and the ten outputs in two rows of five in front of them.

Under the jacks, on the floor:
- **The adapter** sits on the right, its D2–D12 row facing the front and the Nano's USB-C toward the right wall.
- **The input board** sits on the left, its terminal blocks toward the back wall's cable gland.

- **The template** above is drawn at 1:1. Open [the SVG file](images/bnc-breakout-panel.svg) on its own and print it at 100 %. It fits on a Letter or A4 page in landscape.
  - Check the 50 mm scale bar with a ruler.
  - Line its six small circles up on the lid's six cover screws. Its outline is the lid's nominal size.
- **The drill jig**, if you have a 3D printer: [bnc-breakout-drill-jig.stl](cad/bnc-breakout-drill-jig.stl).
  - **What it is:** a plate the size of the lid, with the jacks' labels engraved and a 12 mm guide over each jack for a 3 mm pilot drill.
  - **How it lines up:** the lid's own six cover screws go through it and the lid into the box. So it lines up on the screw holes, not on the cast outline, and it holds the lid still while you drill.
  - **The screws:** each head sits in a 1 mm counterbore, leaving 2 mm of jig under it, so the original ½″ screws still reach the box's thread. 6-32 × ⅝″ screws reach more.
  - **Printing:** it needs a print bed of at least 190 × 122 mm. Print it flat, guides up, without supports, in PLA or PETG with 4 walls.
  - **Before use:** printed holes come out a little small, so turn the 3 mm drill through each guide by hand first.
  - **Where it comes from:** [`tools/bnc_breakout/jig.py`](../tools/bnc_breakout/jig.py) makes it from the same layout as the template ([`layout.py`](../tools/bnc_breakout/layout.py)). `uv run tools/bnc_breakout/jig.py out.stl` rebuilds it.
  - Not printed or tried yet.
- **Height.** The jacks reach about 20 mm under the lid. The Nano stands about 20 mm above the floor on 6 mm standoffs, and the input board about the same with its terminal blocks. So they clear each other with a few millimetres to spare. Check with your parts before drilling: hold a jack against the lid with the boards in place.
- **Grounds.** A ground bus wire joins every jack's ground lug, so the box doesn't have to carry the ground. If the jacks touch the metal lid, the box is at ground too. Stand it on rubber feet so that it doesn't also touch a metal table or rack.

## Parts list

For one box with eighteen jacks.

| Part | Qty | Example | Notes |
|---|---|---|---|
| Arduino Nano ESP32 with headers | 1 | ABX00083 | |
| Arduino Nano Screw Terminal Adapter | 1 | ASX00037 | 70 × 50.5 mm, four 3.2 mm holes, 3.5 mm screw terminals for 16–30 AWG wire |
| BNC jack, panel mount, 50 Ω, solder cup, rear nut | 18, plus a spare | Amphenol RF 31-10 | needs a ground solder lug under its nut (add one if it comes without) |
| Resistor, 47 Ω, ¼ W | 10 | any metal film, 1 % | the outputs |
| Resistor, 10 kΩ, ¼ W | 18 | any metal film, 1 % | 10 output pull-downs, 8 enable pull-ups; plus 8 more for the optional sensor pull-ups |
| Resistor, 1 kΩ, ¼ W | 16 | any metal film, 1 % | two per input |
| Resistor, 100 kΩ, ¼ W | 8 | any metal film, 1 % | one per input |
| Quad buffer with enables, 3.3 V supply, 5 V-tolerant inputs | 2 | TI SN74AHC125N (DIP-14) | |
| DIP-14 socket | 2 | | so a damaged buffer is a swap |
| Capacitor, 100 nF ceramic | 2 | | one at each buffer's supply pins |
| DIP switch, 8 positions | 1 | | through-hole |
| Prototyping board | 1 | about 70 × 90 mm, 2.54 mm pitch | the input board |
| PCB screw terminal blocks, 3.5 mm pitch | 24 positions | e.g. eight 3-position blocks | S, +, − for each input |
| Diecast box | 1 | Hammond 1590D | 187.75 × 119.5 × 56 mm |
| M3 standoffs, 6 mm, with screws | 8 | nylon or brass | four for the adapter, four for the input board |
| Cable gland | 1 | PG9 or PG11 | for the sensor and servo cables |
| Rubber feet | 4 | | |
| Stranded hookup wire, 22–24 AWG, several colours | about 5 m | | |
| Bare tinned copper wire, 20–22 AWG | 50 cm | | the ground bus |
| Heat-shrink tubing, 2–3 mm | 50 cm | | |
| USB-C panel-mount extension, USB 2.0 | 1, optional | | otherwise a slot in the right wall for the cable's plug |
| Momentary push button, recessed | 1, optional | | the reset button |
| Labels | | a label maker, or the template printed on label paper | |

Tools: a 2.5 mm flat screwdriver, a soldering iron, a wire stripper, a step drill (9.5 mm holes, and 15.5 mm for a PG9 gland), an M3 drill and a multimeter. For the drill jig: a 3D printer and a 3 mm drill.

## Building it

1. **Drill the lid**, with the jig or the paper template.
   - **With the jig:**
     1. Put the lid on the empty box and screw the jig on top with the lid's six cover screws.
     2. Pencil a mark on the box wall under the jig's FRONT edge. The rows aren't placed symmetrically, so the lid only goes back on one way round.
     3. Drill the eighteen 3 mm pilots through the guides at a low speed.
     4. Take the jig off and open each pilot to 9.5 mm with the step drill.
     5. Blow the chips out of the box.
   - **With the template:** print it at 100 % and check its scale bar. Tape it on the lid with its six small circles on the screws. Centre-punch the eighteen holes and drill them to 9.5 mm.
   - Deburr the holes.
2. **Drill the box.**
   - The back wall: a hole for the cable gland, on the left, behind the input board.
   - The right wall: a slot for the USB-C plug, or a hole for the panel-mount extension, where the Nano's USB-C will be.
   - The floor: four M3 holes for the adapter (right) and four for the input board (left). Mark each set through its board, with the adapter's D2–D12 row toward the front.
3. **Build the input board.** Use sockets for the two buffers (U1 for IN 0–3, U2 for IN 4–7).
   - **Each input:**
     - a 1 kΩ resistor from a pad for the jack's wire to the buffer's A pin;
     - a 100 kΩ resistor from that A pin to ground;
     - a 1 kΩ resistor from the buffer's Y pin to the input's node;
     - from the node: the input's S terminal, and a 20 cm wire for the adapter, labelled A0 … A7.
   - **The enables:** each OE pin to its DIP switch position, and through 10 kΩ to 3.3 V. The DIP switch's other side goes to ground.
   - **Each buffer's supply:** pin 14 to 3.3 V, pin 7 to ground, and a 100 nF capacitor from pin 14 to pin 7 at the chip.
   - **The terminal blocks:** every + terminal joined to a 5 V wire, every − terminal to ground.
   - **Three wires for the adapter:** 3.3 V, 5 V and GND.

   | Input | Chip | OE pin | A pin | Y pin | DIP position |
   |---|---|---|---|---|---|
   | IN 0 | U1 | 1 | 2 | 3 | 1 |
   | IN 1 | U1 | 4 | 5 | 6 | 2 |
   | IN 2 | U1 | 10 | 9 | 8 | 3 |
   | IN 3 | U1 | 13 | 12 | 11 | 4 |
   | IN 4 | U2 | 1 | 2 | 3 | 5 |
   | IN 5 | U2 | 4 | 5 | 6 | 6 |
   | IN 6 | U2 | 10 | 9 | 8 | 7 |
   | IN 7 | U2 | 13 | 12 | 11 | 8 |

4. **Mount the jacks** on the lid, each with its ground lug under the nut and all the lugs pointing the same way.
5. **Solder each output jack** (D2–D10, D13) the same way:
   - the 10 kΩ from the centre solder cup to the jack's own ground lug, legs short, with heat-shrink over the leg at the centre cup;
   - one leg of the 47 Ω into the centre cup too;
   - a 15 cm wire to the 47 Ω's free leg, with heat-shrink over the resistor and the joint;
   - a label on the wire's far end: D2 … D10, D13.
6. **Solder each input jack** (IN 0–7): a 20 cm wire from the centre cup, labelled IN 0 … IN 7. No resistors at these jacks: theirs are on the input board.
7. **The ground bus:** a bare tinned wire through all eighteen ground lugs, soldered at each, and two black wires from it: one for the adapter (15 cm), one for the input board (20 cm).
8. **Check before connecting anything**, with a multimeter, the buffers out of their sockets:
   - each output jack's centre to its lug: 10 kΩ;
   - each output jack's centre to its wire's far end: 47 Ω;
   - one output jack's centre to another's: about 20 kΩ, through the two pull-downs, never 0;
   - every lug to the black wires: 0 Ω;
   - on the input board, each buffer socket's pin 14 to pin 7: open, not a short.
9. **Mount the adapter and the input board** on their standoffs, without the Nano. Then screw in the wires:
   - D2–D10 into the D2–D10 terminals;
   - D13 into the D13 terminal on the other row;
   - the ground bus into the GND terminal next to D2;
   - each input jack's wire into its pad on the input board;
   - the input board's A0 … A7 wires into the A0–A7 terminals;
   - its 3.3 V, 5 V and GND wires into the 3.3V, 5V and a GND terminal.

   Strip 6 mm and twist. Don't tin the wire: tinned wire loosens under a screw. Bootlace ferrules (0.25–0.34 mm²) are better if you have a crimper. Tug each wire.
10. **Sensors and servos** come in through the gland. Sensors go on the input board's S, + and − terminals (their DIP switches OFF). Servo signals go on D11 and D12, and the servo supply's ground on a GND terminal.
11. **Leave empty:** REF, the RST on the analog row, VIN, TX1 and RX0. The optional reset button goes from the RST next to TX1 to a GND terminal.
12. **Put the buffers in their sockets and plug in the Nano**, its USB-C toward the right wall. Close the lid and stick the labels on.
13. **Test the outputs.** Unplugged, every output jack reads 0 V. Then flash `campy_outputs` ([Closed-loop outputs](closed-loop.md#uploading-the-sketch)) and send commands from a serial terminal:
    - `on 0`: the D2 jack reads about 3.3 V;
    - `off 0`: back to 0 V;
    - the same for channels 1–7 (D3–D9);
    - `alloff` at the end.

    D10 and D13 go high only when the board runs the cameras or the sync output (`cam`, `camsync`). For `trigger.ino`, the jacks stay at 0 V until campy-acquire starts.
14. **Test the inputs**, with the Nano on USB:
    - **DIP switch 1 ON, the IN 0 jack unplugged:** the A0 terminal reads 0 V.
    - **A wire from the 3.3V terminal to the IN 0 jack's centre:** A0 reads about 3.3 V. Do the same from the 5V terminal: A0 still reads about 3.3 V, never 5.
    - **DIP switch 1 OFF:** A0 no longer follows the jack.
    - **With the sketch:** switch DIP 1 back ON and send `in 0 100 2 1 0` (input 0 on A0, pull-down, active high, no lockout). The board answers `ok in 0 100 0`. Touch the 3.3V wire to the IN 0 jack again: the board says `i 0 1 …`, then `i 0 0 …` when you take it away.
    - Do the same for IN 1–7 (pins 101–107) with their switches.
