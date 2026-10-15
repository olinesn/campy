// Just enough of the Arduino API to run campy/trigger/trigger.ino on a computer,
// with a simulated clock (see sim.cpp). For tests only.
#pragma once
#include <cstdint>

#define HIGH 1
#define LOW 0
#define OUTPUT 1

unsigned long micros();
void delay(unsigned long ms);
void pinMode(int pin, int mode);
void digitalWrite(int pin, int level);
void noInterrupts();
void interrupts();

struct SimSerial {
	void begin(uint32_t) {}
	int available();
	float parseFloat();
	template <typename T> void print(T) {}
	template <typename T> void println(T) {}
};
extern SimSerial Serial;
