// ChronicRecord timer, for an Arduino Nano ESP32: makes the rig's TTLs (the
// OneBox record gate, the camera trigger and the two LED controllers) from
// one crystal clock, on a schedule the Raspberry Pi loads over USB serial
// (chronicrecord/chronic_control.py). Once started it runs the schedule on
// its own: the Pi, or its USB link, can go away without changing an edge.
//
// Every change is due at a time in microseconds from "go" (session time).
// Changes due at the same time are written together (one register write on
// the ESP32), with interrupts off for the last few hundred microseconds
// before them, so an edge lands within a few microseconds of its time.
//
// Commands, one per line (\n or \r), each answered at once with a line
// starting "ok" or "err":
//   id                                    -> ok chronic_timer <version>
//   pin <ch> <pin>                        channel ch (0-7) on Arduino pin pin; -1 = none
//   clear                                 forget the schedule
//   edge <ch> <t_us> <0|1>                set channel ch low/high at t_us
//                                         (edges in order of time)
//   train <ch> <start_us> <count> <rate_mHz> <width_us>
//                                         count pulses on ch: pulse k rises at
//                                         start_us + floor(k * 1e9 / rate_mHz),
//                                         high for width_us (trains in order)
//   go                                    start: session time 0 is 20 ms later
//   end <t_us>                            the session ends at t_us: nothing due after
//                                         it happens (a pulse already high still falls
//                                         on time), then all channels go low
//   stop                                  end now
//   status                                -> ok status <idle|running> t=... pulses=...
// What it says on its own, as things happen:
//   ready chronic_timer <version>         at power-up
//   ev <t_us> <ch> <level> <late_us>      an edge was made (late_us after its time)
//   camon <train> <first_rise_us>         a train's first pulse rose
//   cam <train> <pulses> <first_rise_us> <last_fall_us>   a train is over
//   done <t_us> pulses=<n> late_max=<us> lost=<lines>     the session is over, all low
// Lines it couldn't send (the Pi not reading) are counted in "lost", never waited for.

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#if defined(ESP32)
#include "soc/gpio_struct.h"
#endif

#define VERSION 1
#define MAX_CH 8
#define MAX_EDGES 4096
#define MAX_TRAINS 1024
#define NONE INT64_MAX
#define GO_DELAY_US 20000   // session time 0, after "go": room to answer first
#define CRITICAL_US 300     // interrupts off this long before a change
#define SERIAL_US 1500      // serial only when the next change is further off

// Declared here so the sketch also compiles as plain C++ (campy's tests run it)
struct Edge { int64_t t; uint8_t ch; uint8_t level; };
struct Train { int64_t start; uint32_t count; uint32_t rate_mhz; uint32_t width; uint8_t ch; };
uint64_t Micros64();
int64_t Now();
int64_t Rise(const Train& tr, uint32_t k);
int64_t LastFall(const Train& tr);
void Command(char* line);
void ReadSerial(int64_t until);
void Say(const char* line);
void Queue(const char* line);
void FlushQueue(int64_t until);
void SetPin(int ch, int pin);
void AllLow();
void Settle();
int64_t NextTime();
void Apply(int64_t t);
void Finish();

int ch_pin[MAX_CH];
#if defined(ESP32)
uint32_t ch_mask[MAX_CH];
uint8_t ch_bank[MAX_CH];
#endif
Edge edges[MAX_EDGES];
Train trains[MAX_TRAINS];
int n_edges = 0, n_trains = 0;

// The session
bool running = false;
uint64_t t0 = 0;           // Micros64() at session time 0
int64_t end_t = NONE;      // session time it ends at
int ei = 0;                // next edge
int ti = 0;                // current train
uint32_t tk = 0;           // its next pulse
bool high = false;         // that pulse is high: its fall comes next
int64_t first_rise = -1;
uint32_t pulses = 0;       // pulses made this session
int64_t late_max = 0;
uint32_t lost = 0;

// Lines waiting to be said, while changes were close
#define QUEUE 64
char queue[QUEUE][64];
int q_head = 0, q_len = 0;

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
  return (int64_t) (Micros64() - t0);
}


