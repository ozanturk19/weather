#!/usr/bin/env python3
"""
Weather Polymarket Paper Trading Bot
Gerçek para olmadan alım-satım simülasyonu.

Kullanım:
  python scanner.py scan      # Fırsat tara, yeni trade aç
  python scanner.py settle    # Dünkü pozisyonları kapat
  python scanner.py report    # Tüm geçmişi göster
  python scanner.py status    # Açık pozisyonlar
"""
from __future__ import annotations

import httpx
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# ── Ayarlar ────────────────────────────────────────────────────────────────
WEATHER_API   = "http://localhost:8001"
TRADES_FILE   = Path(__file__).parent / "paper_trades.json"

STATIONS = ["eglc", "ltac", "limc", "ltfm", "lemd", "lfpg",
            "eham", "eddm", "epwa", "efhk", "omdb", "rjtt",
            "rksi", "vhhh"]   # Asya Faz 11 (whitelist dışı — backtest tamamlanana dek)

STATION_LABELS = {
    "eglc": "Londra   ", "lfpg": "Paris    ", "limc": "Milano   ",
    "lemd": "Madrid   ", "ltfm": "İstanbul ", "ltac": "Ankara   ",
    "eham": "Amsterdam", "eddm": "Münih    ", "epwa": "Varşova  ",
    "efhk": "Helsinki ", "omdb": "Dubai    ", "rjtt": "Tokyo    ",
    "rksi": "Seoul    ", "vhhh": "HongKong ",
}

# İstasyon koordinatları (Open-Meteo settlement için)
# WU (Weather Underground) veri kaynağı Polymarket'ın kullandığı kaynak
STATION_COORDS: dict = {
    "eglc": (51.505,   0.055),
    "lfpg": (49.009,   2.548),
    "limc": (45.630,   8.723),
    "lemd": (40.472,  -3.562),
    "ltfm": (41.261,  28.742),
    "ltac": (40.128,  32.995),
    "eham": (52.309,   4.764),
    "eddm": (48.364,  11.786),
    "epwa": (52.166,  20.967),
    "efhk": (60.317,  24.963),
    "omdb": (25.253,   55.364),
    "rjtt": (35.5494, 139.7798),  # Haneda — hassas koordinat (sahil kıyısı)
    "rksi": (37.4602, 126.4407),  # Incheon — şehir merkezinden ~2°C soğuk
    "vhhh": (22.3080, 113.9185),  # Chek Lap Kok (Lantau) — Kowloon'dan 1–3°C soğuk
}

# Risk parametreleri
SHARES       = 10     # her işlemde alınan share adedi (1 share kazanınca $1 öder)
MIN_PRICE    = 0.05   # çok ucuz → şüpheli likidite, atla
MAX_PRICE    = 0.40   # 40¢ üzeri pozisyonlarda edge zayıflıyor → pas
                      # (eski 0.50'den düşürüldü — canlı öncesi edge kalitesi için)

# Kalite filtreleri
SKIP_UNCERTAINTY = {"yüksek", "high", "very high"}  # bu seviyelerde hiç pozisyon açma
MIN_BIAS_TRADES  = 5   # bias hesabı için minimum kapalı trade (8'den düşürüldü — Faz A)
                       # 4'te Paris +3°C vakası → 8'e çıkardık; şimdi 5-7 gerçek gözlem var
                       # Settlement delta (Faz A2) WU-OM offset'ini ayrıca ele aldığı için
                       # Kalman bias daha az kritik → 5 güvenli eşik
MAX_BIAS_CORRECTION = 2  # bias tavanı: en fazla ±2°C düzeltme uygulanır

# Ensemble kalite filtreleri
MIN_MODE_PCT = 30    # ensemble üyelerinin en az %30'u aynı bucket'ta hemfikir olmalı
                     # ICON 40 üye → min 12 üye aynı bucket'ta demek
MIN_EDGE     = 0.05  # ensemble olasılığı (mode_pct) - market fiyatı en az 5 puan fazla olmalı
                     # Örn: %35 ensemble → 28¢ market → edge +7% → gir ✓
                     #      %30 ensemble → 28¢ market → edge +2% → geçersiz ✗

# Faz 2: bootstrap CI tabanlı kırılganlık eşiği
# Bootstrap CI alt sınırı MIN_MODE_PCT - 10'un altındaysa consensus aslında
# hassas (üye değişince dağılabilir) — pas geç.
MIN_MODE_CI_LOW = 20  # bootstrap %5 percentile en az bu olmalı (MIN_MODE_PCT=30 iken)

# Faz 2: bimodal dağılım uyarısı (sadece log, ret değil — 2-bucket halleder)
BIMODAL_MAX_SEPARATION = 1   # üstü = tepeler uzak, tek bucket stratejisi riskli

# İstasyon bazlı fiyat tavanı (sistematik sorunlu istasyonlar)
STATION_MAX_PRICE: dict[str, float] = {
    "lfpg": 0.18,   # Paris: %10 win rate, settlement kaynak uyumsuzluğu (+1.9°C artık hata)
                    # 18¢ altında EV hâlâ pozitif olabilir, üstü riskli
}

# ── Faz 5: Kalibrasyon-odaklı filtreler (2026-04 tanısı) ──────────────────
# 90 günlük veride (n=131) genel skill = -0.36 (climatology'den kötü).
# Tanı: mode_pct ∈ [50,70) bandı 51/131 trade'i oluşturuyor ama gerçek win
# oranı %20 (beklenen ~%58). Düşük-güven bandı [30,50) kalibre.
# Kaynaklar: /api/calibration (per_station + bins); signal_score ("zayıf").

# Mid-range over-confidence: bu bantta ensemble olasılıkları sistematik
# olarak şişiyor. Kalibrasyon (Platt/isotonic) eklenene kadar skip.
MID_RANGE_SKIP_LOW  = 50   # mode_pct bu %'den itibaren skip
MID_RANGE_SKIP_HIGH = 80   # bu %'ye kadar skip (80+ çok az örnek, belirsiz)

# İstasyon-bazlı skill pause — live ROI analizi sonrası güncellendi (2026-04-24):
# lfpg live: 1W/2L +64% ROI → unpause (eski backtest single-bucket bazlıydı)
# ltac live: 2W/5L +15% ROI → unpause (pozitif ROI, kullanıcı onayı)
# limc: 0W/4L -100% ROI → whitelist dışı zaten; altyapı temizlendi
# Set boş — circuit breaker SQLite override (should_pause_station) hâlâ aktif.
STATION_SKILL_PAUSE: frozenset = frozenset()

# Sinyal skoru alt sınırı — "orta" derecenin alt kenarı (signal_score.py)
# "Zayıf" sinyalleri (<50) gate'te düşür.
MIN_SIGNAL_SCORE = 55

# ── Faz 8: Micro-hedge (off-by-one koruması) ──────────────────────────────
# 23 Nisan'da 7 filled pozisyonun 5'i off-by-one miss oldu (pick ±1°C uzak).
# Ana pick yüksek güvenli olsa bile blend ile top_pick arasındaki drift
# ensemble'ın kenara düştüğünü ima ediyor olabilir. Moderate sinyalde
# (≥55) ±1°C komşuya ana boyutun %40'ı kadar hedge koy (ucuz sigorta).
MICRO_HEDGE_SIZE_RATIO  = 0.40
MICRO_HEDGE_MIN_SIGNAL  = 55

