#!/usr/bin/env python3
"""Run a watcher in the process owning its lock and supervisor lifetime pipe."""
import fcntl
import os
from pathlib import Path
import runpy
import select
import signal
import sys
import threading
import time


def run(path):
    with path.with_name('process.lock').open('a') as lock:
        while True:
            if select.select([sys.stdin], [], [], .1)[0] and not sys.stdin.buffer.read(1):
                return 0
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                continue

        def parent_lifetime():
            # Nobody writes data: EOF means the supervisor is gone.
            sys.stdin.buffer.read()
            os.kill(os.getpid(), signal.SIGTERM)
            # A worker with a graceful signal handler gets a bounded shutdown interval.
            time.sleep(5)
            os._exit(143)

        threading.Thread(target=parent_lifetime, daemon=True).start()
        sys.argv = [str(path)]
        runpy.run_path(str(path), run_name='__main__')
    return 0

if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1])))
