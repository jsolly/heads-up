"""Compressed AIS framing tested against an offline local WebSocket server."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ships import watcher as W
from websockets.sync.client import connect
from websockets.sync.server import serve


class Stream(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = patch.object(W, 'STATE_PATH', Path(self.temp.name) / 'state.json')
        pin = patch.object(W, '_PIN', 1)
        state.start(); pin.start()
        self.addCleanup(state.stop); self.addCleanup(pin.stop)
        self.watcher = W.Watcher(None)

    def test_confirmation_is_not_ais_health(self):
        self.watcher.handle_message(json.dumps({'MessageType': 'SubscriptionConfirmation', 'Message': {'CompressionEnabled': True}}), now=100)
        self.assertEqual(self.watcher.last_msg, 0)
        self.assertEqual(self.watcher.msgs_total, 0)

    def test_any_disconnect_resets_continuous_coverage(self):
        self.watcher.last_msg = 100
        self.watcher.stream_resumed_t = 50
        with patch.object(W.time, 'time', return_value=110):
            self.watcher.reset_coverage()
        self.assertEqual(self.watcher.last_msg, 0)
        self.assertEqual(self.watcher.stream_resumed_t, 110)
        message = {'MessageType': 'ShipStaticData', 'MetaData': {'MMSI': 123456789}, 'Message': {'ShipStaticData': {}}}
        self.watcher.handle_message(json.dumps(message), now=120)
        self.assertEqual(self.watcher.stream_resumed_t, 120)

    def test_real_deflate_binary_confirmation_and_subscription(self):
        subscriptions = []
        completed = threading.Event()
        def handler(ws):
            subscriptions.append(json.loads(ws.recv(timeout=3)))
            ws.send(json.dumps({'MessageType': 'SubscriptionConfirmation', 'Message': {'CompressionEnabled': True}}).encode())
            ws.send(json.dumps({'MessageType': 'ShipStaticData', 'MetaData': {'MMSI': 123456789}, 'Message': {'ShipStaticData': {}}}).encode())
            completed.wait(3)
        server = serve(handler, '127.0.0.1', 0, compression='deflate')
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        original = self.watcher.handle_message
        def handle(raw):
            original(raw)
            self.watcher.stop.set()
        self.watcher.handle_message = handle
        try:
            port = server.socket.getsockname()[1]
            with connect(f'ws://127.0.0.1:{port}', compression='deflate', proxy=None) as ws:
                self.assertEqual(self.watcher.consume_stream(ws, {'APIKey': 'offline-test', 'BoundingBoxes': W.DEFAULT_BBOX}), 1)
                self.assertTrue(self.watcher.compression_enabled)
            self.assertEqual(subscriptions[0]['APIKey'], 'offline-test')
        finally:
            completed.set()
            server.shutdown()
            thread.join(5)

    def test_unconfirmed_or_unnegotiated_compression_fails_closed(self):
        from types import SimpleNamespace
        for enabled, extension in [(False, 'permessage-deflate'), (True, '')]:
            class Fake:
                response = SimpleNamespace(headers={'Sec-WebSocket-Extensions': extension})
                def send(self, message):
                    pass
                def recv(self, timeout):
                    self.timeout = timeout
                    return json.dumps({'MessageType': 'SubscriptionConfirmation', 'Message': {'CompressionEnabled': enabled}})
            fake = Fake()
            with self.assertRaises(RuntimeError):
                self.watcher.consume_stream(fake, {})
            self.assertEqual(fake.timeout, 3)
            self.assertFalse(self.watcher.connected)
            self.assertEqual(self.watcher.last_msg, 0)

if __name__ == '__main__':
    unittest.main()
