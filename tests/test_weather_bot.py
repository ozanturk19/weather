#!/usr/bin/env python3
"""
Weather Bot + BTC Bot Kapsamlı Test Süiti
Çalıştır: python3 test_weather_bot.py

Bağımlılık gerektirmez — tüm testler mock/unit düzeyinde.
"""

import json
import re
import sys
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

PASS = 0
FAIL = 0

def test(name: str, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except AssertionError as e:
        print(f"  ❌ {name}")
        print(f"     → {e}")
        FAIL += 1
    except Exception as e:
        print(f"  💥 {name} [EXCEPTION]")
        print(f"     → {type(e).__name__}: {e}")
        FAIL += 1

def eq(a, b, msg=""):
    assert a == b, f"{msg} | beklenen={b!r}, gerçek={a!r}"

def ok(cond, msg=""):
    assert cond, msg

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 1: bucket_won() — Polymarket Bucket Eşleştirme")
print("══════════════════════════════════════════════════════════════")

def bucket_won(title: str, actual: float):
    """trader.py'den kopyalandı — fix uygulandı"""
    t = title.strip()
    higher_m = re.search(r'(-?\d+).*or higher', t, re.I)
    below_m  = re.search(r'(-?\d+).*or below',  t, re.I)
    range_m  = re.search(r'(-?\d+)\D+(-?\d+)', t)
    exact_m  = re.match(r'^(-?\d+)\s*°?C?$', t)
    if higher_m: return actual >= int(higher_m.group(1))
    if below_m:  return actual <= int(below_m.group(1))
    if exact_m:  return round(actual) == int(exact_m.group(1))
    if range_m:  return int(range_m.group(1)) <= actual <= int(range_m.group(2))
    return None

test("bucket_won: exact match tam isabet", lambda: (
    eq(bucket_won("19°C", 19.0), True),
    eq(bucket_won("19°C", 18.0), False),
    # Python 3 banker's rounding: round(18.5)=18, round(19.5)=20
    eq(bucket_won("19°C", 18.6), True),    # round(18.6) == 19
    eq(bucket_won("19°C", 19.4), True),    # round(19.4) == 19
))

test("bucket_won: exact match formatsız", lambda: (
    eq(bucket_won("22", 22.0), True),
    eq(bucket_won("22", 23.0), False),
))

test("bucket_won: or higher", lambda: (
    eq(bucket_won("25°C or higher", 25.0), True),
    eq(bucket_won("25°C or higher", 30.0), True),
    eq(bucket_won("25°C or higher", 24.9), False),
))

test("bucket_won: or below", lambda: (
    eq(bucket_won("5°C or below", 5.0), True),
    eq(bucket_won("5°C or below", 0.0), True),
    eq(bucket_won("5°C or below", 5.1), False),
))

test("bucket_won: range 'X to Y' format (FİX)", lambda: (
    eq(bucket_won("14°C to 16°C", 15.0), True),
    eq(bucket_won("14°C to 16°C", 14.0), True),
    eq(bucket_won("14°C to 16°C", 16.0), True),
    eq(bucket_won("14°C to 16°C", 17.0), False),
    eq(bucket_won("14°C to 16°C", 13.0), False),
))

test("bucket_won: range 'X-Y' format (FİX)", lambda: (
    eq(bucket_won("14-16", 15.0), True),
    eq(bucket_won("14-16", 14.0), True),
    eq(bucket_won("14-16", 17.0), False),
))

test("bucket_won: negatif sıcaklıklar", lambda: (
    eq(bucket_won("-5°C or below", -5.0), True),
    eq(bucket_won("-5°C or below", -6.0), True),
    eq(bucket_won("-5°C or below", -4.0), False),
    eq(bucket_won("-3°C to 0°C", -2.0), True),
    eq(bucket_won("-3°C to 0°C", 1.0), False),
))

test("bucket_won: bilinmeyen format None döner", lambda: (
    eq(bucket_won("Rainfall > 5mm", 10.0), None),
    eq(bucket_won("", 10.0), None),
))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 2: today_spend() — Günlük Harcama Hesabı")
print("══════════════════════════════════════════════════════════════")

def today_spend_mock(trades: list) -> float:
    """trader.py today_spend — mock trades listesi ile"""
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(
        t.get("cost_usdc", 0)
        for t in trades
        if t.get("placed_at", "")[:10] == today
        and t["status"] not in ("cancelled", "expired")
    )

def make_trade(status, cost, placed_today=True):
    d = datetime.now() if placed_today else datetime.now() - timedelta(days=1)
    return {"status": status, "cost_usdc": cost, "placed_at": d.isoformat()}

test("today_spend: sadece bugünkü pending/filled sayılır", lambda:
    eq(today_spend_mock([
        make_trade("pending_fill", 1.0),
        make_trade("filled", 2.0),
        make_trade("pending_fill", 0.5, placed_today=False),  # dün
    ]), 3.0)
)

test("today_spend: cancelled sayılmaz", lambda:
    eq(today_spend_mock([
        make_trade("pending_fill", 1.0),
        make_trade("cancelled", 5.0),
    ]), 1.0)
)

test("today_spend: expired sayılmaz (FİX)", lambda:
    eq(today_spend_mock([
        make_trade("pending_fill", 1.0),
        make_trade("expired", 5.0),
    ]), 1.0)
)

test("today_spend: settled_win/loss sayılır (harcandı)", lambda:
    eq(today_spend_mock([
        make_trade("settled_win", 1.0),
        make_trade("settled_loss", 2.0),
    ]), 3.0)
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 3: D+1 / D+2 Horizon Mantığı")
print("══════════════════════════════════════════════════════════════")

def get_horizon(order_date: str) -> str:
    now      = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return "D+1" if order_date == tomorrow else "D+2"

def get_expiry_hours(order_date: str) -> int:
    ORDER_EXPIRY_D1_HOURS = 5
    ORDER_EXPIRY_D2_HOURS = 20
    now      = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return ORDER_EXPIRY_D1_HOURS if order_date == tomorrow else ORDER_EXPIRY_D2_HOURS

tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
day2_str     = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

test("D+1 horizon yarın için doğru", lambda:
    eq(get_horizon(tomorrow_str), "D+1")
)
test("D+2 horizon öbür gün için doğru", lambda:
    eq(get_horizon(day2_str), "D+2")
)
test("D+1 expiry 5 saat", lambda:
    eq(get_expiry_hours(tomorrow_str), 5)
)
test("D+2 expiry 20 saat", lambda:
    eq(get_expiry_hours(day2_str), 20)
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 4: P&L Hesabı — settle_live()")
print("══════════════════════════════════════════════════════════════")

def calc_pnl(shares, fill_price, limit_price, won: bool, cost_usdc: float):
    fp = fill_price or limit_price
    if won:
        return round(shares * (1.0 - fp), 2)
    else:
        return round(-cost_usdc, 2)

test("WIN P&L: 5 share @ 0.20 fill → +$4.00", lambda:
    eq(calc_pnl(5, 0.20, 0.20, True, 1.00), 4.00)
)
test("WIN P&L: 5 share @ 0.30 fill → +$3.50", lambda:
    eq(calc_pnl(5, 0.30, 0.30, True, 1.50), 3.50)
)
test("LOSS P&L: 5 share @ 0.20 → -$1.00", lambda:
    eq(calc_pnl(5, 0.20, 0.20, False, 1.00), -1.00)
)
test("WIN P&L fill_price None → limit_price kullanılır", lambda:
    eq(calc_pnl(5, None, 0.25, True, 1.25), 3.75)
)
test("WIN P&L köşe: fill_price=0.01 → +$4.95", lambda:
    eq(calc_pnl(5, 0.01, 0.01, True, 0.05), 4.95)
)
test("WIN P&L köşe: fill_price=0.39 → +$3.05", lambda:
    eq(calc_pnl(5, 0.39, 0.39, True, 1.95), 3.05)
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 5: Atomic File Write — save_live_trades()")
print("══════════════════════════════════════════════════════════════")

def test_atomic_write():
    with tempfile.TemporaryDirectory() as tmpdir:
        trades_file = Path(tmpdir) / "live_trades.json"

        def save_atomic(trades):
            tmp = trades_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(trades_file)

        # İlk yazma
        save_atomic([{"id": "t1", "status": "pending_fill"}])
        ok(trades_file.exists(), "dosya oluşturulmalı")
        eq(json.loads(trades_file.read_text()),[{"id": "t1", "status": "pending_fill"}])

        # Atomik: .tmp dosyası kalmamış olmalı
        tmp = trades_file.with_suffix(".tmp")
        ok(not tmp.exists(), ".tmp dosyası temizlenmeli")

        # Güncelleme
        save_atomic([{"id": "t1", "status": "filled"}])
        eq(json.loads(trades_file.read_text())[0]["status"], "filled")

test("save_live_trades atomic write", test_atomic_write)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 6: check_fills() — Dolum Durumu Güncellemesi")
print("══════════════════════════════════════════════════════════════")

def simulate_check_fills(trades: list, clob_responses: dict) -> tuple[list, int]:
    """check_fills mantığını simüle et"""
    pending = [t for t in trades if t["status"] == "pending_fill"]
    updated = 0
    for t in pending:
        resp    = clob_responses.get(t["order_id"], {})
        status  = (resp.get("status") or "").upper()
        matched = float(resp.get("size_matched") or 0)
        size    = float(resp.get("original_size") or t["shares"])

        if status == "MATCHED" or matched >= size:
            t["status"]     = "filled"
            t["fill_price"] = float(resp.get("price") or t["limit_price"])
            t["fill_time"]  = datetime.now().isoformat()
            updated += 1
        elif status in ("CANCELLED", "CANCELED"):
            t["status"] = "cancelled"
            updated += 1
    return trades, updated

test("check_fills: MATCHED → filled", lambda: (
    (lambda r: (
        eq(r[0][0]["status"], "filled"),
        eq(r[1], 1),
    ))(simulate_check_fills(
        [{"order_id": "oid1", "status": "pending_fill", "shares": 5, "limit_price": 0.20}],
        {"oid1": {"status": "MATCHED", "size_matched": "5", "price": "0.20", "original_size": "5"}}
    ))
))

test("check_fills: size_matched >= size → filled", lambda: (
    (lambda r: eq(r[0][0]["status"], "filled"))(simulate_check_fills(
        [{"order_id": "oid1", "status": "pending_fill", "shares": 5, "limit_price": 0.20}],
        {"oid1": {"status": "OPEN", "size_matched": "5.0", "original_size": "5", "price": "0.21"}}
    ))
))

test("check_fills: CANCELLED → cancelled", lambda: (
    (lambda r: eq(r[0][0]["status"], "cancelled"))(simulate_check_fills(
        [{"order_id": "oid1", "status": "pending_fill", "shares": 5, "limit_price": 0.20}],
        {"oid1": {"status": "CANCELLED", "size_matched": "0"}}
    ))
))

test("check_fills: kısmi dolum → pending_fill kaldı", lambda: (
    (lambda r: (
        eq(r[0][0]["status"], "pending_fill"),
        eq(r[1], 0),
    ))(simulate_check_fills(
        [{"order_id": "oid1", "status": "pending_fill", "shares": 5, "limit_price": 0.20}],
        {"oid1": {"status": "OPEN", "size_matched": "2.5", "original_size": "5"}}
    ))
))

test("check_fills: fill_price önceki limit'ten alınır (API'de yok)", lambda: (
    (lambda r: eq(r[0][0]["fill_price"], 0.20))(simulate_check_fills(
        [{"order_id": "oid1", "status": "pending_fill", "shares": 5, "limit_price": 0.20}],
        {"oid1": {"status": "MATCHED", "size_matched": "5", "original_size": "5"}}  # price yok
    ))
))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 7: Risk Kontrolleri — place_limit_order()")
print("══════════════════════════════════════════════════════════════")

def risk_check(price, shares, live_trades, balance,
               max_open=30, max_daily=60.0, min_reserve=10.0,
               min_price=0.05, max_price=0.40):
    """Gerçek risk kontrol mantığını simüle et. None = geç, str = engel sebebi"""
    open_live = [t for t in live_trades if t["status"] == "pending_fill"]
    if len(open_live) >= max_open:
        return "MAX_OPEN_REACHED"

    today = datetime.now().strftime("%Y-%m-%d")
    spent = sum(t.get("cost_usdc", 0)
                for t in live_trades
                if t.get("placed_at", "")[:10] == today
                and t["status"] not in ("cancelled", "expired"))
    cost  = round(shares * price, 2)
    if spent + cost > max_daily:
        return "DAILY_LIMIT"

    if price < min_price or price > max_price:
        return "PRICE_OUT_OF_RANGE"

    if balance < min_reserve + cost:
        return "INSUFFICIENT_BALANCE"

    return None

test("risk: normal emir geçer", lambda:
    eq(risk_check(0.20, 5, [], 50.0), None)
)
test("risk: max_open doldu", lambda:
    eq(risk_check(0.20, 5,
        [{"status": "pending_fill", "cost_usdc": 1} for _ in range(30)],
        50.0), "MAX_OPEN_REACHED")
)
test("risk: günlük limit aşıldı", lambda:
    eq(risk_check(0.20, 5,
        [{"status": "pending_fill", "cost_usdc": 60.0,
          "placed_at": datetime.now().isoformat()}],
        50.0), "DAILY_LIMIT")
)
test("risk: fiyat çok ucuz (< 0.05)", lambda:
    eq(risk_check(0.04, 5, [], 50.0), "PRICE_OUT_OF_RANGE")
)
test("risk: fiyat çok pahalı (> 0.40)", lambda:
    eq(risk_check(0.41, 5, [], 50.0), "PRICE_OUT_OF_RANGE")
)
test("risk: bakiye tam yeterli (sınırda = geçer)", lambda:
    eq(risk_check(0.20, 5, [], 11.0), None)  # 11.0 < 10.0+1.0 → False → geçer
)
test("risk: bakiye yetersiz (1 kuruş eksik)", lambda:
    eq(risk_check(0.20, 5, [], 10.99), "INSUFFICIENT_BALANCE")  # 10.99 < 11.0
)
test("risk: bakiye sadece min_reserve kadar → yetersiz", lambda:
    eq(risk_check(0.20, 5, [], 10.0), "INSUFFICIENT_BALANCE")  # 10.0 < 10.0+1.0=11.0
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 8: cancel_stale_orders() — D+1 Re-entry (FİX)")
print("══════════════════════════════════════════════════════════════")

def simulate_cancel_stale(trades: list) -> tuple[int, int]:
    """
    D+1 re-entry fix simülasyonu.
    Düzeltme: save_before_reentry = True → disk'e yaz, sonra yer açılmış olsun.
    """
    now      = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    pending  = [t for t in trades if t["status"] == "pending_fill"]
    cancelled = 0
    requeued  = 0

    for t in pending:
        try:
            exp = datetime.fromisoformat(t["expires_at"])
        except Exception:
            continue
        if now < exp:
            continue

        horizon = t.get("horizon", "D+2")
        t["status"] = "cancelled"
        cancelled += 1

        if horizon == "D+1" and t["date"] == tomorrow:
            # FİX: save → reload → duplicate check artık cancelled görür
            # Simüle: disk'e kaydedip check yapalım
            already = any(
                x["station"] == t["station"] and x["date"] == t["date"]
                and x["top_pick"] == t["top_pick"] and x["status"] == "pending_fill"
                for x in trades  # trades'de artık cancelled
            )
            if not already:
                requeued += 1

    return cancelled, requeued

past_exp = (datetime.now() - timedelta(hours=1)).isoformat()
future_exp = (datetime.now() + timedelta(hours=1)).isoformat()

test("cancel_stale: D+2 sadece iptal edilir, yeniden girilmez", lambda: (
    (lambda r: (eq(r[0], 1), eq(r[1], 0)))(simulate_cancel_stale([{
        "station": "eglc", "date": tomorrow_str, "top_pick": 20,
        "status": "pending_fill", "horizon": "D+2",
        "expires_at": past_exp
    }]))
))

test("cancel_stale: D+1 iptal + yeniden giriş yapılır", lambda: (
    (lambda r: (eq(r[0], 1), eq(r[1], 1)))(simulate_cancel_stale([{
        "station": "eglc", "date": tomorrow_str, "top_pick": 20,
        "status": "pending_fill", "horizon": "D+1",
        "expires_at": past_exp
    }]))
))

test("cancel_stale: süresi dolmamış → dokunma", lambda: (
    (lambda r: (eq(r[0], 0), eq(r[1], 0)))(simulate_cancel_stale([{
        "station": "eglc", "date": tomorrow_str, "top_pick": 20,
        "status": "pending_fill", "horizon": "D+1",
        "expires_at": future_exp
    }]))
))

test("cancel_stale: filled trade dokunulmaz", lambda: (
    (lambda r: eq(r[0], 0))(simulate_cancel_stale([{
        "station": "eglc", "date": tomorrow_str, "top_pick": 20,
        "status": "filled", "horizon": "D+1",
        "expires_at": past_exp
    }]))
))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 9: BTC Bot — parseBalanceFromError()")
print("══════════════════════════════════════════════════════════════")

def parseBalanceFromError(errMsg: str):
    """scalp_live.ts'den Python'a çevrildi"""
    m = re.search(r'balance:\s*(\d+)', errMsg)
    if not m: return None
    raw = int(m.group(1))
    if raw <= 0: return None
    return int((raw / 1e6) * 100) / 100  # floor 2 decimal

test("parseBalanceFromError: normal hata mesajı", lambda:
    eq(parseBalanceFromError("the balance is not enough -> balance: 4974800, order amount: 5000000"),
       4.97)
)
test("parseBalanceFromError: sıfır balance", lambda:
    eq(parseBalanceFromError("balance: 0, order amount: 5000000"), None)
)
test("parseBalanceFromError: hata mesajında balance yok", lambda:
    eq(parseBalanceFromError("some other error message"), None)
)
test("parseBalanceFromError: büyük balance", lambda:
    eq(parseBalanceFromError("balance: 10000000"), 10.0)
)
test("parseBalanceFromError: kesirli hesap", lambda:
    eq(parseBalanceFromError("balance: 5969760"), 5.96)  # floor değil, math.floor
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 10: BTC Bot — Stop Loss Mantığı")
print("══════════════════════════════════════════════════════════════")

ENTRY_MIN = 0.91
ENTRY_MAX = 0.93
STOP_DIST = 0.06

def round_tick(price: float) -> float:
    return round(price * 100) / 100

def should_enter(ask: float) -> bool:
    return ENTRY_MIN <= ask <= ENTRY_MAX

def calc_stop_price(entry: float) -> float:
    return round_tick(entry - STOP_DIST)

def stop_attempts(mid: float) -> list:
    return [
        round_tick(max(mid - 0.01, 0.02)),
        round_tick(max(mid - 0.03, 0.02)),
        round_tick(max(mid - 0.06, 0.02)),
        round_tick(max(mid - 0.10, 0.02)),
    ]

test("entry: ask=0.92 → giriş yapılır", lambda:
    ok(should_enter(0.92))
)
test("entry: ask=0.90 → pas (çok ucuz)", lambda:
    ok(not should_enter(0.90))
)
test("entry: ask=0.94 → pas (çok pahalı)", lambda:
    ok(not should_enter(0.94))
)
test("stop_price: entry=0.92 → stop=0.86", lambda:
    eq(calc_stop_price(0.92), 0.86)
)
test("stop_price: entry=0.93 → stop=0.87", lambda:
    eq(calc_stop_price(0.93), 0.87)
)
test("stop cascade: mid=0.80 → kademeli fiyatlar doğru", lambda: (
    (lambda a: (
        eq(a[0], 0.79),
        eq(a[1], 0.77),
        eq(a[2], 0.74),
        eq(a[3], 0.70),
    ))(stop_attempts(0.80))
))
test("stop cascade: mid çok düşük → 0.02 alt sınır", lambda: (
    (lambda a: ok(all(p >= 0.02 for p in a)))(stop_attempts(0.03))
))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 11: BTC Bot — MIN_HOLD_BEFORE_STOP Mantığı")
print("══════════════════════════════════════════════════════════════")

MIN_HOLD   = 60
CRASH_DIST = 0.10

def should_stop(mid, stop_price, hold_time, remaining):
    if mid > stop_price:
        return False, "above_stop"
    if hold_time >= MIN_HOLD:
        return True, "normal_stop"
    crash_diff = stop_price - mid
    if crash_diff > CRASH_DIST:
        return True, "crash_bypass"
    if remaining < 30:
        return True, "market_closing"
    return False, "fake_stop_blocked"

test("stop: mid > stop_price → tetiklenmez", lambda:
    eq(should_stop(0.90, 0.86, 100, 100)[0], False)
)
test("stop: normal (60s hold geçti)", lambda:
    eq(should_stop(0.85, 0.86, 61, 100)[1], "normal_stop")
)
test("stop: erken ama küçük dip → engellendi (fake stop)", lambda:
    eq(should_stop(0.84, 0.86, 30, 100)[1], "fake_stop_blocked")
)
test("stop: erken ama CRASH (0.10+ diff) → geç", lambda:
    eq(should_stop(0.75, 0.86, 30, 100)[1], "crash_bypass")
)
test("stop: erken ama market kapanıyor (remaining<30)", lambda:
    eq(should_stop(0.84, 0.86, 30, 20)[1], "market_closing")
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 12: JSON Dosya Bozulma Dayanıklılığı")
print("══════════════════════════════════════════════════════════════")

def load_safe(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
    return []

def test_corrupt_json():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "trades.json"

        # Sıfır dosya
        result = load_safe(f)
        eq(result, [], "dosya yoksa [] döner")

        # Bozuk JSON
        f.write_text("{ bozuk json !!!", encoding="utf-8")
        result = load_safe(f)
        eq(result, [], "bozuk JSON'da [] döner")

        # Kısmi yazım (crash simülasyonu)
        f.write_text('[{"id": "t1",', encoding="utf-8")
        result = load_safe(f)
        eq(result, [], "kısmi JSON'da [] döner")

        # Geçerli dosya
        f.write_text(json.dumps([{"id": "t1"}]), encoding="utf-8")
        result = load_safe(f)
        eq(len(result), 1, "geçerli JSON okunur")

test("JSON bozulma dayanıklılığı", test_corrupt_json)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 13: Settle — Çok Günlük (Cron Atlama) (FİX)")
print("══════════════════════════════════════════════════════════════")

def get_settle_candidates(trades: list) -> list:
    """Yeni settle mantığı: sadece yesterday değil, today'den önce"""
    today = datetime.now().strftime("%Y-%m-%d")
    return [t for t in trades if t["date"] < today and t["status"] == "filled"]

test("settle: sadece dünü değil tüm eskiyi alır (FİX)", lambda: (
    (lambda r: eq(len(r), 2))(get_settle_candidates([
        {"date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), "status": "filled"},
        {"date": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"), "status": "filled"},
        {"date": datetime.now().strftime("%Y-%m-%d"), "status": "filled"},  # bugün → hayır
        {"date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), "status": "pending_fill"},  # filled değil → hayır
    ]))
))

test("settle: bugünkü filled trade settle edilmez (settlement sabah)", lambda: (
    (lambda r: eq(len(r), 0))(get_settle_candidates([
        {"date": datetime.now().strftime("%Y-%m-%d"), "status": "filled"},
    ]))
))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 14: Güvenlik — API Token Kontrolü")
print("══════════════════════════════════════════════════════════════")

def check_token(api_token_env: str, request_token: str) -> bool:
    """require_token mantığı: token boşsa serbest, değilse eşit olmalı"""
    if not api_token_env:
        return True  # token ayarlanmamışsa açık
    return request_token == api_token_env

test("güvenlik: token boşsa her şeyi geçirir", lambda:
    ok(check_token("", ""))
)
test("güvenlik: token doğru → geçer", lambda:
    ok(check_token("secret123", "secret123"))
)
test("güvenlik: token yanlış → engeller", lambda:
    ok(not check_token("secret123", "yanlis"))
)
test("güvenlik: token boş request → engeller", lambda:
    ok(not check_token("secret123", ""))
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 15: Trader Kaynak Kod Doğrulaması")
print(" (Yaşanan bugların bir daha çıkmamasını garanti eder)")
print("══════════════════════════════════════════════════════════════")

TRADER_SRC = (Path(__file__).parent.parent / "bot" / "trader.py").read_text(encoding="utf-8")

def test_min_usdc_reserve():
    m = re.search(r'MIN_USDC_RESERVE\s*=\s*([\d.]+)', TRADER_SRC)
    ok(m, "MIN_USDC_RESERVE tanımı bulunamadı")
    val = float(m.group(1))
    # Reserve + max maliyet (5 share × 0.40¢ = $2) < tipik düşük bakiye ($7)
    max_cost = 5 * 0.40
    ok(val + max_cost < 7.5,
       f"MIN_USDC_RESERVE={val} — $7 bakiyeyle maks maliyet ${max_cost:.2f} trade geçemez "
       f"(reserve+maliyet=${val+max_cost:.2f} ≥ $7.50)")

test("MIN_USDC_RESERVE: $7 bakiyeyle en pahalı emir geçebilmeli", test_min_usdc_reserve)

def test_clob_balance_uses_params_object():
    ok("BalanceAllowanceParams" in TRADER_SRC,
       "get_balance(): BalanceAllowanceParams import edilmiyor — CLOB çağrısı hata verir")
    # Ham dict geçilmiyor mu? ('dict' has no attribute 'signature_type' hatası)
    bad_patterns = ['params={"asset_type"', "params={'asset_type'"]
    for p in bad_patterns:
        ok(p not in TRADER_SRC,
           f"get_balance_allowance'a ham dict geçiliyor: '{p}' — signature_type hatası çıkar")

test("get_balance(): CLOB çağrısı BalanceAllowanceParams kullanıyor (dict değil)", test_clob_balance_uses_params_object)

def test_polygon_rpcs_no_dead_endpoints():
    m = re.search(r'POLYGON_RPCS\s*=\s*\[([^\]]+)\]', TRADER_SRC, re.DOTALL)
    ok(m, "POLYGON_RPCS listesi bulunamadı")
    rpc_block = m.group(1)
    dead = [
        ("rpc.ankr.com/polygon", "ankr API key gerektiriyor"),
        ("1rpc.io/matic",         "1rpc SSL hatası veriyor"),
    ]
    for endpoint, reason in dead:
        ok(endpoint not in rpc_block,
           f"POLYGON_RPCS dead endpoint içeriyor: {endpoint} ({reason})")

test("POLYGON_RPCS: bilinen dead endpoint'ler yok (ankr / 1rpc)", test_polygon_rpcs_no_dead_endpoints)

def test_polygon_rpcs_has_working_endpoints():
    m = re.search(r'POLYGON_RPCS\s*=\s*\[([^\]]+)\]', TRADER_SRC, re.DOTALL)
    ok(m, "POLYGON_RPCS listesi bulunamadı")
    rpc_block = m.group(1)
    working = ["quiknode", "drpc.org", "publicnode.com"]
    ok(any(w in rpc_block for w in working),
       f"POLYGON_RPCS hiçbir çalışan endpoint içermiyor (beklenen: {working})")

test("POLYGON_RPCS: çalışan endpoint var (quiknode / drpc / publicnode)", test_polygon_rpcs_has_working_endpoints)

def test_get_w3_uses_polygon_rpcs():
    func_m = re.search(r'def _get_w3\(\).*?(?=\ndef |\Z)', TRADER_SRC, re.DOTALL)
    ok(func_m, "_get_w3() fonksiyonu bulunamadı")
    body = func_m.group(0)
    ok("POLYGON_RPCS" in body, "_get_w3() POLYGON_RPCS listesini kullanmıyor")
    ok("for " in body,         "_get_w3() döngüyle deneme yapmıyor — tek endpoint başarısız olunca çöker")

test("_get_w3(): POLYGON_RPCS listesini döngüyle deniyor (fallback zinciri)", test_get_w3_uses_polygon_rpcs)

def test_web3_import_in_redeem():
    # cmd_redeem veya _get_w3 içinde web3 import var mı?
    ok("from web3 import" in TRADER_SRC or "import web3" in TRADER_SRC,
       "web3 import yok — redeem çalışmaz")
    # web3 ModuleNotFoundError üretiyorsa redeem sessizce başarısız olmaz mı?
    # _get_w3 try/except mi sarıyor?
    func_m = re.search(r'def _get_w3\(\).*?(?=\ndef |\Z)', TRADER_SRC, re.DOTALL)
    ok(func_m, "_get_w3() bulunamadı")
    body = func_m.group(0)
    ok("RuntimeError" in body or "raise" in body,
       "_get_w3() bağlantı yoksa exception fırlatmıyor — cmd_redeem sessizce atlayabilir")

test("web3 import ve _get_w3() hata yönetimi doğru", test_web3_import_in_redeem)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 16: Bakiye + Reserve Mantığı")
print("══════════════════════════════════════════════════════════════")

def can_trade(balance: float, min_reserve: float, cost: float) -> bool:
    return balance >= min_reserve + cost

# — Eski bug: MIN_USDC_RESERVE=$10 her şeyi blokluyordu —
test("BUG (eski $10 reserve): $7.13 + $1.35 maliyet → ENGELLENİRDİ", lambda:
    ok(not can_trade(7.13, 10.0, 1.35),
       "Bu test eski bugı belgeliyor — $10 reserve $7.13 bakiyeyi blokluyordu")
)

# — Mevcut $5 reserve ile beklenen davranış —
test("$7.13 bakiye, $5 reserve, $1.35 maliyet (LTFM 27¢) → GEÇER", lambda:
    ok(can_trade(7.13, 5.0, 1.35))
)
test("$7.13 bakiye, $5 reserve, $1.82 maliyet (EDDM 36¢) → GEÇER", lambda:
    ok(can_trade(7.13, 5.0, 1.82))
)
test("$7.13 bakiye, $5 reserve, $2.13 maliyet (sınır) → GEÇER", lambda:
    ok(can_trade(7.13, 5.0, 2.13))
)
test("$7.13 bakiye, $5 reserve, $2.14 maliyet (1 kuruş fazla) → ENGELLENİR", lambda:
    ok(not can_trade(7.13, 5.0, 2.14))
)
test("tam reserve kadar bakiye → her zaman engellenir (cost > 0)", lambda:
    ok(not can_trade(5.0, 5.0, 0.01))
)
test("$0 bakiye → engellenir", lambda:
    ok(not can_trade(0.0, 5.0, 1.0))
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 17: Scanner retry_live Mantığı")
print(" (paper var ama live order eksik → yeniden dene)")
print("══════════════════════════════════════════════════════════════")

def scanner_dedup_check(station, target_date, top_pick, trades, live_trades, live_mode):
    """scanner.py scan_date() dedup + retry_live mantığını simüle et."""
    already_same = any(
        t["station"] == station and t["date"] == target_date
        and t["top_pick"] == top_pick and t["status"] == "open"
        for t in trades
    )
    if not already_same:
        return "new"

    if live_mode:
        has_live = any(
            t["station"] == station and t["date"] == target_date
            and t["status"] in ("pending_fill", "filled", "settled_win", "settled_loss")
            for t in live_trades
        )
        if not has_live:
            paper_match = next(
                (t for t in trades
                 if t["station"] == station and t["date"] == target_date
                 and t["top_pick"] == top_pick and t["status"] == "open"),
                None,
            )
            if paper_match:
                return ("retry_live", paper_match)

    return None  # skip

def make_paper(station, date, pick, status="open"):
    return {"station": station, "date": date, "top_pick": pick, "status": status}

def make_live(station, date, status):
    return {"station": station, "date": date, "status": status}

# — Ana bug: cron silinince py_clob_client eksikti, paper oluştu ama live order gitmedi.
# — Sonraki scan "already_same" görüp pas geçiyordu. retry_live bunu önler. —

test("retry_live: paper VAR, live YOK → ('retry_live', paper) döner", lambda: (
    (lambda r: (
        ok(isinstance(r, tuple),  "tuple dönmeli"),
        eq(r[0], "retry_live"),
        ok(r[1]["station"] == "eglc", "paper_match dönmeli"),
    ))(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15)],
        live_trades=[],
        live_mode=True,
    ))
))

test("retry_live: paper VAR, live PENDING_FILL var → None (skip)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15)],
        live_trades=[make_live("eglc", "2026-04-22", "pending_fill")],
        live_mode=True,
    ), None)
)

test("retry_live: paper VAR, live FILLED var → None (skip)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15)],
        live_trades=[make_live("eglc", "2026-04-22", "filled")],
        live_mode=True,
    ), None)
)

test("retry_live: paper VAR, live SETTLED_WIN var → None (skip)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15)],
        live_trades=[make_live("eglc", "2026-04-22", "settled_win")],
        live_mode=True,
    ), None)
)

test("retry_live: live_mode=False → None (paper scan, live deneme yok)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15)],
        live_trades=[],
        live_mode=False,
    ), None)
)

test("retry_live: paper YOK → 'new' sinyal (ilk kez görülüyor)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[],
        live_trades=[],
        live_mode=True,
    ), "new")
)

