"""
Faz 0 — Actual observed temperatures fetcher (METAR)

Aviationweather.gov METAR API kullanarak her istasyonun geçmiş günlerdeki
gerçek gözlenen maksimum sıcaklıklarını çeker.

API: https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=N

Maks API penceresi ~720 saat (30 gün). Daha fazlası için ogimet veya
Iowa State IEM Asos gerekebilir. Şimdilik 30 gün yeterli.

Kullanım:
    python3 fetch_actuals.py --days 30

Çıktı: backtest/data/actuals.json
{
  "eglc": {
    "2026-03-10": {"max_temp": 14.5, "readings": 23},
    ...
  }
}
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

STATIONS = {
    "eglc": {"tz": "Europe/London"},
    "ltac": {"tz": "Europe/Istanbul"},
    "limc": {"tz": "Europe/Rome"},
    "ltfm": {"tz": "Europe/Istanbul"},
    "lemd": {"tz": "Europe/Madrid"},
    "lfpg": {"tz": "Europe/Paris"},
}

API = "https://aviationweather.gov/api/data/metar"
DATA_FILE = Path(__file__).parent / "data" / "actuals.json"


async def fetch_station(client: httpx.AsyncClient, station: str, hours: int) -> dict:
    """Tek istasyon için METAR gözlemlerini tarih bazında gruple."""
    icao = station.upper()
    try:
        r = await client.get(API, params={"ids": icao, "format": "json", "hours": hours}, timeout=30)
        if not r.is_success:
            print(f"  ⚠ {station}: HTTP {r.status_code}")
            return {}
        data = r.json()
    except Exception as e:
        print(f"  ⚠ {station}: {e}")
        return {}

    if not isinstance(data, list):
        return {}

    tz = ZoneInfo(STATIONS[station]["tz"])
    date_temps: dict = {}   # {local_date: [temps]}

    for obs in data:
        temp = obs.get("temp")
        obs_time = obs.get("obsTime")
        if temp is None or obs_time is None:
            continue
        dt_local = datetime.fromtimestamp(obs_time, tz=timezone.utc).astimezone(tz)
        date_str = dt_local.strftime("%Y-%m-%d")
        date_temps.setdefault(date_str, []).append(float(temp))

    result = {}
    for date_str, temps in date_temps.items():
        if len(temps) >= 6:  # en az 6 gözlem (gün içi yeterli örneklem)
            result[date_str] = {
                "max_temp": round(max(temps), 1),
                "readings": len(temps),
            }
    return result


async def main(days: int):
    hours = days * 24
    print(f"📅 Son {days} gün ({hours} saat) METAR verisi çekiliyor")

    actuals: dict = {}
    if DATA_FILE.exists():
        try:
            actuals = json.loads(DATA_FILE.read_text())
            print(f"📂 Mevcut veri yüklendi: {sum(len(v) for v in actuals.values())} kayıt")
        except Exception:
            actuals = {}

    async with httpx.AsyncClient() as client:
        for station in STATIONS:
            print(f"\n🌍 {station.upper()}")
            data = await fetch_station(client, station, hours)
            actuals.setdefault(station, {}).update(data)

            dates_found = sorted(data.keys())
            if dates_found:
                print(f"  ✓ {len(dates_found)} gün: {dates_found[0]} → {dates_found[-1]}")
            else:
                print(f"  ⚠ veri yok")

            await asyncio.sleep(0.5)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(actuals, indent=2, sort_keys=True))

    total = sum(len(v) for v in actuals.values())
    print(f"\n✅ Kaydedildi: {DATA_FILE}")
    print(f"📊 Toplam: {total} gün-istasyon kaydı")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Kaç gün geriye (max 30 API limiti)")
    args = parser.parse_args()
    if args.days > 30:
        print("⚠ API limiti 30 gün, otomatik sınırlıyorum")
        args.days = 30
    asyncio.run(main(args.days))