int64_t Rise(const Train& tr, uint32_t k) {
  return tr.start + (int64_t) ((uint64_t) k * 1000000000ULL / tr.rate_mhz);
}


int64_t LastFall(const Train& tr) {
  return Rise(tr, tr.count - 1) + tr.width;
}


void setup() {
  Serial.begin(115200);
#if defined(ESP32)
  Serial.setTxTimeoutMs(0); // never wait for a Pi that isn't reading
#endif
  for (int ch = 0; ch < MAX_CH; ch++) { ch_pin[ch] = -1; }
  // The rig's wiring (chronicrecord/README.md): gate, camera, LED1, LED2 on
  // D2-D5, low from power-up. The Pi sets them again before each session.
  SetPin(0, 2);
  SetPin(1, 3);
  SetPin(2, 4);
  SetPin(3, 5);
  char text[40];
  snprintf(text, sizeof text, "ready chronic_timer %d", VERSION);
  Say(text);
}


void SetPin(int ch, int pin) {
  if (ch_pin[ch] >= 0) { digitalWrite(ch_pin[ch], LOW); }
  ch_pin[ch] = pin;
  if (pin < 0) { return; }
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
#if defined(ESP32)
  int gpio = digitalPinToGPIONumber(pin);
  ch_bank[ch] = gpio > 31 ? 1 : 0;
  ch_mask[ch] = 1UL << (gpio & 31);
#endif
}


void WritePins(const int8_t* level) {
  // level[ch]: 1 high, 0 low, -1 unchanged. All at once.
#if defined(ESP32)
  uint32_t set0 = 0, set1 = 0, clr0 = 0, clr1 = 0;
  for (int ch = 0; ch < MAX_CH; ch++) {
    if (level[ch] < 0 || ch_pin[ch] < 0) { continue; }
    if (level[ch]) { if (ch_bank[ch]) set1 |= ch_mask[ch]; else set0 |= ch_mask[ch]; }
    else { if (ch_bank[ch]) clr1 |= ch_mask[ch]; else clr0 |= ch_mask[ch]; }
  }
  if (set0) { GPIO.out_w1ts = set0; }
  if (set1) { GPIO.out1_w1ts.val = set1; }
  if (clr0) { GPIO.out_w1tc = clr0; }
  if (clr1) { GPIO.out1_w1tc.val = clr1; }
#else
  for (int ch = 0; ch < MAX_CH; ch++) {
    if (level[ch] >= 0 && ch_pin[ch] >= 0) { digitalWrite(ch_pin[ch], level[ch] ? HIGH : LOW); }
  }
#endif
}


void AllLow() {
  int8_t level[MAX_CH];
  for (int ch = 0; ch < MAX_CH; ch++) { level[ch] = 0; }
  WritePins(level);
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
  // What's waiting, until it's time (session time) to make the next change
  while (q_len > 0 && (!running || Now() < until)) {
    Say(queue[q_head]);
    q_head = (q_head + 1) % QUEUE;
    q_len--;
  }
}


bool Number(char* text, long long* value) {
  if (text == NULL) { return false; }
  char* stop;
  *value = strtoll(text, &stop, 10);
  return *stop == 0;
}