# ── Faz 9: Strateji geliştirmeleri (backtest 37 trade analizi) ────────────
#
# İstasyon whitelist: pozitif ROI'li istasyonlar (backtest bazlı).
# LEMD/LIMC/EFHK/RJTT → 0% win rate, blacklist edildi.
# LTFM → negatif ROI, whitelist dışı.
# RKSI → yapısal -2.3°C soğuk bias (Incheon adası grid sorunu), skip kalıcı.
# RJTT → backtest %37.7 wr (ilkbahar), yaz verisi beklenecek (Haziran-Ağustos).
# VHHH → Faz 11 backtest: 47.5% wr, MAE 0.59°C, EV +19.5% — whitelist'e alındı.
STATION_WHITELIST: frozenset = frozenset({
    "eglc",  # London        — live:  4W/0L  %100  roi+241%
    "eham",  # Amsterdam     — live:  2W/2L  %50   roi+115%
    "epwa",  # Warsaw        — live:  1W/1L  %50   roi+41%
    "eddm",  # Munich        — live:  2W/3L  %40   roi+45%
    "ltac",  # Ankara        — live:  2W/4L  %33   roi+32%  (skill pause aktif)
    "lfpg",  # Paris         — live:  1W/2L  %33   roi+64%  (skill pause aktif)
    "vhhh",  # HK Intl       — bt61d: 47.5%  MAE=0.59°C  EV=+19.5%  (Faz 11)
})

# Ana bucket için minimum fiyat (backtest: <15¢ → %0 win rate, %−100 ROI).
# Micro-hedge / komşu bucket'lar için MIN_PRICE (5¢) hâlâ geçerli.
MIN_MAIN_PRICE = 0.25

# D+1 minimum sinyal skoru: D+1 backtest %14 win rate vs D+2 %47.
# Market D+1'i çok verimli fiyatlıyor; sadece çok yüksek sinyale gir.
D1_MIN_SIGNAL_SCORE = 88

# Günlük maksimum pozisyon sayısı (station bazlı, D+1 ve D+2 ayrı ayrı).
# Her tarih için en iyi signal_score'lu N istasyon seçilir.
MAX_DAILY_POSITIONS_PER_DATE = 3

# Multi-bucket bütçe tavanı (limit fiyatları toplamı):
#   2-bucket: main(≤35¢) + adj1(≤10¢) ≤ 45¢
#   3-bucket: main(≤35¢) + adj1(≤20¢) + adj2(≤10¢) ≤ 65¢
# Simülasyon: 3-bucket → win rate %32→%75, ROI %23→%64 (37 trade backtest)
TWO_BUCKET_BUDGET   = 0.45
THREE_BUCKET_BUDGET = 0.65
MULTI_BUCKET_N      = 3      # 3 = 3-bucket aktif, 2 = eski 2-bucket, 1 = single

# Faz 11: Komşu bucket edge koruması (2026-04-29 LEMD dersi)
# Ana pick'i geçen trade için komşu bucket da eklenmeden önce:
#   edge = (ens_pct / 100) - yes_price
# Bu değer ADJ_MAX_NEG_EDGE'den küçükse market modelden çok uzak → pas.
# Örn: 0% ens, 15¢ market → edge=-15% < -8% → skip (piyasa çok yüksek)
#       3% ens, 8¢ market  → edge=-5%  > -8% → pass (ucuz sigorta)
ADJ_MAX_NEG_EDGE    = -0.08  # komşu bucket: piyasa ens'ten max 8 puan fazla

# Faz 10: Isınma/soğuma trend tiebreaker (settlement_delta yönü)
# Sistematik sıcaklık sapması ≥ TREND_BIAS_THRESHOLD ise, adj bucket sıralamasında
# trend yönündeki bucket'a sanal TREND_PRICE_BONUS eklenir.
# → Market fiyatı primary; sadece yakın fiyatlarda (< ~3¢ fark) etkili.
# Örn: ısınma trendi (+1.2°C) + 19°C@20¢ vs 17°C@21¢ → 20+3=23 > 21 → 19°C öne çıkar.
TREND_BIAS_THRESHOLD = 0.30   # °C: bu altındaki sdelta nötr (bias uygulanmaz)
TREND_PRICE_BONUS    = 0.03   # sanal bonus (3¢ ≈ 1 kademe fiyat farkına eşdeğer)


def should_pause_station(station: str) -> bool:
    """İstasyon-bazlı skill pause aktif mi? (negatif skill korunağı).

    Faz 7: SQLite station_status tablosunu öncelikli kontrol eder. Tabloya
    runtime'da yazılabilir (override), eksikse statik STATION_SKILL_PAUSE set
    fallback olarak kullanılır.
    Faz 8: auto_resume_at < now() ise otomatik unpause — circuit breaker
    kısa dönem durdurmalarını süre dolunca kaldırır.
    """
    try:
        import time as _time
        from bot.db import DB_PATH, get_db
        with get_db(DB_PATH, readonly=True) as conn:
            row = conn.execute(
                "SELECT paused, auto_resume_at FROM station_status WHERE station = ?",
                (station,),
            ).fetchone()
            if row is not None:
                paused = bool(row[0])
                auto_resume = row[1]
                if paused and auto_resume is not None and int(auto_resume) <= int(_time.time()):
                    return False  # süre doldu → otomatik unpause (read-only; yazım
                                  # circuit_breaker.enforce_circuit_breakers tarafında)
                return paused
    except Exception:
        pass
    return station in STATION_SKILL_PAUSE


def is_mid_range_mode(mode_pct) -> bool:
    """mode_pct kalibrasyonu kırık mid-range bandında mı?"""
    if mode_pct is None:
        return False
    return MID_RANGE_SKIP_LOW <= mode_pct < MID_RANGE_SKIP_HIGH


def is_weak_signal(signal_score) -> bool:
    """Signal skoru "zayıf" derecesine mi düşüyor? None ise False (neutral)."""
    if signal_score is None:
        return False
    return signal_score < MIN_SIGNAL_SCORE

# ── Trade Depolama ──────────────────────────────────────────────────────────
def load_trades() -> list:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception) as e:
            print(f"  ⚠️  paper_trades.json okunamadı: {e}")
            return []
    return []

def save_trades(trades: list):
    """SQLite-first yazım (Faz 7): önce DB, sonra JSON yedek."""
    # 1. SQLite birincil yazım
    try:
        from bot.db import write_paper_trades_list
        write_paper_trades_list(trades)
    except Exception as e:
        print(f"  ⚠️  SQLite paper yazım hatası (JSON'a devam): {e}")
    # 2. JSON yedek (insan-okunur + eski araç uyumluluğu)
    TRADES_FILE.write_text(
        json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8"
    )

# ── Adaptif Bias Hesabı ─────────────────────────────────────────────────────
def compute_station_biases(trades: list) -> dict:
    """Kapalı trade'lerden istasyon bazlı sistematik tahmin hatasını öğren.

    Birincil: Kalman filter (Faz 3) — son gözlemlere daha fazla ağırlık,
              zaman aralığına duyarlı (mevsim kaymasına adapte).
    Yedek: basit ortalama (Kalman modülü yüklenemezse).

    Yeterli veri (MIN_BIAS_TRADES) yoksa o istasyon için 0 döner.
    Örnek: EPWA için geçmiş 8 trade'de ortalama +1.4°C hata → bias = +1
    """
    # Önce Kalman dene (Faz 3)
    try:
        from bot.kalman import kalman_station_biases
        return kalman_station_biases(
            trades,
            max_correction=MAX_BIAS_CORRECTION,
            min_trades=MIN_BIAS_TRADES,
        )
    except Exception:
        pass

    # Yedek: basit ortalama (eski davranış — hiçbir şey bozmasın)
    from collections import defaultdict
    errors: dict = defaultdict(list)

    for t in trades:
        if t["status"] == "closed" and t.get("actual_temp") is not None and t.get("top_pick") is not None:
            delta = t["actual_temp"] - t["top_pick"]
            errors[t["station"]].append(delta)

    biases: dict = {}
    for station, deltas in errors.items():
        if len(deltas) >= MIN_BIAS_TRADES:
            avg  = sum(deltas) / len(deltas)
            bias = math.floor(avg + 0.5)
            bias = max(-MAX_BIAS_CORRECTION, min(MAX_BIAS_CORRECTION, bias))
            if bias != 0:
                biases[station] = bias

    return biases

