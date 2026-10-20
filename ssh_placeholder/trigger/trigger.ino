// Camera Sync - TTL triggering with Teensy board
// Inter-frame interval precision: ~ +/- 0.35 us
// Inter-frame interval accuracy: ~0.0035%
// Synchronicity: ~ 30 ns
//
// Command (from campy/trigger/arduino.py, or the Serial Monitor), comma-separated:
//   # dig pins, dig pin IDs, frame rate, test LED pin, test LED Hz
// The test LED (for campy-synctest) is optional: -1 or nothing leaves it off.
// It blinks from the same clock as the trigger, toggled while the loop waits
// for the trigger's edges, so it can move an edge by a few microseconds.

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

void setup( void ) {

  Serial.begin(baudrate);
  delay(500);
  Serial.print("Set baudrate to ");
  Serial.print(baudrate);

  Serial.println("");
  Serial.print("Enter comma-separated string in this order: # dig pins, dig pin IDs, frame rate, test LED pin, test LED Hz:");

  // Set DigPins using Python or Serial Monitor input
  n_sync = SetDigPins();

  // Set Frame Rate using Python or Serial Monitor input
  frame_rate_out = SetFrameRate();
  frame_period = SetFramePeriod(frame_rate_out);
  SetBlink();

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
  // frame rate (campy before the test LED) times out here and leaves it off.
  int pin = int(Serial.parseFloat());
  float hz = Serial.parseFloat();

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
    n_sync = SetDigPins();
    frame_rate_out = SetFrameRate();
    frame_period = SetFramePeriod(frame_rate_out);
    SetBlink();
    ResetTimer();
  }

  if (frame_rate_out > 0) {
    SetPinsLow();

    // Wait until end of this pulse period
    while (Timer() < frame_period / 2) { Blink(); }

    SetPinsHigh();

    // Wait until end of this frame period
    while (Timer() < frame_period) { Blink(); }

    frame_start = frame_start + frame_period;
  }
  else {
    Blink();
  }
  
}
