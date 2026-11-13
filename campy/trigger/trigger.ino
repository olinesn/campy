// Camera Sync - TTL triggering with Teensy board
// Inter-frame interval precision: ~ +/- 0.35 us
// Inter-frame interval accuracy: ~0.0035%
// Synchronicity: ~ 30 ns
//
// Command (from campy/trigger/arduino.py, or the Serial Monitor), comma-separated:
//   # dig pins, dig pin IDs, frame rate, test LED pin, test LED Hz[, sync pin, sync seed,
//   sync pulse us, shortest gap ms, longest gap ms]
// The test LED (for campy-synctest) is optional: -1 or nothing leaves it off.
// A command that ends after the frame rate (upstream campy's, and campy's
// before the test LED) starts the trigger as soon as the sketch it shipped did.
// It blinks from the same clock as the trigger, toggled while the loop waits
// for the trigger's edges, so it can move an edge by a few microseconds.
//
// The sync output is optional too: a command that ends after the test LED's Hz
// leaves it off (and a sketch from before it reads only that far, and throws
// the rest away). It is a train of pulses with random gaps, for a recording
// system that has its own clock (an ephys or photometry recorder): any four
// gaps in a row say where in the recording they are (campy/syncmatch.py).
// Pulse k rises at the sum of gaps 1..k+1, gap j = 1000 * (shortest + SplitMix64(seed, j)
// % (longest - shortest + 1)) us, and is high for the pulse's us; all from the
// trigger's time 0, when its first frame period starts (frame k's pulse rises at
// k * period + period / 2). The same as chronicrecord/chronic_timer.ino's sync train.
// It runs while the trigger does. Its edges are made in the loop's waits, so
// none may come just before a camera edge: an edge due within SYNC_GUARD_US
// before one (or on it) waits until SYNC_AFTER_US after it (SyncHold), which
// the computer works out the same way. Camera edges stay where they were.

// Declared here so the sketch also compiles as plain C++ (campy's tests run it)
int SetDigPins();
unsigned long SetFrameRate();
unsigned long SetFramePeriod(unsigned long fr);
void SetBlink();
void FlushSerialBuffer();
void ResetTimer();
void SetPinsHigh();
void SetPinsLow();
void Blink();
unsigned long Timer();
void SetSync();
void SyncStart();
void SyncOff();
void SyncPrepare();
void SyncStep(unsigned long now, unsigned long left);
unsigned long SyncHold(unsigned long mod);
uint64_t SplitMix64(uint64_t seed, uint64_t j);

// User-set parameters
uint32_t baudrate = 115200;

// Global variables
int DIG_out_pins[100];
int n_sync;
float frame_rate_in = 0;
float frame_rate_out = 0;
unsigned long frame_start, frame_period, time_zero;
uint32_t frame_count = 0;
int blink_pin = -1;               // test LED for campy-synctest; -1 = none
unsigned long blink_half = 0;     // half its period, microseconds; 0 = off
unsigned long blink_next = 0;
bool blink_on = false;

// The sync output (SetSync): -1 = none
#define SYNC_GUARD_US 50   // a sync edge due this close before a camera edge, or on it,
#define SYNC_AFTER_US 10   // waits until this long after it
#if defined(__AVR__)
#define SYNC_PREP_US 1500  // working out the next sync edge (64-bit sums, slow on an AVR) only
#else                      // when the next camera edge is further off than this
#define SYNC_PREP_US 200
#endif
int sync_pin = -1;
uint32_t sync_seed = 0;
unsigned long sync_width = 0, sync_min_ms = 0, sync_max_ms = 0;
uint32_t sync_j = 0;            // gaps drawn so far
uint64_t sync_rise = 0;         // this pulse's planned rise, us from time 0
unsigned long sync_rise_mod = 0; // the same, modulo the frame period
unsigned long sync_t0 = 0;      // micros() at time 0
unsigned long sync_at = 0;      // micros() when the next sync edge is due
bool sync_due = false;          // the next edge is worked out (sync_at)
bool sync_high = false;         // the sync pin's level now