# ── Sinyal Skoru Yardımcısı (Faz 3) ─────────────────────────────────────────
def _score_for_second_bucket(
    mode_pct, mode_ci_low, mode_ci_high, edge, unc, is_bimodal, n_members
) -> dict:
    """İkinci bucket için {signal_score, signal_grade} döner. Sessiz fallback."""
    try:
        from bot.signal_score import compute_signal_score
        sig = compute_signal_score(
            mode_pct=mode_pct, mode_ci_low=mode_ci_low,
            mode_ci_high=mode_ci_high, edge=edge,
            uncertainty=unc, is_bimodal=is_bimodal, n_members=n_members,
        )
        return {"signal_score": sig["score"], "signal_grade": sig["grade"]}
    except Exception:
        return {"signal_score": None, "signal_grade": None}


# ── Bucket Eşleştirme ───────────────────────────────────────────────────────
def find_top_pick_bucket(buckets: list, top_pick: int) -> dict | None:
    """round(blend) için doğru PM bucket'ını bul."""
    for b in buckets:
        t = b.get("threshold")
        if t is None:
            continue
        if b.get("is_below") and top_pick <= t:
            return b
        if b.get("is_above") and top_pick >= t:
            return b
        if not b.get("is_below") and not b.get("is_above") and round(t) == top_pick:
            return b
    return None

def bucket_won(title: str, actual: float) -> bool | None:
    """Verilen bucket başlığına göre actual sıcaklık kazandı mı?
    Desteklenen formatlar:
      "19°C"              → exact match (round(actual) == 19)
      "25°C or higher"    → actual >= 25
      "5°C or below"      → actual <= 5
      "18°C to 20°C"      → 18 <= actual <= 20  (range bucket)
    """
    t = title.strip()
    higher_m = re.search(r'(-?\d+).*or higher', t, re.I)
    below_m  = re.search(r'(-?\d+).*or below',  t, re.I)
    range_m  = re.search(r'(-?\d+)\D+(-?\d+)', t)   # "X to Y", "X-Y", "14°C to 16°C"
    exact_m  = re.match(r'^(-?\d+)\s*°?C?$', t)

    if higher_m: return actual >= int(higher_m.group(1))
    if below_m:  return actual <= int(below_m.group(1))
    if exact_m:  return round(actual) == int(exact_m.group(1))
    if range_m:  return int(range_m.group(1)) <= actual <= int(range_m.group(2))
    return None

# ── Open-Meteo Gerçek Sıcaklık (Settlement İçin Birincil Kaynak) ───────────
def get_actual_temp_open_meteo(station: str, date: str):
    """Open-Meteo arşivinden günlük maks. sıcaklık çek (°C olarak).

    Polymarket, WU (Weather Underground) verisiyle settle eder.
    Open-Meteo temperature_2m_max → WU daily max ile uyumlu.
    METAR saatlik ölçümlerinin ortalamasından çok daha doğru.

    Döner: float (°C) veya None (veri yoksa / hata).
    """
    coords = STATION_COORDS.get(station)
    if not coords:
        return None
    lat, lon = coords
    try:
        url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={date}&end_date={date}"
            f"&daily=temperature_2m_max&timezone=auto"
        )
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        temps = r.json().get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception as e:
        print(f"  ⚠️  Open-Meteo hatası ({station}, {date}): {e}")
    return None

