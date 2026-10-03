#!/usr/bin/env python3
"""One lock-owning supervisor, controlled through a local Unix socket."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SOCKET = ROOT / 'control.sock'


def request(command):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(SOCKET))
        client.sendall(command.encode())
        response = client.recv(4096).decode()
        if command == 'status' and response == 'stopping':
            raise OSError('supervisor is stopping')
        return response


def ensure():
    try:
        print(request('status'))
        return 0
    except OSError:
        pass
    deadline = time.monotonic() + 15
    process = None
    while time.monotonic() < deadline:
        if process is None or process.poll() is not None:
            if process is not None and process.returncode not in (0, None):
                print('supervisor failed; inspect supervisor.log', file=sys.stderr)
                return 1
            with (ROOT / 'supervisor.log').open('ab') as output:
                process = subprocess.Popen([sys.executable, str(Path(__file__)), 'run'],
                                           cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=output, start_new_session=True)
        try:
            print(request('status'))
            return 0
        except OSError:
            time.sleep(.1)
    print('supervisor did not become ready; inspect supervisor.log', file=sys.stderr)
    return 1


def run():
    with (ROOT / 'supervisor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        cfg = json.loads((ROOT / 'config.local.json').read_text())
        children = {}
        paths = {'ships': ROOT / 'ships' / 'watcher.py'}
        if cfg.get('sky', {}).get('enabled', True):
            paths['sky'] = ROOT / 'sky' / 'watcher.py'
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        SOCKET.unlink(missing_ok=True)
        logs = {name: (path.parent / 'supervisor.log').open('ab') for name, path in paths.items()}
        try:
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(SOCKET))
                SOCKET.chmod(0o600)
                server.listen(8)
                server.settimeout(.5)
                restart_at = {}
                while not stopping:
                    for name, path in paths.items():
                        child = children.get(name)
                        if child and child.poll() is not None:
                            print(f'{name} exited {child.returncode}; retry in 10s', flush=True)
                            if child.stdin:
                                child.stdin.close()
                            children.pop(name)
                            restart_at[name] = time.monotonic() + 10
                        if name not in children and time.monotonic() >= restart_at.get(name, 0):
                            children[name] = subprocess.Popen([sys.executable, str(ROOT / 'child_runner.py'), str(path)], cwd=ROOT,
                                                              stdin=subprocess.PIPE, stdout=logs[name], stderr=logs[name])
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        conn.settimeout(1)
                        try:
                            command = conn.recv(32).decode()
                            if command == 'stop':
                                stopping = True
                                conn.sendall(b'stopping')
                            elif stopping:
                                conn.sendall(b'stopping')
                            elif command == 'status':
                                conn.sendall(json.dumps({'supervisor_pid': os.getpid(),
                                    'children': {name: p.pid for name, p in children.items() if p.poll() is None}}).encode())
                            else:
                                conn.sendall(b'unknown command')
                        except (OSError, UnicodeError):
                            pass
        finally:
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            deadline = time.monotonic() + 10
            for child in children.values():
                try:
                    child.wait(timeout=max(.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            SOCKET.unlink(missing_ok=True)
            for child in children.values():
                if child.stdin:
                    child.stdin.close()
            for output in logs.values():
                output.close()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['ensure', 'run', 'stop', 'status'])
    args = parser.parse_args()
    if args.command == 'ensure':
        return ensure()
    if args.command == 'run':
        return run()
    try:
        print(request(args.command))
    except OSError:
        if args.command != 'stop':
            return 1
    if args.command == 'stop':
        deadline = time.monotonic() + 12
        while SOCKET.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if SOCKET.exists():
            print('supervisor shutdown timed out', file=sys.stderr)
            return 1
        print('stopped')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