test("retry_live: farklı top_pick → 'new' (model güncelledi, eski paper sayılmaz)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 16,       # model şimdi 16°C diyor
        trades=[make_paper("eglc", "2026-04-22", 15)],   # paper 15°C
        live_trades=[],
        live_mode=True,
    ), "new")
)

test("retry_live: paper 'closed' durumda → 'new' (open değil, geçerli paper yok)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15, status="closed")],
        live_trades=[],
        live_mode=True,
    ), "new")
)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 18: Venv Bağımlılık Kontrolü (VPS'te çalışır)")
print("══════════════════════════════════════════════════════════════")

import subprocess as _subprocess

VENV_PY   = "/root/weather/venv/bin/python3"
ON_VPS    = Path(VENV_PY).exists()

if not ON_VPS:
    print("  ⏭️  Lokal Mac — venv testleri atlandı")

def venv_import_test(module: str):
    """Venv Python ile import test — sadece VPS'te çalışır."""
    if not ON_VPS:
        return   # skip locally, not a failure
    r = _subprocess.run(
        [VENV_PY, "-c", f"import {module}; print('OK')"],
        capture_output=True, text=True, timeout=20,
    )
    ok(r.returncode == 0 and "OK" in r.stdout,
       f"venv'de '{module}' import edilemiyor:\n     {r.stderr.strip()[:200]}\n"
       f"     → Çözüm: /root/weather/venv/bin/pip install {module.replace('_', '-')}")

test("venv: py_clob_client importable (CLOB order / balance)",
     lambda: venv_import_test("py_clob_client"))
test("venv: web3 importable (on-chain redeem)",
     lambda: venv_import_test("web3"))
test("venv: httpx importable (FastAPI / weather API çağrıları)",
     lambda: venv_import_test("httpx"))
test("venv: eth_account importable (cüzdan türetme / TX imzalama)",
     lambda: venv_import_test("eth_account"))
test("venv: dotenv importable (.env yükleme)",
     lambda: venv_import_test("dotenv"))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 19: Cron Tablosu Doğrulaması (VPS'te çalışır)")
print("══════════════════════════════════════════════════════════════")

def _get_crontab() -> str:
    if not ON_VPS:
        return ""
    r = _subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

CRONTAB = _get_crontab()

if not ON_VPS:
    print("  ⏭️  Lokal Mac — cron testleri atlandı")

REQUIRED_CRONS = [
    (r"0 4,10,16,22.*scanner.*scan.*--live",
     "Scanner scan  → 04:00/10:00/16:00/22:00"),
    (r"0 11.*scanner.*settle",
     "Scanner settle → 11:00"),
    (r"5 11.*trader.*settle",
     "Trader settle  → 11:05"),
    (r"15 11.*trader.*redeem",
     "Trader redeem  → 11:15  (kazanç claim)"),
    (r"\*/30.*trader.*check-fills",
     "Fill check     → her 30dk"),
    (r"0 4,8,12,16,20.*trader.*cancel-stale",
     "Cancel stale   → 04/08/12/16/20h"),
    (r"venv/bin/python3",
     "Tüm işler venv Python kullanıyor (py_clob_client erişimi için şart)"),
]

def make_cron_test(pattern: str, desc: str):
    def _t():
        if not ON_VPS:
            return
        ok(bool(re.search(pattern, CRONTAB)),
           f"Cron eksik veya yanlış: {desc}\n"
           f"     Beklenen pattern: {pattern}")
    return _t

for cron_pattern, cron_desc in REQUIRED_CRONS:
    test(f"cron: {cron_desc}", make_cron_test(cron_pattern, cron_desc))

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 20: On-chain Redeem Güvenliği")
print("══════════════════════════════════════════════════════════════")

def simulate_redeem(payout_denom: int, token_balance: float) -> str:
    """_redeem_ctf() + cmd_redeem() kritik dallarını simüle et."""
    # payoutDenominator=0 → oracle henüz raporlamamış, erken çağrıyı engelle
    if payout_denom == 0:
        raise ValueError("condition on-chain raporlanmamış (payout denom=0)")
    if token_balance <= 0:
        # TX gider ama $0 öder — Exchange contract tutuyordur (CLOB flow)
        return "tx_ok_zero_payout"
    return "tx_ok_with_payout"

def test_redeem_denom_zero():
    raised = False
    try:
        simulate_redeem(0, 5.0)
    except ValueError as e:
        raised = True
        ok("denom=0" in str(e) or "raporlanmamış" in str(e))
    ok(raised, "denom=0 için ValueError fırlatılmalı — erken çağrı engeli")

def test_redeem_denom_pos_no_tokens():
    r = simulate_redeem(1, 0.0)
    eq(r, "tx_ok_zero_payout",
       "Denom>0, token=0 → TX gönderilmeli ama payout $0 (Exchange contract tutuyordur)")

def test_redeem_denom_pos_with_tokens():
    r = simulate_redeem(1, 5.0)
    eq(r, "tx_ok_with_payout")

test("redeem: denom=0 → ValueError (oracle raporlamamış, erken çağrı engeli)",
     test_redeem_denom_zero)
test("redeem: denom>0, token=0 → TX gider ama $0 payout (Exchange tutuyor, normal)",
     test_redeem_denom_pos_no_tokens)
test("redeem: denom>0, token>0 → TX gider + payout alınır",
     test_redeem_denom_pos_with_tokens)

def test_cmd_redeem_handles_w3_failure():
    """cmd_redeem() _get_w3 başarısız olursa erken çıkmalı (crash yok)."""
    # cmd_redeem kodunda: try: w3 = _get_w3() except: print + return
    ok("_get_w3" in TRADER_SRC,         "_get_w3 çağrısı yok")
    ok("Web3 bağlantı hatası" in TRADER_SRC or "bağlantı" in TRADER_SRC,
       "cmd_redeem() RPC hatasını sessizce yutmamalı — kullanıcı bilgilendirilmeli")

test("cmd_redeem(): RPC bağlantı hatası → loglayıp çıkıyor (crash yok)",
     test_cmd_redeem_handles_w3_failure)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 21: get_balance() Fallback Zinciri")
print("══════════════════════════════════════════════════════════════")

def simulate_get_balance(clob_bal, rpc_bal):
    """get_balance() CLOB→RPC fallback mantığını simüle et."""
    # Birincil: CLOB API
    if clob_bal is not None:
        try:
            if clob_bal > 0:
                return clob_bal
        except Exception:
            pass

    # İkincil: on-chain RPC
    if rpc_bal is not None:
        try:
            if rpc_bal > 0:
                return rpc_bal
        except Exception:
            pass

    return 0.0

test("get_balance: CLOB başarılı → CLOB değeri döner (birincil kaynak)", lambda:
    eq(simulate_get_balance(22.13, 7.13), 22.13)
)
test("get_balance: CLOB None (exception) → RPC değeri döner", lambda:
    eq(simulate_get_balance(None, 7.13), 7.13)
)
test("get_balance: CLOB $0 (iç bakiye boş) → RPC değeri döner (on-chain cüzdan)", lambda:
    eq(simulate_get_balance(0.0, 7.13), 7.13)
)
test("get_balance: her ikisi de None → 0.0 döner (panic/crash yok)", lambda:
    eq(simulate_get_balance(None, None), 0.0)
)
test("get_balance: her ikisi de 0 → 0.0 döner", lambda:
    eq(simulate_get_balance(0.0, 0.0), 0.0)
)

def test_get_balance_has_two_sources():
    """get_balance() kaynak kodunda hem CLOB hem RPC olmalı."""
    ok("BalanceAllowanceParams" in TRADER_SRC,   "CLOB birincil kaynak eksik")
    ok("eth_call" in TRADER_SRC or "70a08231" in TRADER_SRC,
       "RPC yedek kaynak eksik (eth_call / balanceOf)")
    ok("rpc-mainnet.matic.quiknode.pro" in TRADER_SRC or "drpc.org" in TRADER_SRC
       or "publicnode.com" in TRADER_SRC,
       "get_balance RPC fallback'te çalışan endpoint yok")

test("get_balance(): kaynak kodda CLOB + RPC iki katmanlı fallback mevcut",
     test_get_balance_has_two_sources)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 22: Open-Meteo Settlement (METAR → Open-Meteo Primary)")
print("══════════════════════════════════════════════════════════════")

SCANNER_SRC_PATH = Path(__file__).parent.parent / "bot" / "scanner.py"
SCANNER_SRC = SCANNER_SRC_PATH.read_text(encoding="utf-8") if SCANNER_SRC_PATH.exists() else ""

def test_openmeteo_in_scanner_source():
    """scanner.py settle() Open-Meteo birincil kaynak kullanıyor mu?"""
    ok("archive-api.open-meteo.com" in SCANNER_SRC,
       "scanner.py settle() Open-Meteo API endpoint içermiyor")
    ok("temperature_2m_max" in SCANNER_SRC,
       "scanner.py settle() temperature_2m_max parametresi yok")
    ok("get_actual_temp_open_meteo" in SCANNER_SRC,
       "scanner.py get_actual_temp_open_meteo() fonksiyonu yok")
    ok("STATION_COORDS" in SCANNER_SRC,
       "scanner.py STATION_COORDS dict yok")
    # METAR hâlâ yedek olarak kalmalı
    ok("metar-history" in SCANNER_SRC,
       "scanner.py METAR yedek kaynak kaldırılmış — yedek kalmalı")

def test_openmeteo_in_trader_source():
    """trader.py settle_live() Open-Meteo birincil kaynak kullanıyor mu?"""
    ok("archive-api.open-meteo.com" in TRADER_SRC,
       "trader.py settle_live() Open-Meteo API endpoint içermiyor")
    ok("temperature_2m_max" in TRADER_SRC,
       "trader.py settle_live() temperature_2m_max parametresi yok")
    ok("get_actual_temp_open_meteo" in TRADER_SRC,
       "trader.py get_actual_temp_open_meteo() fonksiyonu yok")
    ok("STATION_COORDS" in TRADER_SRC,
       "trader.py STATION_COORDS dict yok")
    ok("metar-history" in TRADER_SRC,
       "trader.py METAR yedek kaynak kaldırılmış — yedek kalmalı")

def simulate_open_meteo_settle(om_temp, metar_temp):
    """get_actual_temp_open_meteo + METAR fallback mantığını simüle et."""
    actual = None
    # Open-Meteo birincil
    if om_temp is not None:
        actual = round(om_temp)
    # METAR yedek
    if actual is None and metar_temp is not None:
        actual = round(metar_temp)
    return actual

test("Open-Meteo kaynak kodu: scanner.py'de birincil kaynak",
     test_openmeteo_in_scanner_source)
test("Open-Meteo kaynak kodu: trader.py'de birincil kaynak",
     test_openmeteo_in_trader_source)
test("Open-Meteo settle: Open-Meteo veri varsa kullan, METAR'ı atla", lambda:
    eq(simulate_open_meteo_settle(14.7, 13.0), 15,
       "Open-Meteo 14.7 → round = 15 (METAR'ın 13.0'ı gözardı edilmeli)")
)
test("Open-Meteo settle: Open-Meteo None → METAR yedek devreye girer", lambda:
    eq(simulate_open_meteo_settle(None, 16.3), 16,
       "Open-Meteo None ise METAR yedek 16.3 → 16")
)
test("Open-Meteo settle: ikisi de None → None (settlement bekle)", lambda:
    eq(simulate_open_meteo_settle(None, None), None,
       "Her iki kaynak da None ise settlement yapılmamalı")
)
test("Open-Meteo settle: sınır değer yuvarlama 15.5 → 16", lambda:
    eq(simulate_open_meteo_settle(15.5, 14.0), 16)
)
test("Open-Meteo settle: negatif sıcaklık doğru yuvarlanır (-0.4 → 0)", lambda:
    eq(simulate_open_meteo_settle(-0.4, None), 0)
)
test("Open-Meteo settle: negatif sıcaklık doğru yuvarlanır (-1.6 → -2)", lambda:
    eq(simulate_open_meteo_settle(-1.6, None), -2)
)

def test_station_coords_coverage():
    """STATION_COORDS tüm önemli istasyonları kapsıyor mu?"""
    required = ["eglc", "ltfm", "lemd", "lfpg", "limc", "ltac",
                "eham", "eddm", "epwa", "efhk", "omdb", "rjtt"]
    for station in required:
        ok(f'"{station}"' in SCANNER_SRC or f"'{station}'" in SCANNER_SRC,
           f"STATION_COORDS'da {station} yok")
        # Koordinatlar makul aralıkta mı? (sadece kaynak kod varlığı doğrulandı)
    ok("55.364" in SCANNER_SRC or "55.3" in SCANNER_SRC,
       "Dubai (OMDB) koordinatları (55.36x boylamı) eksik")
    ok("139.78" in SCANNER_SRC or "139.7" in SCANNER_SRC,
       "Tokyo (RJTT) koordinatları (139.78x boylamı) eksik")

test("STATION_COORDS: tüm 12 istasyon koordinatları mevcut",
     test_station_coords_coverage)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 23: Yeni İstasyonlar — Dubai (OMDB) & Tokyo (RJTT)")
print("══════════════════════════════════════════════════════════════")

def test_new_stations_in_scanner():
    """scanner.py STATIONS listesine omdb ve rjtt eklendi mi?"""
    ok('"omdb"' in SCANNER_SRC or "'omdb'" in SCANNER_SRC,
       "scanner.py STATIONS'da 'omdb' yok")
    ok('"rjtt"' in SCANNER_SRC or "'rjtt'" in SCANNER_SRC,
       "scanner.py STATIONS'da 'rjtt' yok")
    ok('"omdb": "Dubai' in SCANNER_SRC or "'omdb': 'Dubai'" in SCANNER_SRC
       or '"omdb": "Dubai    "' in SCANNER_SRC,
       "scanner.py STATION_LABELS'da Dubai etiketi yok")
    ok('"rjtt": "Tokyo' in SCANNER_SRC or "'rjtt': 'Tokyo'" in SCANNER_SRC
       or '"rjtt": "Tokyo    "' in SCANNER_SRC,
       "scanner.py STATION_LABELS'da Tokyo etiketi yok")

def test_new_stations_in_trader():
    """trader.py STATION_LABELS'a omdb ve rjtt eklendi mi?"""
    ok('"omdb"' in TRADER_SRC or "'omdb'" in TRADER_SRC,
       "trader.py STATION_LABELS'da 'omdb' yok")
    ok('"rjtt"' in TRADER_SRC or "'rjtt'" in TRADER_SRC,
       "trader.py STATION_LABELS'da 'rjtt' yok")
    ok("Dubai" in TRADER_SRC,
       "trader.py STATION_LABELS'da 'Dubai' etiketi yok")
    ok("Tokyo" in TRADER_SRC,
       "trader.py STATION_LABELS'da 'Tokyo' etiketi yok")

def test_station_count():
    """Toplam istasyon sayısı 12 olmalı (10 Avrupa + Dubai + Tokyo)."""
    # STATIONS listesindeki istasyon sayısını kaynak koddan çıkar
    import re as _re
    m = _re.search(r'STATIONS\s*=\s*\[([^\]]+)\]', SCANNER_SRC, _re.S)
    if m:
        items = [s.strip().strip('"\'') for s in m.group(1).split(',') if s.strip()]
        ok(len(items) >= 12,
           f"STATIONS listesinde {len(items)} istasyon var, en az 12 bekleniyor (Dubai+Tokyo eklendi)")
    else:
        ok(False, "STATIONS listesi parse edilemedi")

test("Yeni istasyonlar: scanner.py STATIONS + STATION_LABELS güncellendi",
     test_new_stations_in_scanner)
test("Yeni istasyonlar: trader.py STATION_LABELS güncellendi",
     test_new_stations_in_trader)
test("Yeni istasyonlar: STATIONS listesi en az 12 istasyon içeriyor",
     test_station_count)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 24: 2-Bucket Stratejisi")
print("══════════════════════════════════════════════════════════════")

def simulate_two_bucket_decision(
    top_pick, top_pct, second_pick, second_pct,
    top_price, second_price,
    min_mode_pct=30, min_edge=0.05, min_price=0.05, max_price=0.40,
):
    """scan_date() 2-bucket karar mantığını simüle et.

    Döner: ["primary"] veya ["primary", "secondary"] veya ["primary"] (edge yetersiz)
    """
    result = ["primary"]

    if (
        second_pick is not None
        and second_pct is not None
        and abs(second_pick - top_pick) == 1    # bitişik bucket
        and second_pct >= min_mode_pct          # ensemble konsensüsü yeterli
    ):
        s_edge = (second_pct / 100) - second_price
        if (
            min_price <= second_price < max_price
            and s_edge >= min_edge
        ):
            result.append("secondary")

    return result

test("2-bucket: bitişik 2. pick + edge → iki bucket açılır", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=45,
        second_pick=15, second_pct=35,    # bitişik, %35 konsensüs
        top_price=0.28, second_price=0.22  # 35%-22%=+13% edge
    ), ["primary", "secondary"])
)
test("2-bucket: 2. pick bitişik DEĞİL (2°C fark) → tek bucket", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=45,
        second_pick=16, second_pct=35,    # 2°C fark, bitişik değil
        top_price=0.28, second_price=0.22
    ), ["primary"])
)
test("2-bucket: 2. pick konsensüs çok düşük (<%30) → tek bucket", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=45,
        second_pick=15, second_pct=25,    # %25 < MIN_MODE_PCT=30
        top_price=0.28, second_price=0.22
    ), ["primary"])
)
test("2-bucket: 2. bucket edge yetersiz → tek bucket", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=45,
        second_pick=15, second_pct=32,    # 32%-30%=+2% edge < MIN_EDGE=5%
        top_price=0.28, second_price=0.30
    ), ["primary"])
)
test("2-bucket: 2. bucket fiyatı MAX_PRICE'ı aşıyor → tek bucket", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=45,
        second_pick=15, second_pct=35,
        top_price=0.28, second_price=0.42  # > MAX_PRICE=0.40
    ), ["primary"])
)
test("2-bucket: 2. bucket fiyatı MIN_PRICE'ın altında → tek bucket", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=45,
        second_pick=15, second_pct=35,
        top_price=0.28, second_price=0.03  # < MIN_PRICE=0.05
    ), ["primary"])
)
test("2-bucket: second_pick=None → tek bucket (ensemble tek mod)", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=60,
        second_pick=None, second_pct=None,
        top_price=0.28, second_price=0.00
    ), ["primary"])
)
test("2-bucket: alt komşu da geçerli (14→13)", lambda:
    eq(simulate_two_bucket_decision(
        top_pick=14, top_pct=40,
        second_pick=13, second_pct=32,    # alt bitişik
        top_price=0.28, second_price=0.20
    ), ["primary", "secondary"])
)
test("2-bucket: kaynak kodda two_bucket flag var (trade kaydı)", lambda:
    ok("two_bucket" in SCANNER_SRC,
       "scanner.py 2-bucket trade dict'inde 'two_bucket' flag yok")
)
test("2-bucket: kaynak kodda second_bucket mantığı mevcut", lambda: (
    ok("result_trades" in SCANNER_SRC,
       "scanner.py'de result_trades listesi yok (2-bucket dönüş yapısı)"),
    ok("second_pick" in SCANNER_SRC and "second_pct" in SCANNER_SRC,
       "scanner.py'de second_pick/second_pct kullanımı yok"),
))

