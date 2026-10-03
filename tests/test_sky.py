import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sky.forecast import sample, sun_score, clear, HOURLY
from sky.watcher import Engine, due, sun_events

UTC = timezone.utc


def data_at(stamp, **values):
    weather = dict(cloud_cover=35, cloud_cover_low=0, cloud_cover_mid=30,
                   cloud_cover_high=45, visibility=30000, precipitation=0)
    weather.update(values)
    return {'hourly': {'time': [stamp], **{key: [value] for key, value in weather.items()}},
            'daily': {'sunrise': [stamp]}}


class FakeDelivery:
    """Deterministic delivery boundary; production lifecycle uses the same interface."""
    def __init__(self, send):
        self.send = send

    def deliver(self, payload, background=False):
        return self.send(payload)

    def cancel(self, key):
        pass

    def sweep(self, now):
        pass

    def diagnostics(self):
        return {}


class Forecast(unittest.TestCase):
    def test_texture_beats_clear_and_low_clouds(self):
        ideal = sample(data_at(100), 100)
        self.assertGreater(sun_score(ideal), sun_score(sample(data_at(100, cloud_cover_high=0, cloud_cover_mid=0), 100)))
        self.assertLess(sun_score(sample(data_at(100, cloud_cover_low=90), 100)), 30)
        self.assertEqual(sun_score(sample(data_at(100, precipitation=1), 100)), 0)

    def test_missing_stale_invalid_values_fail_closed(self):
        for value in (None, float('nan'), -1, 101):
            self.assertIsNone(sample(data_at(100, cloud_cover_low=value), 100))
        self.assertIsNone(sample(data_at(100), 2000))
        self.assertIsNone(sample({'hourly': {}}, 100))
        self.assertFalse(clear(None, {}))

    def test_clear_sky_limits(self):
        self.assertTrue(clear(sample(data_at(100, cloud_cover=20), 100), {}))
        self.assertFalse(clear(sample(data_at(100, cloud_cover=70), 100), {}))
        self.assertFalse(clear(sample(data_at(100, cloud_cover=10, visibility=5000), 100), {}))


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.now = datetime(2026, 10, 3, 10, 45, tzinfo=UTC)  # 06:45 ET
        self.view = (self.now + timedelta(minutes=90)).timestamp()
        self.data = data_at(self.view)
        self.events = sun_events(self.data)
        self.sent = []
        self.engine = Engine(self.path, {}, delivery=FakeDelivery(lambda p: self.sent.append(p) or True))

    def test_missing_weather_never_qualifies_at_zero_threshold(self):
        self.engine.settings['sunrise_threshold'] = 0
        self.engine.step({'hourly': {}}, self.now.timestamp(), self.events, self.now)
        self.assertEqual(self.sent, [])

    def test_failed_delivery_remains_visible_after_expiry(self):
        from unittest.mock import patch, Mock
        from delivery import Outbox
        box = Outbox(Path(self.tmp.name) / 'delivery.sqlite', resolve=lambda: {'envoy': ('https://example.invalid', 'private')})
        self.addCleanup(box.close)
        self.engine.delivery = box
        original = box.deliver
        with patch.object(box, 'deliver', side_effect=lambda payload, background: original(payload, self.now.timestamp())), patch('requests.post', return_value=Mock(status_code=401)):
            with self.assertLogs('sky', level='WARNING') as captured:
                self.engine.step(self.data, self.now.timestamp(), self.events, self.now)
            self.assertIn('HTTP 401', captured.output[0])
            self.assertNotIn('private', captured.output[0])
        self.assertEqual(self.engine.diagnostics()['failed_pending'], 1)
        with self.assertLogs('sky', level='ERROR'):
            self.engine.step(self.data, self.now.timestamp(), [], self.now + timedelta(minutes=16))
        restarted = Engine(self.path, {}, delivery=FakeDelivery(lambda _: True))
        self.assertEqual(restarted.diagnostics()['failed_expired'][0]['error'], 'HTTP 401')
        self.assertEqual(restarted.diagnostics()['failed_pending'], 0)

    def test_due_and_dedup_after_restart(self):
        self.engine.step(self.data, self.now.timestamp(), self.events, self.now)
        Engine(self.path, {}, delivery=FakeDelivery(lambda p: self.sent.append(p) or True)).step(
            self.data, self.now.timestamp(), self.events, self.now + timedelta(minutes=1))
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('lat', self.sent[0])
        self.assertEqual(self.sent[0]['score'], 95)

    def test_no_early_or_late_catchup(self):
        for minutes in (-1, 16, 100):
            self.engine.step(self.data, self.now.timestamp(), self.events, self.now + timedelta(minutes=minutes))
        self.assertEqual(self.sent, [])

    def test_failed_send_retries_across_restart_same_id(self):
        attempted = []
        self.engine.delivery = FakeDelivery(lambda p: attempted.append(p['id']) or False)
        self.engine.step(self.data, self.now.timestamp(), self.events, self.now)
        self.engine = Engine(self.path, {}, delivery=FakeDelivery(lambda p: attempted.append(p['id']) or True))
        self.engine.step(self.data, self.now.timestamp(), self.events, self.now + timedelta(minutes=1))
        self.assertEqual(attempted, [self.events[0]['id']] * 2)
        self.assertFalse(self.engine.state['pending'])

    def test_weather_deteriorates_before_retry(self):
        self.engine.delivery = FakeDelivery(lambda _: False)
        self.engine.step(self.data, self.now.timestamp(), self.events, self.now)
        self.engine.delivery = FakeDelivery(lambda p: self.sent.append(p) or True)
        later = self.now + timedelta(minutes=1)
        self.engine.step(data_at(self.view, precipitation=1), later.timestamp(), [], later)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.engine.state['pending'])

    def test_stale_forecast_and_expired_outbox(self):
        self.engine.step(self.data, self.now.timestamp() - 7201, self.events, self.now)
        self.assertEqual(self.sent, [])
        self.engine.delivery = FakeDelivery(lambda _: False)
        self.engine.step(self.data, self.now.timestamp(), self.events, self.now)
        self.engine.delivery = FakeDelivery(lambda p: self.sent.append(p) or True)
        self.engine.step(self.data, self.now.timestamp(), [], self.now + timedelta(minutes=91))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.engine.state['pending'])

    def test_preceding_evening_weather_at_viewing_time(self):
        now = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)  # Oct 3 20:00 ET
        viewing = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
        event = dict(id='meteor:1', kind='meteor_shower', name='Test', view_at=viewing.timestamp(), source='fixture', notes='')
        self.assertTrue(due(event, now))
        self.engine.step(data_at(viewing.timestamp(), cloud_cover=20), now.timestamp(), [event], now)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(due(event, now + timedelta(days=1)))

    def test_restart_bad_weather_cancels_persisted_receiver_before_resume(self):
        from delivery import Outbox
        enabled = {'envoy': ('envoy', 'private'), 'xai': ('xai', 'private')}
        calls = []
        def send(endpoint, payload):
            calls.append(endpoint[0])
            return endpoint[0] == 'envoy', 'HTTP 503'
        path = Path(self.tmp.name) / 'delivery.sqlite'
        box = Outbox(path, resolve=lambda: dict(enabled), send=send)
        self.engine.delivery = box
        original = box.deliver
        with patch.object(box, 'deliver', side_effect=lambda payload, background: original(payload, self.now.timestamp())):
            self.engine.step(self.data, self.now.timestamp(), self.events, self.now)
        box.close()
        enabled.pop('xai')
        box = Outbox(path, resolve=lambda: dict(enabled), send=send)
        self.addCleanup(box.close)
        restarted = Engine(self.path, {}, delivery=box)
        later = self.now + timedelta(minutes=1)
        restarted.step(data_at(self.view, precipitation=1), later.timestamp(), [], later)
        self.assertFalse(restarted.state['pending'])
        enabled['xai'] = ('xai', 'rotated')
        restarted.step(self.data, later.timestamp(), self.events, later)
        box.drain(later.timestamp())
        self.assertEqual(calls.count('xai'), 1)

    def test_background_dispatch_cannot_bypass_other_occurrence_weather(self):
        from delivery import Outbox
        calls = []
        box = Outbox(Path(self.tmp.name) / 'delivery.sqlite', resolve=lambda: {'envoy': ('envoy', 'private')},
                     send=lambda endpoint, payload: calls.append(payload['id']) or (True, 'HTTP 200'))
        self.addCleanup(box.close)
        self.engine.delivery = box
        old = self.now.timestamp() - 60
        for identity, view in [('first', self.view), ('second', self.view + 60)]:
            payload = dict(id=identity, kind='sunrise', event='sky_sunrise', name='Sunrise', notify=True,
                           view_at=datetime.fromtimestamp(view, UTC).isoformat(), useful_until=self.now.timestamp() + 600)
            self.engine.state['pending'][identity] = dict(payload=payload, expires=payload['useful_until'], forecast_at=old)
            box.publish(payload)
        good = sample(self.data, self.view)
        bad = {**good, 'precipitation': 2}
        original = box.deliver
        def dispatch(payload, background):
            self.assertTrue(background)
            return original(payload, background=background)  # actual dispatch clock, not step stamp
        with patch('sky.watcher.F.sample', side_effect=lambda data, view: good if view == self.view else bad), \
                patch('delivery.time.time', return_value=self.now.timestamp()), patch.object(box, 'deliver', side_effect=dispatch):
            self.engine.step({}, self.now.timestamp(), [], self.now)
            for thread in box.workers.values():
                thread.join(2)
        self.assertEqual(calls, ['first'])
        self.assertNotIn('second', self.engine.state['pending'])
        self.assertEqual(box.diagnostics(), {'accepted': 1, 'cancelled': 1})

    def test_quiet_hours_do_not_deliver(self):
        now = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)  # 04:00 ET
        view = (now + timedelta(minutes=90)).timestamp()
        self.engine.step(data_at(view), now.timestamp(), sun_events(data_at(view)), now)
        self.assertEqual(self.sent, [])

