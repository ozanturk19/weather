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
            "eham", "eddm", "epwa", "efhk", "omdb", "rjtt"]

STATION_LABELS = {
    "eglc": "Londra   ", "lfpg": "Paris    ", "limc": "Milano   ",
    "lemd": "Madrid   ", "ltfm": "İstanbul ", "ltac": "Ankara   ",
    "eham": "Amsterdam", "eddm": "Münih    ", "epwa": "Varşova  ",
    "efhk": "Helsinki ", "omdb": "Dubai    ", "rjtt": "Tokyo    ",
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
    "omdb": (25.253,  55.364),
    "rjtt": (35.552, 139.780),
}

# Risk parametreleri
SHARES       = 10     # her işlemde alınan share adedi (1 share kazanınca $1 öder)
MIN_PRICE    = 0.05   # çok ucuz → şüpheli likidite, atla
MAX_PRICE    = 0.40   # 40¢ üzeri pozisyonlarda edge zayıflıyor → pas
                      # (eski 0.50'den düşürüldü — canlı öncesi edge kalitesi için)

# Kalite filtreleri
SKIP_UNCERTAINTY = {"yüksek", "high", "very high"}  # bu seviyelerde hiç pozisyon açma
MIN_BIAS_TRADES  = 8   # bias hesabı için minimum kapalı trade (4'ten artırıldı)
                       # Az veriyle yanlış bias uygulanmasını önler (Paris +3°C vakası)
MAX_BIAS_CORRECTION = 2  # bias tavanı: en fazla ±2°C düzeltme uygulanır

# Ensemble kalite filtreleri
MIN_MODE_PCT = 30    # ensemble üyelerinin en az %30'u aynı bucket'ta hemfikir olmalı
                     # ICON 40 üye → min 12 üye aynı bucket'ta demek
MIN_EDGE     = 0.05  # ensemble olasılığı (mode_pct) - market fiyatı en az 5 puan fazla olmalı
                     # Örn: %35 ensemble → 28¢ market → edge +7% → gir ✓
                     #      %30 ensemble → 28¢ market → edge +2% → geçersiz ✗