# P&L doğrulaması: 2-bucket senaryosu
def test_two_bucket_pnl():
    """2-bucket P&L matematik: biri kazanırsa net pozitif mu?
    10 share, 1.bucket 28¢, 2.bucket 22¢ → toplam maliyet $5.00
    Birisi kazanırsa $10, öteki kaybeder (2.28+2.22=4.50 loss)
    Net: +$10 - $2.80 (kaybeden 1. bucket) - 0 (kazanan 2.) = +$7.20
    Veya: +$10 - $2.20 (kaybeden 2. bucket) - 0 = +$7.80
    Her iki senaryo net pozitif."""
    shares    = 10
    price1    = 0.28
    price2    = 0.22
    cost1     = shares * price1   # $2.80
    cost2     = shares * price2   # $2.20
    payout    = shares * 1.0      # $10.00

    # Senaryo A: 1. bucket kazanır
    pnl_a = (payout - cost1) - cost2   # +$10 - $2.80 - $2.20 = +$5.00
    # Senaryo B: 2. bucket kazanır
    pnl_b = (payout - cost2) - cost1   # +$10 - $2.20 - $2.80 = +$5.00
    # Senaryo C: ikisi de kaybeder
    pnl_c = -(cost1 + cost2)            # -$5.00

    ok(pnl_a > 0, f"Senaryo A (1. kazanır) net negatif: ${pnl_a:.2f}")
    ok(pnl_b > 0, f"Senaryo B (2. kazanır) net negatif: ${pnl_b:.2f}")
    ok(pnl_c < 0, f"Senaryo C (ikisi kayıp) pozitif gösteriliyor: ${pnl_c:.2f}")
    # En az biri kazanınca toplam maliyet geri alınıyor
    eq(round(pnl_a, 2), round(pnl_b, 2),
       "Simetrik fiyat varsayımı kırıldı — P&L simetrik olmalı")

