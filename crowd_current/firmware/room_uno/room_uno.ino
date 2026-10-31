/*
  Crowd Current - ROOM NODE (Arduino Uno), one per room.
  Set NODE_ID = 1, 2, or 3 before uploading to each respective room.

  Sensors : MQ-2 (analog A0), DHT11 (digital D3), Flame (digital D2, LOW = flame)
  Outputs : Buzzer (D4), 6 Red LEDs in series via N-channel MOSFET (PWM D6)
  Link    : Hardware UART (pins D0/D1) to the ESP32 Hub @ 38400 baud.
            Uno TX (D1) -> Voltage Divider (1k + 2k) -> ESP32 RX
            ESP32 TX    -> Uno RX (D0) direct
            *** UNPLUG the D0/D1 wires while uploading via USB ***

  Libraries: DHT sensor library (Adafruit) + Adafruit Unified Sensor
             (No NeoPixel library needed - MOSFET driven directly via Pin 6)

  Fusion runs HERE, so the room alarms autonomously even if the hub or laptop disconnects:
    Uno -> hub every 0.5 s : $U,id,lvl,fault,ratio*100,temp,hum,flame*HH
    hub -> Uno every 0.5 s : $K,strip_mode,buzzer_mode,reset*HH   (HH = XOR checksum, hex)
*/
#include <DHT.h>

// Set to 1 for Room 1, 2 for Room 2, 3 for Room 3
#define NODE_ID 1

// ===================== PINS (5 V logic, sensors wired directly) =========
const uint8_t PIN_MQ2    = A0;  // MQ-2 Analog Out (0-5V direct)
const uint8_t PIN_FLAME  = 2;   // Flame Sensor Digital Out (active LOW)
const uint8_t PIN_DHT    = 3;   // DHT11 1-wire data
const uint8_t PIN_BUZ    = 4;   // Active Buzzer (via NPN or direct)
const uint8_t PIN_MOSFET = 6;   // Gate of N-channel MOSFET driving 6 Red LEDs in series

// ===================== THRESHOLDS =======================================
const unsigned long MQ2_WARMUP_MS = 60000;      // demo; real MQ-2 wants 1-2 minutes
const float GAS_WARN = 1.35f, GAS_ALARM = 1.9f, GAS_CLEAR = 1.20f;     // x clean-air baseline
const float T_WARN = 40, T_ALARM = 50, T_RISE_WARN = 3, T_RISE_ALARM = 6;   // C, C per ~20 s

struct Level {                                   // debounced level with hysteresis: 0/1/2
  uint8_t v = 0, up = 0, down = 0;
  void update(uint8_t raw, uint8_t upN, uint8_t downN) {
    if (raw > v)      { if (++up >= upN)     { v = raw; up = 0; } down = 0; }
    else if (raw < v) { if (++down >= downN) { v = raw; down = 0; } up = 0; }
    else              { up = down = 0; }
  }
} gasL, tmpL, flmL;

DHT dht(PIN_DHT, DHT11);

float ema = 0, base = 0, ratio = 1, temp = NAN, hum = NAN, hist[10];
uint8_t histN = 0, level = 0;
bool baseSet = false, latched = false, fault = false;
unsigned long lastDhtOk = 0, mqBad = 0, lastCmdMs = 0;
uint8_t cStrip = 0, cBuz = 0, cReset = 0;

// ===================== SENSING ==========================================
void readDht() {
  float t = dht.readTemperature(), h = dht.readHumidity();
  if (isnan(t)) return;
  temp = t; hum = h; lastDhtOk = millis();
  if (histN < 10) hist[histN++] = t;
  else { for (uint8_t k = 0; k < 9; k++) hist[k] = hist[k + 1]; hist[9] = t; }
  float rise = t - hist[0];
  uint8_t raw = (t >= T_ALARM || rise >= T_RISE_ALARM) ? 2
              : (t >= T_WARN || rise >= T_RISE_WARN || (tmpL.v >= 1 && t >= T_WARN - 2)) ? 1 : 0;
  tmpL.update(raw, 1, 3);
}

void updateSensors(bool doReset) {
  uint16_t s = 0; for (uint8_t k = 0; k < 8; k++) s += analogRead(PIN_MQ2);
  float v = s / 8.0f;                                            // 0..1023
  ema = (ema == 0) ? v : 0.8f * ema + 0.2f * v;
  if (!baseSet && millis() > MQ2_WARMUP_MS) { base = ema > 1 ? ema : 1; baseSet = true; }
  if (baseSet) {
    ratio = ema / base;
    if (ratio < 1.1f) base = 0.999f * base + 0.001f * ema;       // slow clean-air drift tracking
    uint8_t raw = ratio > GAS_ALARM ? 2
                : (ratio > GAS_WARN || (gasL.v >= 1 && ratio > GAS_CLEAR)) ? 1 : 0;
    gasL.update(raw, raw == 2 ? 10 : 5, 30);                     // 1 s / 0.5 s up, 3 s down
  }
  bool bad = (v < 15 || v > 1015);                               // unplugged / saturated
  if (bad) { if (!mqBad) mqBad = millis(); } else mqBad = 0;
  flmL.update(digitalRead(PIN_FLAME) == LOW ? 2 : 0, 3, 20);     // 0.3 s to trigger, 2 s to clear
  fault = (mqBad && millis() - mqBad > 5000) ||                  // a dead sensor is reported, never "safe"
          (millis() > 15000 && millis() - lastDhtOk > 10000);
  uint8_t warns = (gasL.v >= 1) + (tmpL.v >= 1) + (flmL.v >= 1);
  uint8_t fused = 0;
  if (gasL.v == 2 || tmpL.v == 2 || flmL.v == 2 || warns >= 2) fused = 2;
  else if (warns == 1) fused = 1;
  if (fused == 2) latched = true;                                // alarm latches ...
  if (doReset && fused == 0) latched = false;                    // ... until reset AND air is clear
  level = latched ? 2 : fused;
}

