// campy outputs, for an Arduino Nano ESP32 (or another Arduino): the TTL
// outputs that campy-rules (campy/rules.py) switches from the animals' live
// poses, on a board of their own, next to the camera trigger's.
//
// The board watches the camera trigger on an input pin and counts its rising
// edges, frame 1, 2, ... from when campy-rules sets it up (trig, or reset).
// A pause in the trigger doesn't restart the count, as campy doesn't restart
// its frame count: it goes on, and the board says how long the pause was
// (gap). So frame k here stays campy's frame k through planned pauses (bouts),
// and a train's every/phase stays on campy's frame numbers. It says when each
// one rose, so every output can be lined up with the frames afterwards.
//
// Outputs are low at power-up. A channel is high while it is "on" (a level),
// or during a pulse: one a command asks for, or one of its train, which the
// board makes itself, locked to the camera trigger: a pulse of width_us,
// offset_us after the trigger of every frame k with (k - phase) % every == 0.
// So 3 ms photometry excitation pulses, 2 wavelengths on alternate frames,
// are exact to the board's clock whatever the host does; the host only says
// when a train runs. A train that is stopped finishes the pulse it is making.
//
// A command may name the frame it was decided on (k): the board then says
// how long after that frame's trigger the pin changed, and refuses (late) a
// command that came more than the bound after it, so no output ever follows
// a pose that old.
//
// Safety: if the host says nothing for the watchdog's time (a crash, a hang,
// an unplugged cable) while any output is on or running, everything goes low
// and stays low. A level is never on longer than its channel's max. Two
// channels of the same group are never high together: what would make the
// second one high is skipped and said (clash).
//
// An operant box (version 3) adds inputs, servos and reflexes, all off until the host sets them:
// - Inputs (0-7): beam breaks, nose pokes, TTL lick or touch sensors. Each edge is said with the time
//   the pin changed (the board's clock, to a few microseconds: the pins are read in every timing loop
//   too), the camera frame it fell in and how long after that frame's trigger. 1 is "active" (a beam
//   broken, a poke in), whichever level that is on the wire. Debouncing is a lockout: an edge is taken
//   at once, then the input is ignored for debounce_us; a change during the lockout is said once it
//   ends, with the time it happened. So the first edge is never late and a short pulse still has both
//   its edges.
// - Servos (0-3): hobby servos on the ESP32's PWM hardware, 50 Hz, a pulse of min_us to max_us.
//   A move goes at most speed microseconds of pulse a second (0: at once), and is said once there.
// - Reflexes (links 0-7): an input's edge makes a pulse, a level on or off, a train run or a servo
//   move, on the board, within a millisecond, as long as the host has armed the link (once, n times
//   or until disarmed).
// Safety: the watchdog and alloff also disarm every link and send every servo to its safe position
// (at its speed). An armed link and a servo away from its safe position count as active: the
// watchdog guards them as it guards an output that is on.
//
// One board for the cameras and the box (version 4, docs/design/single-board-box.md): the board can make the
// camera pulses itself, so that one Nano ESP32 does both jobs. They come from a hardware timer (the ESP32-S3's
// MCPWM), as trigger.ino's: a period of P microseconds, low for its first half and high for its second, so frame
// k (from 0) rises at k * P + P / 2 after time 0, and from the timer alone: nothing the sketch does moves an edge.
// The board's frames then come from it (not from a trigger pin): it knows each one's time, so its frame count and
// times are exact by design. An interrupt counts the rises and stops the timer at its planned count, at a period's
// end. The sync output for other recorders (trigger.ino's seeded random train, from the same time 0) comes from
// the board's own timed changes. Neither the watchdog nor alloff stops the cameras; camwatch can, after a long
// silence.
//
// Commands, one per line (\n or \r), each answered at once with a line
// starting "ok" or "err" (k: a frame number, optional; ch: channel 0-7). A
// command refused because its frame was too long ago is answered
// "err late <ch> <k> <after_us>"; one that would break its group, "err clash
// <ch> <k> <t_us>":
//   id                                -> ok campy_outputs <version>
//   pin <ch> <pin>                    channel ch on Arduino pin pin; -1 = none
//   trig <pin>                        the camera trigger's input pin; -1 = none
//   group <ch> <g>                    channels of the same group g (1-255) are never
//                                     high together; 0 = no group
//   max <ch> <us>                     a level stays on at most us; 0 = no limit
//   bound <us>                        refuse commands that come over us after their
//                                     frame's trigger; 0 = no bound
//   watchdog <ms>                     all off when nothing comes for ms (50-10000)
//   on <ch> [k]                       -> ok on <ch> <t_us> <k> <after_us>
//   off <ch>                          -> ok off <ch> <t_us>
//   pulse <ch> <width_us> [k]         -> ok pulse <ch> <t_us> <k> <after_us>
//   train <ch> <every> <phase> <offset_us> <width_us>
//                                     set the channel's train (while it is stopped)
//   run <ch> <n> [k]                  run its train for n pulses, -1 until stopped,
//                                     0 stop -> ok run <ch> <n> <t_us> <k> <after_us>
//   alloff                            every channel low, every train stopped
//   reset                             count the trigger's frames from 1 again -> ok reset
//   time                              -> ok time <t_us> <frame> <its rise_us>
//   hb                                nothing but keeping the watchdog quiet (no answer)
//   status                            -> ok status ...
//   in <i> <pin> <pull> <active> <debounce_us>
//                                     input i (0-7) on pin; pull 0 none, 1 up, 2 down; active 1 high,
//                                     0 low -> ok in <i> <pin> <its level now>; in <i> -1: none
//   servo <s> <pin> <min_us> <max_us> <safe_us> <speed>
//                                     servo s (0-3) on pin, starting at safe_us; speed: us of pulse a
//                                     second, 0 at once -> ok servo <s> <pin> <t_us>; servo <s> -1: none
//                                     (its pulses stop)
//   move <s> <us> [k]                 -> ok move <s> <us> <t_us> <k> <after_us>
//   link <l> <in> <edge> <what> <a> <b>
//                                     link l (0-7): input in's edge (1 to active, 0 to inactive) does
//                                     what: 1 pulse channel a for b us, 2 on a, 3 off a, 4 move servo
//                                     a to b us, 5 run channel a's train for b pulses. Disarmed.
//                                     link <l> -1: none
//   arm <l> <n>                       the link fires n more times, -1 until disarmed, 0 disarmed
//                                     -> ok arm <l> <n> <t_us>
//   box                               -> ok box t=... in=<active inputs> armed=<armed links> s0=<pos>/<target> ...
//   cam <period_us> <pin> [pin ...]   the camera pulses: their period (100-65535) and up to 4 pins; the frames
//                                     now come from them, not from the trigger pin -> ok cam <period_us> <pins>;
//                                     cam 0: none
//   camsync <pin> <seed> <width_us> <min_ms> <max_ms>
//                                     the sync output, as trigger.ino's -> ok camsync ...; camsync -1: none
//   camgo <n>                         start them now: n pulses, 0 until stopped -> ok camgo <n> <t0_us> <period_us>
//                                     (time 0, when the first period starts, on the board's clock)
//   camstop                           stop at a period's end, no pulse cut or added -> ok camstop <frames>
//   camwatch <ms>                     stop them after that long without a word from the host (0: never, the default)
//   camstatus                         -> ok camstatus running=.. frames=.. planned=.. hw=.. t0=.. period=..
// Pins: 0-48, or 100-107 for A0-A7.
// What it says on its own (t_us: the board's clock, microseconds since power-up):
//   ready campy_outputs <version>     at power-up
//   t <k> <t_us>                      the camera trigger rose: frame k
//   gap <k> <us>                      frame k came us after the one before (half a second or more)
//   p <ch> <k> <rise_us> <fall_us>    a pulse is over (k: the frame it was for, -1 none)
//   e <ch> <0|1> <t_us> <why>         a level changed by itself: max, wd
//   clash <ch> <k> <t_us>             a pulse skipped: its group had a channel high
//   wd <t_us>                         the watchdog fired: everything is low
//   i <in> <0|1> <t_us> <k> <after_us> an input changed at t_us (1: active), in frame k (0: none yet),
//                                     after_us after its trigger (-1: unknown)
//   x <l> <in> <t_us> <k> <done>      a link fired on its input's edge at t_us; done 0: what it does was
//                                     refused (the channel high, a clash, no pin)
//   m <s> <us> <t_us>                 a servo got where it was sent
//   camend <frames> <t_us>            the camera pulses ended (t_us: the last one's period's end)
//   camwd <t_us>                      camwatch stopped them (the host said nothing for that long)
//   camerr hw=<n> frames=<n>          the timer's count and the frames said differ
// Every line waits its turn in a queue and goes out as USB takes it, in pieces if need be, between the board's
// timed changes: never waited for. The Nano ESP32's USB serial buffer holds 64 bytes, so a line that didn't fit
// at once used to be dropped (status, about 110 characters, always was). Lines are dropped, and counted in
// "lost", only when the queue is full (a host that stopped reading), and the queue is emptied when the host
// closes the port, so the next one hears none of the last one's answers.

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(ESP32)
#include "soc/gpio_struct.h"
#endif
// The camera timer: the ESP32-S3's MCPWM, register by register (Arduino-ESP32 2's IDF), or the simulated board's
// stand-in (tests/arduino_sim). Not on other boards, nor yet on Arduino-ESP32 3.
#if defined(ESP32) && !(defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3)
#include <type_traits>
#include "soc/mcpwm_struct.h"
#include "soc/gpio_sig_map.h"
#include "esp_rom_gpio.h"
#include "esp_intr_alloc.h"
#include "driver/periph_ctrl.h"
#define CAM_TIMER 1
#elif defined(ARDUINO_SIM)
#define CAM_TIMER 1
#else
#define CAM_TIMER 0
#endif

