"""Real offline Skyfield predictions against compact public DE421/TLE fixtures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from sky.astronomy import Astronomy

FIXTURES = Path(__file__).parent / 'fixtures'
UTC = timezone.utc
ZONE = ZoneInfo('America/New_York')

class Predictions(unittest.TestCase):
    def observer(self, fixture, pin=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name)
        shutil.copy(FIXTURES / fixture, cache / 'de421.bsp')
        astro = Astronomy(pin or {'lat': 39.9535, 'lon': -75.132}, cache)
        self.addCleanup(astro.eph.close)
        return astro

    @patch('requests.get', side_effect=AssertionError('network forbidden'))
    def test_real_eclipse_supermoon_planets_and_visibility(self, _):
        for fixture, now, kind in [('eclipse.bsp', datetime(2026,8,27,12,tzinfo=UTC), 'eclipse'),
                                   ('moon.bsp', datetime(2026,12,23,12,tzinfo=UTC), 'supermoon')]:
            astro = self.observer(fixture)
            with patch.object(astro, 'iss', return_value=[]):
                events = astro.events(now, ZONE)
            self.assertTrue(any(row['kind'] == kind for row in events), events)
            planets = [row for row in events if row['kind'] == 'planet']
            if kind == "supermoon":
                self.assertTrue(planets)
            self.assertTrue(all(row['altitude_deg'] >= 15 for row in planets))
            noon = astro.ts.from_datetime(now.replace(hour=16))
            self.assertFalse(astro.visible(astro.eph['sun'], noon))
            self.assertFalse(astro.visible(astro.eph['moon'], noon, minimum=90, darkness=False))

    @patch('requests.get', side_effect=AssertionError('network forbidden'))
    def test_accepted_meteor_catalog(self, _):
        astro = self.observer('meteors.bsp')
        events = astro.catalog_events(FIXTURES.parent.parent / 'sky/calendar.json',
                                    datetime(2026,10,21,12,tzinfo=UTC), ZONE)
        self.assertEqual([row['name'] for row in events], ['Orionids'])
        astro.pin = {'lat': -33.86, 'lon': 151.2}
        self.assertEqual(astro.catalog_events(FIXTURES.parent.parent / 'sky/calendar.json',
                         datetime(2026,10,21,12,tzinfo=UTC), ZONE), [])

    @patch('requests.get', side_effect=AssertionError('network forbidden'))
    def test_iss_sunlit_dark_passes_and_stale_elements(self, _):
        from skyfield.api import EarthSatellite
        astro = self.observer('iss.bsp', {'lat': -33.86, 'lon': 151.2})
        now = datetime(2026,10,3,tzinfo=UTC)
        path = astro.cache / 'iss.tle'
        shutil.copy(FIXTURES / 'iss.tle', path)
        os.utime(path, (now.timestamp(), now.timestamp()))
        start, end = astro.ts.from_datetime(now), astro.ts.from_datetime(now + timedelta(days=3))
        events = astro.iss(now, start, end)
        self.assertEqual(len(events), 2)
        self.assertAlmostEqual(events[0]['view_at'], 1791139420.49, delta=2)
        lines = path.read_text().splitlines()
        sat = EarthSatellite(lines[1], lines[2], lines[0], astro.ts)
        times, codes = sat.find_events(astro.site, start, end, altitude_degrees=30)
        rejected = []
        for time, code in zip(times, codes):
            if code == 1 and (not sat.at(time).is_sunlit(astro.eph) or astro.altitude(astro.eph['sun'], time)[0] > -6):
                rejected.append(time.utc_datetime().timestamp())
        self.assertTrue(rejected)
        self.assertTrue(all(abs(row['view_at'] - time) > 1 for row in events for time in rejected))
        stale = now + timedelta(days=4)
        os.utime(path, (stale.timestamp(), stale.timestamp()))
        with self.assertRaisesRegex(ValueError, 'stale ISS'):
            astro.iss(stale, start, end)
