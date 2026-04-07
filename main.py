import asyncio
import json
import math
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Polymarket Hava Analizi")

# ── Weather cache: Open-Meteo rate limit koruması ───────────────────────────
# 6 istasyon × 5 model = 30 eşzamanlı istek → HTTP 429.
# Her istasyon sonucunu 10 dk cache'le; ?refresh=true ile bypass edilebilir.
_weather_cache: dict = {}   # {station: {"ts": float, "data": dict}}
CACHE_TTL = 600             # saniye (10 dakika)

STATIONS = {
    "eglc": {"lat": 51.505, "lon": 0.055,  "tz": "Europe/London",   "label": "EGLC (Londra City)",     "pm_query": "London"},
    "ltac": {"lat": 40.128, "lon": 32.995, "tz": "Europe/Istanbul", "label": "LTAC (Ankara Esenboğa)", "pm_query": "Ankara"},
    "limc": {"lat": 45.627, "lon": 8.723,  "tz": "Europe/Rome",     "label": "LIMC (Milano Malpensa)", "pm_query": "Milan"},
    "ltfm": {"lat": 41.262, "lon": 28.742, "tz": "Europe/Istanbul", "label": "LTFM (İstanbul)",        "pm_query": "Istanbul"},
    "lemd": {"lat": 40.472, "lon": -3.561,  "tz": "Europe/Madrid",   "label": "LEMD (Madrid)",   "pm_query": "Madrid",  "settlement": "wu"},
    "lfpg": {"lat": 49.009, "lon": 2.547,  "tz": "Europe/Paris",    "label": "LFPG (Paris)",    "pm_query": "Paris",   "settlement": "wu"},
}

# Her model için Open-Meteo model adı
MODELS = {
    "gfs":         "gfs_seamless",
    "ecmwf":       "ecmwf_ifs025",
    "icon":        "icon_seamless",
    "ukmo":        "ukmo_seamless",
    "meteofrance": "meteofrance_seamless",
}

# Model ağırlıkları: ECMWF küresel en doğru, GFS Asya'da zayıf
MODEL_WEIGHTS = {
    "ecmwf":       2.0,
    "icon":        1.0,
    "gfs":         0.8,
    "ukmo":        0.9,
    "meteofrance": 1.0,
}

# Horizon bazlı belirsizlik eşikleri (ağırlıklı std için)
# D+0 daha sıkı, D+2 daha toleranslı — her gün için (düşük_sınır, orta_sınır)
UNCERTAINTY_THRESHOLDS = {
    0: (0.6, 1.2),   # Bugün   — spread < 0.6 = Düşük, < 1.2 = Orta
    1: (0.8, 1.5),   # Yarın   — spread < 0.8 = Düşük, < 1.5 = Orta
    2: (1.1, 1.8),   # Öbür gün — toleranslı ama gerçek ayrışmayı yakalar
}

# Bias düzeltmesi için minimum gün sayısı
BIAS_MIN_DAYS = 7

KEY_HOURS = ["06:00", "09:00", "12:00", "15:00", "18:00"]


def parse_hourly(data: dict) -> dict:
    """Open-Meteo yanıtını {date: {hours, max_temp}} olarak döndür."""
    h = data.get("hourly", {})
    times = h.get("time", [])
    temps  = h.get("temperature_2m", [None] * len(times))
    precip = h.get("precipitation_probability", [None] * len(times))
    wind   = h.get("wind_speed_10m", h.get("windspeed_10m", [None] * len(times)))

    days: dict = {}
    for i, t in enumerate(times):
        date_str, hour_str = t[:10], t[11:16]
        days.setdefault(date_str, []).append({
            "hour":   hour_str,
            "temp":   temps[i],
            "precip": precip[i],
            "wind":   wind[i],
        })

    result = {}
    for date_str, hours in days.items():
        valid = [h["temp"] for h in hours if h["temp"] is not None]
        result[date_str] = {
            "hours":    hours,
            "max_temp": round(max(valid), 1) if valid else None,
        }
    return result