class CelestialIdentity(unittest.TestCase):
    def test_numerical_eclipse_refresh_has_stable_id(self):
        from sky.astronomy import event
        first = datetime(2026, 8, 28, 4, 12, 53, 662511, tzinfo=UTC)
        second = datetime(2026, 8, 28, 4, 12, 53, 601478, tzinfo=UTC)
        self.assertEqual(event('eclipse', 'Partial lunar eclipse', first, 'NASA', '')['id'],
                         event('eclipse', 'Partial lunar eclipse', second, 'NASA', '')['id'])

    def test_shifted_iss_prediction_is_one_occurrence_across_restart(self):
        from sky.astronomy import event
        now = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)
        viewing = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
        sent = []
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            engine = Engine(path, {}, delivery=FakeDelivery(lambda p: sent.append(p) or True))
            engine.step(data_at(viewing.timestamp(), cloud_cover=20), now.timestamp(),
                        [event('iss', 'ISS pass', viewing, 'Celestrak', '')], now)
            later = viewing + timedelta(seconds=40)
            engine = Engine(path, {}, delivery=FakeDelivery(lambda p: sent.append(p) or True))
            engine.step(data_at(later.timestamp(), cloud_cover=20), now.timestamp(),
                        [event('iss', 'ISS pass', later, 'Celestrak', '')], now)
            self.assertEqual(len(sent), 1)

    def test_pending_iss_refresh_updates_time_weather_and_geometry(self):
        from sky.astronomy import event
        now = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)
        viewing = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
        sent = []
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            engine = Engine(path, {}, delivery=FakeDelivery(lambda p: False))
            original = event('iss', 'ISS pass', viewing, 'Celestrak', '', altitude_deg=30)
            engine.step(data_at(viewing.timestamp(), cloud_cover=20), now.timestamp(), [original], now)
            later = viewing + timedelta(minutes=10)
            engine = Engine(path, {}, delivery=FakeDelivery(lambda p: sent.append(p) or True))
            fresh = event('iss', 'ISS pass', later, 'Celestrak', '', altitude_deg=50)
            engine.step(data_at(later.timestamp(), cloud_cover=10), now.timestamp(), [fresh], now)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['id'], original['id'])
            self.assertEqual(datetime.fromisoformat(sent[0]['view_at']).timestamp(), later.timestamp())
            self.assertEqual(sent[0]['altitude_deg'], 50)
            self.assertEqual(sent[0]['weather']['cloud_cover'], 10)

    def test_outside_region_and_expired_catalog_do_not_need_ephemeris(self):
        from sky.astronomy import Astronomy
        astro = object.__new__(Astronomy)
        astro.pin = {'lat': -30, 'lon': 140}
        self.assertEqual(astro.catalog_events(Path(__file__).resolve().parent.parent / 'sky/calendar.json',
                         datetime(2026, 10, 21, tzinfo=UTC), None), [])
        astro.pin = {'lat': 40, 'lon': -75}
        self.assertEqual(astro.catalog_events(Path(__file__).resolve().parent.parent / 'sky/calendar.json',
                         datetime(2028, 1, 1, tzinfo=UTC), None), [])

if __name__ == '__main__':
    unittest.main()