#define VERSION 4           // 2: counts on through trigger pauses, says gap, takes reset (pull request 304);
                            // 3: inputs, servos and links, for operant boxes; 4: the camera pulses themselves
#define MAX_CH 8
#define MAX_IN 8
#define MAX_SERVO 4
#define MAX_LINK 8
#define SERVO_HZ 50
#define SERVO_BITS 14
#define SERVO_PERIOD_US 20000
#define SERVO_STEP_US 20000 // a moving servo's pulse changes once a period
#define MAX_CAM_PINS 4
#define CAM_MARGIN_US 50    // a stop asked for this close to a period's end lets one more period through
#define CAM_HOLD_US 20      // the loop holds the pins low this long after the last period's end (the timer has
                            // stopped itself at it): a second guard, and never early
#define RISES 1024          // trigger rises remembered: 17 s at 60 fps
#define NONE INT64_MAX
#define CRITICAL_US 300     // interrupts off this long before a change
#define SERIAL_US 1500      // serial only when the next change is further off
#define TRIG_SLACK_US 300   // how long past its expected time a trigger is waited for
#define GAP_US 500000       // a trigger after this long is after a pause: said (gap), the rate learnt again
#ifndef INPUT_PULLDOWN
#define INPUT_PULLDOWN INPUT
#endif

// Declared here so the sketch also compiles as plain C++ (campy's tests run it)
struct Channel {
  int pin;
  uint8_t group;
  bool high;                // as written
  bool level;               // on (a level)
  int64_t level_since;
  int64_t max_us;           // 0: no limit
  int64_t fall_at;          // a pulse's end, NONE: no pulse
  int64_t pulse_rise;
  int64_t pulse_k;
  uint32_t every, phase;    // its train
  int64_t offset, width;    // 0 width: none
  int64_t remaining;        // pulses left to make: -1 until stopped, 0 stopped
  int64_t next_rise;        // its next pulse's rise, NONE: not yet known
  int64_t next_k;
};
struct Input {
  int pin;                  // -1: none
  bool active_low;
  int64_t debounce;
  bool raw;                 // the pin as last read (true: high)
  int64_t first_t;          // when the pin first left the taken level since it was taken, NONE: it hasn't
  bool level;               // the taken level (true: high)
  int64_t locked_until;
#if defined(ESP32)
  uint32_t mask;
  uint8_t bank;
#endif
};
struct Servo {
  int pin;                  // -1: none
  int64_t min_us, max_us, safe_us, speed;
  int64_t pos_ns;           // its pulse now, ns (so slow moves keep their fractions)
  int64_t target_us;
  int64_t last_step;
  uint32_t duty;            // as written
  bool told;                // its arrival said
};
struct Link {
  int in;                   // -1: none
  bool edge;                // true: to active
  int what;
  int64_t a, b;
  int64_t armed;            // fires left: -1 until disarmed, 0 disarmed
};
uint64_t Micros64();
int64_t Now();
void Command(char* line);
void ReadSerial(int64_t until);
void Say(const char* line);
void Queue(const char* line);
bool SendSome();
void FlushQueue(int64_t until);
void SetPin(int ch, int pin);
void WritePins(const int8_t* level);
bool ReadTrigger();
void Rose(int64_t t);
void AllOff();
int64_t NextTime();
int64_t ExpectedTrigger(int64_t now);
void Apply(int64_t t);
void WaitTrigger(int64_t until);
void SampleInputs(int64_t now);
void TakeInputs(int64_t now);
void StepServos(int64_t now);
void ServoWrite(int s);
void SafeBox();
bool CamPin(int pin);
int64_t CamNextRise();
void WaitRise(int64_t t);
void CamCheck(int64_t now);
void SyncNext();
void WriteSync(int level);

Channel chans[MAX_CH];
Input inputs[MAX_IN];
Servo servos[MAX_SERVO];
Link links[MAX_LINK];
int input_count = 0;        // inputs with a pin: none, and SampleInputs costs nothing
// The camera pulses (cam): their pins, period, time 0 and planned count; cam_hw_* are the timer interrupt's
int cam_pins[MAX_CAM_PINS];
int cam_npins = 0;
int64_t cam_period = 0;     // 0: no camera pulses (the frames come from the trigger pin)
bool cam_running = false;
int64_t cam_t0 = 0;
volatile uint32_t cam_planned = 0;  // pulses in all, 0: until stopped
volatile uint32_t cam_hw_rises = 0;
int64_t camwatch_us = 0;
bool camwd_fired = false;
// The sync output: trigger.ino's train, from the cameras' time 0
int sync_pin = -1;
uint64_t sync_seed = 0;
int64_t sync_width = 0, sync_min_ms = 0, sync_max_ms = 0;
uint64_t sync_j = 0;
int64_t sync_rise = 0;      // the pulse's rise, us from time 0
bool sync_high = false;
int64_t sync_at = INT64_MAX; // its next edge, board us; NONE: none due
#if defined(ESP32)
uint32_t sync_mask = 0;
uint8_t sync_bank = 0;
#endif
#if defined(ESP32)
uint32_t in_mask0 = 0, in_mask1 = 0, in_last0 = 0, in_last1 = 0;
#endif
#if defined(ESP32)
uint32_t ch_mask[MAX_CH];
uint8_t ch_bank[MAX_CH];
uint32_t trig_mask = 0;
uint8_t trig_bank = 0;
#endif
int trig_pin = -1;
bool trig_level = false;
int64_t rises[RISES];
int64_t frame = 0;          // the last trigger's frame number, 0: none yet
int64_t period = 0;         // between the last two, 0: unknown
int64_t bound_us = 0;
int64_t watchdog_us = 250000;
int64_t last_heard = 0;
uint32_t clashes = 0, lates = 0, wds = 0;
int64_t late_max = 0;

// Lines waiting to be said, each with its "\r\n": while changes were close, or while USB couldn't take them yet.
// The head line may be partly sent (q_sent of its characters).
#define QUEUE 128
#define LINE_BYTES 128
char queue[QUEUE][LINE_BYTES];
int q_head = 0, q_len = 0, q_sent = 0;
uint32_t lost = 0;

char line[128];
int line_len = 0;


uint64_t Micros64() {
  // micros() wraps after 71.6 minutes: count the wraps (called far more often)
  static uint32_t last = 0;
  static uint64_t wraps = 0;
  uint32_t m = (uint32_t) micros();
  if (m < last) { wraps += 1ULL << 32; }
  last = m;
  return wraps | m;
}


int64_t Now() {
  return (int64_t) Micros64();
}


void setup() {
  Serial.begin(115200);
#if defined(ESP32)
  Serial.setTxTimeoutMs(0); // never wait for a host that isn't reading
#endif
  for (int ch = 0; ch < MAX_CH; ch++) {
    Channel& c = chans[ch];
    c.pin = -1; c.group = 0; c.high = false; c.level = false; c.level_since = 0; c.max_us = 0;
    c.fall_at = NONE; c.pulse_rise = 0; c.pulse_k = -1;
    c.every = 1; c.phase = 0; c.offset = 0; c.width = 0; c.remaining = 0; c.next_rise = NONE; c.next_k = -1;
  }
  for (int i = 0; i < MAX_IN; i++) { inputs[i].pin = -1; }
  for (int s = 0; s < MAX_SERVO; s++) { servos[s].pin = -1; }
  for (int l = 0; l < MAX_LINK; l++) { links[l].in = -1; links[l].armed = 0; }
  for (int i = 0; i < MAX_CAM_PINS; i++) { cam_pins[i] = -1; }
  // Defaults (docs/live-tracking.md): channels 0-7 on D2-D9, low from
  // power-up, the camera trigger in on D10. campy-rules sets them again.
  for (int ch = 0; ch < MAX_CH; ch++) { SetPin(ch, 2 + ch); }
  trig_pin = 10;
  pinMode(trig_pin, INPUT_PULLDOWN);
#if defined(ESP32)
  int gpio = digitalPinToGPIONumber(trig_pin);
  trig_bank = gpio > 31 ? 1 : 0;
  trig_mask = 1UL << (gpio & 31);
#endif
  trig_level = ReadTrigger();
  last_heard = Now();
  char text[40];
  snprintf(text, sizeof text, "ready campy_outputs %d", VERSION);
  Say(text);
}


void SetPin(int ch, int pin) {
  if (chans[ch].pin >= 0) { digitalWrite(chans[ch].pin, LOW); }
  chans[ch].pin = pin;
  chans[ch].high = false;
  if (pin < 0) { return; }
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
#if defined(ESP32)
  int gpio = digitalPinToGPIONumber(pin);
  ch_bank[ch] = gpio > 31 ? 1 : 0;
  ch_mask[ch] = 1UL << (gpio & 31);
#endif
}


bool ReadTrigger() {
  if (trig_pin < 0) { return false; }
#if defined(ESP32)
  return ((trig_bank ? GPIO.in1.val : GPIO.in) & trig_mask) != 0;
#else
  return digitalRead(trig_pin) == HIGH;
#endif
}


