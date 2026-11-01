"""Crowd Current controller with real-time OpenCV HUD and Web Dashboard.

Run:
  python main.py [--serial COMx] [--sim] [--no-camera] [--source 1]

Web Dashboard available at:
  http://localhost:5000  (or http://<laptop-ip>:5000 on your local Wi-Fi / phone)

Keys in the OpenCV window:
  1 / 2 / 3 = toggle simulated ALARM in room 1 / 2 / 3
  4 / 5 / 6 = toggle simulated WARNING in room 1 / 2 / 3
  r         = reset latched alarms
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

system_state = {
    "mode": "NORMAL",
    "levels": {r: "NORMAL" for r in C.ROOMS},
    "counts": {r: 0 for r in C.ROOMS},
    "assign": {},
    "alerts": [],
    "fans": [0] * len(C.ROOMS),
    "buz": [0] * len(C.ROOMS),
    "strips": [0] * len(C.STRIP_SLOTS),
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
  <title>Crowd Current — Mission Control</title>
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
    .title { font-size: 1.5rem; font-weight: 700; letter-spacing: 0.5px; }
    .subtitle { font-size: 0.85rem; color: var(--text-muted); }
    .status-badge { font-size: 0.95rem; font-weight: 700; padding: 0.4rem 1rem; border-radius: 9999px; text-transform: uppercase; letter-spacing: 1px; display: inline-flex; align-items: center; gap: 0.5rem; }
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
    
    .room-card { margin-bottom: 1rem; border: 1px solid var(--border); border-radius: 10px; padding: 1rem; background: rgba(255,255,255,0.02); transition: all 0.2s; }
    .room-card:last-child { margin-bottom: 0; }
    .room-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }
    .room-name { font-size: 1.15rem; font-weight: 700; }
    .room-meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; margin-bottom: 0.75rem; text-align: center; }
    .meta-box { background: rgba(0,0,0,0.25); padding: 0.5rem; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); }
    .meta-label { font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; margin-bottom: 0.25rem; }
    .meta-val { font-size: 0.95rem; font-weight: 700; }
    .route-box { background: rgba(59, 130, 246, 0.1); border: 1px solid rgba(59, 130, 246, 0.3); padding: 0.6rem 0.8rem; border-radius: 6px; font-size: 0.85rem; color: #93c5fd; margin-bottom: 0.75rem; font-weight: 500; }
    
    .actuators { display: flex; gap: 0.75rem; font-size: 0.8rem; color: var(--text-muted); }
    .act-tag { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.25rem 0.5rem; border-radius: 4px; background: rgba(255,255,255,0.05); }
    .act-active { background: rgba(16, 185, 129, 0.2); color: var(--green); font-weight: 600; }
    .act-hazard { background: rgba(239, 68, 68, 0.2); color: var(--red); font-weight: 600; }

    .btn-group { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
    button { background: #1e293b; color: white; border: 1px solid #334155; padding: 0.4rem 0.75rem; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }
    button:hover { background: #334155; }
    .btn-alarm { background: rgba(239, 68, 68, 0.2); border-color: rgba(239, 68, 68, 0.4); color: #fca5a5; }
    .btn-alarm:hover { background: rgba(239, 68, 68, 0.4); }
    .btn-warn { background: rgba(245, 158, 11, 0.2); border-color: rgba(245, 158, 11, 0.4); color: #fde68a; }
    .btn-warn:hover { background: rgba(245, 158, 11, 0.4); }
    .btn-reset { background: linear-gradient(135deg, #2563eb, #1d4ed8); border: none; padding: 0.5rem 1.25rem; font-size: 0.9rem; }
    .btn-reset:hover { opacity: 0.9; }

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
        <div class="subtitle">Multi-Node Intelligent Evacuation & Hazard Control</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 1rem;">
      <div id="mode-badge" class="status-badge badge-normal">NORMAL</div>
      <button class="btn-reset" onclick="sendAction('RESET')">↺ RESET ALARMS</button>
    </div>
  </div>

  <div class="grid">
    <!-- Camera Section -->
    <div class="card">
      <div class="card-title">
        <span>Overhead Vision Stream (HUD)</span>
        <span id="cam-status" style="font-size: 0.8rem; color: var(--green);">● LIVE FEED</span>
      </div>
      <div class="video-container">
        <img class="video-feed" src="/video_feed" alt="Camera Stream" onerror="this.style.display='none'; document.getElementById('vid-fallback').style.display='block';">
        <div id="vid-fallback" style="display:none; padding: 3rem; text-align: center; color: var(--text-muted);">
          Camera Feed Offline or in Simulation Mode
        </div>
      </div>
      <div style="margin-top: 1rem; font-size: 0.8rem; color: var(--text-muted); display: flex; justify-content: space-between;">
        <span>Routing Algorithm: Congestion-Aware Dijkstra</span>
        <span>Detection: Blob Surface Integration</span>
      </div>
    </div>

    <!-- Rooms Section -->
    <div class="card">
      <div class="card-title">
        <span>Room Hazard & Route Telemetry</span>
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
      const anyAlarm = Object.values(data.levels).includes('ALARM');
      const sirenTag = document.getElementById('aux-siren');
      sirenTag.innerText = anyAlarm ? '🚨 SIREN: SOUNDING' : 'SIREN: SILENT';
      sirenTag.className = 'act-tag ' + (anyAlarm ? 'act-hazard' : '');

      // 3. Rooms
      const rc = document.getElementById('rooms-container');
      const rooms = ['R1', 'R2', 'R3'];
      let html = '';

      rooms.forEach((r, idx) => {
        const lvl = data.levels[r] || 'UNKNOWN';
        const count = data.counts[r] || 0;
        const assign = data.assign[r];
        const telem = (data.telemetry && data.telemetry.rooms && data.telemetry.rooms[idx]) ? data.telemetry.rooms[idx] : null;

        const gas = telem ? telem.gr.toFixed(2) + 'x' : '--';
        const temp = telem && telem.t >= 0 ? telem.t + ' °C' : '--';
        const hum = telem && telem.h >= 0 ? telem.h + ' %' : '--';
        const flame = telem ? (telem.fl ? '🔥 FLAME' : 'CLEAR') : '--';

        const fanOn = data.fans && data.fans[idx];
        const stripMode = data.strips ? data.strips[idx] : 0;
        let ledText = 'OFF';
        if (stripMode === 1 || stripMode === 2) ledText = 'EVAC GREEN';
        else if (stripMode === 3) ledText = 'STROBE RED';
        else if (stripMode === 4 || stripMode === 5) ledText = 'AMBER PULSE';

        const routeText = assign ? `🚪 Evacuate via <b>${assign.exit}</b> &nbsp;(Path: ${assign.path.join(' → ')})` : 'No active evacuation guidance';

        let badgeClass = 'badge-normal';
        if (lvl === 'ALARM') badgeClass = 'badge-evacuate';
        else if (lvl === 'WARNING') badgeClass = 'badge-advisory';
        else if (lvl === 'UNKNOWN') badgeClass = '';

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
            <div class="actuators">
              <span class="act-tag ${fanOn ? 'act-active' : ''}">🌀 Fan: ${fanOn ? 'RUNNING' : 'OFF'}</span>
              <span class="act-tag ${stripMode===3 ? 'act-hazard' : (stripMode ? 'act-active' : '')}">💡 LEDs: ${ledText}</span>
            </div>
            <div class="btn-group">
              <button class="btn-alarm" onclick="sendAction('ALARM', '${r}')">🔥 Test Alarm</button>
              <button class="btn-warn" onclick="sendAction('WARNING', '${r}')">⚠️ Test Warning</button>
              <button onclick="sendAction('CLEAR', '${r}')">✓ Clear</button>
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


@app.post("/api/action")
def api_action():
    global sim, reset_until
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


# ---------------- Serial link to the ESP32 Hub ----------------
def serial_link(port):
    """ESP32 Hub over USB: parse 'T,...' telemetry lines, send 'C,...' command lines."""
    global telemetry
    import serial
    print(f"[Serial] Connecting to ESP32 Hub on {port} @ 115200 baud...")
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"[Serial ERROR] Could not open {port}: {e}")
        return

    time.sleep(2.0)  # Wait for board reset after opening port
    print(f"[Serial] Connected to {port} successfully.")
    last_tx = 0.0
    while True:
        try:
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
            print(f"[Serial WARN] Communication error: {e}")
            time.sleep(1.0)


if args.serial:
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
        commands = {
            "s": [res["strips"].get(s, IDLE) for s in C.STRIP_SLOTS],
            "fan": [int(levels[r] != "NORMAL") for r in C.ROOMS],
            "buz": [2 if res["mode"] == "EVACUATE" else 0 for r in C.ROOMS],
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
        reset_until = time.time() + 3.0

cv2.destroyAllWindows()
