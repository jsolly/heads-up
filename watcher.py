#!/usr/bin/env python3
"""River Rubber Necker: no-AI Delaware River big-ship watcher.

Subscribes to the AISStream websocket for a Delaware River bounding box, keeps
per-vessel state, projects each big commercial ship onto a channel polyline,
and POSTs JSON events (new_ship, t60, t30, eta_shift, stopped_short, passed,
lost_signal, watcher_error) to a webhook as the ship approaches a private shore
pin. The pin coordinates live ONLY in config.local.json (gitignored).

Usage:
    python3 watcher.py            # run forever
    python3 watcher.py --test     # send one test event and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

CONFIG_PATH = HERE / "config.local.json"
STATE_PATH = HERE / "state.json"
HEARTBEAT_PATH = HERE / "heartbeat.json"
LOG_PATH = HERE / "watcher.log"
PENDING_PATH = HERE / "pending-events.jsonl"
EVENTS_PATH = HERE / "events.jsonl"
ENV_FILE = HERE / ".env.local"  # optional, gitignored; re-read at send time

WS_URL = "wss://stream.aisstream.io/v0/stream"
DEFAULT_BBOX = [[[39.7, -75.55], [40.15, -74.7]]]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIN_LENGTH_M = 120          # any non-excluded type at/above this length
CARGO_MIN_LENGTH_M = 80     # type 70-89 needs at least this (drops small coasters / bunker boats)
PASSENGER_MIN_LENGTH_M = 120  # type 60-69 only "if large"
EXCLUDED_TYPES = set([30, 31, 32, 33, 34, 35, 36, 37]) | set(range(50, 60))
# 30 fishing, 31/32 towing, 33 dredging, 34 diving, 35 military, 36 sailing,
# 37 pleasure, 50 pilot, 51 SAR, 52 tug, 53 port tender, 54 anti-pollution,
# 55 law enforcement, 56/57 spare (often ATB tug+barge units), 58 medical, 59.
EXCLUDED_NAME_WORDS = ("PILOT", "POLICE", "COAST GUARD", "USCG", "FIREBOAT", "BARGE")

UNDERWAY_KN = 3.0
MOVING_KN = 4.0
STOP_KN = 3.0
STOP_MIN_FIXES = 2
STOP_MIN_SECONDS = 180
SOG_WINDOW_S = 15 * 60
SOG_WINDOW_N = 8
T60_MIN = 60
T30_MIN = 30
ETA_SHIFT_MIN = 15
ETA_SHIFT_COOLDOWN_S = 10 * 60
LOST_SIGNAL_S = 45 * 60
MAX_LATERAL_NM = 1.2        # farther than this from the channel = not in the river channel
TURNED_AWAY_S = 10 * 60
TURNED_AWAY_NM = 0.5
BERTHED_CLOSE_S = 6 * 3600  # stopped short this long = transit over
STALE_CLOSE_S = 12 * 3600
FORGET_SHIP_S = 48 * 3600
NEW_TRANSIT_AFTER_PASS_S = 60 * 60
HEARTBEAT_S = 30
WS_SILENT_RECONNECT_S = 300
WS_FAILS_FOR_ERROR = 5
WATCHER_ERROR_COOLDOWN_S = 2 * 3600
ALERT_WINDOW = (5, 20)      # ET hours; only used for a note, never suppresses events

# ---------------------------------------------------------------------------
# Delaware River channel polyline, bay mouth -> Trenton (lat, lon, label).
# Index increases upriver ("northbound").
# ---------------------------------------------------------------------------
CHANNEL = [
    (38.8500, -75.0300, "Bay mouth (Cape Henlopen / Cape May)"),
    (38.9500, -75.1000, "Lower bay / Brandywine Shoal"),
    (39.0500, -75.1700, "Fourteen Foot Bank"),
    (39.1300, -75.2150, "Miah Maull"),
    (39.1900, -75.2700, "Cross Ledge"),
    (39.3000, -75.3750, "Ship John Shoal"),
    (39.3900, -75.4600, "Liston Range"),
    (39.4700, -75.5550, "Reedy Island"),
    (39.5700, -75.5550, "Delaware City / Pea Patch"),
    (39.6550, -75.5480, "New Castle"),
    (39.7120, -75.5080, "Wilmington / Cherry Island"),
    (39.7650, -75.4620, "Bellefonte"),
    (39.8000, -75.4120, "Marcus Hook"),
    (39.8360, -75.3620, "Chester"),
    (39.8480, -75.3220, "Eddystone"),
    (39.8530, -75.2820, "Tinicum (west)"),
    (39.8560, -75.2430, "Tinicum / Paulsboro"),
    (39.8660, -75.2100, "Fort Mifflin / Mantua Creek"),
    (39.8770, -75.1880, "Schuylkill mouth"),
    (39.8820, -75.1600, "Navy Yard / Horseshoe Bend"),
    (39.8990, -75.1320, "Packer Ave / Gloucester"),
    (39.9150, -75.1310, "Greenwich piers"),
    (39.9330, -75.1320, "Camden / Washington Ave"),
    (39.9480, -75.1335, "Penn's Landing"),
    (39.9535, -75.1320, "Ben Franklin Bridge"),
    (39.9665, -75.1225, "Fishtown"),
    (39.9760, -75.0955, "Petty Island"),
    (39.9800, -75.0750, "Port Richmond"),
    (39.9975, -75.0560, "Palmyra"),
    (40.0125, -75.0420, "Tacony-Palmyra Bridge"),
    (40.0290, -75.0040, "Riverton / Torresdale"),
    (40.0530, -74.9600, "Delanco / Andalusia"),
    (40.0710, -74.9120, "Beverly"),
    (40.0830, -74.8660, "Burlington-Bristol Bridge"),
    (40.1180, -74.8270, "Bristol / Florence"),
    (40.1380, -74.7720, "Fairless Hills"),
    (40.1620, -74.7500, "Fairless turning basin"),
    (40.2000, -74.7600, "Trenton"),
]

# Destinations that are short of (south of) a Penn's-Landing-area pin; used only for a hedge note.
SOUTH_OF_PIN_DEST = ("USPHL", "PHILADELPHIA", "PHILADELPIA", "USGLC", "GLOUCESTER", "USCDE", "CAMDEN",
                     "USCMD", "USPAU", "PAULSBORO", "USMAH", "MARCUSHOOK", "USILG", "USWIL",
                     "WILMINGTON", "USDCI", "DELAWARECITY", "CHESTER", "PACKER")

log = logging.getLogger("watcher")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def haversine_nm(lat1, lon1, lat2, lon2):
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


_SEG_LEN = [haversine_nm(CHANNEL[i][0], CHANNEL[i][1], CHANNEL[i + 1][0], CHANNEL[i + 1][1])
            for i in range(len(CHANNEL) - 1)]
_CUM = [0.0]
for _l in _SEG_LEN:
    _CUM.append(_CUM[-1] + _l)


def project_on_channel(lat, lon):
    """Project a point onto the NEAREST channel segment.

    Returns dict(s=along-channel nm from bay mouth, lateral_nm, seg=index,
    bearing=segment bearing in degrees, i.e. the northbound/upriver direction).
    """
    best = None
    for i in range(len(CHANNEL) - 1):
        la, loa, _ = CHANNEL[i]
        lb, lob, _ = CHANNEL[i + 1]
        k = math.cos(math.radians((la + lb) / 2)) * 60.0  # nm per degree lon
        ax, ay = 0.0, 0.0
        bx, by = (lob - loa) * k, (lb - la) * 60.0
        px, py = (lon - loa) * k, (lat - la) * 60.0
        seg2 = bx * bx + by * by
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg2))
        cx, cy = ax + t * bx, ay + t * by
        d = math.hypot(px - cx, py - cy)
        if best is None or d < best[0]:
            brg = (math.degrees(math.atan2(bx, by)) + 360) % 360
            best = (d, i, t, brg)
    d, i, t, brg = best
    return {"s": _CUM[i] + t * _SEG_LEN[i], "lateral_nm": d, "seg": i, "bearing": brg}


_PIN = None


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"missing {CONFIG_PATH.name}; copy config.example.json and fill in the pin")
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("pin") or cfg["pin"].get("lat") is None:
        raise SystemExit("config.local.json has no pin")
    return cfg


def pin_s():
    global _PIN
    if _PIN is None:
        cfg = load_config()
        p = project_on_channel(float(cfg["pin"]["lat"]), float(cfg["pin"]["lon"]))
        _PIN = p["s"]
    return _PIN


def along_to_pin(lat, lon):
    """Signed along-channel distance (nm) from a position to the pin.

    Positive = the pin is still ahead for a NORTHBOUND (upriver) ship, i.e. the
    position is south/downriver of the pin. Negative = upriver of the pin.
    Uses projection on the nearest polyline segment (not a latitude test).
    """
    return pin_s() - project_on_channel(lat, lon)["s"]


def avg_sog(readings, was_moving=None):
    """Average recent SOG readings (list of floats, oldest first).

    Once the ship has been moving at MOVING_KN+ (was_moving True, or any
    reading >= MOVING_KN when was_moving is None), readings under UNDERWAY_KN
    are ignored so anchor/drift zeros don't skew the ETA. If every reading is
    slow, the plain mean is returned (she really is stopped).
    """
    vals = [float(v) for v in readings if v is not None and 0 <= float(v) < 102.2]
    if not vals:
        return None
    if was_moving is None:
        was_moving = any(v >= MOVING_KN for v in vals)
    if was_moving:
        fast = [v for v in vals if v >= UNDERWAY_KN]
        if fast:
            return sum(fast) / len(fast)
    return sum(vals) / len(vals)


def ang_diff(a, b):
    return (a - b + 180) % 360 - 180


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_et():
    return datetime.now(ET)


def iso_et(ts=None):
    dt = datetime.fromtimestamp(ts, ET) if ts is not None else now_et()
    return dt.isoformat(timespec="seconds")


def hhmm_et(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def atomic_write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def append_jsonl(path: Path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def read_env_file():
    out = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            out[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def webhook_config():
    """(url, key) from env DELAWARE_WEBHOOK_URL / DELAWARE_WEBHOOK_KEY, then .env.local."""
    filev = read_env_file()
    url = os.environ.get("DELAWARE_WEBHOOK_URL") or filev.get("DELAWARE_WEBHOOK_URL")
    key = os.environ.get("DELAWARE_WEBHOOK_KEY") or filev.get("DELAWARE_WEBHOOK_KEY")
    return url, key


def aisstream_key():
    return os.environ.get("AISSTREAM_API_KEY") or read_env_file().get("AISSTREAM_API_KEY")


def is_qualifying(static):
    """Big commercial ship test from ShipStaticData-derived dict."""
    if not static:
        return False
    t = static.get("type_code")
    length = static.get("length") or 0
    name = (static.get("name") or "").upper()
    if t in EXCLUDED_TYPES:
        return False
    if any(w in name for w in EXCLUDED_NAME_WORDS):
        return False
    if t is not None and 70 <= t <= 89:
        return length >= CARGO_MIN_LENGTH_M
    if t is not None and 60 <= t <= 69:
        return length >= PASSENGER_MIN_LENGTH_M
    return length >= MIN_LENGTH_M


def norm_dest(dest):
    return "".join(ch for ch in (dest or "").upper() if ch.isalnum())


# ---------------------------------------------------------------------------
# Webhook sender
# ---------------------------------------------------------------------------
class Sender:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.t = threading.Thread(target=self._run, name="sender", daemon=True)
        self.t.start()

    def submit(self, payload):
        append_jsonl(EVENTS_PATH, payload)
        self.q.put(payload)

    def _run(self):
        while True:
            payload = self.q.get()
            try:
                send_now(payload)
            except Exception:  # never let the sender die
                log.exception("sender crashed on %s", payload.get("event"))


def send_now(payload, attempts=3):
    """POST one payload. Returns (ok, detail). Never raises for network errors."""
    import requests

    url, key = webhook_config()
    if not url or not key:
        append_jsonl(PENDING_PATH, {**payload, "_pending_reason": "webhook env not set"})
        log.info("event %s %s queued locally (webhook not configured)", payload.get("event"), payload.get("name"))
        return False, "webhook not configured; written to pending-events.jsonl"
    err = None
    for i in range(attempts):
        try:
            r = requests.post(url, json=payload, timeout=15,
                              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                       "User-Agent": "river-rubber-necker/1.0"})
            if 200 <= r.status_code < 300:
                log.info("event %s %s sent (HTTP %s)", payload.get("event"), payload.get("name"), r.status_code)
                return True, f"HTTP {r.status_code}"
            err = f"HTTP {r.status_code}"
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                break
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        time.sleep([5, 20, 0][min(i, 2)])
    append_jsonl(PENDING_PATH, {**payload, "_pending_reason": f"send failed: {err}"})
    log.warning("event %s %s NOT sent (%s); saved to pending-events.jsonl", payload.get("event"),
                payload.get("name"), err)
    return False, err


# ---------------------------------------------------------------------------
# Watcher core
# ---------------------------------------------------------------------------
class Watcher:
    def __init__(self, sender: Sender | None):
        self.sender = sender
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.last_msg = 0.0
        self.msgs_total = 0
        self.connected = False
        self.ws = None
        self.stop = threading.Event()
        self.fail_times: list[float] = []
        self.dirty = False
        pin_s()  # validate config early

    # ---- persistence ----
    def _load_state(self):
        if STATE_PATH.exists():
            try:
                st = json.loads(STATE_PATH.read_text())
                st.setdefault("ships", {})
                st.setdefault("static", {})
                st.setdefault("meta", {})
                return st
            except Exception:
                log.exception("state.json unreadable; starting fresh (old copy kept)")
                STATE_PATH.rename(STATE_PATH.with_suffix(f".bad-{int(time.time())}"))
        st = {"ships": {}, "static": {}, "meta": {"created": iso_et()}}
        self._seed_static(st)
        return st

    def _seed_static(self, st):
        """Seed known ship dimensions/types from the old Sep-2026 poll dumps, if present."""
        import glob
        n = 0
        for f in sorted(glob.glob(str(HERE.parent / "*dump*.json"))):
            try:
                d = json.loads(Path(f).read_text())
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            for key in ("ships", "all_near", "interesting"):
                for s in d.get(key) or []:
                    if isinstance(s, dict) and s.get("mmsi") and s.get("loa_m"):
                        st["static"][str(s["mmsi"])] = {
                            "name": s.get("name"), "length": s.get("loa_m"), "type_code": s.get("ship_type"),
                            "imo": s.get("imo") or None, "destination": s.get("dest"), "callsign": s.get("callsign"),
                            "seeded": True}
                        n += 1
        if n:
            log.info("seeded static data for %d ships from old dumps", len(st["static"]))

    def save(self, force=False):
        with self.lock:
            if not (self.dirty or force):
                return
            self.state["meta"]["saved"] = iso_et()
            atomic_write_json(STATE_PATH, self.state)
            self.dirty = False

    # ---- messages ----
    def handle_message(self, raw: str, now: float | None = None):
        now = now or time.time()
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if "error" in msg and "MessageType" not in msg:
            log.error("AISStream error message: %s", str(msg.get("error"))[:200])
            return
        self.last_msg = now
        self.msgs_total += 1
        mtype = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = (msg.get("Message") or {}).get(mtype) or {}
        mmsi = str(meta.get("MMSI") or body.get("UserID") or "")
        if not mmsi:
            return
        with self.lock:
            if mtype == "ShipStaticData":
                self._on_static(mmsi, meta, body)
            elif mtype in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
                self._on_position(mmsi, meta, body, now)

    def _on_static(self, mmsi, meta, b):
        dim = b.get("Dimension") or {}
        a, bb = int(dim.get("A") or 0), int(dim.get("B") or 0)
        length = a + bb if a + bb > 0 else None
        name = (b.get("Name") or meta.get("ShipName") or "").strip() or None
        st = self.state["static"].get(mmsi, {})
        st.update({
            "name": name or st.get("name"),
            "length": length or st.get("length"),
            "type_code": b.get("Type") if b.get("Type") is not None else st.get("type_code"),
            "imo": b.get("ImoNumber") or st.get("imo") or None,
            "callsign": (b.get("CallSign") or "").strip() or st.get("callsign"),
            "destination": (b.get("Destination") or "").strip() or st.get("destination"),
            "draught": b.get("MaximumStaticDraught") or st.get("draught"),
            "updated": iso_et(),
        })
        st.pop("seeded", None)
        self.state["static"][mmsi] = st
        self.dirty = True

    def _on_position(self, mmsi, meta, b, now):
        lat = b.get("Latitude", meta.get("latitude"))
        lon = b.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return
        sog = b.get("Sog")
        sog = None if sog is None or sog >= 102.2 else float(sog)
        cog = b.get("Cog")
        cog = None if cog is None or cog >= 360 else float(cog)
        ships = self.state["ships"]
        sh = ships.get(mmsi)
        if sh is None:
            sh = ships[mmsi] = {"first_seen": now, "fixes": [], "transit": None, "history": []}
        name = (meta.get("ShipName") or "").strip()
        if name:
            sh["name"] = name
        proj = project_on_channel(lat, lon)
        along = pin_s() - proj["s"]
        fix = {"t": now, "lat": round(lat, 6), "lon": round(lon, 6), "sog": sog, "cog": cog,
               "nav": b.get("NavigationalStatus"), "along": round(along, 3), "lat_nm": round(proj["lateral_nm"], 3),
               "brg": round(proj["bearing"], 1)}
        sh["fixes"] = [f for f in sh["fixes"] if now - f["t"] <= 3600][-60:] + [fix]
        sh["last"] = fix
        sh["last_t"] = now
        if sog is not None and sog >= MOVING_KN:
            sh["was_moving_t"] = now
        self.dirty = True
        static = self.state["static"].get(mmsi)
        if not is_qualifying(static):
            return
        if proj["lateral_nm"] > MAX_LATERAL_NM:
            return
        self._evaluate(mmsi, sh, static, fix, now)

    # ---- derived ----
    def direction(self, sh, fix):
        """+1 northbound (upriver), -1 southbound, 0 unknown."""
        fixes = sh["fixes"]
        old = [f for f in fixes if fix["t"] - f["t"] >= 240 and fix["t"] - f["t"] <= 1200]
        if old:
            ds = old[-1]["along"] - fix["along"]  # +ve = moved upriver
            if abs(ds) >= 0.15:
                return 1 if ds > 0 else -1
        if fix["sog"] is not None and fix["sog"] >= 1.0 and fix["cog"] is not None:
            c = math.cos(math.radians(ang_diff(fix["cog"], fix["brg"])))
            if c > 0.3:
                return 1
            if c < -0.3:
                return -1
        return 0

    def ship_avg_sog(self, sh, now):
        recent = [f["sog"] for f in sh["fixes"] if now - f["t"] <= SOG_WINDOW_S and f["sog"] is not None]
        recent = recent[-SOG_WINDOW_N:]
        was_moving = bool(sh.get("was_moving_t")) and now - sh["was_moving_t"] <= 2 * 3600
        return avg_sog(recent, was_moving)

    def eta(self, sh, fix, now):
        a = self.ship_avg_sog(sh, now)
        dist = abs(fix["along"])
        if a is None or a < 0.5:
            return None, None, a
        minutes = dist / a * 60.0
        return minutes, now + minutes * 60.0, a

    def is_stopped(self, sh, now):
        slow = []
        for f in reversed(sh["fixes"]):
            if f["sog"] is None:
                continue
            if f["sog"] < STOP_KN:
                slow.append(f)
            else:
                break
        return len(slow) >= STOP_MIN_FIXES and (slow[0]["t"] - slow[-1]["t"]) >= STOP_MIN_SECONDS

    # ---- event logic ----
    def _evaluate(self, mmsi, sh, static, fix, now):
        d = self.direction(sh, fix)
        along = fix["along"]
        tr = sh.get("transit")
        sog = fix["sog"] or 0.0
        toward = (d == 1 and along > 0) or (d == -1 and along < 0)

        if tr and tr.get("closed"):
            # allow a fresh transit later (e.g. she comes back out from Fairless)
            if now - tr.get("closed_t", now) >= NEW_TRANSIT_AFTER_PASS_S and toward and sog >= UNDERWAY_KN:
                sh["history"] = (sh.get("history") or [])[-5:] + [tr]
                tr = sh["transit"] = None
            else:
                return

        if tr is None:
            if not toward or sog < UNDERWAY_KN:
                return
            a = self.ship_avg_sog(sh, now)
            if a is None or a < UNDERWAY_KN:
                return
            tr = sh["transit"] = {"dir": d, "start_t": now, "sent": {}, "notes": []}
            minutes, eta_t, _ = self.eta(sh, fix, now)
            notes = []
            if minutes is not None and minutes <= T60_MIN:
                tr["sent"]["t60"] = "covered_by_new_ship"
                notes.append("already inside 60 min")
            if minutes is not None and minutes <= T30_MIN:
                tr["sent"]["t30"] = "covered_by_new_ship"
                notes.append("already inside 30 min")
            self.fire("new_ship", mmsi, sh, static, fix, now, extra_notes=notes)
            tr["ref_eta"] = eta_t
            tr["ref_eta_t"] = now
            return

        d_tr = tr["dir"]
        # ----- passed -----
        prev_along = tr.get("last_along")
        tr["last_along"] = along
        crossed = (d_tr == 1 and along <= 0) or (d_tr == -1 and along >= 0)
        if crossed:
            gap = now - tr.get("last_fix_t", now)
            note = ["inferred across a signal gap"] if gap > 15 * 60 else []
            if prev_along is None:
                note.append("first fix of this transit already past the pin")
            self.fire("passed", mmsi, sh, static, fix, now, extra_notes=note, minutes_override=0)
            tr["closed"] = "passed"
            tr["closed_t"] = now
            return
        tr["last_fix_t"] = now
        if tr.get("lost_signal_t") and not tr.get("signal_back"):
            tr["signal_back"] = now
            log.info("%s signal back after gap", sh.get("name"))

        # ----- turned away / berthed / stopped -----
        moving_away = d == -d_tr and sog >= UNDERWAY_KN
        if moving_away:
            tr.setdefault("away_since", now)
            tr.setdefault("away_along", along)
            if now - tr["away_since"] >= TURNED_AWAY_S and abs(along - tr["away_along"]) >= TURNED_AWAY_NM:
                log.info("%s turned away from the pin; closing transit quietly", sh.get("name"))
                tr["closed"] = "turned_away"
                tr["closed_t"] = now
                return
        else:
            tr.pop("away_since", None)
            tr.pop("away_along", None)

        stopped = self.is_stopped(sh, now)
        if stopped:
            tr.setdefault("stopped_since", now)
            if "stopped_short" not in tr["sent"]:
                self.fire("stopped_short", mmsi, sh, static, fix, now)
            tr["stopped"] = True
            if now - tr["stopped_since"] >= BERTHED_CLOSE_S:
                tr["closed"] = "berthed"
                tr["closed_t"] = now
            return
        resumed_note = []
        if tr.get("stopped"):
            if sog >= UNDERWAY_KN and d == d_tr:
                tr["stopped"] = False
                tr.pop("stopped_since", None)
                tr["resumed_t"] = now
                resumed_note = ["under way again after stopping short"]
                tr["ref_eta_t"] = 0  # let eta_shift fire right away
            else:
                return

        minutes, eta_t, _ = self.eta(sh, fix, now)
        if minutes is None:
            return
        if minutes <= T30_MIN and "t30" not in tr["sent"]:
            if "t60" not in tr["sent"]:
                tr["sent"]["t60"] = "skipped_jumped_to_t30"
            self.fire("t30", mmsi, sh, static, fix, now, extra_notes=resumed_note)
            tr["ref_eta"], tr["ref_eta_t"] = eta_t, now
            return
        if minutes <= T60_MIN and "t60" not in tr["sent"]:
            self.fire("t60", mmsi, sh, static, fix, now, extra_notes=resumed_note)
            tr["ref_eta"], tr["ref_eta_t"] = eta_t, now
            return
        if "t60" in tr["sent"] and tr.get("ref_eta"):
            shift = (eta_t - tr["ref_eta"]) / 60.0
            if abs(shift) >= ETA_SHIFT_MIN and now - tr.get("ref_eta_t", 0) >= ETA_SHIFT_COOLDOWN_S:
                n = [f"ETA {'later' if shift > 0 else 'earlier'} by {abs(shift):.0f} min "
                     f"(was {hhmm_et(tr['ref_eta'])})"] + resumed_note
                self.fire("eta_shift", mmsi, sh, static, fix, now, extra_notes=n, repeatable=True)
                tr["ref_eta"], tr["ref_eta_t"] = eta_t, now

    def periodic(self, now=None):
        """Lost-signal, stale and forget sweeps. Call every ~30-60s."""
        now = now or time.time()
        with self.lock:
            for mmsi, sh in list(self.state["ships"].items()):
                age = now - sh.get("last_t", now)
                tr = sh.get("transit")
                if tr and not tr.get("closed"):
                    if (age >= LOST_SIGNAL_S and "lost_signal" not in tr["sent"] and not tr.get("stopped")):
                        static = self.state["static"].get(mmsi) or {}
                        tr["lost_signal_t"] = now
                        self.fire("lost_signal", mmsi, sh, static, sh["last"], now,
                                  extra_notes=[f"no AIS for {age/60:.0f} min; last fix {hhmm_et(sh['last_t'])} ET; "
                                               "keeping last-known ETA"],
                                  use_fix_time=True)
                    if age >= STALE_CLOSE_S:
                        tr["closed"] = "stale"
                        tr["closed_t"] = now
                        self.dirty = True
                if age >= FORGET_SHIP_S:
                    del self.state["ships"][mmsi]
                    self.dirty = True

    # ---- payload ----
    def build_payload(self, event, mmsi, sh, static, fix, now, notes, minutes_override="calc", use_fix_time=False):
        minutes, eta_t = None, None
        if minutes_override == "calc":
            minutes, eta_t, _ = self.eta(sh, fix, fix["t"] if use_fix_time else now)
            if use_fix_time and eta_t:  # last-known ETA, minutes counted from now
                minutes = max(0.0, (eta_t - now) / 60.0)
        elif minutes_override == 0:
            minutes, eta_t = 0, fix["t"]
        tr = sh.get("transit") or {}
        d = tr.get("dir") or self.direction(sh, fix)
        all_notes = list(notes)
        dest = (static or {}).get("destination") or ""
        if event in ("new_ship", "t60", "t30") and d == 1 and dest:
            nd = norm_dest(dest.split(">")[-1])
            if any(nd.startswith(x) for x in SOUTH_OF_PIN_DEST):
                all_notes.append(f"destination {dest} - may stop at a berth before the pin")
        if eta_t and event != "passed":
            h = datetime.fromtimestamp(eta_t, ET).hour
            if not (ALERT_WINDOW[0] <= h < ALERT_WINDOW[1]):
                all_notes.append("pass ETA is outside the 05:00-20:00 ET window")
        return {
            "event": event,
            "ts": iso_et(now),
            "mmsi": int(mmsi) if str(mmsi).isdigit() else mmsi,
            "imo": (static or {}).get("imo"),
            "name": (static or {}).get("name") or sh.get("name"),
            "callsign": (static or {}).get("callsign"),
            "length": (static or {}).get("length"),
            "type_code": (static or {}).get("type_code"),
            "destination": dest or None,
            "lat": fix["lat"],
            "lon": fix["lon"],
            "sog": fix["sog"],
            "cog": fix["cog"],
            "eta_et": hhmm_et(eta_t) if eta_t else None,
            "minutes_out": round(minutes) if minutes is not None else None,
            "direction": "northbound" if d == 1 else "southbound" if d == -1 else "unknown",
            "dist_nm": round(abs(fix["along"]), 2),
            "notes": "; ".join(all_notes),
        }

    def fire(self, event, mmsi, sh, static, fix, now, extra_notes=(), minutes_override="calc",
             repeatable=False, use_fix_time=False):
        tr = sh.get("transit")
        if tr is not None and not repeatable:
            if event in tr["sent"]:
                return
            tr["sent"][event] = iso_et(now)
        elif tr is not None:
            tr["sent"].setdefault(event + "_count", 0)
            tr["sent"][event + "_count"] += 1
        payload = self.build_payload(event, mmsi, sh, static, fix, now, extra_notes, minutes_override, use_fix_time)
        log.info("EVENT %s %s dir=%s dist=%.2fnm eta=%s min=%s", event, payload["name"], payload["direction"],
                 payload["dist_nm"], payload["eta_et"], payload["minutes_out"])
        self.dirty = True
        self.save(force=True)  # persist BEFORE sending so a crash never repeats
        if self.sender:
            self.sender.submit(payload)

    def fire_watcher_error(self, detail):
        meta = self.state["meta"]
        now = time.time()
        if now - meta.get("last_watcher_error_t", 0) < WATCHER_ERROR_COOLDOWN_S:
            return
        meta["last_watcher_error_t"] = now
        self.dirty = True
        self.save(force=True)
        payload = {"event": "watcher_error", "ts": iso_et(now), "mmsi": None, "imo": None, "name": None,
                   "callsign": None, "length": None, "type_code": None, "destination": None, "lat": None,
                   "lon": None, "sog": None, "cog": None, "eta_et": None, "minutes_out": None, "notes": detail}
        log.error("EVENT watcher_error: %s", detail)
        if self.sender:
            self.sender.submit(payload)

    # ---- heartbeat ----
    def heartbeat(self):
        now = time.time()
        with self.lock:
            tracked, active = [], []
            for mmsi, sh in self.state["ships"].items():
                st = self.state["static"].get(mmsi)
                if not is_qualifying(st) or now - sh.get("last_t", 0) > 2 * 3600:
                    continue
                if sh.get("last", {}).get("lat_nm", 99) > MAX_LATERAL_NM:
                    continue
                nm = st.get("name") or sh.get("name") or mmsi
                tracked.append(nm)
                tr = sh.get("transit")
                if tr and not tr.get("closed"):
                    m, e, _ = self.eta(sh, sh["last"], now)
                    active.append({"name": nm, "minutes_out": round(m) if m is not None else None,
                                   "eta_et": hhmm_et(e) if e else None, "stopped": bool(tr.get("stopped"))})
            url, key = webhook_config()
            hb = {
                "ts": iso_et(now),
                "last_msg_age_s": round(now - self.last_msg, 1) if self.last_msg else None,
                "tracked": sorted(tracked),
                "tracked_count": len(tracked),
                "active_transits": active,
                "vessels_seen": len(self.state["ships"]),
                "msgs_total": self.msgs_total,
                "connected": self.connected,
                "webhook_configured": bool(url and key),
                "pid": os.getpid(),
            }
        atomic_write_json(HEARTBEAT_PATH, hb)

    # ---- websocket ----
    def subscription(self):
        cfg = load_config()
        bbox = cfg.get("bbox")
        if not bbox:
            try:
                bbox = json.loads((HERE.parent / "aisstream-bbox.json").read_text())["BoundingBoxes"]
            except Exception:
                bbox = DEFAULT_BBOX
        return {"APIKey": aisstream_key(), "BoundingBoxes": bbox,
                "FilterMessageTypes": cfg.get("message_types") or ["PositionReport", "ShipStaticData"]}

    def run_ws_forever(self):
        import websocket  # websocket-client

        backoff = 5
        while not self.stop.is_set():
            if not aisstream_key():
                log.error("AISSTREAM_API_KEY not in env; retrying in 60s")
                self._note_fail("AISSTREAM_API_KEY missing")
                self.stop.wait(60)
                continue
            got_msg = {"v": False}

            def on_open(ws):
                self.connected = True
                ws.send(json.dumps(self.subscription()))
                log.info("websocket open; subscribed")

            def on_message(ws, m):
                got_msg["v"] = True
                self.handle_message(m)

            def on_error(ws, e):
                log.warning("websocket error: %s", type(e).__name__ if not isinstance(e, str) else e[:120])

            def on_close(ws, code, reason):
                self.connected = False
                log.info("websocket closed code=%s reason=%s", code, (reason or "")[:120])

            self.ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                             on_error=on_error, on_close=on_close)
            started = time.time()
            try:
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("run_forever raised %s", type(e).__name__)
            self.connected = False
            if self.stop.is_set():
                break
            if got_msg["v"] and time.time() - started > 120:
                backoff = 5
                self.fail_times = []
            else:
                self._note_fail("websocket closed without data")
                backoff = min(backoff * 2, 300)
            log.info("reconnecting in %ss", backoff)
            self.stop.wait(backoff)

    def _note_fail(self, why):
        now = time.time()
        self.fail_times = [t for t in self.fail_times if now - t < 3600] + [now]
        if len(self.fail_times) >= WS_FAILS_FOR_ERROR:
            self.fire_watcher_error(f"AISStream websocket failing repeatedly ({len(self.fail_times)} "
                                    f"failures in the last hour; last: {why})")

    def watchdog(self):
        """Heartbeat + sweeps + silent-socket reconnect."""
        last_periodic = 0
        while not self.stop.is_set():
            try:
                self.heartbeat()
                now = time.time()
                if now - last_periodic >= 60:
                    self.periodic(now)
                    last_periodic = now
                self.save()
                if (self.connected and self.last_msg and now - self.last_msg > WS_SILENT_RECONNECT_S
                        and self.ws is not None):
                    log.warning("no AIS for %ds; forcing reconnect", now - self.last_msg)
                    self.ws.close()
            except Exception:
                log.exception("watchdog error")
            self.stop.wait(HEARTBEAT_S)


def setup_logging(stderr=True):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send one test event to the webhook and exit")
    args = ap.parse_args()
    setup_logging(stderr=not args.test)

    if args.test:
        payload = {"event": "test", "ts": iso_et(), "mmsi": 0, "imo": None, "name": "TEST SHIP",
                   "callsign": None, "length": 200, "type_code": 70, "destination": "USPHL", "lat": None,
                   "lon": None, "sog": 10.0, "cog": 20.0, "eta_et": now_et().strftime("%H:%M"), "minutes_out": 0,
                   "notes": "river-rubber-necker webhook test"}
        ok, detail = send_now(payload, attempts=1)
        print(json.dumps({"ok": ok, "detail": detail}))
        return 0 if ok else 1

    w = Watcher(Sender())

    def _stop(*_):
        log.info("signal received; shutting down")
        w.stop.set()
        if w.ws is not None:
            try:
                w.ws.close()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    url, key = webhook_config()
    log.info("watcher starting pid=%s webhook_configured=%s ships_in_state=%d", os.getpid(), bool(url and key),
             len(w.state["ships"]))
    threading.Thread(target=w.watchdog, name="watchdog", daemon=True).start()
    try:
        w.run_ws_forever()
    finally:
        w.save(force=True)
        log.info("watcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