# ── Scan: Tek Tarih İçin İstasyon Tara ─────────────────────────────────────
def scan_date(station: str, target_date: str, trades: list,
              station_biases: dict | None = None,
              live_mode: bool = False, trader=None,
              horizon: int = 2) -> dict | None:
    """Bir istasyon + tarih için sinyal ara. Yeni trade dict döner veya None.

    station_biases: compute_station_biases() çıktısı — adaptif tahmin düzeltmesi.
    live_mode/trader: aktarılırsa, paper var ama live order eksikse retry_live tuple döner.
    horizon: 1=D+1, 2=D+2 — D+1 için daha yüksek signal_score eşiği uygulanır.
    """
    label = STATION_LABELS.get(station, station.upper())

    # ── Faz 9: İstasyon whitelist (negatif ROI istasyonları erken elek) ────
    if station not in STATION_WHITELIST:
        return None  # sessiz — whitelist dışı istasyonlar log'a bile düşmez

    # ── Faz 5: istasyon-bazlı skill pause (ağ çağrısından önce erken çık) ──
    if should_pause_station(station):
        print(
            f"  ⛔ {station.upper()} {label}  — istasyon skill pause aktif "
            f"(90g skill < -0.7), pas"
        )
        return None

    try:
        # 1. Hava tahmini çek
        r = httpx.get(f"{WEATHER_API}/api/weather?station={station}", timeout=30)
        r.raise_for_status()
        days      = r.json().get("days", {})
        day_data  = days.get(target_date, {})
        blend_obj = day_data.get("blend", {})

        # Bias düzeltmeli blend kullan
        if blend_obj.get("bias_active") and blend_obj.get("bias_corrected_blend"):
            blend = blend_obj["bias_corrected_blend"]
        else:
            blend = blend_obj.get("max_temp")

        if blend is None:
            print(f"  ⬜ {station.upper()} {label}  — tahmin yok")
            return None

        spread = blend_obj.get("spread", 0) or 0
        unc    = blend_obj.get("uncertainty", "?")

        # top_pick: ensemble member maxlarının modu, yoksa round(blend)
        ens_day: dict = {}
        try:
            r_ens   = httpx.get(f"{WEATHER_API}/api/ensemble?station={station}", timeout=30)
            ens_day = r_ens.json().get("days", {}).get(target_date, {})
            members = ens_day.get("member_maxes", [])
        except Exception:
            members = []

        if members:
            counts      = Counter(round(m) for m in members)
            top_pick    = counts.most_common(1)[0][0]
            mode_pct    = round(counts[top_pick] / len(members) * 100)
            top2        = counts.most_common(2)
            second_pick = top2[1][0] if len(top2) > 1 else None
            second_pct  = round(top2[1][1] / len(members) * 100) if second_pick is not None else None
        else:
            top_pick    = round(blend)
            mode_pct    = None
            second_pick = None
            second_pct  = None

        # Faz 2: ensemble şekil metrikleri (API varsa)
        is_bimodal    = bool(ens_day.get("is_bimodal"))
        peak_sep      = ens_day.get("peak_separation")
        mode_ci_low   = ens_day.get("mode_ci_low")
        mode_ci_high  = ens_day.get("mode_ci_high")

        # ── Ensemble varlık kontrolü ────────────────────────────────────────
        # Ensemble verisi olmadan top_pick güvenilirliği düşük → pas
        if not members:
            print(f"  ⛔ {station.upper()} {label}  — ensemble verisi alınamadı, pas")
            return None

        # ── Ensemble konsensüs filtresi ─────────────────────────────────────
        # Düşük mode_pct = ensemble dağınık, bucket seçimi güvenilmez
        if mode_pct is not None and mode_pct < MIN_MODE_PCT:
            print(
                f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C"
                f" — konsensüs zayıf (%{mode_pct} < %{MIN_MODE_PCT}), pas"
            )
            return None

        # ── Faz 5: mid-range over-confidence skip ───────────────────────────
        # [50,70) bandı canlı veride gap ≈ -0.39 (beklenen %58, gerçek %20).
        # Kalibrasyon eklenene dek bu bandı komple atla.
        if is_mid_range_mode(mode_pct):
            print(
                f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C"
                f" — mid-range (%{mode_pct} ∈ [{MID_RANGE_SKIP_LOW},"
                f"{MID_RANGE_SKIP_HIGH})) kalibrasyon kırık, pas"
            )
            return None

        # ── Faz 2: bootstrap CI kırılganlık filtresi ────────────────────────
        # mode_pct kendi başına yüksek olsa da bootstrap %5 alt sınırı çok
        # düşükse consensus kırılgan (birkaç üye değişince dağılır) — pas geç.
        if mode_ci_low is not None and mode_ci_low < MIN_MODE_CI_LOW:
            print(
                f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C"
                f" — CI alt %{mode_ci_low} < %{MIN_MODE_CI_LOW} (konsensüs kırılgan), pas"
            )
            return None

        # ── Faz 2: bimodal uyarı (tepe ayrımı geniş = risk) ──────────────────
        # 2-bucket stratejisi bitişik tepeleri (±1°C) halleder; 2°C+ ayrımda
        # hangi tepenin gerçekleşeceği belirsiz → pas.
        if is_bimodal and peak_sep is not None and peak_sep > BIMODAL_MAX_SEPARATION:
            print(
                f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C"
                f" — bimodal dağılım (tepe ayrımı {peak_sep}°C > {BIMODAL_MAX_SEPARATION}), pas"
            )
            return None

        # ── Uncertainty filtresi ────────────────────────────────────────────
        # "Yüksek" belirsizlikte tahmin çok zayıf → geçmiş: %0 win rate
        if isinstance(unc, str) and unc.lower() in SKIP_UNCERTAINTY:
            print(f"  ⛔ {station.upper()} {label}  — belirsizlik çok yüksek ({unc}), pas")
            return None

        # ── Adaptif Bias Düzeltmesi ─────────────────────────────────────────
        # Geçmiş kapalı trade'lerden öğrenilen sistematik sapma (°C cinsinden)
        raw_top_pick = top_pick
        bias = (station_biases or {}).get(station, 0)
        if bias != 0:
            top_pick = top_pick + bias
            print(
                f"  📐 {station.upper()} {label}  "
                f"bias düzeltme {bias:+d}°C  ({raw_top_pick}°C → {top_pick}°C)"
            )

        # ── Settlement Delta Düzeltmesi (Faz 7 + Faz 8 horizon-aware) ───────
        # WU ↔ Open-Meteo sistematik farkı öğrenilmiş medyan (proxy: METAR-OM).
        # settlement_audit tablosundan rolling 60g medyan; yeterli veri yoksa 0.
        # Faz 8: horizon_days geçiyoruz → D+2'de delta %70'e dampening (bias
        # kaynaklarının çakışmasına karşı koruma).
        try:
            from bot.settlement_delta import learn_station_delta
            try:
                _td = datetime.strptime(target_date, "%Y-%m-%d").date()
                _today = datetime.now().date()
                _horizon = max(1, (_td - _today).days)
            except Exception:
                _horizon = None
            sdelta = learn_station_delta(station, horizon_days=_horizon)
        except Exception:
            sdelta = 0.0
        if sdelta:
            pre_sdelta = top_pick
            top_pick = int(round(top_pick + sdelta))
            if top_pick != pre_sdelta:
                print(
                    f"  🎯 {station.upper()} {label}  "
                    f"settlement delta {sdelta:+.1f}°C  ({pre_sdelta}°C → {top_pick}°C)"
                )

        # Aynı station + tarih + top_pick için zaten pozisyon var mı?
        already_same = any(
            t["station"] == station and t["date"] == target_date
            and t["top_pick"] == top_pick and t["status"] == "open"
            for t in trades
        )
        if already_same:
            # Live modda: paper var ama live order hiç açılmamış olabilir (önceki hata).
            # Bu durumda live order atmak için scan_date'i None döndürmeden çağıranın
            # devam etmesi gerekir — ama paper yeniden oluşturulmaz.
            if live_mode:
                live_trades = trader.load_live_trades()
                has_live = any(
                    t["station"] == station and t["date"] == target_date
                    and t["status"] in ("pending_fill", "filled", "sell_pending",
                                        "cancelled", "settled_win", "settled_loss")
                    for t in live_trades
                )
                if not has_live:
                    # Paper var ama live yok → live order at, paper değiştirme
                    paper_match = next(
                        (t for t in trades
                         if t["station"] == station and t["date"] == target_date
                         and t["top_pick"] == top_pick and t["status"] == "open"),
                        None,
                    )
                    if paper_match:
                        print(f"  🔁 {station.upper()} {label}  — {top_pick}°C paper var, live order eksik → tekrar deneniyor")
                        return ("retry_live", paper_match)
            print(f"  ⬜ {station.upper()} {label}  — {top_pick}°C zaten açık, pas")
            return None

        # Farklı top_pick varsa bilgi ver ama devam et
        prev_open = [
            t for t in trades
            if t["station"] == station and t["date"] == target_date and t["status"] == "open"
        ]
        if prev_open:
            prev_picks = ", ".join(f"{t['top_pick']}°C" for t in prev_open)
            print(f"  🔄 {station.upper()} {label}  — tahmin değişti ({prev_picks} → {top_pick}°C), yeni pozisyon açılıyor")

    except Exception as e:
        print(f"  ❌ {station.upper()} {label}  — hava API hatası: {e}")
        return None

    try:
        # 2. PM market fiyatını çek
        r2 = httpx.get(
            f"{WEATHER_API}/api/polymarket",
            params={"station": station, "date": target_date},
            timeout=30,
        )
        r2.raise_for_status()
        pm      = r2.json()
        buckets = pm.get("buckets", [])

        if not buckets:
            print(f"  ⬜ {station.upper()} {label}  — PM market henüz yok")
            return None

        bucket = find_top_pick_bucket(buckets, top_pick)
        if not bucket:
            print(f"  ⬜ {station.upper()} {label}  — bucket eşleşmedi (pick={top_pick}°C)")
            return None

        price   = bucket.get("yes_price", 0)
        cond_id_raw = bucket.get("condition_id", "")
        if isinstance(cond_id_raw, str) and cond_id_raw.startswith("0x"):
            cond_id = str(int(cond_id_raw, 16))
        else:
            cond_id = str(cond_id_raw)
        liq     = pm.get("liquidity", 0)

    except Exception as e:
        print(f"  ❌ {station.upper()} {label}  — PM API hatası: {e}")
        return None

    # 3. Karar ver
    pct = round(price * 100)

    # Faz 9: Ana bucket için 25¢ floor (<15¢ → backtest %0 win rate).
    if price < MIN_MAIN_PRICE:
        print(f"  ⬜ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢ — ucuz bucket ({int(price*100)}¢ < {int(MIN_MAIN_PRICE*100)}¢ floor), pas")
        return None

    # İstasyon bazlı fiyat tavanı (Paris gibi sistematik sorunlu istasyonlar)
    station_max = STATION_MAX_PRICE.get(station, MAX_PRICE)
    if price >= station_max:
        note = f"(Paris tavana kısıtlı: {station_max:.0%})" if station in STATION_MAX_PRICE else "pahalı"
        print(f"  ⬜ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢ — {note}, pas")
        return None

    # EV filtresi: ensemble konsensüs olasılığı vs. piyasa fiyatı
    # Yalnızca mode_pct > market fiyatından en az MIN_EDGE puan fazlaysa gir
    if mode_pct is not None:
        edge = (mode_pct / 100) - price
        if edge < MIN_EDGE:
            print(
                f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢"
                f" — edge yetersiz (ens%{mode_pct} - mkt{pct}¢ = {edge:+.0%} < %{MIN_EDGE*100:.0f}), pas"
            )
            return None

    # trade_shares Faz 7'de signal_score'a göre aşağıda hesaplanır; burada
    # henüz bilinmediği için baz cost'u EV filtresinin ihtiyacı için kur.
    cost          = round(SHARES * price, 2)
    potential_win = round(SHARES - cost, 2)
    cheap_tag     = "💰" if price < 0.20 else "←"
    mode_tag      = f" [ens %{mode_pct}]" if mode_pct else ""

    # ── Faz 3: sinyal kalitesi skoru (0-100 kompozit) ───────────────────────
    try:
        from bot.signal_score import compute_signal_score
        sig = compute_signal_score(
            mode_pct     = mode_pct,
            mode_ci_low  = mode_ci_low,
            mode_ci_high = mode_ci_high,
            edge         = (mode_pct / 100 - price) if mode_pct is not None else None,
            uncertainty  = unc,
            is_bimodal   = is_bimodal,
            n_members    = len(members) if members else 0,
        )
        signal_score = sig["score"]
        signal_grade = sig["grade"]
    except Exception:
        signal_score = None
        signal_grade = None

    # ── Faz 5: sinyal skoru gate'i (zayıf sinyalleri blokla) ────────────────
    # signal_score None ise (hesap başarısız) geçir — eksik veri cezalandırma.
    if is_weak_signal(signal_score):
        print(
            f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢"
            f" — sinyal skoru {signal_score} < {MIN_SIGNAL_SCORE} (zayıf), pas"
        )
        return None

    # ── Faz 9: D+1 yüksek sinyal eşiği ──────────────────────────────────────
    # D+1 backtest: %14 win rate vs D+2 %47. Market D+1'i çok daha verimli
    # fiyatlıyor; sadece D1_MIN_SIGNAL_SCORE (≥88) olan sinyallere gir.
    if horizon == 1 and signal_score is not None and signal_score < D1_MIN_SIGNAL_SCORE:
        print(
            f"  ⛔ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢"
            f" — D+1 eşiği: {signal_score} < {D1_MIN_SIGNAL_SCORE}, pas"
        )
        return None

    # ── Faz 7: sinyal skoru → dinamik pozisyon boyutu ───────────────────────
    # Tier bazlı çarpan: Premium 1.5x, Strong 1.2x, Moderate 1.0x (zayıf bloke).
    try:
        from bot.position_sizing import compute_shares
        trade_shares = compute_shares(SHARES, signal_score)
    except Exception:
        trade_shares = SHARES
    if trade_shares != SHARES:
        print(
            f"  📏 {station.upper()} {label}  "
            f"dinamik size: {SHARES} → {trade_shares} share "
            f"(Q{signal_score})"
        )
    # cost/potential_win trade_shares kullanarak yeniden hesaplanır (aşağıda).

    # 2. pick bilgisi: ensemble'ın 2. adayı neydi?
    second_tag = ""
    if second_pick is not None and second_pct is not None:
        if abs(second_pick - top_pick) == 1:
            second_tag = f" [2.pick:{second_pick}°C %{second_pct}]"

    trade = {
        "id":            f"{station}_{target_date}_{datetime.now().strftime('%H%M%S')}",
        "station":       station,
        "date":          target_date,
        "blend":         round(blend, 1),
        "spread":        round(spread, 2),
        "uncertainty":   unc,
        "top_pick":      top_pick,
        "raw_top_pick":  raw_top_pick,        # bias öncesi değer
        "bias_applied":  bias,                # uygulanan düzeltme (0 = yok)
        "ens_mode_pct":  mode_pct,
        "ens_2nd_pick":  second_pick,
        "ens_2nd_pct":   second_pct,
        # Faz 2 şekil metrikleri (geçmiş analizi için kayıt altına alınır)
        "ens_is_bimodal":   is_bimodal,
        "ens_peak_sep":     peak_sep,
        "ens_mode_ci_low":  mode_ci_low,
        "ens_mode_ci_high": mode_ci_high,
        # Faz 3: sinyal kalitesi
        "signal_score":     signal_score,
        "signal_grade":     signal_grade,
        "bucket_title":  bucket["title"],
        "condition_id":  cond_id,
        "entry_price":   price,
        "shares":        trade_shares,
        "cost_usd":      round(trade_shares * price, 2),
        "potential_win": round(trade_shares - trade_shares * price, 2),
        "liquidity":     liq,
        "status":        "open",
        "entered_at":    datetime.now().isoformat(),
        "actual_temp":   None,
        "result":        None,
        "pnl":           None,
        "settled_at":    None,
    }
    # Dinamik cost/potential_win log için de güncelle
    cost          = trade["cost_usd"]
    potential_win = trade["potential_win"]

    edge_str = f" · edge{((mode_pct/100)-price):+.0%}" if mode_pct is not None else ""
    score_str = f" · Q{signal_score}({signal_grade[0]})" if signal_score is not None else ""
    print(
        f"  ✅ {station.upper()} {label}  "
        f"🎯{top_pick}°C (blend={blend:.1f}){mode_tag}{second_tag} @ {pct}¢ {cheap_tag}  "
        f"{trade_shares} share · risk=${cost:.2f} · pot +${potential_win:.2f}{edge_str}{score_str}"
    )
    result_trades = [trade]

    # ── Faz 9: Multi-Bucket Stratejisi ─────────────────────────────────────
    # Backtest: 3-bucket → win rate %32→%75, ROI %23→%64 (37 trade).
    #
    # Bütçe mantığı:
    #   MULTI_BUCKET_N=3 → THREE_BUCKET_BUDGET=65¢ toplam limit fiyat
    #   MULTI_BUCKET_N=2 → TWO_BUCKET_BUDGET=45¢
    #
    # Faz 10: Market-tabanlı adj seçimi — ensemble count yerine MARKET fiyatına
    # göre sırala. Yüksek market fiyatı = piyasanın en çok güvendiği komşu bucket.
    # Avantajlar:
    #   (a) Ensemble oyu 0 olsa bile market'ta olan ±2°C bucket'ları değerlendirilir
    #   (b) "Top 2 bucket'a her türlü gir" garantisi — bütçe el verdiği sürece
    #       en pahalı (= en olası) komşu her zaman eklenir
    # Her komşu bucket için MIN_PRICE (5¢) alt sınırı uygulanır (ana için
    # MIN_MAIN_PRICE=25¢ ayrı eşik, zaten üstte kontrol edildi).
    if MULTI_BUCKET_N >= 2:
        budget = THREE_BUCKET_BUDGET if MULTI_BUCKET_N >= 3 else TWO_BUCKET_BUDGET
        remaining_budget = budget - price  # ana pick'ten sonra kalan bütçe
        added = 0

        # Market bucket'larını fiyat sırasına göre al (yüksekten düşüğe).
        # Sıralama: 1) market fiyatı yüksek (piyasanın en güvendiği komşu önce)
        #           2) ensemble oyu yüksek (beraberlik bozucu)
        _adj_cands: list = []
        for _b in buckets:
            _thresh = _b.get("threshold")
            if _thresh is None:
                continue
            _t = round(float(_thresh))
            if _t == top_pick:
                continue
            if abs(_t - top_pick) > 2:
                continue
            _bp = float(_b.get("yes_price", 0) or 0)
            if _bp < MIN_PRICE:
                continue
            _ens = counts.get(_t, 0) if members else 0
            _adj_cands.append((_t, _ens, _bp))
        # Faz 10: Trend yönü tiebreaker
        # sdelta ≥ TREND_BIAS_THRESHOLD ise trend yönündeki bucket'a sanal bonus.
        _trend_dir = 0
        if abs(sdelta) >= TREND_BIAS_THRESHOLD:
            _trend_dir = 1 if sdelta > 0 else -1
            _trend_label = "ısınma ↑" if _trend_dir > 0 else "soğuma ↓"
            print(
                f"  🌡️  {station.upper()} {label}  "
                f"trend bias: {_trend_label} (δ={sdelta:+.1f}°C) "
                f"→ {'sıcak' if _trend_dir > 0 else 'soğuk'} yönlü adj öncelikli"
            )

        _adj_cands.sort(
            key=lambda x, _dir=_trend_dir: (
                -(x[2] + (TREND_PRICE_BONUS if (_dir and (x[0] - top_pick) * _dir > 0) else 0.0)),
                -x[1],
            )
        )
        cand_picks_sorted = [(_t, _e) for _t, _e, _ in _adj_cands]

        for cand_temp, cand_count in cand_picks_sorted:
            if added >= MULTI_BUCKET_N - 1:
                break  # yeterince komşu eklendi
            if remaining_budget < MIN_PRICE:
                break  # bütçe bitti

            c_bucket = find_top_pick_bucket(buckets, cand_temp)
            if not c_bucket:
                continue

            c_price = float(c_bucket.get("yes_price", 0) or 0)
            if c_price < MIN_PRICE or c_price > remaining_budget:
                continue  # fiyat bütçe aşıyor veya çok ucuz

            # Faz 11: komşu bucket edge koruması
            # Piyasa, ensemble'ın öngördüğünden çok daha yüksek fiyatlarsa
            # bu komşuya sürpriz bir şekilde girilmesin.
            _c_pct_raw = round(cand_count / len(members) * 100) if members else 0
            _c_edge = (_c_pct_raw / 100) - c_price
            if _c_edge < ADJ_MAX_NEG_EDGE:
                print(
                    f"  ⛔ {station.upper()} {label}  adj skip {cand_temp}°C"
                    f" — edge {_c_edge:+.0%} < {ADJ_MAX_NEG_EDGE:.0%}"
                    f" (piyasa ens'ten uzak: ens%{_c_pct_raw} vs mkt{int(c_price*100)}¢), pas"
                )
                continue

            # Zaten aynı bucket için açık pozisyon var mı?
            already_open = any(
                t["station"] == station and t["date"] == target_date
                and t["top_pick"] == cand_temp and t["status"] == "open"
                for t in trades
            )
            if already_open:
                continue

            c_cond_raw = c_bucket.get("condition_id", "")
            c_cond = (
                str(int(c_cond_raw, 16))
                if isinstance(c_cond_raw, str) and c_cond_raw.startswith("0x")
                else str(c_cond_raw)
            )
            c_pct_ens = round(cand_count / len(members) * 100) if members else 0
            c_cost    = round(trade_shares * c_price, 2)
            c_potwin  = round(trade_shares - c_cost, 2)
            c_pct_lbl = round(c_price * 100)

            adj_trade = {
                "id":               f"{station}_{target_date}_{datetime.now().strftime('%H%M%S')}_adj{added+1}",
                "station":          station,
                "date":             target_date,
                "blend":            round(blend, 1),
                "spread":           round(spread, 2),
                "uncertainty":      unc,
                "top_pick":         cand_temp,
                "raw_top_pick":     cand_temp,
                "bias_applied":     0,
                "ens_mode_pct":     c_pct_ens,
                "ens_2nd_pick":     top_pick,
                "ens_2nd_pct":      mode_pct,
                "ens_is_bimodal":   is_bimodal,
                "ens_peak_sep":     peak_sep,
                "ens_mode_ci_low":  mode_ci_low,
                "ens_mode_ci_high": mode_ci_high,
                "signal_score":     signal_score,
                "signal_grade":     signal_grade,
                "bucket_title":     c_bucket["title"],
                "condition_id":     c_cond,
                "entry_price":      c_price,
                "shares":           trade_shares,
                "cost_usd":         c_cost,
                "potential_win":    c_potwin,
                "liquidity":        liq,
                "status":           "open",
                "entered_at":       datetime.now().isoformat(),
                "actual_temp":      None,
                "result":           None,
                "pnl":              None,
                "settled_at":       None,
                "trade_type":       "multi_bucket",
                "bucket_num":       added + 2,  # 1=main, 2=1.komşu, 3=2.komşu
                "main_pick":        top_pick,   # referans için
            }
            bucket_n = added + 2
            print(
                f"  🎯 BUCKET-{bucket_n}  {station.upper()} {label}  "
                f"🎯{cand_temp}°C [ens%{c_pct_ens}] @ {c_pct_lbl}¢  "
                f"{trade_shares} share · risk=${c_cost:.2f}"
                f"  (bütçe: {int(price*100)}+{int(c_price*100)}={int((price+c_price)*100)}¢/"
                f"{int(budget*100)}¢)"
            )
            result_trades.append(adj_trade)
            remaining_budget -= c_price
            added += 1

    return result_trades


