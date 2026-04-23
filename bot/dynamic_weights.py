#!/usr/bin/env python3
"""
İstasyon × model bazlı dinamik ağırlıklandırma (Faz 4).

Mevcut sistem: MODEL_WEIGHTS statik — tüm istasyonlar için aynı.
  ECMWF=1.5, ICON=1.8, GFS=1.0, UKMO=0.5, METEOFRANCE=0.9

Problem:
  - LTFM'de UKMO MAE=3.22°C ama genel ağırlık hâlâ 0.5 → hâlâ blend'e karışıyor
  - Bazı istasyonlarda ICON > ECMWF, bazılarında tam tersi

Çözüm: rolling 30 günlük RMSE'nin tersini ağırlık olarak kullan — istasyon+horizon
bazlı. Yeterli veri yoksa statik ağırlığa düş (güvenli fallback).

Kaynak veri: model_forecasts tablosu (scanner/main.py tarafından doldurulur).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

# Faz 4 parametreleri
ROLLING_DAYS       = 30      # kaç günlük pencereden RMSE hesaplansın
MIN_SAMPLES_MODEL  = 10      # bir model için min. kaydedilmiş örnek (az ise düş)
RMSE_EPSILON       = 0.3     # bölünme güvenliği + en iyi modelde bile floor

# Faz 7 — Bayes cold-start prior.
# Yeterli örneklem yokken (n < MIN_SAMPLES_MODEL) modeli tamamen atmak yerine
# modele özel tipik RMSE (prior) ile shrinkage uygula:
#   posterior = (n·observed + k·prior) / (n + k)
# k (pseudo-örnek) = PRIOR_STRENGTH; prior = PRIOR_RMSE.
# Sonuç: early days'te statik yakınında hafif dinamik tat, n büyüdükçe gözlem
# dominant. n=0 (hiç veri) → posterior = prior → statik davranışa yakın.
PRIOR_STRENGTH = 10          # prior 10 sözde örneklem kadar ağır
PRIOR_RMSE = {
    "ecmwf":       2.2,      # endüstri baseline (Temp24h global ~2-2.5°C)
    "icon":        2.1,
    "aifs":        2.0,      # AI tabanlı, 2025 benchmark'larda biraz daha iyi
    "gfs":         2.5,
    "ukmo":        2.8,      # daha zayıf referans
    "meteofrance": 2.5,
}
DEFAULT_PRIOR_RMSE = 2.5     # tanımsız model için jenerik prior

# Yeterli veri eşiği — shrinkage ile MIN_SAMPLES_MODEL altı da kabul edilir
MIN_SAMPLES_SHRINKAGE = 3    # tamamen veri yoksa prior'a göre de çok anlamsız

# Statik fallback — main.py ile senkron (değişirse oradan import edin)
STATIC_WEIGHTS = {
    "ecmwf":       1.5,
    "icon":        1.8,
    "gfs":         1.0,
    "ukmo":        0.5,
    "meteofrance": 0.9,
    "aifs":        1.6,     # ECMWF AIFS (AI tabanlı) — 2025 itibarıyla aktif
}


def compute_rolling_rmse(
    station: str,
    horizon_days: int | None = None,
    days: int = ROLLING_DAYS,
    db_path: Path | None = None,
) -> dict:
    """Son `days` günde her model için RMSE hesapla.

    Döner: {model: {"rmse": float, "n": int}}  — sadece settle edilmiş kayıtlar.
    """
    try:
        from bot.db import DB_PATH, get_db
        path = db_path or DB_PATH
    except Exception:
        return {}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sql = """
        SELECT model, AVG(abs_error * abs_error) as mse, COUNT(*) as n
        FROM model_forecasts
        WHERE station    = ?
          AND date       >= ?
          AND actual_temp IS NOT NULL
    """
    params: list = [station, cutoff]
    if horizon_days is not None:
        sql += " AND horizon_days = ?"
        params.append(horizon_days)
    sql += " GROUP BY model"

    try:
        with get_db(path, readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return {}

    out: dict = {}
    for row in rows:
        model, mse, n = row[0], row[1], row[2]
        if mse is None or n < 1:
            continue
        out[model] = {"rmse": round(math.sqrt(mse), 3), "n": int(n)}
    return out


def _posterior_rmse(observed_rmse: float, n: int, model: str) -> float:
    """Bayes shrinkage (Faz 7): gözlem + prior ağırlıklı birleşim."""
    prior = PRIOR_RMSE.get(model, DEFAULT_PRIOR_RMSE)
    k     = PRIOR_STRENGTH
    return (n * observed_rmse + k * prior) / (n + k)


def compute_dynamic_weights(
    station: str,
    horizon_days: int | None = None,
    days: int = ROLLING_DAYS,
    db_path: Path | None = None,
) -> dict | None:
    """İstasyon × horizon için 1/RMSE tabanlı normalize ağırlık dict'i.

    Döner:
      None — yeterli veri yok (statik ağırlığa düş)
      {model: weight, ...} — Bayes shrinkage ile cold-start destekli

    Faz 7: MIN_SAMPLES_MODEL altı modelleri eskiden düşürüyorduk. Artık
    posterior = (n·gözlem + k·prior) / (n + k) ile shrinkage uygulanır.
    n çok küçükse (≤ MIN_SAMPLES_SHRINKAGE) yine güvenli tarafta kal.
    """
    rmse_map = compute_rolling_rmse(station, horizon_days, days, db_path)
    usable = {m: v for m, v in rmse_map.items() if v["n"] >= MIN_SAMPLES_SHRINKAGE}
    if len(usable) < 2:
        return None

    # Bayes posterior ile düzleştir — sert örneklem eşikleri yumuşar
    posterior = {
        m: _posterior_rmse(v["rmse"], v["n"], m) for m, v in usable.items()
    }
    raw = {m: 1.0 / (r + RMSE_EPSILON) for m, r in posterior.items()}
    total = sum(raw.values())
    if total <= 0:
        return None
    # ortalama 1.0 civarı normalize (eski davranış korundu)
    return {m: round(w / total * len(usable), 3) for m, w in raw.items()}


def effective_weights(
    station: str,
    horizon_days: int | None = None,
    db_path: Path | None = None,
) -> tuple[dict, str]:
    """Kullanılacak ağırlıkları döndür: dinamik varsa onu, yoksa statiği.

    Döner: (weights_dict, source)  — source: "dynamic" | "static"
    """
    dyn = compute_dynamic_weights(station, horizon_days, db_path=db_path)
    if dyn is not None:
        # Statik ağırlığı eksik modelleri korumak için birleştir
        merged = dict(STATIC_WEIGHTS)
        merged.update(dyn)
        return merged, "dynamic"
    return dict(STATIC_WEIGHTS), "static"


def persist_weights_to_db(
    station: str,
    weights: dict,
    rmse_map: dict,
    db_path: Path | None = None,
) -> None:
    """Hesaplanan ağırlıkları model_weights tablosuna geçmiş için yaz."""
    try:
        from bot.db import DB_PATH, get_db
        path = db_path or DB_PATH
        with get_db(path) as conn:
            for model, w in weights.items():
                info = rmse_map.get(model, {})
                conn.execute(
                    """INSERT INTO model_weights (station, model, weight, rmse_30d, n_samples)
                       VALUES (?, ?, ?, ?, ?)""",
                    (station, model, w, info.get("rmse"), info.get("n")),
                )
    except Exception:
        pass