void WritePins(const int8_t* level) {
  // level[ch]: 1 high, 0 low, -1 unchanged. All at once.
#if defined(ESP32)
  uint32_t set0 = 0, set1 = 0, clr0 = 0, clr1 = 0;
  for (int ch = 0; ch < MAX_CH; ch++) {
    if (level[ch] < 0 || chans[ch].pin < 0) { continue; }
    if (level[ch]) { if (ch_bank[ch]) set1 |= ch_mask[ch]; else set0 |= ch_mask[ch]; }
    else { if (ch_bank[ch]) clr1 |= ch_mask[ch]; else clr0 |= ch_mask[ch]; }
  }
  if (set0) { GPIO.out_w1ts = set0; }
  if (set1) { GPIO.out1_w1ts.val = set1; }
  if (clr0) { GPIO.out_w1tc = clr0; }
  if (clr1) { GPIO.out1_w1tc.val = clr1; }
#else
  for (int ch = 0; ch < MAX_CH; ch++) {
    if (level[ch] >= 0 && chans[ch].pin >= 0) { digitalWrite(chans[ch].pin, level[ch] ? HIGH : LOW); }
  }
#endif
  for (int ch = 0; ch < MAX_CH; ch++) {
    if (level[ch] >= 0) { chans[ch].high = level[ch] != 0; }
  }
}


void WriteOne(int ch, int level) {
  int8_t levels[MAX_CH];
  for (int i = 0; i < MAX_CH; i++) { levels[i] = -1; }
  levels[ch] = (int8_t) level;
  WritePins(levels);
}


bool Clash(int ch, const int8_t* rising) {
  // Would ch going high put two channels of its group high together?
  uint8_t g = chans[ch].group;
  if (g == 0) { return false; }
  for (int i = 0; i < MAX_CH; i++) {
    if (i != ch && chans[i].group == g && (chans[i].high || (rising != NULL && rising[i] == 1))) { return true; }
  }
  return false;
}


void Say(const char* text) {
  // An answer: now, as far as USB takes it, after whatever is waiting (so the answers keep their order); the rest
  // as USB takes it (FlushQueue). Never waited for.
  Queue(text);
  SendSome();
}


void Queue(const char* text) {
  if (q_len == QUEUE) { lost++; return; }
  char* slot = queue[(q_head + q_len) % QUEUE];
  size_t n = strlen(text);
  if (n > LINE_BYTES - 3) { n = LINE_BYTES - 3; }
  memcpy(slot, text, n);
  memcpy(slot + n, "\r\n", 3);
  q_len++;
}


bool SendSome() {
  // As much of what's waiting as USB takes now, without waiting for it. True once nothing is left.
  if (!Serial) { q_len = 0; q_sent = 0; return true; } // the host closed the port: the next one hears none of it
  char piece[LINE_BYTES];
  while (q_len > 0) {
    const char* text = queue[q_head] + q_sent;
    int len = (int) strlen(text);
    int room = Serial.availableForWrite();
    if (room <= 0) { return false; }
    int n = len < room ? len : room;
    memcpy(piece, text, n);
    piece[n] = 0;
    Serial.print(piece);
    if (n < len) { q_sent += n; return false; }
    q_head = (q_head + 1) % QUEUE;
    q_len--;
    q_sent = 0;
  }
  return true;
}


void FlushQueue(int64_t until) {
  // What's waiting, as USB takes it, until it's time to make the next change
  while (q_len > 0 && Now() < until) {
    if (!SendSome()) { return; }
  }
}


bool Active() {
  // Anything on, pulsing or running: what the watchdog guards
  for (int ch = 0; ch < MAX_CH; ch++) {
    const Channel& c = chans[ch];
    if (c.level || c.fall_at != NONE || c.remaining != 0) { return true; }
  }
  for (int l = 0; l < MAX_LINK; l++) {
    if (links[l].in >= 0 && links[l].armed != 0) { return true; }
  }
  for (int s = 0; s < MAX_SERVO; s++) {
    // Sent away from its safe position (not on its way back to it: that needs no guarding)
    if (servos[s].pin >= 0 && servos[s].target_us != servos[s].safe_us) { return true; }
  }
  return false;
}


int64_t RiseOf(int64_t k) {
  // Frame k's trigger rise, NONE if not remembered
  if (k < 1 || k > frame || frame - k >= RISES) { return NONE; }
  return rises[k % RISES];
}


void ScheduleTrain(int ch, int64_t now) {
  // The channel's next pulse, from the frames whose triggers came: one whose
  // rise is still ahead (offset after the last trigger), else it waits for
  // the next trigger (Rose)
  Channel& c = chans[ch];
  c.next_rise = NONE;
  c.next_k = -1;
  if (c.remaining == 0 || c.width <= 0 || frame < 1 || frame < (int64_t) c.phase) { return; }
  if ((uint64_t) (frame - (int64_t) c.phase) % c.every != 0) { return; }
  int64_t t = rises[frame % RISES] + c.offset;
  if (t > now + CRITICAL_US) {
    c.next_rise = t;
    c.next_k = frame;
  }
}


void Rose(int64_t t) {
  // The camera trigger rose at t: frame k, and the trains' pulses for it.
  // Those due right away (offset 0) go high here, in one write.
  int64_t gap = frame > 0 ? t - rises[frame % RISES] : 0;
  period = gap >= GAP_US ? 0 : gap; // after a pause, the rate is learnt again from the next two
  frame++;
  rises[frame % RISES] = t;
  if (gap >= GAP_US) {
    char said[48];
    snprintf(said, sizeof said, "gap %lld %lld", (long long) frame, (long long) gap);
    Queue(said);
  }
  int8_t level[MAX_CH];
  for (int ch = 0; ch < MAX_CH; ch++) { level[ch] = -1; } // all of it before Clash reads it
  bool any = false;
  for (int ch = 0; ch < MAX_CH; ch++) {
    Channel& c = chans[ch];
    if (c.remaining == 0 || c.width <= 0 || frame < (int64_t) c.phase) { continue; }
    if ((uint64_t) (frame - (int64_t) c.phase) % c.every != 0) { continue; }
    if (c.offset > 0) {
      c.next_rise = t + c.offset;
      c.next_k = frame;
      continue;
    }
    if (c.fall_at != NONE || c.level) { continue; } // still high: no second rise
    if (Clash(ch, level)) {
      char text[64];
      snprintf(text, sizeof text, "clash %d %lld %lld", ch, (long long) frame, (long long) t);
      Queue(text);
      clashes++;
      continue;
    }
    level[ch] = 1;
    any = true;
  }
  if (any) {
    int64_t now = Now();
    WritePins(level);
    for (int ch = 0; ch < MAX_CH; ch++) {
      if (level[ch] != 1) { continue; }
      Channel& c = chans[ch];
      c.pulse_rise = now;
      c.pulse_k = frame;
      c.fall_at = now + c.width;
      if (c.remaining > 0) { c.remaining--; }
      if (now - t > late_max) { late_max = now - t; }
    }
  }
  char text[48];
  snprintf(text, sizeof text, "t %lld %lld", (long long) frame, (long long) t);
  Queue(text);
}


int64_t NextTime() {
  // The next change the board makes by itself; NONE when none is due
  int64_t t = NONE;
  for (int ch = 0; ch < MAX_CH; ch++) {
    const Channel& c = chans[ch];
    if (c.fall_at < t) { t = c.fall_at; }
    if (c.next_rise < t) { t = c.next_rise; }
    if (c.level && c.max_us > 0 && c.level_since + c.max_us < t) { t = c.level_since + c.max_us; }
  }
  if (sync_at < t) { t = sync_at; }
  if (Active() && last_heard + watchdog_us < t) { t = last_heard + watchdog_us; }
  return t;
}


int64_t ExpectedTrigger(int64_t now) {
  // When the next trigger should rise, from the last two; NONE if unknown
  if (trig_pin < 0 || frame < 2 || period <= 0) { return NONE; }
  int64_t last = rises[frame % RISES];
  if (now - last > GAP_US) { return NONE; }
  int64_t next = last + period;
  while (next + TRIG_SLACK_US < now) { next += period; } // missed: the one after
  return next;
}


void Apply(int64_t t) {
  // Every change due at t, at once
  int8_t level[MAX_CH];
  for (int ch = 0; ch < MAX_CH; ch++) { level[ch] = -1; }
  bool watchdog = Active() && last_heard + watchdog_us == t;
  char said[MAX_CH][64];
  for (int ch = 0; ch < MAX_CH; ch++) { said[ch][0] = 0; }
  for (int ch = 0; ch < MAX_CH; ch++) {
    Channel& c = chans[ch];
    if (watchdog) { level[ch] = 0; continue; }
    if (c.level && c.max_us > 0 && c.level_since + c.max_us == t) {
      c.level = false;
      if (c.fall_at == NONE) { level[ch] = 0; }
      snprintf(said[ch], 64, "e %d 0 %lld max", ch, (long long) t);
    }
    if (c.fall_at == t && !c.level) { level[ch] = 0; }
  }
  if (!watchdog) {
    for (int ch = 0; ch < MAX_CH; ch++) {
      Channel& c = chans[ch];
      if (c.next_rise != t) { continue; }
      if (c.fall_at != NONE || c.level || c.remaining == 0) { c.next_rise = NONE; continue; }
      if (Clash(ch, level)) {
        snprintf(said[ch], 64, "clash %d %lld %lld", ch, (long long) c.next_k, (long long) t);
        clashes++;
        c.next_rise = NONE;
        continue;
      }
      level[ch] = 1;
    }
  }

  bool sync_due = sync_at == t;

  noInterrupts();
  int64_t now;
  while (true) {
    now = Now();
    if (now >= t) { break; }
    SampleInputs(now);
  }
  WritePins(level);
  if (sync_due) { WriteSync(sync_high ? 0 : 1); }
  interrupts();

  if (now - t > late_max) { late_max = now - t; }
  if (sync_due) {
    sync_high = !sync_high;
    SyncNext();
  }
  if (watchdog) {
    AllOff();
    SafeBox();
    wds++;
    char text[40];
    snprintf(text, sizeof text, "wd %lld", (long long) now);
    Queue(text);
    return;
  }
  for (int ch = 0; ch < MAX_CH; ch++) {
    Channel& c = chans[ch];
    if (level[ch] == 1) {
      c.pulse_rise = now;
      c.pulse_k = c.next_k;
      c.fall_at = c.pulse_rise + c.width;
      c.next_rise = NONE;
      if (c.remaining > 0) { c.remaining--; }
    }
    else if (c.fall_at == t) {
      char text[64];
      snprintf(text, sizeof text, "p %d %lld %lld %lld", ch, (long long) c.pulse_k, (long long) c.pulse_rise,
        (long long) now);
      Queue(text);
      c.fall_at = NONE;
    }
    if (said[ch][0]) { Queue(said[ch]); }
  }
}


