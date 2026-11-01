"""Crowd Current controller with real-time OpenCV HUD and Web Dashboard.

Run:
  python main.py [--serial COMx] [--sim] [--no-camera] [--source 1]

Web Dashboard available at:
  http://localhost:5000  (or http://<laptop-ip>:5000 on your local Wi-Fi / phone)

Keys in the OpenCV window:
  1 / 2 / 3 = toggle simulated ALARM in room 1 / 2 / 3
  4 / 5 / 6 = toggle simulated WARNING in room 1 / 2 / 3
  r         = reset latched alarms & revert all manual overrides to AUTO
  q         = quit
"""
import argparse, csv, logging, threading, time
import cv2, numpy as np
from flask import Flask, jsonify, request, Response
import config as C
from routing import Planner, IDLE

ap = argparse.ArgumentParser(description="Crowd Current Controller")
ap.add_argument("--sim", action="store_true", help="no hardware connected: absent nodes count as NORMAL")
ap.add_argument("--no-camera", action="store_true", help="disable camera and use fake/zero counts")
ap.add_argument("--fake", default="0,0,0", help="fake counts per room (e.g. '2,4,1') when --no-camera")
ap.add_argument("--source", default=C.CAMERA_SOURCE, help="camera index or video URL (default: config.CAMERA_SOURCE)")
ap.add_argument("--serial", default=None, help="ESP32 Hub COM port (e.g. COM5 or /dev/ttyUSB0)")
args = ap.parse_args()

RANK = {"NORMAL": 0, "UNKNOWN": 1, "WARNING": 1, "ALARM": 2}
LV = ["NORMAL", "WARNING", "ALARM"]
COL = {"NORMAL": (0, 200, 0), "WARNING": (0, 200, 255), "UNKNOWN": (180, 180, 180), "ALARM": (0, 0, 255)}

lock = threading.Lock()
frame_lock = threading.Lock()
latest_jpeg = None

telemetry = None
commands = {
    "s": [0] * len(C.STRIP_SLOTS),
    "buz": [0] * len(C.ROOMS),
    "fan": [0] * len(C.ROOMS),
}
reset_until = 0.0
sim = {r: None for r in C.ROOMS}

# Manual override states: None = AUTO, integer = Forced Value
manual_fan = [None] * len(C.ROOMS)     # None=Auto, 1=Force ON, 0=Force OFF
manual_buz = [None] * len(C.ROOMS)     # None=Auto, 2=Force Siren/Alarm, 0=Force OFF
manual_s = [None] * len(C.STRIP_SLOTS) # None=Auto, 1=Solid ON, 3=Strobe Hazard, 0=OFF

system_state = {
    "mode": "NORMAL",
    "levels": {r: "NORMAL" for r in C.ROOMS},
    "counts": {r: 0 for r in C.ROOMS},
    "assign": {},
    "alerts": [],
    "fans": [0] * len(C.ROOMS),
    "buz": [0] * len(C.ROOMS),
    "strips": [0] * len(C.STRIP_SLOTS),
    "manual": {
        "fans": manual_fan,
        "buz": manual_buz,
        "strips": manual_s,
    },
    "telemetry": None,
    "timestamp": time.time(),
}