test("2-bucket P&L: birisi kazanınca net pozitif, ikisi kayıpsa negatif",
     test_two_bucket_pnl)

# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 25: SQLite Altyapısı (Faz 1)")
print("══════════════════════════════════════════════════════════════")

def _import_db_module():
    """bot/db.py'yi bağımsız yükle — Python 3.9 annotation sorunu için."""
    import importlib.util, pathlib
    db_path = pathlib.Path(__file__).resolve().parent.parent / "bot" / "db.py"
    if not db_path.exists():
        raise FileNotFoundError(f"bot/db.py bulunamadı: {db_path}")
    spec = importlib.util.spec_from_file_location("weather_bot_db_test", db_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_db_module_loads():
    db = _import_db_module()
    ok(hasattr(db, "init_db"),           "init_db eksik")
    ok(hasattr(db, "sync_all"),          "sync_all eksik")
    ok(hasattr(db, "sync_paper_trades"), "sync_paper_trades eksik")
    ok(hasattr(db, "sync_live_trades"),  "sync_live_trades eksik")
    ok(hasattr(db, "record_forecast_error"), "record_forecast_error eksik")
    ok(hasattr(db, "summary_stats"),     "summary_stats eksik")

test("db modülü yüklenir ve gerekli API'yi sağlar", test_db_module_loads)


def _make_sample_paper(n=3):
    """Örnek paper trade kayıtları üret."""
    return [
        {
            "id": f"test_{i}", "station": "eglc", "date": "2026-04-22",
            "blend": 14.5 + i*0.3, "spread": 0.5, "uncertainty": "Düşük",
            "top_pick": 15, "raw_top_pick": 14, "bias_applied": 1,
            "ens_mode_pct": 40, "ens_2nd_pick": 14, "ens_2nd_pct": 22,
            "bucket_title": "15°C", "condition_id": f"cond{i}",
            "entry_price": 0.18, "shares": 10, "cost_usd": 1.80,
            "potential_win": 8.20, "liquidity": 10000,
            "status": "closed" if i < 2 else "open",
            "entered_at": "2026-04-21T12:00:00",
            "actual_temp": 15.0 if i < 2 else None,
            "result": "WIN" if i < 2 else None,
            "pnl": 8.20 if i < 2 else None,
            "settled_at": "2026-04-22T17:00:00" if i < 2 else None,
        }
        for i in range(n)
    ]


def test_db_schema_creates_all_tables():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        expected = {"paper_trades", "live_trades", "forecast_errors",
                    "bias_corrections", "model_weights"}
        for t in expected:
            ok(t in tables, f"Tablo {t} oluşmamış (mevcut: {tables})")

test("db şeması 5 tablo oluşturur", test_db_schema_creates_all_tables)


def test_db_wal_mode_enabled():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        eq(mode.lower(), "wal", f"Journal mode WAL olmalı, gerçek: {mode}")

test("db WAL mode aktif (crash recovery)", test_db_wal_mode_enabled)


def test_sync_paper_trades_roundtrip():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "paper.json"
        db_path   = Path(td) / "test.db"
        samples   = _make_sample_paper(5)
        json_path.write_text(json.dumps(samples))

        db.init_db(db_path)
        n = db.sync_paper_trades(db_path, json_path)
        eq(n, 5, "sync_paper_trades kayıt sayısı yanlış")

        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()
            eq(len(rows), 5, "DB'de 5 kayıt olmalı")
            eq(rows[0]["station"], "eglc")
            eq(rows[0]["status"], "closed")
            eq(rows[0]["top_pick"], 15)
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE pnl IS NOT NULL"
            ).fetchone()[0]
            # 2 closed trade (i<2) — her biri 8.20, toplam 16.40
            ok(abs(total_pnl - 16.40) < 0.01, f"pnl toplamı yanlış: {total_pnl}")

