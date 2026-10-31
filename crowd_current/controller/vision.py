"""Top-down people-token counter (phone camera above the model).

Method: background subtraction against a photo of the EMPTY model, restricted to
each room polygon.  Blobs are counted by area relative to ONE token (so two
touching capacitors count as 2).  A median over several frames removes flicker
from hands placing tokens, camera noise and LED glare.
"""
import collections, json, os, threading, time, sys
import cv2
import numpy as np
import config as C

HERE = os.path.dirname(os.path.abspath(__file__))
ROOMS_JSON = os.path.join(HERE, "rooms.json")
BG_PNG = os.path.join(HERE, "background.png")
CALIB_JSON = os.path.join(HERE, "calib.json")


def resize(frame):
    h, w = frame.shape[:2]
    return cv2.resize(frame, (C.FRAME_W, int(h * C.FRAME_W / w)))


class CameraStream:
    """Always returns the NEWEST frame (no buffered lag) and reconnects by itself."""
    def __init__(self, source):
        self.source = int(source) if str(source).isdigit() else source
        self.frame, self.ts, self._run = None, 0.0, True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        cap = None
        while self._run:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(self.source)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    time.sleep(1.0); continue
            ok, f = cap.read()
            if ok:
                self.frame, self.ts = f, time.time()
            else:
                cap.release(); cap = None; time.sleep(0.5)

    def read(self):
        return (None if self.frame is None else resize(self.frame)), time.time() - self.ts

    def stop(self):
        self._run = False


def _prep(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (7, 7), 0)


class CrowdCounter:
    def __init__(self):
        self.polys = {k: np.array(v, np.int32) for k, v in json.load(open(ROOMS_JSON)).items()}
        self.bg = _prep(cv2.imread(BG_PNG))
        cal = json.load(open(CALIB_JSON)) if os.path.exists(CALIB_JSON) else {}
        self.single = cal.get("single_area", 400)
        self.thr = cal.get("diff_threshold", C.DIFF_THRESHOLD)
        self.hist = {r: collections.deque(maxlen=C.SMOOTH_FRAMES) for r in self.polys}
        self.masks = {}
        self.all_mask = None

    def _make_masks(self, shape):
        self.masks = {}
        for r, p in self.polys.items():
            m = np.zeros(shape, np.uint8); cv2.fillPoly(m, [p], 255); self.masks[r] = m
        self.all_mask = np.maximum.reduce(list(self.masks.values()))

    def foreground(self, frame):
        g = _prep(frame)
        if not self.masks:
            self._make_masks(g.shape)
        # compensate slow exposure drift of the phone camera
        sel = self.all_mask > 0
        gain = (np.median(g[sel]) + 1) / (np.median(self.bg[sel]) + 1)
        bg = np.clip(self.bg.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        fg = (cv2.absdiff(g, bg) > self.thr).astype(np.uint8) * 255
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        return fg

    def count(self, frame):
        fg = self.foreground(frame)
        vis = frame.copy()
        counts, raw = {}, {}
        for r, mask in self.masks.items():
            n, _, stats, cent = cv2.connectedComponentsWithStats(cv2.bitwise_and(fg, mask))
            total = 0
            for i in range(1, n):
                a = stats[i, cv2.CC_STAT_AREA]
                if a < 0.4 * self.single:
                    continue
                k = max(1, int(round(a / self.single))) if a > 1.5 * self.single else 1
                total += k
                x, y, w, h = stats[i, :4]
                cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 0), 1)
                if k > 1:
                    cv2.putText(vis, f"x{k}", (x, y - 3), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 255, 0), 1)
            raw[r] = total
            self.hist[r].append(total)
            counts[r] = int(round(float(np.median(self.hist[r]))))
            cv2.polylines(vis, [self.polys[r]], True, (255, 160, 0), 2)
            x, y = self.polys[r][0]
            cv2.putText(vis, f"{r}:{counts[r]}", (int(x), int(y) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, .7, (255, 160, 0), 2)
        return counts, vis, fg
