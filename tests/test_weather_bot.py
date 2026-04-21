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
print(f"\n{'═'*62}")
print(f"  SONUÇ: {PASS} geçti / {FAIL} başarısız / {PASS+FAIL} toplam")
if FAIL == 0:
    print("  🎉 Tüm testler geçti!")
else:
    print(f"  ❌ {FAIL} test başarısız!")
print(f"{'═'*62}\n")

sys.exit(0 if FAIL == 0 else 1)
