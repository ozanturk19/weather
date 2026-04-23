#!/usr/bin/env python3
"""Signal-score → dynamic position sizing (Faz 7).

Basit, güvenli, tiered yaklaşım. Kelly benzeri modeller daha büyük örnek
boyutu gerektirir (>200 kapalı trade); Faz 7'de kademeli bir tier mapping
yeterli — sinyal güçlendikçe büyük, zayıfladıkça küçük.

Tavan: ana SHARES'in 1.5 katı (risk bandı ±%50 içinde kalsın).

Tier tanımları (bot/signal_score.py ile hizalı):
    ≥85  Premium   → 1.5x  (nadir, çok temiz tahmin)
    70-84 Strong   → 1.2x  (iyi, yüksek konsensüs)
    55-69 Moderate → 1.0x  (baseline)
    <55   Weak     → 0     (gate tarafından zaten engelleniyor)

None skor (hesap başarısız) → 1.0x (nötr).
"""
from __future__ import annotations

# Tavanlar — tek nokta konfigürasyonu
MAX_MULTIPLIER = 1.5
MIN_MULTIPLIER = 0.5


def size_multiplier(signal_score: int | None) -> float:
    """Signal skoruna göre SHARES çarpanı (0.5–1.5 arası).

    Hesap başarısızsa (None) nötr 1.0 döner — sinyal eksikliği cezalandırılmaz.
    Skor 0 veya negatif gelirse güvenli 0.5'a düşürülür.
    """
    if signal_score is None:
        return 1.0
    try:
        s = int(signal_score)
    except Exception:
        return 1.0
    if s >= 85:
        return 1.5
    if s >= 70:
        return 1.2
    if s >= 55:
        return 1.0
    # <55 zaten gate'te bloke; yine de güvenli taban
    return max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, 0.5))


def compute_shares(base_shares: int, signal_score: int | None) -> int:
    """Baz SHARES'i skor çarpanıyla ölçeklendirir. En az 1 hisse garantili."""
    mult = size_multiplier(signal_score)
    scaled = int(round(base_shares * mult))
    return max(1, scaled)