void Command(char* text) {
  char reply[96];
  char* word = strtok(text, " \t,");
  if (word == NULL) { return; }
  long long a[5];
  int n = 0;
  char* arg;
  while (n < 5 && (arg = strtok(NULL, " \t,")) != NULL) {
    if (!Number(arg, &a[n])) { Say("err not a number"); return; }
    n++;
  }
  if (strtok(NULL, " \t,") != NULL) { Say("err too many numbers"); return; }

  if (strcmp(word, "id") == 0) {
    snprintf(reply, sizeof reply, "ok chronic_timer %d", VERSION);
    Say(reply);
  }
  else if (strcmp(word, "status") == 0) {
    if (running) {
      snprintf(reply, sizeof reply, "ok status running t=%lld pulses=%lu edges=%d/%d trains=%d/%d late_max=%lld",
        (long long) Now(), (unsigned long) pulses, ei, n_edges, ti, n_trains, (long long) late_max);
    }
    else {
      snprintf(reply, sizeof reply, "ok status idle edges=%d trains=%d", n_edges, n_trains);
    }
    Say(reply);
  }
  else if (strcmp(word, "stop") == 0 || strcmp(word, "end") == 0) {
    bool stop = word[0] == 's';
    if (stop ? n != 0 : n != 1) { Say(stop ? "err stop takes no numbers" : "err end <t_us>"); return; }
    if (!running) {
      AllLow();
      Say(stop ? "ok stop idle" : "err not running");
      return;
    }
    int64_t t = stop ? Now() : (int64_t) a[0];
    if (t < Now()) { t = Now(); }
    if (t < end_t) { end_t = t; }
    snprintf(reply, sizeof reply, "ok %s %lld", word, (long long) end_t);
    Say(reply);
  }
  else if (running) {
    Say("err running: stop first");
  }
  else if (strcmp(word, "pin") == 0) {
    if (n != 2 || a[0] < 0 || a[0] >= MAX_CH || a[1] < -1 || a[1] > 48) { Say("err pin <ch 0-7> <pin, -1 none>"); return; }
    SetPin((int) a[0], (int) a[1]);
    snprintf(reply, sizeof reply, "ok pin %lld %lld", a[0], a[1]);
    Say(reply);
  }
  else if (strcmp(word, "clear") == 0) {
    n_edges = 0;
    n_trains = 0;
    Say("ok clear");
  }
  else if (strcmp(word, "edge") == 0) {
    if (n != 3 || a[0] < 0 || a[0] >= MAX_CH || a[1] < 0 || (a[2] != 0 && a[2] != 1)) { Say("err edge <ch> <t_us> <0|1>"); return; }
    if (ch_pin[a[0]] < 0) { Say("err that channel has no pin"); return; }
    if (n_edges > 0 && a[1] < edges[n_edges - 1].t) { Say("err edges must come in order of time"); return; }
    if (n_edges == MAX_EDGES) { Say("err too many edges"); return; }
    edges[n_edges].t = a[1];
    edges[n_edges].ch = (uint8_t) a[0];
    edges[n_edges].level = (uint8_t) a[2];
    n_edges++;
    snprintf(reply, sizeof reply, "ok edge %d", n_edges);
    Say(reply);
  }
  else if (strcmp(word, "train") == 0) {
    if (n != 5 || a[0] < 0 || a[0] >= MAX_CH || a[1] < 0 || a[2] < 1 || a[2] > 0xFFFFFFFFLL
        || a[3] < 1 || a[3] > 0xFFFFFFFFLL || a[4] < 1) {
      Say("err train <ch> <start_us> <count> <rate_mHz> <width_us>");
      return;
    }
    if (ch_pin[a[0]] < 0) { Say("err that channel has no pin"); return; }
    Train tr = {a[1], (uint32_t) a[2], (uint32_t) a[3], (uint32_t) a[4], (uint8_t) a[0]};
    if ((uint64_t) tr.width * tr.rate_mhz >= 1000000000ULL) { Say("err width must be under the period"); return; }
    if (n_trains > 0 && tr.start <= LastFall(trains[n_trains - 1])) { Say("err trains must come in order, apart"); return; }
    if (n_trains == MAX_TRAINS) { Say("err too many trains"); return; }
    trains[n_trains++] = tr;
    snprintf(reply, sizeof reply, "ok train %d", n_trains);
    Say(reply);
  }
  else if (strcmp(word, "go") == 0) {
    if (n_edges == 0 && n_trains == 0) { Say("err nothing to run"); return; }
    for (int i = 0; i < n_edges; i++) {
      for (int j = 0; j < n_trains; j++) {
        if (edges[i].ch == trains[j].ch) { Say("err an edge is on a train's channel"); return; }
      }
    }
    AllLow();
    ei = 0; ti = 0; tk = 0; high = false; first_rise = -1;
    pulses = 0; late_max = 0; lost = 0;
    end_t = NONE;
    t0 = Micros64() + GO_DELAY_US;
    running = true;
    Say("ok go");
  }
  else {
    Say("err unknown command");
  }
}


