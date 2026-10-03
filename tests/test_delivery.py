"""Crash, retry and receiver-isolation checks; no external requests."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from delivery import Outbox
from runtime import ET

NOW = datetime(2026, 10, 3, 12, tzinfo=ET).timestamp()


class Delivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'delivery.sqlite'
        self.enabled = {'envoy': ('envoy', 'private'), 'xai': ('xai', 'private')}
        self.calls = []
        self.box = self.open()
        self.addCleanup(lambda: self.box.close())
        self.payload = dict(id='ship:transit:1', event='t30', notify=True, useful_until=NOW + 600)

    def open(self, send=None):
        def sender(endpoint, payload):
            self.calls.append((endpoint[0], dict(payload)))
            return endpoint[0] == 'envoy', 'HTTP 200' if endpoint[0] == 'envoy' else 'HTTP 503'
        return Outbox(self.path, resolve=lambda: dict(self.enabled), send=send or sender)

    def restart(self):
        self.box.close()
        self.box = self.open()

    def test_partial_acceptance_survives_restart_immutable_retry(self):
        self.assertFalse(self.box.deliver(self.payload, NOW)[0])
        self.restart()
        changed = {**self.payload, 'event': 'changed', 'useful_until': NOW + 99999}
        self.box.deliver(changed, NOW + 6)
        self.assertEqual([name for name, _ in self.calls].count('envoy'), 1)
        self.assertEqual([name for name, _ in self.calls].count('xai'), 2)
        self.assertTrue(all(payload == self.payload for _, payload in self.calls))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_disabled_destination_restart_resume_never_replays(self):
        self.box.deliver(self.payload, NOW)
        del self.enabled['xai']
        self.restart()  # disabling commits cancellation before re-enable
        self.enabled['xai'] = ('xai', 'rotated')
        self.restart()
        self.box.deliver(self.payload, NOW + 6)
        self.assertEqual([name for name, _ in self.calls].count('xai'), 1)
        self.assertEqual(self.box.diagnostics(), {'accepted': 1, 'cancelled': 1})

    def test_new_destination_does_not_backfill(self):
        del self.enabled['xai']
        self.box.deliver(self.payload, NOW)
        self.enabled['xai'] = ('xai', 'private')
        self.box.deliver(self.payload, NOW + 6)
        self.assertEqual([name for name, _ in self.calls], ['envoy'])

    def test_expiry_quiet_false_notify_and_retention(self):
        self.box.publish(self.payload)
        quiet = datetime(2026, 10, 3, 4, tzinfo=ET).timestamp()
        self.box.drain(quiet)
        self.assertEqual(self.calls, [])
        self.box.drain(NOW + 600)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.box.diagnostics(), {'expired': 2})
        self.box.prune(NOW + 31 * 86400)
        self.assertEqual(self.box.diagnostics(), {})
        self.box.deliver({**self.payload, 'id': 'quiet', 'notify': False}, NOW)
        self.assertEqual(self.calls, [])

    def test_slow_receiver_does_not_block_other_or_producer(self):
        entered, release, received = threading.Event(), threading.Event(), threading.Event()
        def sender(endpoint, payload):
            if endpoint[0] == 'xai':
                entered.set()
                self.assertTrue(release.wait(5))
            else:
                received.set()
            return True, 'HTTP 200'
        self.box.send = sender
        self.box.publish(self.payload)
        thread = threading.Thread(target=self.box.drain, args=(NOW,))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(received.wait(2))
            # Publication must complete while xAI remains blocked.
            self.box.publish({**self.payload, 'id': 'second'})
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_disabled_publication_never_backfills(self):
        self.enabled.clear()
        self.assertTrue(self.box.publish(self.payload)[0])
        self.restart()
        self.enabled['envoy'] = ('envoy', 'private')
        self.box.deliver(self.payload, NOW)
        self.assertEqual(self.calls, [])

    def test_explicit_test_requires_receivers_and_acceptance(self):
        self.enabled.clear()
        self.assertEqual(self.box.deliver(self.payload, NOW, require_acceptance=True),
                         (False, 'no enabled destinations'))
        self.enabled['envoy'] = ('envoy', 'private')
        active = {**self.payload, 'id': 'active'}
        self.assertEqual(self.box.deliver(active, NOW, require_acceptance=True),
                         (True, 'receiver accepted'))
        self.assertEqual(len(self.calls), 1)

    def test_explicit_test_rejects_partial_cancelled_and_expired(self):
        self.assertFalse(self.box.deliver(self.payload, NOW, require_acceptance=True)[0])
        self.enabled.pop('xai')
        self.assertEqual(self.box.deliver(self.payload, NOW + 6, require_acceptance=True),
                         (False, 'delivery cancelled'))
        self.assertTrue(self.box.deliver(self.payload, NOW + 6)[0])
        expired = {**self.payload, 'id': 'expired', 'useful_until': NOW}
        self.assertEqual(self.box.deliver(expired, NOW, require_acceptance=True),
                         (False, 'delivery expired'))
        self.assertEqual([name for name, _ in self.calls].count('envoy'), 1)

    def test_explicit_test_during_quiet_hours_is_not_acceptance(self):
        quiet = datetime(2026, 10, 3, 4, tzinfo=ET).timestamp()
        payload = {**self.payload, 'useful_until': quiet + 600}
        self.assertEqual(self.box.deliver(payload, quiet, require_acceptance=True),
                         (False, 'awaiting delivery'))
        self.assertEqual(self.calls, [])

    def test_second_event_reaches_healthy_destination_while_first_stalls(self):
        import time
        entered, release, first, second = (threading.Event() for _ in range(4))
        def sender(endpoint, payload):
            if endpoint[0] == 'xai':
                entered.set()
                release.wait(5)
            elif payload['id'] == self.payload['id']:
                first.set()
            else:
                second.set()
            return True, 'HTTP 200'
        self.box.send = sender
        self.box.publish(self.payload)
        self.box.drain(NOW, background=True)
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(first.wait(2))
            self.box.publish({**self.payload, 'id': 'second'})
            deadline = time.monotonic() + 2
            while not second.is_set() and time.monotonic() < deadline:
                self.box.drain(NOW, background=True)
                second.wait(0.01)
            self.assertTrue(second.is_set())
        finally:
            release.set()

    def test_disable_sweep_cancels_queued_jobs_behind_stalled_request(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        self.enabled = {'xai': ('xai', 'private')}
        def sender(endpoint, payload):
            calls.append(payload['id'])
            entered.set()
            release.wait(5)
            return True, 'HTTP 200'
        self.box.send = sender
        self.box.publish(self.payload)
        self.box.publish({**self.payload, 'id': 'second'})
        self.box.drain(NOW, background=True)
        try:
            self.assertTrue(entered.wait(2))
            self.enabled.clear()
            self.box.drain(NOW, background=True)
            self.assertEqual(self.box.diagnostics(), {'cancelled': 2})
            self.enabled['xai'] = ('xai', 'rotated')
        finally:
            release.set()
        self.box.workers['xai'].join(2)
        self.box.drain(NOW)
        self.assertEqual(calls, [self.payload['id']])

    def test_background_rows_recheck_actual_expiry_after_stall(self):
        entered, release = threading.Event(), threading.Event()
        calls, clock = [], [NOW]
        self.enabled = {'xai': ('xai', 'private')}
        def sender(endpoint, payload):
            calls.append(payload['id'])
            entered.set()
            release.wait(5)
            return True, 'HTTP 200'
        self.box.send = sender
        self.box.publish(self.payload)
        self.box.publish({**self.payload, 'id': 'second'})
        with patch('delivery.time.time', side_effect=lambda: clock[0]):
            self.box.drain(background=True)
            try:
                self.assertTrue(entered.wait(2))
                clock[0] += 601
            finally:
                release.set()
            self.box.workers['xai'].join(2)
        self.assertEqual(calls, [self.payload['id']])
        self.assertEqual(self.box.diagnostics(), {'accepted': 1, 'expired': 1})

    def test_http_crash_after_accept_reuses_id_and_body(self):
        self.enabled = {'envoy': ('envoy', 'private')}
        self.box.publish(self.payload)
        # Simulate remote acceptance with no local acknowledgement commit.
        first = json.loads(self.box.db.execute('SELECT body FROM events').fetchone()[0])
        self.calls.append(('envoy', first))
        self.restart()
        self.box.deliver({**self.payload, 'event': 'mutated'}, NOW)
        self.assertEqual(self.calls[0], self.calls[1])


class Publication(unittest.TestCase):
    def test_cli_send_without_destination_fails(self):
        from ships import watcher as W
        with tempfile.TemporaryDirectory() as temporary:
            def open_box(path):
                return Outbox(path, resolve=lambda: {})
            with patch.object(W, 'HERE', Path(temporary)), patch.object(W, 'Outbox', side_effect=open_box):
                self.assertEqual(W.send_now(dict(id='test', notify=True, useful_until=NOW + 600)),
                                 (False, 'no enabled destinations'))

    def test_ship_intent_survives_crash_before_and_after_queue_publication(self):
        from ships import watcher as W
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(W, 'STATE_PATH', root / 'state.json'), patch.object(W, '_PIN', 1):
                class Crash:
                    def submit(self, payload):
                        raise RuntimeError('crash before publication')
                watcher = W.Watcher(Crash())
                payload = dict(id='error:fixed', useful_until=NOW + 60, event='watcher_error')
                with self.assertRaises(RuntimeError):
                    watcher.publish_event(payload)
                self.assertEqual(json.loads(W.STATE_PATH.read_text())['publication_pending'][payload['id']], payload)
                box = Outbox(root / 'delivery.sqlite', resolve=lambda: {'envoy': ('envoy', 'private')}, send=lambda *args: (True, 'HTTP 200'))
                try:
                    class PublishThenCrash:
                        def submit(self, body):
                            box.publish(body)
                            raise RuntimeError('crash after publication')
                    with self.assertRaises(RuntimeError):
                        W.Watcher(PublishThenCrash())
                    class Publish:
                        def submit(self, body):
                            box.publish(body)
                    recovered = W.Watcher(Publish())
                    self.assertFalse(recovered.state['publication_pending'])
                    self.assertEqual(box.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
                    box.drain(NOW)
                    self.assertEqual(box.diagnostics(), {'accepted': 1})
                finally:
                    box.close()


if __name__ == '__main__':
    unittest.main()
