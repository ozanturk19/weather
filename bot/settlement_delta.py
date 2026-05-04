#!/usr/bin/env python3
"""
Settlement Delta Calibrator (Faz 7) — WU ↔ Open-Meteo sistematik fark öğrenimi.

Problem: Polymarket marketleri Weather Underground (WU) verisiyle settle olur;
bot ise Open-Meteo arşivinden ölçüm alır (tahmin kaynağı ile uyumlu).
İki kaynak arasında istasyon × mevsim bazlı sistematik sapma var.

Örnek: LFPG için WU günlük max tipik +1.4°C Open-Meteo üstünde → top_pick 17°C
olsa bile gerçek settle 19°C'ye yakın → yanlış bucket seçimi.

Mevcut veri kaynağı: `settlement_audit` tablosu (Faz 6b) — her gün her kaynak
için günlük max kaydediyor. Bu modül delta = WU - Open-Meteo hesaplar, rolling
medyan olarak blend'e eklenir.

Kullanım:
    from bot.settlement_delta import learn_station_delta, apply_delta

    delta = learn_station_delta("lfpg")     # rolling 30 gün median
    adjusted_top_pick = apply_delta("lfpg", top_pick=17)  # → 19

Fallback (Faz A2): yeterli gözlem yoksa (<3 çift kaynak), STATION_DELTA_PRIORS
kullanılır. Bu değerler METAR-OM proxy analizi + ilk 3 haftalık canlı settlement
verisinden türetilmiştir; WU doğrudan entegre edilene kadar muhafazakâr tutulur.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

# ── Faz A1: Hız ayarları ───────────────────────────────────────────────────
# Önceki: MIN_PAIRED_SAMPLES=5, DEFAULT_WINDOW_DAYS=60, D+2 dampening=0.70
# Yeni:   3 gözlem yeterli (daha hızlı aktivasyon), 30 gün pencere (daha güncel),
#         D+2 dampening 0.85 (settlement bias D+2'de de güçlü).

# Minimum çift-kaynaklı gözlem sayısı delta güvenilir sayılsın
MIN_PAIRED_SAMPLES = 3

# Delta tavanı — saçma değerleri kes (anomalik gün blend'i mahvetmesin)
MAX_DELTA_C = 3.0

# Rolling pencere (gün)
DEFAULT_WINDOW_DAYS = 30

# Faz 8/A1: horizon-specific dampening
# D+2'de hem model skew hem settlement skew birlikte var. %85'e çıkarıldı:
# bias tutarlı olduğunda agresif düzeltme > ihtiyatlı ton-down.
HORIZON_DELTA_DAMPENING: dict[int, float] = {
    1: 1.0,    # D+1: tam uygula
    2: 0.85,   # D+2: %15 azalt (eski: %30 — çok muhafazakârdı)
}

# ── Faz A2: İstasyon prior'ları (cold-start kalibrasyonu) ──────────────────
# Kaynak: METAR − Open-Meteo audit analizi (ilk 3 hafta, n=1-3 gözlem)
# Sadece tutarlı pozitif bias olan istasyonlara prior uygulandı.
# EGLC, EFHK, LEMD: METAR proxy güvenilmez (WU lokasyonu farklı) → 0.0
# Değerler bilinçli olarak muhafazakâr (gerçek deltanın ~%70'i):
# gerçek WU entegrasyonu tamamlandığında üzerine yazılacak.
STATION_DELTA_PRIORS: dict[str, float] = {
    "eddm":  1.0,   # METAR-OM median +1.50°C (3 gün, tutarlı) → prior 1.0
    "eham":  0.4,   # METAR-OM median +0.40°C (3 gün, tutarlı)
    "lfpg":  0.8,   # METAR-OM +1.40°C (1 gün) — muhafazakâr
    "limc":  0.8,   # METAR-OM +1.75°C (2 gün) — muhafazakâr
    "ltac":  0.5,   # METAR-OM median +1.20°C ama yüksek varyasyon → dikkatli
    "ltfm":  0.4,   # METAR-OM median +0.40°C (3 gün, tutarlı)
    "eglc":  0.0,   # METAR < OM (airport kanalı farkı); WU belirsiz
    "efhk":  0.0,   # karma (-0.40), WU lokasyonu farklı
    "epwa":  0.0,   # nötr (median 0.0, 3 gün)
    "lemd":  0.0,   # negatif eğilim (-0.70), az veri
    "rjtt":  0.0,   # 1 gün veri, nötr (-0.10)
    "rksi":  0.0,   # yapısal soğuk bias ayrı ele alınıyor
    "vhhh":  0.0,   # henüz yeterli audit yok
    "omdb":  0.0,   # çölde METAR-WU farkı farklı dinamik
}


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
    except Exception as _db_err:
        # DB erişimi başarısız → prior'lara fallback olacak, ama sessiz kalmamalı.
        print(f"  ⚠️  settlement_delta DB erişim hatası: {_db_err}")
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
    horizon_days: int | None = None,
) -> float:
    """Tek istasyon için delta.

    Öncelik sırası:
      1. Yeterli gözlem (n >= MIN_PAIRED_SAMPLES): rolling medyan kullan
      2. Az gözlem (n < threshold): STATION_DELTA_PRIORS kullan (Faz A2)
      3. Hiç gözlem yoksa: prior (ya da 0)

    Faz 8/A1: horizon_days verilmişse HORIZON_DELTA_DAMPENING çarpanı uygulanır.
    """
    deltas = compute_station_deltas(days=days, db_path=db_path)
    info = deltas.get(station)

    if info is not None:
        # Yeterli gözlem var → rolling medyan kullan
        raw = float(info["delta"])
    else:
        # Gözlem yok ya da threshold altı → prior kullan (Faz A2)
        raw = STATION_DELTA_PRIORS.get(station, 0.0)

    if raw == 0.0:
        return 0.0

    if horizon_days is None:
        return raw
    factor = HORIZON_DELTA_DAMPENING.get(int(horizon_days), 1.0)
    return round(raw * factor, 2)


def apply_delta(
    station: str,
    top_pick: int,
    days: int = DEFAULT_WINDOW_DAYS,
    db_path: Path | None = None,
    horizon_days: int | None = None,
) -> int:
    """top_pick'e settlement delta'yı uygular (round) → adjusted top_pick.

    horizon_days geçilirse horizon-specific dampening aktif (Faz 8/A1).
    """
    delta = learn_station_delta(
        station, days=days, db_path=db_path, horizon_days=horizon_days,
    )
    if delta == 0:
        return top_pick
    return int(round(top_pick + delta))


def summary(days: int = DEFAULT_WINDOW_DAYS, db_path: Path | None = None) -> list:
    """Tüm istasyonlar için delta özeti (dashboard için)."""
    deltas = compute_station_deltas(days=days, db_path=db_path)
    # Prior'ları da dahil et (gözlem yoksa prior göster)
    all_stations = set(deltas.keys()) | set(STATION_DELTA_PRIORS.keys())
    result = []
    for s in sorted(all_stations):
        if s in deltas:
            result.append({"station": s, **deltas[s], "source": "observed"})
        else:
            prior = STATION_DELTA_PRIORS.get(s, 0.0)
            if prior != 0.0:
                result.append({
                    "station": s,
                    "delta": prior,
                    "n": 0,
                    "source_pair": "prior",
                    "source": "prior",
                })
    return result