void WaitTrigger(int64_t until) {
  // Watch the trigger closely, interrupts off, until it rises or until
  bool rose = false;
  int64_t t = 0;
  noInterrupts();
  while (true) {
    t = Now();
    bool level = ReadTrigger();
    if (level && !trig_level) { trig_level = true; rose = true; break; }
    trig_level = level;
    SampleInputs(t);
    if (t >= until) { break; }
  }
  if (rose) { Rose(t); }
  interrupts();
}


// ---------------------------------------------------------------- an operant box: inputs, servos, links

int PinOf(long long value) {
  // A command's pin: 0-48, or 100-107 for A0-A7; -2 if neither
#if defined(A7)
  static const int analog[8] = {A0, A1, A2, A3, A4, A5, A6, A7};
  if (value >= 100 && value <= 107) { return analog[value - 100]; }
#endif
  if (value >= 0 && value <= 48) { return (int) value; }
  return -2;
}


bool PinTaken(int pin, int in_except, int servo_except) {
  // Is pin already the trigger's, a channel's, another input's or another servo's?
  if (pin == trig_pin) { return true; }
  for (int ch = 0; ch < MAX_CH; ch++) { if (chans[ch].pin == pin) { return true; } }
  for (int i = 0; i < MAX_IN; i++) { if (i != in_except && inputs[i].pin == pin) { return true; } }
  for (int s = 0; s < MAX_SERVO; s++) { if (s != servo_except && servos[s].pin == pin) { return true; } }
  return CamPin(pin);
}


bool BoxPin(int pin) {
  // An input's or a servo's: a channel or the trigger can't have it (channels may still share pins with each other,
  // as before version 3)
  for (int i = 0; i < MAX_IN; i++) { if (inputs[i].pin == pin) { return true; } }
  for (int s = 0; s < MAX_SERVO; s++) { if (servos[s].pin == pin) { return true; } }
  return CamPin(pin);
}


bool ReadInput(const Input& in) {
#if defined(ESP32)
  return ((in.bank ? GPIO.in1.val : GPIO.in) & in.mask) != 0;
#else
  return digitalRead(in.pin) == HIGH;
#endif
}


void SampleInputs(int64_t now) {
  // Read the inputs' pins, noting when each changed. Cheap enough for the timing loops: on the ESP32 two
  // register reads, and nothing more unless a pin changed.
  if (input_count == 0) { return; }
#if defined(ESP32)
  uint32_t v0 = GPIO.in & in_mask0;
  uint32_t v1 = in_mask1 ? (GPIO.in1.val & in_mask1) : 0;
  if (v0 == in_last0 && v1 == in_last1) { return; }
  in_last0 = v0;
  in_last1 = v1;
#endif
  for (int i = 0; i < MAX_IN; i++) {
    Input& in = inputs[i];
    if (in.pin < 0) { continue; }
#if defined(ESP32)
    bool r = ((in.bank ? v1 : v0) & in.mask) != 0;
#else
    bool r = ReadInput(in);
#endif
    if (r == in.raw) { continue; }
    in.raw = r;
    if (r != in.level) { if (in.first_t == NONE) { in.first_t = now; } }
    else { in.first_t = NONE; } // back where it was: a change shorter than a look, or a bounce in the lockout
  }
}


void FrameAt(int64_t t, int64_t* k, int64_t* after) {
  // The camera frame t fell in (the last trigger rise at or before it), 0 if none is known, and how long after
  // its rise
  int64_t j = frame;
  while (j >= 1 && frame - j < RISES && rises[j % RISES] > t) { j--; }
  if (j < 1 || frame - j >= RISES) { *k = 0; *after = -1; return; }
  *k = j;
  *after = t - rises[j % RISES];
}


bool Fire(const Link& link, int64_t t) {
  // What a link does, now. False if it was refused.
  int what = link.what;
  if (what == 4) {
    Servo& s = servos[link.a];
    if (s.pin < 0) { return false; }
    s.target_us = link.b;
    s.told = false;
    if (s.speed == 0) { StepServos(Now()); }
    return true;
  }
  Channel& c = chans[link.a];
  if (c.pin < 0) { return false; }
  if (what == 3) {
    c.level = false;
    if (c.fall_at == NONE && c.high) {
      WriteOne((int) link.a, 0);
      char text[48];
      snprintf(text, sizeof text, "e %d 0 %lld link", (int) link.a, (long long) Now());
      Queue(text);
    }
    return true;
  }
  if (what == 5) {
    if (c.width <= 0) { return false; }
    c.remaining = link.b;
    ScheduleTrain((int) link.a, Now());
    return true;
  }
  // pulse or on
  if (what == 1 && (c.fall_at != NONE || c.level)) { return false; }
  if (!c.high && Clash((int) link.a, NULL)) { clashes++; return false; }
  noInterrupts();
  int64_t now = Now();
  WriteOne((int) link.a, 1);
  interrupts();
  if (what == 1) {
    int64_t k, after;
    FrameAt(t, &k, &after);
    c.pulse_rise = now;
    c.pulse_k = k > 0 ? k : -1;
    c.fall_at = now + link.b;
  }
  else if (!c.level) {
    c.level = true;
    c.level_since = now;
    char text[48];
    snprintf(text, sizeof text, "e %d 1 %lld link", (int) link.a, (long long) now);
    Queue(text);
  }
  return true;
}


void TakeInputs(int64_t now) {
  // The inputs' changes, once their lockout is over: said, and their links fired
  SampleInputs(now);
  for (int i = 0; i < MAX_IN; i++) {
    Input& in = inputs[i];
    if (in.pin < 0 || in.raw == in.level || now < in.locked_until) { continue; }
    int64_t t = in.first_t != NONE ? in.first_t : now;
    in.level = in.raw;
    in.first_t = NONE;
    in.locked_until = t + in.debounce;
    bool active = in.level != in.active_low;
    int64_t k, after;
    FrameAt(t, &k, &after);
    char text[64];
    snprintf(text, sizeof text, "i %d %d %lld %lld %lld", i, active ? 1 : 0, (long long) t, (long long) k,
      (long long) after);
    Queue(text); // said once USB takes it: the links below fire first
    for (int l = 0; l < MAX_LINK; l++) {
      Link& link = links[l];
      if (link.in != i || link.armed == 0 || link.edge != active) { continue; }
      bool done = Fire(link, t);
      if (link.armed > 0) { link.armed--; }
      snprintf(text, sizeof text, "x %d %d %lld %lld %d", l, i, (long long) t, (long long) k, done ? 1 : 0);
      Queue(text);
    }
  }
}


void SetInputMasks() {
  input_count = 0;
#if defined(ESP32)
  in_mask0 = in_mask1 = 0;
#endif
  for (int i = 0; i < MAX_IN; i++) {
    if (inputs[i].pin < 0) { continue; }
    input_count++;
#if defined(ESP32)
    if (inputs[i].bank) { in_mask1 |= inputs[i].mask; } else { in_mask0 |= inputs[i].mask; }
#endif
  }
#if defined(ESP32)
  in_last0 = GPIO.in & in_mask0;
  in_last1 = in_mask1 ? (GPIO.in1.val & in_mask1) : 0;
#endif
}


// The servos' PWM: the ESP32's LEDC hardware (Arduino-ESP32 3's API, or 2's), or the simulated board's
#if defined(ESP32) || defined(ARDUINO_SIM)
#define SERVOS 1
#if defined(ESP32) && defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR < 3
bool PwmStart(int s, int pin) { ledcSetup(s, SERVO_HZ, SERVO_BITS); ledcAttachPin(pin, s); return true; }
void PwmWrite(int s, int pin, uint32_t duty) { (void) pin; ledcWrite(s, duty); }
void PwmStop(int s, int pin) { (void) s; ledcDetachPin(pin); pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }
#else
bool PwmStart(int s, int pin) { (void) s; return ledcAttach(pin, SERVO_HZ, SERVO_BITS); }
void PwmWrite(int s, int pin, uint32_t duty) { (void) s; ledcWrite(pin, duty); }
void PwmStop(int s, int pin) { (void) s; ledcDetach(pin); pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }
#endif
#else
#define SERVOS 0
#endif


void ServoWrite(int s) {
#if SERVOS
  Servo& sv = servos[s];
  int64_t us = (sv.pos_ns + 500) / 1000;
  uint32_t duty = (uint32_t) ((us * (1L << SERVO_BITS) + SERVO_PERIOD_US / 2) / SERVO_PERIOD_US);
  if (duty == sv.duty) { return; }
  sv.duty = duty;
  PwmWrite(s, sv.pin, duty);
#else
  (void) s;
#endif
}