# ── Faz 9: Yardımcı — En İyi N Aday Seçimi ─────────────────────────────────
def _select_top_candidates(candidates: list, max_n: int = MAX_DAILY_POSITIONS_PER_DATE) -> list:
    """Aday trade'leri signal_score'a göre station bazlı sıralar, top N station döner.

    Her station'ın tüm bucket'larını (main + adj) birlikte tutar.
    Örn: max_n=3 → en iyi 3 station'ın trade'leri (her biri 1-3 trade içerebilir).
    """
    if not candidates:
        return []

    # Station'a göre grupla
    by_station: dict = {}
    for t in candidates:
        s = t.get("station", "?")
        by_station.setdefault(s, []).append(t)

    # Her station'ın primary signal_score'unu bul (main bucket = bucket_num yok veya 1)
    def primary_score(station_trades: list) -> int:
        primaries = [
            t for t in station_trades
            if not t.get("trade_type") in ("multi_bucket", "micro_hedge")
        ]
        src = primaries if primaries else station_trades
        scores = [t.get("signal_score") or 0 for t in src]
        return max(scores)

    sorted_stations = sorted(
        by_station.items(),
        key=lambda kv: primary_score(kv[1]),
        reverse=True,
    )

    selected: list = []
    for _station, station_trades in sorted_stations[:max_n]:
        selected.extend(station_trades)
    return selected


