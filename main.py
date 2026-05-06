from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Polymarket Hava Analizi")

# ── API Token (trading endpoint koruma) ─────────────────────────────────────
_API_TOKEN = os.getenv("API_TOKEN", "")

def require_token(x_api_token: str = Header(default="")):
    if _API_TOKEN and x_api_token != _API_TOKEN:
        raise HTTPException(status_code=403, detail="Geçersiz API token")

# ── Weather cache: Open-Meteo rate limit koruması ───────────────────────────
# 6 istasyon × 5 model = 30 eşzamanlı istek → HTTP 429.
# Her istasyon sonucunu 10 dk cache'le; ?refresh=true ile bypass edilebilir.
_weather_cache: dict = {}   # {station: {"ts": float, "data": dict}}
CACHE_TTL = 600             # saniye (10 dakika)

# ── Ensemble cache ───────────────────────────────────────────────────────────
# Ensemble verisi (ICON 40 + ECMWF 50 + AIFS 50 = 140 üye) her istasyon için
# 3 ayrı Open-Meteo isteği gerektirir. 14 istasyon × 3 = 42 istek/yükleme.
# 20 dk cache → dashboard yeniden yüklemede anlık yanıt.
_ensemble_cache: dict = {}
ENSEMBLE_CACHE_TTL = 1200   # saniye (20 dakika)

# ── METAR history cache ──────────────────────────────────────────────────────
# 7 günlük METAR geçmişi nadir değişir; 30 dk cache dashboard'u hızlandırır.
_metar_history_cache: dict = {}
METAR_HISTORY_CACHE_TTL = 1800  # saniye (30 dakika)

# Open-Meteo eşzamanlı istek sınırı: 12 istasyon × 6 model = 72 req → HTTP 429.
# Semaphore ile aynı anda max 5 istek → rate limit aşılmaz (AIFS eklendi).
_openmeteo_sem = asyncio.Semaphore(5)

STATIONS = {
    "eglc": {"lat": 51.505, "lon": 0.055,  "tz": "Europe/London",      "label": "EGLC (Londra City)",        "pm_query": "London"},
    "ltac": {"lat": 40.128, "lon": 32.995, "tz": "Europe/Istanbul",    "label": "LTAC (Ankara Esenboğa)",    "pm_query": "Ankara"},
    "limc": {"lat": 45.627, "lon": 8.723,  "tz": "Europe/Rome",        "label": "LIMC (Milano Malpensa)",    "pm_query": "Milan"},
    "ltfm": {"lat": 41.262, "lon": 28.742, "tz": "Europe/Istanbul",    "label": "LTFM (İstanbul)",           "pm_query": "Istanbul"},
    "lemd": {"lat": 40.472, "lon": -3.561, "tz": "Europe/Madrid",      "label": "LEMD (Madrid)",             "pm_query": "Madrid",    "settlement": "wu"},
    "lfpg": {"lat": 49.009, "lon": 2.547,  "tz": "Europe/Paris",       "label": "LFPG (Paris)",              "pm_query": "Paris",     "settlement": "wu"},
    "eham": {"lat": 52.308, "lon": 4.764,  "tz": "Europe/Amsterdam",   "label": "EHAM (Amsterdam Schiphol)", "pm_query": "Amsterdam", "settlement": "wu"},
    "eddm": {"lat": 48.354, "lon": 11.786, "tz": "Europe/Berlin",      "label": "EDDM (Münih)",              "pm_query": "Munich",    "settlement": "wu"},
    "epwa": {"lat": 52.166, "lon": 20.967, "tz": "Europe/Warsaw",      "label": "EPWA (Varşova Chopin)",     "pm_query": "Warsaw",    "settlement": "wu"},
    "efhk": {"lat": 60.317, "lon": 24.963, "tz": "Europe/Helsinki",    "label": "EFHK (Helsinki Vantaa)",    "pm_query": "Helsinki",  "settlement": "wu"},
    "omdb": {"lat": 25.253,  "lon":  55.364,  "tz": "Asia/Dubai",         "label": "OMDB (Dubai Intl)",          "pm_query": "Dubai",       "settlement": "wu"},
    "rjtt": {"lat": 35.5494, "lon": 139.7798,"tz": "Asia/Tokyo",        "label": "RJTT (Tokyo Haneda)",        "pm_query": "Tokyo",       "settlement": "wu"},
    # Asya Faz 11 — JMA/KMA modelleri ile (STATION_MODEL_CONFIG'a bakınız)
    "rksi": {"lat": 37.4602, "lon": 126.4407,"tz": "Asia/Seoul",        "label": "RKSI (Seoul Incheon)",       "pm_query": "Seoul",       "settlement": "wu"},
    "vhhh": {"lat": 22.3080, "lon": 113.9185,"tz": "Asia/Hong_Kong",    "label": "VHHH (HK Intl)",             "pm_query": "Hong Kong",   "settlement": "wu"},
}

# Her model için Open-Meteo model adı
# NOT: AIFS ID'leri dokümanda yanlış bildirilmiş.
#   Forecast API  : "ecmwf_aifs025_single"  (tek-üye deterministik koşum)
#   Ensemble API  : "ecmwf_aifs025"          (51 üyeli dağılım)
# Yalın "ecmwf_aifs025" forecast API'de 200 döner ama tüm değerler None —
# doğru ID 'ecmwf_aifs025_single' (2026-04 itibarıyla live).
MODELS = {
    "gfs":         "gfs_seamless",
    "ecmwf":       "ecmwf_ifs025",
    "icon":        "icon_seamless",
    "ukmo":        "ukmo_seamless",
    "meteofrance": "meteofrance_seamless",
    "aifs":        "ecmwf_aifs025_single",    # ECMWF AIFS (AI tabanlı) — 6. model
}

# Model ağırlıkları — 60 günlük backtest MAE⁻¹ normalize (D+1, 6 istasyon)
# Önceki: ECMWF=2.0, ICON=1.0, UKMO=0.9 (varsayım bazlı)
# Backtest sonucu: ICON D+1 MAE=1.08 (en iyi), UKMO=1.80 (en kötü), ECMWF=1.20
# UKMO LTFM'de özellikle kötü: 3.22°C MAE — ağırlığı yarıya indirildi
# AIFS (2025+): 2025 benchmark'larında IFS'yi geçti; başlangıçta ecmwf'e yakın
# ağırlık (1.6). Faz 4 dinamik ağırlık yeterli veri biriktirince otomatik
# güncellenecek (istasyon × horizon × rolling 30-gün RMSE).
MODEL_WEIGHTS = {
    "ecmwf":       1.5,
    "icon":        1.8,
    "gfs":         1.0,
    "ukmo":        0.5,
    "meteofrance": 0.9,
    "aifs":        1.6,
}