void StepServos(int64_t now) {
  // Each moving servo a step nearer its target, once a period; said once there
  for (int s = 0; s < MAX_SERVO; s++) {
    Servo& sv = servos[s];
    if (sv.pin < 0) { continue; }
    int64_t target = sv.target_us * 1000;
    if (sv.pos_ns == target) {
      sv.last_step = now;
      if (!sv.told) {
        sv.told = true;
        char text[48];
        snprintf(text, sizeof text, "m %d %lld %lld", s, (long long) sv.target_us, (long long) now);
        Queue(text);
      }
      continue;
    }
    if (sv.speed > 0 && now - sv.last_step < SERVO_STEP_US) { continue; }
    int64_t step = sv.speed > 0 ? sv.speed * (now - sv.last_step) / 1000 : INT64_MAX; // ns of pulse
    if (step < 1) { step = 1; }
    sv.last_step = now;
    int64_t left = target - sv.pos_ns;
    if (left > step) { sv.pos_ns += step; }
    else if (-left > step) { sv.pos_ns -= step; }
    else { sv.pos_ns = target; }
    ServoWrite(s);
    if (sv.pos_ns == target && !sv.told) {
      sv.told = true;
      char text[48];
      snprintf(text, sizeof text, "m %d %lld %lld", s, (long long) sv.target_us, (long long) now);
      Queue(text);
    }
  }
}


void SafeBox() {
  // The watchdog's and alloff's part for a box: every link disarmed, every servo on its way to its safe position
  for (int l = 0; l < MAX_LINK; l++) { links[l].armed = 0; }
  for (int s = 0; s < MAX_SERVO; s++) {
    Servo& sv = servos[s];
    if (sv.pin < 0 || sv.target_us == sv.safe_us) { continue; }
    sv.target_us = sv.safe_us;
    sv.told = false;
  }
}


// ---------------------------------------------------------------- the camera pulses (one board for both)

void CamHwStopAtZero();

void CamOnRise() {
  // The timer's interrupt at each rise (the simulated board calls it there too): count it, and at the planned count
  // stop the timer when it next reaches zero, the end of this period, so the last pulse ends whole
  cam_hw_rises = cam_hw_rises + 1;
  if (cam_planned != 0 && cam_hw_rises >= cam_planned) { CamHwStopAtZero(); }
}

#if CAM_TIMER && defined(ESP32)
// MCPWM0, register by register (IDF's hal/mcpwm_ll.h, which does the same, is C only): timer 0 at 1 MHz
// (160 MHz / 16 / 10), counting 0..P-1; operator 0's generator A low at zero, high at comparator A (P/2); its output
// on every camera pin; an interrupt at comparator A. Each register is read, changed and written whole (32 bits).
#define CAM_SET(reg, field, value) do { \
  std::remove_cv<std::remove_reference<decltype(reg)>::type>::type r_; r_.val = (reg).val; \
  r_.field = (value); (reg).val = r_.val; } while (0)
#define CAM_CMP_BIT (1UL << 15) // operator 0, comparator A: int bit 15 + comparator * 3 + operator
#define CAM_STOP_AT_ZERO 0      // timer_start: stop when counting to zero
#define CAM_START_NO_STOP 2     // timer_start: start, run on
#define CAM_GEN_ACTIONS ((1 << 0) | (2 << 4)) // generator A: low at zero (UTEZ = 1), high at comparator A (UTEA = 2)
intr_handle_t cam_intr = NULL;

void IRAM_ATTR CamHwStopAtZero() {
  CAM_SET(MCPWM0.timer[0].timer_cfg1, timer_start, CAM_STOP_AT_ZERO);
}

void IRAM_ATTR CamIsr(void*) {
  uint32_t status = MCPWM0.int_st.val;
  MCPWM0.int_clr.val = status;
  if (status & CAM_CMP_BIT) {
    cam_hw_rises = cam_hw_rises + 1;
    if (cam_planned != 0 && cam_hw_rises >= cam_planned) { CamHwStopAtZero(); }
  }
}

void CamForceLow(bool on) {
  // Generator A held low (continuous force), or let go
  CAM_SET(MCPWM0.operators[0].gen_force, gen_cntuforce_upmethod, 0); // at once
  CAM_SET(MCPWM0.operators[0].gen_force, gen_a_cntuforce_mode, on ? 1 : 0);
}

bool CamHwSetup() {
  periph_module_enable(PERIPH_PWM0_MODULE);
  CAM_SET(MCPWM0.clk_cfg, clk_prescale, 16 - 1);
  CAM_SET(MCPWM0.timer[0].timer_cfg0, timer_prescale, 10 - 1);
  CAM_SET(MCPWM0.timer[0].timer_cfg1, timer_mod, 0); // paused
  CAM_SET(MCPWM0.timer[0].timer_cfg1, timer_start, CAM_STOP_AT_ZERO);
  CAM_SET(MCPWM0.timer[0].timer_cfg0, timer_period_upmethod, 0); // its period at once
  CAM_SET(MCPWM0.timer[0].timer_cfg0, timer_period, (uint32_t) cam_period - 1);
  CAM_SET(MCPWM0.operator_timersel, operator0_timersel, 0);
  MCPWM0.operators[0].gen_stmp_cfg.val = MCPWM0.operators[0].gen_stmp_cfg.val & ~0x0FUL; // comparator A at once
  CAM_SET(MCPWM0.operators[0].timestamp[0], gen, (uint32_t) (cam_period / 2));
  MCPWM0.operators[0].generator[0].val = CAM_GEN_ACTIONS;
  CamForceLow(true); // low until it starts
  for (int i = 0; i < cam_npins; i++) {
    pinMode(cam_pins[i], OUTPUT);
    digitalWrite(cam_pins[i], LOW);
    esp_rom_gpio_connect_out_signal(digitalPinToGPIONumber(cam_pins[i]), PWM0_OUT0A_IDX, false, false);
  }
  if (cam_intr == NULL && esp_intr_alloc(ETS_PWM0_INTR_SOURCE, ESP_INTR_FLAG_IRAM, CamIsr, NULL, &cam_intr) != ESP_OK) {
    return false;
  }
  MCPWM0.int_clr.val = CAM_CMP_BIT;
  MCPWM0.int_ena.val = MCPWM0.int_ena.val | CAM_CMP_BIT;
  return true;
}

int64_t CamHwStart() {
  // Starts counting from 0 now; returns time 0 on the board's clock (both from the one crystal)
  CamForceLow(false);
  int64_t before = Now();
  CAM_SET(MCPWM0.timer[0].timer_cfg1, timer_mod, 1); // counting up
  CAM_SET(MCPWM0.timer[0].timer_cfg1, timer_start, CAM_START_NO_STOP);
  int64_t after = Now();
  return (before + after) / 2;
}

void CamHwHold() {
  // Stopped, its pins low
  CamHwStopAtZero();
  CamForceLow(true);
}

void CamHwFree() {
  // The pins back to plain outputs, low
  CamHwHold();
  for (int i = 0; i < cam_npins; i++) {
    esp_rom_gpio_connect_out_signal(digitalPinToGPIONumber(cam_pins[i]), SIG_GPIO_OUT_IDX, false, false);
    digitalWrite(cam_pins[i], LOW);
  }
}
#elif CAM_TIMER
void CamHwStopAtZero() { SimCamStopAtZero(); }
bool CamHwSetup() {
  for (int i = 0; i < cam_npins; i++) { pinMode(cam_pins[i], OUTPUT); digitalWrite(cam_pins[i], LOW); }
  return true;
}
int64_t CamHwStart() { return (int64_t) SimCamStart(cam_pins, cam_npins, (unsigned long) cam_period, CamOnRise); }
void CamHwHold() { SimCamHold(); }
void CamHwFree() { SimCamHold(); }
#else
void CamHwStopAtZero() {}
#endif


int64_t CamNextRise() {
  // The next camera rise the board hasn't counted as a frame yet; NONE when none is to come
  if (!cam_running) { return NONE; }
  if (cam_planned != 0 && frame >= (int64_t) cam_planned) { return NONE; }
  return cam_t0 + frame * cam_period + cam_period / 2;
}


void WaitRise(int64_t t) {
  // A camera rise the board made: its frame at exactly t, as Rose for one the trigger pin brought
  noInterrupts();
  while (true) {
    int64_t now = Now();
    if (now >= t) { break; }
    SampleInputs(now);
  }
  Rose(t);
  interrupts();
}


void WriteSync(int level) {
#if defined(ESP32)
  if (level) { if (sync_bank) GPIO.out1_w1ts.val = sync_mask; else GPIO.out_w1ts = sync_mask; }
  else { if (sync_bank) GPIO.out1_w1tc.val = sync_mask; else GPIO.out_w1tc = sync_mask; }
#else
  digitalWrite(sync_pin, level ? HIGH : LOW);
#endif
}


uint64_t SplitMix64(uint64_t seed, uint64_t j) {
  // As trigger.ino's (and campy/syncmatch.py's)
  uint64_t z = seed + j * 0x9E3779B97F4A7C15ULL;
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}


