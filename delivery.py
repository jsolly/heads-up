"""Private durable event ledger. HTTP acceptance is distinct from notification delivery."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from runtime import in_notify_window, read_env_file, webhook_config


def destinations():
    """Names only in config; endpoint credentials remain injected environment values."""
    values = {**read_env_file(), **os.environ}
    names = values.get('HEADSUP_DESTINATIONS')
    if names is None:
        url, key = webhook_config()
        return {'legacy': (url, key)} if url and key else {}
    result = {}
    if len(names.split(",")) > 8:
        raise ValueError("at most eight destinations supported")
    for name in filter(None, (part.strip() for part in names.split(','))):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', name) or name in result:
            raise ValueError('invalid or duplicate destination name')
        prefix = 'HEADSUP_' + name.upper()
        url, key = values.get(prefix + '_URL'), values.get(prefix + '_KEY')
        if not url or not key:
            raise ValueError('destination requires injected URL and key')
        result[name] = (url, key)
    return result


def http_send(endpoint, payload):
    import requests
    url, key = endpoint
    try:
        response = requests.post(url, json=payload, timeout=15, stream=True, headers={
            'Authorization': f'Bearer {key}', 'Idempotency-Key': payload['id'],
            'User-Agent': 'heads-up/3.0'})
        try:
            return 200 <= response.status_code < 300, f'HTTP {response.status_code}'
        finally:
            response.close()
    except requests.RequestException as exc:
        return False, type(exc).__name__


class Outbox:
    """One process owns each database; SQLite serializes producer and sender threads.

    Enabled destinations are snapshotted at publication. Removing a destination
    cancels its pending records; adding one never backfills historical events.
    Receivers must deduplicate IDs: a crash after HTTP success can repeat a POST.
    """
    def __init__(self, path, resolve=destinations, send=http_send):
        self.path = Path(path)
        self.resolve, self.send = resolve, send
        self.lock = threading.RLock()
        self.in_flight = set()
        self.workers = {}
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                event_id TEXT NOT NULL, destination TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, detail TEXT,
                PRIMARY KEY (event_id, destination));
        ''')
        enabled = self.resolve()
        with self.db:
            for (name,) in self.db.execute("SELECT DISTINCT destination FROM deliveries WHERE status='pending'").fetchall():
                if name not in enabled:
                    self.db.execute("UPDATE deliveries SET status='cancelled' WHERE destination=? AND status='pending'", (name,))

    def close(self):
        for thread in list(self.workers.values()):
            thread.join(timeout=15)
        # A misbehaving network peer can exceed an inactivity timeout. Its daemon
        # must retain the database until process exit, rather than use a closed DB.
        if not any(thread.is_alive() for thread in self.workers.values()):
            self.db.close()

    def publish(self, payload):
        with self.lock:
            if self.db.execute('SELECT 1 FROM events WHERE id=?', (payload['id'],)).fetchone():
                return True, 'already queued'
        enabled = self.resolve()
        body = json.dumps(payload, sort_keys=True, allow_nan=False)
        with self.lock, self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?)',
                                     (payload['id'], body, payload['useful_until']))
            if cursor.rowcount:
                self.db.executemany('INSERT INTO deliveries (event_id, destination) VALUES (?, ?)',
                                    [(payload['id'], name) for name in enabled])
        return True, 'durably queued'

    def cancel(self, event_id):
        with self.lock, self.db:
            self.db.execute("UPDATE deliveries SET status='cancelled' WHERE event_id=? AND status='pending'", (event_id,))

    def sweep(self, now):
        enabled = self.resolve()
        with self.lock, self.db:
            for key, name, expires in self.db.execute("""SELECT e.id, d.destination, e.expires
                    FROM events e JOIN deliveries d ON e.id=d.event_id WHERE d.status='pending'""").fetchall():
                if name not in enabled or now >= expires:
                    status = 'cancelled' if name not in enabled else 'expired'
                    self.db.execute("UPDATE deliveries SET status=? WHERE event_id=? AND destination=?", (status, key, name))
        self.prune(now)

    def drain(self, now=None, event_id=None, background=False):
        started = time.monotonic()
        self.sweep(time.time() if now is None else now)
        with self.lock:
            rows = self.db.execute("""SELECT e.id, e.body, e.expires, d.destination,
                d.attempts, d.retry_at FROM events e JOIN deliveries d ON e.id=d.event_id
                WHERE d.status='pending' AND (? IS NULL OR e.id=?)""", (event_id, event_id)).fetchall()
        # One worker per destination: a stalled receiver never blocks another's POST
        # or the producer's publication transaction. HTTP timeouts measure inactivity.
        def worker(name):
            for key, body, expires, destination, attempts, retry_at in rows:
                if destination != name:
                    continue
                stamp = time.time() if now is None else now + time.monotonic() - started
                marker = (key, name)
                with self.lock:
                    if marker in self.in_flight:
                        continue
                    current = self.db.execute('SELECT status FROM deliveries WHERE event_id=? AND destination=?', marker).fetchone()
                    if current != ('pending',):
                        continue
                    self.in_flight.add(marker)
                try:
                    payload = json.loads(body)
                    enabled = self.resolve()  # recheck changes after earlier network waits
                    status, detail = None, None
                    if name not in enabled or not payload.get('notify', True):
                        status = 'cancelled'
                    elif stamp >= expires:
                        status = 'expired'
                    elif stamp >= retry_at and in_notify_window(stamp):
                        try:
                            ok, detail = self.send(enabled[name], payload)
                        except Exception as exc:
                            ok, detail = False, type(exc).__name__
                        status = 'accepted' if ok else 'pending'
                        attempts += 1
                    if status is not None:
                        with self.lock, self.db:
                            self.db.execute("""UPDATE deliveries SET status=?, attempts=?, retry_at=?, detail=?
                                WHERE event_id=? AND destination=? AND status='pending'""",
                                (status, attempts, stamp + min(300, 5 * 2 ** min(max(attempts - 1, 0), 6)), detail, key, name))
                finally:
                    with self.lock:
                        self.in_flight.discard(marker)
        names = {row[3] for row in rows}
        if background:
            with self.lock:
                for name in names:
                    previous = self.workers.get(name)
                    if previous is None or not previous.is_alive():
                        thread = threading.Thread(target=worker, args=(name,), daemon=True)
                        self.workers[name] = thread
                        thread.start()
        elif names:
            with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
                list(pool.map(worker, names))
        self.prune(time.time() if now is None else now)

    def prune(self, now):
        # IDs remain longer than maximum useful lifetime. Never delete active work.
        with self.lock, self.db:
            keys = self.db.execute("""SELECT id FROM events WHERE expires < ? AND NOT EXISTS
                (SELECT 1 FROM deliveries WHERE event_id=events.id AND status='pending')""",
                (now - 30 * 86400,)).fetchall()
            self.db.executemany('DELETE FROM deliveries WHERE event_id=?', keys)
            self.db.executemany('DELETE FROM events WHERE id=?', keys)

    def deliver(self, payload, now=None, background=False):
        ok, detail = self.publish(payload)
        if not ok:
            return ok, detail
        # Sky must dispatch only the occurrence it has just requalified.
        # Ship's independent sender uses drain() to sweep its full ledger.
        self.drain(now, payload['id'], background=background)
        with self.lock:
            statuses = self.db.execute('SELECT status, detail FROM deliveries WHERE event_id=?', (payload['id'],)).fetchall()
        pending = [detail or 'awaiting delivery' for status, detail in statuses if status == 'pending']
        if any(status == 'expired' for status, _ in statuses):
            return False, 'delivery expired'
        return not pending, pending[0] if pending else 'destinations terminal'

    def diagnostics(self):
        with self.lock:
            return dict(self.db.execute('SELECT status, COUNT(*) FROM deliveries GROUP BY status').fetchall())