test("sync_paper_trades roundtrip (JSON→DB, veri bütünlüğü)",
     test_sync_paper_trades_roundtrip)


def test_sync_handles_missing_json_gracefully():
    """JSON dosyası yoksa veya bozuksa hata verme."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path   = Path(td) / "test.db"
        missing   = Path(td) / "nonexistent.json"
        db.init_db(db_path)
        # Eksik dosya: 0 döner, hata fırlatmaz
        n = db.sync_paper_trades(db_path, missing)
        eq(n, 0, "Eksik dosya 0 döndürmeli")
        # Bozuk JSON: 0 döner
        corrupt = Path(td) / "corrupt.json"
        corrupt.write_text("{ not valid json")
        n = db.sync_paper_trades(db_path, corrupt)
        eq(n, 0, "Bozuk JSON 0 döndürmeli")

test("sync eksik/bozuk JSON'da sessizce 0 döner", test_sync_handles_missing_json_gracefully)


def test_sync_idempotent():
    """Aynı JSON'u 2 kez sync et → kayıt sayısı değişmesin."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "paper.json"
        db_path   = Path(td) / "test.db"
        samples   = _make_sample_paper(3)
        json_path.write_text(json.dumps(samples))
        db.init_db(db_path)
        db.sync_paper_trades(db_path, json_path)
        db.sync_paper_trades(db_path, json_path)  # ikinci kez
        db.sync_paper_trades(db_path, json_path)  # üçüncü kez
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
            eq(n, 3, "Çoklu sync sonrası 3 kayıt olmalı (duplicate yok)")