void SyncNext() {
  // The sync output's next edge: this pulse's fall, or the next pulse's rise (pulse k rises at the sum of gaps
  // 1..k+1, gap j = 1000 * (min + SplitMix64(seed, j) % (max - min + 1)) us, from the cameras' time 0). No rise
  // after the cameras' last period.
  if (sync_pin < 0) { sync_at = NONE; return; }
  if (sync_high) { sync_at = cam_t0 + sync_rise + sync_width; return; }
  sync_j++;
  sync_rise += 1000 * (sync_min_ms + (int64_t) (SplitMix64(sync_seed, sync_j) %
    (uint64_t) (sync_max_ms - sync_min_ms + 1)));
  sync_at = cam_t0 + sync_rise;
  if (!cam_running || (cam_planned != 0 && sync_at >= cam_t0 + (int64_t) cam_planned * cam_period)) {
    sync_at = NONE;
  }
}


uint32_t CamStop(int64_t now) {
  // Stop at a period's end with no pulse cut or added: in a period's first half, after its rise (the rise's
  // interrupt stops the timer); in its second half, at its end, unless that is under CAM_MARGIN_US away, when the
  // next period's rise stops it. Interrupts off, so the rise's interrupt can't come between the count and the order.
  int64_t rel = now - cam_t0;
  int64_t i = rel / cam_period, pos = rel % cam_period;
  uint32_t planned = (uint32_t) (pos < cam_period / 2 || cam_period - pos > CAM_MARGIN_US ? i + 1 : i + 2);
  noInterrupts();
  if (cam_planned == 0 || planned < cam_planned) { cam_planned = planned; }
  if (cam_hw_rises >= cam_planned) { CamHwStopAtZero(); }
  interrupts();
  return cam_planned;
}


void CamCheck(int64_t now) {
  // The pulses' end (their planned count, and the last period over), and camwatch
  if (!cam_running) { return; }
  if (camwatch_us > 0 && !camwd_fired && now - last_heard > camwatch_us) {
    camwd_fired = true;
    CamStop(now);
    char text[40];
    snprintf(text, sizeof text, "camwd %lld", (long long) now);
    Queue(text);
  }
  if (cam_planned == 0 || frame < (int64_t) cam_planned) { return; }
  int64_t end = cam_t0 + (int64_t) cam_planned * cam_period;
  if (now < end + CAM_HOLD_US) { return; }
#if CAM_TIMER
  CamHwHold();
#endif
  cam_running = false;
  if (!sync_high) { sync_at = NONE; } // a sync pulse that is high ends as planned
  char text[64];
  snprintf(text, sizeof text, "camend %lld %lld", (long long) frame, (long long) end);
  Queue(text);
  if ((int64_t) cam_hw_rises != frame) {
    snprintf(text, sizeof text, "camerr hw=%lu frames=%lld", (unsigned long) cam_hw_rises, (long long) frame);
    Queue(text);
  }
}


bool CamPin(int pin) {
  for (int i = 0; i < cam_npins; i++) { if (cam_pins[i] == pin) { return true; } }
  return pin >= 0 && pin == sync_pin;
}


void AllOff() {
  for (int ch = 0; ch < MAX_CH; ch++) {
    Channel& c = chans[ch];
    if (c.fall_at != NONE) {
      char text[64];
      snprintf(text, sizeof text, "p %d %lld %lld %lld", ch, (long long) c.pulse_k, (long long) c.pulse_rise,
        (long long) Now());
      Queue(text);
    }
    c.level = false;
    c.fall_at = NONE;
    c.remaining = 0;
    c.next_rise = NONE;
  }
  int8_t level[MAX_CH];
  for (int ch = 0; ch < MAX_CH; ch++) { level[ch] = 0; }
  WritePins(level);
}


bool Number(char* text, long long* value) {
  if (text == NULL) { return false; }
  char* stop;
  *value = strtoll(text, &stop, 10);
  return *stop == 0;
}


bool Late(int ch, long long k, int n, int64_t now, int64_t* after) {
  // A command's frame (its last number, if given): how long ago its trigger
  // rose, and whether that is past the bound (said)
  *after = -1;
  if (n == 0) { return false; }
  int64_t r = RiseOf(k);
  if (r == NONE) { return false; }
  *after = now - r;
  if (bound_us > 0 && *after > bound_us) {
    char text[64];
    snprintf(text, sizeof text, "err late %d %lld %lld", ch, k, (long long) *after);
    Say(text);
    lates++;
    return true;
  }
  return false;
}