def blend_day(models_data: dict, horizon: int = 1) -> dict:
    """
    Ağırlıklı blend — 4 iyileştirme:
    1) Adaptif outlier tespiti (2× std, sabit 5°C değil)
    2) Ağırlıklı standart sapma (hi-lo range değil)
    3) Horizon-aware eşikler (D+0/1/2 için farklı tolerans)
    4) Konsensüs skoru (±1°C içindeki model oranı)
    """
    model_maxes = {
        name: v["max_temp"]
        for name, v in models_data.items()
        if v.get("max_temp") is not None
    }
    if not model_maxes:
        return {
            "max_temp": None, "min_max": None, "max_max": None,
            "spread": None, "uncertainty": "?",
            "outliers_removed": [], "models_used": [],
            "consensus_ratio": None, "horizon": horizon,
        }

    values = list(model_maxes.values())
    n_models = len(values)

    # ── Adım 1: Median + MAD tabanlı adaptif outlier tespiti ────────────
    # Neden MAD? Klasik mean+std kendi kendini besler: büyük outlier stdev'i
    # şişirir → threshold büyür → outlier içeride kalır (kısır döngü).
    # Median ve MAD outlier'a karşı sağlam (robust statistics).
    sorted_vals = sorted(values)
    anchor = statistics.median(sorted_vals)         # medyan: outlier etkilemez
    if n_models > 2:
        mads = [abs(v - anchor) for v in sorted_vals]
        mad = statistics.median(mads) or 0          # median absolute deviation
        dynamic_threshold = max(2.0, 2.5 * mad)     # 2.5×MAD ≈ Tukey 1.5×IQR
    else:
        dynamic_threshold = 3.0   # az model varsa sabit güvenlik sınırı

    filtered = {k: v for k, v in model_maxes.items()
                if abs(v - anchor) < dynamic_threshold}
    if len(filtered) < 2:
        filtered = model_maxes   # güvenlik: en az 2 model kalsın
    outliers_removed = [k for k in model_maxes if k not in filtered]

    # ── Adım 2: Ağırlıklı blend ─────────────────────────────────────────
    total_w = sum(MODEL_WEIGHTS.get(k, 1.0) for k in filtered)
    blend   = round(sum(v * MODEL_WEIGHTS.get(k, 1.0) for k, v in filtered.items()) / total_w, 1)

    lo = round(min(filtered.values()), 1)
    hi = round(max(filtered.values()), 1)

    # ── Adım 3: Ağırlıklı standart sapma (hi-lo range değil) ────────────
    # Eski: spread = hi - lo  (tek outlier tüm metriği bozar)
    # Yeni: ECMWF 2x ağırlıklı, gerçek istatistiksel dağılım
    if len(filtered) > 1:
        variance = sum(
            MODEL_WEIGHTS.get(k, 1.0) * (v - blend) ** 2
            for k, v in filtered.items()
        ) / total_w
        spread = round(math.sqrt(variance), 2)
    else:
        spread = 0.0

    # ── Adım 4: Horizon-aware belirsizlik eşiği ─────────────────────────
    # D+2 için D+1'den farklı tolerans — atmosfer fiziğini yansıtır
    low_t, mid_t = UNCERTAINTY_THRESHOLDS.get(min(horizon, 2), (0.8, 1.5))
    if spread < low_t:
        uncertainty = "Düşük"
    elif spread < mid_t:
        uncertainty = "Orta"
    else:
        uncertainty = "Yüksek"

    # ── Konsensüs skoru (bonus metrik) ──────────────────────────────────
    # Kaç model blend'den ±1°C içinde? 1.0 = tam konsensüs
    consensus_count = sum(1 for v in filtered.values() if abs(v - blend) < 1.0)
    consensus_ratio = round(consensus_count / len(filtered), 2)

    # ── Saatlik ağırlıklı blend (outlier modeller hariç) ────────────────
    all_hours: dict = {}
    for model_name, model_day in models_data.items():
        if model_name in outliers_removed:
            continue
        w = MODEL_WEIGHTS.get(model_name, 1.0)
        for h in model_day.get("hours", []):
            if h["temp"] is not None:
                all_hours.setdefault(h["hour"], []).append((h["temp"], w))

    hourly_blend = []
    for hour, tw_list in sorted(all_hours.items()):
        total_wh = sum(w for _, w in tw_list)
        avg_temp = sum(t * w for t, w in tw_list) / total_wh
        hourly_blend.append({
            "hour": hour,
            "temp": round(avg_temp, 1),
            "n":    len(tw_list),
        })

    return {
        "max_temp":         blend,
        "min_max":          lo,
        "max_max":          hi,
        "spread":           spread,
        "uncertainty":      uncertainty,
        "hours":            hourly_blend,
        "outliers_removed": outliers_removed,
        "models_used":      list(filtered.keys()),
        "consensus_ratio":  consensus_ratio,
        "horizon":          horizon,
    }