void setup( void ) {

  Serial.begin(baudrate);
  delay(500);
  Serial.print("Set baudrate to ");
  Serial.print(baudrate);

  Serial.println("");
  Serial.print("Enter comma-separated string in this order: # dig pins, dig pin IDs, frame rate, test LED pin, test LED Hz"
    "[, sync pin, sync seed, sync pulse us, shortest gap ms, longest gap ms]:");

  // Set DigPins using Python or Serial Monitor input
  n_sync = SetDigPins();

  // Set Frame Rate using Python or Serial Monitor input
  frame_rate_out = SetFrameRate();
  frame_period = SetFramePeriod(frame_rate_out);
  SetBlink();
  SetSync();

  // Wait for streams and cams to initialize
  ResetTimer(); 
  
}


int SetDigPins() {
  
  // Receive Dig Pin array size from Python or Serial Monitor
  while (Serial.available() == 0) {}
  int num_pins = int( (unsigned int) Serial.parseFloat());

  // Print result and initialize array
  Serial.println("");
  Serial.print("Number of digital pins: ");
  Serial.print(num_pins);
  
  Serial.println("");
  Serial.print("Digital pins: ");
  
  for ( int i = 0; i < num_pins; i++ ) {
    // Receive digital pin IDs
    while (Serial.available() == 0) {}
    int input = int( (unsigned int) Serial.parseFloat());
    Serial.print(input);
    if (i+1 < num_pins) { Serial.print(","); }

    // Append pin number to array
    DIG_out_pins[i] = input;
  }

  // Set digital pins to OUTPUT mode and to LOW voltage
  for ( int i = 0; i < num_pins; i++ ) {
    pinMode(DIG_out_pins[i], OUTPUT);
    digitalWrite(DIG_out_pins[i], LOW);
  }

  return num_pins;
}


unsigned long SetFrameRate() {
  // Wait to receive frame rate from Python or Serial Monitor...
  while (Serial.available() == 0) {}
  frame_rate_in = Serial.parseFloat();

  // Output frame rate, avoid negative values
  if (frame_rate_in < 0) {
    frame_rate_out = 0;
  }
  else {
    frame_rate_out = frame_rate_in;
  }

  // Print results
  Serial.println("");
  Serial.print("Frame rate set to: ");
  Serial.print(frame_rate_out);
  Serial.print(" fps.");

  return frame_rate_out;
}


unsigned long SetFramePeriod( unsigned long fr ) {
  
  // Calculate frame period and pulse duration
  // Avoid divide-by-zero error
  if (fr == 0) {
    frame_period = 0xFFFFFFFF;
  }
  else {
    frame_period = 1e6 / fr;
  }

  Serial.println("");
  Serial.print("Frame period set to: ");
  Serial.print(frame_period);
  Serial.print(" microseconds.");

  return frame_period;
}


void SetBlink() {
  // Optional test LED: pin, then frequency. A sender that stops after the
  // frame rate (campy before the test LED, and upstream campy) has nothing
  // left to read, or a line end: the test LED is off, at once, so its
  // trigger starts as soon as with the sketch it shipped (no wait for numbers
  // that never come). One that goes on has its comma waiting.
  int pin = -1;
  float hz = 0;
  if (Serial.available() > 0 && Serial.peek() == ',') {
    pin = int(Serial.parseFloat());
    hz = Serial.parseFloat();
  }

  if (blink_pin >= 0) { digitalWrite(blink_pin, LOW); }
  blink_pin = -1;
  blink_half = 0;
  if (pin >= 0 && hz > 0) {
    blink_pin = pin;
    blink_half = (unsigned long) (5e5 / hz + 0.5);
    pinMode(blink_pin, OUTPUT);
    digitalWrite(blink_pin, LOW);
  }

  Serial.println("");
  if (blink_half > 0) {
    Serial.print("Test LED on pin ");
    Serial.print(blink_pin);
    Serial.print(", half period ");
    Serial.print(blink_half);
    Serial.print(" microseconds.");
  }
  else {
    Serial.print("Test LED off.");
  }
}


