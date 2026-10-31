/*
  Crowd Current - HUB (ESP32-S3).  Talks to 3 room Unos over UART and to the laptop over USB.
  Arduino IDE:  Board = ESP32S3 Dev Module,  Tools -> "USB CDC On Boot" = ENABLED
                (so `Serial` = the USB cable to the laptop and all 3 hardware UARTs are free).
  Hub job : collect the 3 rooms, forward to laptop, pass laptop commands back to each Uno,
            run the 3 fan relays + building siren, keep the building safe if the laptop dies.
  Laptop protocol (same as before, run:  python main.py --serial COMx):
      hub -> laptop : T,lvl,fault,ratio*100,temp,hum  (x3 rooms)        every 0.5 s
      laptop -> hub : C,s0,s1,s2,s3,buz0,buz1,buz2,fan0,fan1,fan2,reset
  Uno link: $U,...*HH from Unos,  $K,strip,buz,reset*HH to Unos   (XOR checksum), 38400 baud
*/
#include <Arduino.h>

const int NR = 3;
HardwareSerial* U[NR] = {&Serial1, &Serial2, &Serial0};
const int RX_PIN[NR] = {4, 6, 15};     // <- Uno TX through 1k + 2k divider (5 V -> 3.3 V!)
const int TX_PIN[NR] = {5, 7, 16};     // -> Uno RX (3.3 V is enough for an Uno)
const int PIN_FAN[NR] = {10, 11, 12};  // 4-ch relay IN1..IN3 (12 V fans)
const int PIN_AUX = 13;                // IN4: siren/beacon when ANY room is in ALARM
const bool RELAY_ACTIVE_LOW = true;
const unsigned long UNO_STALE_MS = 3000, PC_STALE_MS = 5000, FAN_HOLD_MS = 30000;

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
  Serial.begin(115200);                               // USB CDC to laptop
  for (int i = 0; i < NR; i++) {
    U[i]->begin(38400, SERIAL_8N1, RX_PIN[i], TX_PIN[i]);
    digitalWrite(PIN_FAN[i], RELAY_ACTIVE_LOW ? HIGH : LOW);      // level before output: no boot glitch
    pinMode(PIN_FAN[i], OUTPUT);
  }
  digitalWrite(PIN_AUX, RELAY_ACTIVE_LOW ? HIGH : LOW); pinMode(PIN_AUX, OUTPUT);
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
