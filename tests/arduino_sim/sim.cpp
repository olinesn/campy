// Runs trigger.ino against a simulated clock and prints every pin change as
// "<microseconds> <pin> <level>".
//   sim <end_us> <command> [<at_us>:<command> ...]
// The first command is waiting at power-up; the others arrive at their times.
#include "Arduino.h"
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>
#include <vector>

void setup();
void loop();

static unsigned long long now_us = 0;
static std::string input;
static size_t pos = 0;
static std::vector<std::pair<unsigned long long, std::string> > later;
static std::map<int, int> level;
SimSerial Serial;

unsigned long micros() { return (unsigned long) (++now_us); } // each call takes a microsecond
void delay(unsigned long ms) { now_us += 1000ULL * ms; }
void pinMode(int, int) {}
void noInterrupts() {}
void interrupts() {}

void digitalWrite(int pin, int value) {
	if (level.count(pin) && level[pin] == value) return;
	level[pin] = value;
	std::printf("%llu %d %d\n", now_us, pin, value);
}

int SimSerial::available() {
	while (!later.empty() && later.front().first <= now_us) {
		input += later.front().second;
		later.erase(later.begin());
	}
	return (int) (input.size() - pos);
}

float SimSerial::parseFloat() {
	// Like Stream::parseFloat: skip to a number, read it; nothing there = timeout, 0
	available();
	while (pos < input.size() && !(std::isdigit((unsigned char) input[pos]) || input[pos] == '-' || input[pos] == '.')) pos++;
	if (pos >= input.size()) { now_us += 1000000; return 0; } // the 1 s default timeout
	size_t end = pos;
	while (end < input.size() && (std::isdigit((unsigned char) input[end]) || input[end] == '-' || input[end] == '.')) end++;
	float value = std::strtof(input.substr(pos, end - pos).c_str(), 0);
	pos = end;
	return value;
}

int main(int argc, char** argv) {
	if (argc < 3) return 2;
	unsigned long long end = std::strtoull(argv[1], 0, 10);
	input = argv[2];
	for (int i = 3; i < argc; i++) {
		std::string arg = argv[i];
		size_t colon = arg.find(':');
		later.push_back(std::make_pair(std::strtoull(arg.substr(0, colon).c_str(), 0, 10), arg.substr(colon + 1)));
	}
	setup();
	while (now_us < end) {
		loop();
		now_us++; // a real clock runs whether or not the sketch reads it
	}
	return 0;
}
