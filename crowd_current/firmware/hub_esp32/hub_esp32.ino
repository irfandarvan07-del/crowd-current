/*
  Crowd Current - HUB (Classic ESP32 DevKit / NodeMCU-32S / ESP32-WROOM-32).
  Talks to 3 room Unos over UART and to the laptop over USB Serial.

  Arduino IDE:
    Board: "ESP32 Dev Module" (or "DOIT ESP32 DEVKIT V1")
    Upload speed: 921600 or 115200

  Hardware Connections:
    - Laptop: USB cable to ESP32 (Hardware UART0 @ 115200 baud via CP2102/CH340)
    - Room 1: Hardware Serial1 (RX = GPIO 4,  TX = GPIO 5)  @ 38400 baud
    - Room 2: Hardware Serial2 (RX = GPIO 16, TX = GPIO 17) @ 38400 baud
    - Room 3: Software Serial  (RX = GPIO 18, TX = GPIO 19) @ 38400 baud
    - 4-Channel Relays (Active-LOW):
        IN1 (Room 1 Fan): GPIO 21
        IN2 (Room 2 Fan): GPIO 22
        IN3 (Room 3 Fan): GPIO 23
        IN4 (Aux Siren):  GPIO 25

  *** IMPORTANT FOR CLASSIC ESP32: ***
  - GPIO 6, 7, 8, 9, 10, 11 are connected to internal SPI flash -> NEVER USE THEM.
  - GPIO 1 and 3 are used by the USB port to the laptop.
  - All selected pins below are 100% safe general-purpose GPIOs.
*/
#include <Arduino.h>
#include <SoftwareSerial.h>

const int NR = 3;

// Safe pin mapping for classic ESP32 DevKit
const int RX_PIN[NR] = {4, 16, 18};    // <- Uno TX through voltage divider (5V -> 3.3V)
const int TX_PIN[NR] = {5, 17, 19};    // -> Uno RX direct (3.3V is logic HIGH for Uno)
const int PIN_FAN[NR] = {21, 22, 23};  // Relays IN1..IN3 (12V room fans)
const int PIN_AUX = 25;                // Relay IN4: siren/beacon when ANY room is in ALARM

const bool RELAY_ACTIVE_LOW = true;
const unsigned long UNO_STALE_MS = 3000, PC_STALE_MS = 5000, FAN_HOLD_MS = 30000;

SoftwareSerial softSerial(RX_PIN[2], TX_PIN[2]);
Stream* U[NR];

struct RoomState {
  uint8_t lvl = 0, fault = 1; int gr = 100, t = -1, h = -1, fl = 0;
  unsigned long rx = 0, fanOffAt = 0;
} S[NR];
struct LineBuf { char b[72]; uint8_t n = 0; } LB[NR], PC;
uint8_t cS[4] = {0}, cBuz[NR] = {0}, cFan[NR] = {0}, cReset = 0;
unsigned long lastPc = 0;

