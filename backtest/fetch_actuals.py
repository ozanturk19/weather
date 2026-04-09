"""
Faz 0 — Actual observed temperatures fetcher (Iowa State IEM ASOS)

Iowa State Mesonet ASOS arşivi kullanarak geçmiş günlerdeki gerçek
gözlenen maksimum sıcaklıkları çeker. Sınırsız geçmişe erişim sağlar.

API: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
- CSV formatı, UTC zaman damgası
- METAR'dan türetilmiş, günlük maks yerel saate göre hesaplanır

Kullanım:
    python3 fetch_actuals.py --days 60

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
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

STATIONS = {
    "eglc": {"icao": "EGLC", "tz": "Europe/London"},
    "ltac": {"icao": "LTAC", "tz": "Europe/Istanbul"},
    "limc": {"icao": "LIMC", "tz": "Europe/Rome"},
    "ltfm": {"icao": "LTFM", "tz": "Europe/Istanbul"},
    "lemd": {"icao": "LEMD", "tz": "Europe/Madrid"},
    "lfpg": {"icao": "LFPG", "tz": "Europe/Paris"},
}

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
DATA_FILE = Path(__file__).parent / "data" / "actuals.json"
MIN_READINGS = 6   # günlük en az 6 METAR okuma


async def fetch_station(
    client: httpx.AsyncClient,
    station: str,
    date_start: datetime,
    date_end: datetime,
) -> dict:
    """Iowa IEM'den tek istasyon için CSV çek, yerel günlük maks hesapla."""
    cfg = STATIONS[station]
    icao = cfg["icao"]
    tz = ZoneInfo(cfg["tz"])

    params = {
        "station":  icao,
        "data":     "tmpf",          # Fahrenheit sıcaklık
        "tz":       "UTC",
        "format":   "onlycomma",     # CSV, başlık satırı yok
        "latlon":   "no",
        "elev":     "no",
        "missing":  "M",
        "trace":    "T",
        "direct":   "no",
        "report_type": "1",          # sadece METAR (routine + special)
        "year1":  date_start.year,
        "month1": date_start.month,
        "day1":   date_start.day,
        "year2":  date_end.year,
        "month2": date_end.month,
        "day2":   date_end.day,
    }

    try:
        r = await client.get(IEM_URL, params=params, timeout=60)
        if not r.is_success:
            print(f"  ⚠ {station}: HTTP {r.status_code}")
            return {}
        text = r.text
    except Exception as e:
        print(f"  ⚠ {station}: {e}")
        return {}

    # CSV parse: station,valid(UTC),tmpf
    date_temps: dict = {}   # {local_date: [temps_celsius]}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 3:
            continue
        # Başlık satırlarını atla
        if row[0].strip().lower() in ("station", "#") or row[1].strip().lower() == "valid":
            continue
        valid_str = row[1].strip()
        tmpf_str  = row[2].strip()
        if tmpf_str in ("M", "T", "", "tmpf"):
            continue
        try:
            tmpf = float(tmpf_str)
        except ValueError:
            continue
        # Fahrenheit → Celsius
        tmpc = round((tmpf - 32) / 1.8, 1)
        # UTC → yerel saat
        try:
            dt_utc = datetime.strptime(valid_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dt_local = dt_utc.astimezone(tz)
        date_str = dt_local.strftime("%Y-%m-%d")
        date_temps.setdefault(date_str, []).append(tmpc)

    result = {}
    for date_str, temps in date_temps.items():
        if len(temps) >= MIN_READINGS:
            result[date_str] = {
                "max_temp": round(max(temps), 1),
                "readings": len(temps),
            }

    return result


async def main(days: int):
    now_utc = datetime.now(timezone.utc)
    # Dünü son gün al (bugün henüz tamamlanmadı)
    date_end   = (now_utc - timedelta(days=1)).replace(hour=23, minute=59)
    date_start = (now_utc - timedelta(days=days)).replace(hour=0, minute=0)

    print(f"📅 {date_start.date()} → {date_end.date()} ({days} gün) Iowa IEM ASOS")

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
            data = await fetch_station(client, station, date_start, date_end)
            actuals.setdefault(station, {}).update(data)

            dates_found = sorted(data.keys())
            if dates_found:
                print(f"  ✓ {len(dates_found)} gün: {dates_found[0]} → {dates_found[-1]}")
                sample = [(d, data[d]["max_temp"]) for d in dates_found[-5:]]
                for d, t in sample:
                    print(f"    {d}: {t}°C  ({data[d]['readings']} okuma)")
            else:
                print(f"  ⚠ veri yok (IEM'de istasyon kaydı eksik olabilir)")

            await asyncio.sleep(1.0)  # IEM rate limit

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(actuals, indent=2, sort_keys=True))

    total = sum(len(v) for v in actuals.values())
    print(f"\n✅ Kaydedildi: {DATA_FILE}")
    print(f"📊 Toplam: {total} gün-istasyon kaydı")
    for s in STATIONS:
        cnt = len(actuals.get(s, {}))
        print(f"   {s.upper()}: {cnt} gün")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60, help="Kaç gün geriye (IEM sınırsız)")
    args = parser.parse_args()
    asyncio.run(main(args.days))
