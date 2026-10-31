# Crowd Current – Dynamic Crowd Evacuation & Hazard Control System

```
 Room 1 Uno ──┐
 Room 2 Uno ──┼── Hardware/Software UART ──► ESP32 DevKit HUB ── USB (115200) ──► Laptop Controller (Python)
 Room 3 Uno ──┘   (38400 baud)                 Relays 1-4 (Fans + Siren)             Vision counting + Dijkstra HUD
(Sensors, Buzzer,
 6 Red LEDs / MOSFET)
```

## System Architecture

1. **Room Nodes (×3 Arduino Uno)**:
   - One Uno per room (Room 1, Room 2, Room 3).
   - Reads 5V sensors: **MQ-2 Smoke/Gas (A0)**, **Flame Sensor (D2)**, **DHT11 Temperature & Humidity (D3)**.
   - Actuators: **Active Buzzer (D4)** and **6 Red LEDs in series driven via N-channel MOSFET (PWM D6)**.
   - Autonomous multi-sensor fusion: evaluates hazard state locally (`NORMAL`, `WARNING`, `ALARM`) so the room alarms autonomously even if the hub or laptop disconnects.
   - Communicates with ESP32 DevKit Hub over UART (`D0`/`D1`) @ 38400 baud.

2. **Hub (ESP32 DevKit)**:
   - Connects to the 3 room Unos (UART1 on GPIO 4/5, UART2 on GPIO 16/17, SoftwareSerial on GPIO 18/19).
   - 4-channel relay module:
     - Relays 1, 2, 3: Exhaust fans for Rooms 1, 2, 3 (triggered on gas/heat/smoke warning or alarm).
     - Relay 4: Building-wide emergency siren/beacon (activates if any room is in `ALARM`).
   - Forwards consolidated telemetry to laptop over USB Serial (`Serial`) at 115200 baud.
   - Relays laptop routing commands back to the room Unos. If laptop disconnects, hub maintains emergency fallback.

3. **Laptop Controller (`crowd_current/controller/main.py`)**:
   - DroidCam / Webcam overhead computer vision for people-token counting.
   - Hazard-aware, congestion-penalized Dijkstra dynamic routing.
   - Real-time OpenCV HUD and event logging to `events.csv`.

---

## Folder Structure

```
crowd_current_code/
├── README.md
└── crowd_current/
    ├── README.md
    ├── controller/
    │   ├── calibrate.py       # Camera polygon & token calibration
    │   ├── config.py          # Map dimensions, camera index, thresholds
    │   ├── main.py            # Main runner (OpenCV HUD + Dijkstra + Hub Serial)
    │   ├── requirements.txt   # Dependencies: opencv-python, numpy, flask, pyserial
    │   ├── routing.py         # Dijkstra congestion-aware router
    │   └── vision.py          # Overhead token counter & background subtraction
    └── firmware/
        ├── hub_esp32/
        │   └── hub_esp32.ino  # ESP32-S3 Hub: 3 UARTs to Unos, 4 Relays, USB to Laptop
        └── room_uno/
            └── room_uno.ino   # Arduino Uno Room Node: MQ2, Flame, DHT11, Buzzer, 6 Red LEDs + MOSFET
```

---

## Wiring Guide

### 1. Room Uno Wiring (Each Room - 5V Logic)

| Sensor / Actuator | Arduino Uno Pin | Notes |
|---|---|---|
| **MQ-2 Gas / Smoke AO** | **A0** | Analog output (0-5V direct) |
| **Flame Sensor DO** | **D2** | Digital output (Active LOW on flame) |
| **DHT11 DATA** | **D3** | Bidirectional 1-wire data |
| **Active Buzzer** | **D4** | Active HIGH (via NPN transistor or direct) |
| **MOSFET Gate (6 Red LEDs)** | **D6** | **N-channel MOSFET** (e.g. IRF520 / IRLZ44N / 2N7000):<br>• Uno D6 → 100–220 Ω gate resistor → MOSFET Gate<br>• 10k Ω pulldown resistor from Gate to GND<br>• MOSFET Source → GND<br>• MOSFET Drain → Cathode of 6 series LEDs<br>• Anode of 6 series LEDs → +12V (via current-limiting resistor) |
| **Link to Hub: Uno TX** | **D1** | **Must use voltage divider to ESP32 RX**:<br>`Uno D1` → 1kΩ resistor → `ESP32 RX`<br>Connect 2kΩ resistor from `ESP32 RX` to `GND`. |
| **Link to Hub: Uno RX** | **D0** | Directly connected to ESP32 TX (3.3V logic is HIGH for Uno) |
| **GND** | **GND** | Common ground connected across all Unos, ESP32, MOSFET sources, and supplies |