void Blink() {
  // Toggle the test LED when its half period is up (wrap-safe on micros())
  if (blink_half == 0) { return; }
  if ((long) (micros() - blink_next) >= 0) {
    blink_on = !blink_on;
    digitalWrite(blink_pin, blink_on ? HIGH : LOW);
    blink_next += blink_half;
  }
}


void SetSync() {
  // Optional sync output: pin, seed, pulse us, shortest and longest gap ms. A
  // command that ended after the test LED's Hz has nothing left to read (that
  // number was read when the timeout for one more digit was up), or a line
  // end (the Serial Monitor's): the sync output is off, at once. One that goes
  // on has its comma waiting, which ended the number before.
  SyncOff();
  sync_pin = -1;
  if (Serial.available() == 0 || Serial.peek() != ',') {
    Serial.println("");
    Serial.print("Sync output off.");
    return;
  }
  int pin = int(Serial.parseFloat());
  long seed = Serial.parseInt(); // a float holds only 24 bits
  float width = Serial.parseFloat();
  float min_ms = Serial.parseFloat();
  float max_ms = Serial.parseFloat();

  const char* why = 0;
  if (pin < 0) { why = "no pin"; }
  else if (frame_rate_out <= 0) { why = "no trigger"; }
  else if (seed < 0) { why = "a seed below 0"; }
  else if (min_ms < 1 || max_ms < min_ms || max_ms > 60000) { why = "gaps must be 1-60000 ms, shortest first"; }
  else if (width < 2 * SYNC_PREP_US || width + 2 * SYNC_PREP_US > min_ms * 1000) {
    why = "pulse too short, or too long for the gaps";
  }
  else if (frame_period / 2 <= SYNC_PREP_US + SYNC_GUARD_US + SYNC_AFTER_US) { why = "frame rate too high for this board"; }
  else {
    for (int i = 0; i < n_sync; i++) {
      if (DIG_out_pins[i] == pin) { why = "pin triggers a camera"; }
    }
    if (pin == blink_pin) { why = "pin is the test LED's"; }
  }

  Serial.println("");
  if (why) {
    Serial.print("Sync output off: ");
    Serial.print(why);
    Serial.print(".");
    return;
  }
  sync_pin = pin;
  sync_seed = (uint32_t) seed;
  sync_width = (unsigned long) width;
  sync_min_ms = (unsigned long) min_ms;
  sync_max_ms = (unsigned long) max_ms;
  pinMode(sync_pin, OUTPUT);
  digitalWrite(sync_pin, LOW);
  Serial.print("Sync output on pin ");
  Serial.print(sync_pin);
  Serial.print(", seed ");
  Serial.print((unsigned long) sync_seed);
  Serial.print(", ");
  Serial.print(sync_width);
  Serial.print(" us pulses, gaps ");
  Serial.print(sync_min_ms);
  Serial.print(" to ");
  Serial.print(sync_max_ms);
  Serial.print(" ms.");
}


uint64_t SplitMix64(uint64_t seed, uint64_t j) {
  // The j-th number of SplitMix64 from seed (Steele, Lea and Flood 2014): the
  // same in campy/syncmatch.py, which works the sync pulses out from the seed
  uint64_t z = seed + j * 0x9E3779B97F4A7C15ULL;
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}


unsigned long SyncHold(unsigned long mod) {
  // How long a sync edge planned at mod microseconds into a frame period
  // waits: the camera edges are at 0 and frame_period / 2 in each, and one
  // due within SYNC_GUARD_US before either (or on it) waits until
  // SYNC_AFTER_US after it. 0: it needn't wait. (campy/syncmatch.py: Hold)
  unsigned long half = frame_period / 2, wait = 0;
  for (int i = 0; i < 3; i++) {
    unsigned long r = (mod + wait) % frame_period;
    unsigned long to_edge = r == 0 ? 0 : (r <= half ? half - r : frame_period - r);
    if (to_edge > SYNC_GUARD_US) { break; }
    wait += to_edge + SYNC_AFTER_US;
  }
  return wait;
}


