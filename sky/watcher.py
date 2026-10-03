#!/usr/bin/env python3
"""No-AI sky alerts. Run from any directory; all private state stays beside this module."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from runtime import load_config, webhook_config, in_notify_window, atomic_write_json, append_jsonl
from sky import forecast as F
from delivery import Outbox

HERE = Path(__file__).resolve().parent
log = logging.getLogger('sky')
UTC = timezone.utc
ZONE = ZoneInfo('America/New_York')


def due(event, now):
    view = datetime.fromtimestamp(event['view_at'], ZONE)
    if event['kind'] in ('sunrise', 'sunset'):
        return 75 * 60 <= event['view_at'] - now.timestamp() <= 90 * 60
    local = now.astimezone(ZONE)
    # "Night before" means the preceding local calendar evening, including for evening planets.
    return (local.date() == view.date() - timedelta(days=1)
            and 18 * 60 <= local.hour * 60 + local.minute <= 21 * 60 + 30)


def post(payload):
    outbox = Outbox(HERE / 'delivery.sqlite')
    try:
        return outbox.deliver(payload)
    finally:
        outbox.close()


class Engine:
    def __init__(self, state_path, settings, send=post, candidate_path=None):
        self.path = Path(state_path)
        self.settings = settings
        self.send = send
        self.delivery = None
        self.candidate_path = candidate_path
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'sent': {}, 'pending': {}}

    def qualify(self, kind, weather):
        score = F.sun_score(weather) if kind in ('sunrise', 'sunset') else None
        good = weather is not None and (score >= self.settings.get(kind + '_threshold',
            65 if kind == 'sunrise' else 75) if score is not None else F.clear(weather, self.settings))
        return score, good

    def refresh(self, item, event, weather, score, fetched_at):
        item['payload'].update(event)
        item['payload'].update(view_at=datetime.fromtimestamp(event['view_at'], ZONE).isoformat(),
            score=score, weather=weather, forecast_at=datetime.fromtimestamp(fetched_at, UTC).isoformat())
        item['expires'] = event['view_at'] - 75 * 60 if event['kind'] in ('sunrise', 'sunset') else event['view_at']
        item['forecast_at'] = fetched_at

    def diagnostics(self):
        return {'failed_pending': sum(bool(item.get('last_error')) for item in self.state['pending'].values()),
                'failed_expired': self.state.get('failed_expired', []),
                'receivers': self.delivery.diagnostics() if self.delivery else {}}

    def save(self):
        atomic_write_json(self.path, self.state)

    def cancel(self, key):
        if self.send is post:
            self.ledger().cancel(key)
        del self.state['pending'][key]

    def ledger(self):
        self.delivery = self.delivery or Outbox(self.path.with_name('delivery.sqlite'))
        return self.delivery

    def step(self, data, fetched_at, events, now):
        stamp = now.timestamp()
        if self.send is post:
            self.ledger().sweep(stamp)  # reconcile disabled/expired jobs even without eligible weather
        # Never turn missing or old forecasts into optimistic alerts.
        if 0 <= stamp - fetched_at <= 2 * 3600:
            for original in events:
                event = dict(original)
                if event['kind'] == 'iss':
                    # Orbital root solvers and fresh elements shift times slightly. Preserve occurrence identity.
                    records = list(self.state['sent'].items()) + [(key, item['payload']) for key, item in self.state['pending'].items()]
                    for key, record in records:
                        if not isinstance(record, dict) or record.get('kind') != 'iss':
                            continue
                        previous = record.get('view_at')
                        previous = datetime.fromisoformat(previous).timestamp() if isinstance(previous, str) else previous
                        if previous is not None and abs(previous - event['view_at']) <= 15 * 60:
                            event['id'] = key
                            break
                weather = F.sample(data, event['view_at'])
                if event['id'] in self.state['pending']:
                    item = self.state['pending'][event['id']]
                    score, good = self.qualify(event['kind'], weather)
                    if good:
                        self.refresh(item, event, weather, score, fetched_at)
                    else:
                        self.cancel(event['id'])
                    continue
                if not due(event, now) or event['id'] in self.state['sent']:
                    continue
                kind = event['kind']
                score, good = self.qualify(kind, weather)
                if self.candidate_path:
                    append_jsonl(self.candidate_path, {'id': event['id'], 'ts': now.isoformat(),
                        'view_at': datetime.fromtimestamp(event['view_at'], ZONE).isoformat(),
                        'kind': kind, 'score': score, 'weather': weather, 'qualified': good})
                if not good:
                    continue
                payload = {**event, 'event': 'sky_' + kind, 'ts': now.isoformat(),
                           'view_at': datetime.fromtimestamp(event['view_at'], ZONE).isoformat(),
                           'score': score, 'weather': weather, 'forecast_source': F.SOURCE,
                           'forecast_at': datetime.fromtimestamp(fetched_at, UTC).isoformat(),
                           'notify': True}
                self.state['pending'][event['id']] = {'payload': payload,
                    'expires': event['view_at'] - 75 * 60 if kind in ('sunrise', 'sunset') else event['view_at'], 'forecast_at': fetched_at}
                self.save()  # durable before first attempted delivery
        for key, item in list(self.state['pending'].items()):
            if stamp > item['expires'] or stamp - item['forecast_at'] > 2 * 3600:
                if item.get('last_error'):
                    self.state.setdefault('failed_expired', []).append({'id': key, 'at': stamp,
                        'error': item['last_error'], 'attempts': item.get('attempts', 0)})
                    log.error('Sky delivery expired: %s (%s)', key, item['last_error'])
                self.cancel(key)
                continue
            if not in_notify_window(stamp):
                continue
            # Recheck weather at delivery when a newer forecast is available.
            payload = item['payload']
            viewing = datetime.fromisoformat(payload['view_at']).timestamp()
            if fetched_at > item['forecast_at'] and 0 <= stamp - fetched_at <= 7200:
                weather = F.sample(data, viewing)
                score, good = self.qualify(payload['kind'], weather)
                if not good:
                    self.cancel(key)
                    continue
                payload.update(weather=weather, score=score,
                               forecast_at=datetime.fromtimestamp(fetched_at, UTC).isoformat())
                item['forecast_at'] = fetched_at
            payload.update(schema_version=1, useful_until=min(item['expires'], item['forecast_at'] + 2 * 3600))
            result = self.ledger().deliver(payload, background=True) if self.send is post else self.send(payload)
            ok, detail = result if isinstance(result, tuple) else (bool(result), 'delivery rejected')
            item['attempts'] = item.get('attempts', 0) + 1
            if not ok and detail != 'awaiting delivery':
                if item.get('last_error') != detail:
                    log.warning('Sky delivery failed: %s (%s)', key, detail)
                item['last_error'] = detail
            if ok:
                self.state['sent'][key] = {'sent_at': stamp, 'view_at': viewing, 'kind': payload['kind'], 'name': payload['name']}
                del self.state['pending'][key]
        self.state['failed_expired'] = [row for row in self.state.get('failed_expired', []) if stamp - row['at'] <= 30 * 86400][-100:]
        self.state['sent'] = {key: value for key, value in self.state['sent'].items() if stamp - (value['sent_at'] if isinstance(value, dict) else value) <= 30 * 86400}
        self.save()


def sun_events(data):
    out = []
    for kind in ('sunrise', 'sunset'):
        for stamp in data.get('daily', {}).get(kind, []):
            if not isinstance(stamp, (int, float)) or stamp <= 0:
                continue
            out.append({'id': f'{kind}:{datetime.fromtimestamp(stamp, ZONE).date().isoformat()}', 'kind': kind, 'name': kind.capitalize(),
                        'view_at': stamp, 'source': F.SOURCE,
                        'notes': 'Uncalibrated forecast heuristic. Local cloud layers do not measure the distant horizon.'})
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    import logging.handlers
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(HERE / 'watcher.log', maxBytes=2_000_000, backupCount=5)
    log.addHandler(handler)
    cfg = load_config()
    settings = cfg.get('sky', {})
    if not settings.get('enabled', True):
        return
    engine = Engine(HERE / 'state.json', settings, candidate_path=HERE / 'candidates.jsonl')
    cached_path = HERE / 'forecast.json'
    cached = json.loads(cached_path.read_text()) if cached_path.exists() else {'data': {}, 'at': 0}
    cached['data'] = F.sanitize(cached['data'])
    atomic_write_json(cached_path, cached)  # remove old location metadata and repair permissions
    astro, celestial, astro_at = None, [], 0
    forecast_attempt, astro_attempt = 0, 0
    while True:
        now = datetime.now(UTC)
        if now.timestamp() - cached['at'] >= 3600 and now.timestamp() - forecast_attempt >= 300:
            forecast_attempt = now.timestamp()
            try:
                data = F.fetch(cfg['pin'])
                cached = {'data': data, 'at': now.timestamp()}
                atomic_write_json(cached_path, cached)
            except Exception as exc:
                log.warning('Forecast unavailable: %s', type(exc).__name__)
        if now.timestamp() - astro_at >= 6 * 3600 and now.timestamp() - astro_attempt >= 300:
            astro_attempt = now.timestamp()
            try:
                from sky.astronomy import Astronomy
                astro = astro or Astronomy(cfg['pin'], HERE / 'cache')
                celestial = astro.events(now, ZONE)
                try:
                    celestial += astro.catalog_events(HERE / 'calendar.json', now, ZONE)
                    catalog = json.loads((HERE / 'calendar.json').read_text())
                    astro.status['catalog'] = 'ok' if catalog['valid_from'] <= now.date().isoformat() <= catalog['valid_until'] else 'expired'
                except Exception as exc:
                    astro.status['catalog'] = type(exc).__name__
                astro_at = now.timestamp()
            except Exception as exc:
                # Sun scoring keeps working when the astronomy data provider is unavailable.
                log.warning('Astronomy unavailable: %s', type(exc).__name__)
                celestial = []
        engine.step(cached['data'], cached['at'], sun_events(cached['data']) + celestial, now)
        atomic_write_json(HERE / 'heartbeat.json', {'ts': now.isoformat(), 'forecast_age_s':
            now.timestamp() - cached['at'], 'astronomy_age_s': now.timestamp() - astro_at,
            'astronomy_status': astro.status if astro else {'provider': 'unavailable'},
            'delivery': engine.diagnostics(), 'pending': len(engine.state['pending']), 'pid': __import__('os').getpid()})
        if args.once:
            break
        time.sleep(60)

if __name__ == '__main__':
    main()
