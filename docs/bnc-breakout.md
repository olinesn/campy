# BNC breakout for the Nano ESP32

In short: a small box that turns an Arduino Nano ESP32 on the **Arduino Nano Screw Terminal Adapter** (ASX00037) into ten BNC outputs. Eight are the outputs board's channels (OUT 0–7), one is the camera line (CAM) and one is the sync output for other recorders (SYNC). Every jack is wired the same way: a **47 Ω resistor in series** and a **10 kΩ resistor from the centre pin to ground**. The box's sensors (beam breaks, nose pokes, lick sensors) and servos stay on the adapter's screw terminals and leave through a cable gland. One box suits all three ways campy uses the board: the camera trigger (`trigger.ino`), the outputs or operant-box board (`campy_outputs`), and the one board that does both ([the design](design/single-board-box.md)).

> **Status: a design, agreed on 17 November 2026; not built or tested yet.** Its numbers come from the parts' datasheets and from measurements on a 4-camera rig, each cited where it is used.

![Wiring of the BNC breakout: the screw terminal adapter turned so its D2–D12 row faces the BNC jacks. D2 to D9 (OUT 0 to 7) and D10 (CAM) each run through a 47 Ω resistor to a BNC jack's centre pin, with a 10 kΩ resistor from that pin to a ground bus; D13 (SYNC) comes round from the adapter's other row the same way. The ground bus goes to the GND terminal next to D2. On the other row, inputs 0 to 7 (A0 to A7), 5 V, 3.3 V and GND go to the sensor cables; REF, the RST on that row, VIN, TX1 and RX0 stay empty; D11 and D12 are servo signals; the RST next to TX1 can take a recessed reset button.](images/bnc-breakout-wiring.svg)

## The jacks

The labels name the Nano's pin and the role campy's sketches give it by default. The computer sets every pin when it starts, so a rig's config or task file must use these pins for the labels to hold.

| Jack | Label | Outputs or box board (`campy_outputs`) | One board for both (`campy-box --camera-fps`) | Trigger board (`trigger.ino`) |
|---|---|---|---|---|
| D2–D9 | OUT 0–7 | channels 0–7, the sketch's defaults (`campy-rules` outputs, `campy-box` outputs) | the same | free (an OUT jack can take `blinkPin`, the `campy-synctest` LED) |
| D10 | CAM | camera trigger **in**: the trigger board's camera line, through a BNC T | camera pulses **out**: `--camera-pins 10` | camera pulses out: `digitalPins: [10]` |
| D13 | SYNC | (none) | sync output: `--sync-pin 13` | sync output: `syncPin: 13` |

