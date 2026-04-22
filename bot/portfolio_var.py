#!/usr/bin/env python3
"""Portföy bazlı VaR (Faz 6a) — Monte Carlo + Gaussian copula + istasyon korelasyon.

Problem: açık pozisyonları bağımsız kazanma olasılıklarından türetmek yetersiz —
istanbul-ankara, milano-paris gibi yakın istasyonlar benzer hava sistemlerinde
birlikte iner/kalkar. Pozisyonlar korele ise worst-case, bağımsız varsayımdan
çok daha sert olabilir.

Çözüm:
  1. forecast_errors tablosundan istasyon çiftleri için Pearson korelasyon
     (blend-actual hatalarının) hesapla. Yetersiz veri → DEFAULT_CORR (0.3).
  2. Shrinkage ile pozitif-tanımlı hale getir → Cholesky.
  3. N simülasyon: çok değişkenli normal → Φ(x) ile üniform → binary outcome.
  4. Her simülasyonda toplam P&L → 5% / 1% percentile = VaR.

Stdlib'den çıkmıyoruz (numpy/scipy yok). Deterministik seed — trade ID hash.
"""
from __future__ import annotations

import math
import random
import statistics
from datetime import datetime, timedelta
from pathlib import Path

# Parametreler
N_SIMS        = 5000
DEFAULT_CORR  = 0.3      # veri yetersizse istasyonlar arası varsayım
MIN_PAIRED    = 5        # çift için minimum ortak gün sayısı
SHRINKAGE     = 0.15     # λ·I + (1-λ)·R → PSD garantisine yaklaş
CHOL_JITTER   = 1e-8     # Cholesky diagonal loading (num. stabilite)


def station_error_series(
    station: str, days: int = 60, db_path: Path | None = None
) -> dict[str, float]:
    """Son `days` gün: date -> (blend - actual_temp). Sadece settle edilmişler."""
    try:
        from bot.db import DB_PATH, get_db
        path = db_path or DB_PATH
    except Exception:
        return {}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with get_db(path, readonly=True) as conn:
            rows = conn.execute(
                """SELECT date, (blend - actual_temp) AS err
                   FROM forecast_errors
                   WHERE station = ? AND date >= ? AND actual_temp IS NOT NULL
                         AND blend IS NOT NULL""",
                (station, cutoff)
            ).fetchall()
    except Exception:
        return {}
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def pearson(x: list[float], y: list[float]) -> float | None:
    """Pearson korelasyonu. Yetersiz/sabit veri → None. [-0.99, 0.99]'a klip."""
    if len(x) < 3 or len(x) != len(y):
        return None
    try:
        mx, my = statistics.fmean(x), statistics.fmean(y)
        num = sum((a - mx) * (b - my) for a, b in zip(x, y))
        dx  = math.sqrt(sum((a - mx) ** 2 for a in x))
        dy  = math.sqrt(sum((b - my) ** 2 for b in y))
        if dx == 0 or dy == 0:
            return None
        r = num / (dx * dy)
        return max(-0.99, min(0.99, r))
    except Exception:
        return None