test("sync idempotent (çoklu çağrı duplicate üretmez)", test_sync_idempotent)


def test_sync_reflects_status_changes():
    """JSON'da status değiştiğinde SQLite yansıtsın."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "paper.json"
        db_path   = Path(td) / "test.db"
        samples   = _make_sample_paper(3)
        json_path.write_text(json.dumps(samples))
        db.init_db(db_path)
        db.sync_paper_trades(db_path, json_path)

        # 3. trade'i open'dan closed'a çevir
        samples[2]["status"]      = "closed"
        samples[2]["actual_temp"] = 14.0
        samples[2]["result"]      = "LOSS"
        samples[2]["pnl"]         = -1.80
        json_path.write_text(json.dumps(samples))
        db.sync_paper_trades(db_path, json_path)

        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, result, pnl FROM paper_trades WHERE id='test_2'"
            ).fetchone()
            eq(row["status"], "closed")
            eq(row["result"], "LOSS")
            ok(abs(row["pnl"] - (-1.80)) < 0.01)

test("sync status değişimlerini yansıtır (open→closed)",
     test_sync_reflects_status_changes)


def test_live_trades_sync():
    """Live trade sync + status field'ları."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "live.json"
        db_path   = Path(td) / "test.db"
        samples = [
            {
                "id": "live_1", "paper_id": "test_0", "station": "eglc",
                "date": "2026-04-22", "top_pick": 15, "bucket_title": "15°C",
                "condition_id": "c1", "order_id": "o1", "limit_price": 0.18,
                "shares": 5, "cost_usdc": 0.90, "fill_price": 0.18,
                "fill_time": "2026-04-21T13:00:00",
                "placed_at": "2026-04-21T12:00:00",
                "expires_at": "2026-04-21T17:00:00", "horizon": "D+1",
                "status": "settled_win", "result": "WIN", "pnl_usdc": 4.10,
                "settled_at": "2026-04-22T17:00:00", "notes": "actual=15",
                "redeemed": True, "redeemed_at": "2026-04-22T18:00:00",
                "redeem_tx": "abc123",
            },
            {
                "id": "live_2", "paper_id": "test_1", "station": "lfpg",
                "date": "2026-04-23", "top_pick": 18, "bucket_title": "18°C",
                "condition_id": "c2", "order_id": "o2", "limit_price": 0.25,
                "shares": 5, "cost_usdc": 1.25, "fill_price": None,
                "placed_at": "2026-04-22T10:00:00",
                "expires_at": "2026-04-22T15:00:00", "horizon": "D+1",
                "status": "pending_fill", "result": None, "pnl_usdc": None,
                "settled_at": None, "notes": "",
            },
        ]
        json_path.write_text(json.dumps(samples))
        db.init_db(db_path)
        n = db.sync_live_trades(db_path, json_path)
        eq(n, 2)
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM live_trades ORDER BY id"
            ).fetchall()
            eq(rows[0]["status"], "settled_win")
            eq(rows[0]["redeemed"], 1, "bool True → 1 olarak kaydedilmeli")
            eq(rows[1]["status"], "pending_fill")
            ok(rows[1]["fill_price"] is None)

test("sync_live_trades tüm field'ları (bool dönüşümü dahil)",
     test_live_trades_sync)


def test_record_forecast_error_writes_row():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        db.record_forecast_error(
            date="2026-04-22", station="eglc",
            horizon_days=1, blend=14.5, top_pick=15,
            spread=0.5, uncertainty="Düşük",
            actual_temp=14.0, trade_id="test_x",
            db_path=db_path,
        )
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM forecast_errors").fetchone()
            eq(row["date"], "2026-04-22")
            eq(row["station"], "eglc")
            eq(row["month"], 4)
            eq(row["season"], "spring")
            ok(abs(row["error_c"] - 0.5) < 0.01,   "error_c yanlış")
            ok(abs(row["abs_error_c"] - 0.5) < 0.01, "abs_error_c yanlış")
            eq(row["pick_error"], 1)   # 15 - round(14.0) = 1
            eq(row["trade_id"], "test_x")

test("record_forecast_error tüm bileşenleri yazar (season, pick_error)",
     test_record_forecast_error_writes_row)


def test_already_recorded_error():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        eq(db.already_recorded_error("t1", db_path), False, "Başta yok")
        db.record_forecast_error(
            date="2026-04-22", station="eglc", horizon_days=1,
            blend=14.0, top_pick=14, spread=0.3, uncertainty="Düşük",
            actual_temp=14.0, trade_id="t1", db_path=db_path,
        )
        eq(db.already_recorded_error("t1", db_path), True, "Yazılan true olmalı")
        eq(db.already_recorded_error("t2", db_path), False, "Yazılmayan false")

test("already_recorded_error duplicate koruması", test_already_recorded_error)