# ── İstasyon-bazlı Model Konfigürasyonu (Asya) ─────────────────────────────
# Avrupa istasyonları (eglc, eham, epwa …) bu dict'te YOK → mevcut kod path'i
# aynen devam eder. Asya istasyonları kendi endpoint + model + ağırlık kümelerini
# kullanır; Avrupa stack'i (ICON, UKMO, MeteoFrance) bunlara uygulanmaz.
#
# forecast.{model}: (open-meteo-base-url, open-meteo-model-id)
# ensemble.{model}: open-meteo ensemble model id
# weights.{model}:  başlangıç blend ağırlığı (dinamik ağırlık DB'ye yazılana kadar)
STATION_MODEL_CONFIG: dict = {
    # Tokyo Haneda (RJTT) — JMA MSM 5 km bölgesel model + ECMWF/AIFS global
    "rjtt": {
        "forecast": {
            "jma_msm": ("https://api.open-meteo.com/v1/jma",     "jma_msm"),
            "jma_gsm": ("https://api.open-meteo.com/v1/jma",     "jma_gsm"),
            "ecmwf":   ("https://api.open-meteo.com/v1/forecast", "ecmwf_ifs025"),
            "aifs":    ("https://api.open-meteo.com/v1/forecast", "ecmwf_aifs025_single"),
            "cma":     ("https://api.open-meteo.com/v1/cma",     "cma_grapes_global"),
        },
        "ensemble": {
            "ecmwf": "ecmwf_ifs025",
            "aifs":  "ecmwf_aifs025",
        },
        "weights": {
            "jma_msm": 2.5,   # Japonya için en iyi bölgesel model (5 km, 8×/gün)
            "jma_gsm": 1.0,   # JMA global — orta/uzun vade
            "ecmwf":   1.5,
            "aifs":    1.6,
            "cma":     0.8,
        },
    },
    # Seoul Incheon (RKSI) — KMA LDPS 1.5 km + ECMWF/AIFS
    # ÖNEMLİ: Hedef koordinat = Incheon havalimanı (37.4602°N, 126.4407°E),
    # Seoul şehir merkezi değil. Uçuş alanı şehirden ~2°C daha soğuk → piyasa
    # modelleri şehir merkezini fiyatlıyor, bizim avantajımız.
    # NOT: KMA modelleri /v1/forecast?models=kma_* üzerinden çalışır (/v1/kma yok).
    "rksi": {
        "forecast": {
            "kma_ldps": ("https://api.open-meteo.com/v1/forecast", "kma_ldps"),
            "kma_gdps": ("https://api.open-meteo.com/v1/forecast", "kma_gdps"),
            "ecmwf":    ("https://api.open-meteo.com/v1/forecast", "ecmwf_ifs025"),
            "aifs":     ("https://api.open-meteo.com/v1/forecast", "ecmwf_aifs025_single"),
        },
        "ensemble": {
            "ecmwf": "ecmwf_ifs025",
            "aifs":  "ecmwf_aifs025",
        },
        "weights": {
            "kma_ldps": 2.5,  # 1.5 km çözünürlük — open-meteo'daki en ince Asya modeli
            "kma_gdps": 1.0,
            "ecmwf":    1.5,
            "aifs":     1.6,
        },
    },
    # Hong Kong Intl (VHHH) — ECMWF/AIFS + GFS (bölgesel model yok)
    # Not: Lantau adası konumu → Kowloon'dan 1–3°C daha serin (deniz meltemi)
    "vhhh": {
        "forecast": {
            "ecmwf": ("https://api.open-meteo.com/v1/forecast", "ecmwf_ifs025"),
            "aifs":  ("https://api.open-meteo.com/v1/forecast", "ecmwf_aifs025_single"),
            "gfs":   ("https://api.open-meteo.com/v1/forecast", "gfs_seamless"),
        },
        "ensemble": {
            "ecmwf": "ecmwf_ifs025",
            "aifs":  "ecmwf_aifs025",
        },
        "weights": {
            "ecmwf": 2.0,
            "aifs":  2.0,
            "gfs":   1.0,
        },
    },
}

# Horizon bazlı belirsizlik eşikleri (ağırlıklı std için)
# D+0 daha sıkı, D+2 daha toleranslı — her gün için (düşük_sınır, orta_sınır)
UNCERTAINTY_THRESHOLDS = {
    0: (0.6, 1.2),   # Bugün   — spread < 0.6 = Düşük, < 1.2 = Orta
    1: (0.8, 1.5),   # Yarın   — spread < 0.8 = Düşük, < 1.5 = Orta
    2: (1.1, 1.8),   # Öbür gün — toleranslı ama gerçek ayrışmayı yakalar
}

# ── Dinamik CALIB_STD_FACTOR (Faz 2) ────────────────────────────────────────
# Eski: statik 1.8 — tüm horizon/spread kombinasyonlarına aynı çarpan.
# Yeni: horizon + spread bağlamlı; kısa horizonda düşük, uzun/dağınık dönemde yüksek.
# Değerler backtest'ten değil fiziksel sezgiyle: D+0 dar, D+2 ~2× daha geniş.
CALIB_BASE = {0: 1.2, 1: 1.5, 2: 2.0}
CALIB_MAX  = 2.5

def dynamic_calib_factor(horizon_days: int, spread: float | None) -> float:
    """Horizon + spread'e göre kalibrasyon çarpanı (1.2 – 2.5 arası).

    Amaç: ensemble spread'i küçükse modelin güvenini koru; büyükse
    (model disagreement yüksek) spread'i biraz daha şişir — aşırı güveni
    aşındır, Brier ve CRPS'yi iyileştirir.
    """
    base = CALIB_BASE.get(min(max(horizon_days, 0), 2), 1.8)
    if spread is None:
        return base
    # spread > 2°C ise ek +0.2 (model ayrışması fazla)
    extra = 0.2 if spread > 2.0 else 0.0
    return round(min(base + extra, CALIB_MAX), 2)

# ── Bimodal Tespiti (Faz 2, stdlib-only) ────────────────────────────────────
BIMODAL_MIN_PEAK_PCT = 18  # tepe başına minimum üye oranı (%)
BIMODAL_MIN_SEPARATION = 2  # °C — iki tepe arasında en az bu kadar mesafe

def bimodal_analysis(member_maxes: list[float]) -> dict:
    """Ensemble üye dağılımında kaç tepe (mod) var?

    Yöntem: yuvarlanmış °C histogramında lokal maksimumları say.
    Her tepe en az BIMODAL_MIN_PEAK_PCT oranında üyeye sahip olmalı.

    Döner:
      is_bimodal: 2+ anlamlı tepe var mı
      n_peaks:    tepe sayısı
      peaks:      [(temp, count, pct), ...] azalan count sırasıyla
      separation: ilk iki tepenin °C farkı (None eğer 1 tepe)
    """
    if not member_maxes:
        return {"is_bimodal": False, "n_peaks": 0, "peaks": [], "separation": None}

    from collections import Counter
    n = len(member_maxes)
    counts = Counter(round(m) for m in member_maxes)
    if not counts:
        return {"is_bimodal": False, "n_peaks": 0, "peaks": [], "separation": None}

    temps = sorted(counts.keys())
    # temp → count haritasını komşu değerlere göre tara (1°C delikleri tolere et)
    def _get(t: int) -> int:
        return counts.get(t, 0)

    min_count = max(1, int(n * BIMODAL_MIN_PEAK_PCT / 100))
    peaks = []
    for t in temps:
        c = _get(t)
        if c < min_count:
            continue
        # lokal maksimum: solundakinden ≥ ve sağındakinden ≥, en az biri >
        left  = _get(t - 1)
        right = _get(t + 1)
        if c >= left and c >= right and (c > left or c > right or (left == 0 and right == 0)):
            peaks.append((t, c, round(c / n * 100)))

    peaks.sort(key=lambda x: x[1], reverse=True)

    # İkinci tepenin en büyük tepeden yeterince uzak olduğunu doğrula
    is_bimodal = False
    separation = None
    if len(peaks) >= 2:
        t1, _, _ = peaks[0]
        t2, _, _ = peaks[1]
        separation = abs(t1 - t2)
        is_bimodal = separation >= BIMODAL_MIN_SEPARATION

    return {
        "is_bimodal": is_bimodal,
        "n_peaks":    len(peaks),
        "peaks":      peaks[:3],       # en fazla 3 göster
        "separation": separation,
    }

# ── Bootstrap Güven Aralığı — Mod Yüzdesi (Faz 2, stdlib-only) ──────────────
BOOTSTRAP_SAMPLES = 200

def bootstrap_mode_ci(member_maxes: list[float], n_boot: int = BOOTSTRAP_SAMPLES) -> dict:
    """Ensemble'ın replacement'lı yeniden örneklenmesinden mod-yüzdesi %90 CI.

    Döner: {mode_pct, ci_low, ci_high, top_pick}
    CI geniş (örn. 25→55) ise consensus aslında çok kırılgan — uyarı ver.
    """
    import random
    from collections import Counter
    if not member_maxes:
        return {"mode_pct": None, "ci_low": None, "ci_high": None, "top_pick": None}

    n = len(member_maxes)
    # Orijinal mod yüzdesi
    orig_counts = Counter(round(m) for m in member_maxes)
    top_pick, top_n = orig_counts.most_common(1)[0]
    orig_pct = round(top_n / n * 100)

    # Deterministik CI — scanner her çağrıda aynı sonucu görsün (aynı gün içinde)
    rng = random.Random(hash(tuple(sorted(round(m, 1) for m in member_maxes))) & 0xFFFFFFFF)
    pcts = []
    for _ in range(n_boot):
        sample = [member_maxes[rng.randrange(n)] for _ in range(n)]
        c = Counter(round(m) for m in sample)
        # aynı top_pick için olasılık (farklı mod olsa bile orijinal top_pick'i takip et)
        pcts.append(c.get(top_pick, 0) / n * 100)

    pcts.sort()
    lo_idx = int(0.05 * n_boot)
    hi_idx = int(0.95 * n_boot) - 1
    return {
        "mode_pct": orig_pct,
        "ci_low":   round(pcts[lo_idx]),
        "ci_high":  round(pcts[hi_idx]),
        "top_pick": top_pick,
    }