# ---------------- Web Dashboard & API (Flask) ----------------
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Crowd Current — Mission Control & Manual Override</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: #131c2e;
      --border: #1e2d4a;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --green: #10b981;
      --amber: #f59e0b;
      --red: #ef4444;
      --blue: #3b82f6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background-color: var(--bg); color: var(--text); padding: 1.5rem; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
    .logo-area { display: flex; align-items: center; gap: 0.75rem; }
    .logo-icon { width: 36px; height: 36px; background: linear-gradient(135deg, #2563eb, #10b981); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 20px; color: white; }
    .title { font-size: 1.4rem; font-weight: 700; letter-spacing: 0.5px; }
    .subtitle { font-size: 0.8rem; color: var(--text-muted); }
    .status-badge { font-size: 0.9rem; font-weight: 700; padding: 0.4rem 1rem; border-radius: 9999px; text-transform: uppercase; letter-spacing: 1px; display: inline-flex; align-items: center; gap: 0.5rem; }
    .badge-normal { background: rgba(16, 185, 129, 0.15); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-advisory { background: rgba(245, 158, 11, 0.15); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-evacuate { background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.5); animation: pulse 1s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.85; transform: scale(1.02); } }
    
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
    
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
    .card-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; }
    
    .video-container { position: relative; width: 100%; border-radius: 8px; overflow: hidden; background: #000; min-height: 280px; }
    .video-feed { width: 100%; height: auto; display: block; }
    
    .room-card { margin-bottom: 1.25rem; border: 1px solid var(--border); border-radius: 10px; padding: 1rem; background: rgba(255,255,255,0.02); transition: all 0.2s; }
    .room-card:last-child { margin-bottom: 0; }
    .room-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }
    .room-name { font-size: 1.15rem; font-weight: 700; }
    .room-meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; margin-bottom: 0.75rem; text-align: center; }
    .meta-box { background: rgba(0,0,0,0.25); padding: 0.5rem; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); }
    .meta-label { font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase; margin-bottom: 0.25rem; }
    .meta-val { font-size: 0.95rem; font-weight: 700; }
    .route-box { background: rgba(59, 130, 246, 0.1); border: 1px solid rgba(59, 130, 246, 0.3); padding: 0.6rem 0.8rem; border-radius: 6px; font-size: 0.85rem; color: #93c5fd; margin-bottom: 0.75rem; font-weight: 500; }
    
    .actuators { display: flex; flex-direction: column; gap: 0.6rem; padding: 0.75rem; border-radius: 8px; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.05); }
    .act-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; }
    .act-tag { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.2rem 0.5rem; border-radius: 4px; background: rgba(255,255,255,0.05); font-size: 0.8rem; }
    .act-active { background: rgba(16, 185, 129, 0.2); color: var(--green); font-weight: 600; }
    .act-hazard { background: rgba(239, 68, 68, 0.2); color: var(--red); font-weight: 600; }

    /* Override buttons */
    .ctrl-group { display: inline-flex; border-radius: 6px; overflow: hidden; border: 1px solid #334155; }
    .ctrl-btn { background: #1e293b; color: #94a3b8; border: none; padding: 0.25rem 0.6rem; font-size: 0.75rem; font-weight: 600; cursor: pointer; transition: all 0.15s; border-right: 1px solid #334155; }
    .ctrl-btn:last-child { border-right: none; }
    .ctrl-btn:hover { background: #334155; color: white; }
    .ctrl-active { background: #2563eb !important; color: white !important; }
    .ctrl-on { background: #059669 !important; color: white !important; }
    .ctrl-off { background: #dc2626 !important; color: white !important; }
    .ctrl-strobe { background: #d97706 !important; color: white !important; }

    .btn-group { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
    button.action-btn { background: #1e293b; color: white; border: 1px solid #334155; padding: 0.35rem 0.65rem; border-radius: 6px; font-size: 0.75rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }
    button.action-btn:hover { background: #334155; }
    .btn-alarm { background: rgba(239, 68, 68, 0.2); border-color: rgba(239, 68, 68, 0.4); color: #fca5a5; }
    .btn-alarm:hover { background: rgba(239, 68, 68, 0.4); }
    .btn-warn { background: rgba(245, 158, 11, 0.2); border-color: rgba(245, 158, 11, 0.4); color: #fde68a; }
    .btn-warn:hover { background: rgba(245, 158, 11, 0.4); }
    .btn-reset { background: linear-gradient(135deg, #2563eb, #1d4ed8); border: none; padding: 0.5rem 1rem; font-size: 0.85rem; font-weight: 600; border-radius: 6px; color: white; cursor: pointer; }
    .btn-reset:hover { opacity: 0.9; }
    .btn-all-auto { background: #334155; border: none; padding: 0.5rem 1rem; font-size: 0.85rem; font-weight: 600; border-radius: 6px; color: white; cursor: pointer; }
    .btn-all-auto:hover { background: #475569; }

    .alerts-card { margin-top: 1.5rem; }
    .alert-item { padding: 0.5rem 0.75rem; border-radius: 6px; background: rgba(239, 68, 68, 0.15); border-left: 3px solid var(--red); color: #fca5a5; margin-bottom: 0.5rem; font-size: 0.85rem; font-weight: 500; }
  </style>
</head>
<body>
  <div class="header">
    <div class="logo-area">
      <div class="logo-icon">⚡</div>
      <div>
        <div class="title">CROWD CURRENT</div>
        <div class="subtitle">Multi-Node Dynamic Evacuation & Hardware Control Center</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 0.75rem;">
      <div id="mode-badge" class="status-badge badge-normal">NORMAL</div>
      <button class="btn-all-auto" onclick="setManual('all_auto')">⚡ ALL AUTO</button>
      <button class="btn-reset" onclick="sendAction('RESET')">↺ RESET ALARMS</button>
    </div>
  </div>

  <div class="grid">
    <!-- Camera Section -->
    <div class="card">
      <div class="card-title">
        <span>Overhead Camera Stream (CV HUD)</span>
        <span id="cam-status" style="font-size: 0.8rem; color: var(--green);">● LIVE FEED</span>
      </div>
      <div class="video-container">
        <img class="video-feed" src="/video_feed" alt="Camera Stream" onerror="this.style.display='none'; document.getElementById('vid-fallback').style.display='block';">
        <div id="vid-fallback" style="display:none; padding: 3rem; text-align: center; color: var(--text-muted);">
          Camera Feed Offline or in Simulation Mode
        </div>
      </div>
      <div style="margin-top: 1rem; font-size: 0.8rem; color: var(--text-muted); display: flex; justify-content: space-between;">
        <span>Routing: Congestion-Aware Dijkstra</span>
        <span>Actuator Override: Hardware Serial (115200)</span>
      </div>
    </div>

    <!-- Rooms Section with Live Telemetry + Manual Control -->
    <div class="card">
      <div class="card-title">
        <span>Room Controls & Real-Time Sensors</span>
        <span id="aux-siren" class="act-tag">SIREN: SILENT</span>
      </div>
      <div id="rooms-container">
        <!-- Rendered by JS -->
      </div>
    </div>
  </div>

  <div class="card alerts-card" id="alerts-container" style="display: none;">
    <div class="card-title" style="color: var(--red);">System Alerts & Warnings</div>
    <div id="alerts-list"></div>
  </div>

  <script>
    async function fetchState() {
      try {
        const res = await fetch('/api/state');
        const data = await res.json();
        updateUI(data);
      } catch (e) {
        console.error("Failed to fetch state:", e);
      }
    }

    function updateUI(data) {
      // 1. Mode Badge
      const mb = document.getElementById('mode-badge');
      mb.innerText = data.mode;
      mb.className = 'status-badge ' + (
        data.mode === 'EVACUATE' ? 'badge-evacuate' :
        data.mode === 'ADVISORY' ? 'badge-advisory' : 'badge-normal'
      );

      // 2. Aux Siren
      const anyAlarm = Object.values(data.levels).includes('ALARM') || (data.buz && data.buz.some(b => b > 0));
      const sirenTag = document.getElementById('aux-siren');
      sirenTag.innerText = anyAlarm ? '🚨 SIREN: SOUNDING' : 'SIREN: SILENT';
      sirenTag.className = 'act-tag ' + (anyAlarm ? 'act-hazard' : '');

      // 3. Rooms
      const rc = document.getElementById('rooms-container');
      const rooms = ['R1', 'R2', 'R3'];
      let html = '';

      const man = data.manual || { fans: [null, null, null], strips: [null, null, null] };

      rooms.forEach((r, idx) => {
        const lvl = data.levels[r] || 'UNKNOWN';
        const count = data.counts[r] || 0;
        const assign = data.assign[r];
        const telem = (data.telemetry && data.telemetry.rooms && data.telemetry.rooms[idx]) ? data.telemetry.rooms[idx] : null;

        const gas = telem ? telem.gr.toFixed(2) + 'x' : '--';
        const temp = telem && telem.t >= 0 ? telem.t + ' °C' : '--';
        const hum = telem && telem.h >= 0 ? telem.h + ' %' : '--';
        const flame = telem ? (telem.fl ? '🔥 FLAME' : 'CLEAR') : '--';

        const fanRunning = Boolean(data.fans && data.fans[idx]);
        const stripVal = data.strips ? data.strips[idx] : 0;
        let ledStatusText = 'OFF';
        if (stripVal === 1 || stripVal === 2) ledStatusText = 'ROUTE ILLUMINATION';
        else if (stripVal === 3) ledStatusText = 'DANGER STROBE';
        else if (stripVal === 4 || stripVal === 5) ledStatusText = 'CAUTION BLINK';

        const routeText = assign ? `🚪 Evacuate via <b>${assign.exit}</b> &nbsp;(Path: ${assign.path.join(' → ')})` : 'No active evacuation guidance';

        let badgeClass = 'badge-normal';
        if (lvl === 'ALARM') badgeClass = 'badge-evacuate';
        else if (lvl === 'WARNING') badgeClass = 'badge-advisory';
        else if (lvl === 'UNKNOWN') badgeClass = '';

        const mFan = man.fans[idx];
        const mLed = man.strips[idx];

        html += `
          <div class="room-card" style="border-left: 4px solid ${lvl==='ALARM'?'#ef4444':lvl==='WARNING'?'#f59e0b':'#10b981'};">
            <div class="room-header">
              <span class="room-name">${r}</span>
              <span class="status-badge ${badgeClass}" style="font-size: 0.75rem; padding: 0.2rem 0.6rem;">${lvl}</span>
            </div>
            <div class="route-box">${routeText}</div>
            <div class="room-meta">
              <div class="meta-box"><div class="meta-label">People</div><div class="meta-val" style="color: #60a5fa;">n = ${count}</div></div>
              <div class="meta-box"><div class="meta-label">Gas Ratio</div><div class="meta-val">${gas}</div></div>
              <div class="meta-box"><div class="meta-label">Temp</div><div class="meta-val">${temp}</div></div>
              <div class="meta-box"><div class="meta-label">Flame</div><div class="meta-val" style="${telem && telem.fl ? 'color:#ef4444;font-weight:800;' : ''}">${flame}</div></div>
            </div>

            <!-- Manual Overrides for Actuators -->
            <div class="actuators">
              <div class="act-row">
                <span>🌀 <b>Exhaust Fan</b>: <span class="${fanRunning ? 'act-active' : ''}">${fanRunning ? 'RUNNING' : 'OFF'}</span></span>
                <div class="ctrl-group">
                  <button class="ctrl-btn ${mFan===null ? 'ctrl-active' : ''}" onclick="setManual('fan', ${idx}, null)">AUTO</button>
                  <button class="ctrl-btn ${mFan===1 ? 'ctrl-on' : ''}" onclick="setManual('fan', ${idx}, 1)">FORCE ON</button>
                  <button class="ctrl-btn ${mFan===0 ? 'ctrl-off' : ''}" onclick="setManual('fan', ${idx}, 0)">OFF</button>
                </div>
              </div>

              <div class="act-row">
                <span>💡 <b>6 Red LEDs</b>: <span class="${stripVal===3 ? 'act-hazard' : (stripVal ? 'act-active' : '')}">${ledStatusText}</span></span>
                <div class="ctrl-group">
                  <button class="ctrl-btn ${mLed===null ? 'ctrl-active' : ''}" onclick="setManual('led', ${idx}, null)">AUTO</button>
                  <button class="ctrl-btn ${mLed===1 ? 'ctrl-on' : ''}" onclick="setManual('led', ${idx}, 1)">ON</button>
                  <button class="ctrl-btn ${mLed===3 ? 'ctrl-strobe' : ''}" onclick="setManual('led', ${idx}, 3)">STROBE</button>
                  <button class="ctrl-btn ${mLed===0 ? 'ctrl-off' : ''}" onclick="setManual('led', ${idx}, 0)">OFF</button>
                </div>
              </div>
            </div>

            <!-- Simulation Triggers -->
            <div class="btn-group">
              <button class="action-btn btn-alarm" onclick="sendAction('ALARM', '${r}')">🔥 Test Fire</button>
              <button class="action-btn btn-warn" onclick="sendAction('WARNING', '${r}')">⚠️ Test Warning</button>
              <button class="action-btn" onclick="sendAction('CLEAR', '${r}')">✓ Clear</button>
            </div>
          </div>
        `;
      });
      rc.innerHTML = html;

      // 4. Alerts
      const ac = document.getElementById('alerts-container');
      const al = document.getElementById('alerts-list');
      if (data.alerts && data.alerts.length > 0) {
        ac.style.display = 'block';
        al.innerHTML = data.alerts.map(a => `<div class="alert-item">⚠️ ${a}</div>`).join('');
      } else {
        ac.style.display = 'none';
      }
    }

    async function setManual(device, index = 0, value = null) {
      try {
        await fetch('/api/manual', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device, index, value })
        });
        fetchState();
      } catch (e) {
        console.error("Manual control error:", e);
      }
    }

    async function sendAction(action, room = null) {
      try {
        await fetch('/api/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action, room })
        });
        fetchState();
      } catch (e) {
        console.error("Action error:", e);
      }
    }

    setInterval(fetchState, 500);
    fetchState();
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    return DASHBOARD_HTML


def gen_frames():
    while True:
        with frame_lock:
            frame_bytes = latest_jpeg
        if frame_bytes is not None:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
        time.sleep(0.04)


@app.get("/video_feed")
def video_feed():
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/state")
def api_state():
    with lock:
        return jsonify(system_state)


@app.post("/api/manual")
def api_manual():
    global manual_fan, manual_buz, manual_s
    data = request.get_json(force=True) or {}
    dev = data.get("device")
    idx = int(data.get("index", 0))
    val = data.get("value")  # None, 0, 1, 2, 3
    with lock:
        if dev == "fan" and 0 <= idx < len(manual_fan):
            manual_fan[idx] = val
        elif dev == "buz" and 0 <= idx < len(manual_buz):
            manual_buz[idx] = val
        elif dev == "led" and 0 <= idx < len(manual_s):
            manual_s[idx] = val
        elif dev == "all_auto":
            manual_fan = [None] * len(C.ROOMS)
            manual_buz = [None] * len(C.ROOMS)
            manual_s = [None] * len(C.STRIP_SLOTS)
    return jsonify({
        "ok": True,
        "manual_fan": manual_fan,
        "manual_buz": manual_buz,
        "manual_s": manual_s,
    })


@app.post("/api/action")
def api_action():
    global sim, reset_until, manual_fan, manual_buz, manual_s
    data = request.get_json(force=True) or {}
    act = data.get("action")
    room = data.get("room")
    with lock:
        if act == "ALARM" and room in C.ROOMS:
            sim[room] = None if sim[room] == "ALARM" else "ALARM"
        elif act == "WARNING" and room in C.ROOMS:
            sim[room] = None if sim[room] == "WARNING" else "WARNING"
        elif act == "CLEAR" and room in C.ROOMS:
            sim[room] = None
        elif act == "RESET":
            sim = {r: None for r in C.ROOMS}
            manual_fan = [None] * len(C.ROOMS)
            manual_buz = [None] * len(C.ROOMS)
            manual_s = [None] * len(C.STRIP_SLOTS)
            reset_until = time.time() + 3.0
    return jsonify({"ok": True, "sim": sim})


@app.post("/telemetry")
def telem():
    global telemetry
    d = request.get_json(force=True)
    with lock:
        d["rx"] = time.time()
        telemetry = d
        cmd = dict(commands)
        cmd["reset"] = 1 if time.time() < reset_until else 0
    return jsonify(cmd)


@app.get("/status")
def status():
    with lock:
        return jsonify({"telemetry": telemetry, "commands": commands})


threading.Thread(target=lambda: app.run("0.0.0.0", C.SERVER_PORT, threaded=True), daemon=True).start()


def find_esp32_port():
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").lower()
            if "cp210" in desc or "ch340" in desc or "usb serial" in desc or "10c4:ea60" in hwid or "1a86:7523" in hwid:
                return p.device
    except Exception:
        pass
    return None

# ---------------- Serial link to the ESP32 Hub ----------------
def serial_link(port):
    """ESP32 Hub over USB: parse 'T,...' telemetry lines, send 'C,...' command lines."""
    global telemetry
    import serial

    while True:
        ser = None
        target_port = port or find_esp32_port()
        if not target_port:
            print("[Serial] Waiting for ESP32 Hub to be plugged in...")
            time.sleep(2.0)
            continue

        print(f"[Serial] Connecting to ESP32 Hub on {target_port} @ 115200 baud...")
        try:
            ser = serial.Serial(target_port, 115200, timeout=0.2)
            time.sleep(1.5)  # Wait for board reset after opening port
            ser.reset_input_buffer()
            print(f"[Serial] Connected to {target_port} successfully! Receiving telemetry...")
            last_tx = 0.0
            while True:
                line = ser.readline().decode(errors="ignore").strip()
                if line.startswith("T,"):
                    try:
                        p = [int(x) for x in line.split(",")[1:]]
                        rooms = [
                            {
                                "lvl": p[i * 5],
                                "f": p[i * 5 + 1],
                                "gr": p[i * 5 + 2] / 100.0,
                                "t": p[i * 5 + 3],
                                "h": p[i * 5 + 4],
                            }
                            for i in range(len(C.ROOMS))
                        ]
                        with lock:
                            telemetry = {"rooms": rooms, "rx": time.time()}
                    except (ValueError, IndexError):
                        pass
                if time.time() - last_tx >= 0.5:
                    last_tx = time.time()
                    with lock:
                        c = commands
                        rst = 1 if time.time() < reset_until else 0
                        msg = "C,%s,%s,%s,%d\n" % (
                            ",".join(map(str, c["s"])),
                            ",".join(map(str, c["buz"])),
                            ",".join(map(str, c["fan"])),
                            rst,
                        )
                    ser.write(msg.encode())
        except Exception as e:
            print(f"[Serial WARN] COM port error on {target_port}: {e}")
            print("Note: If Arduino Serial Monitor is open, please close it so Python can access the port.")
            if ser:
                try: ser.close()
                except Exception: pass
            time.sleep(2.0)


# Start serial thread with given port or auto-detect
serial_target = args.serial if args.serial else find_esp32_port()
if serial_target or not args.sim:
    threading.Thread(target=serial_link, args=(args.serial,), daemon=True).start()


def room_levels():
    now, out = time.time(), {}
    t = telemetry
    fresh = t is not None and (now - t.get("rx", 0) <= C.STALE_AFTER_S)
    for r in C.ROOMS:
        if not fresh:
            lvl = "NORMAL" if args.sim else "UNKNOWN"
        else:
            idx = C.ROOM_INDEX[r]
            if "rooms" in t and idx < len(t["rooms"]):
                rd = t["rooms"][idx]
                lvl = LV[min(int(rd.get("lvl", 0)), 2)]
                if rd.get("f") and lvl == "NORMAL":
                    lvl = "UNKNOWN"
            else:
                lvl = "NORMAL" if args.sim else "UNKNOWN"

        if sim[r] and RANK[sim[r]] > RANK[lvl]:
            lvl = sim[r]
        out[r] = lvl
    return out


# ---------------- Vision setup ----------------
counter = cam = None
if not args.no_camera:
    try:
        from vision import CameraStream, CrowdCounter
        counter, cam = CrowdCounter(), CameraStream(args.source)
        print(f"[Vision] Camera source {args.source} initialized.")
    except FileNotFoundError as e:
        print(f"\n[Notice] Calibration files not found: {e}")
        print("Run 'python calibrate.py' first to calibrate rooms and tokens.")
        print("Falling back to --no-camera mode with simulated counts.\n")
        counter = cam = None

fake = dict(zip(C.ROOMS, [int(x) for x in args.fake.split(",")]))
last_counts = {r: 0 for r in C.ROOMS}

planner = Planner()
log = csv.writer(open("events.csv", "a", newline=""))
last_sig = None
canvas_h = 420

print("\n=======================================================")
print("  CROWD CURRENT CONTROLLER RUNNING")
print(f"  Web Dashboard : http://localhost:{C.SERVER_PORT}")
print("  OpenCV Window : Press 'q' to exit")
print("=======================================================\n")

while True:
    now = time.time()
    cam_alert = None
    if counter:
        frame, age = cam.read()
        if frame is not None and age < C.CAM_STALE_S:
            last_counts, vis, _ = counter.count(frame)
        else:
            vis = np.zeros((canvas_h, C.FRAME_W, 3), np.uint8)
            cam_alert = "CAMERA STALE - using last counts"
    else:
        last_counts = fake
        vis = np.zeros((canvas_h, C.FRAME_W, 3), np.uint8)

    with lock:
        levels = room_levels()
        res = planner.plan(levels, last_counts, now)

        # Automatic decisions
        auto_s = [res["strips"].get(s, IDLE) for s in C.STRIP_SLOTS]
        auto_fan = [int(levels[r] != "NORMAL") for r in C.ROOMS]
        auto_buz = [2 if res["mode"] == "EVACUATE" else 0 for r in C.ROOMS]

        # Apply manual overrides if set by the operator in the Web UI
        final_s = [
            manual_s[i] if manual_s[i] is not None else auto_s[i]
            for i in range(len(C.STRIP_SLOTS))
        ]
        final_fan = [
            manual_fan[i] if manual_fan[i] is not None else auto_fan[i]
            for i in range(len(C.ROOMS))
        ]
        final_buz = [
            manual_buz[i] if manual_buz[i] is not None else auto_buz[i]
            for i in range(len(C.ROOMS))
        ]

        commands = {
            "s": final_s,
            "fan": final_fan,
            "buz": final_buz,
        }

        # Update global system state for Web UI
        system_state = {
            "mode": res["mode"],
            "levels": levels,
            "counts": last_counts,
            "assign": res["assign"],
            "alerts": res["alerts"] + ([cam_alert] if cam_alert else []),
            "fans": commands["fan"],
            "buz": commands["buz"],
            "strips": commands["s"],
            "manual": {
                "fans": list(manual_fan),
                "buz": list(manual_buz),
                "strips": list(manual_s),
            },
            "telemetry": telemetry,
            "timestamp": now,
        }

    # ---- HUD overlay ----
    cv2.rectangle(vis, (0, 0), (330, 40 + 62 * len(C.ROOMS) + 30), (20, 20, 20), -1)
    mc = {"NORMAL": (0, 200, 0), "ADVISORY": (0, 200, 255), "EVACUATE": (0, 0, 255)}[res["mode"]]
    cv2.putText(vis, f"MODE: {res['mode']}", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, mc, 2)
    y = 62
    for r in C.ROOMS:
        a = res["assign"].get(r)
        cv2.putText(
            vis,
            f"{r} n={last_counts.get(r, 0)}  {levels[r]}",
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            COL[levels[r]],
            2,
        )
        txt = ("-> " + a["exit"] + "  " + ">".join(a["path"])) if a else "no guidance"
        cv2.putText(vis, txt, (10, y + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
        y += 62
    for i, msg in enumerate(res["alerts"] + ([cam_alert] if cam_alert else [])):
        cv2.putText(vis, msg, (10, y + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    # Encode current frame to JPEG for Web Stream
    ret, jpeg_buffer = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if ret:
        with frame_lock:
            latest_jpeg = jpeg_buffer.tobytes()

    cv2.imshow("Crowd Current", vis)

    # ---- Event log ----
    sig = (res["mode"], tuple(levels.values()), tuple((r, a["exit"]) for r, a in res["assign"].items()))
    if sig != last_sig:
        log.writerow([
            f"{now:.2f}",
            res["mode"],
            levels,
            last_counts,
            {r: a["exit"] for r, a in res["assign"].items()},
            res["alerts"],
        ])
        last_sig = sig

    k = cv2.waitKey(30) & 0xFF
    if k == ord("q"):
        break
    if k in (ord("1"), ord("2"), ord("3")):
        r = C.ROOMS[k - ord("1")]
        sim[r] = None if sim[r] == "ALARM" else "ALARM"
    if k in (ord("4"), ord("5"), ord("6")):
        r = C.ROOMS[k - ord("4")]
        sim[r] = None if sim[r] == "WARNING" else "WARNING"
    if k == ord("r"):
        sim = {r: None for r in C.ROOMS}
        with lock:
            manual_fan = [None] * len(C.ROOMS)
            manual_buz = [None] * len(C.ROOMS)
            manual_s = [None] * len(C.STRIP_SLOTS)
        reset_until = time.time() + 3.0

cv2.destroyAllWindows()
