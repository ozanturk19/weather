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

# Statik fallback — main.py ile senkron (değişirse oradan import edin)
STATIC_WEIGHTS = {
    "ecmwf":       1.5,
    "icon":        1.8,
    "gfs":         1.0,
    "ukmo":        0.5,
    "meteofrance": 0.9,
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


def compute_dynamic_weights(
    station: str,
    horizon_days: int | None = None,
    days: int = ROLLING_DAYS,
    db_path: Path | None = None,
) -> dict | None:
    """İstasyon × horizon için 1/RMSE tabanlı normalize ağırlık dict'i.

    Döner:
      None — yeterli veri yok (statik ağırlığa düş)
      {model: weight, ...} — min 2 modelde ≥MIN_SAMPLES_MODEL örnek varsa
    """
    rmse_map = compute_rolling_rmse(station, horizon_days, days, db_path)
    # Yeterli kayıt olan modelleri süz
    good = {m: v for m, v in rmse_map.items() if v["n"] >= MIN_SAMPLES_MODEL}
    if len(good) < 2:
        return None

    raw = {m: 1.0 / (v["rmse"] + RMSE_EPSILON) for m, v in good.items()}
    total = sum(raw.values())
    if total <= 0:
        return None
    return {m: round(w / total * len(good), 3) for m, w in raw.items()}   # ortalama 1.0 civarı normalize


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