void ReadSerial(int64_t until) {
  // A command, if it came; reading stops when it's time for the next change
  while ((!running || Now() < until) && Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) { return; }
    if (c == '\n' || c == '\r') {
      line[line_len] = 0;
      line_len = 0;
      Command(line);
      return; // one command a turn: loop() checks the time between them
    }
    if (line_len < (int) sizeof line - 1) { line[line_len++] = (char) c; }
  }
}


void Settle() {
  // Drop what the end cut off, and end trains that are over
  while (ei < n_edges && edges[ei].t > end_t) { ei = n_edges; }
  while (ti < n_trains && !high && (tk >= trains[ti].count || Rise(trains[ti], tk) > end_t)) {
    char text[64];
    snprintf(text, sizeof text, "cam %d %lu %lld %lld", ti, (unsigned long) tk, (long long) first_rise,
      (long long) (tk > 0 ? Rise(trains[ti], tk - 1) + trains[ti].width : -1));
    Queue(text);
    ti++; tk = 0; first_rise = -1;
  }
}


int64_t NextTime() {
  // Session time of the next change; NONE when nothing is left
  int64_t t = NONE;
  if (ei < n_edges) { t = edges[ei].t; }
  if (ti < n_trains) {
    int64_t r = Rise(trains[ti], tk);
    int64_t next = high ? r + trains[ti].width : r;
    if (next < t) { t = next; }
  }
  return t;
}


void Apply(int64_t t) {
  // Every change due at t, at once
  int8_t level[MAX_CH];
  for (int ch = 0; ch < MAX_CH; ch++) { level[ch] = -1; }
  int first = ei;
  while (ei < n_edges && edges[ei].t == t) {
    level[edges[ei].ch] = edges[ei].level;
    ei++;
  }
  bool rose = false;
  if (ti < n_trains) {
    int64_t r = Rise(trains[ti], tk);
    if (!high && r == t) { level[trains[ti].ch] = 1; rose = true; }
    else if (high && r + trains[ti].width == t) { level[trains[ti].ch] = 0; }
  }

  noInterrupts();
  int64_t now;
  do { now = Now(); } while (now < t);
  WritePins(level);
  interrupts();

  int64_t late = now - t;
  if (late > late_max) { late_max = late; }
  if (ti < n_trains && level[trains[ti].ch] >= 0) {
    if (rose) {
      high = true;
      pulses++;
      if (tk == 0) {
        first_rise = t;
        char text[64];
        snprintf(text, sizeof text, "camon %d %lld", ti, (long long) t);
        Queue(text);
      }
    }
    else {
      high = false;
      tk++;
    }
  }
  for (int i = first; i < ei; i++) {
    char text[64];
    snprintf(text, sizeof text, "ev %lld %d %d %lld", (long long) edges[i].t, edges[i].ch, edges[i].level, (long long) late);
    Queue(text);
  }
}


void Finish() {
  AllLow();
  running = false;
  n_edges = 0;
  n_trains = 0;
  char text[64];
  snprintf(text, sizeof text, "done %lld pulses=%lu late_max=%lld lost=%lu",
    (long long) Now(), (unsigned long) pulses, (long long) late_max, (unsigned long) lost);
  Queue(text);
}


void loop() {
  if (!running) {
    FlushQueue(NONE);
    ReadSerial(NONE);
    return;
  }
  Settle();
  int64_t t = NextTime();
  int64_t now = Now();
  if (t == NONE) {
    // Nothing left: all low, at the end if one was set later than that
    if (end_t == NONE || now >= end_t) { Finish(); }
    else if (end_t - now > SERIAL_US) { FlushQueue(end_t - CRITICAL_US); ReadSerial(end_t - CRITICAL_US); }
    return;
  }
  if (t - now > SERIAL_US) {
    FlushQueue(t - CRITICAL_US);
    ReadSerial(t - CRITICAL_US);
    return;
  }
  if (t - now > CRITICAL_US) { return; }
  Apply(t);
}
