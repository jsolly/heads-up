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


def run(path):
    # A buffered stdin read in a background thread holds CPython's I/O lock
    # during finalization. Own a raw duplicate instead, then stop and join it.
    parent_fd = os.dup(sys.stdin.fileno())
    finished = threading.Event()
    monitor = None
    try:
        with path.with_name('process.lock').open('a') as lock:
            while True:
                if select.select([parent_fd], [], [], .1)[0] and not os.read(parent_fd, 4096):
                    return 0
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    continue

            def parent_lifetime():
                while not finished.is_set():
                    if not select.select([parent_fd], [], [], .1)[0]:
                        continue
                    if os.read(parent_fd, 4096):
                        continue
                    if finished.is_set():
                        return
                    # EOF means supervisor death. Preserve bounded worker shutdown.
                    os.kill(os.getpid(), signal.SIGTERM)
                    if not finished.wait(5):
                        os._exit(143)
                    return

            monitor = threading.Thread(target=parent_lifetime, name='parent_lifetime')
            monitor.start()
            try:
                sys.argv = [str(path)]
                runpy.run_path(str(path), run_name='__main__')
            finally:
                finished.set()
                monitor.join()  # no buffered I/O or unbounded read remains at finalization
    finally:
        finished.set()
        if monitor is not None:
            monitor.join()
        os.close(parent_fd)
    return 0

if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1])))
