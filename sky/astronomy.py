"""Local visibility from Skyfield. Ephemeris and ISS elements are cached privately."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
SOURCE = 'https://rhodesmill.org/skyfield/'
TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE'


def event(kind, name, when, source, notes, **extra):
    identity = when.date().isoformat() if kind != 'iss' else when.isoformat(timespec='seconds')
    return {'id': f'{kind}:{name}:{identity}', 'kind': kind, 'name': name,
            'view_at': when.timestamp(), 'source': source, 'notes': notes, **extra}


class Astronomy:
    def __init__(self, pin, cache):
        from skyfield.api import Loader, wgs84
        self.cache = Path(cache)
        self.load = Loader(str(self.cache))
        self.ts = self.load.timescale()
        self.eph = self.load('de421.bsp')
        self.site = wgs84.latlon(pin['lat'], pin['lon'])
        self.observer = self.eph['earth'] + self.site
        self.pin = pin
        self.status = {}

    def altitude(self, body, time):
        alt, az, _ = self.observer.at(time).observe(body).apparent().altaz()
        return float(alt.degrees), float(az.degrees)

    def visible(self, body, time, minimum=15, darkness=True):
        return (self.altitude(body, time)[0] >= minimum
                and (not darkness or self.altitude(self.eph['sun'], time)[0] <= -6))

    def events(self, now, zone):
        from skyfield import almanac, eclipselib
        start, end = self.ts.from_datetime(now), self.ts.from_datetime(now + timedelta(days=3))
        out = []
        # Daily evening planet opportunities, restricted to bright naked-eye planets.
        for day in range(4):
            local = now.astimezone(zone).replace(hour=20, minute=30, second=0, microsecond=0) + timedelta(days=day)
            if local <= now:
                continue
            time = self.ts.from_datetime(local)
            for name, key in [('Venus', 'venus'), ('Mars', 'mars'), ('Jupiter', 'jupiter barycenter'), ('Saturn', 'saturn barycenter')]:
                body = self.eph[key]
                if self.visible(body, time):
                    alt, az = self.altitude(body, time)
                    out.append(event('planet', name, local, SOURCE, 'Viewing opportunity; not a rare alignment.',
                                     altitude_deg=round(alt), azimuth_deg=round(az)))
        times, phases = almanac.find_discrete(start, end, almanac.moon_phases(self.eph))
        for time, phase in zip(times, phases):
            if phase != 2:
                continue
            distance = self.eph['earth'].at(time).observe(self.eph['moon']).distance().km
            if distance > 360000:
                continue
            # View on the local evening of the full moon, rather than at a daytime maximum.
            local = time.utc_datetime().astimezone(zone).replace(hour=20, minute=30, second=0, microsecond=0)
            viewing = self.ts.from_datetime(local)
            if local > now and self.visible(self.eph['moon'], viewing):
                out.append(event('supermoon', 'Full moon near perigee', local, SOURCE,
                                 'HeadsUp definition: full moon at Earth-center distance <=360,000 km.',
                                 distance_km=round(float(distance))))
        times, types, _ = eclipselib.lunar_eclipses(start, end, self.eph)
        for time, typ in zip(times, types):
            if self.visible(self.eph['moon'], time, minimum=5):
                name = ('Penumbral', 'Partial', 'Total')[int(typ)] + ' lunar eclipse'
                out.append(event('eclipse', name, time.utc_datetime(), 'https://eclipse.gsfc.nasa.gov/lunar.html',
                                 'Time is greatest eclipse; Moon must be above the local horizon.'))
        # An ISS failure must not suppress planets or other events.
        try:
            out.extend(self.iss(now, start, end))
            self.status["iss"] = "ok"
        except Exception as exc:
            # Record provider failure without discarding independently computed events.
            self.status["iss"] = type(exc).__name__
        return out

    def iss(self, now, start, end):
        import requests
        from skyfield.api import EarthSatellite
        path = self.cache / 'iss.tle'
        if not path.exists() or now.timestamp() - path.stat().st_mtime > 6 * 3600:
            response = requests.get(TLE_URL, timeout=20)
            response.raise_for_status()
            lines = response.text.strip().splitlines()
            if len(lines) != 3 or not lines[1].startswith('1 25544') or not lines[2].startswith('2 25544'):
                raise ValueError('invalid ISS elements')
            path.write_text(response.text)
        lines = path.read_text().strip().splitlines()
        sat = EarthSatellite(lines[1], lines[2], lines[0], self.ts)
        if abs(now.timestamp() - sat.epoch.utc_datetime().timestamp()) > 3 * 86400:
            raise ValueError('stale ISS elements')
        times, codes = sat.find_events(self.site, start, end, altitude_degrees=30)
        out = []
        for time, code in zip(times, codes):
            if code == 1 and sat.at(time).is_sunlit(self.eph) and self.altitude(self.eph['sun'], time)[0] <= -6:
                alt, az, _ = (sat - self.site).at(time).altaz()
                out.append(event('iss', 'ISS pass', time.utc_datetime(), TLE_URL,
                                 'Sunlit pass above 30 degrees; time is maximum elevation.',
                                 altitude_deg=round(float(alt.degrees)), azimuth_deg=round(float(az.degrees))))
        return out

    def catalog_events(self, catalog, now, zone):
        data = json.loads(Path(catalog).read_text())
        if not data['valid_from'] <= now.date().isoformat() <= data['valid_until']:
            return []
        out = []
        lat, lon = self.pin['lat'], self.pin['lon']
        for row in data['events']:
            south, west, north, east = row['region']
            if not south <= lat <= north or not west <= lon <= east:
                continue
            when = datetime.fromisoformat(row['view_at'])
            if when.tzinfo is None:
                raise ValueError('catalog viewing times must have timezone offsets')
            if not now < when <= now + timedelta(days=3):
                continue
            time = self.ts.from_datetime(when)
            if row['kind'] == 'meteor_shower':
                from skyfield.api import Star
                target = Star(ra_hours=row['radiant_ra_hours'], dec_degrees=row['radiant_dec_degrees'])
                if not self.visible(target, time, minimum=20):
                    continue
            elif row['kind'] == 'eclipse':
                # Curated solar eclipses require local contact times and a narrow verified region.
                if not self.visible(self.eph['sun'], time, minimum=5, darkness=False):
                    continue
            else:
                raise ValueError('unsupported catalog kind')
            out.append(event(row['kind'], row['name'], when, row['source'], row['notes']))
        return out