@app.get("/api/weather")
async def get_weather(station: str, refresh: bool = False):
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    # ── Cache kontrolü ──────────────────────────────────────────────────
    now = time.monotonic()
    cached = _weather_cache.get(station)
    if not refresh and cached and (now - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    s = STATIONS[station]
    base = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m"
        f"&timezone={s['tz']}&forecast_days=3"
    )

    # İlk deneme: tüm modellere paralel istek
    async def fetch_model(client: httpx.AsyncClient, model_id: str):
        try:
            return await client.get(base + f"&models={model_id}")
        except Exception:
            return None

    model_days: dict = {}  # model_name -> {date -> {hours, max_temp}}

    async with httpx.AsyncClient(timeout=25) as client:
        responses = await asyncio.gather(
            *[fetch_model(client, model_id) for model_id in MODELS.values()],
            return_exceptions=True,
        )

        # Her model için parse et
        failed_models = []
        for model_name, model_id, resp in zip(MODELS.keys(), MODELS.values(), responses):
            if isinstance(resp, Exception) or resp is None or not resp.is_success:
                failed_models.append((model_name, model_id))
                continue
            try:
                model_days[model_name] = parse_hourly(resp.json())
            except Exception:
                failed_models.append((model_name, model_id))
                continue

        # Başarısız modeller için tek tek retry (timeout=30s)
        if failed_models and not model_days:
            for model_name, model_id in failed_models:
                try:
                    r = await client.get(base + f"&models={model_id}", timeout=30)
                    if r.is_success:
                        model_days[model_name] = parse_hourly(r.json())
                except Exception:
                    continue

    if not model_days:
        raise HTTPException(status_code=502, detail="Hiçbir modelden veri alınamadı")

    # Tüm tarihleri topla
    all_dates = sorted({d for days in model_days.values() for d in days})

    days_result = {}
    for i, date in enumerate(all_dates):
        per_model = {
            name: days[date]
            for name, days in model_days.items()
            if date in days
        }
        days_result[date] = {
            "models": per_model,
            "blend":  blend_day(per_model, horizon=min(i, 2)),
        }

    # Bias düzeltmesi: predictions.json'dan sistematik hatayı hesapla
    preds = _load_preds()
    bias_entries = []
    for date_key, e in sorted(preds.get(station, {}).items()):
        if e.get("blend") is not None and e.get("actual") is not None:
            bias_entries.append(e["blend"] - e["actual"])

    recent_bias = bias_entries[-7:]
    bias_active  = len(recent_bias) >= BIAS_MIN_DAYS
    bias_correction = 0.0
    if bias_active:
        w       = [2 ** i for i in range(len(recent_bias))]
        w_bias  = sum(err * wi for err, wi in zip(recent_bias, w)) / sum(w)
        mae     = sum(abs(e) for e in recent_bias) / len(recent_bias)
        bias_correction = round(max(-mae, min(mae, w_bias)), 2)

    for date_key in days_result:
        b = days_result[date_key]["blend"]
        b["bias_count"]       = len(recent_bias)
        b["bias_active"]      = bias_active
        b["bias_correction"]  = round(-bias_correction, 2)   # blend'e eklenecek miktar
        if b["max_temp"] is not None:
            b["bias_corrected_blend"] = round(b["max_temp"] - bias_correction, 1)
        else:
            b["bias_corrected_blend"] = None

    result = {"station": station, "days": days_result}
    _weather_cache[station] = {"ts": time.monotonic(), "data": result}
    return result


@app.get("/api/weather/cache-clear")
async def clear_weather_cache(station: str = ""):
    """Cache'i temizle — tüm istasyonlar veya tek istasyon."""
    if station:
        _weather_cache.pop(station.lower(), None)
        return {"cleared": station}
    _weather_cache.clear()
    return {"cleared": "all"}