void SyncPrepare() {
  // The next sync edge: this pulse's fall, or the next pulse's rise
  uint64_t t;
  unsigned long mod;
  if (sync_high) {
    t = sync_rise + sync_width;
    mod = (sync_rise_mod + sync_width % frame_period) % frame_period;
  }
  else {
    sync_j++;
    unsigned long gap = 1000UL * (sync_min_ms + (unsigned long) (SplitMix64(sync_seed, sync_j)
      % (uint64_t) (sync_max_ms - sync_min_ms + 1)));
    sync_rise += gap;
    sync_rise_mod = (sync_rise_mod + gap % frame_period) % frame_period;
    t = sync_rise;
    mod = sync_rise_mod;
  }
  sync_at = sync_t0 + (unsigned long) t + SyncHold(mod); // micros() wraps as this does
  sync_due = true;
}


void SyncStep(unsigned long now, unsigned long left) {
  // In the loop's waits: make the sync edge once it's due, and work out the
  // next one, but only while the next camera edge is more than SYNC_PREP_US
  // off (left), so that the sums never hold up a camera edge
  if (sync_pin < 0) { return; }
  if (sync_due) {
    if ((long) (now - sync_at) >= 0) {
      sync_high = !sync_high;
      digitalWrite(sync_pin, sync_high ? HIGH : LOW);
      sync_due = false;
    }
  }
  else if (left > SYNC_PREP_US) {
    SyncPrepare();
  }
}


void SyncStart() {
  // Time 0: the first frame period's start
  sync_t0 = frame_start;
  sync_j = 0;
  sync_rise = 0;
  sync_rise_mod = 0;
  sync_high = false;
  sync_due = false;
  if (sync_pin >= 0) {
    digitalWrite(sync_pin, LOW);
    SyncPrepare();
  }
}


void SyncOff() {
  if (sync_pin >= 0) { digitalWrite(sync_pin, LOW); }
  sync_high = false;
  sync_due = false;
}


void FlushSerialBuffer() {
  
  while (Serial.available() != 0) {
    Serial.parseFloat();
  }
  
}


void ResetTimer() {
  
  delay(4000);
  FlushSerialBuffer();
  frame_count = 0;
  time_zero = frame_count * frame_period;
  frame_start = micros() + time_zero;
  blink_on = false;
  if (blink_pin >= 0) { digitalWrite(blink_pin, LOW); }
  blink_next = micros();
  SyncStart();
  
}


void SetPinsHigh() {
  
  // Disable interrupts to synchronize TTLs
  noInterrupts();
  for ( int i = 0; i < n_sync; i++ ) {
    digitalWrite(DIG_out_pins[i], HIGH);
  }
  interrupts();
  
}


void SetPinsLow() {
  // Disable interrupts to synchronize TTLs
  noInterrupts();
  for ( int i = 0; i < n_sync; i++ ) {
    digitalWrite(DIG_out_pins[i], LOW);
  }
  interrupts();
}


unsigned long Timer() {
  
  return micros() + time_zero - frame_start;
}

void loop( void ) {
  
  if (Serial.available())
  {
    SetPinsLow();
    SyncOff();
    n_sync = SetDigPins();
    frame_rate_out = SetFrameRate();
    frame_period = SetFramePeriod(frame_rate_out);
    SetBlink();
    SetSync();
    ResetTimer();
  }

  if (frame_rate_out > 0) {
    SetPinsLow();

    // Wait until end of this pulse period (t: Timer(), so frame_start + t is micros())
    unsigned long t;
    while ((t = Timer()) < frame_period / 2) { Blink(); SyncStep(frame_start + t, frame_period / 2 - t); }

    SetPinsHigh();

    // Wait until end of this frame period
    while ((t = Timer()) < frame_period) { Blink(); SyncStep(frame_start + t, frame_period - t); }

    frame_start = frame_start + frame_period;
  }
  else {
    Blink();
  }
  
}
