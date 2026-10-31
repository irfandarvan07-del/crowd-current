"""Crowd Current controller.
Run:
  python main.py [--serial COMx] [--sim] [--no-camera] [--source 1]

Keys in the window:
  1 / 2 / 3 = toggle simulated ALARM in room 1 / 2 / 3
  4 / 5 / 6 = toggle simulated WARNING in room 1 / 2 / 3
  r         = reset latched alarms
  q         = quit
"""
import argparse, csv, logging, threading, time
import cv2, numpy as np
from flask import Flask, jsonify, request
import config as C
from routing import Planner, IDLE

ap = argparse.ArgumentParser(description="Crowd Current Controller")
ap.add_argument("--sim", action="store_true", help="no hardware connected: absent nodes count as NORMAL")
ap.add_argument("--no-camera", action="store_true", help="disable camera and use fake/zero counts")
ap.add_argument("--fake", default="0,0,0", help="fake counts per room (e.g. '2,4,1') when --no-camera")
ap.add_argument("--source", default=C.CAMERA_SOURCE, help="camera index or video URL (default: config.CAMERA_SOURCE)")
ap.add_argument("--serial", default=None, help="ESP32-S3 Hub COM port (e.g. COM5 or /dev/ttyUSB0)")
args = ap.parse_args()

RANK = {"NORMAL": 0, "UNKNOWN": 1, "WARNING": 1, "ALARM": 2}
LV = ["NORMAL", "WARNING", "ALARM"]
COL = {"NORMAL": (0, 200, 0), "WARNING": (0, 200, 255), "UNKNOWN": (180, 180, 180), "ALARM": (0, 0, 255)}

lock = threading.Lock()
telemetry = None
commands = {
    "s": [0] * len(C.STRIP_SLOTS),
    "buz": [0] * len(C.ROOMS),
    "fan": [0] * len(C.ROOMS),
}
reset_until = 0.0
sim = {r: None for r in C.ROOMS}

# ---------------- HTTP link to any web-based clients / nodes ----------------
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


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


# ---------------- Serial link to the ESP32-S3 Hub ----------------
def serial_link(port):
    """ESP32-S3 Hub over USB: parse 'T,...' telemetry lines, send 'C,...' command lines."""
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
                    pass  # Partial or damaged frame
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

print("\n--- Crowd Current Controller Running ---")
print("Press 'q' in the OpenCV window to exit.\n")

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
    cv2.imshow("Crowd Current", vis)

    # ---- Event log (logged whenever routing mode, room levels, or exit assignments change) ----
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