// ===================== OUTPUTS ==========================================
void updateBuzzer(uint8_t laptopMode) {
  uint8_t m = (level > laptopMode) ? level : laptopMode;         // 0 off, 1 slow beep, 2 fast
  unsigned long t = millis();
  digitalWrite(PIN_BUZ, (m == 1) ? (t % 1000) < 150 : (m == 2) ? (t % 300) < 150 : false);
}

/*
  LED Modes for 6 Red LEDs in series via MOSFET (Pin 6):
    0: IDLE / Normal        -> OFF
    1 or 2: EVACUATION PATH -> Solid ON (bright illumination for safe exit route)
    3: BLOCKED / HAZARD     -> Fast Emergency Strobe (~4 Hz flash, DANGER DO NOT ENTER)
    4 or 5: ADVISORY ROUTE  -> Medium Warning Blink (1 Hz caution pulse)
*/
void renderLeds(uint8_t mode) {
  unsigned long t = millis();
  if (mode == 0) {
    // Normal / Idle: Off
    digitalWrite(PIN_MOSFET, LOW);
  } else if (mode == 3) {
    // BLOCKED / HAZARD: rapid emergency strobe (120ms ON / 120ms OFF)
    digitalWrite(PIN_MOSFET, ((t / 120) % 2) ? HIGH : LOW);
  } else if (mode == 1 || mode == 2) {
    // ACTIVE EVACUATION ROUTE: solid ON to illuminate exit corridor
    digitalWrite(PIN_MOSFET, HIGH);
  } else if (mode == 4 || mode == 5) {
    // ADVISORY / CAUTION: 1 Hz blink (500ms ON / 500ms OFF)
    digitalWrite(PIN_MOSFET, ((t / 500) % 2) ? HIGH : LOW);
  } else {
    digitalWrite(PIN_MOSFET, LOW);
  }
}

// ===================== UART LINK ========================================
char rx[40]; uint8_t rxN = 0;
void parseLine() {                                 // "$K,strip,buz,reset*HH"
  rx[rxN] = 0;
  char* star = strrchr(rx, '*');
  if (rx[0] != '$' || !star) return;
  *star = 0;
  uint8_t ck = 0; for (char* p = rx + 1; *p; p++) ck ^= *p;
  if ((uint8_t)strtol(star + 1, NULL, 16) != ck) return;          // corrupted -> ignore
  if (rx[1] != 'K') return;
  int a, b, c;
  if (sscanf(rx + 2, ",%d,%d,%d", &a, &b, &c) != 3) return;
  cStrip = a; cBuz = b; cReset = c; lastCmdMs = millis();
}

void readLink() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { parseLine(); rxN = 0; }
    else if (c != '\r') { if (rxN < sizeof(rx) - 1) rx[rxN++] = c; else rxN = 0; }
  }
}

void sendTelemetry() {
  char buf[40];
  snprintf(buf, sizeof(buf), "U,%d,%d,%d,%d,%d,%d,%d", NODE_ID, level, fault, (int)(ratio * 100),
           isnan(temp) ? -1 : (int)temp, isnan(hum) ? -1 : (int)hum, digitalRead(PIN_FLAME) == LOW);
  uint8_t ck = 0; for (char* p = buf; *p; p++) ck ^= *p;
  Serial.print('$'); Serial.print(buf); Serial.print('*');
  if (ck < 16) Serial.print('0');
  Serial.println(ck, HEX);
}

// ===================== SETUP / LOOP =====================================
void setup() {
  Serial.begin(38400);
  pinMode(PIN_FLAME, INPUT);
  pinMode(PIN_BUZ, OUTPUT);
  digitalWrite(PIN_BUZ, LOW);
  pinMode(PIN_MOSFET, OUTPUT);
  digitalWrite(PIN_MOSFET, LOW);
  dht.begin();

  // Self-test at boot: pulse LEDs and beep buzzer once for 200 ms
  digitalWrite(PIN_MOSFET, HIGH);
  digitalWrite(PIN_BUZ, HIGH);
  delay(200);
  digitalWrite(PIN_MOSFET, LOW);
  digitalWrite(PIN_BUZ, LOW);
}

void loop() {
  static unsigned long tSense = 0, tDht = 0, tLed = 0, tTx = 0;
  unsigned long now = millis();
  readLink();
  bool online = lastCmdMs && (now - lastCmdMs < 5000);

  if (now - tDht >= 2100) { tDht = now; readDht(); }
  if (now - tSense >= 100) { tSense = now; updateSensors(online && cReset); }
  updateBuzzer(online ? cBuz : 0);

  if (now - tLed >= 40) {
    tLed = now;
    uint8_t m;
    if (online) {
      m = cStrip;
    } else if (level == 2) {
      m = 3;  // local ALARM fallback: rapid strobe
    } else if (level == 1) {
      m = 4;  // local WARNING fallback: caution blink
    } else {
      m = 0;  // normal idle: OFF
    }
    renderLeds(m);
  }

  if (now - tTx >= 500) { tTx = now; sendTelemetry(); }
}
