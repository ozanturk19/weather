#!/usr/bin/env python3
"""
Kalman filter tabanlı istasyon bazlı sistematik tahmin biası tahmincisi.

Eski basit ortalama yaklaşımı:
  bias = mean(actual - top_pick)
  Sorun: her gözleme eşit ağırlık, mevsim değişirken bile eski verinin ağırlığı
  bugünküyle aynı. Sıcak→soğuk geçişlerde bias yavaş adapte oluyor.

Yeni Kalman filtresi:
  - State:  bias (°C), variance P
  - Process noise Q: bias günlük sürüklenme hızı (mevsim kayması)
  - Observation noise R: tek gözlem gürültüsü (ölçüm + ensemble belirsizliği)
  - Yeni gözlem ağırlığı = P_pred / (P_pred + R)  → hızlı başta, yavaş sonda
  - Eski veri üzerindeki etki P büyüdükçe (Q nedeniyle) azalır → mevsim adapte

Kullanım:
    from bot.kalman import kalman_bias_estimate
    bias, variance = kalman_bias_estimate(
        [(-0.5, "2026-03-01"), (+1.2, "2026-03-05"), ...]
    )
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

# ── Ayarlar (fiziksel/sezgisel temel) ──────────────────────────────────────
# Q = 0.04 → bias günlük sürüklenme std ~0.2°C. Bir ay boyunca ~1°C.
#   Bu, mevsim geçişlerinde (örn. Ekim→Kasım) bias'ın kayabileceğini varsayar.
# R = 1.0 → tek gözlem std ~1°C. ensemble top_pick - actual tipik saçılımı.
# P0 = 4.0 → başlangıç belirsizliği ±2°C, prior nötr.
KALMAN_Q       = 0.04
KALMAN_R       = 1.0
KALMAN_P_INIT  = 4.0
# Günlere göre zaman ayarı — iki ölçüm arası çoksa Q orantılı büyür
KALMAN_TIME_SCALE = True


def kalman_bias_estimate(
    observations: list[tuple[float, str]],
    q: float = KALMAN_Q,
    r: float = KALMAN_R,
    p_init: float = KALMAN_P_INIT,
) -> tuple[float, float]:
    """Kalman filtreyle bias tahmini.

    observations: [(delta, date_iso), ...] — delta = actual - top_pick,
                  date_iso: "YYYY-MM-DD" (kronolojik sıraya göre sıralanır).

    Döner: (bias_mean, bias_variance)
      bias_mean: point-estimate bias (°C)
      bias_variance: tahmin belirsizliği (σ²) — ne kadar düşük, o kadar emin.
    """
    if not observations:
        return 0.0, p_init

    # Tarihe göre sırala (eski → yeni)
    obs = sorted(observations, key=lambda o: o[1])

    x = 0.0           # prior bias: nötr
    p = p_init
    prev_date: datetime | None = None

    for delta, date_str in obs:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            d = None

        # Predict: P_pred = P + Q·dt (zaman aralığı büyükse daha fazla drift)
        if KALMAN_TIME_SCALE and prev_date is not None and d is not None:
            days = max(1, (d - prev_date).days)
            q_eff = q * days
        else:
            q_eff = q
        p_pred = p + q_eff
        x_pred = x

        # Update: Kalman gain
        k_gain = p_pred / (p_pred + r)
        x = x_pred + k_gain * (delta - x_pred)
        p = (1 - k_gain) * p_pred

        prev_date = d

    return round(x, 3), round(p, 3)


def kalman_station_biases(
    trades: list,
    max_correction: int = 2,
    min_trades: int = 5,
) -> dict:
    """Kapalı trade'lerden istasyon bazlı Kalman bias dict'i üret.

    Döner: {station: bias_int}  — bias, compute_station_biases ile uyumlu
    olarak en yakın tam sayıya yuvarlanır ve ±max_correction ile tavanlanır.
    """
    import math
    from collections import defaultdict

    per_station: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for t in trades:
        if (
            t.get("status") == "closed"
            and t.get("actual_temp") is not None
            and t.get("top_pick") is not None
            and t.get("date")
        ):
            delta = t["actual_temp"] - t["top_pick"]
            per_station[t["station"]].append((delta, t["date"]))

    biases: dict[str, int] = {}
    for station, obs in per_station.items():
        if len(obs) < min_trades:
            continue
        mean, _var = kalman_bias_estimate(obs)
        # Python banker's rounding'i atla
        bias = math.floor(mean + 0.5) if mean >= 0 else -math.floor(-mean + 0.5)
        bias = max(-max_correction, min(max_correction, bias))
        if bias != 0:
            biases[station] = bias
    return biases


# ── SQLite entegrasyonu (Faz 1 tabloları kullanılır) ───────────────────────
def persist_bias_to_db(
    station: str,
    date: str,
    measured_err: float,
    bias_est: float,
    uncertainty: float,
    correction: float,
    db_path: Path | None = None,
) -> None:
    """Kalman tahminini bias_corrections tablosuna ekle (analitik için).

    Her settlement sonrası tek bir satır eklenir. History zaman içinde
    büyür, dashboard trend takibine izin verir.
    """
    try:
        from bot.db import DB_PATH, get_db
        path = db_path or DB_PATH
        with get_db(path) as conn:
            conn.execute(
                """INSERT INTO bias_corrections
                   (station, date, measured_err, bias_est, uncertainty, correction)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (station, date, measured_err, bias_est, uncertainty, correction),
            )
    except Exception:
        pass   # sessiz: bot akışını bozmasın