void Command(char* text) {
  char reply[128];
  char* word = strtok(text, " \t,");
  if (word == NULL) { return; }
  long long a[6];
  int n = 0;
  char* arg;
  while (n < 6 && (arg = strtok(NULL, " \t,")) != NULL) {
    if (!Number(arg, &a[n])) { Say("err not a number"); return; }
    n++;
  }
  if (strtok(NULL, " \t,") != NULL) { Say("err too many numbers"); return; }
  int64_t now = Now();
  bool chOk = n >= 1 && a[0] >= 0 && a[0] < MAX_CH;
  int ch = chOk ? (int) a[0] : 0;

  if (strcmp(word, "hb") == 0) {
    return;
  }
  else if (strcmp(word, "id") == 0) {
    snprintf(reply, sizeof reply, "ok campy_outputs %d", VERSION);
    Say(reply);
  }
  else if (strcmp(word, "time") == 0) {
    snprintf(reply, sizeof reply, "ok time %lld %lld %lld", (long long) now, (long long) frame,
      (long long) (frame > 0 ? rises[frame % RISES] : -1));
    Say(reply);
  }
  else if (strcmp(word, "status") == 0) {
    int high = 0, running = 0;
    for (int i = 0; i < MAX_CH; i++) {
      if (chans[i].high) { high |= 1 << i; }
      if (chans[i].remaining != 0) { running |= 1 << i; }
    }
    snprintf(reply, sizeof reply, "ok status t=%lld frame=%lld period=%lld high=%d running=%d clashes=%lu "
      "lates=%lu wds=%lu late_max=%lld lost=%lu", (long long) now, (long long) frame, (long long) period, high,
      running, (unsigned long) clashes, (unsigned long) lates, (unsigned long) wds, (long long) late_max,
      (unsigned long) lost);
    Say(reply);
  }
  else if (strcmp(word, "alloff") == 0) {
    AllOff();
    SafeBox();
    snprintf(reply, sizeof reply, "ok alloff %lld", (long long) Now());
    Say(reply);
  }
  else if ((strcmp(word, "reset") == 0 || strcmp(word, "trig") == 0) && cam_period > 0) {
    Say("err the board makes the camera pulses (cam): its frames come from them; cam 0 first");
  }
  else if (strcmp(word, "reset") == 0) {
    frame = 0;
    period = 0;
    snprintf(reply, sizeof reply, "ok reset %lld", (long long) Now());
    Say(reply);
  }
  else if (strcmp(word, "pin") == 0) {
    int pin = n == 2 ? (a[1] == -1 ? -1 : PinOf(a[1])) : -2;
    if (!chOk || pin < -1) { Say("err pin <ch 0-7> <pin, -1 none>"); return; }
    if (pin >= 0 && BoxPin(pin)) { Say("err that pin is an input's, a servo's or a camera's"); return; }
    AllOff();
    SetPin(ch, pin);
    snprintf(reply, sizeof reply, "ok pin %d %lld", ch, a[1]);
    Say(reply);
  }
  else if (strcmp(word, "trig") == 0) {
    int pin = n == 1 ? (a[0] == -1 ? -1 : PinOf(a[0])) : -2;
    if (pin < -1) { Say("err trig <pin, -1 none>"); return; }
    if (pin >= 0 && BoxPin(pin)) { Say("err that pin is an input's, a servo's or a camera's"); return; }
    trig_pin = pin;
    frame = 0;
    period = 0;
    if (trig_pin >= 0) {
      pinMode(trig_pin, INPUT_PULLDOWN);
#if defined(ESP32)
      int gpio = digitalPinToGPIONumber(trig_pin);
      trig_bank = gpio > 31 ? 1 : 0;
      trig_mask = 1UL << (gpio & 31);
#endif
    }
    trig_level = ReadTrigger();
    snprintf(reply, sizeof reply, "ok trig %d", pin);
    Say(reply);
  }
  else if (strcmp(word, "group") == 0) {
    if (n != 2 || !chOk || a[1] < 0 || a[1] > 255) { Say("err group <ch> <g 0-255>"); return; }
    chans[ch].group = (uint8_t) a[1];
    snprintf(reply, sizeof reply, "ok group %d %lld", ch, a[1]);
    Say(reply);
  }
  else if (strcmp(word, "max") == 0) {
    if (n != 2 || !chOk || a[1] < 0) { Say("err max <ch> <us, 0 none>"); return; }
    chans[ch].max_us = a[1];
    snprintf(reply, sizeof reply, "ok max %d %lld", ch, a[1]);
    Say(reply);
  }
  else if (strcmp(word, "bound") == 0) {
    if (n != 1 || a[0] < 0) { Say("err bound <us, 0 none>"); return; }
    bound_us = a[0];
    snprintf(reply, sizeof reply, "ok bound %lld", a[0]);
    Say(reply);
  }
  else if (strcmp(word, "watchdog") == 0) {
    if (n != 1 || a[0] < 50 || a[0] > 10000) { Say("err watchdog <ms 50-10000>"); return; }
    watchdog_us = a[0] * 1000;
    snprintf(reply, sizeof reply, "ok watchdog %lld", a[0]);
    Say(reply);
  }
  else if (strcmp(word, "on") == 0 || strcmp(word, "pulse") == 0) {
    bool pulse = word[0] == 'p';
    int need = pulse ? 2 : 1;
    if (!chOk || n < need || n > need + 1 || (pulse && a[1] < 1)) {
      Say(pulse ? "err pulse <ch> <width_us> [frame]" : "err on <ch> [frame]");
      return;
    }
    Channel& c = chans[ch];
    if (c.pin < 0) { Say("err that channel has no pin"); return; }
    long long k = n > need ? a[need] : -1;
    int64_t after;
    if (Late(ch, k, n - need, now, &after)) { return; }
    if (pulse && (c.fall_at != NONE || c.level)) { Say("err that channel is high"); return; }
    if (!c.high && Clash(ch, NULL)) {
      snprintf(reply, sizeof reply, "err clash %d %lld %lld", ch, k, (long long) now);
      Say(reply);
      clashes++;
      return;
    }
    noInterrupts();
    int64_t t = Now();
    WriteOne(ch, 1);
    interrupts();
    if (pulse) {
      c.pulse_rise = t;
      c.pulse_k = k;
      c.fall_at = t + a[1];
    }
    else if (!c.level) {
      c.level = true;
      c.level_since = t;
    }
    snprintf(reply, sizeof reply, "ok %s %d %lld %lld %lld", word, ch, (long long) t, k,
      (long long) (after >= 0 ? after + (t - now) : -1));
    Say(reply);
  }
  else if (strcmp(word, "off") == 0) {
    if (!chOk || n != 1) { Say("err off <ch>"); return; }
    Channel& c = chans[ch];
    c.level = false;
    int64_t t = Now();
    if (c.fall_at == NONE) {
      noInterrupts();
      t = Now();
      WriteOne(ch, 0);
      interrupts();
    }
    snprintf(reply, sizeof reply, "ok off %d %lld", ch, (long long) t);
    Say(reply);
  }
  else if (strcmp(word, "train") == 0) {
    if (!chOk || n != 5 || a[1] < 1 || a[2] < 0 || a[2] >= a[1] || a[3] < 0 || a[4] < 1) {
      Say("err train <ch> <every> <phase 0..every-1> <offset_us> <width_us>");
      return;
    }
    Channel& c = chans[ch];
    if (c.remaining != 0) { Say("err that train is running: stop it first"); return; }
    c.every = (uint32_t) a[1];
    c.phase = (uint32_t) a[2];
    c.offset = a[3];
    c.width = a[4];
    snprintf(reply, sizeof reply, "ok train %d %lld %lld %lld %lld", ch, a[1], a[2], a[3], a[4]);
    Say(reply);
  }
  else if (strcmp(word, "run") == 0) {
    if (!chOk || n < 2 || n > 3 || a[1] < -1) { Say("err run <ch> <n, -1 until stopped, 0 stop> [frame]"); return; }
    Channel& c = chans[ch];
    if (c.pin < 0) { Say("err that channel has no pin"); return; }
    if (a[1] != 0 && c.width <= 0) { Say("err that channel has no train"); return; }
    long long k = n > 2 ? a[2] : -1;
    int64_t after = -1;
    if (a[1] != 0 && Late(ch, k, n - 2, now, &after)) { return; }
    c.remaining = a[1];
    if (a[1] == 0) { c.next_rise = NONE; c.next_k = -1; }
    else { ScheduleTrain(ch, now); }
    snprintf(reply, sizeof reply, "ok run %d %lld %lld %lld %lld", ch, a[1], (long long) now, k, (long long) after);
    Say(reply);
  }
  else if (strcmp(word, "cam") == 0) {
    if (cam_running) { Say("err the camera pulses are running: camstop first"); return; }
    if (n == 1 && a[0] == 0) {
#if CAM_TIMER
      if (cam_period > 0) { CamHwFree(); }
#endif
      cam_period = 0;
      cam_npins = 0;
      Say("ok cam 0");
      return;
    }
#if !CAM_TIMER
    Say("err no camera timer on this board");
    return;
#else
    int pins[MAX_CAM_PINS];
    bool ok = n >= 2 && a[0] >= 100 && a[0] <= 65535;
    for (int i = 1; ok && i < n; i++) {
      pins[i - 1] = PinOf(a[i]);
      ok = pins[i - 1] >= 0;
      for (int j = 1; ok && j < i; j++) { ok = pins[j - 1] != pins[i - 1]; }
    }
    if (!ok) { Say("err cam <period_us 100-65535> <pin> [pin ...] (up to 4), or cam 0"); return; }
    // Its own pins, and the trigger pin it replaces, may be named again
    int old_n = cam_npins, old_trig = trig_pin;
    cam_npins = 0;
    trig_pin = -1;
    for (int i = 0; i < n - 1; i++) {
      if (PinTaken(pins[i], -1, -1)) { cam_npins = old_n; trig_pin = old_trig; Say("err that pin is taken"); return; }
    }
    if (old_n > 0) { cam_npins = old_n; CamHwFree(); }
    // trig_pin stays -1: the frames come from the camera pulses now
    for (int i = 0; i < n - 1; i++) { cam_pins[i] = pins[i]; }
    cam_npins = n - 1;
    cam_period = a[0];
    frame = 0;
    period = 0;
    if (!CamHwSetup()) { cam_period = 0; cam_npins = 0; Say("err the camera timer could not be set up"); return; }
    snprintf(reply, sizeof reply, "ok cam %lld %d", a[0], cam_npins);
    Say(reply);
#endif
  }
  else if (strcmp(word, "camsync") == 0) {
    if (cam_running) { Say("err the camera pulses are running: camstop first"); return; }
    if (n == 1 && a[0] == -1) {
      if (sync_pin >= 0) { digitalWrite(sync_pin, LOW); }
      sync_pin = -1;
      Say("ok camsync -1");
      return;
    }
    int pin = n == 5 ? PinOf(a[0]) : -2;
    if (pin < 0 || a[1] < 0 || a[2] < 1 || a[3] < 1 || a[4] < a[3] || a[2] >= a[3] * 1000) {
      Say("err camsync <pin> <seed> <width_us, under min_ms> <min_ms> <max_ms>, or camsync -1");
      return;
    }
    if (pin != sync_pin && PinTaken(pin, -1, -1)) { Say("err that pin is taken"); return; }
    sync_pin = pin;
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
#if defined(ESP32)
    int gpio = digitalPinToGPIONumber(pin);
    sync_bank = gpio > 31 ? 1 : 0;
    sync_mask = 1UL << (gpio & 31);
#endif
    sync_seed = (uint64_t) a[1];
    sync_width = a[2];
    sync_min_ms = a[3];
    sync_max_ms = a[4];
    snprintf(reply, sizeof reply, "ok camsync %d %lld %lld %lld %lld", pin, a[1], a[2], a[3], a[4]);
    Say(reply);
  }
  else if (strcmp(word, "camgo") == 0) {
    if (n != 1 || a[0] < 0) { Say("err camgo <n, 0 until stopped>"); return; }
    if (cam_period <= 0) { Say("err no camera pulses set (cam)"); return; }
    if (cam_running) { Say("err the camera pulses are running"); return; }
#if CAM_TIMER
    frame = 0;
    period = 0;
    cam_planned = (uint32_t) a[0];
    cam_hw_rises = 0;
    camwd_fired = false;
    sync_j = 0;
    sync_rise = 0;
    sync_high = false;
    cam_running = true;
    cam_t0 = CamHwStart();
    SyncNext();
    snprintf(reply, sizeof reply, "ok camgo %lld %lld %lld", a[0], (long long) cam_t0, (long long) cam_period);
    Say(reply);
#endif
  }
  else if (strcmp(word, "camstop") == 0) {
    if (!cam_running) { Say("err the camera pulses aren't running"); return; }
    snprintf(reply, sizeof reply, "ok camstop %lu", (unsigned long) CamStop(Now()));
    Say(reply);
  }
  else if (strcmp(word, "camwatch") == 0) {
    if (n != 1 || a[0] < 0) { Say("err camwatch <ms, 0 never>"); return; }
    camwatch_us = a[0] * 1000;
    snprintf(reply, sizeof reply, "ok camwatch %lld", a[0]);
    Say(reply);
  }
  else if (strcmp(word, "camstatus") == 0) {
    snprintf(reply, sizeof reply, "ok camstatus running=%d frames=%lld planned=%lu hw=%lu t0=%lld period=%lld",
      cam_running ? 1 : 0, (long long) frame, (unsigned long) cam_planned, (unsigned long) cam_hw_rises,
      (long long) cam_t0, (long long) cam_period);
    Say(reply);
  }
#if defined(ARDUINO_SIM)
  else if (strcmp(word, "simstall") == 0) {
    // Tests only: the loop held up for a[0] us (the timer and its interrupt go on), as a stalled sketch would be
    int64_t until = now + (n == 1 ? a[0] : 0);
    while (Now() < until) {}
    Say("ok simstall");
  }
#endif
  else if (strcmp(word, "in") == 0) {
    int i = n >= 1 && a[0] >= 0 && a[0] < MAX_IN ? (int) a[0] : -1;
    if (i >= 0 && n == 2 && a[1] == -1) {
      inputs[i].pin = -1;
      for (int l = 0; l < MAX_LINK; l++) { if (links[l].in == i) { links[l].in = -1; links[l].armed = 0; } }
      SetInputMasks();
      snprintf(reply, sizeof reply, "ok in %d -1", i);
      Say(reply);
      return;
    }
    int pin = n == 5 ? PinOf(a[1]) : -2;
    if (i < 0 || pin < 0 || a[2] < 0 || a[2] > 2 || a[3] < 0 || a[3] > 1 || a[4] < 0 || a[4] > 1000000) {
      Say("err in <i 0-7> <pin, -1 none> <pull 0 none, 1 up, 2 down> <active 1 high, 0 low> <debounce_us>");
      return;
    }
    if (PinTaken(pin, i, -1)) { Say("err that pin is taken"); return; }
    Input& in = inputs[i];
    in.pin = pin;
    pinMode(pin, a[2] == 1 ? INPUT_PULLUP : a[2] == 2 ? INPUT_PULLDOWN : INPUT);
    in.active_low = a[3] == 0;
    in.debounce = a[4];
#if defined(ESP32)
    int gpio = digitalPinToGPIONumber(pin);
    in.bank = gpio > 31 ? 1 : 0;
    in.mask = 1UL << (gpio & 31);
#endif
    in.raw = in.level = ReadInput(in);
    in.first_t = NONE;
    in.locked_until = 0;
    SetInputMasks();
    snprintf(reply, sizeof reply, "ok in %d %d %d", i, pin, in.level != in.active_low ? 1 : 0);
    Say(reply);
  }
  else if (strcmp(word, "servo") == 0) {
    int s = n >= 1 && a[0] >= 0 && a[0] < MAX_SERVO ? (int) a[0] : -1;
    if (s >= 0 && n == 2 && a[1] == -1) {
      if (servos[s].pin >= 0) {
#if SERVOS
        PwmStop(s, servos[s].pin);
#endif
      }
      servos[s].pin = -1;
      for (int l = 0; l < MAX_LINK; l++) {
        if (links[l].in >= 0 && links[l].what == 4 && links[l].a == s) { links[l].in = -1; links[l].armed = 0; }
      }
      snprintf(reply, sizeof reply, "ok servo %d -1", s);
      Say(reply);
      return;
    }
    int pin = n == 6 ? PinOf(a[1]) : -2;
    if (s < 0 || pin < 0 || a[2] < 500 || a[3] > 2500 || a[2] >= a[3] || a[4] < a[2] || a[4] > a[3] || a[5] < 0) {
      Say("err servo <s 0-3> <pin, -1 none> <min_us 500-> <max_us -2500> <safe_us> <speed us/s, 0 at once>");
      return;
    }
#if !SERVOS
    Say("err no servos on this board");
    return;
#else
    if (servos[s].pin >= 0 && servos[s].pin != pin) { PwmStop(s, servos[s].pin); servos[s].pin = -1; }
    if (PinTaken(pin, -1, s)) { Say("err that pin is taken"); return; }
    Servo& sv = servos[s];
    if (sv.pin != pin && !PwmStart(s, pin)) { Say("err that pin can't make PWM"); return; }
    sv.pin = pin;
    sv.min_us = a[2];
    sv.max_us = a[3];
    sv.safe_us = a[4];
    sv.speed = a[5];
    sv.target_us = sv.safe_us;
    sv.pos_ns = sv.safe_us * 1000;
    sv.last_step = now;
    sv.duty = 0xffffffffu;
    sv.told = true;
    ServoWrite(s);
    snprintf(reply, sizeof reply, "ok servo %d %d %lld", s, pin, (long long) now);
    Say(reply);
#endif
  }
  else if (strcmp(word, "move") == 0) {
    int s = n >= 2 && a[0] >= 0 && a[0] < MAX_SERVO ? (int) a[0] : -1;
    if (s < 0 || n > 3) { Say("err move <s 0-3> <us> [frame]"); return; }
    Servo& sv = servos[s];
    if (sv.pin < 0) { Say("err that servo has no pin"); return; }
    if (a[1] < sv.min_us || a[1] > sv.max_us) { Say("err move: past that servo's min or max"); return; }
    long long k = n > 2 ? a[2] : -1;
    int64_t after;
    if (Late(s, k, n - 2, now, &after)) { return; }
    sv.target_us = a[1];
    sv.told = false;
    if (sv.speed == 0) { StepServos(now); }
    snprintf(reply, sizeof reply, "ok move %d %lld %lld %lld %lld", s, a[1], (long long) now, k, (long long) after);
    Say(reply);
  }
  else if (strcmp(word, "link") == 0) {
    int l = n >= 1 && a[0] >= 0 && a[0] < MAX_LINK ? (int) a[0] : -1;
    if (l >= 0 && n == 2 && a[1] == -1) {
      links[l].in = -1;
      links[l].armed = 0;
      snprintf(reply, sizeof reply, "ok link %d -1", l);
      Say(reply);
      return;
    }
    bool ok = l >= 0 && n == 6 && a[1] >= 0 && a[1] < MAX_IN && (a[2] == 0 || a[2] == 1) && a[3] >= 1 && a[3] <= 5;
    if (ok && a[3] == 4) { ok = a[4] >= 0 && a[4] < MAX_SERVO; }
    else if (ok) { ok = a[4] >= 0 && a[4] < MAX_CH; }
    if (ok && a[3] == 1) { ok = a[5] >= 1; }
    if (ok && a[3] == 5) { ok = a[5] >= -1 && a[5] != 0; }
    if (!ok) {
      Say("err link <l 0-7> <in> <edge 1 active, 0 inactive> <what: 1 pulse ch us, 2 on ch, 3 off ch, "
        "4 move servo us, 5 run ch n> <a> <b>");
      return;
    }
    if (inputs[a[1]].pin < 0) { Say("err that input has no pin"); return; }
    if (a[3] == 4) {
      const Servo& sv = servos[a[4]];
      if (sv.pin < 0) { Say("err that servo has no pin"); return; }
      if (a[5] < sv.min_us || a[5] > sv.max_us) { Say("err link: past that servo's min or max"); return; }
    }
    else if (chans[a[4]].pin < 0) { Say("err that channel has no pin"); return; }
    else if (a[3] == 5 && chans[a[4]].width <= 0) { Say("err that channel has no train"); return; }
    Link& link = links[l];
    link.in = (int) a[1];
    link.edge = a[2] == 1;
    link.what = (int) a[3];
    link.a = a[4];
    link.b = a[5];
    link.armed = 0;
    snprintf(reply, sizeof reply, "ok link %d %lld %lld %lld %lld %lld", l, a[1], a[2], a[3], a[4], a[5]);
    Say(reply);
  }
  else if (strcmp(word, "arm") == 0) {
    int l = n == 2 && a[0] >= 0 && a[0] < MAX_LINK ? (int) a[0] : -1;
    if (l < 0 || a[1] < -1) { Say("err arm <l 0-7> <n, -1 until disarmed, 0 disarm>"); return; }
    if (links[l].in < 0 && a[1] != 0) { Say("err that link isn't set"); return; }
    links[l].armed = a[1];
    snprintf(reply, sizeof reply, "ok arm %d %lld %lld", l, a[1], (long long) now);
    Say(reply);
  }
  else if (strcmp(word, "box") == 0) {
    int active = 0, armed = 0;
    for (int i = 0; i < MAX_IN; i++) {
      if (inputs[i].pin >= 0 && inputs[i].level != inputs[i].active_low) { active |= 1 << i; }
    }
    for (int l = 0; l < MAX_LINK; l++) { if (links[l].in >= 0 && links[l].armed != 0) { armed |= 1 << l; } }
    int len = snprintf(reply, sizeof reply, "ok box t=%lld in=%d armed=%d", (long long) now, active, armed);
    for (int s = 0; s < MAX_SERVO && len < (int) sizeof reply; s++) {
      if (servos[s].pin < 0) { continue; }
      len += snprintf(reply + len, sizeof reply - len, " s%d=%lld/%lld", s,
        (long long) ((servos[s].pos_ns + 500) / 1000), (long long) servos[s].target_us);
    }
    Say(reply);
  }
  else {
    Say("err unknown command");
  }
}


