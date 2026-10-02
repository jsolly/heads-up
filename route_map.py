#!/workspace/mapenv/bin/python
"""Draw an end-to-end voyage map for a ship the watcher knows about.

    /workspace/mapenv/bin/python route_map.py <MMSI> [--from LOCODE] [--to LOCODE] [--out path]

- last position (and name / IMO / AIS destination) come from state.json
- origin defaults to cargo/<IMO>.json "last_port" if that file exists
- destination defaults to the AIS destination when it maps to a known port
Routes run over a small hand-built sea graph (Delaware channel + ocean legs)
with Dijkstra, so lines stay at sea. It is an estimate, not a filed passage plan.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from watcher import CHANNEL, haversine_nm  # noqa: E402

ET = ZoneInfo("America/New_York")
CAPTION = "route is my estimate from AIS and tracker data, not her filed passage plan"
SPEED_KN = 12.0

# code: (name, lat, lon, aliases)
PORTS = {
    "USPHL":   ("Philadelphia", 39.9200, -75.1370, ["PHILADELPHIA", "PHILADELPIA", "PHILLY", "USPHI"]),
    "USPHLPT": ("Philadelphia (Packer Ave)", 39.9020, -75.1370, ["PACKER", "PACKERAVE"]),
    "USMAH":   ("Marcus Hook", 39.8110, -75.4130, ["MARCUSHOOK", "USMKH"]),
    "USDCI":   ("Delaware City", 39.5750, -75.5800, ["DELAWARECITY"]),
    "USPAU":   ("Paulsboro", 39.8470, -75.2450, ["PAULSBORO"]),
    "USWIL":   ("Wilmington DE", 39.7160, -75.5200, ["USILG", "WILMINGTON", "WILMINGTONDE"]),
    "USCMD":   ("Camden", 39.9330, -75.1280, ["USCDE", "CAMDEN"]),
    "USFAH":   ("Fairless Hills", 40.1600, -74.7560, ["USFAIRLESS", "FAIRLESS", "FAIRLESSHILLS", "MORRISVILLE"]),
    "USGLC":   ("Gloucester City", 39.8930, -75.1270, ["GLOUCESTER", "GLOUCESTERCITY"]),
    "USBUH":   ("Burlington NJ", 40.0790, -74.8600, ["BURLINGTON"]),
    "USBRL":   ("Bristol PA", 40.1100, -74.8400, ["BRISTOL", "BRISTOLUSA"]),
    "USNYC":   ("New York", 40.6600, -74.0500, ["NEWYORK", "NEWARK", "USEWR", "USNWK"]),
    "USBAL":   ("Baltimore", 39.2500, -76.5600, ["BALTIMORE"]),
    "USORF":   ("Norfolk", 36.9200, -76.3300, ["NORFOLK", "USNFK"]),
    "USSAV":   ("Savannah", 32.0800, -81.0900, ["SAVANNAH"]),
    "CAHAL":   ("Halifax", 44.6400, -63.5600, ["HALIFAX"]),
    "CAWHH":   ("Whiffen Head", 47.7700, -54.0000, ["WHIFFENHEAD"]),
    "CASOR":   ("Sorel-Tracy", 46.0500, -73.1200, ["SOREL", "SORELTRACY"]),
    "CAMTR":   ("Montreal", 45.5300, -73.5300, ["MONTREAL"]),
    "NLRTM":   ("Rotterdam", 51.9500, 4.1000, ["ROTTERDAM"]),
    "BEANR":   ("Antwerp", 51.2800, 4.3500, ["ANTWERP", "ANTWERPEN"]),
    "DEHAM":   ("Hamburg", 53.5400, 9.9500, ["HAMBURG"]),
    "DEWVN":   ("Wilhelmshaven", 53.5200, 8.1500, ["WILHELMSHAVEN"]),
    "DKSKA":   ("Skagen", 57.7200, 10.6000, ["SKAGEN", "DKSKAW"]),
    "GTSTC":   ("Santo Tomas de Castilla", 15.7000, -88.6200, ["SANTOTOMAS", "SANTOTOMASDECASTILLA"]),
    "HNPCR":   ("Puerto Cortes", 15.8500, -87.9500, ["PUERTOCORTES"]),
    "BRPEC":   ("Pecem", -3.5400, -38.8100, ["PECEM"]),
    "BRSSZ":   ("Santos", -23.9800, -46.3000, ["SANTOS"]),
    "EGEDK":   ("El Dekheila", 31.1400, 29.8100, ["ELDEKHEILA", "DEKHEILA", "ALEXANDRIA"]),
    "ESALG":   ("Algeciras", 36.1300, -5.4300, ["ALGECIRAS"]),
    "BSFPO":   ("Freeport", 26.5100, -78.7700, ["FREEPORT"]),
    "JMKIN":   ("Kingston", 17.9500, -76.8300, ["KINGSTON"]),
    "COCTG":   ("Cartagena", 10.4000, -75.5300, ["CARTAGENA"]),
}

# Sea nodes (lat, lon). Ocean "region legs" keep lines off land.
SEA = {
    "DEL_OFF": (38.70, -74.70),          # off the Delaware Bay mouth
    "NY_OFF": (40.30, -73.75), "AMBROSE": (40.45, -73.85),
    "CHES_OFF": (36.95, -75.70), "CHES_MOUTH": (36.97, -76.02), "CHES_MID": (37.60, -76.10),
    "CHES_UP": (38.40, -76.35), "CHES_NORTH": (39.00, -76.40),
    "CD_CANAL_E": (39.56, -75.57), "CD_CANAL_W": (39.53, -75.83), "ELK": (39.45, -76.00), "CHES_TOP": (39.20, -76.30),
    "HATTERAS": (34.90, -75.05), "CAPE_FEAR_OFF": (33.20, -77.30), "SAV_OFF": (31.90, -80.60),
    "FL_NE": (30.30, -80.40), "CANAVERAL_OFF": (28.50, -80.20), "FL_E": (27.00, -79.85),
    "GRAND_BAHAMA_W": (26.30, -79.40), "FL_STRAITS": (25.00, -79.75), "KEYS_E": (24.15, -81.50),
    "KEYS_W": (23.70, -83.50), "YUCATAN": (21.90, -85.90), "QROO": (19.50, -86.40), "BELIZE_OFF": (17.20, -86.90),
    "HONDURAS_GULF": (16.30, -87.60), "CORTES_APP": (15.95, -88.10),
    "ATL_SW": (30.00, -72.50), "BAHAMAS_E": (25.00, -72.00), "CAICOS_PASS": (22.00, -72.55),
    "INAGUA_S": (21.20, -72.65), "WINDWARD": (20.05, -73.80), "TIBURON_W": (19.30, -74.60),
    "JAM_E": (17.80, -74.80), "JAM_SE": (17.60, -76.30), "COL_APP": (11.00, -75.80),
    "NANTUCKET_S": (40.00, -69.00), "NS_OFF": (42.50, -64.50), "HFX_APP": (44.35, -63.45),
    "CB_E": (45.30, -59.00), "CABOT": (47.20, -59.90), "HONGUEDO": (49.15, -64.40), "SL_ESTUARY": (49.10, -67.30),
    "RIMOUSKI": (48.65, -68.70), "TADOUSSAC": (48.00, -69.65), "SL_ISLE": (47.40, -70.40),
    "SL_ORLEANS": (46.95, -70.90), "QUEBEC": (46.80, -71.20), "SL_PORTNEUF": (46.60, -71.90),
    "TROIS_RIV": (46.35, -72.50),
    "GRAND_BANKS_S": (44.00, -52.00), "PLACENTIA": (46.40, -54.30),
    "ATL_N1": (44.50, -45.00), "ATL_N2": (48.50, -20.00), "CHANNEL_W": (49.20, -6.00), "DOVER": (50.95, 1.40),
    "NSEA_S": (51.60, 2.60), "RTM_APP": (52.00, 3.80), "SCHELDT_APP": (51.42, 3.40), "GERMAN_BIGHT": (54.00, 7.20),
    "ELBE_APP": (54.00, 8.30), "JADE_APP": (53.90, 7.90), "JUTLAND_W": (56.50, 7.30), "SKAG_W": (57.90, 9.50),
    "AZORES_S": (37.00, -40.00), "ST_VINCENT": (36.70, -10.00), "GIBRALTAR": (35.95, -5.70),
    "ALBORAN": (36.10, -2.00), "ALGERIA_OFF": (37.60, 5.00), "SARDINIA_S": (37.90, 9.50), "SICILY_CH": (37.40, 11.60),
    "IONIAN": (35.80, 15.50), "LIBYA_OFF": (33.80, 22.00), "DEK_APP": (31.40, 29.60),
    "ANTILLES_N": (21.00, -58.00), "ATL_EQ_W": (8.00, -45.00), "BR_NORTH": (0.00, -40.00), "PECEM_APP": (-3.30, -38.75),
    "EQ_E": (1.00, -35.50), "ROQUE": (-5.00, -34.40), "RECIFE_OFF": (-8.50, -34.30), "SALVADOR_OFF": (-13.00, -37.50),
    "ABROLHOS_E": (-18.00, -37.80), "SAO_TOME_E": (-22.00, -40.00), "CABO_FRIO_S": (-23.30, -41.80),
    "SANTOS_APP": (-24.30, -45.00), "FREEPORT_APP": (26.40, -78.80),
}

SEA_EDGES = [
    ("DEL_OFF", "NY_OFF"), ("NY_OFF", "AMBROSE"), ("DEL_OFF", "CHES_OFF"), ("CHES_OFF", "CHES_MOUTH"),
    ("CHES_MOUTH", "CHES_MID"), ("CHES_MID", "CHES_UP"), ("CHES_UP", "CHES_NORTH"), ("CHES_NORTH", "CHES_TOP"),
    ("CD_CANAL_E", "CD_CANAL_W"), ("CD_CANAL_W", "ELK"), ("ELK", "CHES_TOP"),
    ("DEL_OFF", "HATTERAS"), ("CHES_OFF", "HATTERAS"), ("HATTERAS", "CAPE_FEAR_OFF"), ("CAPE_FEAR_OFF", "SAV_OFF"),
    ("SAV_OFF", "FL_NE"), ("FL_NE", "CANAVERAL_OFF"), ("CANAVERAL_OFF", "FL_E"), ("FL_E", "GRAND_BAHAMA_W"),
    ("GRAND_BAHAMA_W", "FREEPORT_APP"), ("GRAND_BAHAMA_W", "FL_STRAITS"), ("FL_STRAITS", "KEYS_E"),
    ("KEYS_E", "KEYS_W"), ("KEYS_W", "YUCATAN"), ("YUCATAN", "QROO"), ("QROO", "BELIZE_OFF"),
    ("BELIZE_OFF", "HONDURAS_GULF"), ("HONDURAS_GULF", "CORTES_APP"),
    ("HATTERAS", "ATL_SW"), ("DEL_OFF", "ATL_SW"), ("ATL_SW", "BAHAMAS_E"), ("BAHAMAS_E", "CAICOS_PASS"),
    ("CAICOS_PASS", "INAGUA_S"), ("INAGUA_S", "WINDWARD"), ("WINDWARD", "TIBURON_W"), ("TIBURON_W", "JAM_E"),
    ("JAM_E", "JAM_SE"), ("JAM_E", "COL_APP"), ("JAM_SE", "BELIZE_OFF"),
    ("DEL_OFF", "NANTUCKET_S"), ("NY_OFF", "NANTUCKET_S"), ("NANTUCKET_S", "NS_OFF"), ("NS_OFF", "HFX_APP"),
    ("NS_OFF", "CB_E"), ("CB_E", "CABOT"), ("CABOT", "HONGUEDO"), ("HONGUEDO", "SL_ESTUARY"),
    ("SL_ESTUARY", "RIMOUSKI"), ("RIMOUSKI", "TADOUSSAC"), ("TADOUSSAC", "SL_ISLE"), ("SL_ISLE", "SL_ORLEANS"),
    ("SL_ORLEANS", "QUEBEC"), ("QUEBEC", "SL_PORTNEUF"), ("SL_PORTNEUF", "TROIS_RIV"),
    ("CB_E", "PLACENTIA"), ("CB_E", "GRAND_BANKS_S"), ("NS_OFF", "GRAND_BANKS_S"), ("GRAND_BANKS_S", "PLACENTIA"),
    ("GRAND_BANKS_S", "ATL_N1"), ("NANTUCKET_S", "ATL_N1"), ("ATL_N1", "ATL_N2"), ("ATL_N2", "CHANNEL_W"),
    ("CHANNEL_W", "DOVER"), ("DOVER", "NSEA_S"), ("NSEA_S", "SCHELDT_APP"), ("NSEA_S", "RTM_APP"),
    ("RTM_APP", "GERMAN_BIGHT"), ("GERMAN_BIGHT", "ELBE_APP"), ("GERMAN_BIGHT", "JADE_APP"),
    ("GERMAN_BIGHT", "JUTLAND_W"), ("JUTLAND_W", "SKAG_W"),
    ("DEL_OFF", "AZORES_S"), ("HATTERAS", "AZORES_S"), ("AZORES_S", "ST_VINCENT"), ("ATL_N2", "ST_VINCENT"),
    ("ST_VINCENT", "GIBRALTAR"), ("GIBRALTAR", "ALBORAN"), ("ALBORAN", "ALGERIA_OFF"), ("ALGERIA_OFF", "SARDINIA_S"),
    ("SARDINIA_S", "SICILY_CH"), ("SICILY_CH", "IONIAN"), ("IONIAN", "LIBYA_OFF"), ("LIBYA_OFF", "DEK_APP"),
    ("ATL_SW", "ANTILLES_N"), ("HATTERAS", "ANTILLES_N"), ("ANTILLES_N", "ATL_EQ_W"), ("ATL_EQ_W", "BR_NORTH"),
    ("BR_NORTH", "PECEM_APP"), ("ATL_EQ_W", "EQ_E"), ("EQ_E", "ROQUE"), ("PECEM_APP", "ROQUE"),
    ("ROQUE", "RECIFE_OFF"), ("RECIFE_OFF", "SALVADOR_OFF"), ("SALVADOR_OFF", "ABROLHOS_E"),
    ("ABROLHOS_E", "SAO_TOME_E"), ("SAO_TOME_E", "CABO_FRIO_S"), ("CABO_FRIO_S", "SANTOS_APP"),
]

# Port -> graph attachment
PORT_LINKS = {
    "USNYC": ["AMBROSE"], "USBAL": ["CHES_TOP"], "USORF": ["CHES_MOUTH"], "USSAV": ["SAV_OFF"],
    "CAHAL": ["HFX_APP"], "CAWHH": ["PLACENTIA"], "CASOR": ["TROIS_RIV"], "CAMTR": [],
    "NLRTM": ["RTM_APP"], "BEANR": ["SCHELDT_APP"], "DEHAM": ["ELBE_APP"], "DEWVN": ["JADE_APP"],
    "DKSKA": ["SKAG_W"], "GTSTC": ["CORTES_APP"], "HNPCR": ["CORTES_APP"], "BRPEC": ["PECEM_APP"],
    "BRSSZ": ["SANTOS_APP"], "EGEDK": ["DEK_APP"], "ESALG": ["GIBRALTAR", "ALBORAN"], "BSFPO": ["FREEPORT_APP"],
    "JMKIN": ["JAM_SE"], "COCTG": ["COL_APP"],
}
EXTRA_PORT_EDGES = [("CAMTR", "CASOR")]


def build_graph():
    nodes = {}
    edges = {}

    def add_edge(a, b):
        d = haversine_nm(*nodes[a], *nodes[b])
        edges.setdefault(a, []).append((b, d))
        edges.setdefault(b, []).append((a, d))

    for i, (la, lo, _) in enumerate(CHANNEL):
        nodes[f"CH{i}"] = (la, lo)
        if i:
            add_edge(f"CH{i-1}", f"CH{i}")
    nodes.update(SEA)
    for code, (_, la, lo, _) in PORTS.items():
        nodes[code] = (la, lo)
    add_edge("CH0", "DEL_OFF")
    for a, b in SEA_EDGES:
        add_edge(a, b)
    for code, links in PORT_LINKS.items():
        for l in links:
            add_edge(code, l)
    for a, b in EXTRA_PORT_EDGES:
        add_edge(a, b)
    # Delaware River ports attach to their nearest channel waypoint
    for code, (_, la, lo, _) in PORTS.items():
        if code in PORT_LINKS or code in ("CAMTR",):
            continue
        i = min(range(len(CHANNEL)), key=lambda k: haversine_nm(la, lo, CHANNEL[k][0], CHANNEL[k][1]))
        add_edge(code, f"CH{i}")
    # C&D canal joins the channel near Delaware City
    i = min(range(len(CHANNEL)), key=lambda k: haversine_nm(*SEA["CD_CANAL_E"], CHANNEL[k][0], CHANNEL[k][1]))
    add_edge("CD_CANAL_E", f"CH{i}")
    return nodes, edges


NODES, EDGES = build_graph()


def dijkstra(a, b):
    dist, prev, pq = {a: 0.0}, {}, [(0.0, a)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == b:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w in EDGES.get(u, []):
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if b not in dist:
        return None
    path = [b]
    while path[-1] != a:
        path.append(prev[path[-1]])
    return list(reversed(path))


def resolve_port(s):
    if not s:
        return None
    if isinstance(s, dict):
        s = s.get("locode") or s.get("code") or s.get("name")
    raw = str(s).upper()
    # AIS destinations like "US BUH > CA ACO", "USNYC>>USPHL", "GTSTC-USGLC": take the last leg
    parts = [p for p in re.split(r">+|-|/|,", raw) if p.strip()]
    for cand in ([parts[-1]] if parts else []) + [raw]:
        n = re.sub(r"[^A-Z0-9]", "", cand)
        if n in PORTS:
            return n
        for code, (name, _, _, aliases) in PORTS.items():
            if n == re.sub(r"[^A-Z0-9]", "", name.upper()) or n in aliases:
                return code
        for code, (name, _, _, aliases) in PORTS.items():
            if any(len(a) >= 5 and n.startswith(a) for a in aliases):
                return code
    return None


def nearest_node(lat, lon):
    return min(NODES, key=lambda k: haversine_nm(lat, lon, *NODES[k]))


def path_coords(path):
    return [NODES[n] for n in path]


def polyline_len(pts):
    return sum(haversine_nm(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


def split_at(pts, lat, lon):
    """Split a polyline at the point nearest (lat, lon). Returns (sailed, ahead) incl. the now point."""
    best = None
    for i in range(len(pts) - 1):
        (la, lo), (lb, lob) = pts[i], pts[i + 1]
        k = math.cos(math.radians((la + lb) / 2))
        bx, by = (lob - lo) * k, lb - la
        px, py = (lon - lo) * k, lat - la
        s2 = bx * bx + by * by
        t = 0 if s2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / s2))
        d = math.hypot(px - t * bx, py - t * by)
        if best is None or d < best[0]:
            best = (d, i)
    _, i = best
    return pts[: i + 1] + [(lat, lon)], [(lat, lon)] + pts[i + 1:]


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "ship").lower()).strip("-") or "ship"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mmsi")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--out")
    ap.add_argument("--state", default=str(HERE / "state.json"))
    args = ap.parse_args()

    state = json.loads(Path(args.state).read_text()) if Path(args.state).exists() else {"ships": {}, "static": {}}
    ship = state.get("ships", {}).get(str(args.mmsi), {})
    static = state.get("static", {}).get(str(args.mmsi), {})
    name = static.get("name") or ship.get("name") or f"MMSI {args.mmsi}"
    imo = static.get("imo")
    last = ship.get("last")

    last_port = None
    if imo and (HERE / "cargo" / f"{imo}.json").exists():
        try:
            last_port = json.loads((HERE / "cargo" / f"{imo}.json").read_text()).get("last_port")
        except Exception:
            pass
    frm = resolve_port(args.frm) if args.frm else resolve_port(last_port)
    to = resolve_port(args.to) if args.to else resolve_port(static.get("destination"))
    if args.frm and not frm:
        print(f"warning: unknown --from {args.frm!r}", file=sys.stderr)
    if args.to and not to:
        print(f"warning: unknown --to {args.to!r}", file=sys.stderr)

    sailed, ahead = [], []
    if frm and to:
        p = dijkstra(frm, to)
        pts = path_coords(p)
        if last:
            sailed, ahead = split_at(pts, last["lat"], last["lon"])
        else:
            ahead = pts
    elif frm and last:
        sailed = path_coords(dijkstra(frm, nearest_node(last["lat"], last["lon"]))) + [(last["lat"], last["lon"])]
    elif to and last:
        ahead = [(last["lat"], last["lon"])] + path_coords(dijkstra(nearest_node(last["lat"], last["lon"]), to))
    elif to and frm is None and not last:
        print("no position and no origin; nothing to draw", file=sys.stderr)

    total = polyline_len(sailed) + polyline_len(ahead)
    remaining = polyline_len(ahead)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    allpts = sailed + ahead + ([(last["lat"], last["lon"])] if last else [])
    if not allpts:
        print("nothing to draw (no position, no known ports)", file=sys.stderr)
        return 2
    lats = [p[0] for p in allpts]
    lons = [p[1] for p in allpts]
    pad = max(1.0, 0.12 * max(max(lats) - min(lats), max(lons) - min(lons)))
    extent = [min(lons) - pad, max(lons) + pad, min(lats) - pad, max(lats) + pad]
    if extent[1] - extent[0] < 3:
        c = (extent[0] + extent[1]) / 2
        extent[0], extent[1] = c - 1.5, c + 1.5
    if extent[3] - extent[2] < 2:
        c = (extent[2] + extent[3]) / 2
        extent[2], extent[3] = c - 1, c + 1

    lon_span, lat_span = extent[1] - extent[0], extent[3] - extent[2]
    h = max(4.5, min(11.0, 11.0 * lat_span / lon_span))
    fig = plt.figure(figsize=(11.0, h + 1.2), dpi=130)
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_extent(extent, crs=ccrs.PlateCarree())
    res = "10m" if (extent[1] - extent[0]) < 6 else "50m"
    ax.add_feature(cfeature.OCEAN.with_scale(res), facecolor="#dceefb")
    ax.add_feature(cfeature.LAND.with_scale(res), facecolor="#f2efe6")
    ax.add_feature(cfeature.COASTLINE.with_scale(res), linewidth=0.5, edgecolor="#6b6b6b")
    ax.add_feature(cfeature.BORDERS.with_scale(res), linewidth=0.3, edgecolor="#9a9a9a")
    if res == "10m":
        ax.add_feature(cfeature.RIVERS.with_scale(res), linewidth=0.6, edgecolor="#9cc7e8")
    gl = ax.gridlines(draw_labels=True, linewidth=0.2, color="gray", alpha=0.5)
    gl.top_labels = gl.right_labels = False

    tr = ccrs.Geodetic()
    if sailed:
        ax.plot([p[1] for p in sailed], [p[0] for p in sailed], "-", color="#0b4f8a", lw=2.2, transform=tr,
                label="sailed")
    if ahead:
        ax.plot([p[1] for p in ahead], [p[0] for p in ahead], "--", color="#d9480f", lw=2.0, transform=tr,
                label="ahead")
    for code in [c for c in (frm, to) if c]:
        _, la, lo, _ = PORTS[code]
        ax.plot(lo, la, "s", color="black", ms=6, transform=ccrs.PlateCarree())
        ax.text(lo, la, f"  {PORTS[code][0]} ({code})", fontsize=9, transform=ccrs.PlateCarree(),
                va="center")
    if last:
        t = datetime.fromtimestamp(last["t"], ET).strftime("%a %b %d %H:%M ET")
        ax.plot(last["lon"], last["lat"], "o", color="#e03131", ms=9, mec="white", mew=1.5,
                transform=ccrs.PlateCarree(), zorder=5)
        ax.annotate(f"now · {t}", xy=(last["lon"], last["lat"]), xycoords=ccrs.PlateCarree()._as_mpl_transform(ax),
                    xytext=(10, -16), textcoords="offset points", fontsize=9, color="#c92a2a", weight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.8), zorder=6)

    fname = PORTS[frm][0] if frm else "unknown"
    tname = PORTS[to][0] if to else "unknown"
    ax.set_title(f"{name} · {fname} → {tname}", fontsize=15, weight="bold")
    days = total / SPEED_KN / 24 if total else 0
    stats = f"{total:,.0f} nm end-to-end · {days:.1f} days at {SPEED_KN:.0f} kn"
    if sailed and ahead:
        stats += f" · {remaining:,.0f} nm ahead ({remaining / SPEED_KN / 24:.1f} d)"
    if not (frm and to):
        stats += " · partial (an end is unknown)"
    ax.annotate(stats, xy=(0.5, 0), xycoords="axes fraction", xytext=(0, -26), textcoords="offset points",
                ha="center", va="top", fontsize=11)
    ax.annotate(CAPTION, xy=(0.5, 0), xycoords="axes fraction", xytext=(0, -44), textcoords="offset points",
                ha="center", va="top", fontsize=9, style="italic", color="#555")
    if sailed or ahead:
        ax.legend(loc="lower left", fontsize=9)

    out = Path(args.out) if args.out else HERE / "maps" / f"{slugify(name)}-{datetime.now(ET):%Y-%m-%d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    print(json.dumps({"out": str(out), "name": name, "from": frm or "unknown", "to": to or "unknown",
                      "total_nm": round(total), "days_at_12kn": round(days, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