> [!IMPORTANT]
> **Disconnect pins D0 & D1 when uploading firmware via USB to the Arduino Uno** (as D0/D1 are shared with the Uno's USB-to-UART chip). Reconnect after uploading.

### 2. LED Behavior Modes (6 Red LEDs via MOSFET)
- **Mode 0 (Normal / Idle)**: **OFF** (standby state).
- **Mode 1 / 2 (Active Evacuation Route)**: **Solid Bright ON** (illuminates the safe evacuation path out of the room).
- **Mode 3 (Blocked / Fire Hazard)**: **Rapid Emergency Strobe (~4 Hz)** (fast strobe alerting occupants of immediate hazard / do not enter).
- **Mode 4 / 5 (Advisory / Caution)**: **Medium Warning Blink (1 Hz)** (slow caution flash for detours).

### 3. ESP32 DevKit Hub Wiring (Classic ESP32 / WROOM-32 / DevKit V1)

| Room / Component | ESP32 RX Pin (from Uno TX via divider) | ESP32 TX Pin (to Uno RX direct) | Relay Pin (Active-LOW) |
|---|---|---|---|
| **Room 1** (Hardware Serial1) | **GPIO 4** | **GPIO 5** | Relay IN1 → **GPIO 21** (Fan 1) |
| **Room 2** (Hardware Serial2) | **GPIO 16** | **GPIO 17** | Relay IN2 → **GPIO 22** (Fan 2) |
| **Room 3** (Software Serial) | **GPIO 18** | **GPIO 19** | Relay IN3 → **GPIO 23** (Fan 3) |
| **Building Aux Siren** | — | — | Relay IN4 → **GPIO 25** (Siren/Strobe) |

> [!CAUTION]
> **Never connect anything to GPIO 6, 7, 8, 9, 10, or 11 on an ESP32 DevKit.** These pins are directly wired to the internal SPI Flash memory chip. Connecting to them causes instant crashes or prevents boot.

* **Arduino IDE Settings for ESP32 DevKit**:
  - Board: **ESP32 Dev Module** (or **DOIT ESP32 DEVKIT V1**)
  - Upload Speed: **921600** or **115200**
  - Connect laptop to the ESP32 via Micro-USB / Type-C cable.

---

## Software Setup & Run

### 1. Flash Arduino Unos (`firmware/room_uno/room_uno.ino`)
1. Open `room_uno.ino` in Arduino IDE.
2. For Room 1: ensure line 22 has `#define NODE_ID 1`. Upload to Uno 1.
3. For Room 2: change line 22 to `#define NODE_ID 2`. Upload to Uno 2.
4. For Room 3: change line 22 to `#define NODE_ID 3`. Upload to Uno 3.

### 2. Flash ESP32-S3 Hub (`firmware/hub_esp32/hub_esp32.ino`)
1. Open `hub_esp32.ino` in Arduino IDE.
2. Select **ESP32S3 Dev Module** and enable **USB CDC On Boot**.
3. Upload sketch and connect the ESP32 to the laptop via USB.

### 3. Controller (Laptop)
```bash
cd crowd_current/controller
pip install -r requirements.txt

# 1. Camera & Room Calibration (one-time setup)
python calibrate.py

# 2. Run with Hardware Hub connected on COM port (e.g. COM5)
python main.py --serial COM5

# 3. Or run in simulation mode without hardware
python main.py --sim
```

### Keyboard Shortcuts in HUD Window
- `1` / `2` / `3`: Toggle simulated **ALARM** in Room 1 / 2 / 3
- `4` / `5` / `6`: Toggle simulated **WARNING** in Room 1 / 2 / 3
- `r`: Reset latched alarms
- `q`: Quit controller
