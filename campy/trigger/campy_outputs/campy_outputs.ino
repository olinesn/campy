// campy outputs, for an Arduino Nano ESP32 (or another Arduino): the TTL
// outputs that campy-rules (campy/rules.py) switches from the animals' live
// poses, on a board of their own, next to the camera trigger's.
//
// The board watches the camera trigger on an input pin and counts its rising
// edges, frame 1, 2, ... (counting starts again at 1 when the trigger comes
// back after half a second or more without a pulse, as at the start of each
// recording: then frame k here is campy's frame k). It says when each one
// rose, so every output can be lined up with the frames afterwards.
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
//   time                              -> ok time <t_us> <frame> <its rise_us>
//   hb                                nothing but keeping the watchdog quiet (no answer)
//   status                            -> ok status ...
// What it says on its own (t_us: the board's clock, microseconds since power-up):
//   ready campy_outputs <version>     at power-up
//   t <k> <t_us>                      the camera trigger rose: frame k
//   p <ch> <k> <rise_us> <fall_us>    a pulse is over (k: the frame it was for, -1 none)
//   e <ch> <0|1> <t_us> <why>         a level changed by itself: max, wd
//   clash <ch> <k> <t_us>             a pulse skipped: its group had a channel high
//   wd <t_us>                         the watchdog fired: everything is low
// Lines it couldn't send (the host not reading) are counted in "lost", never waited for.

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(ESP32)
#include "soc/gpio_struct.h"
#endif

#define VERSION 1
#define MAX_CH 8
#define RISES 1024          // trigger rises remembered: 17 s at 60 fps
#define NONE INT64_MAX
#define CRITICAL_US 300     // interrupts off this long before a change
#define SERIAL_US 1500      // serial only when the next change is further off
#define TRIG_SLACK_US 300   // how long past its expected time a trigger is waited for
#define GAP_US 500000       // a trigger after this long starts again at frame 1
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
uint64_t Micros64();
int64_t Now();
void Command(char* line);
void ReadSerial(int64_t until);
void Say(const char* line);
void Queue(const char* line);
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

Channel chans[MAX_CH];
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

// Lines waiting to be said, while changes were close
#define QUEUE 128
char queue[QUEUE][64];
int q_head = 0, q_len = 0;
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
  // Now, if it fits in what USB can take without waiting; else it's lost
#if defined(ESP32)
  if (Serial.availableForWrite() < (int) strlen(text) + 2) { lost++; return; }
#endif
  Serial.print(text);
  Serial.print("\r\n");
}


void Queue(const char* text) {
  if (q_len == QUEUE) { lost++; return; }
  strncpy(queue[(q_head + q_len) % QUEUE], text, 63);
  queue[(q_head + q_len) % QUEUE][63] = 0;
  q_len++;
}


void FlushQueue(int64_t until) {
  // What's waiting, until it's time to make the next change
  while (q_len > 0 && Now() < until) {
    Say(queue[q_head]);
    q_head = (q_head + 1) % QUEUE;
    q_len--;
  }
}


bool Active() {
  // Anything on, pulsing or running: what the watchdog guards
  for (int ch = 0; ch < MAX_CH; ch++) {
    const Channel& c = chans[ch];
    if (c.level || c.fall_at != NONE || c.remaining != 0) { return true; }
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
  if (frame > 0 && t - rises[frame % RISES] >= GAP_US) { frame = 0; }
  if (frame > 0) { period = t - rises[frame % RISES]; } else { period = 0; }
  frame++;
  rises[frame % RISES] = t;
  int8_t level[MAX_CH];
  bool any = false;
  for (int ch = 0; ch < MAX_CH; ch++) {
    level[ch] = -1;
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

  noInterrupts();
  int64_t now;
  do { now = Now(); } while (now < t);
  WritePins(level);
  interrupts();

  if (now - t > late_max) { late_max = now - t; }
  if (watchdog) {
    AllOff();
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
    if (t >= until) { break; }
  }
  if (rose) { Rose(t); }
  interrupts();
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
    snprintf(reply, sizeof reply, "ok alloff %lld", (long long) Now());
    Say(reply);
  }
  else if (strcmp(word, "pin") == 0) {
    if (n != 2 || !chOk || a[1] < -1 || a[1] > 48) { Say("err pin <ch 0-7> <pin, -1 none>"); return; }
    AllOff();
    SetPin(ch, (int) a[1]);
    snprintf(reply, sizeof reply, "ok pin %d %lld", ch, a[1]);
    Say(reply);
  }
  else if (strcmp(word, "trig") == 0) {
    if (n != 1 || a[0] < -1 || a[0] > 48) { Say("err trig <pin, -1 none>"); return; }
    trig_pin = (int) a[0];
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
    snprintf(reply, sizeof reply, "ok trig %lld", a[0]);
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

  int64_t t = NextTime();
  int64_t expected = ExpectedTrigger(now);
  int64_t soon = t;
  if (expected != NONE && expected - CRITICAL_US < soon) { soon = expected - CRITICAL_US; }
  if (soon == NONE || soon - now > SERIAL_US) {
    int64_t until = soon == NONE ? NONE : soon - CRITICAL_US;
    if (soon == NONE) { until = now + SERIAL_US; } // still look at the trigger often
    FlushQueue(until);
    ReadSerial(until);
    return;
  }
  if (t != NONE && (t <= now || (t - now <= CRITICAL_US && (expected == NONE || t <= expected)))) {
    Apply(t);
    return;
  }
  if (expected != NONE && expected - now <= CRITICAL_US) {
    int64_t until = expected + TRIG_SLACK_US;
    if (t != NONE && t < until) { until = t; }
    WaitTrigger(until);
    return;
  }
}
