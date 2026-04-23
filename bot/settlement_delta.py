#!/usr/bin/env python3
"""
Settlement Delta Calibrator (Faz 7) — WU ↔ Open-Meteo sistematik fark öğrenimi.

Problem: Polymarket marketleri Weather Underground (WU) verisiyle settle olur;
bot ise Open-Meteo arşivinden ölçüm alır (tahmin kaynağı ile uyumlu).
İki kaynak arasında istasyon × mevsim bazlı sistematik sapma var.

Örnek: LFPG için WU günlük max tipik +1.9°C Open-Meteo üstünde → top_pick 17°C
olsa bile gerçek settle 19°C'ye yakın → yanlış bucket seçimi.

Mevcut veri kaynağı: `settlement_audit` tablosu (Faz 6b) — her gün her kaynak
için günlük max kaydediyor. Bu modül delta = WU - Open-Meteo hesaplar, rolling
medyan olarak blend'e eklenir.

Kullanım:
    from bot.settlement_delta import learn_station_delta, apply_delta

    delta = learn_station_delta("lfpg")     # rolling 60 gün median
    adjusted_top_pick = apply_delta("lfpg", top_pick=17)  # → 19

Fallback: yeterli veri yoksa (<5 gün çift kaynak), delta=0 (etkisiz).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

# Minimum çift-kaynaklı gözlem sayısı delta güvenilir sayılsın
MIN_PAIRED_SAMPLES = 5

# Delta tavanı — saçma değerleri kes (anomalik gün blend'i mahvetmesin)
MAX_DELTA_C = 3.0

# Rolling pencere
DEFAULT_WINDOW_DAYS = 60


def compute_station_deltas(
    days: int = DEFAULT_WINDOW_DAYS,
    db_path: Path | None = None,
) -> dict:
    """Tüm istasyonlar için WU vs Open-Meteo rolling medyan deltası.

    Not: `wu` kaynağı henüz audit'e yazılmıyor — Polymarket WU API'sini ayrı
    bir resolver'dan almak için `bot/wu_resolver.py` (gelecek iş) gerekir.
    Şimdilik mevcut iki kaynak ("open-meteo" vs "metar") arasındaki farkı da
    proxy olarak kullanıyoruz — METAR çoğu istasyonda WU'ya daha yakın.

    Döner: {station: {"delta": float, "n": int, "source_pair": "wu-om"|"metar-om"}}
    """
    try:
        from bot.db import DB_PATH, get_db
        path = db_path or DB_PATH
    except Exception:
        return {}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sql = """
        SELECT station, date, source, actual_temp
        FROM settlement_audit
        WHERE date >= ? AND actual_temp IS NOT NULL
        ORDER BY station, date
    """

    try:
        with get_db(path, readonly=True) as conn:
            rows = conn.execute(sql, (cutoff,)).fetchall()
    except Exception:
        return {}

    # İstasyon + tarih bazında gruplandır
    by_key: dict = {}
    for r in rows:
        key = (r[0], r[1])
        by_key.setdefault(key, {})[r[2]] = r[3]

    # İstasyon bazlı paired deltaları topla
    station_pairs: dict = {}
    for (station, date), sources in by_key.items():
        # Öncelik: wu - open-meteo; fallback: metar - open-meteo
        om = sources.get("open-meteo")
        wu = sources.get("wu")
        mt = sources.get("metar")
        if om is None:
            continue
        delta = None
        pair_type = None
        if wu is not None:
            delta = wu - om
            pair_type = "wu-om"
        elif mt is not None:
            delta = mt - om
            pair_type = "metar-om"
        if delta is None:
            continue
        # Aşırı uç değeri filtrele
        if abs(delta) > MAX_DELTA_C * 2:  # ≥6°C anomalik → atla
            continue
        lst = station_pairs.setdefault(station, {"deltas": [], "pair": pair_type})
        lst["deltas"].append(delta)

    # Medyan hesap
    out: dict = {}
    for station, info in station_pairs.items():
        n = len(info["deltas"])
        if n < MIN_PAIRED_SAMPLES:
            continue
        deltas = sorted(info["deltas"])
        median = deltas[n // 2] if n % 2 == 1 else (deltas[n // 2 - 1] + deltas[n // 2]) / 2
        # Tavana kırp
        median = max(-MAX_DELTA_C, min(MAX_DELTA_C, median))
        out[station] = {
            "delta":       round(median, 2),
            "n":           n,
            "source_pair": info["pair"],
        }
    return out


def learn_station_delta(
    station: str,
    days: int = DEFAULT_WINDOW_DAYS,
    db_path: Path | None = None,
) -> float:
    """Tek istasyon için delta — yeterli veri yoksa 0.0."""
    deltas = compute_station_deltas(days=days, db_path=db_path)
    info = deltas.get(station)
    if info is None:
        return 0.0
    return float(info["delta"])


def apply_delta(
    station: str,
    top_pick: int,
    days: int = DEFAULT_WINDOW_DAYS,
    db_path: Path | None = None,
) -> int:
    """top_pick'e settlement delta'yı uygular (round) → adjusted top_pick."""
    delta = learn_station_delta(station, days=days, db_path=db_path)
    if delta == 0:
        return top_pick
    return int(round(top_pick + delta))


def summary(days: int = DEFAULT_WINDOW_DAYS, db_path: Path | None = None) -> list:
    """Tüm istasyonlar için delta özeti (dashboard için)."""
    deltas = compute_station_deltas(days=days, db_path=db_path)
    return [
        {"station": s, **info}
        for s, info in sorted(deltas.items())
    ]
