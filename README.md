# HeadsUp

No-AI watchers for commercial ships and skies worth looking at. Ships use AISStream; sky uses Open-Meteo and Skyfield. Both publish durable JSON events to your enabled webhook receivers. Your private shore pin and credentials stay in ignored local files.

## Layout

- `ships/watcher.py` follows ship transits; `ships/route_map.py` renders estimated end-to-end voyage maps.
- `sky/watcher.py` scores sunrise/sunset forecasts and checks celestial viewing opportunities.
- `supervisor.py`, `ensure-running.sh`, `stop.sh` supervise both processes with an OS file lock and private control socket.
- `config.example.json` is a public template. Runtime files live under ships/ and sky/; root config/env stay private.
- `docs/operations.md` defines webhook handling, enrichment, quiet rules and forecast calibration.
- `docs/box-migration.md` records live migration/recovery requirements.
- `docs/delivery.md` defines event IDs, immutable retries, destination controls and live acceptance gates.

## Setup

Commands below use /Users/johnsolly/code/heads-up as cwd. Copy the example into config.local.json, fill in your own pin, and protect it with permissions 0600. Use a shore point on the Delaware channel for ship ETAs. The bundled geometry is specific to the Delaware River; adapting another river requires replacing channel/port data.

```bash
cp config.example.json config.local.json
chmod 600 config.local.json
python3 -m venv .venv
.venv/bin/pip install -r requirements-watcher.txt
.venv/bin/python -m unittest discover -s tests -v
./ensure-running.sh
./stop.sh
```

Set `AISSTREAM_API_KEY`, `HEADSUP_WEBHOOK_URL` and `HEADSUP_WEBHOOK_KEY` in your platform's injected environment or a private root `.env.local` with literal KEY=value lines. The files are read as data, never executed as shell code. Values in the injected environment take precedence. Keys are not logged. For multiple replaceable receivers, configure named destinations as described in docs/delivery.md; keep paused xAI excluded until rotation and an authorized enable. No personal pin is required for tests; fixtures use public channel waypoints.

Configuration keys are pin.lat/pin.lon, optional bbox/message_types, and sky settings. Default bbox covers 38.75–40.25 N, -75.65–-74.65 E, including the bay mouth and Fairless/Trenton. Existing explicit bbox overrides need updating. sky.enabled disables the sky child when false. Sunrise threshold defaults to 65, sunset to 75; cloud ceiling 30% and visibility floor 10 km apply to celestial alerts. These defaults are heuristics pending seven-day observation calibration.

The Linux/macOS supervisor uses one held file lock, automatically restarts a failed child after 10 seconds, and accepts status/stop through a socket with mode 0600. Child wrappers own per-process locks and stop the worker when the supervisor pipe closes, including after a hard supervisor crash. PID reuse does not authorize killing an unrelated process. Set HEADSUP_PYTHON to a chosen Python executable if needed. It needs Python 3.11+. ensure-running verifies supervisor readiness, not child health; inspect both heartbeat files. A platform boot hook or external health routine is required for reboot/rollback recovery.

## Ships

Cargo/tankers must be at least 80 m, passengers at least 120 m, other eligible hulls at least 120 m. Fishing, tugs, pleasure, pilot and similar craft are excluded. Static vessel dimensions/types are persisted. Direction and distance use the nearest segment of the Delaware channel polyline rather than latitude. ETA uses recent underway speeds, ignoring anchor zeros after motion.

Events: new_ship, t60, t30, eta_shift, stopped_short, passed, lost_signal, watcher_error. Events are remembered once per transit except meaningful ETA changes. Signal loss waits 90 minutes of continuous stream coverage after the last global gap and explicitly labels AIS uncertainty. Stopped vessels do not get lost-signal alerts. Full-stream silence is a service problem; it should not be interpreted as every vessel disappearing. Coverage recovery starts a fresh 90-minute evidence window.

Ship publication intents and emitted markers are saved together before delivery. A private SQLite ledger records immutable event bodies and separate receiver acceptances. Failed receivers retry automatically while an event remains useful; successful receivers are not resent during ordinary retries. Outside quiet hours, events remain local with `notify=false` and their notification deliveries are cancelled. User-facing notifications are allowed 05:15 through 21:30 America/New_York, including the endpoint minutes; receivers recheck current delivery time. Legacy ships/pending-events.jsonl remains historical evidence and is not replayed. See docs/delivery.md for expiry, crash recovery and disabled-receiver behavior.

```bash
.venv/bin/python ships/watcher.py --test
.venv/bin/python ships/route_map.py 123456789 --from USPHL --to CAHAL
.venv/bin/python supervisor.py status
```

Use an actual tracked MMSI for a map. Map data comes from ships/state.json and optional ships/cargo/IMO.json. Output is in ignored ships/maps/. The solid/dashed route is an estimate with unknown endpoints retained. It is never a filed passage plan. Map rendering requires the complete requirements.txt in the assistant-side environment. The map renderer uses locally installed Cartopy/Matplotlib and map data, not runtime CDN assets.

## Sky

Open-Meteo supplies daily sun times and hourly total/high/middle/low cloud, visibility and precipitation. Forecasts refresh hourly and fail closed after two hours. Alerts qualify between 90 and 75 minutes before sunrise/sunset, favoring sunrise with a lower threshold. Low clouds, poor visibility, rain and overcast reduce scores. Candidate records enable calibration. Quiet hours can prevent a 90-minute sunrise alert; quiet policy wins over lead time.

Skyfield derives local bright-planet evening opportunities, full moons within 360,000 km, lunar eclipses above the horizon and sunlit ISS passes above 30 degrees during local darkness. It downloads a cached DE421 ephemeris and refreshes ISS elements every six hours, rejecting orbital epochs over three days old. The sourced meteor calendar expires August 14, 2027 and covers the US Northeast region. Solar eclipse support accepts curated local entries in the calendar with NASA-verified contact times/regions; none are bundled without verified local visibility. Catalog expiry and provider failures appear in logs/heartbeat and must be reviewed.

Celestial outlooks are evaluated 18:00–21:30 on the preceding local evening against the forecast at viewing time. Sky outbox entries are persisted before delivery, freeze their payload at first ledger publication, retry with a stable idempotency key, and expire at viewing time for celestial events, 75 minutes before sunrise/sunset, or two hours after their forecast. A receiving webhook must deduplicate IDs; delivery is at least once across a crash between HTTP success and state commit. Missing/null weather never qualifies. Private observer coordinates are sent to weather services for calculations but omitted from event payloads/logs.

Provider references: [Open-Meteo forecast API](https://open-meteo.com/en/docs), [Skyfield satellite visibility](https://rhodesmill.org/skyfield/earth-satellites.html), [AMS calendar](https://www.amsmeteors.org/calendar/), [NASA eclipse catalog](https://eclipse.gsfc.nasa.gov/lunar.html). Open-Meteo's free endpoint is for noncommercial use; check its terms before commercial deployment.

GitHub Actions runs offline tests on Python 3.11–3.13 without secrets. Live calibration, provider accuracy and reboot/routine acceptance require evidence from the installed box. See the migration runbook before upgrading an existing installation.