# Bias düzeltmesi için minimum gün sayısı (7'den 5'e düşürüldü — daha hızlı aktivasyon)
BIAS_MIN_DAYS = 5

# Bias outlier filtresi: geçiş günü anomalilerini dışarıda bırak.
# WU urban ≠ METAR airport farkı geçiş günlerinde sahte büyük hata üretir
# (örn. LFPG May03: model=14.6, METAR=12.9 → +1.7°C ama WU≈14.5 → gerçek hata ~0.1).
# Bu anomali üstel ağırlık yüzünden bias'ı ters yöne iter (cooling correction).
# 1.5°C üstü hatalar geçiş günü sayılır ve bias hesabına dahil edilmez.
BIAS_OUTLIER_CAP = 1.5  # °C

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


def blend_day(models_data: dict, horizon: int = 1,
              weights: dict | None = None) -> dict:
    """
    Ağırlıklı blend — 4 iyileştirme:
    1) Adaptif outlier tespiti (2× std, sabit 5°C değil)
    2) Ağırlıklı standart sapma (hi-lo range değil)
    3) Horizon-aware eşikler (D+0/1/2 için farklı tolerans)
    4) Konsensüs skoru (±1°C içindeki model oranı)

    weights: Faz 4 — opsiyonel dinamik ağırlık dict. None ise MODEL_WEIGHTS statik.
    """
    mw = weights if weights else MODEL_WEIGHTS   # ağırlık kaynağı
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
            "weights_source": "static",
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
    total_w = sum(mw.get(k, 1.0) for k in filtered)
    blend   = round(sum(v * mw.get(k, 1.0) for k, v in filtered.items()) / total_w, 1)

    lo = round(min(filtered.values()), 1)
    hi = round(max(filtered.values()), 1)

    # ── Adım 3: Ağırlıklı standart sapma (hi-lo range değil) ────────────
    # Eski: spread = hi - lo  (tek outlier tüm metriği bozar)
    # Yeni: ECMWF 2x ağırlıklı, gerçek istatistiksel dağılım
    if len(filtered) > 1:
        variance = sum(
            mw.get(k, 1.0) * (v - blend) ** 2
            for k, v in filtered.items()
        ) / total_w
        spread = round(math.sqrt(variance), 2)
    else:
        spread = 0.0

    # ── Adım 4: Horizon-aware belirsizlik eşiği + Dinamik CALIB ─────────
    # D+2 için D+1'den farklı tolerans — atmosfer fiziğini yansıtır.
    # CALIB çarpanı spread'i "kalibre edilmiş" hâle şişirir — modelin aşırı
    # güvenini aşındırır. Eski statik 1.8 yerine horizon+spread bazlı dinamik.
    calib = dynamic_calib_factor(horizon, spread)
    calibrated_spread = spread * (calib / 1.8)   # 1.8 referans; yeni çarpan ona göre
    low_t, mid_t = UNCERTAINTY_THRESHOLDS.get(min(horizon, 2), (0.8, 1.5))
    if calibrated_spread < low_t:
        uncertainty = "Düşük"
    elif calibrated_spread < mid_t:
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
        "calib_factor":     calib,                   # Faz 2: dinamik CALIB_STD_FACTOR
        "calibrated_spread": round(calibrated_spread, 2),
        "weights_source":   "dynamic" if weights else "static",   # Faz 4
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

    # ── İstasyon-bazlı model seçimi ─────────────────────────────────────────
    # Asya istasyonları (rjtt, rksi, vhhh) → STATION_MODEL_CONFIG'daki model seti.
    # Avrupa istasyonları → global MODELS dict'i (değişmez, eski davranış korunur).
    if station in STATION_MODEL_CONFIG:
        _cfg = STATION_MODEL_CONFIG[station]
        # {model_name: (endpoint, model_id)}
        _station_models: dict = _cfg["forecast"]
        _station_weights: dict | None = _cfg.get("weights")
    else:
        _station_models = {
            name: ("https://api.open-meteo.com/v1/forecast", mid)
            for name, mid in MODELS.items()
        }
        _station_weights = None

    _query = (
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m"
        f"&timezone={s['tz']}&forecast_days=3"
    )

    # Semaphore ile korunan model çekme — her model kendi endpoint'ini kullanır
    async def fetch_model(client: httpx.AsyncClient, endpoint: str, model_id: str):
        async with _openmeteo_sem:
            try:
                return await client.get(f"{endpoint}{_query}&models={model_id}")
            except Exception:
                return None

    model_days: dict = {}
    _tasks = [
        (mn, ep, mid) for mn, (ep, mid) in _station_models.items()
    ]

    async with httpx.AsyncClient(timeout=25) as client:
        responses = await asyncio.gather(
            *[fetch_model(client, ep, mid) for _, ep, mid in _tasks],
            return_exceptions=True,
        )

        # Her model için parse et; başarısız olanları kaydet
        failed_models = []
        for (model_name, endpoint, model_id), resp in zip(_tasks, responses):
            if isinstance(resp, Exception) or resp is None or not resp.is_success:
                failed_models.append((model_name, endpoint, model_id))
                continue
            try:
                model_days[model_name] = parse_hourly(resp.json())
            except Exception:
                failed_models.append((model_name, endpoint, model_id))

        # Başarısız modelleri 1s bekleyip tek tek retry (429 sonrası)
        if failed_models:
            await asyncio.sleep(1.0)
            for model_name, endpoint, model_id in failed_models:
                async with _openmeteo_sem:
                    try:
                        r = await client.get(
                            f"{endpoint}{_query}&models={model_id}", timeout=30
                        )
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
        horizon = min(i, 2)

        # Faz 4: istasyon + horizon bazlı dinamik ağırlık (yeterli veri varsa)
        dyn_weights = None
        try:
            from bot.dynamic_weights import compute_dynamic_weights
            dyn_weights = compute_dynamic_weights(station, horizon_days=horizon)
        except Exception:
            pass

        # Asya istasyonları: dinamik ağırlık yoksa (yeni istasyon) station-specific
        # başlangıç ağırlıklarını kullan; Avrupa için _station_weights zaten None.
        effective_weights = dyn_weights or _station_weights

        days_result[date] = {
            "models": per_model,
            "blend":  blend_day(per_model, horizon=horizon, weights=effective_weights),
        }

        # Faz 4: her modelin max_temp'ini DB'ye kaydet (rolling RMSE kaynağı)
        try:
            from bot.db import record_model_forecast
            for model_name, day_data in per_model.items():
                mt = day_data.get("max_temp")
                if mt is not None:
                    record_model_forecast(
                        station=station, date=date, model=model_name,
                        max_temp=mt, horizon_days=horizon,
                    )
        except Exception:
            pass

    # Bias düzeltmesi: predictions.json'dan sistematik hatayı hesapla
    # BIAS_OUTLIER_CAP: geçiş günü anomalilerini filtrele — sahte ters correction'ı önler.
    preds = _load_preds()
    bias_entries = []
    for date_key, e in sorted(preds.get(station, {}).items()):
        if e.get("blend") is not None and e.get("actual") is not None:
            err = e["blend"] - e["actual"]
            if abs(err) <= BIAS_OUTLIER_CAP:
                bias_entries.append(err)

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
async def get_ensemble(station: str, refresh: bool = False):
    """
    Çok-model ensemble: ICON (40 üye) + ECMWF IFS (50 üye) = 90 üye.
    Her iki model de Open-Meteo ensemble API'sinden çekilir, paralel istek.
    ECMWF başarısız olursa sadece ICON ile devam edilir (graceful fallback).
    member_maxes: birleştirilmiş tüm üyelerin günlük maksimumları (backward compatible).
    Cache: 20 dakika — dashboard'da 14 istasyon × 3 model = 42 req/yükleme önlenir.
    """
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    # Cache kontrolü
    cached = _ensemble_cache.get(station)
    now = time.monotonic()
    if not refresh and cached and (now - cached["ts"]) < ENSEMBLE_CACHE_TTL:
        return cached["data"]

    s = STATIONS[station]

    # Desteklenen ensemble modelleri (model_adı: open-meteo-id)
    # Asya istasyonları → STATION_MODEL_CONFIG'daki ensemble seti (ICON yok).
    # Avrupa istasyonları → ICON (40 üye) + ECMWF (50) + AIFS (50) = 140 üye.
    # Not: ensemble API'de AIFS ID'si "ecmwf_aifs025" (alt çizgi YOK).
    if station in STATION_MODEL_CONFIG:
        ENSEMBLE_MODELS = dict(STATION_MODEL_CONFIG[station].get("ensemble", {
            "ecmwf": "ecmwf_ifs025",
            "aifs":  "ecmwf_aifs025",
        }))
    else:
        ENSEMBLE_MODELS = {
            "icon":  "icon_seamless",
            "ecmwf": "ecmwf_ifs025",
            "aifs":  "ecmwf_aifs025",
        }
    # Faz 7: Beklenen üye sayıları (ctrl dahil). Çok düşerse sessiz degradation
    # var demektir → log'da uyar, dashboard'da izlenebilir.
    EXPECTED_MEMBERS = {
        "icon":  40,
        "ecmwf": 50,
        "aifs":  50,
    }

    base_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m"
        f"&timezone={s['tz']}&forecast_days=3"
    )

    def parse_ensemble_response(raw: dict) -> dict:
        """Open-Meteo ensemble yanıtını {date: [daily_maxes]} olarak döndür."""
        hourly = raw.get("hourly", {})
        times  = hourly.get("time", [])
        member_keys = sorted(k for k in hourly if k.startswith("temperature_2m_member"))
        if not member_keys:
            return {}

        date_member: dict = {}
        for key in member_keys:
            vals = hourly[key]
            for i, t in enumerate(times):
                d = t[:10]
                v = vals[i]
                if v is None:
                    continue
                date_member.setdefault(d, {}).setdefault(key, []).append(v)

        result = {}
        for date, members in date_member.items():
            maxes = [max(temps) for temps in members.values() if temps]
            if maxes:
                result[date] = maxes
        return result

    async def fetch_model(client: httpx.AsyncClient, model_id: str) -> dict:
        """Tek model için ensemble verisi çek. Hata durumunda boş dict.

        Faz 7: üye sayısını doğrula — beklenenin %80'inden azsa uyar. Modelin
        tamamen kaybolması (0 üye) üst katmanda zaten handle ediliyor; ama
        kısmi fail ("5/50 member") sessizce tahmin ağırlıklarını bozar.
        """
        try:
            r = await client.get(base_url + f"&models={model_id}", timeout=20)
            if r.is_success:
                raw = r.json()
                # Üye sayısı doğrulaması
                hourly = raw.get("hourly", {})
                n_members = sum(
                    1 for k in hourly if k.startswith("temperature_2m_member")
                )
                # model_id → logical name (ters arama)
                model_name = next(
                    (k for k, v in ENSEMBLE_MODELS.items() if v == model_id),
                    model_id,
                )
                expected = EXPECTED_MEMBERS.get(model_name, 0)
                if expected and n_members < expected * 0.8:
                    print(
                        f"  ⚠️  {model_name} ensemble üye sayısı düşük: "
                        f"{n_members}/{expected} (< %80 eşiği)"
                    )
                return parse_ensemble_response(raw)
        except Exception:
            pass
        return {}

    # Paralel fetch — her model bağımsız, biri başarısız olursa diğeri devam eder
    async with httpx.AsyncClient(timeout=25) as client:
        results = await asyncio.gather(
            *[fetch_model(client, model_id) for model_id in ENSEMBLE_MODELS.values()],
            return_exceptions=True
        )

    model_names = list(ENSEMBLE_MODELS.keys())

    # Modellerin günlük maxlarını birleştir
    combined: dict = {}   # {date: {"maxes": [...], "model_counts": {...}}}
    for model_name, result in zip(model_names, results):
        if isinstance(result, Exception) or not isinstance(result, dict):
            continue
        for date, maxes in result.items():
            combined.setdefault(date, {"maxes": [], "model_counts": {}})
            combined[date]["maxes"].extend(maxes)
            combined[date]["model_counts"][model_name] = len(maxes)

    if not combined:
        raise HTTPException(status_code=502, detail="Ensemble verisi alınamadı")

    def pct(sorted_vals: list, p: float) -> float:
        n   = len(sorted_vals)
        idx = p / 100 * (n - 1)
        lo  = int(idx)
        hi  = min(lo + 1, n - 1)
        return round(sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo]), 1)

    days = {}
    for date, data in combined.items():
        maxes = sorted(data["maxes"])
        if not maxes:
            continue
        n = len(maxes)
        # Faz 2: şekil metrikleri (bimodal + bootstrap CI)
        shape  = bimodal_analysis(data["maxes"])
        mode_ci = bootstrap_mode_ci(data["maxes"])
        days[date] = {
            "member_maxes":  [round(m, 1) for m in maxes],   # backward compatible
            "count":         n,
            "mean":          round(sum(maxes) / n, 1),
            "p10":           pct(maxes, 10),
            "p25":           pct(maxes, 25),
            "p50":           pct(maxes, 50),
            "p75":           pct(maxes, 75),
            "p90":           pct(maxes, 90),
            "model_counts":  data["model_counts"],    # hangi modelden kaç üye
            # Faz 2 şekil analizi
            "is_bimodal":    shape["is_bimodal"],
            "n_peaks":       shape["n_peaks"],
            "peaks":         shape["peaks"],
            "peak_separation": shape["separation"],
            "mode_pct":      mode_ci["mode_pct"],
            "mode_ci_low":   mode_ci["ci_low"],
            "mode_ci_high":  mode_ci["ci_high"],
        }

    result = {"station": station, "days": days}
    _ensemble_cache[station] = {"ts": time.monotonic(), "data": result}
    return result