# ── Scan: Fırsat Tara (D+1 ve D+2) ─────────────────────────────────────────
def scan():
    trades    = load_trades()
    today     = datetime.now()
    live_mode = "--live" in sys.argv   # python scanner.py scan --live

    scan_targets = [
        (today + timedelta(days=1)).strftime("%Y-%m-%d"),   # D+1
        (today + timedelta(days=2)).strftime("%Y-%m-%d"),   # D+2
    ]

    # Geçmiş veriden istasyon biaslarını öğren
    station_biases = compute_station_biases(trades)

    mode_tag = "🔴 LIVE + PAPER" if live_mode else "📄 PAPER"
    print(f"\n{'='*62}")
    print(f"  🔍 TARAMA — D+1 & D+2  [{mode_tag}]")
    print(f"  {today.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*62}")

    # Öğrenilen biasları logla
    if station_biases:
        bias_parts = [
            f"{s.upper()}:{v:+d}°C"
            for s, v in sorted(station_biases.items())
            if v != 0
        ]
        if bias_parts:
            print(f"\n  📐 Öğrenilen bias düzeltmeleri: {' | '.join(bias_parts)}")
        else:
            print(f"\n  📐 Tüm istasyonlar nötr — bias düzeltme yok")
    else:
        print(f"\n  📐 Yeterli kapalı trade yok — bias düzeltme yok")

    # Live mod için trader modülünü yükle
    trader = None
    if live_mode:
        try:
            import importlib.util, pathlib
            spec   = importlib.util.spec_from_file_location(
                "trader",
                pathlib.Path(__file__).parent / "trader.py"
            )
            trader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(trader)
            print(f"  💼 Trader modülü yüklendi (LIVE_SHARES={trader.LIVE_SHARES})")
        except Exception as e:
            print(f"  ❌ Trader modülü yüklenemedi: {e}")
            live_mode = False

    total_new = 0
    live_total = 0
    for i, target_date in enumerate(scan_targets, start=1):
        horizon = i   # 1=D+1, 2=D+2
        print(f"\n  ── D+{i} ({target_date}) {'─'*38}")
        if horizon == 1:
            print(f"  ℹ️  D+1: sinyal eşiği yüksek ({D1_MIN_SIGNAL_SCORE}+), sadece premium sinyal")
        day_new  = 0
        day_live = 0

        # ── Faz 9: Retry-live (paper var, live eksik) ─────────────────────
        # Bu kolu scan başında işle — best-N filtresi dışında (zaten paper var).
        for station in STATIONS:
            if station not in STATION_WHITELIST:
                continue
            retry = scan_date(
                station, target_date, trades, station_biases,
                live_mode=live_mode, trader=trader if live_mode else None,
                horizon=horizon,
            )
            if isinstance(retry, tuple) and retry[0] == "retry_live":
                _, paper_match = retry
                if live_mode:
                    try:
                        live_r = trader.place_limit_order(
                            condition_id = paper_match["condition_id"],
                            price        = paper_match["entry_price"],
                            station      = paper_match["station"],
                            date         = paper_match["date"],
                            top_pick     = paper_match["top_pick"],
                            bucket_title = paper_match["bucket_title"],
                            paper_id     = paper_match["id"],
                        )
                        if live_r:
                            day_live  += 1
                            live_total += 1
                    except Exception as e:
                        print(f"  ⚠️  Live order hatası ({station}): {e}")

        # ── Faz 9: Tüm adayları topla, top-N seç ─────────────────────────
        date_candidates: list = []
        for station in STATIONS:
            if station not in STATION_WHITELIST:
                continue
            result = scan_date(
                station, target_date, trades, station_biases,
                live_mode=False, trader=None,   # dry-run, kaydet değil
                horizon=horizon,
            )
            if isinstance(result, list) and result:
                date_candidates.extend(result)

        # Seçilmeyenleri logla, seçilenleri kaydet
        selected = _select_top_candidates(date_candidates, max_n=MAX_DAILY_POSITIONS_PER_DATE)
        selected_stations = {t["station"] for t in selected}
        skipped_stations  = {
            t["station"] for t in date_candidates
            if t["station"] not in selected_stations
        }
        if skipped_stations:
            print(
                f"  📊 Best-{MAX_DAILY_POSITIONS_PER_DATE} filtresi: "
                f"{', '.join(s.upper() for s in sorted(skipped_stations))} "
                f"sinyal sırasına göre elendi"
            )

        for new_trade in selected:
            station = new_trade["station"]
            # Supersede: aynı station+date'de eski open trade'leri kapat
            new_top_picks = {
                t["top_pick"] for t in selected
                if t["station"] == station and t["date"] == target_date
            }
            for old in trades:
                if (old["station"] == station and old["date"] == target_date
                        and old["status"] == "open"
                        and old["top_pick"] not in new_top_picks):
                    old["status"] = "superseded"
                    picks_str = ", ".join(f"{p}°C" for p in sorted(new_top_picks))
                    old["notes"] = f"Tahmin değişti → {picks_str}"

            trades.append(new_trade)
            day_new   += 1
            total_new += 1

            # Live mode: CLOB'a gönder
            if live_mode:
                try:
                    live_r = trader.place_limit_order(
                        condition_id = new_trade["condition_id"],
                        price        = new_trade["entry_price"],
                        station      = new_trade["station"],
                        date         = new_trade["date"],
                        top_pick     = new_trade["top_pick"],
                        bucket_title = new_trade["bucket_title"],
                        paper_id     = new_trade["id"],
                    )
                    if live_r:
                        day_live  += 1
                        live_total += 1
                except Exception as e:
                    print(f"  ⚠️  Live order hatası ({station}): {e}")

        live_str = f"  |  🔴 {day_live} live emir" if live_mode else ""
        print(f"  → D+{i}: {day_new} yeni paper{live_str}")

    save_trades(trades)

    open_count = len([t for t in trades if t["status"] == "open"])
    print(f"\n  📝 Toplam {total_new} yeni paper trade kaydedildi")
    if live_mode:
        print(f"  🔴 Toplam {live_total} live emir gönderildi")
    print(f"  📂 Toplam açık paper pozisyon: {open_count}")
    print()

