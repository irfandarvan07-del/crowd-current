"""Hazard-first, congestion-aware exit assignment + LED strip commands."""
import heapq
import config as C

ADJ = {}
for _a, _b, _w, _s in C.EDGES:
    ADJ.setdefault(_a, []).append((_b, _w))
    ADJ.setdefault(_b, []).append((_a, _w))

# LED strip codes sent to the ESP32 nodes
IDLE, FWD_GREEN, REV_GREEN, BLOCKED, FWD_AMBER, REV_AMBER = 0, 1, 2, 3, 4, 5


def _dijkstra(src, blocked, node_pen, edge_dir):
    dist, prev = {src: 0.0}, {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18) or u in C.EXITS:      # exits are terminal
            continue
        for v, w in ADJ[u]:
            if v in blocked:
                continue
            cost = w + node_pen.get(v, 0.0)
            if edge_dir.get(frozenset((u, v))) == (v, u):
                cost += C.COUNTERFLOW_PENALTY
            nd = d + cost
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def _path(prev, src, dst):
    p = [dst]
    while p[-1] != src:
        p.append(prev[p[-1]])
    return p[::-1]


class Planner:
    def __init__(self):
        self.assign = {}      # room -> exit (for hysteresis)
        self.since = {}       # room -> time assignment was made

    def plan(self, levels, counts, now):
        """levels: room -> NORMAL|WARNING|UNKNOWN|ALARM ; counts: room -> int"""
        hazard = {r for r, l in levels.items() if l == "ALARM"}
        soft = {r: C.WARN_PENALTY for r, l in levels.items() if l in ("WARNING", "UNKNOWN")}
        evac = bool(hazard)
        mode = "EVACUATE" if evac else ("ADVISORY" if soft else "NORMAL")
        out = {"mode": mode, "assign": {}, "alerts": [], "strips": {}}
        if mode == "NORMAL":
            self.assign.clear(); self.since.clear()
            out["strips"] = {s: IDLE for *_, s in C.EDGES if s}
            return out

        load = {e: 0.0 for e in C.EXITS}
        edge_dir = {}
        order = sorted(C.ROOMS, key=lambda r: (r not in hazard, -counts.get(r, 0)))
        for r in order:
            n = max(counts.get(r, 0), 1)
            dist, prev = _dijkstra(r, hazard - {r}, soft, edge_dir)
            cand = {e: dist[e] for e in C.EXITS if e in dist}
            if not cand:
                out["alerts"].append(f"NO ELIGIBLE EXIT for {r}")
                self.assign.pop(r, None)
                continue
            score = {e: cand[e] + C.CONGESTION_K * (load[e] + n) / C.EXITS[e]["cap"] for e in cand}
            best = min(score, key=score.get)
            choice, old = best, self.assign.get(r)
            if old in cand and old != best:
                held = now - self.since.get(r, 0) < C.MIN_DWELL_S
                if held or score[old] <= score[best] * (1 + C.HYSTERESIS):
                    choice = old
            if choice != old:
                self.since[r] = now
            self.assign[r] = choice
            path = _path(prev, r, choice)
            load[choice] += n
            for u, v in zip(path, path[1:]):
                edge_dir.setdefault(frozenset((u, v)), (u, v))
            out["assign"][r] = {"exit": choice, "path": path, "n": n,
                                "score": round(score[choice], 1)}

        # ---- LED strips --------------------------------------------------
        for a, b, _w, s in C.EDGES:
            if not s:
                continue
            fwd = rev = 0
            for info in out["assign"].values():
                for u, v in zip(info["path"], info["path"][1:]):
                    if (u, v) == (a, b): fwd += info["n"]
                    elif (u, v) == (b, a): rev += info["n"]
            if fwd or rev:
                f = fwd >= rev
                out["strips"][s] = (FWD_GREEN if f else REV_GREEN) if evac else (FWD_AMBER if f else REV_AMBER)
                if fwd and rev:
                    out["alerts"].append(f"counter-flow on {a}-{b}")
            elif a in hazard or b in hazard:
                out["strips"][s] = BLOCKED
            else:
                out["strips"][s] = IDLE
        return out
