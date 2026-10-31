"""One-time set-up:  python calibrate.py [camera_source]
 1. click the corners of each room (n = next room, u = undo)
 2. with the model EMPTY press b  -> saves background photo
 3. put ONE token on the floor, press t -> measures the area of a single person
"""
import json, sys
import cv2, numpy as np
import config as C
import vision as V

src = sys.argv[1] if len(sys.argv) > 1 else C.CAMERA_SOURCE
print(f"Connecting to camera source: {src} ...", flush=True)
cam = V.CameraStream(src)
print("Waiting for camera feed...", flush=True)
while cam.read()[0] is None:
    cv2.waitKey(100)
print("Camera stream received! Instructions:", flush=True)
print("1. Click the corners of each room polygon (n = save & next room, u = undo last point).", flush=True)
print("2. When prompted 'bg', remove all tokens and press 'b' to capture clean background.", flush=True)
print("3. When prompted 'token', place ONE token in any room and press 't' to calibrate person token area.", flush=True)
print("4. Press 'q' to quit when finished.", flush=True)

polys, cur, ri, stage = {}, [], 0, "rooms"
single = None


def click(ev, x, y, *_):
    if ev == cv2.EVENT_LBUTTONDOWN and stage == "rooms":
        cur.append([x, y])


cv2.namedWindow("calibrate"); cv2.setMouseCallback("calibrate", click)
while True:
    frame, _ = cam.read()
    vis = frame.copy()
    for r, p in polys.items():
        cv2.polylines(vis, [np.array(p, np.int32)], True, (255, 160, 0), 2)
    if cur:
        cv2.polylines(vis, [np.array(cur, np.int32)], False, (0, 255, 255), 2)
    if stage == "rooms":
        msg = f"Click corners of {C.ROOMS[ri]}  (n=next, u=undo)"
    elif stage == "bg":
        msg = "Remove ALL tokens, press b to save background"
    else:
        msg = "Place ONE token in any room, press t   (q = quit)"
    cv2.putText(vis, msg, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 255), 2)
    cv2.imshow("calibrate", vis)
    k = cv2.waitKey(30) & 0xFF
    if k == ord("u") and cur:
        cur.pop()
    elif k == ord("n") and stage == "rooms" and len(cur) >= 3:
        polys[C.ROOMS[ri]] = cur[:]; cur.clear(); ri += 1
        if ri == len(C.ROOMS):
            json.dump(polys, open(V.ROOMS_JSON, "w")); stage = "bg"
    elif k == ord("b") and stage == "bg":
        frames = []
        for _ in range(20):
            f, _ = cam.read()
            if f is not None:
                frames.append(f.astype(np.float32))
            cv2.waitKey(50)
        if frames:
            acc = np.mean(frames, axis=0)
            cv2.imwrite(V.BG_PNG, acc.astype(np.uint8))
            stage = "token"
            print("background saved")
        else:
            print("Failed to capture frames for background!")
    elif k == ord("t") and stage == "token":
        cc = V.CrowdCounter()
        fg = cc.foreground(frame)
        fg_rooms = cv2.bitwise_and(fg, cc.all_mask)
        n, _, st, _ = cv2.connectedComponentsWithStats(fg_rooms)
        candidates = [int(st[i, cv2.CC_STAT_AREA]) for i in range(1, n) if 200 <= st[i, cv2.CC_STAT_AREA] <= 3500]
        if candidates:
            candidates.sort(key=lambda a: abs(a - 800))
            single = candidates[0]
            json.dump({"single_area": single, "diff_threshold": C.DIFF_THRESHOLD},
                      open(V.CALIB_JSON, "w"))
            print(f"Capacitor/token area calibrated = {single} px  -> saved! Press 'q' to finish.", flush=True)
        elif n > 1:
            single = int(sorted([st[i, cv2.CC_STAT_AREA] for i in range(1, n)])[-1])
            json.dump({"single_area": single, "diff_threshold": C.DIFF_THRESHOLD},
                      open(V.CALIB_JSON, "w"))
            print(f"Capacitor/token area calibrated = {single} px  -> saved! Press 'q' to finish.", flush=True)
        else:
            print("No token detected inside rooms. Please place a token clearly on the floor and press 't' again.", flush=True)
    elif k == ord("q"):
        break
cam.stop(); cv2.destroyAllWindows()
