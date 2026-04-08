"""
Faz 0 — Historical Polymarket resolved markets fetcher

Geçmiş günler için "highest-temperature-in-{city}-on-{month}-{day}-{year}"
market'ini çeker. Her market için:
- Bucket listesi (eşik, YES/NO fiyatları)
- Kapanış fiyatları (settlement öncesi son snapshot)
- Resolved değer (kazanan bucket)
- Volume ve liquidity

Kullanım:
    python3 fetch_polymarket.py --days 60

Çıktı: backtest/data/polymarket.json
{
  "eglc": {
    "2026-03-10": {
      "slug": "highest-temperature-in-london-on-march-10-2026",
      "resolved": true,
      "winning_bucket": 15,
      "buckets": [
        {
          "title": "14°C",
          "threshold": 14.0,
          "is_below": false,
          "is_above": false,
          "yes_price": 0.08,
          "no_price": 0.92,
          "volume": 5234,
          "liquidity": 1100,
          "resolved": "no"
        },
        ...
      ]
    }
  }
}
"""

import argparse
import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

STATIONS = {
    "eglc": {"pm_query": "London"},
    "ltac": {"pm_query": "Ankara"},
    "limc": {"pm_query": "Milan"},
    "ltfm": {"pm_query": "Istanbul"},
    "lemd": {"pm_query": "Madrid"},
    "lfpg": {"pm_query": "Paris"},
}

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

API = "https://gamma-api.polymarket.com/events"
DATA_FILE = Path(__file__).parent / "data" / "polymarket.json"


def pm_slug(city: str, date_str: str) -> str:
    y, m, d = date_str.split("-")
    return f"highest-temperature-in-{city.lower()}-on-{MONTH_NAMES[int(m) - 1]}-{int(d)}-{y}"


def parse_threshold(title: str) -> Optional[float]:
    m = re.search(r"(-?\d+)\s*°?C", title)
    return float(m.group(1)) if m else None


async def fetch_market(client: httpx.AsyncClient, station: str, date: str) -> Optional[dict]:
    city = STATIONS[station]["pm_query"]
    slug = pm_slug(city, date)

    try:
        r = await client.get(API, params={"slug": slug}, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
        if not r.is_success:
            return None
        events = r.json()
    except Exception as e:
        print(f"  ⚠ {station}/{date}: {e}")
        return None

    if not isinstance(events, list) or not events:
        return None

    event = events[0]
    markets = event.get("markets", [])

    buckets = []
    winning_threshold = None

    for m in markets:
        title = m.get("groupItemTitle", "") or m.get("question", "")
        thresh = parse_threshold(title)

        prices = m.get("outcomePrices", "[]")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []

        yes_price = float(prices[0]) if len(prices) >= 1 else None
        no_price = float(prices[1]) if len(prices) >= 2 else None

        tl = title.lower()
        is_below = any(kw in tl for kw in ("below", "or less", "at most", "not more"))
        is_above = any(kw in tl for kw in ("above", "or more", "at least", "higher", "or greater"))

        # Resolved durumu
        resolved_state = None
        closed = m.get("closed", False)
        if closed:
            # UMA outcome: 1.0 = YES kazandı, 0.0 = NO kazandı
            if yes_price is not None and yes_price > 0.95:
                resolved_state = "yes"
                if thresh is not None and not is_below and not is_above:
                    winning_threshold = thresh
            elif no_price is not None and no_price > 0.95:
                resolved_state = "no"

        buckets.append({
            "title":     title,
            "threshold": thresh,
            "is_below":  is_below,
            "is_above":  is_above,
            "yes_price": round(yes_price, 4) if yes_price is not None else None,
            "no_price":  round(no_price, 4) if no_price is not None else None,
            "liquidity": round(float(m.get("liquidity", 0) or 0)),
            "volume":    round(float(m.get("volume", 0) or 0)),
            "closed":    closed,
            "resolved":  resolved_state,
        })

    buckets.sort(key=lambda x: (x["threshold"] if x["threshold"] is not None else -999))

    return {
        "slug":             slug,
        "title":            event.get("title", ""),
        "resolved":         any(b["closed"] for b in buckets),
        "winning_bucket":   winning_threshold,
        "total_liquidity":  round(float(event.get("liquidity", 0) or 0)),
        "total_volume":     round(float(event.get("volume", 0) or 0)),
        "buckets":          buckets,
    }


async def main(days: int):
    end = datetime.now().date() - timedelta(days=1)  # dünden başla (bugün open olabilir)
    start = end - timedelta(days=days)

    print(f"📅 Tarih aralığı: {start} → {end} ({days} gün)")

    markets: dict = {}
    if DATA_FILE.exists():
        try:
            markets = json.loads(DATA_FILE.read_text())
            print(f"📂 Mevcut veri yüklendi: {sum(len(v) for v in markets.values())} market")
        except Exception:
            markets = {}

    total_fetched = 0
    total_missing = 0

    async with httpx.AsyncClient() as client:
        for station in STATIONS:
            print(f"\n🌍 {station.upper()}")
            markets.setdefault(station, {})

            for day_offset in range(days + 1):
                date = (start + timedelta(days=day_offset)).strftime("%Y-%m-%d")

                # Zaten varsa ve resolved ise atla
                if date in markets[station] and markets[station][date].get("resolved"):
                    continue

                data = await fetch_market(client, station, date)
                if data is None:
                    total_missing += 1
                    continue

                markets[station][date] = data
                total_fetched += 1
                win = data.get("winning_bucket")
                print(f"  ✓ {date}: {len(data['buckets'])} bucket, winner={win}")

                await asyncio.sleep(0.3)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(markets, indent=2, sort_keys=True))

    print(f"\n✅ Kaydedildi: {DATA_FILE}")
    print(f"📊 Yeni: {total_fetched}, Bulunamadı: {total_missing}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(main(args.days))