def _get_recent_actuals(station: str, n_days: int = 3) -> list:
    """Son n_days günün gerçek max sıcaklıklarını predictions.json'dan döndür.

    Döner: [{"date": "2026-04-25", "actual": 21.0}, ...]  — en yeni önde.
    NO bot ve analiz için: model blind spot tespiti.
    """
    try:
        preds = _load_preds()
        entries = []
        for date, e in sorted(preds.get(station, {}).items(), reverse=True):
            actual = e.get("actual")
            if actual is not None:
                entries.append({"date": date, "actual": float(actual)})
                if len(entries) >= n_days:
                    break
        return entries
    except Exception:
        return []


def _compute_model_streak(station: str) -> dict:
    """Model bias yönünde kaç gün üst üste yanılıyor?

    cold_streak: actual > blend olan ardışık gün sayısı (model alttan vuruyor)
    warm_streak: actual < blend olan ardışık gün sayısı (model üstten vuruyor)
    streak_delta: son streak'teki ortalama sapma (°C)
    """
    try:
        preds = _load_preds()
        entries_sorted = sorted(
            [(d, e) for d, e in preds.get(station, {}).items()
             if e.get("blend") is not None and e.get("actual") is not None],
            key=lambda x: x[0],
            reverse=True,   # en yeni önce
        )
        cold_streak = warm_streak = 0
        cold_deltas: list = []
        warm_deltas: list = []
        for _, e in entries_sorted:
            err = e["actual"] - e["blend"]   # pozitif = model soğuk, gerçek daha yüksek
            if err > 0.5:
                if warm_streak > 0:
                    break   # zincir kırıldı
                cold_streak += 1
                cold_deltas.append(err)
            elif err < -0.5:
                if cold_streak > 0:
                    break
                warm_streak += 1
                warm_deltas.append(abs(err))
            else:
                break   # nötr gün → zincir kırıldı
        streak_delta = round(sum(cold_deltas) / len(cold_deltas), 1) if cold_deltas else (
            round(sum(warm_deltas) / len(warm_deltas), 1) if warm_deltas else 0.0
        )
        return {
            "cold_streak":   cold_streak,   # model soğuk, gerçek daha sıcak
            "warm_streak":   warm_streak,   # model sıcak, gerçek daha soğuk
            "streak_delta":  streak_delta,  # son streak'teki ort. sapma °C
        }
    except Exception:
        return {"cold_streak": 0, "warm_streak": 0, "streak_delta": 0.0}