@app.get("/api/ensemble")
async def get_ensemble(station: str):
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    s = STATIONS[station]
    url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m"
        f"&models=icon_seamless"
        f"&timezone={s['tz']}&forecast_days=3"
    )

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)

    if not r.is_success:
        raise HTTPException(status_code=502, detail="Ensemble verisi alınamadı")

    hourly = r.json().get("hourly", {})
    times  = hourly.get("time", [])
    member_keys = sorted(k for k in hourly if k.startswith("temperature_2m_member"))

    if not member_keys:
        raise HTTPException(status_code=502, detail="Ensemble üyeleri bulunamadı")

    # Her tarih için her üyenin saatlik templerini topla
    date_member: dict = {}   # {date: {member_key: [temps]}}
    for key in member_keys:
        vals = hourly[key]
        for i, t in enumerate(times):
            d = t[:10]
            v = vals[i]
            if v is None:
                continue
            date_member.setdefault(d, {}).setdefault(key, []).append(v)

    def pct(sorted_vals: list, p: float) -> float:
        n   = len(sorted_vals)
        idx = p / 100 * (n - 1)
        lo  = int(idx)
        hi  = min(lo + 1, n - 1)
        return round(sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo]), 1)

    days = {}
    for date, members in date_member.items():
        maxes = sorted(max(temps) for temps in members.values() if temps)
        if not maxes:
            continue
        n = len(maxes)
        days[date] = {
            "member_maxes": [round(m, 1) for m in maxes],
            "count":  n,
            "mean":   round(sum(maxes) / n, 1),
            "p10":    pct(maxes, 10),
            "p25":    pct(maxes, 25),
            "p50":    pct(maxes, 50),
            "p75":    pct(maxes, 75),
            "p90":    pct(maxes, 90),
        }

    return {"station": station, "days": days}


@app.get("/api/metar")
async def get_metar_obs(station: str):
    """
    Son 24 saatin METAR gözlemlerini döndür.
    Wunderground airport station = METAR verisi olduğundan,
    bugünün gözlenen maks'ı = settlement değeridir.
    """
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    s    = STATIONS[station]
    icao = station.upper()
    url  = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours=24"

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)

    if not r.is_success:
        raise HTTPException(status_code=502, detail="METAR alınamadı")

    data = r.json()
    if not isinstance(data, list) or not data:
        return {"station": station, "observations": [], "current_temp": None,
                "day_max": None, "day_date": None, "current_time": None}

    tz          = ZoneInfo(s["tz"])
    local_today = datetime.now(tz=tz).strftime("%Y-%m-%d")

    obs_list = []
    for obs in data:
        temp     = obs.get("temp")
        obs_time = obs.get("obsTime")  # Unix epoch (seconds)
        if temp is None or obs_time is None:
            continue
        dt_local = datetime.fromtimestamp(obs_time, tz=timezone.utc).astimezone(tz)
        obs_list.append({
            "time":       dt_local.strftime("%H:%M"),
            "date":       dt_local.strftime("%Y-%m-%d"),
            "temp":       round(float(temp), 1),
            "epoch":      obs_time,
        })

    obs_list.sort(key=lambda x: x["epoch"], reverse=True)

    current_temp = obs_list[0]["temp"] if obs_list else None
    current_time = obs_list[0]["time"] if obs_list else None

    today_temps = [o["temp"] for o in obs_list if o["date"] == local_today]
    day_max     = max(today_temps, default=None)

    # Son 12 gözlem (sparkline için)
    recent = [{"time": o["time"], "temp": o["temp"]} for o in obs_list[:12]]

    return {
        "station":      station,
        "current_temp": current_temp,
        "current_time": current_time,
        "day_max":      day_max,
        "day_date":     local_today,
        "observations": recent,
    }


@app.get("/api/taf")
async def get_taf(icao: str):
    url = f"https://aviationweather.gov/api/data/taf?ids={icao.upper()}&format=raw"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
    if not r.is_success:
        raise HTTPException(status_code=502, detail="TAF alınamadı")
    return {"icao": icao.upper(), "taf": r.text.strip()}