# ── Settle: Dünkü Pozisyonları Kapat ───────────────────────────────────────
def settle():
    trades    = load_trades()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    to_settle = [t for t in trades if t["date"] == yesterday and t["status"] == "open"]

    print(f"\n{'='*62}")
    print(f"  🏁 SETTLEMENT — {yesterday}")
    print(f"{'='*62}")

    if not to_settle:
        print("  Settle edilecek pozisyon yok.\n")
        return

    settled = 0
    for trade in to_settle:
        station = trade["station"]
        label   = STATION_LABELS.get(station, station.upper())

        try:
            actual = None

            # ── Faz 6b: her iki kaynağı da çağır (disagreement audit için) ──
            actual_om_raw: float | None = get_actual_temp_open_meteo(station, yesterday)
            actual_metar_raw: float | None = None
            try:
                r = httpx.get(
                    f"{WEATHER_API}/api/metar-history?station={station}", timeout=30
                )
                r.raise_for_status()
                daily      = r.json().get("daily_maxes", [])
                day_record = next((d for d in daily if d["date"] == yesterday), None)
                if day_record and day_record.get("max_temp") is not None:
                    actual_metar_raw = float(day_record["max_temp"])
            except Exception as metar_e:
                print(f"  ⚠️  METAR query başarısız ({station}): {metar_e}")

            # Audit kaydı (sessiz) — her mevcut kaynak için ayrı satır
            try:
                from bot.db import record_settlement_source
                if actual_om_raw is not None:
                    record_settlement_source(station, yesterday, "open-meteo", actual_om_raw)
                if actual_metar_raw is not None:
                    record_settlement_source(station, yesterday, "metar", actual_metar_raw)
            except Exception:
                pass

            # Birincil: Open-Meteo (Polymarket WU kaynağıyla uyumlu daily max)
            if actual_om_raw is not None:
                actual = round(actual_om_raw)
                print(f"  🌡️  {station.upper()} Open-Meteo: {actual_om_raw:.1f}°C → {actual}°C")

            # Yedek: METAR (Open-Meteo yoksa)
            if actual is None and actual_metar_raw is not None:
                actual = round(actual_metar_raw)
                print(f"  🌡️  {station.upper()} METAR (yedek): {actual}°C")

            # Uyumsuzluk uyarısı — her iki kaynak da var ama farklılar
            if actual_om_raw is not None and actual_metar_raw is not None:
                diff_c      = abs(actual_om_raw - actual_metar_raw)
                diff_bucket = abs(round(actual_om_raw) - round(actual_metar_raw))
                if diff_bucket >= 1:
                    print(
                        f"  ⚠️  {station.upper()} kaynak uyumsuzluk: "
                        f"OM={actual_om_raw:.1f} METAR={actual_metar_raw:.1f} "
                        f"(Δ={diff_c:.1f}°C, bucket Δ={diff_bucket})"
                    )

            if actual is None:
                print(f"  ⏳ {station.upper()} {label}  — gerçek veri henüz yok (settlement bekle)")
                continue

            won = bucket_won(trade["bucket_title"], actual)

            if won is None:
                print(f"  ❓ {station.upper()} {label}  — bucket sonuç belirlenemedi")
                continue

            # Eski format uyumluluğu: cost_usd yoksa size_usd'den al
            cost = trade.get("cost_usd") or trade.get("size_usd", 0)
            pnl  = round(trade["potential_win"] if won else -cost, 2)

            trade.update({
                "actual_temp": actual,
                "result":      "WIN" if won else "LOSS",
                "pnl":         pnl,
                "status":      "closed",
                "settled_at":  datetime.now().isoformat(),
            })
            settled += 1

            # ── Faz 3: settlement gözlemini analitik tablolara yaz ──
            # forecast_errors — sessiz başarısızlık (bot akışı bozmasın).
            try:
                from bot.db import record_forecast_error, already_recorded_error
                tid = trade.get("id")
                if tid and not already_recorded_error(tid):
                    record_forecast_error(
                        date=yesterday,
                        station=station,
                        horizon_days=None,     # scanner bu bilgiyi henüz taşımıyor
                        blend=trade.get("blend"),
                        top_pick=trade.get("top_pick"),
                        spread=trade.get("spread"),
                        uncertainty=trade.get("uncertainty"),
                        actual_temp=float(actual),
                        trade_id=tid,
                    )
            except Exception:
                pass

            # ── Faz 4: model_forecasts'a actual yaz (per-model RMSE) ──
            try:
                from bot.db import record_model_actuals
                record_model_actuals(station, yesterday, float(actual))
            except Exception:
                pass

            emoji = "🟢" if won else "🔴"
            print(
                f"  {emoji} {station.upper()} {label}  "
                f"tahmin={trade['top_pick']}°C  gerçek={actual}°C  "
                f"[{trade['bucket_title']}]  "
                f"→ {'KAZANDI' if won else 'KAYBETTİ'} "
                f"${'+' if pnl >= 0 else ''}{pnl:.0f}"
            )

        except Exception as e:
            print(f"  ❌ {station.upper()} {label}  — settle hatası: {e}")

    save_trades(trades)

    # Özet
    closed    = [t for t in trades if t["status"] == "closed"]
    wins      = [t for t in closed if t["result"] == "WIN"]
    total_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
    wr        = len(wins) / len(closed) * 100 if closed else 0

    print(f"\n  Bugün settle: {settled} | "
          f"Toplam: {len(wins)}/{len(closed)} kazanıldı ({wr:.0f}%) | "
          f"Net P&L: ${'+'if total_pnl>=0 else ''}{total_pnl:.0f}")
    print()

