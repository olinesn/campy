// Just enough of the Arduino API to run campy/trigger/trigger.ino (and
// chronicrecord/chronic_timer/chronic_timer.ino, campy/outputs_board/campy_outputs) on a computer,
// with a simulated clock (see sim.cpp). For tests only.
#pragma once
#include <cstdint>

#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT 0
#define INPUT_PULLDOWN 9

unsigned long micros();
void delay(unsigned long ms);
void pinMode(int pin, int mode);
void digitalWrite(int pin, int level);
int digitalRead(int pin);
void noInterrupts();
void interrupts();
// A PIO state machine's queue (chronic_timer.ino, compiled with SIM_PIO): see sim.cpp
void SimPioInit(int first_pin, int pins);
void SimPioPush(uint32_t word);
bool SimPioFull();
bool SimPioStalled();
void SimPioClearStall();

struct SimSerial {
	void begin(uint32_t) {}
	int available();
	int read();
	// What USB takes now without waiting (SIM_USB_TX, see sim.cpp); a large number when it isn't set
	int availableForWrite();
	// The host has the port open (always, here)
	explicit operator bool() const { return true; }
	float parseFloat();
	// What the sketch says: Print's formatting (floats with 2 decimals)
	void print(const char* text);
	void print(int value);
	void print(unsigned int value);
	void print(long value);
	void print(unsigned long value);
	void print(double value);
	template <typename T> void println(T value) { print(value); print("\r\n"); }
};
extern SimSerial Serial;