MONTH_NAMES = [
    "january","february","march","april","may","june",
    "july","august","september","october","november","december"
]

def pm_slug(city: str, date_str: str) -> str:
    """'2026-03-23' → 'highest-temperature-in-milan-on-march-23-2026'"""
    y, m, d = date_str.split("-")
    return f"highest-temperature-in-{city}-on-{MONTH_NAMES[int(m)-1]}-{int(d)}-{y}"

def parse_threshold(title: str) -> Optional[float]:
    """'13°C' → 13.0  |  '10°C or below' → 10.0  |  '16°C or above' → 16.0"""
    m = re.search(r'(-?\d+)\s*°C', title)
    return float(m.group(1)) if m else None

@app.get("/api/polymarket")
async def get_polymarket_markets(station: str, date: str):
    """
    Slug pattern: highest-temperature-in-{city}-on-{month}-{day}-{year}
    Her sıcaklık derecesi ayrı YES/NO market → olasılık dağılımı.
    """
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    city = STATIONS[station]["pm_query"].lower()
    slug = pm_slug(city, date)
    url  = f"https://gamma-api.polymarket.com/events?slug={slug}"

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})

    if not r.is_success:
        raise HTTPException(status_code=502, detail="Polymarket verisi alınamadı")

    events = r.json()
    if not isinstance(events, list) or not events:
        return {"station": station, "date": date, "slug": slug, "buckets": [], "event_url": f"https://polymarket.com/event/{slug}"}

    event    = events[0]
    markets  = event.get("markets", [])

    buckets = []
    for m in markets:
        title = m.get("groupItemTitle", "") or m.get("question", "")
        thresh = parse_threshold(title)

        prices = m.get("outcomePrices", "[]")
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []

        yes_price = round(float(prices[0]), 4) if len(prices) >= 1 else None
        no_price  = round(float(prices[1]), 4) if len(prices) >= 2 else None

        tl = title.lower()
        is_below = "below" in tl or "or less" in tl or "at most" in tl or "not more" in tl
        is_above = "above" in tl or "or more" in tl or "at least" in tl or "higher" in tl or "or greater" in tl

        buckets.append({
            "title":     title,
            "threshold": thresh,
            "is_below":  is_below,
            "is_above":  is_above,
            "yes_price": yes_price,
            "no_price":  no_price,
            "liquidity": round(float(m.get("liquidity", 0) or 0)),
            "volume":    round(float(m.get("volume",    0) or 0)),
            "condition_id": m.get("conditionId"),
        })

    # Eşiğe göre sırala
    buckets.sort(key=lambda x: (x["threshold"] or -999))

    return {
        "station":   station,
        "date":      date,
        "slug":      slug,
        "title":     event.get("title", ""),
        "liquidity": round(float(event.get("liquidity", 0) or 0)),
        "volume":    round(float(event.get("volume",    0) or 0)),
        "event_url": f"https://polymarket.com/event/{slug}",
        "buckets":   buckets,
    }




PREDICTIONS_FILE = Path(__file__).parent / "predictions.json"
_preds_lock = asyncio.Lock()

def _load_preds() -> dict:
    try:
        return json.loads(PREDICTIONS_FILE.read_text()) if PREDICTIONS_FILE.exists() else {}
    except Exception:
        return {}

def _save_preds(data: dict):
    tmp = PREDICTIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(PREDICTIONS_FILE)