def test_sync_preserves_pnl_totals():
    """JSON'daki toplam P&L SQLite'a birebir aktarılmalı."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "paper.json"
        db_path   = Path(td) / "test.db"
        samples = [
            {"id": "t1", "station": "eglc", "date": "2026-04-20",
             "status": "closed", "pnl": 7.30, "top_pick": 14,
             "entry_price": 0.27, "shares": 10, "cost_usd": 2.70},
            {"id": "t2", "station": "lfpg", "date": "2026-04-20",
             "status": "closed", "pnl": -1.80, "top_pick": 18,
             "entry_price": 0.18, "shares": 10, "cost_usd": 1.80},
            {"id": "t3", "station": "ltac", "date": "2026-04-20",
             "status": "closed", "pnl": -2.50, "top_pick": 10,
             "entry_price": 0.25, "shares": 10, "cost_usd": 2.50},
        ]
        json_path.write_text(json.dumps(samples))
        db.init_db(db_path)
        db.sync_paper_trades(db_path, json_path)

        json_total = sum(t["pnl"] for t in samples)   # 7.30 - 1.80 - 2.50 = 3.00
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            db_total = conn.execute(
                "SELECT SUM(pnl) FROM paper_trades WHERE pnl IS NOT NULL"
            ).fetchone()[0]
        ok(abs(db_total - json_total) < 0.01,
           f"P&L uyuşmuyor: JSON={json_total} DB={db_total}")

test("sync P&L toplamlarını bozmadan aktarır",
     test_sync_preserves_pnl_totals)


def test_sync_all_handles_both_files():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        paper_json = Path(td) / "paper.json"
        live_json  = Path(td) / "live.json"
        db_path    = Path(td) / "test.db"
        paper_json.write_text(json.dumps(_make_sample_paper(4)))
        live_json.write_text(json.dumps([{
            "id": "l1", "paper_id": "test_0", "station": "eglc",
            "date": "2026-04-22", "top_pick": 15, "bucket_title": "15°C",
            "condition_id": "c1", "order_id": "o1", "limit_price": 0.18,
            "shares": 5, "cost_usdc": 0.90, "status": "filled",
        }]))
        # monkey-patch yollar
        orig_p, orig_l, orig_d = db.PAPER_JSON, db.LIVE_JSON, db.DB_PATH
        db.PAPER_JSON = paper_json
        db.LIVE_JSON  = live_json
        db.DB_PATH    = db_path
        try:
            r = db.sync_all(db_path)
            eq(r["paper"], 4)
            eq(r["live"], 1)
            eq(r["errors"], [])
        finally:
            db.PAPER_JSON, db.LIVE_JSON, db.DB_PATH = orig_p, orig_l, orig_d

test("sync_all her iki JSON'ı birlikte işler",
     test_sync_all_handles_both_files)


def test_summary_stats_reports_counts():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        # 2 open, 1 closed paper
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            for i, st in enumerate(["open", "open", "closed"]):
                conn.execute(
                    "INSERT INTO paper_trades(id,station,date,status) "
                    "VALUES (?,?,?,?)",
                    (f"p{i}", "eglc", "2026-04-22", st),
                )
            conn.commit()
        stats = db.summary_stats(db_path)
        eq(stats["paper_total"], 3)
        eq(stats["paper_open"], 2)
        eq(stats["paper_closed"], 1)

test("summary_stats trade sayılarını doğru raporlar",
     test_summary_stats_reports_counts)


def test_scanner_save_has_sync_hook():
    """scanner.save_trades artık sync_paper_trades çağırıyor."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    ok("sync_paper_trades" in src, "scanner.save_trades sync çağırmıyor")
    # try/except ile güvenli olmalı
    save_fn_start = src.find("def save_trades(")
    save_fn_end   = src.find("\ndef ", save_fn_start + 1)
    save_fn       = src[save_fn_start:save_fn_end]
    ok("try:" in save_fn and "except" in save_fn,
       "save_trades içinde sync try/except eksik — sync hatası scanner'ı çökertebilir")

test("scanner.save_trades sync_paper_trades'i güvenle çağırır",
     test_scanner_save_has_sync_hook)


def test_trader_save_has_sync_hook():
    trader_path = Path(__file__).resolve().parent.parent / "bot" / "trader.py"
    src = trader_path.read_text(encoding="utf-8")
    ok("sync_live_trades" in src, "trader.save_live_trades sync çağırmıyor")
    save_fn_start = src.find("def save_live_trades(")
    save_fn_end   = src.find("\ndef ", save_fn_start + 1)
    save_fn       = src[save_fn_start:save_fn_end]
    ok("try:" in save_fn and "except" in save_fn,
       "save_live_trades içinde sync try/except eksik")

test("trader.save_live_trades sync_live_trades'i güvenle çağırır",
     test_trader_save_has_sync_hook)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 26: Faz 2 — Bimodal + Bootstrap + Dinamik CALIB")
print(f"{'═'*62}")

