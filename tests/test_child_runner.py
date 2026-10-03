"""Assert wrapper exit status and stderr while the parent's pipe stays open."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / 'child_runner.py'


class ChildShutdown(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hu-child-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.children = []
        self.addCleanup(self.clean_children)

    def clean_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream and not stream.closed:
                    stream.close()

    def start(self, code, root=None):
        root = root or self.root
        worker = root / 'worker.py'
        worker.write_text(code)
        child = subprocess.Popen([sys.executable, str(RUNNER), str(worker)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def ready(self, child):
        self.assertTrue(select.select([child.stdout], [], [], 3)[0], 'worker did not become ready')
        self.assertEqual(child.stdout.readline().strip(), 'ready')

    def exited(self, child, code, timeout=3):
        # Do not communicate() here: closing stdin would hide the original defect.
        self.assertEqual(child.wait(timeout=timeout), code)
        self.assertEqual(child.stderr.read(), '')

    def test_normal_return_keeps_parent_pipe_open(self):
        child = self.start('import time\nprint("ready", flush=True)\ntime.sleep(.1)\n')
        self.ready(child)
        self.exited(child, 0)
        self.assertFalse(child.stdin.closed)

    def test_system_exit_preserves_zero_and_nonzero_status(self):
        for code in (0, 7):
            with self.subTest(code=code):
                root = self.root / str(code)
                root.mkdir()
                child = self.start(f'print("ready", flush=True)\nraise SystemExit({code})\n', root)
                self.ready(child)
                self.exited(child, code)
                self.assertFalse(child.stdin.closed)

    def test_sigterm_handler_exits_cleanly_with_open_parent_pipe(self):
        child = self.start('import signal,time\ndef stop(*args):\n raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\nprint("ready",flush=True)\nwhile True: time.sleep(.1)\n')
        self.ready(child)
        child.terminate()
        self.exited(child, 0)
        self.assertFalse(child.stdin.closed)

    def test_sigterm_default_handler_preserves_signal_exit(self):
        child = self.start('import time\nprint("ready",flush=True)\nwhile True: time.sleep(.1)\n')
        self.ready(child)
        child.terminate()
        self.exited(child, -signal.SIGTERM)
        self.assertFalse(child.stdin.closed)

    def test_parent_eof_runs_graceful_handler(self):
        child = self.start('import signal,time\ndef stop(*args):\n raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\nprint("ready",flush=True)\nwhile True: time.sleep(.1)\n')
        self.ready(child)
        child.stdin.close()
        self.exited(child, 0)

    def test_parent_eof_forces_uncooperative_worker_after_five_seconds(self):
        child = self.start('import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint("ready",flush=True)\nwhile True: time.sleep(.1)\n')
        self.ready(child)
        started = time.monotonic()
        child.stdin.close()
        self.exited(child, 143, timeout=7)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 4.9)
        self.assertLess(elapsed, 7)

    def test_abrupt_supervisor_death_closes_parent_pipe(self):
        root = self.root / 'abrupt'
        root.mkdir()
        worker = root / 'worker.py'
        worker.write_text('import signal,time\nfrom pathlib import Path\ndef stop(*args):\n raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\nPath(__file__).with_name("ready").touch()\nwhile True: time.sleep(.1)\n')
        # This helper owns the pipe; the test owns both helper and wrapper and reaps
        # the wrapper itself. Killing the helper must close its sole writer.
        read_fd, write_fd = os.pipe()
        # Let the owned helper inherit the sole lifetime writer.
        helper = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True: time.sleep(.1)\n'], pass_fds=(write_fd,))
        self.children.append(helper)
        child = subprocess.Popen([sys.executable, str(RUNNER), str(worker)], stdin=read_fd,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        os.close(read_fd)
        os.close(write_fd)
        deadline = time.monotonic() + 3
        while not (root / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((root / 'ready').exists())
        helper.kill()
        helper.wait(timeout=3)
        self.exited(child, 0)

    def test_parent_eof_while_waiting_for_process_lock(self):
        owner = self.start('import time\nprint("ready",flush=True)\nwhile True: time.sleep(.1)\n')
        self.ready(owner)
        waiting = self.start('raise RuntimeError("worker must not execute")\n')
        waiting.stdin.close()
        self.exited(waiting, 0)
        self.assertIsNone(owner.poll())

if __name__ == '__main__':
    unittest.main()
