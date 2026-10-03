"""Shared private configuration, notification policy and durable file helpers."""
from __future__ import annotations
from datetime import datetime
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
CONFIG_PATH = ROOT / "config.local.json"
ENV_FILE = ROOT / ".env.local"
ALERT_WINDOW = (5 * 60 + 15, 21 * 60 + 30)

def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(f"missing {CONFIG_PATH.name}; copy config.example.json and fill in the pin")
    cfg = json.loads(CONFIG_PATH.read_text())
    pin = cfg.get("pin") or {}
    for key, bound in (("lat", 90), ("lon", 180)):
        value = pin.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > bound:
            raise SystemExit("config.local.json requires valid numeric pin lat/lon")
    return cfg


def in_notify_window(timestamp):
    local = datetime.fromtimestamp(timestamp, ET)
    minute = local.hour * 60 + local.minute
    return ALERT_WINDOW[0] <= minute <= ALERT_WINDOW[1]


def now_et():
    return datetime.now(ET)


def iso_et(ts=None):
    dt = datetime.fromtimestamp(ts, ET) if ts is not None else now_et()
    return dt.isoformat(timespec="seconds")


def hhmm_et(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def atomic_write_json(path: Path, obj):
    """Atomic replacement with a private random temporary file, including existing paths."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(obj, output, indent=1, default=str)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def append_jsonl(path: Path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(obj, default=str) + "\n")


def read_env_file():
    out = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            out[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def webhook_config():
    """(url, key) from env HEADSUP_WEBHOOK_URL / HEADSUP_WEBHOOK_KEY, then .env.local."""
    filev = read_env_file()
    url = os.environ.get("HEADSUP_WEBHOOK_URL") or filev.get("HEADSUP_WEBHOOK_URL")
    key = os.environ.get("HEADSUP_WEBHOOK_KEY") or filev.get("HEADSUP_WEBHOOK_KEY")
    return url, key


def aisstream_key():
    return os.environ.get("AISSTREAM_API_KEY") or read_env_file().get("AISSTREAM_API_KEY")