# İstasyon bazlı fiyat tavanı (sistematik sorunlu istasyonlar)
STATION_MAX_PRICE: dict[str, float] = {
    "lfpg": 0.18,   # Paris: %10 win rate, settlement kaynak uyumsuzluğu (+1.9°C artık hata)
                    # 18¢ altında EV hâlâ pozitif olabilir, üstü riskli
}

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
    TRADES_FILE.write_text(
        json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # SQLite ayna senkronizasyonu — sessiz başarısızlık (bot akışı bozmasın)
    try:
        from bot.db import sync_paper_trades, init_db
        init_db()
        sync_paper_trades()
    except Exception:
        pass

# ── Adaptif Bias Hesabı ─────────────────────────────────────────────────────
def compute_station_biases(trades: list) -> dict:
    """Kapalı trade'lerden istasyon bazlı sistematik tahmin hatasını öğren.

    Her istasyon için: bias = ortalama(gerçek - tahmin), en yakın tam sayıya yuvarlanır.
    Yeterli veri (MIN_BIAS_TRADES) yoksa o istasyon için 0 döner.

    Örnek: EPWA için geçmiş 8 trade'de ortalama +1.4°C hata → bias = +1
    Yani scanner top_pick'e +1 ekler ve bir üst bucket'ı hedefler.
    """
    from collections import defaultdict
    errors: dict[str, list[float]] = defaultdict(list)

    for t in trades:
        if t["status"] == "closed" and t.get("actual_temp") is not None and t.get("top_pick") is not None:
            delta = t["actual_temp"] - t["top_pick"]
            errors[t["station"]].append(delta)

    biases: dict[str, int] = {}
    for station, deltas in errors.items():
        if len(deltas) >= MIN_BIAS_TRADES:
            avg  = sum(deltas) / len(deltas)
            bias = math.floor(avg + 0.5)   # Python banker's rounding'i atla (round(0.5)=0 problemi)
            # Güvenlik tavanı: çok agresif bias düzeltmesini önle
            # (Örn: Paris 6 trade ile +3°C hesapladı ama bu aşırıydı)
            bias = max(-MAX_BIAS_CORRECTION, min(MAX_BIAS_CORRECTION, bias))
            if bias != 0:
                biases[station] = bias

    return biases

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
              live_mode: bool = False, trader=None) -> dict | None:
    """Bir istasyon + tarih için sinyal ara. Yeni trade dict döner veya None.

    station_biases: compute_station_biases() çıktısı — adaptif tahmin düzeltmesi.
    live_mode/trader: aktarılırsa, paper var ama live order eksikse retry_live tuple döner.
    """
    label = STATION_LABELS.get(station, station.upper())

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
        try:
            r_ens   = httpx.get(f"{WEATHER_API}/api/ensemble?station={station}", timeout=30)
            members = r_ens.json().get("days", {}).get(target_date, {}).get("member_maxes", [])
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
                    and t["status"] in ("pending_fill", "filled",
                                        "settled_win", "settled_loss")
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

    if price < MIN_PRICE:
        print(f"  ⬜ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢ — çok ucuz, şüpheli")
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

    cost          = round(SHARES * price, 2)
    potential_win = round(SHARES - cost, 2)
    cheap_tag     = "💰" if price < 0.20 else "←"
    mode_tag      = f" [ens %{mode_pct}]" if mode_pct else ""

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
        "bucket_title":  bucket["title"],
        "condition_id":  cond_id,
        "entry_price":   price,
        "shares":        SHARES,
        "cost_usd":      cost,
        "potential_win": potential_win,
        "liquidity":     liq,
        "status":        "open",
        "entered_at":    datetime.now().isoformat(),
        "actual_temp":   None,
        "result":        None,
        "pnl":           None,
        "settled_at":    None,
    }

    edge_str = f" · edge{((mode_pct/100)-price):+.0%}" if mode_pct is not None else ""
    print(
        f"  ✅ {station.upper()} {label}  "
        f"🎯{top_pick}°C (blend={blend:.1f}){mode_tag}{second_tag} @ {pct}¢ {cheap_tag}  "
        f"{SHARES} share · risk=${cost:.2f} · pot +${potential_win:.2f}{edge_str}"
    )
    result_trades = [trade]

    # ── 2-Bucket Stratejisi ─────────────────────────────────────────────────
    # Ensemble 2. adayı birinciye bitişikse (±1°C) ve yeterli edge varsa,
    # her iki bucket'a girer: ±1°C ölçüm/WU belirsizliğine karşı hedge.
    # Maliyet 2×risk, biri kazanırsa net ~+$5.50 (2×$2.25 risk, $10 payout).
    if (
        second_pick is not None
        and second_pct is not None
        and abs(second_pick - top_pick) == 1
        and second_pct >= MIN_MODE_PCT
    ):
        second_bucket = find_top_pick_bucket(buckets, second_pick)
        if second_bucket:
            s_price = second_bucket.get("yes_price", 0)
            s_edge  = (second_pct / 100) - s_price
            # 2. bucket için zaten pozisyon var mı?
            already_second = any(
                t["station"] == station and t["date"] == target_date
                and t["top_pick"] == second_pick and t["status"] == "open"
                for t in trades
            )
            station_max = STATION_MAX_PRICE.get(station, MAX_PRICE)
            if (
                not already_second
                and MIN_PRICE <= s_price < station_max
                and s_edge >= MIN_EDGE
            ):
                s_cond_raw = second_bucket.get("condition_id", "")
                if isinstance(s_cond_raw, str) and s_cond_raw.startswith("0x"):
                    s_cond = str(int(s_cond_raw, 16))
                else:
                    s_cond = str(s_cond_raw)
                s_cost    = round(SHARES * s_price, 2)
                s_pot_win = round(SHARES - s_cost, 2)
                s_pct     = round(s_price * 100)
                second_trade = {
                    "id":            f"{station}_{target_date}_{datetime.now().strftime('%H%M%S')}_2nd",
                    "station":       station,
                    "date":          target_date,
                    "blend":         round(blend, 1),
                    "spread":        round(spread, 2),
                    "uncertainty":   unc,
                    "top_pick":      second_pick,
                    "raw_top_pick":  second_pick,
                    "bias_applied":  0,
                    "ens_mode_pct":  second_pct,
                    "ens_2nd_pick":  top_pick,
                    "ens_2nd_pct":   mode_pct,
                    "bucket_title":  second_bucket["title"],
                    "condition_id":  s_cond,
                    "entry_price":   s_price,
                    "shares":        SHARES,
                    "cost_usd":      s_cost,
                    "potential_win": s_pot_win,
                    "liquidity":     liq,
                    "status":        "open",
                    "entered_at":    datetime.now().isoformat(),
                    "actual_temp":   None,
                    "result":        None,
                    "pnl":           None,
                    "settled_at":    None,
                    "two_bucket":    True,
                }
                print(
                    f"  🔀 2.BUCKET  {station.upper()} {label}  "
                    f"🎯{second_pick}°C [ens%{second_pct}] @ {s_pct}¢  "
                    f"{SHARES} share · risk=${s_cost:.2f} · edge{s_edge:+.0%}"
                )
                result_trades.append(second_trade)

    return result_trades


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
        print(f"\n  ── D+{i} ({target_date}) {'─'*38}")
        day_new  = 0
        day_live = 0
        for station in STATIONS:
            trade = scan_date(station, target_date, trades, station_biases,
                              live_mode=live_mode, trader=trader if live_mode else None)

            # Özel durum: paper var ama live order eksik → sadece live order at
            if isinstance(trade, tuple) and trade[0] == "retry_live":
                _, paper_match = trade
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
                continue

            # scan_date artık liste döner ([trade] veya [trade, second_trade])
            new_list = trade if isinstance(trade, list) else []
            if new_list:
                # Yeni top_pick seti: artık sadece bu setde olmayanlar supersede edilir
                new_top_picks = {t["top_pick"] for t in new_list}
                for old in trades:
                    if (old["station"] == station and old["date"] == target_date
                            and old["status"] == "open"
                            and old["top_pick"] not in new_top_picks):
                        old["status"] = "superseded"
                        picks_str = ", ".join(f"{p}°C" for p in sorted(new_top_picks))
                        old["notes"]  = f"Tahmin değişti → {picks_str}"

                for new_trade in new_list:
                    trades.append(new_trade)
                    day_new  += 1
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

            # Birincil: Open-Meteo — Polymarket'ın WU kaynağıyla uyumlu daily max
            actual_om = get_actual_temp_open_meteo(station, yesterday)
            if actual_om is not None:
                actual = round(actual_om)
                print(f"  🌡️  {station.upper()} Open-Meteo: {actual_om:.1f}°C → {actual}°C")

            # Yedek: METAR API (Open-Meteo veri yoksa veya hata verdiyse)
            if actual is None:
                try:
                    r = httpx.get(
                        f"{WEATHER_API}/api/metar-history?station={station}", timeout=30
                    )
                    r.raise_for_status()
                    daily      = r.json().get("daily_maxes", [])
                    day_record = next((d for d in daily if d["date"] == yesterday), None)
                    if day_record:
                        actual = round(day_record["max_temp"])
                        print(f"  🌡️  {station.upper()} METAR (yedek): {actual}°C")
                except Exception as metar_e:
                    print(f"  ⚠️  METAR yedek başarısız ({station}): {metar_e}")

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
