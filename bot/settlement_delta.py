#!/usr/bin/env python3
"""
Settlement Delta Calibrator (Faz 7 → Faz 17) — WU ↔ Open-Meteo sistematik fark öğrenimi.

Problem: Polymarket marketleri Weather Underground (WU) verisiyle settle olur;
bot ise Open-Meteo arşivinden ölçüm alır (tahmin kaynağı ile uyumlu).
İki kaynak arasında istasyon × mevsim bazlı sistematik sapma var.

Örnek: LFPG için WU günlük max tipik +1.4°C Open-Meteo üstünde → top_pick 17°C
olsa bile gerçek settle 19°C'ye yakın → yanlış bucket seçimi.

Mevcut veri kaynağı: `settlement_audit` tablosu (Faz 6b) — her gün her kaynak
için günlük max kaydediyor. Bu modül delta = WU - Open-Meteo hesaplar, rolling
medyan olarak blend'e eklenir.

Faz 17: Mevsim-duyarlı prior kalibrasyonu
Urban heat island (UHI) etkisi mevsime göre değişir:
  Yaz (JJA): UHI maksimum — kent ısı adası en güçlü, model en çok geride kalır
  Kış (DJF): UHI minimum — soğuk hava sirkülasyonu farkı azaltır
  İlkbahar/Sonbahar: Geçiş sezonu

Bu tablo, STATION_DELTA_PRIORS'a eklenen mevsimsel bonus'u tanımlar.
KRİTİK: Mevsimsel bonus SADECE prior modu için geçerlidir.
Rolling observed delta (settlement_audit'ten gelen) kendi içinde mevsimi yansıtır,
dolayısıyla gözlem varsa bonus uygulanmaz.

Kullanım:
    from bot.settlement_delta import learn_station_delta, apply_delta

    delta = learn_station_delta("lfpg")     # rolling 30 gün median veya prior + mevsim bonusu
    adjusted_top_pick = apply_delta("lfpg", top_pick=17)  # → 18-19

Fallback (Faz A2): yeterli gözlem yoksa (<3 çift kaynak), STATION_DELTA_PRIORS + mevsim
bonusu kullanılır. WU doğrudan entegre edilene kadar muhafazakâr tutulur.
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

# ── Faz A2 / Faz 18: İstasyon prior'ları (cold-start kalibrasyonu) ─────────
# Kaynak: METAR − Open-Meteo audit analizi + Mayıs 2026 canlı hata gözlemi.
# Faz 18 güncellemeleri: RJTT/RKSI için gerçekçi kentsel gap prioryları;
# EFHK için tutarlı cold-bias düzeltmesi; EGLC/LEMD 0.0 kalır (ters/karma).
# Değerler %70 muhafazakâr — gerçek WU entegrasyonu ile üzerine yazılacak.
STATION_DELTA_PRIORS: dict[str, float] = {
    "eddm":  1.0,   # METAR-OM median +1.50°C (3 gün, tutarlı)
    "eham":  0.4,   # METAR-OM median +0.40°C (3 gün, tutarlı)
    "lfpg":  0.8,   # METAR-OM +1.40°C — muhafazakâr
    "limc":  0.8,   # METAR-OM +1.75°C — muhafazakâr
    "ltac":  0.5,   # METAR-OM median +1.20°C ama yüksek varyasyon → dikkatli
    "ltfm":  0.4,   # METAR-OM median +0.40°C (3 gün, tutarlı)
    "eglc":  0.0,   # METAR < OM (ters dinamik); WU belirsiz
    "efhk":  0.5,   # Faz 18: canlı gözlem Mayıs avg -0.77→-1.0°C cold bias
    "epwa":  0.0,   # nötr (median 0.0, 3 gün); sezonsal bonus yeterli
    "lemd":  0.0,   # negatif eğilim (-0.70), az veri
    "rjtt":  2.0,   # Faz 18: canlı gözlem avg -2.74°C (Haneda sahil vs kentsel WU)
    "rksi":  1.5,   # Faz 18: canlı gözlem avg -2.31°C (Incheon ada vs kentsel WU)
    "vhhh":  0.0,   # henüz yeterli audit yok
    "omdb":  0.0,   # çöl dinamiği farklı
}

# ── Faz 17: Mevsim-duyarlı prior bonusu ───────────────────────────────────────
# Yapı: {station: {"spring": float, "summer": float, "autumn": float, "winter": float}}
# Bonus = STATION_DELTA_PRIORS üstüne eklenen mevsimsel ek (°C).
# SADECE prior modu için — rolling gözlem mevcut olduğunda bu tablo kullanılmaz.
#
# Kaynaklar:
#   • Avrupa istasyonları: Urban climatology + METAR-OM gap mevsimsel analizi
#   • Asya istasyonları: JMA/KMA yaz-kış UHI literatürü (muhafazakâr)
# Değerler kilibrasyonsuz — gerçek WU entegrasyonu ile üzerine yazılacak.
#
# Hesap mantığı (LFPG örneği):
#   Yaz UHI gap ≈ 2.0°C (literatür) × 70% muhafazakârlık = 1.4°C; bonus = 1.4 - 0.8 = 0.6
#   Kış UHI gap ≈ 1.0°C × 70% = 0.7°C; bonus = 0.7 - 0.8 < 0 → 0.0 (prior yeterli)
#
# İstasyonlar bonus almayan: EGLC (METAR < OM), EFHK (karma sign), LEMD (negatif eğilim)
STATION_SEASONAL_BONUS: dict[str, dict[str, float]] = {
    # ── Güçlü UHI (büyük Avrupa şehirleri, havalimanı uzak) ──────────────────
    "lfpg": {"spring": 0.30, "summer": 0.50, "autumn": 0.15, "winter": 0.0},
    # Paris CDG havalimanı şehir merkezinden 25km uzak. Yaz UHI şiddetli.
    # Mevcut prior 0.8°C → yaz toplam ≈ 1.3°C, ilkbahar ≈ 1.1°C

    "eddm": {"spring": 0.40, "summer": 0.65, "autumn": 0.20, "winter": 0.0},
    # Münih havalimanı şehir merkezi + Föhn etkisi birlikte. En yüksek bonus.
    # Mevcut prior 1.0°C → yaz toplam ≈ 1.65°C

    "limc": {"spring": 0.30, "summer": 0.50, "autumn": 0.15, "winter": 0.0},
    # Po Ovası — Milano Malpensa şehirden 40km uzak. Yaz sıcak ve nemli.
    # Mevcut prior 0.8°C → yaz toplam ≈ 1.3°C

    # ── Orta UHI ─────────────────────────────────────────────────────────────
    "eham": {"spring": 0.20, "summer": 0.30, "autumn": 0.10, "winter": 0.0},
    # Amsterdam Schiphol şehre 9km. Kıyı etkisi UHI'yı bastırır.
    # AMSTERDAM VAKASI: Mayıs prior 0.4 → 0.6°C, Haziran → 0.7°C
    # Mevcut prior 0.4°C → yaz toplam ≈ 0.7°C

    "ltac": {"spring": 0.55, "summer": 0.35, "autumn": 0.15, "winter": 0.0},
    # Faz 18: spring 0.20→0.55 (canlı gözlem Mayıs avg -1.27°C; prior 0.5+0.55=1.05)
    # Ankara kuru/kıta iklimi, Mayıs sıcak geçiş. Yaz nispeten sabit.

    "ltfm": {"spring": 0.35, "summer": 0.25, "autumn": 0.10, "winter": 0.0},
    # Faz 18: spring 0.15→0.35 (canlı gözlem Mayıs avg -0.73°C; prior 0.4+0.35=0.75)
    # İstanbul havalimanı şehirden 35km uzak. İlkbahar ısınması belirgin.

    # ── Küçük UHI / belirsiz (sıfır prior üstüne küçük sezonsal) ────────────
    "epwa": {"spring": 0.15, "summer": 0.25, "autumn": 0.10, "winter": 0.0},
    # Varşova Chopin havalimanı şehre 10km. Kıta iklimi yaz UHI'sı mevcut.
    # prior 0.0 → yaz toplam ≈ 0.25°C

    # ── Helsinki (Faz 18: prior eklendi, sezonsal bonus eklendi) ─────────────
    "efhk": {"spring": 0.20, "summer": 0.30, "autumn": 0.10, "winter": 0.0},
    # Faz 18: canlı gözlem Mayıs -0.77→-1.0°C trend kötüleşiyor.
    # prior 0.5 + spring 0.20 = 0.70°C. EFHK trade-dışı ama oracle_pct için önemli.

    # ── Asya istasyonları ─────────────────────────────────────────────────────
    "rjtt": {"spring": 0.25, "summer": 0.55, "autumn": 0.25, "winter": 0.10},
    # Faz 18: prior 0.0→2.0 (canlı gözlem avg -2.74°C, Tokyo Haneda sahil vs kentsel WU).
    # prior 2.0 + spring 0.25 = 2.25°C, summer = 2.55°C.

    "rksi": {"spring": 0.15, "summer": 0.35, "autumn": 0.15, "winter": 0.0},
    # Faz 18: prior 0.0→1.5 (canlı gözlem avg -2.31°C, Incheon ada vs kentsel WU).
    # prior 1.5 + spring 0.15 = 1.65°C, summer = 1.85°C.

    "vhhh": {"spring": 0.15, "summer": 0.30, "autumn": 0.20, "winter": 0.10},
    # Hong Kong nemli tropik → yıl boyu UHI mevcut. prior 0.0 → yaz toplam ≈ 0.30°C

    "omdb": {"spring": 0.15, "summer": 0.25, "autumn": 0.15, "winter": 0.0},
    # Dubai çöl dinamiği farklı. prior 0.0 → yaz toplam ≈ 0.25°C (muhafazakâr)

    # EGLC, LEMD: mevsimsel bonus YOK.
    # EGLC: METAR < OM (ters dinamik). LEMD: negatif eğilim.
    # EFHK artık bonus alıyor (yukarıda).
}

# Mevsim helper
def _get_season(month: int) -> str:
    """Ay numarasından mevsim ismi (kuzey yarımküre)."""
    if month in (3, 4, 5):  return "spring"
    if month in (6, 7, 8):  return "summer"
    if month in (9, 10, 11): return "autumn"
    return "winter"  # 12, 1, 2


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
    """Tek istasyon için delta (mevsim-duyarlı).

    Öncelik sırası:
      1. Yeterli gözlem (n >= MIN_PAIRED_SAMPLES): rolling medyan kullan
         → Gözlem kendi içinde mevsimsel etkiyi yansıtır; ek bonus YOK.
      2. Gözlem yok / threshold altı: prior + mevsimsel bonus (Faz 17)
         → STATION_DELTA_PRIORS[station] + STATION_SEASONAL_BONUS[station][season]
      3. İstasyon tanımlı değilse: 0.0

    Faz 8/A1: horizon_days verilmişse HORIZON_DELTA_DAMPENING çarpanı uygulanır.
    """
    deltas = compute_station_deltas(days=days, db_path=db_path)
    info = deltas.get(station)

    if info is not None:
        # ── Mod 1: Gözlem var → rolling medyan (mevsimsel etki zaten içinde) ──
        raw = float(info["delta"])
    else:
        # ── Mod 2: Prior + mevsim bonusu ──────────────────────────────────────
        base_prior = STATION_DELTA_PRIORS.get(station, 0.0)
        season     = _get_season(datetime.now().month)
        s_bonus    = STATION_SEASONAL_BONUS.get(station, {}).get(season, 0.0)
        raw        = round(base_prior + s_bonus, 2)

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
    """Tüm istasyonlar için delta özeti (dashboard için).

    Prior modu: mevsim-duyarlı değer (base_prior + seasonal_bonus) gösterilir.
    Gözlem modu: rolling medyan (mevsimsel etki içinde).
    """
    deltas = compute_station_deltas(days=days, db_path=db_path)
    # Tüm bilinen istasyonları kapsa (hem prior hem gözlem olabilenler)
    all_stations = set(deltas.keys()) | set(STATION_DELTA_PRIORS.keys()) | set(STATION_SEASONAL_BONUS.keys())
    season = _get_season(datetime.now().month)
    result = []
    for s in sorted(all_stations):
        if s in deltas:
            result.append({"station": s, **deltas[s], "source": "observed"})
        else:
            base  = STATION_DELTA_PRIORS.get(s, 0.0)
            bonus = STATION_SEASONAL_BONUS.get(s, {}).get(season, 0.0)
            total = round(base + bonus, 2)
            if total != 0.0:
                result.append({
                    "station":   s,
                    "delta":     total,
                    "delta_base": base,
                    "season_bonus": bonus,
                    "season":    season,
                    "n":         0,
                    "source_pair": "prior",
                    "source":    "prior",
                })
    return result
