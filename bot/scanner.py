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
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# ── Ayarlar ────────────────────────────────────────────────────────────────
WEATHER_API   = "http://localhost:8001"
TRADES_FILE   = Path(__file__).parent / "paper_trades.json"

STATIONS = ["eglc", "ltac", "limc", "ltfm", "lemd", "lfpg",
            "eham", "eddm", "epwa", "efhk"]

STATION_LABELS = {
    "eglc": "Londra   ", "lfpg": "Paris    ", "limc": "Milano   ",
    "lemd": "Madrid   ", "ltfm": "İstanbul ", "ltac": "Ankara   ",
    "eham": "Amsterdam", "eddm": "Münih    ", "epwa": "Varşova  ",
    "efhk": "Helsinki ",
}

# Risk parametreleri
SHARES       = 10     # her işlemde alınan share adedi (1 share kazanınca $1 öder)
MIN_PRICE    = 0.05   # çok ucuz → şüpheli likidite, atla
MAX_PRICE    = 0.50   # bunun altı "ucuz" → al sinyali

# ── Trade Depolama ──────────────────────────────────────────────────────────
def load_trades() -> list:
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
    return []

def save_trades(trades: list):
    TRADES_FILE.write_text(
        json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8"
    )

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
    range_m  = re.search(r'(-?\d+)[^-\d]+(-?\d+)', t)   # "X to Y" veya "X-Y"
    exact_m  = re.match(r'^(-?\d+)\s*°?C?$', t)

    if higher_m: return actual >= int(higher_m.group(1))
    if below_m:  return actual <= int(below_m.group(1))
    if exact_m:  return round(actual) == int(exact_m.group(1))
    if range_m:  return int(range_m.group(1)) <= actual <= int(range_m.group(2))
    return None

