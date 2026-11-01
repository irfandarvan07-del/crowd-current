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
from flask import Flask, jsonify, request, Response, send_file
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
app = Flask(__name__, template_folder='templates', static_folder='static')
logging.getLogger('werkzeug').setLevel(logging.ERROR)

sim_enabled = False
sync_hardware = True
sim_counts = {r: 10 for r in C.ROOMS}

@app.get('/')
def index():
    return send_file('templates/index.html')


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


@app.post("/api/sim")
def api_sim():
    global sim_enabled, sync_hardware, sim_counts, sim, reset_until
    data = request.get_json(force=True) or {}
    with lock:
        if "enabled" in data:
            sim_enabled = bool(data["enabled"])
        if "sync_hardware" in data:
            sync_hardware = bool(data["sync_hardware"])
        if "counts" in data and isinstance(data["counts"], dict):
            for r, c in data["counts"].items():
                if r in C.ROOMS:
                    sim_counts[r] = max(0, int(c))
        if "fire" in data:
            r = data["fire"]
            if r in C.ROOMS:
                sim[r] = "ALARM"
        if "warn" in data:
            r = data["warn"]
            if r in C.ROOMS:
                sim[r] = "WARNING"
        if "clear" in data:
            r = data["clear"]
            if r in C.ROOMS:
                sim[r] = "NORMAL"
        if data.get("reset"):
            sim = {r: None for r in C.ROOMS}
            sim_counts = {r: 10 for r in C.ROOMS}
            reset_until = time.time() + 3.0
    return jsonify({
        "ok": True,
        "sim_enabled": sim_enabled,
        "sync_hardware": sync_hardware,
        "sim": sim,
        "sim_counts": sim_counts,
    })


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
        if sim_enabled and sim.get(r):
            lvl = sim[r]
        elif not fresh:
            lvl = "NORMAL" if (args.sim or sim_enabled) else "UNKNOWN"
        else:
            idx = C.ROOM_INDEX[r]
            if "rooms" in t and idx < len(t["rooms"]):
                rd = t["rooms"][idx]
                lvl = LV[min(int(rd.get("lvl", 0)), 2)]
                if rd.get("f") and lvl == "NORMAL":
                    lvl = "UNKNOWN"
            else:
                lvl = "NORMAL" if (args.sim or sim_enabled) else "UNKNOWN"

        if sim.get(r) and RANK[sim[r]] > RANK[lvl]:
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
        active_counts = dict(sim_counts) if (sim_enabled and sim_counts) else last_counts
        levels = room_levels()
        res = planner.plan(levels, active_counts, now)

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

        if not sim_enabled or sync_hardware:
            commands = {
                "s": final_s,
                "fan": final_fan,
                "buz": final_buz,
            }
        else:
            commands = {
                "s": [0] * len(C.STRIP_SLOTS),
                "fan": [0] * len(C.ROOMS),
                "buz": [0] * len(C.ROOMS),
            }

        # Update global system state for Web UI
        system_state = {
            "mode": res["mode"],
            "levels": levels,
            "counts": active_counts,
            "assign": res["assign"],
            "alerts": res["alerts"] + ([cam_alert] if cam_alert else []),
            "fans": final_fan,
            "buz": final_buz,
            "strips": final_s,
            "manual": {
                "fans": list(manual_fan),
                "buz": list(manual_buz),
                "strips": list(manual_s),
            },
            "telemetry": telemetry,
            "timestamp": now,
            "sim": {
                "enabled": sim_enabled,
                "sync_hardware": sync_hardware,
                "levels": sim,
                "counts": sim_counts,
            },
            "topology": {
                "rooms": C.ROOMS,
                "exits": C.EXITS,
                "edges": [
                    {"from": a, "to": b, "len": w, "strip": s}
                    for a, b, w, s in C.EDGES
                ],
                "strip_slots": C.STRIP_SLOTS,
                "k_congestion": C.CONGESTION_K,
                "warn_penalty": C.WARN_PENALTY,
                "counterflow_penalty": C.COUNTERFLOW_PENALTY,
            }
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
            f"{r} n={active_counts.get(r, 0)}  {levels[r]}",
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
