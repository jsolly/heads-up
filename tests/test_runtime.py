"""Private runtime cache replacement, including repair of pre-existing public modes."""
import json
from pathlib import Path
import tempfile
import unittest
from runtime import atomic_write_json
from sky.forecast import sanitize

class PrivateCache(unittest.TestCase):
    def test_replacement_strips_location_metadata_and_repairs_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'forecast.json'
            path.write_text('{}')
            path.chmod(0o644)
            atomic_write_json(path, {'data': sanitize({'latitude': 39.95, 'longitude': -75.13,
                'elevation': 10, 'hourly': {'time': [1]}, 'daily': {'sunrise': [1]}}), 'at': 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(json.loads(path.read_text())['data']), {'hourly', 'daily'})
            self.assertEqual(list(Path(temp).glob('*.tmp')), [])
