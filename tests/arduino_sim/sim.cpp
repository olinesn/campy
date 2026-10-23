// Runs trigger.ino (or another sketch) against a simulated clock and prints every pin change as
// "<microseconds> <pin> <level>", and what the sketch says on its serial port
// as "serial <microseconds> <text>" (text with \r and \n written as such).
//   sim <end_us> <command> [<at_us>:<command> ...]
// The first command is waiting at power-up; the others arrive at their times.
// SIM_START_US in the environment starts the clock there instead of at 0 (to
// run a sketch across micros()'s 32-bit wrap); all times printed are the clock's.
// SIM_INPUT makes an input pin a pulse train, as a camera trigger:
// "<pin>:<start_us>:<period_us>:<high_us>:<count>", several separated by ";"
// (digitalRead: HIGH during a pulse). In live mode, times from the start.
//
//   sim live
// Runs it in real time instead, as a serial device: it reads its commands
// from stdin and says what it says on stdout (a pseudo-terminal's master end,
// so that campy can open the other end as the Arduino's port), and logs to
// stderr every pin change, "rx <microseconds> <text>" for what it received
// and "serial <microseconds> <text>" for what it said. Not on Windows.
#include "Arduino.h"
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>
#ifndef _WIN32
#include <poll.h>
#include <time.h>
#include <unistd.h>
#endif

void setup();
void loop();

static bool live = false;
static std::chrono::steady_clock::time_point started;
static unsigned long long now_us = 0;
static std::string input;
static size_t pos = 0;
static std::vector<std::pair<unsigned long long, std::string> > later;
static std::map<int, int> level;
struct Pulses { int pin; unsigned long long start, period, high, count; };
static std::vector<Pulses> inputs;
SimSerial Serial;

static unsigned long long Now() {
	if (live) return (unsigned long long) std::chrono::duration_cast<std::chrono::microseconds>(
		std::chrono::steady_clock::now() - started).count();
	return now_us;
}

static void Sleep(unsigned long long us) {
	// Live only, which is not on Windows (MinGW may have no std::thread)
#ifndef _WIN32
	struct timespec wait = {(time_t) (us / 1000000), (long) (us % 1000000) * 1000};
	nanosleep(&wait, 0);
#endif
}

unsigned long micros() {
	if (live) {
		Sleep(50); // not a whole core for the sketch's waits
		return (unsigned long) Now();
	}
	return (unsigned long) (++now_us); // each call takes a microsecond
}

void delay(unsigned long ms) {
	if (live) Sleep(1000ULL * ms);
	else now_us += 1000ULL * ms;
}

void pinMode(int, int) {}

int digitalRead(int pin) {
	unsigned long long t = Now();
	for (const Pulses& p : inputs) {
		if (p.pin != pin || t < p.start || p.period == 0) continue;
		unsigned long long k = (t - p.start) / p.period;
		if (k < p.count && t - p.start - k * p.period < p.high) return HIGH;
	}
	return LOW;
}

static void ReadInputs() {
	const char* text = std::getenv("SIM_INPUT");
	if (!text) return;
	std::string all = text;
	size_t at = 0;
	while (at < all.size()) {
		size_t end = all.find(';', at);
		if (end == std::string::npos) end = all.size();
		Pulses p = {0, 0, 0, 0, 0};
		if (std::sscanf(all.substr(at, end - at).c_str(), "%d:%llu:%llu:%llu:%llu", &p.pin, &p.start, &p.period,
				&p.high, &p.count) == 5) inputs.push_back(p);
		at = end + 1;
	}
}
void noInterrupts() {}
void interrupts() {}

void digitalWrite(int pin, int value) {
	if (level.count(pin) && level[pin] == value) return;
	level[pin] = value;
	std::fprintf(live ? stderr : stdout, "%llu %d %d\n", Now(), pin, value);
}

static std::string Escaped(const std::string& text) {
	std::string out;
	for (char c : text) {
		if (c == '\r') out += "\\r";
		else if (c == '\n') out += "\\n";
		else out += c;
	}
	return out;
}

static void Say(const std::string& text) {
#ifndef _WIN32
	if (live) {
		if (write(1, text.data(), text.size()) < 0) std::exit(0);
		std::fprintf(stderr, "serial %llu %s\n", Now(), Escaped(text).c_str());
		return;
	}
#endif
	std::printf("serial %llu %s\n", Now(), Escaped(text).c_str());
}

void SimSerial::print(const char* text) { Say(text); }
void SimSerial::print(int value) { Say(std::to_string(value)); }
void SimSerial::print(unsigned int value) { Say(std::to_string(value)); }
void SimSerial::print(long value) { Say(std::to_string(value)); }
void SimSerial::print(unsigned long value) { Say(std::to_string(value)); }
void SimSerial::print(double value) {
	char text[64];
	std::snprintf(text, sizeof text, "%.2f", value);
	Say(text);
}

static void Receive(int wait_ms) {
	// Live: what came on stdin, waiting up to wait_ms for it. Ends at its end.
#ifndef _WIN32
	struct pollfd in = {0, POLLIN, 0};
	if (poll(&in, 1, wait_ms) <= 0) return;
	char data[256];
	ssize_t n = read(0, data, sizeof data);
	if (n <= 0) std::exit(0);
	std::string text(data, (size_t) n);
	std::fprintf(stderr, "rx %llu %s\n", Now(), Escaped(text).c_str());
	input += text;
#else
	std::exit(2);
#endif
}

int SimSerial::available() {
	if (live) Receive(0); // a look, not a wait: the sketch's loop runs on (micros() paces it)
	while (!later.empty() && later.front().first <= now_us) {
		input += later.front().second;
		later.erase(later.begin());
	}
	return (int) (input.size() - pos);
}

int SimSerial::read() {
	if (available() <= 0) return -1;
	return (unsigned char) input[pos++];
}

static int TimedPeek() {
	// Stream::timedPeek: the next character, waiting up to the 1 s default
	// timeout for it; -1 if none came
	unsigned long long deadline = Now() + 1000000;
	while (true) {
		Serial.available();
		if (pos < input.size()) return (unsigned char) input[pos];
		if (live) {
			unsigned long long now = Now();
			if (now >= deadline) return -1;
			Receive((int) ((deadline - now + 999) / 1000));
		}
		else if (later.empty() || later.front().first > deadline) {
			now_us = deadline;
			return -1;
		}
		else now_us = later.front().first;
	}
}

static bool Numeric(int c) {
	return std::isdigit(c) || c == '-' || c == '.';
}

float SimSerial::parseFloat() {
	// Like Stream::parseFloat: skip to a number and read it, waiting up to the
	// timeout for each character, so a number at the end of what came is read
	// only once the timeout is up. Nothing there: 0, after the timeout.
	int c;
	while ((c = TimedPeek()) >= 0 && !Numeric(c)) pos++;
	if (c < 0) return 0;
	std::string number;
	while ((c = TimedPeek()) >= 0 && Numeric(c)) {
		number += (char) c;
		pos++;
	}
	return std::strtof(number.c_str(), 0);
}

int main(int argc, char** argv) {
	ReadInputs();
	if (argc == 2 && std::strcmp(argv[1], "live") == 0) {
		live = true;
		started = std::chrono::steady_clock::now();
		setup();
		while (true) loop();
	}
	if (argc < 3) return 2;
	if (std::getenv("SIM_START_US")) now_us = std::strtoull(std::getenv("SIM_START_US"), 0, 10);
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
