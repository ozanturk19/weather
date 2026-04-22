#!/usr/bin/env python3
"""
Sinyal Kalitesi Skoru — 0 (kötü) ile 100 (mükemmel) arası kompozit.

Amaç: scanner'ın "gir/girme" gate'lerini tek tek geçtikten sonra, işleme
girecek sinyalin *ne kadar güçlü* olduğunu tek sayıda özetlemek. Log ve
trade kaydına yazılır; sonradan P&L ile korelasyon analizinde kullanılır.

Bileşenler (100 üstü):
  30  mode_pct  — ensemble konsensüsü (30% → +0 skor, 60% → +30)
  20  CI stability — bootstrap CI ne kadar dar (geniş CI → puan düşer)
  25  edge — ensemble olasılık - market fiyat (5pp → +5, 33pp → +25)
  15  calibrated_uncertainty — "Düşük" → +15, "Orta" → +7, "Yüksek" → 0
  10  shape_quality — bimodal değil + yeterli üye → +10

Skor ≥ 70 → güçlü sinyal
Skor 50–70 → orta sinyal (gir ama dikkat)
Skor < 50 → zayıf (genelde filtreleri geçemez ama 2-bucket ikincide olur)
"""
from __future__ import annotations


def compute_signal_score(
    mode_pct: float | None,
    mode_ci_low: float | None,
    mode_ci_high: float | None,
    edge: float | None,                # 0–1 aralığında (örn. 0.08 = 8pp)
    uncertainty: str | None,           # "Düşük" / "Orta" / "Yüksek" / "?"
    is_bimodal: bool = False,
    n_members: int = 0,
) -> dict:
    """Sinyal kalitesi skoru (0-100) + bileşen ayrıştırması.

    Her bileşen None/bilinmeyen durumda nötr (0) puanlanır — eksik veri
    "kötü" sinyal sayılmaz, sadece skorun üst sınırı düşer.

    Döner: {score, components: {...}, grade: "güçlü"|"orta"|"zayıf"}
    """
    components = {}

    # 1) mode_pct: linear 30→60 ölçekli 0→30 puan, 60 üstü cap
    if mode_pct is None:
        components["mode"] = 0
    else:
        # 30'un altı zaten gate'te reddedildi; burada sadece derecelendirme
        pct = max(30, min(80, mode_pct))
        components["mode"] = round(30 * (pct - 30) / 50)   # 30→0, 80→30

    # 2) CI stability: bootstrap aralığı (high-low) ne kadar dar
    if mode_ci_low is None or mode_ci_high is None:
        components["ci"] = 10    # nötr (bilgi yok → orta)
    else:
        width = max(0, mode_ci_high - mode_ci_low)
        # 5pp CI → +20 (sağlam), 30pp → 0 (çok dağınık)
        components["ci"] = round(max(0, min(20, 20 * (30 - width) / 25)))

    # 3) edge: 5pp → +5, 33pp → +25 (lineer)
    if edge is None:
        components["edge"] = 0
    else:
        pp = max(0, edge * 100)
        components["edge"] = round(min(25, 25 * pp / 33))

    # 4) calibrated uncertainty
    u = (uncertainty or "").strip().lower()
    if u.startswith("düş") or u == "low":
        components["uncertainty"] = 15
    elif u == "orta" or u == "medium":
        components["uncertainty"] = 7
    elif u.startswith("yüks") or u == "high":
        components["uncertainty"] = 0
    else:
        components["uncertainty"] = 7   # "?" veya bilinmeyen → nötr

    # 5) shape quality (bimodal değil + yeterli üye)
    shape_score = 0
    if not is_bimodal:
        shape_score += 7
    if n_members >= 80:
        shape_score += 3
    elif n_members >= 40:
        shape_score += 2
    components["shape"] = shape_score

    score = sum(components.values())
    score = max(0, min(100, score))

    if score >= 70:
        grade = "güçlü"
    elif score >= 50:
        grade = "orta"
    else:
        grade = "zayıf"

    return {
        "score":      score,
        "components": components,
        "grade":      grade,
    }
