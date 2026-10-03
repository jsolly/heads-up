# HeadsUp operation contract

This is repo-owned behavior for the webhook handler. The canonical agent skill must be restored in dotagents and distributed through its installers. This file is not an installed skill.

## Notifications

Use America/New_York with daylight saving time. The notification window is 05:15 through 21:30 inclusive at the minute level. Ship events still reach the webhook and are recorded outside this window; `notify` is false. The handler must also check the current delivery time before any user-facing message. An arrival forecast outside the window is a note, not a reason to discard an otherwise useful in-window heads-up. Sky delivery waits for the window and expires at viewing time for celestial events, 75 minutes before sunrise/sunset, or when its forecast becomes stale.

Keep quiet for heartbeat checks, healthy restarts, low-quality skies, clouded-out celestial events and unchanged ship ETAs. Suppress duplicate messages using sky `id`, or ship MMSI + transit + event identity. Never announce a ship's physical arrival or disappearance from an uncertain AIS gap. `lost_signal` means coverage is uncertain; preserve the last-known fix and label the ETA unconfirmed. Recheck fresh tracking before replying. `watcher_error` is an actionable repeated failure, not a healthy heartbeat.

## Ship event handling

`new_ship`, `t60`, `t30`: identify name, direction, local ETA and why this vessel is interesting. Destination is reported AIS intent and may be stale. `eta_shift`: update only the changed ETA. `stopped_short`: countdown pauses, do not imply a shore-pin pass. `passed`: state whether inferred across a gap. `lost_signal`: no current position confirmed. Do not generate repeated alerts for one transit; the persisted ship state already tracks sent events.

For enrichment, look up the IMO first and match name/type/dimensions. A photo must match this actual vessel, not a sister ship, same-name vessel or stock ship. Verify the source page and credit/link it. If identity cannot be confirmed, omit the photo. Do not copy a private pin or address into a message, map, command output or public artifact.

Likely cargo must be phrased as an inference. Support it with vessel class, recent load port and destination terminal. Do not claim a manifested cargo without a reliable source. Distinguish known AIS destination from inferred itinerary and unknown origin.

Create an end-to-end voyage map with `ships/route_map.py`, resolving origin from confirmed tracker/port evidence and destination from normalized AIS/port data. Check both endpoints and the current ship location. Solid means estimated sailed route, dashed means estimated remaining route. Always include the estimate caption; it is not the ship's filed passage plan. Unknown endpoints stay unknown. Do not fabricate a loading port to make a map look complete. Cache enrichment under ships/cargo and verified photos under ships/photos, both ignored.

## Sky events

Say forecast quality is a heuristic, not a probability or guarantee. Report local viewing time, score/clouds, and source. Sunrise is favored by a lower threshold for the east-facing shore. Point cloud layers do not measure distant horizon obstruction, smoke, terrain or the actual colors.

ISS passes require recent elements, sunlit satellite, observer Sun below -6 degrees and culmination above 30 degrees. Planet opportunities are daily evening naked-eye viewing, not rare alignments. Full moons use a stated 360,000 km geocentric distance threshold, not a universal definition of supermoon. Lunar eclipse time is maximum; local Moon must be above 5 degrees. Solar eclipses require a curated local contact entry and region verified against NASA maps; never announce a globally listed eclipse as locally visible. Never suggest looking at the Sun without proper eclipse eye protection.

Meteor entries use AMS peak nights and suggested local viewing times. Rates depend on darkness, moonlight and the radiant; no promise of a meteor storm. The bundled calendar expires August 14, 2027. Refresh it from AMS before then, preserving provenance, local offset, region and radiant checks. A missing/expired calendar means no static events, not no celestial activity.

## Calibration

For seven days after live installation, record sunrise/sunset candidate ID, observed quality 0-100, horizon clarity and whether the alert helped in private sky/observations.jsonl. Compare observations with sky/candidates.jsonl, including rejected candidates. Tune thresholds/weights from that evidence, then rerun tests. Keep this acceptance step open until seven days of observations exist. No private coordinates belong in observations.