def station_correlation(
    stations: list[str], days: int = 60, db_path: Path | None = None
) -> list[list[float]]:
    """NxN korelasyon matrisi — shrinkage ile I'ye çekilmiş."""
    n = len(stations)
    series = {s: station_error_series(s, days, db_path) for s in stations}
    raw = [[1.0 if i == j else DEFAULT_CORR for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            a, b = series[stations[i]], series[stations[j]]
            common = sorted(set(a) & set(b))
            if len(common) >= MIN_PAIRED:
                r = pearson([a[d] for d in common], [b[d] for d in common])
                if r is not None:
                    raw[i][j] = raw[j][i] = r
    # Shrinkage: (1-λ)·R + λ·I → PSD'ye yaklaştır (James-Stein tarzı)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            out[i][j] = 1.0 if i == j else raw[i][j] * (1.0 - SHRINKAGE)
    return out


def cholesky(mat: list[list[float]]) -> list[list[float]]:
    """Cholesky dekompozisyonu. Alt üçgen L: L·L^T = mat.

    PSD değilse jitter ekler (numerik stabilite). Production'da yetecek kadar
    tolerantlı.
    """
    n = len(mat)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                val = mat[i][i] - s
                if val <= 0:
                    val = CHOL_JITTER
                L[i][j] = math.sqrt(val)
            else:
                # L[j][j] her zaman pozitif (yukarıdaki if sayesinde)
                L[i][j] = (mat[i][j] - s) / L[j][j]
    return L


def simulate_portfolio(
    positions: list[dict],
    correlation: list[list[float]],
    n_sims: int = N_SIMS,
    seed: int = 0,
) -> list[float]:
    """Çok değişkenli Gaussian copula → binary outcome → toplam P&L.

    positions: [{"p_win": 0..1, "potential_win": float, "cost": float}, ...]
    Döner: n_sims uzunluğunda P&L listesi (sıralanmamış).
    """
    rng = random.Random(seed)
    n = len(positions)
    if n == 0:
        return []
    L = cholesky(correlation)
    nd = statistics.NormalDist()
    pnls: list[float] = []
    for _ in range(n_sims):
        z = [rng.gauss(0, 1) for _ in range(n)]
        # x = L·z  (alt üçgen çarpım)
        x = [sum(L[i][k] * z[k] for k in range(i + 1)) for i in range(n)]
        total = 0.0
        for i, pos in enumerate(positions):
            u = nd.cdf(x[i])
            # u < p_win  ⇒  kazandı  (üniform[0,1] altında probabilistic bucket)
            if u < pos["p_win"]:
                total += pos["potential_win"]
            else:
                total -= pos["cost"]
        pnls.append(total)
    return pnls


def portfolio_var(
    trades: list[dict],
    days: int = 60,
    n_sims: int = N_SIMS,
    db_path: Path | None = None,
) -> dict:
    """Açık trade listesinden portföy VaR'ını üret.

    Döner:
      {n_positions, n_sims, expected_pnl, var_95, var_99, worst, best,
       stations, avg_abs_correlation, gross_exposure, gross_potential_win}
    """
    if not trades:
        return {
            "n_positions": 0, "n_sims": 0,
            "expected_pnl": 0.0, "var_95": 0.0, "var_99": 0.0,
            "worst": 0.0, "best": 0.0, "stations": [],
            "avg_abs_correlation": 0.0,
            "gross_exposure": 0.0, "gross_potential_win": 0.0,
        }

    positions = []
    for t in trades:
        p = t.get("mode_pct")
        p_win = (float(p) / 100.0) if p is not None else 0.5
        p_win = max(0.01, min(0.99, p_win))
        positions.append({
            "station":       t.get("station"),
            "p_win":         p_win,
            "potential_win": float(t.get("potential_win", 0.0)),
            "cost":          float(t.get("cost_usd") or t.get("size_usd", 0.0) or 0.0),
        })

    stations = [p["station"] for p in positions]
    corr     = station_correlation(stations, days=days, db_path=db_path)

    # Deterministik seed — trade ID hash (aynı portföy = aynı sonuç)
    seed = hash(tuple(sorted(str(t.get("id", "")) for t in trades))) & 0xFFFFFFFF
    sims = simulate_portfolio(positions, corr, n_sims=n_sims, seed=seed)
    sims.sort()

    def pct(p: float) -> float:
        idx = max(0, min(len(sims) - 1, int(len(sims) * p)))
        return sims[idx]

    # Off-diagonal ortalama |ρ|
    off = [abs(corr[i][j]) for i in range(len(corr)) for j in range(len(corr)) if i != j]

    gross_cost = sum(p["cost"] for p in positions)
    gross_win  = sum(p["potential_win"] for p in positions)

    return {
        "n_positions":          len(positions),
        "n_sims":               n_sims,
        "expected_pnl":         round(statistics.fmean(sims), 2),
        "var_95":               round(pct(0.05), 2),    # %5 kuyruk — 20 günden 1 günü
        "var_99":               round(pct(0.01), 2),    # %1 kuyruk — 100 günden 1 günü
        "worst":                round(sims[0], 2),
        "best":                 round(sims[-1], 2),
        "stations":             stations,
        "avg_abs_correlation":  round(statistics.fmean(off) if off else 0.0, 3),
        "gross_exposure":       round(gross_cost, 2),
        "gross_potential_win":  round(gross_win, 2),
    }