@app.get("/api/ens-buckets")
async def get_ens_buckets(station: str, date: str):
    """
    Bias-corrected ENS% per bucket — botun TEK veri kaynağı.
    Dashboard ile aynı hesaplama: bias uygula + CAP_LO=3%.

    Yeni alanlar (model güvenilirlik sinyalleri):
      recent_actuals   — son 3 günün gerçek max°C (NO bot için kontekst)
      cold_streak      — model kaç gün üst üste düşük tahmin etti
      warm_streak      — model kaç gün üst üste yüksek tahmin etti
      streak_delta     — o streak'teki ort. sapma °C
    """
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    # 1. Raw ensemble
    try:
        ens_resp = await get_ensemble(station)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ensemble alınamadı: {e}")

    day_data = ens_resp.get("days", {}).get(date)
    if not day_data:
        raise HTTPException(status_code=404, detail=f"Ensemble verisi yok: {date}")

    raw_members = day_data["member_maxes"]
    n = len(raw_members)

    # 2. Bias correction
    bias_7d = 0.0
    bias_active = False
    try:
        bias_resp = await get_prediction_bias(station)
        bias_7d = float(bias_resp.get("bias_7d") or 0.0)
        bias_active = bool(bias_resp.get("bias_active", False))
    except Exception:
        pass

    # corrected = member - bias_7d
    # bias_7d negatif (model soğuk) → çıkarma ekleme olur
    corrected = [m - bias_7d for m in raw_members]
    corrected_mean = round(sum(corrected) / n, 2) if n else 0.0

    # oracle_delta: WU settlement kaynağının sistematik farkı (settlement_delta.py)
    # corrected = model tahmini; oracle_shifted = WU'nun göreceği değer
    # oracle_pct → NO bot için gerçek risk göstergesi (WU bazlı olasılık)
    oracle_delta = 0.0
    try:
        from bot.settlement_delta import learn_station_delta
        oracle_delta = learn_station_delta(station)
    except Exception:
        pass
    oracle_shifted = [m + oracle_delta for m in corrected]

    # 3. Polymarket buckets
    try:
        pm_resp = await get_polymarket_markets(station, date)
        pm_buckets = pm_resp.get("buckets", [])
    except Exception:
        pm_buckets = []

    # 4. ENS% per bucket (CAP_LO=1%)
    # 0 oy alan bucket'lara %3 yerine %1 taban uygulanır.
    # %3 yapay yüksekti — gerçek ihtimal çok daha düşük (≈%0.5).
    # NO bot edge hesabı: PM% - ENS% = PM% - 0.01 → daha temiz pozitif edge görünümü.
    # oracle_pct: ens_pct'nin settlement delta ile düzeltilmiş hali (WU bazlı)
    CAP_LO = 0.01
    bucket_results = []
    mode_thr = None
    mode_pct = 0.0

    for b in pm_buckets:
        thr = b.get("threshold")
        is_below = b.get("is_below", False)
        is_above = b.get("is_above", False)
        if thr is None:
            continue

        if is_below:
            cnt          = sum(1 for m in corrected       if round(m) <= thr)
            oracle_cnt   = sum(1 for m in oracle_shifted   if round(m) <= thr)
        elif is_above:
            cnt          = sum(1 for m in corrected       if round(m) >= thr)
            oracle_cnt   = sum(1 for m in oracle_shifted   if round(m) >= thr)
        else:
            cnt          = sum(1 for m in corrected       if round(m) == int(thr))
            oracle_cnt   = sum(1 for m in oracle_shifted   if round(m) == int(thr))

        raw_pct    = cnt / n if n else 0.0
        ens_pct    = max(CAP_LO, raw_pct)
        capped     = raw_pct < CAP_LO
        oracle_pct = round(max(CAP_LO, oracle_cnt / n) if n else CAP_LO, 4)

        bucket_results.append({
            "threshold":    thr,
            "is_below":     is_below,
            "is_above":     is_above,
            "yes_price":    b.get("yes_price"),
            "no_price":     b.get("no_price"),
            "liquidity":    b.get("liquidity"),
            "condition_id": b.get("condition_id"),
            "ens_pct":      round(ens_pct, 4),
            "ens_count":    cnt,
            "capped":       capped,
            "oracle_pct":   oracle_pct,     # WU-delta ile düzeltilmiş olasılık
            "oracle_count": oracle_cnt,
        })

        if not is_below and not is_above and raw_pct > mode_pct:
            mode_pct = raw_pct
            mode_thr = int(thr)

    # 5. Trend
    TREND_THRESH = 0.3
    corrected_mode = mode_thr or 0
    diff = corrected_mean - corrected_mode if corrected_mode else 0.0
    if diff > TREND_THRESH:
        trend = "warming"
    elif diff < -TREND_THRESH:
        trend = "cooling"
    else:
        trend = "neutral"

    # 6. Model güvenilirlik sinyalleri (NO bot için)
    recent_actuals = _get_recent_actuals(station, n_days=3)
    streak_info    = _compute_model_streak(station)

    return {
        "station":        station,
        "date":           date,
        "bias":           bias_7d,
        "bias_active":    bias_active,
        "member_count":   n,
        "corrected_mean": corrected_mean,
        "corrected_mode": corrected_mode,
        "mode_pct":       round(mode_pct, 4),
        "trend":          trend,
        "trend_diff":     round(diff, 3),
        "buckets":        bucket_results,
        # Yeni alanlar — model güvenilirlik sinyalleri
        "recent_actuals": recent_actuals,
        "cold_streak":    streak_info["cold_streak"],
        "warm_streak":    streak_info["warm_streak"],
        "streak_delta":   streak_info["streak_delta"],
        # ENS max — bias-corrected en sicak uye (T1 guvenlik marji icin)
        "ens_max":        round(max(corrected), 2) if corrected else 0.0,
        # Oracle delta — WU settlement kaynağının sistematik farkı
        # oracle_pct her bucket'ta zaten var; delta NO bot'un kendi hesabı için
        "oracle_delta":   round(oracle_delta, 2),
    }


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

        # clobTokenIds is a JSON string: '["YES_TOKEN_ID", "NO_TOKEN_ID"]'
        # YES token (index 0) is what CLOB uses for OrderArgs / get_order_book
        clob_raw = m.get("clobTokenIds", "[]")
        if isinstance(clob_raw, str):
            try:
                clob_tokens = json.loads(clob_raw)
            except Exception:
                clob_tokens = []
        else:
            clob_tokens = clob_raw if isinstance(clob_raw, list) else []
        yes_token_id = clob_tokens[0] if clob_tokens else m.get("conditionId")

        buckets.append({
            "title":     title,
            "threshold": thresh,
            "is_below":  is_below,
            "is_above":  is_above,
            "yes_price": yes_price,
            "no_price":  no_price,
            "liquidity": round(float(m.get("liquidity", 0) or 0)),
            "volume":    round(float(m.get("volume",    0) or 0)),
            "condition_id": yes_token_id,  # YES token_id for CLOB order placement
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
async def get_metar_history(station: str, refresh: bool = False):
    """Son 7 günün METAR maks sıcaklıkları + lineer trend.
    Cache: 30 dakika — 14 istasyon × aviation API = 14 req/yükleme; nadir değişir.
    """
    station = station.lower()
    if station not in STATIONS:
        raise HTTPException(status_code=404, detail="Bilinmeyen istasyon")

    # Cache kontrolü
    cached = _metar_history_cache.get(station)
    if not refresh and cached and (time.monotonic() - cached["ts"]) < METAR_HISTORY_CACHE_TTL:
        return cached["data"]

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

    result = {
        "station":     station,
        "daily_maxes": daily_maxes,
        "trend_slope": trend_slope,
        "trend_dir":   trend_dir,
    }
    _metar_history_cache[station] = {"ts": time.monotonic(), "data": result}
    return result


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
    recent_all = entries[-7:]
    if not recent_all:
        return {"station": station, "bias_7d": 0.0, "mae": 0.0, "count": 0,
                "bias_active": False, "entries": []}

    # Outlier filtresi: geçiş günü anomalilerini bias hesabından dışla (dashboard'da görünür).
    # BIAS_OUTLIER_CAP üstündeki hatalar dahil edilmez — ters correction'ı önler.
    recent = [x for x in recent_all if abs(x["error"]) <= BIAS_OUTLIER_CAP]
    if not recent:
        # Tüm günler outlier → bias yok (muhafazakâr)
        return {"station": station, "bias_7d": 0.0, "mae": 0.0, "count": 0,
                "bias_active": False, "entries": entries[-14:],
                "outliers_filtered": len(recent_all)}

    # Üstel ağırlıklı ortalama: en yeni gün en yüksek ağırlık (2^i)
    # [eski ... yeni] → weights [1, 2, 4, 8, ...]
    weights = [2 ** i for i in range(len(recent))]
    w_bias  = sum(x["error"] * w for x, w in zip(recent, weights)) / sum(weights)

    # MAE (basit ortalama — overcorrection sınırı için)
    mae = sum(abs(x["error"]) for x in recent) / len(recent)

    # Bias'ı MAE ile sınırla: aşırı düzeltmeyi önle
    bias = round(max(-mae, min(mae, w_bias)), 2)

    # Streak bilgisi (model hangi yönde ve kaç gün üst üste yanılıyor?)
    streak = _compute_model_streak(station)

    outliers_filtered = len(recent_all) - len(recent)
    return {
        "station":           station,
        "bias_7d":           bias,
        "mae":               round(mae, 2),
        "count":             len(recent),
        "bias_active":       len(recent) >= BIAS_MIN_DAYS,
        "entries":           entries[-14:],
        "outliers_filtered": outliers_filtered,
        # Model güvenilirlik sinyalleri
        "cold_streak":  streak["cold_streak"],
        "warm_streak":  streak["warm_streak"],
        "streak_delta": streak["streak_delta"],
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


BOT_TRADES_FILE = Path("bot/paper_trades.json")

def _load_bot_trades() -> list:
    if BOT_TRADES_FILE.exists():
        return json.loads(BOT_TRADES_FILE.read_text(encoding="utf-8"))
    return []

def _save_bot_trades(trades: list):
    BOT_TRADES_FILE.parent.mkdir(exist_ok=True)
    BOT_TRADES_FILE.write_text(
        json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8"
    )

@app.get("/api/bot-trades")
async def get_bot_trades():
    """Paper trading bot geçmişini döndür."""
    trades = _load_bot_trades()
    closed = [t for t in trades if t.get("status") == "closed"]
    open_t = [t for t in trades if t.get("status") == "open"]
    wins   = [t for t in closed if t.get("result") == "WIN"]
    total_pnl = sum(t.get("pnl", 0) or 0 for t in closed)
    return {
        "trades":    trades,
        "stats": {
            "total":     len(trades),
            "open":      len(open_t),
            "closed":    len(closed),
            "wins":      len(wins),
            "losses":    len(closed) - len(wins),
            "win_rate":  round(len(wins) / len(closed) * 100, 1) if closed else None,
            "total_pnl": round(total_pnl, 1),
        }
    }

@app.get("/api/portfolio/var")
async def get_portfolio_var(source: str = "paper", days: int = 60):
    """Açık pozisyonlar üzerinden Monte Carlo VaR (Faz 6a).

    source: 'paper' (default) | 'live' | 'both'
    days: korelasyon matrisi için geriye bakış penceresi (default 60)
    """
    try:
        from bot.portfolio_var import portfolio_var
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"VaR modülü yüklenemedi: {e}")

    trades_all = []
    if source in ("paper", "both"):
        trades_all.extend(_load_bot_trades())
    if source in ("live", "both"):
        trades_all.extend(_load_live_trades())

    open_trades = [t for t in trades_all if t.get("status") in ("open", "filled", "pending_fill")]
    try:
        result = await asyncio.to_thread(portfolio_var, open_trades, days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"VaR hesaplama hatası: {e}")
    result["source"] = source
    result["lookback_days"] = days
    return result


@app.get("/api/calibration")
async def get_calibration(days: int = 90, station: str = "", per_station: bool = True):
    """Kalibrasyon dashboard verisi (Faz 6c).

    Kapalı paper_trades üzerinden Brier + reliability + sharpness.
    days: geriye bakış (default 90). station: opsiyonel filtre.
    per_station: istasyon başı breakdown dahil edilsin mi.
    """
    try:
        from bot.calibration import compute_calibration, compute_per_station
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Calibration modülü yüklenemedi: {e}")
    trades = _load_bot_trades()
    overall = compute_calibration(trades, days=days, station=station or None)
    out = {
        "days":    days,
        "station": station or None,
        "overall": overall,
    }
    if per_station and not station:
        out["per_station"] = compute_per_station(trades, days=days)
    return out


@app.get("/api/settlement-audit")
async def get_settlement_audit(days: int = 30, station: str = ""):
    """Çok-kaynaklı settle gözlemleri + uyumsuzluk istatistiği (Faz 6b).

    days: geriye bakış penceresi (default 30)
    station: opsiyonel filtre
    """
    try:
        from bot.db import get_settlement_audit as _gsa, settlement_disagreement_stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audit modülü yüklenemedi: {e}")
    entries = _gsa(days=days, station=station or None)
    stats   = settlement_disagreement_stats(days=days)
    n_disagree = sum(1 for e in entries if e.get("disagreement"))
    return {
        "days":            days,
        "station":         station or None,
        "n_entries":       len(entries),
        "n_disagreement":  n_disagree,
        "entries":         entries,
        "station_stats":   stats,
    }


@app.post("/api/bot-trades/scan")
async def bot_scan(x_api_token: str = Header(default="")):
    """Bot taramasını tetikle (scanner.py scan)."""
    require_token(x_api_token)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["python3", "bot/scanner.py", "scan"],
            capture_output=True, text=True, timeout=120
        )
        return {"ok": True, "output": result.stdout + result.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/bot-trades/settle")
async def bot_settle(x_api_token: str = Header(default="")):
    """Dünkü pozisyonları settle et (scanner.py settle)."""
    require_token(x_api_token)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["python3", "bot/scanner.py", "settle"],
            capture_output=True, text=True, timeout=120
        )
        return {"ok": True, "output": result.stdout + result.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Live Trading API Endpoints ───────────────────────────────────────────────

LIVE_TRADES_FILE = Path("bot/live_trades.json")
NO_TRADES_FILE   = Path(os.getenv("NO_TRADES_FILE", "/opt/polymarket/bot/data/weather_no_trades.json"))
CLOSED_LIVE_STATUSES = ("won", "lost", "settled_win", "settled_loss", "expired", "cancelled")

def _load_live_trades() -> list:
    try:
        if LIVE_TRADES_FILE.exists():
            return json.loads(LIVE_TRADES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

@app.get("/api/live-trades")
async def get_live_trades():
    """Canlı live trade geçmişini döndür."""
    trades  = _load_live_trades()
    pending = [t for t in trades if t.get("status") == "pending_fill"]
    filled  = [t for t in trades if t.get("status") == "filled"]
    closed  = [t for t in trades if t.get("status") in CLOSED_LIVE_STATUSES]
    wins    = [t for t in closed if (t.get("result") or "").upper() == "WIN"]
    losses  = [t for t in closed if (t.get("result") or "").upper() == "LOSS"]
    open_n  = len(pending) + len(filled)
    win_rate  = round(len(wins) / len(closed) * 100, 1) if closed else None
    total_pnl = round(sum(t.get("pnl_usdc") or 0 for t in closed), 2)
    return {
        "trades": trades,
        "stats": {
            "total":     len(trades),
            "pending":   len(pending),
            "filled":    len(filled),
            "open":      open_n,
            "closed":    len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  win_rate,
            "total_pnl": total_pnl,
        }
    }

OPEN_LIVE_STATUSES = ("pending_fill", "filled", "sell_pending")

async def _fetch_event_markets(city: str, date: str) -> list:
    """Tek bir (city, date) için gamma-api events çağrısı.
    Başarısızlıkta [] döner (exception swallow edilir)."""
    slug = pm_slug(city, date)
    url  = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if not r.is_success:
            return []
        events = r.json()
        if not isinstance(events, list) or not events:
            return []
        return events[0].get("markets", []) or []
    except Exception:
        return []

@app.get("/api/live-open-positions")
async def get_live_open_positions():
    """Açık pozisyonların (pending_fill / filled / sell_pending) gamma-api'den
    YES fiyatını çekerek unrealized P&L'ini hesaplar.

    Unrealized P&L = (yes_price - fill_price) * shares  (sadece fill_price doluysa)
    Pending için unrealized=0 (fill olmamış)."""
    trades = _load_live_trades()
    open_trades = [t for t in trades if t.get("status") in OPEN_LIVE_STATUSES]

    # Unique (station, date) çiftleri — her biri için TEK fetch
    unique_pairs: dict = {}    # key: (station, date) → city
    for t in open_trades:
        station = (t.get("station") or "").lower()
        date    = t.get("date")
        if not station or not date or station not in STATIONS:
            continue
        key = (station, date)
        if key not in unique_pairs:
            unique_pairs[key] = STATIONS[station]["pm_query"].lower()

    # Paralel fetch — her station-date çifti için bağımsız try/except (fn içinde)
    pair_items = list(unique_pairs.items())
    fetched    = await asyncio.gather(
        *[_fetch_event_markets(city, date) for ((_st, date), city) in pair_items],
        return_exceptions=False
    )
    markets_by_pair: dict = {}   # (station, date) → markets[]
    for (key, _city), markets in zip(pair_items, fetched):
        markets_by_pair[key] = markets

    # condition_id → yes_price lookup (her market'te YES token id = clobTokenIds[0] veya conditionId)
    def _yes_price_for(markets: list, cid: str) -> Optional[float]:
        if not cid:
            return None
        cid = str(cid)
        for m in markets:
            # YES token id (clobTokenIds[0]) trader.py'nin kaydettiği condition_id'sidir
            clob_raw = m.get("clobTokenIds", "[]")
            if isinstance(clob_raw, str):
                try: clob_tokens = json.loads(clob_raw)
                except Exception: clob_tokens = []
            else:
                clob_tokens = clob_raw if isinstance(clob_raw, list) else []
            yes_tok = clob_tokens[0] if clob_tokens else None
            # Match: YES token ya da ham conditionId
            if yes_tok and str(yes_tok) == cid:
                pass
            elif str(m.get("conditionId") or "") == cid:
                pass
            else:
                continue
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except Exception: prices = []
            if prices and len(prices) >= 1:
                try:
                    return round(float(prices[0]), 4)
                except Exception:
                    return None
            return None
        return None

    positions = []
    total_at_risk     = 0.0
    total_mtm         = 0.0
    total_unrealized  = 0.0
    has_any_unrealized = False

    for t in open_trades:
        station = (t.get("station") or "").lower()
        date    = t.get("date")
        cid     = t.get("condition_id")
        fill_p  = t.get("fill_price")
        shares  = float(t.get("shares") or 0)
        cost    = float(t.get("cost_usdc") or 0)
        status  = t.get("status")

        markets = markets_by_pair.get((station, date), [])
        yes_price = _yes_price_for(markets, cid)

        at_risk = cost
        if fill_p is not None and yes_price is not None:
            mtm = round(yes_price * shares, 4)
            unrealized = round((yes_price - float(fill_p)) * shares, 4)
            unrealized_pct = round((unrealized / cost) * 100, 2) if cost else None
            has_any_unrealized = True
            total_mtm        += mtm
            total_unrealized += unrealized
        elif fill_p is None:
            # Pending — henüz fill olmadı, cost = risk, mtm = cost (flat)
            mtm = cost
            unrealized = 0.0
            unrealized_pct = 0.0
            total_mtm += mtm
        else:
            # Fill var ama fiyat alınamadı
            mtm = None
            unrealized = None
            unrealized_pct = None

        total_at_risk += at_risk

        # market_url: event slug üzerinden (station bilinmiyorsa skip)
        market_url = None
        if station in STATIONS:
            city = STATIONS[station]["pm_query"].lower()
            market_url = f"https://polymarket.com/event/{pm_slug(city, date)}"

        positions.append({
            "id":             t.get("id"),
            "station":        station,
            "date":           date,
            "top_pick":       t.get("top_pick"),
            "bucket_title":   t.get("bucket_title"),
            "fill_price":     fill_p,
            "limit_price":    t.get("limit_price"),
            "shares":         shares,
            "cost_usdc":      cost,
            "status":         status,
            "current_price":  yes_price,
            "unrealized_pnl": unrealized,
            "unrealized_pct": unrealized_pct,
            "mark_to_market": mtm,
            "market_url":     market_url,
            "sell_price":     t.get("sell_price"),
            "sell_order_id":  t.get("sell_order_id"),
            "placed_at":      t.get("placed_at"),
            "fill_time":      t.get("fill_time"),
        })

    unrealized_pct_total = (
        round((total_unrealized / total_at_risk) * 100, 2)
        if (has_any_unrealized and total_at_risk > 0) else None
    )

    return {
        "positions": positions,
        "totals": {
            "open_count":          len(positions),
            "at_risk_usdc":        round(total_at_risk, 2),
            "mark_to_market_usdc": round(total_mtm, 2),
            "unrealized_pnl":      round(total_unrealized, 2) if has_any_unrealized else None,
            "unrealized_pct":      unrealized_pct_total,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/live-balance")
async def get_live_balance():
    """USDC bakiyesini döndür (trader.py balance)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["python3", "bot/trader.py", "balance"],
            capture_output=True, text=True, timeout=30,
            cwd="/root/weather"
        )
        output = result.stdout + result.stderr
        # "💰 USDC Bakiyesi   : $12.3456"
        m = re.search(r'Bakiyesi\s*:\s*\$([0-9.]+)', output)
        if not m:
            m = re.search(r'\$\s*([0-9.]+)\s*USDC', output, re.IGNORECASE)
        balance = float(m.group(1)) if m else None
        return {"balance": balance, "raw": output}
    except Exception as e:
        return {"balance": None, "error": str(e)}


# ── NO Bot ────────────────────────────────────────────────────────────────────

CLOSED_NO_STATUSES   = frozenset({"settled", "sold", "sell_filled", "cancelled", "expired"})
OPEN_NO_STATUSES     = frozenset({"pending_fill", "filled", "sell_pending", "settled_pending"})
REALIZED_NO_STATUSES = frozenset({"sold", "sell_filled", "settled"})

def _load_no_trades() -> list:
    try:
        if NO_TRADES_FILE.exists():
            return json.loads(NO_TRADES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []

def _calc_no_trade_pnl(t: dict):
    """Return (pnl_usdc, result) computed from raw NO-bot trade fields.
    Returns (None, None) if the trade is not yet realized.

    P&L rules
    ---------
    sold / sell_filled : pnl = (sell_price - fill_price) × shares
    settled + NO-LOST  : pnl = -fill_price × shares   (YES won → NO redeems $0)
    settled (NO won)   : pnl = (1.0 - fill_price) × shares  (NO redeems $1/token)
    Everything else    : not realized → (None, None)
    """
    status = t.get("status", "")
    if status not in REALIZED_NO_STATUSES:
        return None, None

    fill_price = t.get("fill_price")
    sell_price = t.get("sell_price")
    shares     = t.get("shares")
    notes      = t.get("notes") or ""

    if fill_price is None or shares is None:
        return None, None

    fp = float(fill_price)
    sh = float(shares)

    if status in ("sold", "sell_filled"):
        if sell_price is None:
            return None, None
        pnl    = (float(sell_price) - fp) * sh
        result = "WIN" if pnl >= 0 else "LOSS"
    elif status == "settled":
        if "NO-LOST" in notes:
            # YES outcome won → NO tokens expire worthless; redeem = 0
            pnl    = -fp * sh
            result = "LOSS"
        else:
            # NO outcome won → redeem $1 per token
            pnl    = (1.0 - fp) * sh
            result = "WIN"
    else:
        return None, None

    return round(pnl, 4), result

@app.get("/api/no-bot/trades")
async def get_no_bot_trades():
    raw_trades = _load_no_trades()

    # Enrich every trade with computed pnl_usdc + result (where realized)
    enriched: list = []
    for t in raw_trades:
        tc = dict(t)
        pnl, result = _calc_no_trade_pnl(tc)
        if pnl is not None:
            tc["pnl_usdc"] = pnl
            tc["result"]   = result
        enriched.append(tc)

    open_t   = [t for t in enriched if t.get("status") in OPEN_NO_STATUSES]
    closed   = [t for t in enriched if t.get("status") in CLOSED_NO_STATUSES]
    realized = [t for t in enriched if t.get("pnl_usdc") is not None]
    wins     = [t for t in realized if (t.get("result") or "").upper() == "WIN"]
    losses   = [t for t in realized if (t.get("result") or "").upper() == "LOSS"]
    total_pnl = round(sum(t["pnl_usdc"] for t in realized), 2)
    win_rate  = round(len(wins) / len(realized) * 100, 1) if realized else None

    return {
        "trades": enriched,
        "stats": {
            "total":    len(enriched),
            "open":     len(open_t),
            "closed":   len(closed),
            "realized": len(realized),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
        },
    }


@app.get("/api/no-bot/open-positions")
async def get_no_bot_open_positions():
    trades      = _load_no_trades()
    open_trades = [t for t in trades if t.get("status") in OPEN_NO_STATUSES]

    unique_pairs: dict = {}
    for t in open_trades:
        station = (t.get("station") or "").lower()
        date    = t.get("date")
        if not station or not date or station not in STATIONS:
            continue
        key = (station, date)
        if key not in unique_pairs:
            unique_pairs[key] = STATIONS[station]["pm_query"].lower()

    pair_items = list(unique_pairs.items())
    fetched = (
        await asyncio.gather(
            *[_fetch_event_markets(city, date) for ((_st, date), city) in pair_items],
            return_exceptions=False,
        )
        if pair_items else []
    )
    markets_by_pair: dict = {}
    for (key, _city), markets in zip(pair_items, fetched):
        markets_by_pair[key] = markets

    def _no_price_for(markets: list, no_token_id: str) -> Optional[float]:
        if not no_token_id:
            return None
        cid = str(no_token_id)
        for m in markets:
            clob_raw = m.get("clobTokenIds", "[]")
            if isinstance(clob_raw, str):
                try:    clob_tokens = json.loads(clob_raw)
                except Exception: clob_tokens = []
            else:
                clob_tokens = clob_raw if isinstance(clob_raw, list) else []
            no_tok = clob_tokens[1] if len(clob_tokens) > 1 else None
            if not (no_tok and str(no_tok) == cid):
                continue
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                try:    prices = json.loads(prices)
                except Exception: prices = []
            if prices and len(prices) >= 2:
                try:    return round(float(prices[1]), 4)
                except Exception: return None
        return None

    positions        = []
    total_at_risk    = 0.0
    total_mtm        = 0.0
    total_unrealized = 0.0
    has_any_unreal   = False

    for t in open_trades:
        station  = (t.get("station") or "").lower()
        date     = t.get("date")
        cid      = t.get("condition_id")
        fill_p      = t.get("fill_price")
        shares      = float(t.get("shares") or 0)
        # cost_usdc is a YES-bot field; NO-bot uses fill_price × shares
        _raw_cost   = t.get("cost_usdc")
        cost        = float(_raw_cost) if _raw_cost else (float(fill_p) * shares if fill_p else 0.0)
        status      = t.get("status")
        # NO-bot stores the actual NO token id in "no_token_id" (not condition_id)
        no_token_id = t.get("no_token_id") or cid
        markets     = markets_by_pair.get((station, date), [])
        no_price    = _no_price_for(markets, no_token_id)

        at_risk = cost
        if fill_p is not None and no_price is not None:
            mtm        = round(no_price * shares, 4)
            unrealized = round((no_price - float(fill_p)) * shares, 4)
            unreal_pct = round((unrealized / cost) * 100, 2) if cost else None
            has_any_unreal   = True
            total_mtm        += mtm
            total_unrealized += unrealized
        elif fill_p is None:
            mtm        = cost
            unrealized = 0.0
            unreal_pct = 0.0
            total_mtm += mtm
        else:
            mtm = unrealized = unreal_pct = None

        total_at_risk += at_risk

        market_url = None
        if station in STATIONS:
            city       = STATIONS[station]["pm_query"].lower()
            market_url = f"https://polymarket.com/event/{pm_slug(city, date)}"

        positions.append({
            "id":             t.get("id"),
            "station":        station,
            "date":           date,
            "bucket_title":   t.get("bucket_title"),
            "notes":          t.get("notes"),
            "fill_price":     fill_p,
            "limit_price":    t.get("limit_price"),
            "shares":         shares,
            "cost_usdc":      cost,
            "status":         status,
            "current_price":  no_price,
            "unrealized_pnl": unrealized,
            "unrealized_pct": unreal_pct,
            "mark_to_market": mtm,
            "market_url":     market_url,
            "sell_price":     t.get("sell_price"),
            "sell_order_id":  t.get("sell_order_id"),
            "placed_at":      t.get("placed_at"),
            "fill_time":      t.get("fill_time"),
        })

    unreal_pct_total = (
        round((total_unrealized / total_at_risk) * 100, 2)
        if (has_any_unreal and total_at_risk > 0) else None
    )

    return {
        "positions": positions,
        "totals": {
            "open_count":          len(positions),
            "at_risk_usdc":        round(total_at_risk, 2),
            "mark_to_market_usdc": round(total_mtm, 2),
            "unrealized_pnl":      round(total_unrealized, 2) if has_any_unreal else None,
            "unrealized_pct":      unreal_pct_total,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/live-trades/scan")
async def live_scan(x_api_token: str = Header(default="")):
    """Gerçek para ile live scan tetikle."""
    require_token(x_api_token)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["python3", "bot/scanner.py", "scan", "--live"],
            capture_output=True, text=True, timeout=300,
            cwd="/root/weather"
        )
        return {"ok": True, "output": result.stdout + result.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/live-trades/check-fills")
async def live_check_fills(x_api_token: str = Header(default="")):
    """Order dolumlarını kontrol et (trader.py check-fills)."""
    require_token(x_api_token)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["python3", "bot/trader.py", "check-fills"],
            capture_output=True, text=True, timeout=120,
            cwd="/root/weather"
        )
        return {"ok": True, "output": result.stdout + result.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/live-trades/settle")
async def live_settle(x_api_token: str = Header(default="")):
    """Geçmiş pozisyonları settle et (trader.py settle)."""
    require_token(x_api_token)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["python3", "bot/trader.py", "settle"],
            capture_output=True, text=True, timeout=180,
            cwd="/root/weather"
        )
        return {"ok": True, "output": result.stdout + result.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trader-health")
async def trader_health():
    """Trader cron son çalışma zamanı + istatistikleri."""
    log_file = Path("/root/weather/bot/trader.log")
    if not log_file.exists():
        return {"last_run": None, "fills_today": 0, "entries": []}
    try:
        lines   = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        entries = []
        for line in lines[-200:]:
            if "FILL" in line or "ORDER" in line or "check-fills" in line.lower():
                entries.append(line.strip())
        fills = sum(1 for l in lines[-500:] if "✅ FILL" in l)
        last_line = next(
            (l for l in reversed(lines) if l.strip()), None
        )
        return {"last_run": last_line, "fills_today": fills, "entries": entries[-20:]}
    except Exception as e:
        return {"error": str(e)}


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