inline void relay(int pin, bool on) { digitalWrite(pin, (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW); }

bool feed(LineBuf& L, char c) {                       // true when a full line is ready in L.b
  if (c == '\n') { L.b[L.n] = 0; L.n = 0; return true; }
  if (c != '\r') { if (L.n < sizeof(L.b) - 1) L.b[L.n++] = c; else L.n = 0; }
  return false;
}

void handleUno(int i, char* ln) {                     // "$U,id,lvl,fault,gr,t,h,flame*HH"
  char* star = strrchr(ln, '*');
  if (ln[0] != '$' || !star) return;
  *star = 0;
  uint8_t ck = 0; for (char* p = ln + 1; *p; p++) ck ^= *p;
  if ((uint8_t)strtol(star + 1, NULL, 16) != ck) return;          // corrupted -> ignore
  int id, lvl, f, gr, t, h, fl;
  if (sscanf(ln + 1, "U,%d,%d,%d,%d,%d,%d,%d", &id, &lvl, &f, &gr, &t, &h, &fl) != 7) return;
  if (id != i + 1) return;                                        // wrong wire / wrong NODE_ID
  S[i].lvl = lvl > 2 ? 2 : lvl; S[i].fault = f; S[i].gr = gr; S[i].t = t; S[i].h = h; S[i].fl = fl;
  S[i].rx = millis();
}

void handlePc(char* ln) {                             // "C,s0,s1,s2,s3,b0,b1,b2,f0,f1,f2,reset"
  int v[11];
  if (sscanf(ln, "C,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5],
             &v[6], &v[7], &v[8], &v[9], &v[10]) != 11) return;
  for (int k = 0; k < 4; k++) cS[k] = v[k];
  for (int i = 0; i < NR; i++) { cBuz[i] = v[4 + i]; cFan[i] = v[7 + i]; }
  cReset = v[10]; lastPc = millis();
}

void sendToUno(int i, uint8_t strip, uint8_t buz, uint8_t reset) {
  char buf[24];
  snprintf(buf, sizeof(buf), "K,%d,%d,%d", strip, buz, reset);
  uint8_t ck = 0; for (char* p = buf; *p; p++) ck ^= *p;
  char out[32]; snprintf(out, sizeof(out), "$%s*%02X\n", buf, ck);
  U[i]->print(out);
}

void setup() {
  Serial.begin(115200);                               // USB serial to laptop (UART0)
  Serial1.begin(38400, SERIAL_8N1, RX_PIN[0], TX_PIN[0]); // Room 1 Hardware UART1
  Serial2.begin(38400, SERIAL_8N1, RX_PIN[1], TX_PIN[1]); // Room 2 Hardware UART2
  softSerial.begin(38400, SWSERIAL_8N1, RX_PIN[2], TX_PIN[2]); // Room 3 Software UART

  U[0] = &Serial1;
  U[1] = &Serial2;
  U[2] = &softSerial;

  for (int i = 0; i < NR; i++) {
    digitalWrite(PIN_FAN[i], RELAY_ACTIVE_LOW ? HIGH : LOW);
    pinMode(PIN_FAN[i], OUTPUT);
  }
  digitalWrite(PIN_AUX, RELAY_ACTIVE_LOW ? HIGH : LOW);
  pinMode(PIN_AUX, OUTPUT);
}

void loop() {
  static unsigned long tTx = 0;
  unsigned long now = millis();

  for (int i = 0; i < NR; i++)                        // 1. Unos
    while (U[i]->available()) if (feed(LB[i], U[i]->read())) handleUno(i, LB[i].b);
  while (Serial.available())                          // 2. laptop
    if (feed(PC, Serial.read()) && PC.b[0] == 'C') handlePc(PC.b);

  bool pcOnline = lastPc && (now - lastPc < PC_STALE_MS);
  bool anyAlarm = false;
  bool stale[NR];
  for (int i = 0; i < NR; i++) {
    stale[i] = !S[i].rx || (now - S[i].rx > UNO_STALE_MS);
    anyAlarm |= S[i].lvl == 2;
  }

  // 3. fans + siren: a silent Uno is treated as a problem (fan on), never as "safe"
  for (int i = 0; i < NR; i++) {
    bool lostAfterUp = S[i].rx && stale[i];
    if (S[i].lvl >= 1 || lostAfterUp || (pcOnline && cFan[i])) S[i].fanOffAt = now + FAN_HOLD_MS;
    relay(PIN_FAN[i], now < S[i].fanOffAt);
  }
  relay(PIN_AUX, anyAlarm);

  if (now - tTx >= 500) {
    tTx = now;
    // 4. to laptop (a silent Uno is reported as fault -> laptop treats room as UNKNOWN)
    char line[160]; int n = snprintf(line, sizeof(line), "T");
    for (int i = 0; i < NR; i++)
      n += snprintf(line + n, sizeof(line) - n, ",%d,%d,%d,%d,%d", S[i].lvl,
                    (stale[i] || S[i].fault) ? 1 : 0, S[i].gr, S[i].t, S[i].h);
    Serial.println(line);
    // 5. to Unos: laptop's LED/buzzer commands; if laptop is gone, whole building sounds on any alarm
    for (int i = 0; i < NR; i++)
      sendToUno(i, pcOnline ? cS[i] : 0, pcOnline ? cBuz[i] : (anyAlarm ? 2 : 0), pcOnline ? cReset : 0);
  }
}
