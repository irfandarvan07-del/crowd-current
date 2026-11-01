"""Everything you need to edit for YOUR model lives in this file.

Map (from the hand sketch): 3 rooms (R1,R2,R3), 3 exits (E1,E2,E3).
EDGES are the doors/corridors.  Lengths are in cm on the model (measure them).
A strip name (L1..L4) means an addressable LED strip is laid along that edge.
Wire each strip so DATA flows from the FIRST node of the edge to the SECOND.
"""

# ---- hardware nodes (3 Arduino Unos via ESP32-S3 Hub) ----------------------
ROOM_INDEX = {"R1": 0, "R2": 1, "R3": 2}
STRIP_SLOTS = ["L1", "L2", "L3", "L4"]
ROOMS = ["R1", "R2", "R3"]
EXITS = {"E1": {"cap": 1.0}, "E2": {"cap": 1.0}, "E3": {"cap": 1.0}}  # cap = relative width

# (node_a, node_b, length_cm, led_strip_or_None)
EDGES = [
    ("R1", "E3", 30, "L1"),
    ("R1", "E2", 45, None),
    ("R1", "R2", 25, "L4"),   # door between room 1 and 2
    ("R2", "E1", 30, "L2"),
    ("R2", "E2", 40, None),
    ("R3", "E3", 35, None),
    ("R3", "E1", 70, "L3"),
]

# ---- routing tuning --------------------------------------------------------
CONGESTION_K = 6.0        # cm-equivalent cost per person queued at an exit (/cap)
WARN_PENALTY = 40.0       # extra cost to walk THROUGH a warning/unknown room
COUNTERFLOW_PENALTY = 60  # extra cost to use a corridor already used the other way
HYSTERESIS = 0.15         # new exit must be >15% better to switch ...
MIN_DWELL_S = 4.0         # ... and an assignment is held at least this long
STALE_AFTER_S = 5.0       # no telemetry for this long => room is UNKNOWN (never "safe")
CAM_STALE_S = 3.0

# ---- vision ----------------------------------------------------------------
# DroidCam / Webcam index (0 = default webcam, 1 = DroidCam / secondary cam)
CAMERA_SOURCE = 1
FRAME_W = 960             # all processing is done at this width
DIFF_THRESHOLD = 28       # grey-level change that counts as "something is on the floor"
SMOOTH_FRAMES = 7         # median over this many frames
SERVER_PORT = 8000