# ── Report: Tam Geçmiş ──────────────────────────────────────────────────────
def report():
    trades = load_trades()

    print(f"\n{'='*62}")
    print(f"  📈 PAPER TRADING RAPORU")
    print(f"  {SHARES} share/trade · 1 share = $1 payout")
    print(f"{'='*62}")

    if not trades:
        print("  Henüz trade yok. 'python scanner.py scan' ile başla.\n")
        return

    open_t    = [t for t in trades if t["status"] == "open"]
    closed    = [t for t in trades if t["status"] == "closed"]
    wins      = [t for t in closed if t["result"] == "WIN"]
    losses    = [t for t in closed if t["result"] == "LOSS"]
    total_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
    wr           = len(wins) / len(closed) * 100 if closed else 0
    total_cost   = sum(t.get("cost_usd") or t.get("size_usd", 0) for t in closed)
    roi          = total_pnl / total_cost * 100 if total_cost > 0 else 0

    avg_win  = sum(t["pnl"] for t in wins)  / len(wins)  if wins  else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    ev       = (wr/100) * avg_win + (1 - wr/100) * avg_loss

    # Sapma dağılımı
    deltas = [
        t["actual_temp"] - t["top_pick"]
        for t in closed
        if t.get("actual_temp") is not None and t.get("top_pick") is not None
    ]
    exact   = sum(1 for d in deltas if d == 0)
    off_one = sum(1 for d in deltas if abs(d) == 1)

    print(f"\n  Açık pozisyon : {len(open_t)}")
    print(f"  Toplam kapalı : {len(closed)}  ({len(wins)} kazanç / {len(losses)} kayıp)")
    print(f"  İsabet oranı  : {wr:.1f}%")
    print(f"  Net P&L       : ${'+'if total_pnl>=0 else ''}{total_pnl:.2f}")
    print(f"  Toplam risk   : ${total_cost:.2f}  →  ROI: {'+'if roi>=0 else ''}{roi:.1f}%")
    print(f"  Trade başı EV : ${'+'if ev>=0 else ''}{ev:.2f}  "
          f"(ort.kazanç ${avg_win:+.2f} / ort.kayıp ${avg_loss:.2f})")
    if deltas:
        print(f"  Sapma özeti   : tam isabet {exact}/{len(deltas)} (%{exact/len(deltas)*100:.0f})  "
              f"± 1°C {off_one}/{len(deltas)} (%{off_one/len(deltas)*100:.0f})")

    # İstasyon bazlı özet — bias ve sapma bilgisi ile
    station_biases = compute_station_biases(trades)
    from collections import defaultdict
    station_errors: dict = defaultdict(list)
    for t in closed:
        if t.get("actual_temp") is not None and t.get("top_pick") is not None:
            station_errors[t["station"]].append(t["actual_temp"] - t["top_pick"])

    if closed:
        print(f"\n  İSTASYON BAZLI:")
        stations_seen = dict.fromkeys(t["station"] for t in closed)
        for s in stations_seen:
            s_trades = [t for t in closed if t["station"] == s]
            s_wins   = [t for t in s_trades if t["result"] == "WIN"]
            s_pnl    = sum(t["pnl"] for t in s_trades if t["pnl"] is not None)
            s_wr     = len(s_wins) / len(s_trades) * 100
            label    = STATION_LABELS.get(s, s.upper())
            emoji    = "🟢" if s_pnl > 0 else "🔴"
            errs     = station_errors.get(s, [])
            avg_err  = sum(errs)/len(errs) if errs else 0
            bias_val = station_biases.get(s, 0)
            bias_str = f"bias:{bias_val:+d}°C" if bias_val != 0 else "bias:nötr"
            print(f"  {emoji} {s.upper()} {label}  "
                  f"{len(s_wins)}/{len(s_trades)} ({s_wr:.0f}%)  "
                  f"P&L: ${'+'if s_pnl>=0 else ''}{s_pnl:.0f}  "
                  f"ort.Δ:{avg_err:+.1f}°C  {bias_str}")

    # Açık pozisyonlar
    if open_t:
        print(f"\n  AÇIK POZİSYONLAR ({len(open_t)}):")
        for t in sorted(open_t, key=lambda x: (x["date"], x["station"])):
            label    = STATION_LABELS.get(t["station"], t["station"].upper())
            is_new   = "cost_usd" in t
            cost     = t.get("cost_usd") or t.get("size_usd", 0)
            shares   = t.get("shares", SHARES) if is_new else "—"
            size_str = f"{shares} share · risk=${cost:.2f}" if is_new else f"risk=${cost:.0f}"
            print(
                f"  📂 {t['station'].upper()} {label}  {t['date']}  "
                f"🎯{t['top_pick']}°C @ {round(t['entry_price']*100)}¢  "
                f"{size_str} · pot +${t['potential_win']:.2f}"
            )

    # Son 15 kapalı trade
    if closed:
        recent = sorted(closed, key=lambda x: x.get("settled_at") or "", reverse=True)[:15]
        print(f"\n  SON {len(recent)} KAPANAN TRADE:")
        for t in recent:
            label  = STATION_LABELS.get(t["station"], t["station"].upper())
            emoji  = "🟢" if t["result"] == "WIN" else "🔴"
            pnl    = t["pnl"] or 0
            shares = t.get("shares", SHARES)
            cost   = t.get("cost_usd") or t.get("size_usd", 0)
            print(
                f"  {emoji} {t['station'].upper()} {label}  {t['date']}  "
                f"tahmin={t['top_pick']}°C  gerçek={t['actual_temp']}°C  "
                f"@ {round(t['entry_price']*100)}¢  {shares} share  "
                f"P&L: ${'+'if pnl>=0 else ''}{pnl:.2f}"
            )
    print()

# ── Status: Kısa Özet ───────────────────────────────────────────────────────
def status():
    trades = load_trades()
    open_t = [t for t in trades if t["status"] == "open"]
    closed = [t for t in trades if t["status"] == "closed"]
    wins   = [t for t in closed if t["result"] == "WIN"]
    pnl    = sum(t["pnl"] for t in closed if t["pnl"] is not None)

    print(f"\n  📊 Açık: {len(open_t)} | "
          f"Kapalı: {len(closed)} ({len(wins)} kazanç) | "
          f"P&L: ${'+'if pnl>=0 else ''}{pnl:.2f}\n")

    for t in sorted(open_t, key=lambda x: (x["date"], x["station"])):
        label    = STATION_LABELS.get(t["station"], t["station"].upper())
        is_new   = "cost_usd" in t          # yeni format: 10 share bazlı
        cost     = t.get("cost_usd") or t.get("size_usd", 0)
        shares   = t.get("shares", SHARES) if is_new else "—"
        size_str = f"{shares} share · risk=${cost:.2f}" if is_new else f"risk=${cost:.0f} (eski)"
        print(f"  📂 {t['station'].upper()} {label}  "
              f"{t['date']}  🎯{t['top_pick']}°C @ {round(t['entry_price']*100)}¢  "
              f"{size_str} · pot +${t['potential_win']:.2f}")
    if not open_t:
        print("  Açık pozisyon yok.")
    print()

# ── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    commands = {
        "scan":    scan,
        "settle":  settle,
        "report":  report,
        "status":  status,
    }

    if cmd not in commands:
        print(f"\nKullanım: python scanner.py [{'|'.join(commands.keys())}]\n")
        sys.exit(1)

    commands[cmd]()
