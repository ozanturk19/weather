#!/usr/bin/env python3
"""Kalibrasyon dashboard (Faz 6c) — Brier score + reliability + sharpness.

Kapalı paper_trades'ten öğrenerek modelin olasılık tahminlerinin gerçekle
ne kadar uyumlu olduğunu ölçer. "%35 şans" dediğimizde gerçekten %35'inde mi
oluyor?

Metrikler:
- Brier score: mean((p - outcome)²) — düşük iyi, mükemmel=0, rastgele≈0.25
- Brier reference: base_rate·(1-base_rate) — climatology karşılaştırması
- Skill score: 1 - Brier/Brier_ref — pozitif = climatology'den iyi
- Sharpness: pstdev(p) — ne kadar "güçlü" tahmin yapılıyor (yüksek = sharp)
- Reliability bins: p aralıkları için (mean_p, actual_freq, n)
- Base rate: gerçek kazanma frekansı (climatology)
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

# Varsayılan bin kenarları — 10 eşit aralık [0, 1]
DEFAULT_BINS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]


def _empty_result() -> dict:
    return {
        "n": 0, "brier": None, "brier_ref": None, "skill": None,
        "base_rate": None, "sharpness": None,
        "bins": [],
    }


def _filter_pairs(trades: list, days: int | None = None,
                  station: str | None = None) -> list[tuple[float, int]]:
    """Kapalı trade'lerden (p_predicted, outcome) çiftleri çıkar.

    ens_mode_pct → p (0..1 skalası). result: WIN=1, LOSS=0.
    """
    cutoff_str: str | None = None
    if days is not None:
        cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    pairs: list[tuple[float, int]] = []
    for t in trades:
        if t.get("status") != "closed":
            continue
        res = t.get("result")
        if res not in ("WIN", "LOSS"):
            continue
        if station and t.get("station") != station:
            continue
        if cutoff_str and (t.get("date") or "") < cutoff_str:
            continue
        p = t.get("ens_mode_pct")
        if p is None:
            continue
        pairs.append((float(p) / 100.0, 1 if res == "WIN" else 0))
    return pairs


def compute_calibration(
    trades: list,
    days: int | None = None,
    station: str | None = None,
    bin_edges: list[float] | None = None,
) -> dict:
    """Kapalı trade listesinden Brier + reliability + sharpness hesapla."""
    pairs = _filter_pairs(trades, days=days, station=station)
    n = len(pairs)
    if n == 0:
        return _empty_result()

    brier     = sum((p - y) ** 2 for p, y in pairs) / n
    base_rate = sum(y for _, y in pairs) / n
    brier_ref = base_rate * (1.0 - base_rate)
    skill     = 1.0 - (brier / brier_ref) if brier_ref > 1e-9 else 0.0
    sharpness = statistics.pstdev(p for p, _ in pairs) if n > 1 else 0.0

    edges = bin_edges or DEFAULT_BINS
    bins: list[dict] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        # Son bin kapsayıcı (hi dahil); diğerleri [lo, hi)
        in_bin = [(p, y) for p, y in pairs if lo <= p < hi]
        if not in_bin:
            continue
        mp = sum(p for p, _ in in_bin) / len(in_bin)
        af = sum(y for _, y in in_bin) / len(in_bin)
        bins.append({
            "bin_low":     round(lo, 3),
            "bin_high":    round(min(hi, 1.0), 3),
            "n":           len(in_bin),
            "mean_p":      round(mp, 4),
            "actual_freq": round(af, 4),
            "gap":         round(af - mp, 4),    # + = over-confident dip, - = over-confident peak
        })

    return {
        "n":          n,
        "brier":      round(brier,     4),
        "brier_ref":  round(brier_ref, 4),
        "skill":      round(skill,     4),
        "base_rate":  round(base_rate, 4),
        "sharpness":  round(sharpness, 4),
        "bins":       bins,
    }


def compute_per_station(
    trades: list,
    days: int | None = None,
    min_samples: int = 5,
) -> dict:
    """Her istasyon için ayrı kalibrasyon (min_samples altındakiler atlanır)."""
    stations = sorted({t.get("station") for t in trades if t.get("station")})
    out: dict = {}
    for s in stations:
        r = compute_calibration(trades, days=days, station=s)
        if r["n"] >= min_samples:
            out[s] = r
    return out