def _import_main_module():
    """main.py'yi scanner.py import'unu atlamak için doğrudan yükle."""
    import importlib.util
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    spec = importlib.util.spec_from_file_location("weather_main", main_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dynamic_calib_basic():
    m = _import_main_module()
    # D+0 temel
    ok(abs(m.dynamic_calib_factor(0, 0.5) - 1.2) < 0.01, "D+0 base=1.2")
    # D+1 temel
    ok(abs(m.dynamic_calib_factor(1, 0.5) - 1.5) < 0.01, "D+1 base=1.5")
    # D+2 temel
    ok(abs(m.dynamic_calib_factor(2, 0.5) - 2.0) < 0.01, "D+2 base=2.0")

test("dynamic_calib_factor horizon tabanlı (0→1.2, 1→1.5, 2→2.0)",
     test_dynamic_calib_basic)


def test_dynamic_calib_spread_extra():
    m = _import_main_module()
    # geniş spread → +0.2
    v = m.dynamic_calib_factor(1, 2.5)
    ok(abs(v - 1.7) < 0.01, f"spread>2.0 için 1.5+0.2=1.7 beklenir, bulunan {v}")
    # tavan 2.5
    v = m.dynamic_calib_factor(2, 3.0)
    ok(v <= 2.5, f"tavan 2.5 aşıldı: {v}")

test("dynamic_calib_factor: spread>2°C → +0.2 ek, tavan 2.5",
     test_dynamic_calib_spread_extra)


def test_dynamic_calib_none_spread():
    m = _import_main_module()
    # None spread → sadece base
    v = m.dynamic_calib_factor(1, None)
    ok(abs(v - 1.5) < 0.01)

test("dynamic_calib_factor: spread None → sadece base",
     test_dynamic_calib_none_spread)


def test_bimodal_unimodal():
    m = _import_main_module()
    # tek tepe — 90 üye 15 civarında toplanmış
    members = [15.0] * 50 + [14.7, 15.3, 14.5, 15.5, 15.1] * 2 + [16.0] * 5 + [14.0] * 5
    r = m.bimodal_analysis(members)
    ok(not r["is_bimodal"], f"tek tepe bimodal sayıldı: {r}")

test("bimodal_analysis: tek tepeli dağılım bimodal değil",
     test_bimodal_unimodal)


def test_bimodal_detected():
    m = _import_main_module()
    # iki tepe, 3°C ayrımda — açık bimodal
    members = [13.0] * 25 + [13.2] * 10 + [16.0] * 25 + [15.8] * 10
    r = m.bimodal_analysis(members)
    ok(r["is_bimodal"], f"bimodal dağılım yakalanmadı: {r}")
    ok(r["separation"] is not None and r["separation"] >= 2,
       f"tepe ayrımı yanlış: {r}")

test("bimodal_analysis: iki tepeli dağılım yakalanır (ayrım≥2°C)",
     test_bimodal_detected)


def test_bimodal_close_peaks_not_flagged():
    m = _import_main_module()
    # İki tepe ama sadece 1°C ayrımda — 2-bucket stratejisi halleder, bimodal sayılmaz
    members = [14.0] * 20 + [15.0] * 18 + [14.5] * 5 + [15.5] * 5
    r = m.bimodal_analysis(members)
    ok(not r["is_bimodal"],
       f"bitişik tepeler (ayrım 1°C) bimodal işaretlenmemeli: {r}")

test("bimodal_analysis: bitişik tepeler (ayrım<2°C) bimodal değil",
     test_bimodal_close_peaks_not_flagged)


def test_bootstrap_tight_ci_strong_mode():
    m = _import_main_module()
    # 90 üyenin 70'i 15°C → mod çok kararlı
    members = [15.0] * 70 + [14.7, 15.3, 16.0, 14.0] * 5
    r = m.bootstrap_mode_ci(members)
    ok(r["top_pick"] == 15, f"top_pick yanlış: {r}")
    # CI alt sınır en az 60 olmalı (mod çok güçlü)
    ok(r["ci_low"] >= 60, f"güçlü modda CI alt çok düşük: {r}")

test("bootstrap_mode_ci: güçlü mod → dar CI (ci_low>=60)",
     test_bootstrap_tight_ci_strong_mode)


def test_bootstrap_wide_ci_fragile_mode():
    m = _import_main_module()
    # 40 üye, sadece 11'i (top_pick için) %27 — sınırda zayıf
    members = [15.0] * 11 + [14.0] * 10 + [16.0] * 10 + [13.0] * 9
    r = m.bootstrap_mode_ci(members)
    ok(r["mode_pct"] <= 30, f"mode_pct çok yüksek: {r}")
    # CI geniş olmalı (fragile)
    ok(r["ci_high"] - r["ci_low"] >= 10,
       f"kırılgan modda CI dar: {r}")

test("bootstrap_mode_ci: kırılgan mod → geniş CI (high-low>=10)",
     test_bootstrap_wide_ci_fragile_mode)


def test_bootstrap_deterministic():
    """Aynı ensemble → aynı CI (scanner her çağrıda aynı sonucu görsün)."""
    m = _import_main_module()
    members = [15.0] * 30 + [14.0, 16.0] * 10 + [13.5, 15.5] * 10
    r1 = m.bootstrap_mode_ci(members)
    r2 = m.bootstrap_mode_ci(members)
    ok(r1["ci_low"] == r2["ci_low"] and r1["ci_high"] == r2["ci_high"],
       f"bootstrap deterministik değil: {r1} vs {r2}")

test("bootstrap_mode_ci: aynı veri → aynı CI (deterministik)",
     test_bootstrap_deterministic)


def test_bootstrap_empty_safe():
    m = _import_main_module()
    r = m.bootstrap_mode_ci([])
    ok(r["mode_pct"] is None and r["ci_low"] is None,
       f"boş ensemble None dönmeli: {r}")

test("bootstrap_mode_ci: boş liste güvenli None döner",
     test_bootstrap_empty_safe)


def test_calib_applied_in_blend_day():
    """blend_day() dinamik calib_factor üretip calibrated_spread hesaplıyor."""
    m = _import_main_module()
    models_data = {
        "gfs":         {"max_temp": 14.8, "hours": []},
        "ecmwf":       {"max_temp": 15.0, "hours": []},
        "icon":        {"max_temp": 15.2, "hours": []},
        "ukmo":        {"max_temp": 15.1, "hours": []},
        "meteofrance": {"max_temp": 14.9, "hours": []},
    }
    r = m.blend_day(models_data, horizon=1)
    ok("calib_factor" in r and r["calib_factor"] is not None,
       "blend_day calib_factor döndürmüyor")
    ok("calibrated_spread" in r,
       "blend_day calibrated_spread döndürmüyor")
    # D+1, düşük spread → base 1.5 beklenir
    ok(abs(r["calib_factor"] - 1.5) < 0.01,
       f"D+1 dar spread → 1.5 beklenir, bulunan {r['calib_factor']}")

test("blend_day: calib_factor + calibrated_spread döner",
     test_calib_applied_in_blend_day)


def test_scanner_has_fragility_filter():
    """scanner.py bootstrap CI alt sınırı filtresini kaynak kodda içeriyor."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    ok("MIN_MODE_CI_LOW" in src, "MIN_MODE_CI_LOW sabiti yok")
    ok("mode_ci_low" in src, "scanner mode_ci_low okumuyor")
    ok("BIMODAL_MAX_SEPARATION" in src, "BIMODAL_MAX_SEPARATION sabiti yok")

test("scanner.py: Faz 2 kırılganlık + bimodal filtreleri kodda",
     test_scanner_has_fragility_filter)


def test_db_schema_has_phase2_columns():
    """SQLite şeması ve PAPER_FIELDS Faz 2 kolonlarını içeriyor."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
    for c in ("ens_is_bimodal", "ens_peak_sep",
              "ens_mode_ci_low", "ens_mode_ci_high"):
        ok(c in cols, f"paper_trades şemasında {c} yok")
    for c in ("ens_is_bimodal", "ens_peak_sep",
              "ens_mode_ci_low", "ens_mode_ci_high"):
        ok(c in db.PAPER_FIELDS, f"PAPER_FIELDS'te {c} yok")

test("db.py: paper_trades şemasında Faz 2 kolonları mevcut",
     test_db_schema_has_phase2_columns)


def test_db_migration_adds_phase2_columns():
    """Eski sürüm (v1) şema üstüne init_db çağrılınca yeni kolonlar eklenir."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        import sqlite3
        # v1 şema — Faz 2 kolonlarından yoksun ama indeksler için gerekli
        # kolonları barındıran üretimdeki gerçek mirror
        v1_sql = """
        CREATE TABLE paper_trades (
            id TEXT PRIMARY KEY, station TEXT NOT NULL, date TEXT NOT NULL,
            blend REAL, spread REAL, uncertainty TEXT,
            top_pick INTEGER, raw_top_pick INTEGER, bias_applied INTEGER,
            ens_mode_pct INTEGER, ens_2nd_pick INTEGER, ens_2nd_pct INTEGER,
            bucket_title TEXT, condition_id TEXT, entry_price REAL,
            shares REAL, cost_usd REAL, size_usd REAL, potential_win REAL,
            liquidity REAL, status TEXT NOT NULL, entered_at TEXT,
            actual_temp REAL, result TEXT, pnl REAL, settled_at TEXT,
            two_bucket INTEGER, notes TEXT,
            synced_at INTEGER DEFAULT (strftime('%s','now'))
        );
        """
        with sqlite3.connect(str(db_path)) as conn:
            conn.executescript(v1_sql)
        # init_db çağırınca migration yeni kolonları eklemeli
        db.init_db(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
    for c in ("ens_is_bimodal", "ens_peak_sep",
              "ens_mode_ci_low", "ens_mode_ci_high"):
        ok(c in cols, f"migration sonrası {c} kolonu eklenmedi")

test("db.py: eski (v1) şema üstüne migration yeni kolonları ekler",
     test_db_migration_adds_phase2_columns)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 27: Faz 3 — Kalman Bias + Sinyal Kalitesi")
print(f"{'═'*62}")

def _import_kalman():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "bot" / "kalman.py"
    spec = importlib.util.spec_from_file_location("weather_kalman", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_signal_score():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "bot" / "signal_score.py"
    spec = importlib.util.spec_from_file_location("weather_signal", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_kalman_zero_bias():
    k = _import_kalman()
    # bias=0 civarında 10 gözlem → bias ≈ 0
    obs = [(0.1, "2026-03-01"), (-0.2, "2026-03-02"), (0.0, "2026-03-03"),
           (0.3, "2026-03-04"), (-0.1, "2026-03-05"), (0.05, "2026-03-06"),
           (-0.15, "2026-03-07"), (0.1, "2026-03-08"), (-0.05, "2026-03-09"),
           (0.2, "2026-03-10")]
    bias, var = k.kalman_bias_estimate(obs)
    ok(abs(bias) < 0.5, f"bias sıfıra yakın beklenir, bulunan {bias}")

test("kalman_bias_estimate: sıfır civarı gözlemler → bias ≈ 0",
     test_kalman_zero_bias)


def test_kalman_positive_bias():
    k = _import_kalman()
    # 10 gözlem ortalama +1.5°C — bias yakınsamalı
    obs = [(1.5 + (i % 3 - 1) * 0.2, f"2026-03-{d:02d}")
           for i, d in enumerate(range(1, 11))]
    bias, var = k.kalman_bias_estimate(obs)
    ok(0.8 < bias < 2.0, f"bias +1.5 civarı olmalı, bulunan {bias}")
    ok(var < 1.0, f"10 gözlem sonrası variance düşmeli, bulunan {var}")

test("kalman_bias_estimate: +1.5°C bias yakınsar, variance düşer",
     test_kalman_positive_bias)


def test_kalman_recency_bias():
    """Son gözlemler eski gözlemlerden daha etkili (process noise)."""
    k = _import_kalman()
    # İlk 5 gözlem 0 civarında, son 5 gözlem +3°C — Kalman yakın döneme ağırlık verir
    obs = [(0.0, f"2026-03-{d:02d}") for d in range(1, 6)] + \
          [(3.0, f"2026-03-{d:02d}") for d in range(6, 11)]
    bias, _var = k.kalman_bias_estimate(obs)
    # Son gözlemlere daha fazla ağırlık verildiği için bias > 1.0 olmalı
    ok(bias > 1.0, f"son gözlemler ağırlıklı olmalı, bias={bias}")

test("kalman_bias_estimate: son gözlemler eski gözlemlerden etkili",
     test_kalman_recency_bias)


def test_kalman_empty():
    k = _import_kalman()
    bias, var = k.kalman_bias_estimate([])
    ok(bias == 0.0, f"boş gözlem → bias 0, bulunan {bias}")
    ok(var > 0, f"boş gözlem → variance pozitif (prior), bulunan {var}")

test("kalman_bias_estimate: boş gözlem güvenli (bias=0)",
     test_kalman_empty)


def test_kalman_station_biases_integration():
    k = _import_kalman()
    trades = [
        # 6 trade: actual - top_pick ortalaması +1.3 → bias ≈ +1
        {"status": "closed", "station": "epwa", "date": f"2026-03-{d:02d}",
         "top_pick": 10, "actual_temp": 11.3 + (d % 3 - 1) * 0.2}
        for d in range(1, 7)
    ]
    biases = k.kalman_station_biases(trades, max_correction=2, min_trades=5)
    ok("epwa" in biases, f"EPWA bias hesaplanmadı: {biases}")
    ok(biases["epwa"] == 1, f"EPWA bias +1 beklenir, bulunan {biases['epwa']}")

test("kalman_station_biases: kapalı trade'lerden bias öğrenir (+1)",
     test_kalman_station_biases_integration)


def test_kalman_bias_cap():
    """MAX_BIAS_CORRECTION tavanı çok büyük bias'ı sınırlar."""
    k = _import_kalman()
    trades = [
        {"status": "closed", "station": "lfpg", "date": f"2026-03-{d:02d}",
         "top_pick": 10, "actual_temp": 16.0}   # +6°C sürekli!
        for d in range(1, 11)
    ]
    biases = k.kalman_station_biases(trades, max_correction=2, min_trades=5)
    ok(biases.get("lfpg", 0) == 2, f"tavan 2 bekleniyor, bulunan {biases}")

test("kalman_station_biases: max_correction tavanı uygulanır",
     test_kalman_bias_cap)


def test_signal_score_perfect():
    s = _import_signal_score()
    r = s.compute_signal_score(
        mode_pct=80, mode_ci_low=75, mode_ci_high=85,
        edge=0.30, uncertainty="Düşük",
        is_bimodal=False, n_members=90,
    )
    ok(r["score"] >= 85, f"mükemmel sinyal ≥85 beklenir, bulunan {r}")
    ok(r["grade"] == "güçlü", f"güçlü grade beklenir: {r}")

test("compute_signal_score: mükemmel sinyal → ≥85 puan, 'güçlü'",
     test_signal_score_perfect)


def test_signal_score_weak():
    s = _import_signal_score()
    r = s.compute_signal_score(
        mode_pct=32, mode_ci_low=22, mode_ci_high=48,
        edge=0.05, uncertainty="Yüksek",
        is_bimodal=True, n_members=40,
    )
    ok(r["score"] < 50, f"zayıf sinyal < 50 beklenir: {r}")
    ok(r["grade"] == "zayıf", f"zayıf grade beklenir: {r}")

test("compute_signal_score: sınırda sinyal → <50 puan, 'zayıf'",
     test_signal_score_weak)


def test_signal_score_components_sum():
    s = _import_signal_score()
    r = s.compute_signal_score(
        mode_pct=55, mode_ci_low=48, mode_ci_high=62,
        edge=0.15, uncertainty="Orta",
        is_bimodal=False, n_members=90,
    )
    ok(sum(r["components"].values()) == r["score"],
       f"bileşenler toplamı skora eşit olmalı: {r}")
    ok(0 <= r["score"] <= 100,
       f"skor 0-100 aralığı dışı: {r}")

test("compute_signal_score: bileşenler toplamı 0-100 aralığında, skora eşit",
     test_signal_score_components_sum)


def test_signal_score_missing_fields():
    s = _import_signal_score()
    # None değerler → nötr puanlama, crash yok
    r = s.compute_signal_score(
        mode_pct=None, mode_ci_low=None, mode_ci_high=None,
        edge=None, uncertainty=None,
        is_bimodal=False, n_members=0,
    )
    ok(0 <= r["score"] <= 100,
       f"eksik alan → skor hâlâ sınırda: {r}")
    ok("components" in r, f"components dict yok: {r}")

test("compute_signal_score: None alanlar crash üretmez, nötr puanlanır",
     test_signal_score_missing_fields)


def test_scanner_uses_kalman_fallback():
    """scanner.compute_station_biases Kalman kullanıyor, fallback var."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    ok("kalman_station_biases" in src, "scanner Kalman'ı kullanmıyor")
    # Fallback: try/except ile eski ortalama yönteme düşmeli
    start = src.find("def compute_station_biases")
    end   = src.find("\ndef ", start + 1)
    body  = src[start:end]
    ok("try:" in body and "except" in body,
       "Kalman fallback try/except yok")

test("scanner: compute_station_biases Kalman + eski fallback",
     test_scanner_uses_kalman_fallback)


def test_scanner_has_signal_score_integration():
    """scanner.py sinyal skorunu üretip trade'e yazıyor."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    ok("compute_signal_score" in src, "scanner signal_score çağırmıyor")
    ok('"signal_score"' in src, "trade dict'te signal_score yok")
    ok('"signal_grade"' in src, "trade dict'te signal_grade yok")

test("scanner: compute_signal_score entegrasyonu trade'e yazıyor",
     test_scanner_has_signal_score_integration)


def test_scanner_settle_records_forecast_error():
    """scanner.settle(), settlement sırasında forecast_errors'a yazıyor."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    start = src.find("def settle(")
    end   = src.find("\ndef ", start + 1)
    body  = src[start:end]
    ok("record_forecast_error" in body, "settle() forecast_errors yazmıyor")
    ok("already_recorded_error" in body, "duplicate koruması yok")
    ok("try:" in body and "except" in body, "sessiz fallback yok")

test("scanner.settle: forecast_errors tablosuna sessizce yazar",
     test_scanner_settle_records_forecast_error)


def test_db_phase3_schema():
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")}
    for c in ("signal_score", "signal_grade"):
        ok(c in cols, f"paper_trades şemasında {c} yok")
    for c in ("signal_score", "signal_grade"):
        ok(c in db.PAPER_FIELDS, f"PAPER_FIELDS'te {c} yok")

test("db.py: paper_trades şemasında Faz 3 kolonları (signal_*)",
     test_db_phase3_schema)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(f"  SONUÇ: {PASS} geçti / {FAIL} başarısız / {PASS+FAIL} toplam")
if FAIL == 0:
    print("  🎉 Tüm testler geçti!")
else:
    print(f"  ❌ {FAIL} test başarısız!")
print(f"{'═'*62}\n")

sys.exit(0 if FAIL == 0 else 1)
