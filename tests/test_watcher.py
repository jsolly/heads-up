"""Unit tests. Run: python3 -m unittest discover -s tests -v   (needs config.local.json for the pin)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import watcher as W  # noqa: E402

HAVE_CFG = (HERE / "config.local.json").exists()


@unittest.skipUnless(HAVE_CFG, "config.local.json (pin) missing")
class AlongToPin(unittest.TestCase):
    def setUp(self):
        cfg = json.loads((HERE / "config.local.json").read_text())
        self.plat, self.plon = cfg["pin"]["lat"], cfg["pin"]["lon"]

    def test_points(self):
        tinicum = W.along_to_pin(39.8545, -75.2650)
        marcus = W.along_to_pin(39.8010, -75.4100)
        packer = W.along_to_pin(39.8990, -75.1330)
        north = W.along_to_pin(self.plat + 0.004, self.plon + 0.008)  # just upriver of the pin
        print(f"\n  along_to_pin nm: MarcusHook={marcus:.2f} Tinicum={tinicum:.2f} Packer={packer:.2f} "
              f"justNorth={north:.2f}")
        self.assertTrue(15 < marcus < 21, marcus)  # ~RM 79 -> ~RM 100 statute
        self.assertTrue(7 < tinicum < 11, tinicum)
        self.assertTrue(2.5 < packer < 5, packer)
        self.assertTrue(-1.0 < north < 0, north)
        self.assertGreater(marcus, tinicum)
        self.assertGreater(tinicum, packer)
        self.assertGreater(packer, 0)

    def test_tinicum_east_west_reach(self):
        # Along Tinicum the river runs ~E-W: two points at the SAME latitude must differ by longitude.
        west = W.along_to_pin(39.855, -75.300)
        east = W.along_to_pin(39.855, -75.230)
        self.assertGreater(west - east, 2.5)

    def test_pin_is_zero(self):
        self.assertAlmostEqual(W.along_to_pin(self.plat, self.plon), 0.0, places=6)


class AvgSog(unittest.TestCase):
    def test_anchor_zeros_ignored_when_moving(self):
        self.assertAlmostEqual(W.avg_sog([10.0, 0.0, 10.4, 0.1, 9.8]), (10.0 + 10.4 + 9.8) / 3)

    def test_not_moving_plain_mean(self):
        self.assertAlmostEqual(W.avg_sog([0.0, 0.2, 2.0]), 0.7333333, places=5)

    def test_was_moving_flag(self):
        self.assertAlmostEqual(W.avg_sog([0.0, 3.5, 0.0], was_moving=True), 3.5)
        self.assertAlmostEqual(W.avg_sog([0.0, 3.5, 0.0], was_moving=False), 3.5 / 3)

    def test_all_slow_after_moving_returns_mean(self):
        self.assertAlmostEqual(W.avg_sog([0.0, 0.0], was_moving=True), 0.0)

    def test_invalid_102(self):
        self.assertAlmostEqual(W.avg_sog([102.3, 8.0]), 8.0)
        self.assertIsNone(W.avg_sog([]))


@unittest.skipUnless(HAVE_CFG, "config.local.json (pin) missing")
class Transit(unittest.TestCase):
    """Synthetic AIS messages through the event engine (no network)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        for name, fn in [("STATE_PATH", "state.json"), ("HEARTBEAT_PATH", "hb.json"),
                         ("PENDING_PATH", "p.jsonl"), ("EVENTS_PATH", "e.jsonl")]:
            setattr(W, name, t / fn)
        self.events = []
        outer = self

        class Cap:
            def submit(self, p):
                outer.events.append(p)

        self.w = W.Watcher(Cap())
        self.w.state["static"].clear()
        self.w.state["ships"].clear()

    def tearDown(self):
        self.tmp.cleanup()

    def static(self, mmsi=123456789, name="TEST CARRIER", length=200, typ=70, dest="USFAH"):
        m = {"MessageType": "ShipStaticData", "MetaData": {"MMSI": mmsi, "ShipName": name},
             "Message": {"ShipStaticData": {"Name": name, "Type": typ, "ImoNumber": 9999999, "CallSign": "TEST",
                                             "Destination": dest, "Dimension": {"A": length - 30, "B": 30}}}}
        self.w.handle_message(json.dumps(m), now=1_000_000)

    def pos(self, t, lat, lon, sog, cog, mmsi=123456789):
        m = {"MessageType": "PositionReport", "MetaData": {"MMSI": mmsi, "ShipName": "TEST CARRIER"},
             "Message": {"PositionReport": {"Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": cog,
                                             "NavigationalStatus": 0}}}
        self.w.handle_message(json.dumps(m), now=t)

    def run_track(self, pts, t0=1_000_000, dt=120, sog=10.0, mmsi=123456789):
        """Interpolate along the channel polyline between waypoint indices."""
        t = t0
        for (la, lo, cog) in pts:
            self.pos(t, la, lo, sog, cog, mmsi)
            t += dt
        return t

    def channel_track(self, i0, i1, steps_per_seg=4):
        ch = W.CHANNEL
        step = 1 if i1 > i0 else -1
        out = []
        for i in range(i0, i1, step):
            (a, b, _), (c, d, _) = ch[i], ch[i + step]
            import math
            cog = (math.degrees(math.atan2((d - b) * math.cos(math.radians(a)), c - a)) + 360) % 360
            for k in range(steps_per_seg):
                f = k / steps_per_seg
                out.append((a + (c - a) * f, b + (d - b) * f, cog))
        return out

    def test_northbound_full_transit(self):
        self.static()
        self.run_track(self.channel_track(10, 30))  # Wilmington -> Riverton
        kinds = [e["event"] for e in self.events]
        self.assertEqual(kinds[0], "new_ship")
        for k in ("t60", "t30", "passed"):
            self.assertEqual(kinds.count(k), 1, kinds)
        self.assertNotIn("stopped_short", kinds)
        self.assertEqual(self.events[-1]["direction"], "northbound")

    def test_southbound_outbound_from_fairless(self):
        self.static(dest="CAHAL")
        self.run_track(self.channel_track(36, 15))
        kinds = [e["event"] for e in self.events]
        self.assertEqual(kinds[0], "new_ship")
        self.assertIn("passed", kinds)
        self.assertEqual(self.events[0]["direction"], "southbound")

    def test_small_tug_ignored(self):
        self.static(typ=52, length=215, name="OSG ATB")
        self.run_track(self.channel_track(10, 30))
        self.assertEqual(self.events, [])

    def test_stopped_short_and_no_repeat_after_restart(self):
        self.static(dest="USPHL")
        t = self.run_track(self.channel_track(12, 20))  # Marcus Hook -> Packer
        for k in range(4):  # anchor zeros at Packer
            self.pos(t + k * 120, 39.8995, -75.1320, 0.0, 10)
        kinds = [e["event"] for e in self.events]
        self.assertIn("stopped_short", kinds)
        self.assertNotIn("passed", kinds)
        # restart: new Watcher from persisted state, replay same stop -> no duplicate events
        n = len(self.events)
        self.w.save(force=True)
        outer = self

        class Cap:
            def submit(self, p):
                outer.events.append(p)
        w2 = W.Watcher(Cap())
        self.w = w2
        for k in range(3):
            self.pos(t + 600 + k * 120, 39.8995, -75.1320, 0.0, 10)
        self.assertEqual(len(self.events), n)

    def test_lost_signal(self):
        self.static()
        t = self.run_track(self.channel_track(10, 16))
        self.w.periodic(now=t + 46 * 60)
        self.w.periodic(now=t + 50 * 60)
        kinds = [e["event"] for e in self.events]
        self.assertEqual(kinds.count("lost_signal"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