- **D13 also lights the Nano's own LED**, so the LED blinks with each sync pulse while the sync output runs.
- **Configs that use other pins** need changing if their rig moves to this box: `configs/campy_config_evox2.yaml` puts the camera trigger on D6 (`digitalPins: [6]`), which this box labels OUT 4.
- **More than 4 cameras** need a second camera line. With one board, `campy-box` takes up to 4 camera pins: put the second on an OUT jack you don't use (not D8 or D9, see below) and relabel it.
- **D11 and D12** are the suggested servo pins ([Operant boxes](operant-boxes.md#wiring-arduino-nano-esp32)), so they stay on screw terminals.

## The two resistors on every jack

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

## What a jack can drive

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

## Pins left empty

| Terminal on the adapter | On a Nano ESP32 | Why it stays empty |
|---|---|---|
| REF (analog row) | boot pin B0 (GPIO46) | The adapter's labels are for the classic Nano. On the Nano ESP32 this is a boot pin. |
| RST on the analog row (between 5V and GND) | boot pin B1 (GPIO0) | Also a boot pin: held at ground during a reset, it starts the chip's own bootloader instead of the sketch. The real reset is the RST on the other row. |
| TX1, RX0 (D1, D0) | the chip's serial port 0 | D1 may carry the chip's boot messages at every reset, which a device would see as pulses. |
| VIN | 6–21 V power input | The USB cable powers the board. |

The **RST next to TX1** is the board's reset. A push button from it to GND, recessed so that nobody presses it by accident, saves opening the box. A reset stops everything the board does, the camera pulses included. Two quick presses start the Nano's bootloader, for a board that no longer answers over USB.

## Sensors and servos (screw terminals)

Beam breaks, nose pokes and lick sensors need power, ground and a signal wire. A BNC jack carries two, so they stay on the adapter's analog row. Their cables come in through a cable gland in the back wall.

- **Signals:** inputs 0–7 on A0–A7 (`in` commands, or `inputs:` in a task or rules file).
- **Power:** 5 V (this terminal is the USB cable's 5 V on a Nano ESP32) or 3.3 V, and GND. Fan a supply out to several sensors with a lever connector. A short in a sensor cable can reset the board. With one board for both, a reset also stops the cameras, so on that board power the sensors from a supply of their own and join its ground to the board's.
- **Long sensor cables:** beam breaks with an open-collector output (most IR receivers) use the input's own pull-up (`pull: up`, about 45 kΩ). For a long cable, add a 10 kΩ pull-up per input. Put one leg in the input's terminal with the sensor wire. Put the other legs together in a lever connector, with a wire from the 3.3V terminal.
- **Servos:** signal wires on D11 and D12; power from their own 5–6 V supply, with its ground joined to the board's at one GND terminal ([Operant boxes](operant-boxes.md#wiring-arduino-nano-esp32)).

## The box: a Hammond 1590C, jacks on the lid

![Drilling template for the Hammond 1590C lid at 1:1: ten 9.5 mm holes, 22 mm apart, in two rows of five. The back row reads D2 OUT 0, D3 OUT 1, D4 OUT 2, D5 OUT 3, D6 OUT 4; the front row D7 OUT 5, D8 OUT 6, D9 OUT 7, D10 CAM, D13 SYNC. The front of the box is where the adapter's D2–D12 terminals face, the USB-C side is on the right, and the cable gland is in the back wall. A 50 mm scale bar checks the print.](images/bnc-breakout-panel.svg)

The Hammond 1590C is a diecast aluminium box of about 120 × 94 × 55 mm (sellers list 53 to 56 mm high). The jacks go on its lid, in two rows of five, 22 mm apart, with room for their labels. The adapter sits on the floor under them, its D2–D12 row facing the front and the Nano's USB-C toward the right wall.

- **The template** above is drawn at 1:1. Open [the SVG file](images/bnc-breakout-panel.svg) on its own and print it at 100 %. Check the 50 mm scale bar with a ruler, then centre it on the lid. Its outline is the lid's nominal size.
- **Height.** The jacks reach about 20 mm under the lid and the Nano stands about 20 mm above the floor on 6 mm standoffs, so they clear each other. Check with your parts before drilling: hold a jack against the lid with the adapter in place.
- **Grounds.** A ground bus wire joins every jack's ground lug, so the box doesn't have to carry the ground. If the jacks touch the metal lid, the box is at ground too. Stand it on rubber feet so that it doesn't also touch a metal table or rack.

## Parts list

For one box with ten jacks.

| Part | Qty | Example | Notes |
|---|---|---|---|
| Arduino Nano ESP32 with headers | 1 | ABX00083 | |
| Arduino Nano Screw Terminal Adapter | 1 | ASX00037 | 70 × 50.5 mm, four 3.2 mm holes, 3.5 mm screw terminals for 16–30 AWG wire |
| BNC jack, panel mount, 50 Ω, solder cup, rear nut | 10, plus a spare | Amphenol RF 31-10 | needs a ground solder lug under its nut (add one if it comes without) |
| Resistor, 10 kΩ, ¼ W | 10 | any metal film, 1 % | plus 8 more for the optional input pull-ups |
| Resistor, 47 Ω, ¼ W | 10 | any metal film, 1 % | |
| Diecast box | 1 | Hammond 1590C | about 120 × 94 × 55 mm |
| M3 standoffs, 6 mm, with screws | 4 | nylon or brass | for the adapter |
| Cable gland | 1 | PG7 or PG9 | for the sensor and servo cables |
| Rubber feet | 4 | | |
| Stranded hookup wire, 22–24 AWG, several colours | about 3 m | | |
| Bare tinned copper wire, 20–22 AWG | 30 cm | | the ground bus |
| Heat-shrink tubing, 2–3 mm | 30 cm | | |
| Lever connectors | 2 | WAGO 221 | sensor power, and the input pull-ups if fitted |
| USB-C panel-mount extension, USB 2.0 | 1, optional | | otherwise a slot in the right wall for the cable's plug |
| Momentary push button, recessed | 1, optional | | the reset button |
| Labels | | a label maker, or the template printed on label paper | |

Tools: a 2.5 mm flat screwdriver, a soldering iron, a wire stripper, a step drill (9.5 mm holes; 12.5 mm for a PG7 gland), an M3 drill and a multimeter.

## Building it

1. **Drill the lid.** Print the template at 100 %, check its scale bar, tape it centred on the lid, centre-punch the ten holes and drill them to 9.5 mm. Deburr.
2. **Drill the box.** A hole for the cable gland in the back wall. A slot for the USB-C plug, or a hole for the panel-mount extension, in the right wall where the Nano's USB-C will be. Four M3 holes in the floor for the adapter: mark them through the adapter, with its D2–D12 row toward the front.
3. **Mount the jacks** on the lid, each with its ground lug under the nut and all the lugs pointing the same way.
4. **Solder each jack the same way:**
   - the 10 kΩ from the centre solder cup to the jack's own ground lug, legs short, with heat-shrink over the leg at the centre cup;
   - one leg of the 47 Ω into the centre cup too;
   - a 15 cm wire to the 47 Ω's free leg, with heat-shrink over the resistor and the joint;
   - a label on the wire's far end: D2 … D10, D13.
5. **The ground bus:** a bare tinned wire through every ground lug, soldered at each, and a 15 cm black wire from it.
6. **Check before connecting anything**, with a multimeter:
   - each jack's centre to its lug: 10 kΩ;
   - each jack's centre to its wire's far end: 47 Ω;
   - one jack's centre to another's: about 20 kΩ, through the two pull-downs, never 0;
   - every lug to the black wire: 0 Ω.
7. **Mount the adapter** on its standoffs, without the Nano.
8. **Screw in the jack wires:**
   - D2–D10 into the D2–D10 terminals;
   - D13 into the D13 terminal on the other row;
   - the ground bus into the GND terminal next to D2.
   
   Strip 6 mm and twist. Don't tin the wire: tinned wire loosens under a screw. Bootlace ferrules (0.25–0.34 mm²) are better if you have a crimper. Tug each wire.
9. **Sensors and servos** come in through the gland to the analog row: signals on A0–A7, sensor power on 5V or 3.3V and GND, servo signals on D11 and D12, the servo supply's ground on a GND terminal.
10. **Leave empty:** REF, the RST on the analog row, VIN, TX1 and RX0. The optional reset button goes from the RST next to TX1 to a GND terminal.
11. **Plug in the Nano**, its USB-C toward the right wall. Close the lid and stick the labels on.
12. **Test.** Unplugged, every jack reads 0 V. Then flash `campy_outputs` ([Closed-loop outputs](closed-loop.md#uploading-the-sketch)) and send commands from a serial terminal:
    - `on 0`: the D2 jack reads about 3.3 V;
    - `off 0`: back to 0 V;
    - the same for channels 1–7 (D3–D9);
    - `alloff` at the end.

    D10 and D13 go high only when the board runs the cameras or the sync output (`cam`, `camsync`). For `trigger.ino`, the jacks stay at 0 V until campy-acquire starts.
