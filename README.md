# Crowd Current – Dynamic Crowd Evacuation & Hazard Control System

```
 Room 1 Uno ──┐
 Room 2 Uno ──┼── Hardware UART ──► ESP32-S3 HUB ── USB CDC (115200) ──► Laptop Controller (Python)
 Room 3 Uno ──┘   (38400 baud)      Relays 1-4 (Fans + Siren)             Vision counting + Dijkstra HUD
(Sensors, Buzzer,
 WS2812B Strip)
```

## System Architecture

1. **Room Nodes (×3 Arduino Uno)**:
   - One Uno per room (Room 1, Room 2, Room 3).
   - Reads 5V sensors: **MQ-2 Smoke/Gas (A0)**, **Flame Sensor (D2)**, **DHT11 Temperature & Humidity (D3)**.
   - Actuators: **Active Buzzer (D4)** and **WS2812B NeoPixel Strip (D6)**.
   - Autonomous multi-sensor fusion: evaluates hazard state locally (`NORMAL`, `WARNING`, `ALARM`) so the room alarms autonomously even if the hub or laptop disconnects.
   - Communicates with ESP32-S3 Hub over hardware UART (`D0`/`D1`) @ 38400 baud.

2. **Hub (ESP32-S3)**:
   - 3 dedicated hardware UART lines connected to the 3 room Unos.
   - 4-channel relay module:
     - Relays 1, 2, 3: Exhaust fans for Rooms 1, 2, 3 (triggered on gas/heat/smoke warning or alarm).
     - Relay 4: Building-wide emergency siren/beacon (activates if any room is in `ALARM`).
   - Forwards consolidated telemetry to laptop over USB CDC Serial (`Serial`) at 115200 baud.
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
            └── room_uno.ino   # Arduino Uno Room Node: MQ2, Flame, DHT11, Buzzer, WS2812B
```

---

## Wiring Guide

### 1. Room Uno Wiring (Each Room - 5V Logic, No Dividers on Sensors)

| Sensor / Actuator | Arduino Uno Pin | Notes |
|---|---|---|
| **MQ-2 Gas / Smoke AO** | **A0** | Analog output (0-5V direct) |
| **Flame Sensor DO** | **D2** | Digital output (Active LOW on flame) |
| **DHT11 DATA** | **D3** | Bidirectional 1-wire data |
| **Active Buzzer** | **D4** | Active HIGH (via NPN transistor or direct) |
| **WS2812B Strip DIN** | **D6** | In series with 330 Ω resistor |
| **Link to Hub: Uno TX** | **D1** | **Must use voltage divider to ESP32 RX**:<br>`Uno D1` → 1kΩ resistor → `ESP32 RX`<br>Connect 2kΩ resistor from `ESP32 RX` to `GND`. |
| **Link to Hub: Uno RX** | **D0** | Directly connected to ESP32 TX (3.3V logic is HIGH for Uno) |
| **GND** | **GND** | Common ground connected across all Unos, ESP32, and power supplies |

> [!IMPORTANT]
> **Disconnect pins D0 & D1 when uploading firmware via USB to the Arduino Uno** (as D0/D1 are shared with the Uno's USB-to-UART chip). Reconnect after uploading.

### 2. ESP32-S3 Hub Wiring

| Room / Component | ESP32 RX Pin (from Uno TX via 1k/2k divider) | ESP32 TX Pin (to Uno RX direct) | Relay Pin (Active LOW) |
|---|---|---|---|
| **Room 1** | **GPIO 4** | **GPIO 5** | Relay IN1 → **GPIO 10** (Fan 1) |
| **Room 2** | **GPIO 6** | **GPIO 7** | Relay IN2 → **GPIO 11** (Fan 2) |
| **Room 3** | **GPIO 15** | **GPIO 16** | Relay IN3 → **GPIO 12** (Fan 3) |
| **Building Aux Siren** | — | — | Relay IN4 → **GPIO 13** (Siren/Strobe) |

* **Arduino IDE Settings for ESP32-S3**:
  - Board: **ESP32S3 Dev Module**
  - **USB CDC On Boot**: **Enabled** (Required so `Serial` is the USB port to the laptop, leaving UART0, UART1, and UART2 free for the 3 Unos).

---

## Software Setup & Run

### 1. Flash Arduino Unos (`firmware/room_uno/room_uno.ino`)
1. Open `room_uno.ino` in Arduino IDE.
2. For Room 1: ensure line 16 has `#define NODE_ID 1`. Upload to Uno 1.
3. For Room 2: change line 16 to `#define NODE_ID 2`. Upload to Uno 2.
4. For Room 3: change line 16 to `#define NODE_ID 3`. Upload to Uno 3.

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
