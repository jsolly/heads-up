"""Exercise the real supervisor with isolated dummy children and no secrets/network."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


class Supervision(unittest.TestCase):
    def test_slow_stop_then_ensure_waits_for_lock_release(self):
        with tempfile.TemporaryDirectory(prefix='hu-', dir='/private/tmp' if sys.platform == 'darwin' else '/tmp') as temp:
            root = Path(temp)
            for filename in ('supervisor.py', 'child_runner.py'):
                shutil.copy(ROOT / filename, root / filename)
            (root / 'config.local.json').write_text(json.dumps({'sky': {'enabled': False}}))
            (root / 'ships').mkdir()
            (root / 'ships/watcher.py').write_text('import time,signal\ndef stop(*args):\n time.sleep(6)\n raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\nwhile True: time.sleep(.1)\n')
            def cli(command):
                return subprocess.run([sys.executable, str(root / 'supervisor.py'), command],
                                      capture_output=True, text=True, timeout=20)
            try:
                first = cli('ensure')
                self.assertEqual(first.returncode, 0)
                first_pid = json.loads(first.stdout)['supervisor_pid']
                time.sleep(.3)
                os.kill(first_pid, signal.SIGTERM)
                time.sleep(.1)
                replacement = cli('ensure')
                self.assertEqual(replacement.returncode, 0, replacement.stderr)
                self.assertNotEqual(json.loads(replacement.stdout)['supervisor_pid'], first_pid)
                time.sleep(.3)
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(root / 'control.sock'))
                    client.sendall(b'stop')
                    self.assertEqual(client.recv(32), b'stopping')
                self.assertEqual(cli('stop').returncode, 0)
                self.assertFalse((root / 'control.sock').exists())
                result = cli('ensure')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cli('status').returncode, 0)
            finally:
                self.assertEqual(cli('stop').returncode, 0)
                self.assertFalse((root / 'control.sock').exists())
            child_output = (root / 'ships/supervisor.log').read_text()
            self.assertNotIn('Fatal Python error', child_output)
            self.assertNotIn('_enter_buffered_busy', child_output)
            self.assertEqual(child_output, '')

    def test_concurrent_start_child_restart_and_safe_stop(self):
        with tempfile.TemporaryDirectory(prefix='hu-', dir='/private/tmp' if sys.platform == 'darwin' else '/tmp') as temp:
            root = Path(temp)
            shutil.copy(ROOT / 'supervisor.py', root / 'supervisor.py')
            shutil.copy(ROOT / 'child_runner.py', root / 'child_runner.py')
            (root / 'config.local.json').write_text(json.dumps({'sky': {'enabled': True}}))
            for name in ('ships', 'sky'):
                (root / name).mkdir()
                (root / name / 'watcher.py').write_text('import time,os\nfrom pathlib import Path\nPath(__file__).with_name("actual.pid").write_text(str(os.getpid()))\ntime.sleep(120)\n')
            def request(command):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(str(root / 'control.sock'))
                    client.sendall(command.encode())
                    return client.recv(4096).decode()
            def ensure():
                return subprocess.run([sys.executable, str(root / 'supervisor.py'), 'ensure'],
                                      capture_output=True, text=True, timeout=20)
            try:
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(lambda _: ensure(), range(2)))
                self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
                statuses = [json.loads(r.stdout) for r in results]
                self.assertEqual(statuses[0]['supervisor_pid'], statuses[1]['supervisor_pid'])
                status = json.loads(request('status'))
                self.assertEqual(set(status['children']), {'ships', 'sky'})
                deadline = time.monotonic() + 3
                while not all((root / name / 'actual.pid').exists() for name in ('ships','sky')) and time.monotonic() < deadline:
                    time.sleep(.05)
                old = status['children']['ships']
                actual_old = {name: int((root / name / 'actual.pid').read_text()) for name in ('ships','sky')}
                os.kill(old, signal.SIGKILL)  # this test owns this child
                deadline = time.monotonic() + 14
                while time.monotonic() < deadline:
                    current = json.loads(request('status'))
                    if current['children'].get('ships', old) != old:
                        break
                    time.sleep(.1)
                else:
                    self.fail('ship child did not restart')
                self.assertEqual(current['children']['sky'], status['children']['sky'])
                deadline = time.monotonic() + 3
                while int((root / 'ships/actual.pid').read_text()) == actual_old['ships'] and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertNotEqual(int((root / 'ships/actual.pid').read_text()), actual_old['ships'])
                with self.assertRaises(ProcessLookupError):
                    os.kill(actual_old['ships'], 0)
                # A hard supervisor crash must not leave workers running alongside replacements.
                actual_before = {name: int((root / name / 'actual.pid').read_text()) for name in ('ships','sky')}
                os.kill(current['supervisor_pid'], signal.SIGKILL)
                self.assertEqual(ensure().returncode, 0)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    actual_after = {name: int((root / name / 'actual.pid').read_text()) for name in ('ships','sky')}
                    if all(actual_after[name] != actual_before[name] for name in actual_before):
                        break
                    time.sleep(.05)
                else:
                    self.fail('crashed supervisor did not release and restart actual workers')
                for pid in actual_before.values():
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                current = json.loads(request('status'))
                pids = list(current['children'].values()) + list(actual_after.values())
                self.assertEqual(request('stop'), 'stopping')
                deadline = time.monotonic() + 3
                while (root / 'control.sock').exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse((root / 'control.sock').exists())
                for pid in pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                try:
                    request('stop')
                except OSError:
                    pass
                deadline = time.monotonic() + 3
                while (root / 'control.sock').exists() and time.monotonic() < deadline:
                    time.sleep(.05)

if __name__ == '__main__':
    unittest.main()
