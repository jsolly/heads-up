# River Rubber Necker: Delaware River ship watcher (no AI)

A small, dependency-light watcher that follows big commercial ships on the Delaware River via
[AISStream](https://aisstream.io) and POSTs JSON events to a webhook as they approach and pass a
private shore pin. The pin's coordinates live only in `config.local.json`, which is gitignored and
never logged.

```
watcher.py         long-running AIS websocket client + event engine
river-watcher.sh   supervisor loop (restarts watcher.py ~10 s after it exits)
ensure-running.sh  idempotent starter (nohup); prints "running" or "started"
stop.sh            stops supervisor + watcher
route_map.py       end-to-end voyage map PNG (cartopy, /workspace/mapenv)
tests/             unit tests (geometry, avg_sog, synthetic transits)
config.example.json  template for config.local.json
```

## Setup

```bash
cp config.example.json config.local.json      # then fill in pin.lat / pin.lon (private!)
chmod 600 config.local.json
pip install websocket-client requests          # system python is fine
python3 -m venv /workspace/mapenv && /workspace/mapenv/bin/pip install cartopy matplotlib shapely pyproj
```

`config.local.json` keys:

| key | meaning |
|---|---|
| `pin.lat`, `pin.lon` | private shore point (required) |
| `bbox` | AISStream `BoundingBoxes` (defaults to `../aisstream-bbox.json`, then a built-in Delaware box) |
| `message_types` | AISStream `FilterMessageTypes` (default `PositionReport`, `ShipStaticData`) |

### Secrets / environment

| env var | use |
|---|---|
| `AISSTREAM_API_KEY` | AISStream subscription key (required) |
| `DELAWARE_WEBHOOK_URL` | webhook endpoint (optional) |
| `DELAWARE_WEBHOOK_KEY` | sent as `Authorization: Bearer <key>` (optional) |

On the box these come from the platform-injected shell environment. The supervisor and watcher
inherit the environment of whoever runs `ensure-running.sh`. You can also put `KEY=value` lines in a
gitignored `.env.local` (chmod 600). The supervisor sources it before every restart, and the
watcher re-reads it whenever it sends an event, so webhook values added there take effect without a
restart. If you add the variables to the shell env instead, restart with `./stop.sh && ./ensure-running.sh`.

If the webhook variables are missing, events go to `pending-events.jsonl` and the watcher keeps
running. Every event is also appended to `events.jsonl`.

## Running

```bash
./ensure-running.sh          # started / running
cat heartbeat.json           # ts, last_msg_age_s, tracked, active_transits, webhook_configured ...
tail -f watcher.log          # rotating log (2 MB x 5)
./stop.sh
python3 watcher.py --test    # send one {"event":"test"} payload (exit 0 if delivered)
python3 -m unittest discover -s tests -v
```

## How it works

### Which ships
A ship needs `ShipStaticData` before it can qualify. Static data is cached in `state.json` across restarts.
- Excluded types: 30-37 (fishing, towing, dredging, diving, military, sailing, pleasure) and 50-59
  (pilot, SAR, tug, port tender, law enforcement, ATB-style 56/57 ...). Names with PILOT, POLICE,
  USCG, BARGE and similar are also excluded.
- Cargo/tanker (70-89) must be at least 80 m long; passenger (60-69) at least 120 m; any other type
  at least 120 m (this covers big hulls that report type 0 or 90).

### Channel geometry
`CHANNEL` is a polyline of about 38 waypoints from the bay mouth (Cape Henlopen / Cape May) up past
Philadelphia to Trenton/Fairless. It runs through Reedy Island, New Castle, Wilmington, Marcus Hook,
Chester, Eddystone, Tinicum/Paulsboro (where the river runs east-west), Fort Mifflin, the Schuylkill
mouth, Packer Ave, Penn's Landing, Petty Island, Port Richmond, Tacony, Burlington and Bristol.
`along_to_pin(lat, lon)` projects the position onto the **nearest segment** and returns the signed
along-channel distance in nm to the pin. Positive means the pin is still ahead for a northbound ship.
It does not use a latitude test, because that breaks along the east-west Tinicum reach.

### ETA
`ETA = |along_to_pin| / avg_sog`. `avg_sog` averages up to 8 SOG readings from the last 15 min.
Once the ship has been moving at 4+ kn, readings under 3 kn are ignored so anchor zeros don't skew the
ETA. If every reading is slow, it returns the plain mean.

### Direction
Northbound or southbound comes from the along-channel trend over the last 4-20 min. If there is no
trend yet, it falls back to COG against the local channel bearing. "Toward the pin" means northbound
south of the pin or southbound north of it, so outbound ships from Fairless/Port Richmond work too.

### Events
Each event fires at most once per ship per transit, except eta_shift. State is persisted to
`state.json` **before** sending, so a restart never repeats an alert.

| event | when |
|---|---|
| `new_ship` | first sight of a qualifying ship under way (≥3 kn) toward the pin. Inside 60/30 min already? t60/t30 are marked covered. |
| `t60` / `t30` | ETA crosses 60 / 30 min (t60 is skipped if it jumps straight to t30) |
| `eta_shift` | after t60, ETA moves ≥15 min from the last announced ETA; 10 min cooldown; repeatable |
| `stopped_short` | after new_ship, <3 kn on ≥2 fixes spanning ≥3 min while short of the pin. Countdown pauses until she is under way toward the pin again. |
| `passed` | along-channel sign flips in her direction of travel (noted if inferred across a signal gap) |
| `lost_signal` | no AIS for ≥45 min before passing (not while stopped short); last-known ETA kept |
| `watcher_error` | websocket failed ≥5 times in an hour (2 h cooldown) |

A transit closes quietly when she has passed, turned away (≥3 kn away from the pin for 10 min and
0.5 nm), been stopped short for 6 h, or been silent for 12 h. A new transit can start later, for
example on the way back out.

### Payload
```json
{"event":"t30","ts":"2026-10-02T14:05:12-04:00","mmsi":311000193,"imo":9567740,"name":"TANCHOU ARROW",
 "callsign":"...","length":210,"type_code":70,"destination":"USPHL","lat":39.91,"lon":-75.13,
 "sog":6.9,"cog":347.1,"eta_et":"14:35","minutes_out":30,"direction":"northbound","dist_nm":3.4,
 "notes":"destination USPHL - may stop at a berth before the pin"}
```
`ts` is ISO in ET and `eta_et` is HH:MM ET. `notes` flags hedges, such as a destination short of the
pin or a pass ETA outside 05:00-20:00 ET. Notes never suppress an event.

### Files written (all gitignored)
`state.json`, `heartbeat.json` (every 30 s), `watcher.log` (rotating), `supervisor.log`,
`events.jsonl`, `pending-events.jsonl`, `*.pid`, `maps/`.

## route_map.py

```bash
/workspace/mapenv/bin/python route_map.py <MMSI> [--from LOCODE] [--to LOCODE] [--out path]
```
- The last position, name, IMO and AIS destination come from `state.json`. The origin defaults to
  `last_port` in `cargo/<IMO>.json` when that file exists. The destination defaults to the AIS
  destination if it maps to a known port (handles `US PHL`, `USNYC>>USPHL`, `GTSTC-USGLC`, ...).
- `PORTS` covers the Delaware River (USPHL, USPHLPT Packer, USMAH, USDCI, USPAU, USWIL/USILG,
  USCMD/USCDE, USFAH Fairless, USGLC, USBUH, USBRL), US East Coast (USNYC, USBAL, USORF, USSAV),
  Canada (CAHAL, CAWHH, CASOR, CAMTR), Europe (Rotterdam, Antwerp, Hamburg, Wilhelmshaven, Skagen,
  Algeciras, El Dekheila) and Latin America (Santo Tomas de Castilla, Puerto Cortes, Pecem, Santos,
  Freeport, Kingston, Cartagena).
- Routing runs Dijkstra over the river channel plus hand-placed sea legs: Delaware Bay mouth, the
  C&D canal, Chesapeake, Cape Hatteras, Florida Straits/Yucatan, the Bahamas-Windward Passage, Nova
  Scotia/Cabot Strait/St Lawrence, the North Atlantic/English Channel/German Bight/Skagerrak,
  Gibraltar/Med, and Brazil's north coast. Pecem is reached along the north coast without rounding
  Cape São Roque.
- The map shows a solid line for the part already sailed, a dashed line for the part ahead, a red
  "now" dot with the ET time, the title `NAME · From → To` (`unknown` for a missing end), the total nm
  and days at 12 kn, and the caption *route is my estimate from AIS and tracker data, not her filed
  passage plan*.
- Output goes to `maps/<slug>-<YYYY-MM-DD>.png`.
