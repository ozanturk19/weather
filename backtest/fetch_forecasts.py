"""
Faz 0 — Historical model forecasts fetcher

Open-Meteo Previous Runs API kullanarak geçmişte modellerin ne tahmin ettiğini çeker.
Her istasyon × her gün × her model × her horizon için max_temp hesaplar.

Endpoint: https://previous-runs-api.open-meteo.com/v1/forecast
- temperature_2m_previous_day1 → 24 saat önceki run (D+0 tahmini)
- temperature_2m_previous_day2 → 48 saat önceki run (D+1 tahmini)
- temperature_2m_previous_day3 → 72 saat önceki run (D+2 tahmini)

Çıktı: backtest/data/forecasts.json
{
  "eglc": {
    "2026-03-10": {
      "day1": {"gfs": 18.2, "ecmwf": 17.9, ...},
      "day2": {"gfs": 17.5, "ecmwf": 18.1, ...},
      "day3": {"gfs": 16.8, "ecmwf": 17.6, ...}
    },
    ...
  }
}

Kullanım:
    python3 fetch_forecasts.py --days 60
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# Ana projeden istasyonları al
STATIONS = {
    "eglc": {"lat": 51.505, "lon": 0.055,  "tz": "Europe/London"},
    "ltac": {"lat": 40.128, "lon": 32.995, "tz": "Europe/Istanbul"},
    "limc": {"lat": 45.627, "lon": 8.723,  "tz": "Europe/Rome"},
    "ltfm": {"lat": 41.262, "lon": 28.742, "tz": "Europe/Istanbul"},
    "lemd": {"lat": 40.472, "lon": -3.561, "tz": "Europe/Madrid"},
    "lfpg": {"lat": 49.009, "lon": 2.547,  "tz": "Europe/Paris"},
}

MODELS = {
    "gfs":         "gfs_seamless",
    "ecmwf":       "ecmwf_ifs025",
    "icon":        "icon_seamless",
    "ukmo":        "ukmo_seamless",
    "meteofrance": "meteofrance_seamless",
}

HORIZONS = {
    "day1": "temperature_2m_previous_day1",
    "day2": "temperature_2m_previous_day2",
    "day3": "temperature_2m_previous_day3",
}

API = "https://previous-runs-api.open-meteo.com/v1/forecast"
DATA_FILE = Path(__file__).parent / "data" / "forecasts.json"


def daily_max(hourly_temps: list, hourly_times: list, target_date: str, tz_offset: int = 0) -> float | None:
    """Belirli bir tarihin maksimum sıcaklığını hesapla."""
    valid = []
    for i, t in enumerate(hourly_times):
        if t[:10] == target_date and hourly_temps[i] is not None:
            valid.append(hourly_temps[i])
    return round(max(valid), 1) if valid else None


async def fetch_station_model(client: httpx.AsyncClient, station: str, model_key: str, model_id: str,
                               start_date: str, end_date: str) -> dict:
    """Tek istasyon + tek model için tüm horizon verilerini çek."""
    s = STATIONS[station]
    variables = ",".join(HORIZONS.values())

    params = {
        "latitude":  s["lat"],
        "longitude": s["lon"],
        "hourly":    variables,
        "timezone":  s["tz"],
        "models":    model_id,
        "start_date": start_date,
        "end_date":   end_date,
    }

    try:
        r = await client.get(API, params=params, timeout=30)
        if not r.is_success:
            print(f"  ⚠ {station}/{model_key}: HTTP {r.status_code}")
            return {}
        data = r.json()
    except Exception as e:
        print(f"  ⚠ {station}/{model_key}: {e}")
        return {}

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return {}

    # Her horizon için her tarihin max temp'ini çıkar
    result: dict = {}   # {date: {horizon: temp}}

    # Tarih listesi oluştur
    unique_dates = sorted({t[:10] for t in times})

    for horizon_key, var_name in HORIZONS.items():
        temps = hourly.get(var_name)
        if not temps:
            continue
        for date in unique_dates:
            mx = daily_max(temps, times, date)
            if mx is None:
                continue
            result.setdefault(date, {})[horizon_key] = mx

    return result


async def main(days: int):
    end = datetime.now().date()
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    print(f"📅 Tarih aralığı: {start_str} → {end_str} ({days} gün)")
    print(f"📍 İstasyon sayısı: {len(STATIONS)}, Model sayısı: {len(MODELS)}")

    # Mevcut veriyi yükle (varsa)
    forecasts: dict = {}
    if DATA_FILE.exists():
        try:
            forecasts = json.loads(DATA_FILE.read_text())
            print(f"📂 Mevcut veri yüklendi: {sum(len(v) for v in forecasts.values())} kayıt")
        except Exception:
            forecasts = {}

    async with httpx.AsyncClient() as client:
        for station in STATIONS:
            print(f"\n🌍 {station.upper()}")
            forecasts.setdefault(station, {})

            for model_key, model_id in MODELS.items():
                print(f"  → {model_key} ({model_id})")
                data = await fetch_station_model(client, station, model_key, model_id, start_str, end_str)

                # Her tarih için model değerini merge et
                for date, horizons in data.items():
                    forecasts[station].setdefault(date, {})
                    for horizon_key, temp in horizons.items():
                        forecasts[station][date].setdefault(horizon_key, {})
                        forecasts[station][date][horizon_key][model_key] = temp

                await asyncio.sleep(0.5)   # rate limit dostu

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(forecasts, indent=2, sort_keys=True))

    total_records = sum(len(v) for v in forecasts.values())
    print(f"\n✅ Kaydedildi: {DATA_FILE}")
    print(f"📊 Toplam: {total_records} gün-istasyon kaydı")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60, help="Kaç gün geriye (default: 60)")
    args = parser.parse_args()
    asyncio.run(main(args.days))