@app.get("/api/metar-history")
async def get_metar_history(station: str):
    """Son 7 günün METAR maks sıcaklıkları + lineer trend."""
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    s    = STATIONS[station]
    icao = station.upper()
    url  = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours=168"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
        data = r.json() if r.is_success else []
    except Exception:
        data = []

    if not isinstance(data, list):
        data = []

    tz = ZoneInfo(s["tz"])
    date_temps: dict = {}
    for obs in data:
        temp     = obs.get("temp")
        obs_time = obs.get("obsTime")
        if temp is None or obs_time is None:
            continue
        dt_local = datetime.fromtimestamp(obs_time, tz=timezone.utc).astimezone(tz)
        d = dt_local.strftime("%Y-%m-%d")
        date_temps.setdefault(d, []).append(float(temp))

    daily_maxes = []
    for d in sorted(date_temps):
        temps = date_temps[d]
        if len(temps) >= 6:          # En az 6 gözlem olan günler
            daily_maxes.append({"date": d, "max_temp": round(max(temps), 1), "readings": len(temps)})

    # Lineer trend hesabı (son 5 geçerli gün)
    valid = daily_maxes[-5:]
    trend_slope, trend_dir = 0.0, "→"
    if len(valid) >= 3:
        vals = [v["max_temp"] for v in valid]
        n = len(vals)
        x_mean = (n - 1) / 2
        y_mean = sum(vals) / n
        num = sum((i - x_mean) * (vals[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        trend_slope = round(num / den, 2) if den else 0.0
        trend_dir = "↑" if trend_slope > 0.4 else "↓" if trend_slope < -0.4 else "→"

    return {
        "station":     station,
        "daily_maxes": daily_maxes,
        "trend_slope": trend_slope,
        "trend_dir":   trend_dir,
    }


class PredictionLog(BaseModel):
    station: str
    date: str
    blend: Optional[float] = None
    p50:   Optional[float] = None
    actual: Optional[float] = None


@app.post("/api/log-prediction")
async def log_prediction(req: PredictionLog):
    """Günlük tahmin kaydını sakla (otomatik bias hesabı için)."""
    async with _preds_lock:
        preds = _load_preds()
        st    = req.station.lower()
        preds.setdefault(st, {})
        entry = preds[st].setdefault(req.date, {})
        if req.blend  is not None: entry["blend"]  = req.blend
        if req.p50    is not None: entry["p50"]    = req.p50
        if req.actual is not None: entry["actual"] = req.actual
        _save_preds(preds)
    return {"ok": True}


@app.get("/api/prediction-bias")
async def get_prediction_bias(station: str):
    """Son 7 günün blend–gerçek farkından sistematik bias hesapla."""
    station = station.lower()
    preds   = _load_preds()
    entries = []
    for date, e in sorted(preds.get(station, {}).items()):
        if e.get("blend") is not None and e.get("actual") is not None:
            entries.append({
                "date":   date,
                "blend":  e["blend"],
                "actual": e["actual"],
                "error":  round(e["blend"] - e["actual"], 1),
            })
    recent = entries[-7:]
    if not recent:
        return {"station": station, "bias_7d": 0.0, "mae": 0.0, "count": 0,
                "bias_active": False, "entries": []}

    # Üstel ağırlıklı ortalama: en yeni gün en yüksek ağırlık (2^i)
    # [eski ... yeni] → weights [1, 2, 4, 8, ...]
    weights = [2 ** i for i in range(len(recent))]
    w_bias  = sum(x["error"] * w for x, w in zip(recent, weights)) / sum(weights)

    # MAE (basit ortalama — overcorrection sınırı için)
    mae = sum(abs(x["error"]) for x in recent) / len(recent)

    # Bias'ı MAE ile sınırla: aşırı düzeltmeyi önle
    bias = round(max(-mae, min(mae, w_bias)), 2)

    return {
        "station":    station,
        "bias_7d":    bias,
        "mae":        round(mae, 2),
        "count":      len(recent),
        "bias_active": len(recent) >= BIAS_MIN_DAYS,
        "entries":    entries[-14:],
    }


@app.get("/api/portfolio")
async def get_portfolio(address: str):
    if not address or len(address) < 10:
        raise HTTPException(status_code=400, detail="Geçersiz cüzdan adresi")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(
                "https://data-api.polymarket.com/positions",
                params={"user": address, "sizeThreshold": "0.01"},
            )
            resp.raise_for_status()
            all_pos = resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"Polymarket hatası: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bağlantı hatası: {str(e)}")
    weather = sorted(
        [
            p for p in all_pos
            if "highest-temperature" in p.get("eventSlug", "")
            and float(p.get("size", 0)) >= 0.5
            and float(p.get("currentValue", 0)) > 0
        ],
        key=lambda p: float(p.get("currentValue", 0)),
        reverse=True,
    )
    return {"positions": weather, "address": address, "total_markets": len(all_pos)}


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(
        "static/index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )

app.mount("/static", StaticFiles(directory="static"), name="static")
