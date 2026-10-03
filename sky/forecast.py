"""Open-Meteo forecasts in UTC, with conservative missing-data handling."""
import math

HOURLY = ('cloud_cover', 'cloud_cover_low', 'cloud_cover_mid', 'cloud_cover_high',
          'visibility', 'precipitation')
SOURCE = 'https://open-meteo.com/en/docs'


def fetch(pin):
    import requests
    response = requests.get('https://api.open-meteo.com/v1/forecast', params={
        'latitude': pin['lat'], 'longitude': pin['lon'], 'hourly': ','.join(HOURLY),
        'daily': 'sunrise,sunset', 'timezone': 'UTC', 'timeformat': 'unixtime',
        'forecast_days': 4}, timeout=20)
    response.raise_for_status()
    return sanitize(response.json())


def sanitize(data):
    # Provider grid coordinates/elevation are unnecessary and reveal the observer area.
    return {key: data.get(key, {}) for key in ("hourly", "daily")}


def sample(forecast, timestamp):
    hourly = forecast.get('hourly', {})
    times = hourly.get('time', [])
    if not times:
        return None
    index = min(range(len(times)), key=lambda i: abs(times[i] - timestamp))
    if abs(times[index] - timestamp) > 1800:
        return None
    result = {}
    for key in HOURLY:
        values = hourly.get(key, [])
        if index >= len(values) or not isinstance(values[index], (int, float)):
            return None
        value = values[index]
        if not math.isfinite(value) or value < 0 or ('cloud' in key and value > 100):
            return None
        result[key] = value
    return result


def sun_score(weather):
    """An uncalibrated viewing heuristic, not a probability of a colorful sky."""
    if weather is None or weather['precipitation'] > .1 or weather['visibility'] < 5000:
        return 0
    # Some middle/high cloud reflects light; thick cover and low cloud obstruct it.
    high, mid, low = (weather['cloud_cover_' + layer] for layer in ('high', 'mid', 'low'))
    texture = max(0, 1 - abs(high - 45) / 55) * 25 + max(0, 1 - abs(mid - 30) / 70) * 15
    haze = max(0, 1 - weather['visibility'] / 20000) * 25
    overcast = max(0, weather['cloud_cover'] - 80) * 2
    return round(max(0, min(100, 55 + texture - low * .8 - haze - overcast)))


def clear(weather, settings):
    return (weather is not None and weather['cloud_cover'] <= settings.get('max_cloud_cover', 30)
            and weather['visibility'] >= settings.get('min_visibility_m', 10000)
            and weather['precipitation'] <= .1)