# ── Scan: Fırsat Tara ───────────────────────────────────────────────────────
def scan():
    trades   = load_trades()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\n{'='*62}")
    print(f"  🔍 PAPER TRADING TARAMASI — D+1 ({tomorrow})")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*62}")

    new_trades = 0
    for station in STATIONS:
        label = STATION_LABELS.get(station, station.upper())

        try:
            # 1. Hava tahmini çek
            r = httpx.get(f"{WEATHER_API}/api/weather?station={station}", timeout=30)
            r.raise_for_status()
            days     = r.json().get("days", {})
            day_data = days.get(tomorrow, {})
            blend_obj = day_data.get("blend", {})

            # Bias düzeltmeli blend kullan
            if blend_obj.get("bias_active") and blend_obj.get("bias_corrected_blend"):
                blend = blend_obj["bias_corrected_blend"]
            else:
                blend = blend_obj.get("max_temp")

            if blend is None:
                print(f"  ⬜ {station.upper()} {label}  — tahmin yok")
                continue

            spread = blend_obj.get("spread", 0) or 0
            unc    = blend_obj.get("uncertainty", "?")

            # top_pick: ensemble member maxlarının modu, yoksa round(blend)
            # Böylece 18.6 blend ama üyelerin çoğu 17 diyorsa 17 seçilir
            try:
                r_ens = httpx.get(f"{WEATHER_API}/api/ensemble?station={station}", timeout=30)
                members = r_ens.json().get("days", {}).get(tomorrow, {}).get("member_maxes", [])
            except Exception:
                members = []

            if members:
                counts   = Counter(round(m) for m in members)
                top_pick = counts.most_common(1)[0][0]
                mode_pct = round(counts[top_pick] / len(members) * 100)
            else:
                top_pick = round(blend)
                mode_pct = None

            # Aynı station + tarih + top_pick için zaten pozisyon var mı?
            # top_pick değiştiyse (forecast güncellendiyse) yeni pozisyon açılabilir.
            already_same = any(
                t["station"] == station and t["date"] == tomorrow
                and t["top_pick"] == top_pick and t["status"] == "open"
                for t in trades
            )
            if already_same:
                print(f"  ⬜ {station.upper()} {label}  — {top_pick}°C zaten açık, pas")
                continue

            # Farklı top_pick varsa bilgi ver ama devam et
            prev_open = [t for t in trades if t["station"] == station and t["date"] == tomorrow and t["status"] == "open"]
            if prev_open:
                prev_picks = ", ".join(f"{t['top_pick']}°C" for t in prev_open)
                print(f"  🔄 {station.upper()} {label}  — tahmin değişti ({prev_picks} → {top_pick}°C), yeni pozisyon açılıyor")

        except Exception as e:
            print(f"  ❌ {station.upper()} {label}  — hava API hatası: {e}")
            continue

        try:
            # 2. PM market fiyatını çek
            r2 = httpx.get(
                f"{WEATHER_API}/api/polymarket",
                params={"station": station, "date": tomorrow},
                timeout=30,
            )
            r2.raise_for_status()
            pm      = r2.json()
            buckets = pm.get("buckets", [])

            if not buckets:
                print(f"  ⬜ {station.upper()} {label}  — PM market henüz yok")
                continue

            bucket = find_top_pick_bucket(buckets, top_pick)
            if not bucket:
                print(f"  ⬜ {station.upper()} {label}  — bucket eşleşmedi (pick={top_pick}°C)")
                continue

            price    = bucket.get("yes_price", 0)
            cond_id  = bucket.get("condition_id", "")
            liq      = pm.get("liquidity", 0)

        except Exception as e:
            print(f"  ❌ {station.upper()} {label}  — PM API hatası: {e}")
            continue

        # 3. Karar ver
        pct = round(price * 100)

        if price < MIN_PRICE:
            print(f"  ⬜ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢ — çok ucuz, şüpheli")

        elif price < MAX_PRICE:
            cost          = round(SHARES * price, 2)   # toplam maliyet ($)
            potential_win = round(SHARES - cost, 2)    # net kazanç ($) eğer WIN
            cheap_tag     = "💰" if price < 0.20 else "←"
            mode_tag      = f" [ens %{mode_pct}]" if mode_pct else ""

            trade = {
                "id":            f"{station}_{tomorrow}_{datetime.now().strftime('%H%M%S')}",
                "station":       station,
                "date":          tomorrow,
                "blend":         round(blend, 1),
                "spread":        round(spread, 2),
                "uncertainty":   unc,
                "top_pick":      top_pick,
                "ens_mode_pct":  mode_pct,
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
            trades.append(trade)
            new_trades += 1

            print(
                f"  ✅ {station.upper()} {label}  "
                f"🎯{top_pick}°C (blend={blend:.1f}){mode_tag} @ {pct}¢ {cheap_tag}  "
                f"{SHARES} share · risk=${cost:.2f} · pot +${potential_win:.2f}"
            )

        else:
            print(f"  ⬜ {station.upper()} {label}  🎯{top_pick}°C @ {pct}¢ — pahalı, pas")

    save_trades(trades)

    open_count = len([t for t in trades if t["status"] == "open"])
    print(f"\n  📝 {new_trades} yeni trade kaydedildi")
    print(f"  📂 Toplam açık pozisyon: {open_count}")
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
            r = httpx.get(
                f"{WEATHER_API}/api/metar-history?station={station}", timeout=30
            )
            r.raise_for_status()
            history    = r.json()
            daily      = history.get("daily_maxes", [])
            day_record = next((d for d in daily if d["date"] == yesterday), None)

            if not day_record:
                print(f"  ⏳ {station.upper()} {label}  — gerçek veri henüz yok (settlement bekle)")
                continue

            actual = round(day_record["max_temp"])   # WU tam °C yuvarlaması
            won    = bucket_won(trade["bucket_title"], actual)

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

    print(f"\n  Açık pozisyon : {len(open_t)}")
    print(f"  Toplam kapalı : {len(closed)}  ({len(wins)} kazanç / {len(losses)} kayıp)")
    print(f"  İsabet oranı  : {wr:.1f}%")
    print(f"  Net P&L       : ${'+'if total_pnl>=0 else ''}{total_pnl:.2f}")
    print(f"  Toplam risk   : ${total_cost:.2f}  →  ROI: {'+'if roi>=0 else ''}{roi:.1f}%")

    # İstasyon bazlı özet
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
            print(f"  {emoji} {s.upper()} {label}  "
                  f"{len(s_wins)}/{len(s_trades)} kazanıldı ({s_wr:.0f}%)  "
                  f"P&L: ${'+'if s_pnl>=0 else ''}{s_pnl:.0f}")

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