void ReadSerial(int64_t until) {
  // A command, if it came; reading stops when it's time for the next change
  while (Now() < until && Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) { return; }
    if (c == '\n' || c == '\r') {
      line[line_len] = 0;
      bool empty = line_len == 0;
      line_len = 0;
      if (empty) { continue; }
      last_heard = Now();
      Command(line);
      return; // one command a turn: loop() checks the time between them
    }
    if (line_len < (int) sizeof line - 1) { line[line_len++] = (char) c; }
  }
}


void loop() {
  int64_t now = Now();
  // A trigger the close watch didn't expect (the first ones, a changed rate)
  bool level = ReadTrigger();
  if (level && !trig_level) { trig_level = true; Rose(now); }
  else { trig_level = level; }
  TakeInputs(now);
  CamCheck(now);

  int64_t t = NextTime();
  // The next frame: the camera pulse the board makes (cam), or the trigger pin's, as expected from the last two
  int64_t expected = cam_running ? CamNextRise() : ExpectedTrigger(now);
  int64_t soon = t;
  if (expected != NONE && expected - CRITICAL_US < soon) { soon = expected - CRITICAL_US; }
  if (soon == NONE || soon - now > SERIAL_US) {
    int64_t until = soon == NONE ? NONE : soon - CRITICAL_US;
    if (soon == NONE) { until = now + SERIAL_US; } // still look at the trigger often
    StepServos(now);
    FlushQueue(until);
    ReadSerial(until);
    return;
  }
  if (t != NONE && (t <= now || (t - now <= CRITICAL_US && (expected == NONE || t <= expected)))) {
    Apply(t);
    return;
  }
  if (expected != NONE && expected - now <= CRITICAL_US) {
    if (cam_running) {
      WaitRise(expected); // its time is known: no pin to watch
      return;
    }
    int64_t until = expected + TRIG_SLACK_US;
    if (t != NONE && t < until) { until = t; }
    WaitTrigger(until);
    return;
  }
}
