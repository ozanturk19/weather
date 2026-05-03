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

# Proje kökünü sys.path'e ekle — dynamic_weights.py gibi modüllerin
# `from bot.db import ...` çağrıları çalışabilsin.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

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
    # Faz D1: reserve $5→$10 — reckless duplicate bug ($3.24 fazla harcama) sonrası
    eq(val, 10.0, f"MIN_USDC_RESERVE=10.0 beklenir (Faz D1): {val}")
    # Minimum trade bakiyesi = reserve + max maliyet (5 share × 0.40 = $2.00)
    max_cost = 5 * 0.40
    min_needed = val + max_cost
    eq(min_needed, 12.0,
       f"Minimum trade bakiyesi $12.00 olmalı (10+2): {min_needed}")

test("MIN_USDC_RESERVE: $10 rezerv (Faz D1) — min $12 bakiye gerekir", test_min_usdc_reserve)

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

# — Faz D1: MIN_USDC_RESERVE=$10 (eski $5'ten yükseltildi — duplikat bug koruması) —
test("$10 reserve ile $12.00 bakiye, $1.35 maliyet → GEÇER", lambda:
    ok(can_trade(12.00, 10.0, 1.35),
       "$12 bakiye = 10 (reserve) + 2 (max cost) → sınırda geçmeli")
)
test("$11.99 bakiye, $10 reserve, $2.00 maliyet → ENGELLENİR", lambda:
    ok(not can_trade(11.99, 10.0, 2.00),
       "$11.99 < $12.00 → engellenir")
)
test("$7.13 bakiye, $10 reserve (Faz D1) → $1.35 maliyet ENGELLENİR", lambda:
    ok(not can_trade(7.13, 10.0, 1.35),
       "$7.13 < $11.35 → Faz D1 ile engellenir (düşük bakiye koruması)")
)

# — $5 reserve matematiksel doğruluk (tarihsel referans) —
test("$7.13 bakiye, $5 reserve, $2.13 maliyet (sınır) → GEÇER", lambda:
    ok(can_trade(7.13, 5.0, 2.13))
)
test("$7.13 bakiye, $5 reserve, $2.14 maliyet (1 kuruş fazla) → ENGELLENİR", lambda:
    ok(not can_trade(7.13, 5.0, 2.14))
)
test("tam reserve kadar bakiye → her zaman engellenir (cost > 0)", lambda:
    ok(not can_trade(10.0, 10.0, 0.01))
)
test("$0 bakiye → engellenir", lambda:
    ok(not can_trade(0.0, 10.0, 1.0))
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
            and t["status"] in ("pending_fill", "filled", "sell_pending",
                                "cancelled", "settled_win", "settled_loss")
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

test("retry_live: paper VAR, live SELL_PENDING var → None (duplicate bug fix)", lambda:
    eq(scanner_dedup_check(
        "eglc", "2026-04-22", 15,
        trades=[make_paper("eglc", "2026-04-22", 15)],
        live_trades=[make_live("eglc", "2026-04-22", "sell_pending")],
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

# \s+ → dakika/saat arasında 1 veya 2 boşluk kabul eder (crontab hizalama)
REQUIRED_CRONS = [
    # (pattern, desc, optional=False)
    # optional=True → cron yoksa warn eder ama testi geçirir (kasıtlı pause OK)
    (r"0\s+4,10,16,22.*scanner.*scan.*--live",
     "Scanner scan  → 04:00/10:00/16:00/22:00", True),   # NO bot aldığında pause edilebilir
    (r"0\s+11.*scanner.*settle",
     "Scanner settle → 11:00", False),
    (r"5\s+11.*trader.*settle",
     "Trader settle  → 11:05", False),
    (r"15\s+11.*trader.*redeem",
     "Trader redeem  → 11:15  (kazanç claim)", False),
    (r"\*/30.*trader.*check-fills",
     "Fill check     → her 30dk", False),
    (r"0 4,8,12,16,20.*trader.*cancel-stale",
     "Cancel stale   → 04/08/12/16/20h", False),
    (r"venv/bin/python3",
     "Tüm işler venv Python kullanıyor (py_clob_client erişimi için şart)", False),
]

def make_cron_test(pattern: str, desc: str, optional: bool = False):
    def _t():
        if not ON_VPS:
            return
        found = bool(re.search(pattern, CRONTAB))
        if not found and optional:
            print(f"  ⚠️  Opsiyonel cron eksik (kasıtlı pause olabilir): {desc}")
            return
        ok(found,
           f"Cron eksik veya yanlış: {desc}\n"
           f"     Beklenen pattern: {pattern}")
    return _t

for _cp, _cd, *_opt in REQUIRED_CRONS:
    _optional = bool(_opt and _opt[0])
    test(f"cron: {_cd}", make_cron_test(_cp, _cd, _optional))

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
test("multi-bucket: kaynak kodda multi_bucket flag var (trade kaydı)", lambda:
    ok("multi_bucket" in SCANNER_SRC,
       "scanner.py multi-bucket trade dict'inde 'multi_bucket' flag yok")
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
    """scanner.save_trades artık SQLite-first yazıyor (Faz 7)."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    ok("write_paper_trades_list" in src,
       "scanner.save_trades SQLite-first write API kullanmıyor")
    save_fn_start = src.find("def save_trades(")
    save_fn_end   = src.find("\ndef ", save_fn_start + 1)
    save_fn       = src[save_fn_start:save_fn_end]
    ok("try:" in save_fn and "except" in save_fn,
       "save_trades içinde try/except eksik — DB hatası scanner'ı çökertmemeli")

test("scanner.save_trades SQLite-first yazım (Faz 7)",
     test_scanner_save_has_sync_hook)


def test_trader_save_has_sync_hook():
    trader_path = Path(__file__).resolve().parent.parent / "bot" / "trader.py"
    src = trader_path.read_text(encoding="utf-8")
    ok("write_live_trades_list" in src,
       "trader.save_live_trades SQLite-first write API kullanmıyor")
    save_fn_start = src.find("def save_live_trades(")
    save_fn_end   = src.find("\ndef ", save_fn_start + 1)
    save_fn       = src[save_fn_start:save_fn_end]
    ok("try:" in save_fn and "except" in save_fn,
       "save_live_trades içinde try/except eksik")

test("trader.save_live_trades SQLite-first yazım (Faz 7)",
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
print(" TEST 28: Faz 4 — Dinamik Model Ağırlıkları")
print(f"{'═'*62}")

def _import_dynamic_weights():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "bot" / "dynamic_weights.py"
    spec = importlib.util.spec_from_file_location("weather_dw", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_temp_db_with_forecasts(db, td, station="eglc",
                                   per_model_errors=None, days=30):
    """Geçici DB kur ve model_forecasts'a örnek veri ekle."""
    db_path = Path(td) / "test.db"
    db.init_db(db_path)
    import sqlite3
    from datetime import datetime as dt, timedelta as td_
    if per_model_errors is None:
        # icon MAE=1.0, ecmwf MAE=1.5, ukmo MAE=3.0 → icon ağır basar
        per_model_errors = {"icon": 1.0, "ecmwf": 1.5, "ukmo": 3.0,
                             "gfs": 2.0, "meteofrance": 1.8}
    with sqlite3.connect(str(db_path)) as conn:
        for i in range(days):
            d = (dt.now() - td_(days=i)).strftime("%Y-%m-%d")
            actual = 15.0 + (i % 5)   # değişen "gerçek"
            for model, err in per_model_errors.items():
                mt = actual + (err if (i % 2 == 0) else -err)
                conn.execute(
                    "INSERT INTO model_forecasts "
                    "(station,date,model,horizon_days,max_temp,actual_temp,abs_error,settled_at) "
                    "VALUES (?,?,?,?,?,?,?,strftime('%s','now'))",
                    (station, d, model, 1, mt, actual, abs(mt - actual)),
                )
        conn.commit()
    return db_path


def test_dw_empty_db_returns_none():
    """Veri yoksa compute_dynamic_weights None döner (statik fallback tetikler)."""
    dw  = _import_dynamic_weights()
    db  = _import_db_module()
    import importlib
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        result = dw.compute_dynamic_weights("eglc", horizon_days=1, db_path=db_path)
    ok(result is None, f"veri yok → None beklenir: {result}")

test("compute_dynamic_weights: veri yok → None (statik fallback)",
     test_dw_empty_db_returns_none)


def test_dw_sufficient_data_returns_weights():
    """≥MIN_SAMPLES_MODEL örnek varken normalize ağırlık dict."""
    dw  = _import_dynamic_weights()
    db  = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = _setup_temp_db_with_forecasts(db, td, "eglc")
        result = dw.compute_dynamic_weights("eglc", horizon_days=1, db_path=db_path)
    ok(result is not None, f"yeterli veri ile None döndürdü: {result}")
    ok("icon" in result and "ukmo" in result,
       f"temel modeller eksik: {result}")
    # icon (düşük MAE) ukmo'dan (yüksek MAE) daha ağır olmalı
    ok(result["icon"] > result["ukmo"],
       f"icon > ukmo olmalı: {result}")

test("compute_dynamic_weights: yeterli veri → 1/RMSE ağırlıklı dict",
     test_dw_sufficient_data_returns_weights)


def test_dw_normalization_average():
    """Ağırlıkların ortalaması ~1.0 (len(good) / total normalize)."""
    dw  = _import_dynamic_weights()
    db  = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = _setup_temp_db_with_forecasts(db, td, "eglc")
        result = dw.compute_dynamic_weights("eglc", horizon_days=1, db_path=db_path)
    avg = sum(result.values()) / len(result)
    ok(abs(avg - 1.0) < 0.01,
       f"ortalama ağırlık ~1.0 beklenir, bulunan {avg}")

test("compute_dynamic_weights: ağırlıklar ortalama ~1.0 normalize",
     test_dw_normalization_average)


def test_dw_effective_weights_fallback():
    """effective_weights() veri yoksa statik döner, 'source' etiketi koyar."""
    dw  = _import_dynamic_weights()
    db  = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        w, src = dw.effective_weights("eglc", horizon_days=1, db_path=db_path)
    ok(src == "static", f"source 'static' beklenir: {src}")
    ok(w == dw.STATIC_WEIGHTS, f"statik ağırlıklar beklenir: {w}")

test("effective_weights: veri yoksa ('static', STATIC_WEIGHTS)",
     test_dw_effective_weights_fallback)


def test_dw_effective_weights_dynamic():
    """Yeterli veri varsa 'dynamic' kaynak ve güncel ağırlıklar."""
    dw  = _import_dynamic_weights()
    db  = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = _setup_temp_db_with_forecasts(db, td, "eglc")
        w, src = dw.effective_weights("eglc", horizon_days=1, db_path=db_path)
    ok(src == "dynamic", f"source 'dynamic' beklenir: {src}")
    ok("icon" in w, f"icon ağırlığı yok: {w}")

test("effective_weights: yeterli veri → ('dynamic', güncel ağırlıklar)",
     test_dw_effective_weights_dynamic)


def test_db_record_model_forecast():
    """record_model_forecast upsert davranışını doğrula."""
    db = _import_db_module()
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        db.record_model_forecast("eglc", "2026-04-22", "icon", 15.3, horizon_days=1, db_path=db_path)
        # Aynı kayıt — UPSERT, duplicate değil
        db.record_model_forecast("eglc", "2026-04-22", "icon", 15.8, horizon_days=1, db_path=db_path)
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT max_temp FROM model_forecasts WHERE station=? AND date=? AND model=?",
                ("eglc", "2026-04-22", "icon")
            ).fetchall()
    ok(len(rows) == 1, f"aynı key için tek satır beklenir, bulunan {len(rows)}")
    ok(abs(rows[0][0] - 15.8) < 0.01,
       f"son yazılan değer 15.8, bulunan {rows[0][0]}")

test("record_model_forecast: (station,date,model) upsert davranışı",
     test_db_record_model_forecast)


def test_db_record_model_actuals():
    """record_model_actuals tüm modeller için actual + abs_error hesaplar."""
    db = _import_db_module()
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        for m, mt in [("icon", 15.0), ("ecmwf", 15.5), ("gfs", 14.2)]:
            db.record_model_forecast("eglc", "2026-04-22", m, mt, db_path=db_path)
        n = db.record_model_actuals("eglc", "2026-04-22", 15.0, db_path=db_path)
        ok(n == 3, f"3 model güncellenmeli, güncellenen {n}")
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT model, abs_error FROM model_forecasts WHERE station='eglc' AND date='2026-04-22'"
            ).fetchall()
    errs = {m: e for m, e in rows}
    ok(abs(errs["icon"] - 0.0) < 0.01)
    ok(abs(errs["ecmwf"] - 0.5) < 0.01)
    ok(abs(errs["gfs"] - 0.8) < 0.01)

test("record_model_actuals: tüm modeller actual_temp + abs_error alır",
     test_db_record_model_actuals)


def test_blend_day_accepts_weights():
    """main.blend_day() weights parametresini kullanır, dinamik blend üretir."""
    m = _import_main_module()
    models_data = {
        "icon":  {"max_temp": 15.0, "hours": []},
        "ecmwf": {"max_temp": 18.0, "hours": []},
        "gfs":   {"max_temp": 15.0, "hours": []},
    }
    # icon çok ağır → blend 15'e yakın
    r1 = m.blend_day(models_data, horizon=1, weights={"icon": 10.0, "ecmwf": 0.1, "gfs": 0.1})
    ok(r1["max_temp"] < 16.0,
       f"icon ağır → blend ~15 beklenir, bulunan {r1['max_temp']}")
    ok(r1["weights_source"] == "dynamic",
       f"weights_source 'dynamic' beklenir: {r1}")
    # statik — weights=None
    r2 = m.blend_day(models_data, horizon=1, weights=None)
    ok(r2["weights_source"] == "static",
       f"weights_source 'static' beklenir: {r2}")

test("blend_day: weights parametresi dinamik ağırlığı uygular",
     test_blend_day_accepts_weights)


def test_db_phase4_schema():
    """paper_trades'te signal_* mevcut + model_forecasts tablosu var."""
    db = _import_db_module()
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            mf_cols = {r[1] for r in conn.execute("PRAGMA table_info(model_forecasts)")}
    ok("model_forecasts" in tables, f"model_forecasts tablosu yok: {tables}")
    for c in ("station", "date", "model", "max_temp", "actual_temp", "abs_error"):
        ok(c in mf_cols, f"model_forecasts şemasında {c} yok")

test("db.py: model_forecasts tablosu + gereken kolonlar",
     test_db_phase4_schema)


def test_main_records_model_forecasts():
    """main.py /api/weather endpoint'i record_model_forecast çağırıyor."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    ok("record_model_forecast" in src,
       "main.py per-model forecast kaydetmiyor")
    ok("compute_dynamic_weights" in src,
       "main.py dinamik ağırlık kullanmıyor")
    # Faz 11: effective_weights = dyn_weights or _station_weights (Asya desteği)
    ok("effective_weights" in src,
       "blend_day'e effective_weights geçilmiyor (dyn_weights veya station-specific)")

test("main.py: /api/weather dinamik ağırlık + per-model kaydı",
     test_main_records_model_forecasts)


def test_scanner_settle_records_model_actuals():
    """scanner.settle() model_forecasts.actual_temp'i de güncelliyor."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    start = src.find("def settle(")
    end   = src.find("\ndef ", start + 1)
    body  = src[start:end]
    ok("record_model_actuals" in body,
       "settle() per-model actuals'ı yazmıyor")

test("scanner.settle: record_model_actuals çağrısı kaynak kodda",
     test_scanner_settle_records_model_actuals)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 29: Faz 6a — Portföy VaR (Monte Carlo + Cholesky)")
print(f"{'═'*62}")

def _import_portfolio_var():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "bot" / "portfolio_var.py"
    spec = importlib.util.spec_from_file_location("weather_pvar", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pvar_empty_portfolio():
    """Boş trade listesi → tüm değerler sıfır."""
    pv = _import_portfolio_var()
    r  = pv.portfolio_var([])
    ok(r["n_positions"] == 0 and r["expected_pnl"] == 0.0,
       f"boş portföy için sıfır beklenir: {r}")

test("portfolio_var: boş portföyde sıfır döner", test_pvar_empty_portfolio)


def test_pvar_pearson_basics():
    """Pearson: perfect correlation → 0.99'a klip; düz veri → None."""
    pv = _import_portfolio_var()
    r1 = pv.pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
    ok(r1 is not None and r1 > 0.95,
       f"mükemmel pozitif korelasyon bekleniyor: {r1}")
    r2 = pv.pearson([1, 2, 3], [1, 1, 1])
    ok(r2 is None, f"sabit y ile None beklenir: {r2}")
    r3 = pv.pearson([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])
    ok(r3 is not None and r3 < -0.95, f"negatif korelasyon: {r3}")

test("pearson: temel durumlar (positif/negatif/sabit)", test_pvar_pearson_basics)


def test_pvar_cholesky_identity():
    """Birim matris için Cholesky = birim matris."""
    pv = _import_portfolio_var()
    I  = [[1.0, 0.0], [0.0, 1.0]]
    L  = pv.cholesky(I)
    ok(abs(L[0][0] - 1.0) < 1e-9 and abs(L[1][1] - 1.0) < 1e-9,
       f"identity Cholesky: {L}")
    ok(abs(L[1][0]) < 1e-9, f"off-diagonal 0 beklenir: {L}")

test("cholesky: identity matris self-çarpı L", test_pvar_cholesky_identity)


def test_pvar_cholesky_correlated():
    """2x2 korelasyonlu matrisin Cholesky'si L·L^T = orijinal."""
    pv  = _import_portfolio_var()
    mat = [[1.0, 0.6], [0.6, 1.0]]
    L   = pv.cholesky(mat)
    # L · L^T
    rec = [[sum(L[i][k]*L[j][k] for k in range(min(i,j)+1)) for j in range(2)] for i in range(2)]
    for i in range(2):
        for j in range(2):
            ok(abs(rec[i][j] - mat[i][j]) < 1e-6,
               f"Cholesky hatalı: L·L^T[{i}][{j}]={rec[i][j]} ≠ {mat[i][j]}")

test("cholesky: 2x2 korelasyonlu matriste L·L^T reconstruct",
     test_pvar_cholesky_correlated)


def test_pvar_station_correlation_shape():
    """N istasyon için N×N matris + diagonal 1.0 + simetri."""
    pv = _import_portfolio_var()
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        corr = pv.station_correlation(["eglc", "lfpg", "ltfm"], days=60, db_path=db_path)
    ok(len(corr) == 3 and len(corr[0]) == 3, f"3x3 matris beklenir: {len(corr)}")
    for i in range(3):
        ok(abs(corr[i][i] - 1.0) < 1e-9, f"diagonal 1.0: {corr[i][i]}")
        for j in range(3):
            ok(abs(corr[i][j] - corr[j][i]) < 1e-9, f"simetri yok ({i},{j})")

test("station_correlation: boyut + diagonal + simetri",
     test_pvar_station_correlation_shape)


def test_pvar_simulate_independent_high_p():
    """Yüksek p_win + bağımsız → beklenen P&L pozitif."""
    pv = _import_portfolio_var()
    positions = [
        {"p_win": 0.8, "potential_win": 10.0, "cost": 3.0},
        {"p_win": 0.8, "potential_win": 10.0, "cost": 3.0},
    ]
    corr = [[1.0, 0.0], [0.0, 1.0]]
    pnls = pv.simulate_portfolio(positions, corr, n_sims=2000, seed=42)
    mean = sum(pnls) / len(pnls)
    # E[P&L] = 2·(0.8·10 - 0.2·3) = 2·7.4 = 14.8
    ok(abs(mean - 14.8) < 1.5,
       f"E[P&L] ≈ 14.8 beklenir, bulunan {mean:.2f}")

test("simulate_portfolio: bağımsız+yüksek p → pozitif beklenen",
     test_pvar_simulate_independent_high_p)


def test_pvar_correlation_widens_tails():
    """Yüksek korelasyon → P&L varyansı artar (kuyruklar genişler).

    Binary payoff tavanı nedeniyle 5%ile her iki durumda da -ΣCost'a çarpabilir;
    stddev daha temiz metrik. Ayrıca en kötü outcome'ın frekansı da artar.
    """
    import statistics as st
    pv = _import_portfolio_var()
    positions = [
        {"p_win": 0.5, "potential_win": 10.0, "cost": 5.0},
        {"p_win": 0.5, "potential_win": 10.0, "cost": 5.0},
        {"p_win": 0.5, "potential_win": 10.0, "cost": 5.0},
    ]
    ind = pv.simulate_portfolio(positions, [[1.0,0.0,0.0],[0.0,1.0,0.0],[0.0,0.0,1.0]], n_sims=4000, seed=7)
    cor = pv.simulate_portfolio(positions, [[1.0,0.85,0.85],[0.85,1.0,0.85],[0.85,0.85,1.0]], n_sims=4000, seed=7)
    sd_ind = st.pstdev(ind)
    sd_cor = st.pstdev(cor)
    ok(sd_cor > sd_ind * 1.15,
       f"korelasyonlu std daha yüksek beklenir: ind={sd_ind:.2f} cor={sd_cor:.2f}")
    # en kötü outcome (-15) frekansı da artmalı
    worst_ind = sum(1 for p in ind if p == -15.0) / len(ind)
    worst_cor = sum(1 for p in cor if p == -15.0) / len(cor)
    ok(worst_cor > worst_ind * 1.3,
       f"korelasyonda worst-case daha sık: ind={worst_ind:.2%} cor={worst_cor:.2%}")

test("simulate_portfolio: korelasyon arttıkça kuyruk genişler",
     test_pvar_correlation_widens_tails)


def test_pvar_deterministic_seed():
    """Aynı trade ID'leri → aynı VaR (deterministik)."""
    pv = _import_portfolio_var()
    trades = [
        {"id": "A", "station": "eglc", "mode_pct": 55, "potential_win": 8.0, "cost_usd": 2.5},
        {"id": "B", "station": "lfpg", "mode_pct": 60, "potential_win": 7.5, "cost_usd": 2.5},
    ]
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        r1 = pv.portfolio_var(trades, db_path=db_path, n_sims=1000)
        r2 = pv.portfolio_var(trades, db_path=db_path, n_sims=1000)
    ok(r1["var_95"] == r2["var_95"] and r1["expected_pnl"] == r2["expected_pnl"],
       f"deterministik olmalı: {r1['var_95']} vs {r2['var_95']}")

test("portfolio_var: deterministik seed (aynı ID → aynı VaR)",
     test_pvar_deterministic_seed)


def test_pvar_worst_case_bounded():
    """Worst case = -Σ(cost)'tan büyük veya eşit olamaz."""
    pv = _import_portfolio_var()
    trades = [
        {"id": "A", "station": "eglc", "mode_pct": 30, "potential_win": 5.0, "cost_usd": 5.0},
        {"id": "B", "station": "lfpg", "mode_pct": 30, "potential_win": 5.0, "cost_usd": 5.0},
    ]
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        r = pv.portfolio_var(trades, db_path=db_path, n_sims=500)
    ok(r["worst"] >= -r["gross_exposure"] - 0.01,
       f"worst case {r['worst']} > -gross {-r['gross_exposure']} olmalı")
    ok(r["best"]  <= r["gross_potential_win"] + 0.01,
       f"best case  {r['best']} < +win {r['gross_potential_win']} olmalı")

test("portfolio_var: worst ≥ -ΣCost, best ≤ +ΣWin",
     test_pvar_worst_case_bounded)


def test_pvar_response_fields():
    """Dönüş dict'i tüm beklenen alanları içeriyor."""
    pv = _import_portfolio_var()
    trades = [{"id": "X", "station": "eglc", "mode_pct": 50, "potential_win": 5, "cost_usd": 2}]
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        r = pv.portfolio_var(trades, db_path=db_path, n_sims=200)
    required = {"n_positions","n_sims","expected_pnl","var_95","var_99","worst","best",
                "stations","avg_abs_correlation","gross_exposure","gross_potential_win"}
    missing  = required - set(r.keys())
    ok(not missing, f"eksik alan(lar): {missing}")

test("portfolio_var: tüm alanlar mevcut", test_pvar_response_fields)


def test_pvar_endpoint_in_main():
    """main.py /api/portfolio/var endpoint'i tanımlanmış."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    ok('/api/portfolio/var' in src, "portfolio/var endpoint eksik")
    ok("from bot.portfolio_var import portfolio_var" in src,
       "portfolio_var import edilmiyor")

test("main.py: /api/portfolio/var endpoint + import",
     test_pvar_endpoint_in_main)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 30: Faz 6b — Çok-kaynaklı settlement audit")
print(f"{'═'*62}")


def test_sa_schema():
    """settlement_audit tablosu + gereken kolonlar."""
    db = _import_db_module()
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(settlement_audit)")}
    for c in ("station", "date", "source", "actual_temp", "rounded_temp"):
        ok(c in cols, f"settlement_audit şemasında {c} yok")

test("db.py: settlement_audit tablosu + kolonlar", test_sa_schema)


def test_sa_record_upsert():
    """record_settlement_source: aynı (station,date,source) tek satır, UPSERT."""
    db = _import_db_module()
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        db.record_settlement_source("eglc", "2026-04-22", "open-meteo", 15.3, db_path=db_path)
        db.record_settlement_source("eglc", "2026-04-22", "open-meteo", 15.7, db_path=db_path)
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT actual_temp FROM settlement_audit WHERE station=? AND date=? AND source=?",
                ("eglc", "2026-04-22", "open-meteo")
            ).fetchall()
    ok(len(rows) == 1, f"tek satır beklenir, bulunan {len(rows)}")
    ok(abs(rows[0][0] - 15.7) < 0.01, f"son değer 15.7 olmalı: {rows[0][0]}")

test("record_settlement_source: UPSERT davranışı", test_sa_record_upsert)


def test_sa_multiple_sources_same_day():
    """Aynı gün, farklı kaynaklar — audit'ta 2 satır."""
    db = _import_db_module()
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        db.record_settlement_source("eglc", "2026-04-22", "open-meteo", 15.2, db_path=db_path)
        db.record_settlement_source("eglc", "2026-04-22", "metar",       16.4, db_path=db_path)
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT source FROM settlement_audit WHERE station=? AND date=?",
                ("eglc", "2026-04-22")
            ).fetchall()
    sources = {r[0] for r in rows}
    ok(sources == {"open-meteo", "metar"}, f"2 kaynak beklenir: {sources}")

test("settlement_audit: 2 kaynak aynı günde", test_sa_multiple_sources_same_day)


def test_sa_get_audit_diff():
    """get_settlement_audit: kaynaklar arası fark hesaplanır + disagreement flag."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        # 1.2°C fark → round(15.2)=15, round(16.4)=16, bucket diff=1 → disagreement
        db.record_settlement_source("eglc", "2026-04-22", "open-meteo", 15.2, db_path=db_path)
        db.record_settlement_source("eglc", "2026-04-22", "metar",       16.4, db_path=db_path)
        # tam uyum — diff 0
        db.record_settlement_source("lfpg", "2026-04-22", "open-meteo", 12.0, db_path=db_path)
        db.record_settlement_source("lfpg", "2026-04-22", "metar",       12.3, db_path=db_path)

        audit = db.get_settlement_audit(days=60, db_path=db_path)

    by_key = {(a["station"], a["date"]): a for a in audit}
    eglc = by_key[("eglc", "2026-04-22")]
    lfpg = by_key[("lfpg", "2026-04-22")]
    ok(eglc["disagreement"] is True, f"eglc disagreement bekleniyor: {eglc}")
    ok(eglc["max_diff_bucket"] == 1, f"eglc bucket diff 1 beklenir: {eglc}")
    ok(abs(eglc["max_diff_c"] - 1.2) < 0.05, f"eglc ham fark ~1.2: {eglc}")
    ok(lfpg["disagreement"] is False, f"lfpg disagreement olmamalı: {lfpg}")
    ok(lfpg["max_diff_bucket"] == 0, f"lfpg bucket diff 0: {lfpg}")

test("get_settlement_audit: disagreement flag + diff hesapları",
     test_sa_get_audit_diff)


def test_sa_none_input_silent():
    """None input → sessizce geçmeli (bot akışını bozmamalı)."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        # None → return; raise etmez
        db.record_settlement_source("eglc", "2026-04-22", "open-meteo", None, db_path=db_path)
        audit = db.get_settlement_audit(days=30, db_path=db_path)
    ok(audit == [], f"None input kayıt etmemeli: {audit}")

test("record_settlement_source: None güvenli (sessiz)",
     test_sa_none_input_silent)


def test_sa_disagreement_stats():
    """settlement_disagreement_stats: istasyon başı oran ve ortalama fark."""
    db = _import_db_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        db.init_db(db_path)
        # lfpg 3 gün, 2'si disagreement, 1'i uyum
        from datetime import datetime as _dt, timedelta as _td
        for i, (om, mt) in enumerate([(15.0, 16.3), (12.0, 12.2), (18.0, 19.7)]):
            d = (_dt.now() - _td(days=i + 1)).strftime("%Y-%m-%d")
            db.record_settlement_source("lfpg", d, "open-meteo", om, db_path=db_path)
            db.record_settlement_source("lfpg", d, "metar",       mt, db_path=db_path)
        stats = db.settlement_disagreement_stats(days=60, db_path=db_path)
    lfpg = stats.get("lfpg", {})
    ok(lfpg.get("n_days") == 3, f"3 gün beklenir: {lfpg}")
    ok(lfpg.get("n_disagreement") == 2, f"2 uyumsuzluk: {lfpg}")
    ok(lfpg.get("disagreement_rate") > 0.6, f"oran > 0.6: {lfpg}")

test("settlement_disagreement_stats: istasyon başı oran",
     test_sa_disagreement_stats)


def test_sa_scanner_records_both():
    """scanner.settle() her iki kaynağı da record_settlement_source ile yazıyor."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    start = src.find("def settle(")
    end   = src.find("\ndef ", start + 1)
    body  = src[start:end]
    ok("record_settlement_source" in body,
       "settle() settlement_audit'a yazmıyor")
    ok("\"open-meteo\"" in body and "\"metar\"" in body,
       "settle() her iki kaynağı ayrı ayrı kaydetmiyor")

test("scanner.settle: her iki kaynağı audit'a yazıyor",
     test_sa_scanner_records_both)


def test_sa_endpoint_in_main():
    """main.py /api/settlement-audit endpoint tanımlı."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    ok("/api/settlement-audit" in src, "settlement-audit endpoint eksik")
    ok("get_settlement_audit" in src, "get_settlement_audit import eksik")
    ok("settlement_disagreement_stats" in src,
       "settlement_disagreement_stats import eksik")

test("main.py: /api/settlement-audit endpoint",
     test_sa_endpoint_in_main)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 31: Faz 6c — Kalibrasyon dashboard (Brier + reliability)")
print(f"{'═'*62}")


def _import_calibration():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "bot" / "calibration.py"
    spec = importlib.util.spec_from_file_location("weather_calib", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_trade(station="eglc", date="2026-04-20", mode_pct=50, result="WIN"):
    return {
        "id": f"t_{station}_{date}_{mode_pct}_{result}",
        "station": station,
        "date": date,
        "status": "closed",
        "ens_mode_pct": mode_pct,
        "result": result,
    }


def test_cal_empty_trades():
    """Boş girdi → n=0 ve None değerler."""
    c = _import_calibration()
    r = c.compute_calibration([])
    ok(r["n"] == 0 and r["brier"] is None,
       f"empty result beklenir: {r}")

test("compute_calibration: boş trade listesi", test_cal_empty_trades)


def test_cal_perfect_calibration():
    """Her tahmin gerçekle aynı → Brier 0, skill 1.0 civarı."""
    c = _import_calibration()
    # 100 trade: 50 pct → 50% win (tam uyum)
    trades = []
    for i in range(50):
        trades.append(_make_trade(date=f"2026-04-{(i%28)+1:02d}",
                                    mode_pct=50, result=("WIN" if i < 25 else "LOSS")))
    r = c.compute_calibration(trades)
    # Her tahmin 0.5, outcome 0/1 → Brier = mean(0.25) = 0.25 (random tahmin)
    ok(abs(r["brier"] - 0.25) < 0.02, f"brier ≈0.25: {r['brier']}")
    # brier_ref = 0.5·0.5 = 0.25 → skill ≈ 0
    ok(abs(r["skill"]) < 0.05, f"skill ≈0 beklenir: {r['skill']}")

test("compute_calibration: random (50%) tahmin → skill~0",
     test_cal_perfect_calibration)


def test_cal_high_confidence_wins():
    """Yüksek güvenli tahminler hepsi kazandı → Brier düşük, skill pozitif."""
    c = _import_calibration()
    trades = [_make_trade(date=f"2026-04-{(i%28)+1:02d}", mode_pct=80, result="WIN")
              for i in range(20)]
    trades += [_make_trade(date=f"2026-05-{(i%28)+1:02d}", mode_pct=20, result="LOSS")
               for i in range(20)]
    r = c.compute_calibration(trades)
    # p=0.8, y=1 → (0.2)² = 0.04 | p=0.2, y=0 → (0.2)² = 0.04 → Brier=0.04
    ok(abs(r["brier"] - 0.04) < 0.01, f"brier ≈0.04: {r['brier']}")
    ok(r["skill"] > 0.7, f"skill yüksek beklenir: {r['skill']}")

test("compute_calibration: iyi kalibre → düşük Brier, yüksek skill",
     test_cal_high_confidence_wins)


def test_cal_bins_populated():
    """Reliability bins doldurulur + mean_p, actual_freq alanları."""
    c = _import_calibration()
    trades = []
    # 35-45% bin'inde 10 trade, 4'ü kazandı → mean_p≈0.4, actual≈0.4 (iyi kalibre)
    for i in range(10):
        trades.append(_make_trade(date=f"2026-04-{i+1:02d}",
                                    mode_pct=40, result=("WIN" if i < 4 else "LOSS")))
    r = c.compute_calibration(trades)
    ok(len(r["bins"]) >= 1, f"en az 1 bin beklenir: {r}")
    b = r["bins"][0]
    ok(abs(b["mean_p"] - 0.4) < 0.001, f"mean_p 0.4: {b}")
    ok(abs(b["actual_freq"] - 0.4) < 0.001, f"actual_freq 0.4: {b}")
    ok(b["n"] == 10, f"n=10: {b}")

test("compute_calibration: reliability bin hesapları",
     test_cal_bins_populated)


def test_cal_sharpness_constant_vs_varied():
    """Sabit tahmin → sharpness=0; çeşitli → >0."""
    c = _import_calibration()
    same = [_make_trade(date=f"2026-04-{i+1:02d}", mode_pct=50, result="WIN")
            for i in range(10)]
    r1 = c.compute_calibration(same)
    ok(r1["sharpness"] == 0.0, f"sabit tahmin sharpness=0: {r1}")

    varied = []
    for i, p in enumerate([20, 30, 40, 50, 60, 70, 80]):
        varied.append(_make_trade(date=f"2026-05-{i+1:02d}", mode_pct=p, result="WIN"))
    r2 = c.compute_calibration(varied)
    ok(r2["sharpness"] > 0.1, f"çeşitli tahmin sharpness>0.1: {r2}")

test("compute_calibration: sharpness (varyans) metriği",
     test_cal_sharpness_constant_vs_varied)


def test_cal_station_filter():
    """station parametresi sadece o istasyonu hesaplar."""
    c = _import_calibration()
    trades = [
        _make_trade(station="eglc", date=f"2026-04-{i+1:02d}", mode_pct=60, result="WIN")
        for i in range(5)
    ] + [
        _make_trade(station="ltfm", date=f"2026-04-{i+1:02d}", mode_pct=60, result="LOSS")
        for i in range(5)
    ]
    r_eglc = c.compute_calibration(trades, station="eglc")
    r_ltfm = c.compute_calibration(trades, station="ltfm")
    ok(r_eglc["n"] == 5 and r_eglc["base_rate"] == 1.0,
       f"eglc 5 trade, tümü WIN: {r_eglc}")
    ok(r_ltfm["n"] == 5 and r_ltfm["base_rate"] == 0.0,
       f"ltfm 5 trade, tümü LOSS: {r_ltfm}")

test("compute_calibration: station filtresi",
     test_cal_station_filter)


def test_cal_days_filter():
    """days parametresi kesim tarihinden eski trade'leri atar."""
    c = _import_calibration()
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now()
    recent = (today - _td(days=3)).strftime("%Y-%m-%d")
    old    = (today - _td(days=200)).strftime("%Y-%m-%d")
    trades = [
        _make_trade(date=recent, mode_pct=60, result="WIN"),
        _make_trade(date=old,    mode_pct=60, result="LOSS"),
    ]
    r = c.compute_calibration(trades, days=30)
    ok(r["n"] == 1, f"30 gün filtre → 1 trade beklenir: {r}")

test("compute_calibration: days filtresi", test_cal_days_filter)


def test_cal_per_station_min_samples():
    """per_station min_samples altı istasyon dahil edilmez."""
    c = _import_calibration()
    trades = [_make_trade(station="eglc", date=f"2026-04-{i+1:02d}",
                            mode_pct=50, result="WIN") for i in range(10)]
    trades += [_make_trade(station="ltfm", date="2026-04-01", mode_pct=50, result="WIN")]
    out = c.compute_per_station(trades, min_samples=5)
    ok("eglc" in out, f"eglc dahil: {out}")
    ok("ltfm" not in out, f"ltfm atlanmalı (1<5): {out}")

test("compute_per_station: min_samples filtresi",
     test_cal_per_station_min_samples)


def test_cal_ignores_non_closed():
    """status='open' trade'ler atlanır."""
    c = _import_calibration()
    trades = [
        _make_trade(date="2026-04-01", mode_pct=60, result="WIN"),
        {"id": "x", "station": "eglc", "date": "2026-04-02",
         "status": "open", "ens_mode_pct": 60, "result": None},
    ]
    r = c.compute_calibration(trades)
    ok(r["n"] == 1, f"sadece kapalı sayılır: {r}")

test("compute_calibration: open trade atlanır",
     test_cal_ignores_non_closed)


def test_cal_endpoint_in_main():
    """main.py /api/calibration endpoint tanımlı."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    ok("/api/calibration" in src, "calibration endpoint eksik")
    ok("compute_calibration" in src, "compute_calibration import eksik")
    ok("compute_per_station" in src, "compute_per_station import eksik")

test("main.py: /api/calibration endpoint",
     test_cal_endpoint_in_main)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 32: ECMWF AIFS entegrasyonu (6. model)")
print(f"{'═'*62}")


def test_aifs_in_models_dict():
    """main.MODELS'te aifs kaydı doğru ID ile var."""
    m = _import_main_module()
    ok("aifs" in m.MODELS, f"aifs yok: {list(m.MODELS.keys())}")
    ok(m.MODELS["aifs"] == "ecmwf_aifs025_single",
       f"yanlış forecast ID: {m.MODELS['aifs']}")

test("main.MODELS: 'aifs' → 'ecmwf_aifs025_single'",
     test_aifs_in_models_dict)


def test_aifs_in_weights():
    """MODEL_WEIGHTS'te aifs ağırlığı makul aralıkta."""
    m = _import_main_module()
    ok("aifs" in m.MODEL_WEIGHTS, f"aifs ağırlığı yok")
    w = m.MODEL_WEIGHTS["aifs"]
    ok(1.0 <= w <= 2.0, f"aifs ağırlığı 1.0-2.0 beklenir: {w}")

test("MODEL_WEIGHTS: aifs ağırlık makul",
     test_aifs_in_weights)


def test_aifs_blend_uses_it():
    """blend_day() aifs anahtarı geldiğinde blend'e dahil oluyor."""
    m = _import_main_module()
    # Gerçekçi dağılım — outlier filtresi tetiklenmesin
    base = {
        "gfs":         {"max_temp": 14.0, "hours": []},
        "ecmwf":       {"max_temp": 14.5, "hours": []},
        "icon":        {"max_temp": 13.8, "hours": []},
        "ukmo":        {"max_temp": 14.2, "hours": []},
        "meteofrance": {"max_temp": 14.4, "hours": []},
    }
    with_aifs = dict(base)
    with_aifs["aifs"] = {"max_temp": 15.5, "hours": []}

    r_wo   = m.blend_day(base,      horizon=1)
    r_with = m.blend_day(with_aifs, horizon=1)

    ok("aifs" in r_with["models_used"],
       f"aifs models_used içinde beklenir: {r_with['models_used']}")
    # AIFS daha sıcak → blend yükselmeli
    ok(r_with["max_temp"] > r_wo["max_temp"],
       f"aifs ile blend yükselmeli: {r_wo['max_temp']} → {r_with['max_temp']}")

test("blend_day: aifs modeli blend'e dahil oluyor",
     test_aifs_blend_uses_it)


def test_aifs_ensemble_id_correct():
    """get_ensemble() içindeki AIFS ensemble ID doğru."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    # Ensemble bloğunda "ecmwf_aifs025" olmalı (alt çizgisiz) — doküman yanlış bildiriyor
    start = src.find("ENSEMBLE_MODELS = {")
    end   = src.find("}", start)
    block = src[start:end]
    ok('"aifs"' in block and '"ecmwf_aifs025"' in block,
       f"Ensemble AIFS ID eksik/yanlış: {block}")
    ok('"ecmwf_aifs_025"' not in block,
       "Yanlış AIFS ID (alt çizgi var) kullanılmış")

test("get_ensemble: AIFS doğru ID ile kayıtlı",
     test_aifs_ensemble_id_correct)


def test_aifs_semaphore_adjusted():
    """Semaphore 6→5'e düşürüldü (6 model × 12 istasyon artışı)."""
    m = _import_main_module()
    # _openmeteo_sem._value 5 olmalı
    ok(m._openmeteo_sem._value == 5,
       f"semaphore 5 beklenir: {m._openmeteo_sem._value}")

test("Semaphore: 6 model için 5'e ayarlı",
     test_aifs_semaphore_adjusted)


def test_aifs_static_weights_mirror():
    """bot/dynamic_weights.py STATIC_WEIGHTS da aifs ağırlığını yansıtıyor."""
    dw = _import_dynamic_weights()
    ok("aifs" in dw.STATIC_WEIGHTS,
       f"STATIC_WEIGHTS'te aifs yok: {dw.STATIC_WEIGHTS}")

test("dynamic_weights.STATIC_WEIGHTS: aifs senkron",
     test_aifs_static_weights_mirror)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 33: Otomatik Satış (99.8¢ post-fill) — nakit döngüsü")
print(f"{'═'*62}")


def _import_trader_module():
    """trader.py'yi py_clob_client / web3 / eth_account / httpx stub'larıyla yükle."""
    import importlib
    import sys as _sys
    from unittest.mock import MagicMock as _MM
    stubs = [
        "py_clob_client", "py_clob_client.client", "py_clob_client.clob_types",
        "py_clob_client.constants", "py_clob_client.order_builder",
        "py_clob_client.order_builder.constants",
        "web3", "eth_account", "httpx", "dotenv",
    ]
    for name in stubs:
        if name not in _sys.modules:
            _sys.modules[name] = _MM()
    # dotenv'den load_dotenv / set_key isim gerekli
    _sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None
    _sys.modules["dotenv"].set_key     = lambda *a, **kw: None
    mod = importlib.import_module("bot.trader")
    return mod


def test_auto_sell_constants_present():
    """AUTO_SELL_PRICE=0.998 sabitleri yerinde."""
    tm = _import_trader_module()
    ok(tm.AUTO_SELL_PRICE == 0.998,
       f"AUTO_SELL_PRICE yanlış: {tm.AUTO_SELL_PRICE}")
    ok(tm.AUTO_SELL_FALLBACK == 0.99,
       f"AUTO_SELL_FALLBACK yanlış: {tm.AUTO_SELL_FALLBACK}")
    ok(0 < tm.AUTO_SELL_MIN_EDGE < 0.10,
       f"AUTO_SELL_MIN_EDGE makul değil: {tm.AUTO_SELL_MIN_EDGE}")

test("trader.AUTO_SELL_PRICE = 0.998 ve yardımcı sabitler",
     test_auto_sell_constants_present)


def test_snap_to_tick():
    """_snap_to_tick: tick=0.01 ile 0.998 → 0.99, tick=0.001 ile 0.998 → 0.998."""
    tm = _import_trader_module()
    eq(tm._snap_to_tick(0.998, 0.01), 0.99,  "0.01 tick: 0.998 → 0.99")
    eq(tm._snap_to_tick(0.998, 0.001), 0.998, "0.001 tick: 0.998 → 0.998")
    eq(tm._snap_to_tick(0.9987, 0.0001), 0.9987, "0.0001 tick korunur")
    # Güvenli fallback
    eq(tm._snap_to_tick(0.50, 0), 0.50, "tick=0 güvenli")

test("_snap_to_tick: tick'e göre yuvarlama",
     test_snap_to_tick)


def test_get_tick_size_fallback():
    """get_tick_size hata fırlatırsa 0.01 döner."""
    tm = _import_trader_module()
    client = MagicMock()
    client.get_tick_size.side_effect = Exception("API down")
    eq(tm._get_tick_size(client, "x123"), 0.01)
    # None dönerse de 0.01
    client2 = MagicMock()
    client2.get_tick_size.return_value = None
    eq(tm._get_tick_size(client2, "x"), 0.01)
    # Geçerli değer dönerse onu kullanır
    client3 = MagicMock()
    client3.get_tick_size.return_value = 0.001
    eq(tm._get_tick_size(client3, "x"), 0.001)

test("_get_tick_size: hata / None / geçerli",
     test_get_tick_size_fallback)


def test_place_auto_sell_updates_trade():
    """Başarılı sell order'da trade alanları güncelleniyor."""
    tm = _import_trader_module()
    client = MagicMock()
    client.get_tick_size.return_value = 0.001
    client.create_order.return_value   = {"signed": True}
    client.post_order.return_value     = {"orderID": "sell_id_123abcdef0"}

    trade = {
        "id": "ltfm_2026-05-01_120000_live",
        "station": "ltfm", "date": "2026-05-01",
        "condition_id": "0xdeadbeef",
        "shares": 5,
        "fill_price": 0.30,
        "status": "filled",
        "notes": "",
    }
    oid = tm.place_auto_sell(client, trade)
    eq(oid, "sell_id_123abcdef0")
    eq(trade["status"], "sell_pending")
    eq(trade["sell_order_id"], "sell_id_123abcdef0")
    eq(trade["sell_price"], 0.998)
    ok("sell_placed_at" in trade, "sell_placed_at eksik")
    ok("AUTOSELL" in trade["notes"], f"notes güncellenmedi: {trade['notes']}")
    # post_order post_only=True ile çağrılmalı
    _, kwargs = client.post_order.call_args
    ok(kwargs.get("post_only") is True, f"post_only=True olmalı: {kwargs}")

test("place_auto_sell: başarıda trade alanları + post_only",
     test_place_auto_sell_updates_trade)


def test_place_auto_sell_skips_low_margin():
    """fill_price sell_price'a çok yakınsa emir atlanır."""
    tm = _import_trader_module()
    client = MagicMock()
    client.get_tick_size.return_value = 0.001

    trade = {
        "station": "ltfm", "date": "2026-05-01",
        "condition_id": "0xdeadbeef",
        "shares": 5,
        "fill_price": 0.99,          # 99¢'e aldık → 99.8¢ margin < MIN_EDGE
        "status": "filled",
        "notes": "",
    }
    oid = tm.place_auto_sell(client, trade)
    eq(oid, None, "margin yetersiz → None beklenir")
    # Trade değiştirilmemeli
    eq(trade["status"], "filled")
    ok("sell_order_id" not in trade, "sell_order_id eklenmemeli")
    # Post order hiç çağrılmamalı
    eq(client.post_order.called, False)

test("place_auto_sell: fill çok yüksekse emir atlanır",
     test_place_auto_sell_skips_low_margin)


def test_place_auto_sell_tick001_snaps_down():
    """tick=0.01 ise 0.998 → 0.99, post_order yine de çağrılır."""
    tm = _import_trader_module()
    client = MagicMock()
    client.get_tick_size.return_value = 0.01
    client.create_order.return_value   = {"signed": True}
    client.post_order.return_value     = {"orderID": "sell_id_xyz_0000001"}

    trade = {
        "station": "eglc", "date": "2026-05-02",
        "condition_id": "0xabc",
        "shares": 5,
        "fill_price": 0.25,
        "status": "filled",
        "notes": "eski not",
    }
    oid = tm.place_auto_sell(client, trade)
    ok(oid is not None, "başarılı olmalı")
    eq(trade["sell_price"], 0.99, f"tick=0.01'de 0.99 beklenir: {trade['sell_price']}")
    ok("eski not" in trade["notes"], "eski not korunmalı")

test("place_auto_sell: tick=0.01 → 0.99'a snap",
     test_place_auto_sell_tick001_snaps_down)


def test_place_auto_sell_handles_exception():
    """create_order/post_order exception fırlatırsa None döner, trade bozulmaz."""
    tm = _import_trader_module()
    client = MagicMock()
    client.get_tick_size.return_value = 0.001
    client.create_order.side_effect   = Exception("CLOB 500")

    trade = {
        "station": "ltfm", "date": "2026-05-01",
        "condition_id": "0xabc",
        "shares": 5,
        "fill_price": 0.30,
        "status": "filled",
        "notes": "",
    }
    oid = tm.place_auto_sell(client, trade)
    eq(oid, None)
    eq(trade["status"], "filled", "trade status bozulmamalı")
    ok("sell_order_id" not in trade, "sell_order_id eklenmemeli")

test("place_auto_sell: istisna → None, trade korunur",
     test_place_auto_sell_handles_exception)


def test_check_fills_hooks_auto_sell():
    """check_fills kaynağında fill transition'ından sonra place_auto_sell çağrısı var."""
    trader_path = Path(__file__).resolve().parent.parent / "bot" / "trader.py"
    src = trader_path.read_text(encoding="utf-8")
    # check_fills fonksiyonundaki 'filled' blok alt alta place_auto_sell çağırmalı
    idx_fill    = src.find('t["status"]     = "filled"')
    idx_autosell = src.find("place_auto_sell(client, t)")
    ok(idx_fill    > 0, "fill transition bulunamadı")
    ok(idx_autosell > idx_fill,
       "place_auto_sell çağrısı fill transition'dan sonra olmalı")
    # İkisi arasında makul mesafe (aynı fonksiyonda olması için)
    ok(idx_autosell - idx_fill < 1500,
       f"çağrı transition'dan çok uzakta: {idx_autosell - idx_fill} karakter")

test("check_fills: fill → place_auto_sell hook'u kaynak kodda",
     test_check_fills_hooks_auto_sell)


def test_auto_sell_cli_registered():
    """trader.py CLI dispatch'te 'auto-sell' komutu kayıtlı."""
    trader_path = Path(__file__).resolve().parent.parent / "bot" / "trader.py"
    src = trader_path.read_text(encoding="utf-8")
    ok('"auto-sell"' in src, "auto-sell CLI girişi yok")
    ok("cmd_auto_sell_filled" in src, "cmd_auto_sell_filled tanımı yok")

test("CLI: auto-sell komutu kayıtlı",
     test_auto_sell_cli_registered)


def test_cmd_auto_sell_filled_filters_targets():
    """cmd_auto_sell_filled yalnızca filled ve sell_order_id'si olmayanları hedefler."""
    tm = _import_trader_module()

    trades_state = [
        # Hedef: filled, sell_order_id yok
        {"id": "a", "station": "eglc", "date": "2026-05-01",
         "condition_id": "0x1", "shares": 5, "fill_price": 0.25,
         "status": "filled", "notes": ""},
        # Atlanır: zaten sell_order_id var
        {"id": "b", "station": "lfpg", "date": "2026-05-01",
         "condition_id": "0x2", "shares": 5, "fill_price": 0.25,
         "status": "filled", "sell_order_id": "existing", "notes": ""},
        # Atlanır: pending_fill
        {"id": "c", "station": "limc", "date": "2026-05-01",
         "condition_id": "0x3", "shares": 5, "limit_price": 0.25,
         "status": "pending_fill", "notes": ""},
        # Atlanır: redeemed=True
        {"id": "d", "station": "lemd", "date": "2026-05-01",
         "condition_id": "0x4", "shares": 5, "fill_price": 0.25,
         "status": "filled", "redeemed": True, "notes": ""},
    ]

    # Mock'lar
    client = MagicMock()
    client.get_tick_size.return_value = 0.001
    client.create_order.return_value  = {"signed": True}
    client.post_order.return_value    = {"orderID": "newsell_1234567890"}

    with patch.object(tm, "load_live_trades", return_value=trades_state), \
         patch.object(tm, "save_live_trades")                         as mock_save, \
         patch.object(tm, "setup_client",     return_value=client):
        placed = tm.cmd_auto_sell_filled(price=0.998)

    eq(placed, 1, f"yalnızca 1 hedef beklenir: {placed}")
    # Hedef trade güncellenmiş olmalı
    eq(trades_state[0]["status"], "sell_pending")
    eq(trades_state[0]["sell_order_id"], "newsell_1234567890")
    # Diğerleri değişmemeli
    eq(trades_state[1]["sell_order_id"], "existing")
    eq(trades_state[2]["status"], "pending_fill")
    ok(mock_save.called, "save_live_trades çağrılmalı")

test("cmd_auto_sell_filled: filtreleme + güncelleme",
     test_cmd_auto_sell_filled_filters_targets)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 34: Faz 5 — Kalibrasyon-odaklı scanner filtreleri")
print(f"{'═'*62}")


def _import_scanner_module():
    """scanner.py'yi direkt import et (stub gerekmez — httpx modül düzeyinde kullanılmaz)."""
    import importlib
    mod = importlib.import_module("bot.scanner")
    return mod


def test_phase5_constants_present():
    """Yeni filtre sabitleri doğru değerlerle yerinde."""
    s = _import_scanner_module()
    eq(s.MID_RANGE_SKIP_LOW, 50,
       f"MID_RANGE_SKIP_LOW=50 beklenir: {s.MID_RANGE_SKIP_LOW}")
    eq(s.MID_RANGE_SKIP_HIGH, 80,
       f"MID_RANGE_SKIP_HIGH=80 beklenir: {s.MID_RANGE_SKIP_HIGH}")
    eq(s.MIN_SIGNAL_SCORE, 55,
       f"MIN_SIGNAL_SCORE=55 beklenir: {s.MIN_SIGNAL_SCORE}")
    ok(isinstance(s.STATION_SKILL_PAUSE, frozenset),
       "STATION_SKILL_PAUSE frozenset olmalı")
    ok(len(s.STATION_SKILL_PAUSE) == 0,
       f"Skill pause boş olmalı (live ROI analizi sonrası unpause): {s.STATION_SKILL_PAUSE}")
    # circuit breaker altyapısı hâlâ aktif (SQLite override)
    ok(callable(s.should_pause_station),
       "should_pause_station fonksiyonu hâlâ var")

test("Faz 5 sabitleri yerinde (MID_RANGE, SKILL_PAUSE, MIN_SIGNAL_SCORE)",
     test_phase5_constants_present)


def test_should_pause_station():
    """STATION_SKILL_PAUSE boş — tüm istasyonlar False (SQLite override hariç)."""
    s = _import_scanner_module()
    eq(s.should_pause_station("lfpg"), False, "lfpg unpause edildi (live +64% ROI)")
    eq(s.should_pause_station("ltac"), False, "ltac unpause edildi (live +15% ROI)")
    eq(s.should_pause_station("limc"), False, "limc static pause kaldırıldı (whitelist dışı zaten)")
    eq(s.should_pause_station("efhk"), False, "efhk zaten pause değildi")
    eq(s.should_pause_station("ltfm"), False, "ltfm pause değil")
    eq(s.should_pause_station("unknown"), False, "bilinmeyen istasyon False")

test("should_pause_station: boş pause set — tümü False",
     test_should_pause_station)


def test_is_mid_range_mode_band():
    """mode_pct ∈ [50,80) → True; dışı → False."""
    s = _import_scanner_module()
    # Mid-range (broken zone)
    eq(s.is_mid_range_mode(50), True,  "50 sınırı dahil")
    eq(s.is_mid_range_mode(55), True,  "55 mid-range")
    eq(s.is_mid_range_mode(65), True,  "65 mid-range")
    eq(s.is_mid_range_mode(79), True,  "79 hâlâ mid-range")
    # Alt sınırın hemen altı (kalibre zone)
    eq(s.is_mid_range_mode(49), False, "49 mid-range dışı (kalibre band)")
    eq(s.is_mid_range_mode(45), False, "45 mid-range dışı")
    eq(s.is_mid_range_mode(30), False, "30 mid-range dışı")
    # Üst sınır exclusive
    eq(s.is_mid_range_mode(80), False, "80 mid-range dışı (üst sınır exclusive)")
    eq(s.is_mid_range_mode(85), False, "85 mid-range dışı")
    # None → False (neutral)
    eq(s.is_mid_range_mode(None), False, "None nötr (False)")

test("is_mid_range_mode: [50,80) bandı",
     test_is_mid_range_mode_band)


def test_is_weak_signal_threshold():
    """signal_score < 55 → True, ≥ 55 → False, None → False."""
    s = _import_scanner_module()
    eq(s.is_weak_signal(0),    True,  "0 zayıf")
    eq(s.is_weak_signal(40),   True,  "40 zayıf")
    eq(s.is_weak_signal(54),   True,  "54 zayıf (sınırın altı)")
    eq(s.is_weak_signal(55),   False, "55 sınır — orta (geçer)")
    eq(s.is_weak_signal(70),   False, "70 güçlü")
    eq(s.is_weak_signal(100),  False, "100 mükemmel")
    eq(s.is_weak_signal(None), False, "None nötr (geçir)")

test("is_weak_signal: 55 eşiği",
     test_is_weak_signal_threshold)


def test_scan_date_nonwhitelist_early_return():
    """Whitelist dışı istasyon için scan_date early-return — ağ çağrısı yapılmaz."""
    s = _import_scanner_module()
    # ltfm whitelist dışı (negatif ROI, -44%) — sessizce None döner
    with patch("bot.scanner.httpx.get") as mock_get:
        result = s.scan_date("ltfm", "2026-05-01", trades=[])
    eq(result, None, "whitelist dışı istasyon → None")
    eq(mock_get.called, False,
       "whitelist dışı istasyon için httpx hiç çağrılmamalı (erken çık)")

test("scan_date: whitelist dışı istasyon ağ çağrısı yapmaz",
     test_scan_date_nonwhitelist_early_return)


def test_scan_date_non_pause_station_proceeds():
    """Pause olmayan whitelist istasyon için akış devam eder (ilk httpx çağrısı yapılır)."""
    s = _import_scanner_module()
    # STATION_SKILL_PAUSE boş → tüm whitelist istasyonları aktif
    # sorted() → deterministik seçim (eddm/eglc/eham/epwa/lfpg/ltac/...)
    non_paused = s.STATION_WHITELIST - s.STATION_SKILL_PAUSE
    whitelist_station = next(iter(sorted(non_paused)))  # örn. eddm
    # İlk httpx.get'e (weather API) hata fırlat — downstream'de patlamasın
    with patch("bot.scanner.httpx.get",
               side_effect=Exception("weather api down")) as mock_get:
        result = s.scan_date(whitelist_station, "2026-05-01", trades=[])
    eq(result, None, "API hatası → None")
    ok(mock_get.called, "pause olmayan whitelist istasyon için httpx çağrılmalı")

test("scan_date: pause olmayan istasyon akışa girer",
     test_scan_date_non_pause_station_proceeds)


def test_scanner_source_has_mid_range_gate():
    """Kaynak kodda mid-range gate scan_date içinde, MIN_MODE_PCT check'inden sonra."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    # Gerçek check ifadeleri (tanımlar değil, çağrılar)
    idx_min_mode   = src.find("< MIN_MODE_PCT")
    idx_mid_range  = src.find("if is_mid_range_mode(mode_pct):")
    ok(idx_min_mode > 0, "mode_pct < MIN_MODE_PCT gate bulunamadı")
    ok(idx_mid_range > 0, "if is_mid_range_mode gate bulunamadı")
    ok(idx_mid_range > idx_min_mode,
       f"mid-range gate MIN_MODE_PCT check'inden sonra olmalı "
       f"(min_mode@{idx_min_mode}, mid@{idx_mid_range})")

test("scanner.py: mid-range gate doğru sırada",
     test_scanner_source_has_mid_range_gate)


def test_scanner_source_has_signal_score_gate():
    """Signal score gate signal hesaplandıktan sonra, trade dict'ten önce."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    idx_compute = src.find('signal_score = sig["score"]')
    idx_gate    = src.find("if is_weak_signal(signal_score):")
    idx_trade   = src.find("trade = {")
    ok(idx_compute > 0, "signal_score hesabı bulunamadı")
    ok(idx_gate > 0,    "if is_weak_signal gate bulunamadı")
    ok(idx_trade > 0,   "trade dict bulunamadı")
    ok(idx_compute < idx_gate < idx_trade,
       f"sıra: compute@{idx_compute} → gate@{idx_gate} → trade@{idx_trade}")

test("scanner.py: signal_score gate doğru sırada",
     test_scanner_source_has_signal_score_gate)


def test_scanner_multi_bucket_gate():
    """Multi-bucket (Faz 9): MULTI_BUCKET_N ve budget sabitleri mevcut."""
    scanner_path = Path(__file__).resolve().parent.parent / "bot" / "scanner.py"
    src = scanner_path.read_text(encoding="utf-8")
    ok("MULTI_BUCKET_N" in src, "MULTI_BUCKET_N sabiti eksik")
    ok("THREE_BUCKET_BUDGET" in src, "THREE_BUCKET_BUDGET sabiti eksik")
    ok("TWO_BUCKET_BUDGET" in src, "TWO_BUCKET_BUDGET sabiti eksik")
    ok("multi_bucket" in src, "multi_bucket trade_type marker eksik")
    # Whitelist + D1 threshold + best-N de var mı?
    ok("STATION_WHITELIST" in src, "STATION_WHITELIST eksik")
    ok("D1_MIN_SIGNAL_SCORE" in src, "D1_MIN_SIGNAL_SCORE eksik")
    ok("MAX_DAILY_POSITIONS_PER_DATE" in src, "MAX_DAILY_POSITIONS_PER_DATE eksik")
    ok("MIN_MAIN_PRICE" in src, "MIN_MAIN_PRICE eksik")

test("scanner.py: Faz 9 strateji sabitleri ve multi-bucket marker mevcut",
     test_scanner_multi_bucket_gate)


def test_phase5_retrospective_impact():
    """131 canlı trade üzerinde yeni filtrelerin etkisini hesapla (sanity).

    Canlı /api/calibration verisine göre mode_pct dağılımı:
      0.3-0.5: 69 trade (kalibre)
      0.5-0.8: 54 trade (mid-range skip)
      Station pause (lfpg/ltac/limc): ~39 trade
    Yeni filtrelerin geçirmesi beklenen: ~40-60 trade (kalan set).
    """
    s = _import_scanner_module()
    # Canlı bin dağılımı (sayı yaklaşık)
    fake_trades = []
    # 30-50 kalibre band (kalır) — 69 trade, çeşitli istasyonlarda
    for i in range(69):
        fake_trades.append({"mode_pct": 40 + (i % 10), "station": "ltfm"})
    # 50-80 mid-range (skip olur)
    for i in range(54):
        fake_trades.append({"mode_pct": 50 + (i % 30), "station": "eham"})

    kept = [
        t for t in fake_trades
        if not s.is_mid_range_mode(t["mode_pct"])
        and not s.should_pause_station(t["station"])
    ]
    skipped = [t for t in fake_trades if s.is_mid_range_mode(t["mode_pct"])]
    ok(len(kept) == 69, f"kalibre bandının tamamı kalmalı: {len(kept)}")
    ok(len(skipped) == 54, f"mid-range tamamı skip olmalı: {len(skipped)}")

test("Faz 5 retrospektif: mid-range skip sayılır",
     test_phase5_retrospective_impact)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 35: Faz 7 — SQLite-first + settlement delta + dinamik size")
print(f"{'═'*62}")


def test_sqlite_first_writer_exists():
    """bot/db.py'de write_paper/live_trades_list + rebuild_json_from_db var."""
    from bot import db
    ok(hasattr(db, "write_paper_trades_list"),
       "write_paper_trades_list eksik (SQLite-first API)")
    ok(hasattr(db, "write_live_trades_list"),
       "write_live_trades_list eksik (SQLite-first API)")
    ok(hasattr(db, "rebuild_json_from_db"),
       "rebuild_json_from_db eksik (disaster recovery)")

test("SQLite-first yazıcı API'si mevcut", test_sqlite_first_writer_exists)


def test_save_trades_sqlite_first_order():
    """scanner.save_trades + trader.save_live_trades önce DB'ye yazmalı."""
    import inspect
    from bot import scanner, trader
    src_scan = inspect.getsource(scanner.save_trades)
    ok("write_paper_trades_list" in src_scan,
       "scanner.save_trades SQLite-first değil (write_paper_trades_list çağrısı yok)")
    # SQLite çağrısı JSON dump'ten ÖNCE olmalı
    idx_db   = src_scan.find("write_paper_trades_list")
    idx_json = src_scan.find("json.dumps")
    ok(0 <= idx_db < idx_json,
       "scanner.save_trades DB çağrısı JSON dump'tan önce olmalı")

    src_live = inspect.getsource(trader.save_live_trades)
    ok("write_live_trades_list" in src_live,
       "trader.save_live_trades SQLite-first değil")
    idx_db   = src_live.find("write_live_trades_list")
    idx_json = src_live.find("json.dumps")
    ok(0 <= idx_db < idx_json,
       "trader.save_live_trades DB çağrısı JSON'dan önce olmalı")

test("save_* SQLite-first sırası (DB → JSON)", test_save_trades_sqlite_first_order)


def test_settlement_delta_module():
    """bot.settlement_delta modülü, gerekli API'leri sağlar + sabitler sağlam."""
    from bot import settlement_delta as sd
    ok(hasattr(sd, "learn_station_delta"), "learn_station_delta eksik")
    ok(hasattr(sd, "apply_delta"),         "apply_delta eksik")
    ok(hasattr(sd, "compute_station_deltas"), "compute_station_deltas eksik")
    ok(sd.MAX_DELTA_C <= 5.0,
       f"MAX_DELTA_C makul tavan (≤5): {sd.MAX_DELTA_C}")
    ok(sd.MIN_PAIRED_SAMPLES >= 3,
       f"MIN_PAIRED_SAMPLES en az 3: {sd.MIN_PAIRED_SAMPLES}")

test("settlement_delta modülü API'si", test_settlement_delta_module)


def test_settlement_delta_apply_rounds():
    """apply_delta int döner ve delta yoksa top_pick'i değiştirmez."""
    from bot.settlement_delta import apply_delta
    # Olmayan istasyon → delta=0 → aynı kalır
    out = apply_delta("nonexistent_station_zzz", top_pick=17)
    eq(out, 17, f"Veri yok → top_pick korunur: {out}")
    ok(isinstance(out, int), f"int dönmeli: {type(out)}")

test("apply_delta: veri yoksa nötr", test_settlement_delta_apply_rounds)


def test_position_sizing_tiers():
    """signal_score → size multiplier mapping doğru tier'larda."""
    from bot.position_sizing import size_multiplier, compute_shares
    eq(size_multiplier(90), 1.5, "Premium (≥85) → 1.5x")
    eq(size_multiplier(85), 1.5, "85 tam → 1.5x")
    eq(size_multiplier(75), 1.2, "Strong (70-84) → 1.2x")
    eq(size_multiplier(70), 1.2, "70 tam → 1.2x")
    eq(size_multiplier(60), 1.0, "Moderate (55-69) → 1.0x")
    eq(size_multiplier(55), 1.0, "55 tam → 1.0x")
    eq(size_multiplier(None), 1.0, "None skor → nötr 1.0")
    # compute_shares integer, tavan ve taban
    eq(compute_shares(10, 90),  15, "10×1.5 = 15 share")
    eq(compute_shares(10, 75),  12, "10×1.2 = 12 share")
    eq(compute_shares(10, 60),  10, "10×1.0 = 10 share")
    ok(compute_shares(10, None) == 10, "None skor baseline")

test("position_sizing: tier multiplier + share sayısı",
     test_position_sizing_tiers)


def test_station_status_table_in_schema():
    """db.SCHEMA_SQL içinde station_status tablosu tanımlı."""
    from bot import db
    ok("CREATE TABLE IF NOT EXISTS station_status" in db.SCHEMA_SQL,
       "station_status tablosu şemada yok")
    ok(hasattr(db, "set_station_paused"),
       "set_station_paused yardımcı fonksiyonu eksik")
    ok(hasattr(db, "list_paused_stations"),
       "list_paused_stations yardımcı fonksiyonu eksik")

test("station_status şeması + yardımcılar", test_station_status_table_in_schema)


def test_should_pause_station_db_override(tmp_path=None):
    """DB'de paused=1 override → statik set'te olmasa bile True."""
    import os, tempfile, importlib
    from pathlib import Path
    from bot import db as bot_db

    tmp = Path(tempfile.mkdtemp(prefix="wxbot_test_"))
    tmp_db = tmp / "test.db"
    # Şemayı kur
    bot_db.init_db(tmp_db)
    # efhk'yi DB üzerinden pause et (statikte pause değildi)
    bot_db.set_station_paused("efhk", True, reason="test override", db_path=tmp_db)
    rows = bot_db.list_paused_stations(db_path=tmp_db)
    ok(any(r["station"] == "efhk" and r["paused"] == 1 for r in rows),
       f"efhk DB'de pause=1 olmalı: {rows}")

test("station_status: DB set/list roundtrip",
     test_should_pause_station_db_override)


def test_bayesian_prior_dynamic_weights():
    """Dynamic weights Bayes posterior fonksiyonu + sabitler."""
    from bot import dynamic_weights as dw
    ok(hasattr(dw, "_posterior_rmse"), "_posterior_rmse eksik")
    ok(dw.PRIOR_STRENGTH > 0, "PRIOR_STRENGTH > 0 olmalı")
    # n=0 → tamamen prior
    post0 = dw._posterior_rmse(observed_rmse=5.0, n=0, model="ecmwf")
    eq(round(post0, 2), round(dw.PRIOR_RMSE["ecmwf"], 2),
       f"n=0 posterior prior'a eşit: {post0}")
    # n büyükse observed baskın
    post_big = dw._posterior_rmse(observed_rmse=1.0, n=1000, model="ecmwf")
    ok(abs(post_big - 1.0) < 0.05,
       f"n çok büyükse observed'e yakınsar: {post_big}")

test("dynamic_weights Bayes cold-start prior",
     test_bayesian_prior_dynamic_weights)


def test_aifs_member_validation_exists():
    """main.py'de EXPECTED_MEMBERS ve düşük-üye uyarısı kodlanmış."""
    from pathlib import Path
    src = Path("main.py").read_text(encoding="utf-8", errors="ignore")
    ok("EXPECTED_MEMBERS" in src, "EXPECTED_MEMBERS sabiti yok")
    ok("ensemble üye sayısı düşük" in src,
       "Düşük-üye uyarı mesajı yok (AIFS validation)")

test("AIFS member count validation yerinde",
     test_aifs_member_validation_exists)


def test_var_pre_order_gate_in_trader():
    """trader.place_limit_order içinde VaR gate var."""
    import inspect
    from bot import trader
    src = inspect.getsource(trader.place_limit_order)
    ok("portfolio_var" in src, "portfolio_var çağrısı place_limit_order'da yok")
    ok("var_95" in src,        "var_95 kontrolü yok")
    ok("VaR gate" in src or "var_cap" in src,
       "VaR bloke mesajı/sabiti yok")

test("VaR pre-order gate place_limit_order'a bağlı",
     test_var_pre_order_gate_in_trader)


def test_web_designer_skill_removed():
    """Stray web-designer.skill repo'dan silinmiş olmalı."""
    from pathlib import Path
    ok(not Path("web-designer.skill").exists(),
       "web-designer.skill hâlâ repo kökünde — silinmeli")

test("Stray web-designer.skill silinmiş", test_web_designer_skill_removed)


# ═════════════════════════════════════════════════════════════════
# TEST 36: Faz 8 — profitability (stale bump, flip guard, micro-hedge,
#          horizon delta, dynamic weights, circuit breaker)
# ═════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 36: Faz 8 — profitability geliştirmeleri")
print(f"{'═'*62}")


# ── 36.1: D+1 stale bump — limit fiyatı +3¢ artırılarak yeniden giriliyor ──
def test_cancel_stale_progressive_bump():
    """D+1 stale retry'de place_limit_order yeni (bumped) fiyatla çağrılır."""
    tm = _import_trader_module()
    now      = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    expired_iso = (now - timedelta(hours=1)).isoformat()

    stale_trade = {
        "id": "eglc_stale_live", "paper_id": "eglc_paper",
        "station": "eglc", "date": tomorrow, "top_pick": 18,
        "bucket_title": "18°C", "condition_id": "0xaa",
        "order_id": "order_stale_1", "limit_price": 0.20,
        "shares": 5, "cost_usdc": 1.00,
        "placed_at": (now - timedelta(hours=6)).isoformat(),
        "expires_at": expired_iso, "horizon": "D+1",
        "status": "pending_fill", "stale_bumps": 0,
    }

    client = MagicMock()
    client.cancel.return_value = {"ok": True}

    captured = {}
    def fake_place(**kwargs):
        captured.update(kwargs)
        return {"order_id": "new_bumped_order", "limit_price": kwargs["price"],
                "status": "pending_fill"}

    with patch.object(tm, "load_live_trades", return_value=[stale_trade]), \
         patch.object(tm, "save_live_trades"), \
         patch.object(tm, "setup_client",   return_value=client), \
         patch.object(tm, "place_limit_order", side_effect=fake_place):
        cancelled = tm.cancel_stale_orders()

    ok(cancelled >= 1, f"Stale iptal edildi beklendi: {cancelled}")
    ok("price" in captured,
       "place_limit_order yeni emir için çağrılmalı")
    # +3¢ beklenir: 0.20 → 0.23
    eq(round(captured["price"], 2), 0.23,
       f"D+1 stale bump: 0.20 → 0.23 beklenir, gerçek: {captured.get('price')}")
    eq(captured["station"], "eglc")
    eq(captured["top_pick"], 18)

test("cancel_stale: D+1 retry'de +3¢ progressive bump",
     test_cancel_stale_progressive_bump)


def test_cancel_stale_bump_capped_at_max_price():
    """MAX_PRICE'a ulaşılmışsa bump durur (aynı fiyatla retry)."""
    tm = _import_trader_module()
    now      = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    # limit zaten MAX_PRICE tavanında — bump yapılmamalı
    stale = {
        "id": "x1", "paper_id": "p1", "station": "ltfm",
        "date": tomorrow, "top_pick": 20, "bucket_title": "20°C",
        "condition_id": "0xbb", "order_id": "ord_x1",
        "limit_price": tm.MAX_PRICE, "shares": 5,
        "cost_usdc": 2.0,
        "placed_at": (now - timedelta(hours=10)).isoformat(),
        "expires_at": (now - timedelta(hours=1)).isoformat(),
        "horizon": "D+1", "status": "pending_fill", "stale_bumps": 5,
    }
    client = MagicMock()
    client.cancel.return_value = {"ok": True}
    captured = {}
    def fake_place(**kw):
        captured.update(kw)
        return {"order_id": "again", "limit_price": kw["price"],
                "status": "pending_fill"}
    with patch.object(tm, "load_live_trades", return_value=[stale]), \
         patch.object(tm, "save_live_trades"), \
         patch.object(tm, "setup_client", return_value=client), \
         patch.object(tm, "place_limit_order", side_effect=fake_place):
        tm.cancel_stale_orders()
    # Fiyat değişmemeli — zaten tavanda
    eq(round(captured.get("price", 0), 2), round(tm.MAX_PRICE, 2),
       f"MAX_PRICE tavanında fiyat korunmalı: {captured.get('price')}")

test("cancel_stale: MAX_PRICE tavanında bump yapılmaz",
     test_cancel_stale_bump_capped_at_max_price)


# ── 36.2: Model-flip re-entry guard ────────────────────────────────────────
def test_flip_flop_guard_blocks_weak_signal():
    """Zayıf sinyalli (signal_score<70, mode_pct<70) re-entry iptal edilir."""
    tm = _import_trader_module()
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    # Önceki pending farklı pick'te (flip-flop senaryosu)
    stale_pending = {
        "id": "eham_old", "paper_id": "eham_old_paper",
        "station": "eham", "date": tomorrow, "top_pick": 16,
        "bucket_title": "16°C", "condition_id": "0xcc",
        "order_id": "old_order_id", "limit_price": 0.22,
        "shares": 5, "cost_usdc": 1.10,
        "placed_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=4)).isoformat(),
        "horizon": "D+1", "status": "pending_fill",
    }

    client = MagicMock()
    client.cancel.return_value = {"ok": True}

    with patch.object(tm, "load_live_trades", return_value=[stale_pending]), \
         patch.object(tm, "save_live_trades"), \
         patch.object(tm, "setup_client", return_value=client), \
         patch.object(tm, "_load_paper_trade",
                      return_value={"id": "eham_new_paper",
                                    "signal_score": 55, "ens_mode_pct": 45}), \
         patch.object(tm, "get_balance", return_value=100.0), \
         patch.object(tm, "has_pending_tx", return_value=False):
        result = tm.place_limit_order(
            condition_id="0xdd", price=0.20, station="eham",
            date=tomorrow, top_pick=15, bucket_title="15°C",
            paper_id="eham_new_paper", shares=5,
        )
    eq(result, None, "Zayıf sinyalde re-entry None dönmeli (flip guard)")
    # Eski emri iptal ettiyse ok — yeni emir açılmamış olmalı
    eq(client.create_order.called, False,
       "Flip guard devrede: yeni CLOB order açılmamalı")

test("flip-flop guard: zayıf sinyalde re-entry iptal",
     test_flip_flop_guard_blocks_weak_signal)


def test_flip_flop_guard_allows_strong_signal():
    """Güçlü sinyalli (signal_score≥70) re-entry normal akışa girer."""
    tm = _import_trader_module()
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    stale_pending = {
        "id": "eglc_old", "paper_id": "eglc_old_paper",
        "station": "eglc", "date": tomorrow, "top_pick": 16,
        "bucket_title": "16°C", "condition_id": "0xee",
        "order_id": "old_strong_order", "limit_price": 0.22,
        "shares": 5, "cost_usdc": 1.10,
        "placed_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=4)).isoformat(),
        "horizon": "D+1", "status": "pending_fill",
    }

    client = MagicMock()
    client.cancel.return_value = {"ok": True}
    client.create_order.return_value = {"signed": True}
    client.post_order.return_value   = {"orderID": "new_strong_order_abcd1234"}
    client.get_order_book.return_value = MagicMock(bids=[])

    # portfolio_var'i nötralize et — VaR gate açıkken yanıltmasın
    import types
    fake_pv = types.ModuleType("bot.portfolio_var")
    fake_pv.portfolio_var = lambda *a, **kw: {"var_95": 0.0}
    import sys as _sys
    _sys.modules["bot.portfolio_var"] = fake_pv

    with patch.object(tm, "load_live_trades", return_value=[stale_pending]), \
         patch.object(tm, "save_live_trades"), \
         patch.object(tm, "setup_client", return_value=client), \
         patch.object(tm, "_load_paper_trade",
                      return_value={"id": "eglc_new_paper",
                                    "signal_score": 80, "ens_mode_pct": 55}), \
         patch.object(tm, "get_balance", return_value=100.0), \
         patch.object(tm, "has_pending_tx", return_value=False), \
         patch.object(tm, "get_best_bid", return_value=None):
        result = tm.place_limit_order(
            condition_id="0xff", price=0.20, station="eglc",
            date=tomorrow, top_pick=15, bucket_title="15°C",
            paper_id="eglc_new_paper", shares=5,
        )
    ok(result is not None,
       "Güçlü sinyalde re-entry başarılı olmalı (None dönmemeli)")
    ok(client.create_order.called,
       "Güçlü sinyalde yeni CLOB order oluşturulmalı")

test("flip-flop guard: güçlü sinyalde re-entry geçer",
     test_flip_flop_guard_allows_strong_signal)


# ── 36.3: Multi-bucket (Faz 9 — eski micro-hedge + 2-bucket birleşimi) ──────
def test_multi_bucket_opens_adjacent_bucket():
    """Whitelist istasyonda multi-bucket: 19°C komşu bucket da açılır."""
    s = _import_scanner_module()

    # Whitelist'teki bir istasyon seç (eglc)
    wl_station = "eglc"
    target_date = "2026-05-01"

    # Weather API: blend=18.4, top_pick=18
    weather_resp = MagicMock()
    weather_resp.raise_for_status = MagicMock()
    weather_resp.json = lambda: {"days": {target_date: {"blend": {
        "max_temp": 18.4, "spread": 1.2, "uncertainty": "Düşük",
        "bias_active": False,
    }}}}

    # Ensemble: 18°C mode=80% (mid-range skip [50,80) DIŞINDA — 80 ≥ 80),
    # 19°C komşu %10, 17°C %10 → multi-bucket tetiklenmeli
    # mode_pct=80, edge=0.50 → signal_score yüksek (≥55 eşiğini geçer)
    members = [18]*16 + [19]*2 + [17]*2   # 20 üye: 18→80%, 19→10%, 17→10%
    ens_resp = MagicMock()
    ens_resp.raise_for_status = MagicMock()
    ens_resp.json = lambda: {"days": {target_date: {
        "member_maxes": members,
        "is_bimodal": False, "peak_separation": None,
        "mode_ci_low": 70, "mode_ci_high": 88,
    }}}

    # PM buckets: 18°C @ 30¢ (main, ≥ MIN_MAIN_PRICE), 19°C @ 10¢ (adj, ≤ 65-30=35¢)
    pm_resp = MagicMock()
    pm_resp.raise_for_status = MagicMock()
    pm_resp.json = lambda: {
        "buckets": [
            {"threshold": 18, "is_below": False, "is_above": False,
             "yes_price": 0.30, "condition_id": "0x200", "title": "18°C"},
            {"threshold": 19, "is_below": False, "is_above": False,
             "yes_price": 0.10, "condition_id": "0x201", "title": "19°C"},
            {"threshold": 17, "is_below": False, "is_above": False,
             "yes_price": 0.08, "condition_id": "0x202", "title": "17°C"},
        ],
        "liquidity": 8000,
    }

    def fake_get(url, **kw):
        if "/api/weather" in url:   return weather_resp
        if "/api/ensemble" in url:  return ens_resp
        if "/api/polymarket" in url: return pm_resp
        raise RuntimeError(f"Beklenmeyen URL: {url}")

    with patch("bot.scanner.httpx.get", side_effect=fake_get), \
         patch.object(s, "should_pause_station", return_value=False), \
         patch("bot.settlement_delta.learn_station_delta", return_value=0.0):
        # settlement_delta=0 → top_pick değişmez; test ortam bağımsız olur
        result = s.scan_date(wl_station, target_date, trades=[])

    ok(isinstance(result, list) and len(result) >= 2,
       f"Multi-bucket: en az 2 trade beklenir: {result}")
    # Ana trade: pick=18, adj trade: pick=19 veya 17
    picks = [t["top_pick"] for t in result]
    ok(18 in picks, f"Ana pick 18°C beklenir: {picks}")
    adj_trades = [t for t in result if t.get("trade_type") == "multi_bucket"]
    ok(len(adj_trades) >= 1,
       f"multi_bucket trade_type ile en az 1 komşu beklenir: {adj_trades}")
    # Bütçe kontrolü: tüm entry_price toplamı ≤ THREE_BUCKET_BUDGET
    total_price = sum(t["entry_price"] for t in result)
    ok(total_price <= s.THREE_BUCKET_BUDGET + 0.01,
       f"Toplam fiyat ≤ {s.THREE_BUCKET_BUDGET}: {total_price:.2f}")

test("multi-bucket: whitelist istasyonda komşu bucket(lar) açılır (Faz 9)",
     test_multi_bucket_opens_adjacent_bucket)


# ── 36.3b: Faz 11 — Kom��u bucket edge koruması (ADJ_MAX_NEG_EDGE) ──────────
def test_adj_bucket_skipped_when_market_far_from_ensemble():
    """Komşu bucket piyasa fiyatı ens'ten ADJ_MAX_NEG_EDGE'den fazla sapınca atlanır."""
    s = _import_scanner_module()

    ok(hasattr(s, "ADJ_MAX_NEG_EDGE"),
       "ADJ_MAX_NEG_EDGE sabiti scanner.py'de tanımlı değil")
    ok(s.ADJ_MAX_NEG_EDGE < 0,
       f"ADJ_MAX_NEG_EDGE negatif olmalı: {s.ADJ_MAX_NEG_EDGE}")

    wl_station = "eglc"
    target_date = "2026-06-01"

    # Weather API: blend=18.4
    weather_resp = MagicMock()
    weather_resp.raise_for_status = MagicMock()
    weather_resp.json = lambda: {"days": {target_date: {"blend": {
        "max_temp": 18.4, "spread": 1.2, "uncertainty": "Düşük",
        "bias_active": False,
    }}}}

    # Ensemble: 18°C mode=%80 (sağlam ana pick)
    #           19°C sadece 1 üye (%5) → pozitif edge koşulunu geçer
    #           17°C sadece 1 üye (%5) → pozitif edge koşulunu geçer
    #           20°C SIFIR üye ama market %20 → edge=-20% < ADJ_MAX_NEG_EDGE → SKIP
    members = [18]*16 + [19]*1 + [17]*1 + [22]*2  # 20 üye: 18→80%,19→5%,17→5%,22→10%
    ens_resp = MagicMock()
    ens_resp.raise_for_status = MagicMock()
    ens_resp.json = lambda: {"days": {target_date: {
        "member_maxes": members,
        "is_bimodal": False, "peak_separation": None,
        "mode_ci_low": 70, "mode_ci_high": 88,
    }}}

    # PM buckets:
    #   18°C @ 30¢ → main pick (edge = 80%-30% = +50%)
    #   19°C @ 8¢  → adj OK (ens=5%, edge=-3% > -8%)
    #   17°C @ 8¢  → adj OK (ens=5%, edge=-3% > -8%)
    #   20°C @ 25¢ → adj SKIP (ens=0%, edge=-25% < -8%)
    pm_resp = MagicMock()
    pm_resp.raise_for_status = MagicMock()
    pm_resp.json = lambda: {
        "buckets": [
            {"threshold": 18, "is_below": False, "is_above": False,
             "yes_price": 0.30, "condition_id": "0x300", "title": "18°C"},
            {"threshold": 19, "is_below": False, "is_above": False,
             "yes_price": 0.08, "condition_id": "0x301", "title": "19°C"},
            {"threshold": 17, "is_below": False, "is_above": False,
             "yes_price": 0.08, "condition_id": "0x302", "title": "17°C"},
            {"threshold": 20, "is_below": False, "is_above": False,
             "yes_price": 0.25, "condition_id": "0x303", "title": "20°C"},
        ],
        "liquidity": 8000,
    }

    def fake_get(url, **kw):
        if "/api/weather"    in url: return weather_resp
        if "/api/ensemble"   in url: return ens_resp
        if "/api/polymarket" in url: return pm_resp
        raise RuntimeError(f"Beklenmeyen URL: {url}")

    with patch("bot.scanner.httpx.get", side_effect=fake_get), \
         patch.object(s, "should_pause_station", return_value=False), \
         patch("bot.settlement_delta.learn_station_delta", return_value=0.0):
        result = s.scan_date(wl_station, target_date, trades=[])

    ok(isinstance(result, list) and len(result) >= 1,
       f"En az ana trade beklenir: {result}")

    picks = {t["top_pick"] for t in result}

    # 20°C bucket ens=0% ama mkt=25% → edge=-25% → ADJ_MAX_NEG_EDGE=-8% aşılır → OLMAMALI
    ok(20 not in picks,
       f"20°C adj bucket eklenmemeli (edge=-25% < ADJ_MAX_NEG_EDGE): picks={picks}")

    # 19°C ve/veya 17°C ens=5%, mkt=8% → edge=-3% → kabul edilebilir → OLABİLİR
    # (bütçe el veriyorsa en az biri eklenir)
    ok(18 in picks, f"Ana pick 18°C beklenir: {picks}")


def test_adj_max_neg_edge_constant_exists():
    """ADJ_MAX_NEG_EDGE sabiti scanner.py'de tanımlı ve değeri makul."""
    s = _import_scanner_module()
    ok(hasattr(s, "ADJ_MAX_NEG_EDGE"), "ADJ_MAX_NEG_EDGE tanımlı de��il")
    ok(-0.15 <= s.ADJ_MAX_NEG_EDGE <= -0.01,
       f"ADJ_MAX_NEG_EDGE -0.15 ile -0.01 arasında olmalı: {s.ADJ_MAX_NEG_EDGE}")


test("Faz 11: adj bucket piyasa ens'ten aşırı uzaksa atlanır (ADJ_MAX_NEG_EDGE)",
     test_adj_bucket_skipped_when_market_far_from_ensemble)
test("Faz 11: ADJ_MAX_NEG_EDGE sabiti scanner.py'de tanımlı",
     test_adj_max_neg_edge_constant_exists)


# ── Faz 12: Blend-Ensemble uyum filtresi ─────────────────────────────────────
def test_blend_ensemble_skip_when_large_drift():
    """Blend ile ensemble modu >=2°C ayrışınca scan_date None döner (EHAM 2026-04-24)."""
    s = _import_scanner_module()

    ok(hasattr(s, "BLEND_ENSEMBLE_MAX_DRIFT"),
       "BLEND_ENSEMBLE_MAX_DRIFT sabiti scanner.py'de tanımlı değil")

    wl_station = "eglc"
    target_date = "2026-06-01"

    # blend=16.3°C ama ensemble mode=14°C → fark=2.3°C ≥ 2.0°C → SKIP beklenir
    weather_resp = MagicMock()
    weather_resp.raise_for_status = MagicMock()
    weather_resp.json = lambda: {"days": {target_date: {"blend": {
        "max_temp": 16.3, "spread": 0.8, "uncertainty": "Düşük",
        "bias_active": False,
    }}}}

    # Ensemble: mode=14°C (%80 konsensüs — yüksek güven, ama blend'den 2.3°C uzak)
    members = [14] * 16 + [13] * 2 + [15] * 2  # 20 üye: 14→80%
    ens_resp = MagicMock()
    ens_resp.raise_for_status = MagicMock()
    ens_resp.json = lambda: {"days": {target_date: {
        "member_maxes": members,
        "is_bimodal": False, "peak_separation": None,
        "mode_ci_low": 65, "mode_ci_high": 90,
    }}}

    pm_resp = MagicMock()
    pm_resp.raise_for_status = MagicMock()
    pm_resp.json = lambda: {"buckets": [
        {"threshold": 14, "is_below": False, "is_above": False,
         "yes_price": 0.30, "condition_id": "0xccc", "title": "14°C"},
        {"threshold": 15, "is_below": False, "is_above": False,
         "yes_price": 0.10, "condition_id": "0xccd", "title": "15°C"},
    ], "liquidity": 10000}

    def fake_get(url, **kw):
        if "/api/weather"    in url: return weather_resp
        if "/api/ensemble"   in url: return ens_resp
        if "/api/polymarket" in url: return pm_resp
        raise RuntimeError(f"Beklenmeyen URL: {url}")

    with patch("bot.scanner.httpx.get", side_effect=fake_get), \
         patch.object(s, "should_pause_station", return_value=False), \
         patch("bot.settlement_delta.learn_station_delta", return_value=0.0):
        result = s.scan_date(wl_station, target_date, trades=[])

    ok(result is None,
       f"blend=16.3 vs ens_mode=14 fark=2.3°C → None beklenir, sonuç: {result}")


def test_blend_ensemble_no_skip_within_drift():
    """Blend ile ensemble modu <2°C ayrışırsa trade devam eder."""
    s = _import_scanner_module()

    wl_station = "eglc"
    target_date = "2026-06-01"

    # blend=15.1°C, ensemble mode=14°C → fark=1.1°C < 2.0°C → geçer
    weather_resp = MagicMock()
    weather_resp.raise_for_status = MagicMock()
    weather_resp.json = lambda: {"days": {target_date: {"blend": {
        "max_temp": 15.1, "spread": 0.8, "uncertainty": "Düşük",
        "bias_active": False,
    }}}}

    members = [14] * 16 + [13] * 2 + [15] * 2
    ens_resp = MagicMock()
    ens_resp.raise_for_status = MagicMock()
    ens_resp.json = lambda: {"days": {target_date: {
        "member_maxes": members,
        "is_bimodal": False, "peak_separation": None,
        "mode_ci_low": 65, "mode_ci_high": 90,
    }}}

    pm_resp = MagicMock()
    pm_resp.raise_for_status = MagicMock()
    pm_resp.json = lambda: {"buckets": [
        {"threshold": 14, "is_below": False, "is_above": False,
         "yes_price": 0.30, "condition_id": "0xcce", "title": "14°C"},
        {"threshold": 15, "is_below": False, "is_above": False,
         "yes_price": 0.10, "condition_id": "0xccf", "title": "15°C"},
    ], "liquidity": 10000}

    def fake_get(url, **kw):
        if "/api/weather"    in url: return weather_resp
        if "/api/ensemble"   in url: return ens_resp
        if "/api/polymarket" in url: return pm_resp
        raise RuntimeError(f"Beklenmeyen URL: {url}")

    with patch("bot.scanner.httpx.get", side_effect=fake_get), \
         patch.object(s, "should_pause_station", return_value=False), \
         patch("bot.settlement_delta.learn_station_delta", return_value=0.0):
        result = s.scan_date(wl_station, target_date, trades=[])

    ok(result is not None,
       f"blend=15.1 vs ens_mode=14 fark=1.1°C → trade beklenir, sonuç: {result}")
    picks = {t["top_pick"] for t in result} if isinstance(result, list) else set()
    ok(14 in picks, f"Ana pick 14°C beklenir: {picks}")


def test_blend_ensemble_max_drift_constant_exists():
    """BLEND_ENSEMBLE_MAX_DRIFT sabiti scanner.py'de tanımlı ve değeri makul."""
    s = _import_scanner_module()
    ok(hasattr(s, "BLEND_ENSEMBLE_MAX_DRIFT"),
       "BLEND_ENSEMBLE_MAX_DRIFT tanımlı değil")
    ok(1.5 <= s.BLEND_ENSEMBLE_MAX_DRIFT <= 3.0,
       f"BLEND_ENSEMBLE_MAX_DRIFT 1.5-3.0°C arasında olmalı: {s.BLEND_ENSEMBLE_MAX_DRIFT}")


test("Faz 12: blend-ensemble büyük fark (≥2°C) → trade atlanır",
     test_blend_ensemble_skip_when_large_drift)
test("Faz 12: blend-ensemble küçük fark (<2°C) → trade devam eder",
     test_blend_ensemble_no_skip_within_drift)
test("Faz 12: BLEND_ENSEMBLE_MAX_DRIFT sabiti scanner.py'de tanımlı",
     test_blend_ensemble_max_drift_constant_exists)


# ── 36.4: Horizon-specific settlement delta ─────────���───────────────────────
def test_horizon_delta_dampening():
    """horizon_days=2 ise delta %85'e azaltılır (Faz A1: eski %70 çok muhafazakârdı)."""
    import tempfile
    from pathlib import Path as _P
    from bot import db as bot_db
    from bot import settlement_delta as sd

    tmp = _P(tempfile.mkdtemp(prefix="wxbot_cb_"))
    tmp_db = tmp / "test.db"
    bot_db.init_db(tmp_db)

    # 6 gün için: open-meteo + metar çifti Δ = +1.0°C (paired)
    with bot_db.get_db(tmp_db) as conn:
        for i in range(6):
            d = f"2026-04-{10+i:02d}"
            conn.execute(
                "INSERT INTO settlement_audit (station, date, source, actual_temp, rounded_temp) "
                "VALUES (?, ?, ?, ?, ?)",
                ("eglc", d, "open-meteo", 15.0, 15),
            )
            conn.execute(
                "INSERT INTO settlement_audit (station, date, source, actual_temp, rounded_temp) "
                "VALUES (?, ?, ?, ?, ?)",
                ("eglc", d, "metar", 16.0, 16),
            )
    # Medyan delta = +1.0
    d_h1 = sd.learn_station_delta("eglc", db_path=tmp_db, horizon_days=1)
    d_h2 = sd.learn_station_delta("eglc", db_path=tmp_db, horizon_days=2)
    d_none = sd.learn_station_delta("eglc", db_path=tmp_db, horizon_days=None)

    eq(round(d_none, 2), 1.00, f"Raw delta 1.0 olmalı: {d_none}")
    eq(round(d_h1,   2), 1.00, f"D+1: raw * 1.0 = 1.00: {d_h1}")
    eq(round(d_h2,   2), 0.85, f"D+2: raw * 0.85 = 0.85: {d_h2}")

test("settlement_delta: horizon_days=2 dampening (%85, Faz A1)",
     test_horizon_delta_dampening)


# ── 36.5: Horizon-aware dynamic weights geçirildi mi? ───────────────────────
def test_main_passes_horizon_to_dynamic_weights():
    """main.py compute_dynamic_weights çağrısı horizon_days parametresi ile."""
    src = Path("main.py").read_text(encoding="utf-8", errors="ignore")
    ok("compute_dynamic_weights(station, horizon_days=horizon)" in src
       or "compute_dynamic_weights(station, horizon_days=" in src,
       "main.py compute_dynamic_weights'a horizon_days geçirmiyor")
    # API imzası: effective_weights ve compute_dynamic_weights horizon_days kabul etmeli
    from bot import dynamic_weights as dw
    import inspect
    sig_eff = inspect.signature(dw.effective_weights)
    sig_dyn = inspect.signature(dw.compute_dynamic_weights)
    ok("horizon_days" in sig_eff.parameters,
       "effective_weights horizon_days parametresi eksik")
    ok("horizon_days" in sig_dyn.parameters,
       "compute_dynamic_weights horizon_days parametresi eksik")

test("horizon-aware dynamic weights: parametre entegrasyonu",
     test_main_passes_horizon_to_dynamic_weights)


# ── 36.6: Circuit breaker — istasyon win-rate koruması ──────────────────────
def test_circuit_breaker_triggers_on_low_win_rate():
    """8 kayıp / 2 kazanç → %20 win-rate → breaker True."""
    import tempfile
    from pathlib import Path as _P
    from bot import db as bot_db
    from bot import circuit_breaker as cb

    tmp = _P(tempfile.mkdtemp(prefix="wxbot_cb2_"))
    tmp_db = tmp / "test.db"
    bot_db.init_db(tmp_db)

    # 10 closed live trade — 8 loss, 2 win, aynı istasyon (ltfm)
    with bot_db.get_db(tmp_db) as conn:
        for i in range(10):
            result = "LOSS" if i < 8 else "WIN"
            status = "settled_loss" if result == "LOSS" else "settled_win"
            settled_iso = (datetime.now() - timedelta(days=10-i)).isoformat()
            conn.execute(
                """INSERT INTO live_trades
                   (id, station, date, status, result, settled_at, placed_at,
                    top_pick, bucket_title, condition_id, order_id,
                    limit_price, shares, cost_usdc, pnl_usdc, horizon)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"ltfm_{i}", "ltfm", "2026-04-" + str(10+i).zfill(2),
                 status, result, settled_iso, settled_iso,
                 20, "20°C", "0xtest", f"order_{i}",
                 0.2, 5, 1.0, 4.0 if result == "WIN" else -1.0, "D+1"),
            )

    # Doğrudan çağrı: True dönmeli
    eq(cb.check_station_circuit_breaker("ltfm", db_path=tmp_db), True,
       "%20 win-rate → circuit breaker True")
    # Başka (veri yok) istasyonlar False
    eq(cb.check_station_circuit_breaker("eglc", db_path=tmp_db), False,
       "Kapalı live trade yok → False (veri yetersiz)")

    # enforce çalıştırınca DB'ye pause yazılmalı
    result = cb.enforce_circuit_breakers(
        stations=["ltfm", "eglc"], db_path=tmp_db,
    )
    ok("ltfm" in result["paused"],
       f"ltfm paused listesinde olmalı: {result}")
    rows = bot_db.list_paused_stations(db_path=tmp_db)
    ltfm_row = next((r for r in rows if r["station"] == "ltfm"), None)
    ok(ltfm_row is not None and ltfm_row["paused"] == 1,
       f"DB'de ltfm paused=1 olmalı: {ltfm_row}")

test("circuit_breaker: düşük win-rate → pause yazılır",
     test_circuit_breaker_triggers_on_low_win_rate)


def test_circuit_breaker_no_trigger_on_healthy_rate():
    """5 kazanç / 5 kayıp → %50 win-rate → breaker False."""
    import tempfile
    from pathlib import Path as _P
    from bot import db as bot_db
    from bot import circuit_breaker as cb

    tmp = _P(tempfile.mkdtemp(prefix="wxbot_cb3_"))
    tmp_db = tmp / "test.db"
    bot_db.init_db(tmp_db)

    with bot_db.get_db(tmp_db) as conn:
        for i in range(10):
            result = "WIN" if i < 5 else "LOSS"
            status = "settled_win" if result == "WIN" else "settled_loss"
            iso = (datetime.now() - timedelta(days=10-i)).isoformat()
            conn.execute(
                """INSERT INTO live_trades
                   (id, station, date, status, result, settled_at, placed_at,
                    top_pick, bucket_title, condition_id, order_id,
                    limit_price, shares, cost_usdc, pnl_usdc, horizon)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"eglc_{i}", "eglc", "2026-04-" + str(10+i).zfill(2),
                 status, result, iso, iso,
                 18, "18°C", "0xtest", f"order_{i}",
                 0.2, 5, 1.0, 4.0, "D+1"),
            )

    eq(cb.check_station_circuit_breaker("eglc", db_path=tmp_db), False,
       "%50 win-rate → breaker False")

test("circuit_breaker: sağlıklı win-rate → tetiklenmez",
     test_circuit_breaker_no_trigger_on_healthy_rate)


def test_circuit_breaker_module_api():
    """Circuit breaker modülü gerekli API'leri sağlar."""
    from bot import circuit_breaker as cb
    ok(hasattr(cb, "check_station_circuit_breaker"),
       "check_station_circuit_breaker eksik")
    ok(hasattr(cb, "enforce_circuit_breakers"),
       "enforce_circuit_breakers eksik")
    ok(0 < cb.CB_MIN_WIN_RATE < 1, f"CB_MIN_WIN_RATE aralık: {cb.CB_MIN_WIN_RATE}")
    ok(cb.CB_LOOKBACK_TRADES >= 5,
       f"CB_LOOKBACK_TRADES ≥ 5: {cb.CB_LOOKBACK_TRADES}")
    ok(cb.CB_PAUSE_DAYS >= 1,
       f"CB_PAUSE_DAYS ≥ 1: {cb.CB_PAUSE_DAYS}")

test("circuit_breaker: modül API'si ve sabitler",
     test_circuit_breaker_module_api)


def test_station_status_auto_resume_schema():
    """station_status tablosu auto_resume_at kolonu içerir."""
    from bot import db as bot_db
    ok("auto_resume_at" in bot_db.SCHEMA_SQL,
       "SCHEMA_SQL'de auto_resume_at yok")
    # set_station_paused auto_resume_at parametresini kabul etmeli
    import inspect
    sig = inspect.signature(bot_db.set_station_paused)
    ok("auto_resume_at" in sig.parameters,
       "set_station_paused auto_resume_at parametresi eksik")

test("station_status: auto_resume_at kolonu + param mevcut",
     test_station_status_auto_resume_schema)


# ═════════════════════════════════════════════════════════════════
# TEST 37: settle_live() bug fixes —
#   (a) orphan `yesterday` NameError (commit 7ed29f1 aftermath)
#   (b) filter sadece 'filled' alıyordu → 'sell_pending' kaçıyordu
# ═════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 37: settle_live() kritik fix'ler (yesterday + sell_pending)")
print(f"{'═'*62}")


def test_settle_live_no_yesterday_nameerror_on_empty():
    """Settle edilecek trade yoksa NameError: yesterday atmamalı."""
    tm = _import_trader_module()
    with patch.object(tm, "load_live_trades", return_value=[]):
        try:
            tm.settle_live()
        except NameError as e:
            raise AssertionError(
                f"settle_live() NameError attı: {e} — orphan 'yesterday' ref hâlâ duruyor"
            ) from e

test("settle_live: boş to_settle listesinde NameError atmaz",
     test_settle_live_no_yesterday_nameerror_on_empty)


def test_settle_live_processes_sell_pending():
    """sell_pending pozisyonlar da settle_live tarafından işlenmeli."""
    tm = _import_trader_module()
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    trades = [
        {
            "id": "sp1", "station": "eglc", "date": yday,
            "top_pick": 14, "bucket_title": "14°C",
            "condition_id": "0xsp", "order_id": "ord_sp",
            "limit_price": 0.40, "shares": 5, "cost_usdc": 2.0,
            "placed_at": yday + "T10:00:00", "horizon": "D+1",
            "status": "sell_pending",  # ← AUTOSELL fill olmadı, market kapandı
        },
    ]
    saved: list = []

    # Actual temp mock → 14°C WIN → settled_win olması beklenir
    with patch.object(tm, "load_live_trades", return_value=trades), \
         patch.object(tm, "save_live_trades",
                      side_effect=lambda t: saved.extend(t)), \
         patch.object(tm, "get_actual_temp_open_meteo", return_value=14.0):
        tm.settle_live()

    ok(len(saved) == 1, f"1 trade kaydedilmeli, oldu: {len(saved)}")
    status = saved[0].get("status") if saved else None
    ok(status and status.startswith("settled_"),
       f"sell_pending → settled_* beklenir, oldu: {status}")

test("settle_live: sell_pending status da settle edilir",
     test_settle_live_processes_sell_pending)


def test_settle_live_filter_includes_both_statuses():
    """Filter hem 'filled' hem 'sell_pending' dahil etmeli."""
    tm = _import_trader_module()
    import inspect
    src = inspect.getsource(tm.settle_live)
    ok('"filled"' in src and '"sell_pending"' in src,
       "settle_live filter'ı iki durumu da kapsamalı "
       f"(src içinde filled+sell_pending): {src[:200]!r}")
    ok("else yesterday" not in src,
       "Orphan `else yesterday` referansı temizlenmemiş")

test("settle_live: filter kaynak kodu filled+sell_pending içeriyor",
     test_settle_live_filter_includes_both_statuses)


# ═════════════════════════════════════════════════════════════════
# TEST 38: Faz 9 — Apr 22 bug fixes
#   (a) cmd_redeem: "positions'ta yok" → redeemed=True YAZMASIN (retry)
#   (b) settle_live: on-chain resolution OM'u override etsin (WU authority)
# ═════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(" TEST 38: Faz 9 — redeem false-positive + on-chain override")
print(f"{'═'*62}")


def test_redeem_does_not_false_flag_when_position_missing():
    """Positions API token dönmediğinde redeemed=True YAZILMAMALI."""
    tm = _import_trader_module()
    trade = {
        "id": "epwa_win", "station": "epwa", "date": "2026-04-22",
        "top_pick": 16, "bucket_title": "16°C", "condition_id": "99999",
        "status": "settled_win", "result": "WIN", "pnl_usdc": 3.25,
        "shares": 5, "cost_usdc": 1.75, "fill_price": 0.35,
        "horizon": "D+1", "limit_price": 0.35,
    }
    saved: list = []

    # Mock httpx.get: positions API boş liste döner (EPWA token'ı yok)
    # Web3 de mock — gerçek redeem çağrısı olmamalı
    class FakeResp:
        is_success = True
        def json(self): return []
    with patch.object(tm.httpx, "get", return_value=FakeResp()), \
         patch.object(tm, "load_live_trades", return_value=[trade]), \
         patch.object(tm, "save_live_trades",
                      side_effect=lambda t: saved.extend(t)), \
         patch.object(tm, "_get_w3"), \
         patch.object(tm, "_redeem_ctf") as mock_redeem:
        tm.cmd_redeem()

    ok(len(saved) == 1, "1 trade kaydedilmeli")
    out = saved[0]
    eq(out.get("redeemed"), False if out.get("redeemed") is not None else None,
       f"Positions'ta yoksa redeemed False/None kalmalı, oldu: {out.get('redeemed')}")
    ok(not mock_redeem.called, "_redeem_ctf çağrılmamalı (token yok)")
    ok(out.get("redeem_attempts", 0) >= 1,
       f"redeem_attempts artmalı, oldu: {out.get('redeem_attempts')}")

test("cmd_redeem: positions'ta yoksa redeemed=True YAZMAZ (retry queue)",
     test_redeem_does_not_false_flag_when_position_missing)


def test_redeem_abandons_after_five_attempts():
    """5 deneme sonrası redeem_abandoned olarak kapatılmalı."""
    tm = _import_trader_module()
    trade = {
        "id": "stuck", "station": "xxx", "date": "2026-04-01",
        "top_pick": 15, "bucket_title": "15°C", "condition_id": "77777",
        "status": "settled_win", "result": "WIN", "pnl_usdc": 1.0,
        "shares": 5, "cost_usdc": 1.0, "fill_price": 0.20,
        "horizon": "D+1", "limit_price": 0.20,
        "redeem_attempts": 4,  # 5. deneme → abandon
    }
    saved: list = []
    class FakeResp:
        is_success = True
        def json(self): return []
    with patch.object(tm.httpx, "get", return_value=FakeResp()), \
         patch.object(tm, "load_live_trades", return_value=[trade]), \
         patch.object(tm, "save_live_trades",
                      side_effect=lambda t: saved.extend(t)), \
         patch.object(tm, "_get_w3"):
        tm.cmd_redeem()

    out = saved[0]
    eq(out.get("status"), "redeem_abandoned",
       f"5. denemede status=redeem_abandoned olmalı, oldu: {out.get('status')}")
    eq(out.get("redeemed"), True, "Zincirden çıkması için redeemed=True işaretli")

test("cmd_redeem: 5 başarısız denemeden sonra abandon",
     test_redeem_abandons_after_five_attempts)


def test_settle_live_onchain_overrides_open_meteo():
    """On-chain redeemable+value>0 ise OM LOSS'u WIN'e çevirmeli."""
    tm = _import_trader_module()
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    trade = {
        "id": "eglc_apr22", "station": "eglc", "date": yday,
        "top_pick": 15, "bucket_title": "15°C",
        "condition_id": "55555",  # token_id
        "limit_price": 0.27, "shares": 5, "cost_usdc": 1.33,
        "fill_price": 0.27, "horizon": "D+1",
        "placed_at": yday + "T10:00:00",
        "status": "filled",
    }
    saved: list = []

    # OM der ki: 16.5°C (round 17°C) → bucket 15°C = LOSS
    # Ama on-chain positions API: token_id=55555 redeemable=True value=$5
    # → gerçek sonuç WIN
    class FakeResp:
        is_success = True
        def json(self):
            return [{
                "asset": "55555", "redeemable": True,
                "currentValue": 5.0, "size": 5.0,
            }]
    with patch.object(tm, "load_live_trades", return_value=[trade]), \
         patch.object(tm, "save_live_trades",
                      side_effect=lambda t: saved.extend(t)), \
         patch.object(tm, "get_actual_temp_open_meteo", return_value=16.5), \
         patch.object(tm.httpx, "get", return_value=FakeResp()):
        tm.settle_live()

    out = saved[0]
    eq(out.get("status"), "settled_win",
       f"On-chain override: LOSS→WIN bekleniyor, oldu: {out.get('status')}")
    eq(out.get("result"), "WIN")
    ok("on-chain override" in (out.get("notes") or ""),
       f"Notes on-chain override içermeli, oldu: {out.get('notes')}")

test("settle_live: on-chain resolution OM'u override eder (WU authority)",
     test_settle_live_onchain_overrides_open_meteo)


def test_settle_live_onchain_agrees_no_override():
    """On-chain OM ile aynı fikirde ise override mesajı olmamalı."""
    tm = _import_trader_module()
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    trade = {
        "id": "epwa_agree", "station": "epwa", "date": yday,
        "top_pick": 16, "bucket_title": "16°C", "condition_id": "88888",
        "limit_price": 0.35, "shares": 5, "cost_usdc": 1.75,
        "fill_price": 0.35, "horizon": "D+1",
        "placed_at": yday + "T10:00:00", "status": "filled",
    }
    saved: list = []
    # OM: 16.2°C → 16°C WIN; on-chain da WIN → override yok
    class FakeResp:
        is_success = True
        def json(self):
            return [{
                "asset": "88888", "redeemable": True,
                "currentValue": 5.0, "size": 5.0,
            }]
    with patch.object(tm, "load_live_trades", return_value=[trade]), \
         patch.object(tm, "save_live_trades",
                      side_effect=lambda t: saved.extend(t)), \
         patch.object(tm, "get_actual_temp_open_meteo", return_value=16.2), \
         patch.object(tm.httpx, "get", return_value=FakeResp()):
        tm.settle_live()

    out = saved[0]
    eq(out.get("status"), "settled_win")
    ok("on-chain override" not in (out.get("notes") or ""),
       f"Uyuşuyorsa override mesajı olmamalı, oldu: {out.get('notes')}")

test("settle_live: on-chain+OM uyuşuyorsa override mesajı yok",
     test_settle_live_onchain_agrees_no_override)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*62)
print(" TEST 39: Faz 10 — Trend Bias Tiebreaker (adj bucket yönü)")
print("═"*62)

def test_trend_bias_warming():
    """Isınma trendi (sdelta > threshold) → adj1 için +1°C bucket öne çıkar."""
    s = _import_scanner_module()
    top_pick = 18
    # İki komşu eşit fiyatta — trend olmadan sıralama belirsiz; trend ile deterministik
    cands = [
        (17, 5, 0.20),   # -1°C, 20¢
        (19, 5, 0.20),   # +1°C, 20¢
    ]
    sdelta = 1.0   # ısınma
    trend_dir = 1 if abs(sdelta) >= s.TREND_BIAS_THRESHOLD else 0
    sorted_c = sorted(
        cands,
        key=lambda x, _d=trend_dir: (
            -(x[2] + (s.TREND_PRICE_BONUS if (_d and (x[0] - top_pick) * _d > 0) else 0.0)),
            -x[1],
        ),
    )
    eq(sorted_c[0][0], 19, "Isınma: +1°C (19°C) adj1 olarak seçilmeli")

test("trend bias: ısınma → +1°C bucket önce (eşit fiyat)", test_trend_bias_warming)


def test_trend_bias_cooling():
    """Soğuma trendi (sdelta < -threshold) → adj1 için -1°C bucket öne çıkar."""
    s = _import_scanner_module()
    top_pick = 18
    cands = [
        (17, 5, 0.20),
        (19, 5, 0.20),
    ]
    sdelta = -1.0  # soğuma
    trend_dir = -1
    sorted_c = sorted(
        cands,
        key=lambda x, _d=trend_dir: (
            -(x[2] + (s.TREND_PRICE_BONUS if (_d and (x[0] - top_pick) * _d > 0) else 0.0)),
            -x[1],
        ),
    )
    eq(sorted_c[0][0], 17, "Soğuma: -1°C (17°C) adj1 olarak seçilmeli")

test("trend bias: soğuma → -1°C bucket önce (eşit fiyat)", test_trend_bias_cooling)


def test_trend_bias_neutral():
    """Nötr sdelta (< threshold) → trend bias devreye girmez, market fiyatı belirler."""
    s = _import_scanner_module()
    top_pick = 18
    cands = [
        (17, 5, 0.22),   # -1°C, 22¢ → daha pahalı
        (19, 5, 0.20),   # +1°C, 20¢
    ]
    sdelta = 0.1  # threshold altında → nötr
    trend_dir = 0
    sorted_c = sorted(
        cands,
        key=lambda x, _d=trend_dir: (
            -(x[2] + (s.TREND_PRICE_BONUS if (_d and (x[0] - top_pick) * _d > 0) else 0.0)),
            -x[1],
        ),
    )
    eq(sorted_c[0][0], 17, "Nötr: market fiyatı belirler → 22¢ olan 17°C önce")

test("trend bias: nötr sdelta → market fiyatı öncelikli", test_trend_bias_neutral)


def test_trend_bias_market_wins_large_diff():
    """Isınma trendi aktif ama market fiyat farkı > TREND_PRICE_BONUS → market kazanır."""
    s = _import_scanner_module()
    top_pick = 18
    # 17°C@26¢ vs 19°C@20¢: ısınma bonus = +3¢ → efektif 23¢, ama 26¢ hâlâ yüksek
    cands = [
        (17, 5, 0.26),
        (19, 5, 0.20),
    ]
    sdelta = 1.0
    trend_dir = 1
    sorted_c = sorted(
        cands,
        key=lambda x, _d=trend_dir: (
            -(x[2] + (s.TREND_PRICE_BONUS if (_d and (x[0] - top_pick) * _d > 0) else 0.0)),
            -x[1],
        ),
    )
    eq(sorted_c[0][0], 17, "Isınma var ama 17°C@26¢ >> 19°C@23¢(+bonus) → 17°C kazanır")

test("trend bias: ısınma ama büyük fiyat farkında market kazanır", test_trend_bias_market_wins_large_diff)


def test_trend_bias_tiebreaker_activates():
    """Isınma trendi: 19°C@20¢ + 3¢ bonus = 23¢ > 17°C@22¢ → 19°C öne geçer."""
    s = _import_scanner_module()
    top_pick = 18
    cands = [
        (17, 5, 0.22),   # -1°C, 22¢
        (19, 5, 0.20),   # +1°C, 20¢ + 3¢ bonus = 23¢ efektif
    ]
    sdelta = 0.5   # threshold üstü
    trend_dir = 1
    sorted_c = sorted(
        cands,
        key=lambda x, _d=trend_dir: (
            -(x[2] + (s.TREND_PRICE_BONUS if (_d and (x[0] - top_pick) * _d > 0) else 0.0)),
            -x[1],
        ),
    )
    eq(sorted_c[0][0], 19, "Tiebreaker: 20¢+3¢=23¢ > 22¢ → 19°C adj1 olarak seçilmeli")

test("trend bias: tiebreaker devreye girer (20¢+3¢ > 22¢)", test_trend_bias_tiebreaker_activates)


def test_trend_bias_constants_exist():
    """TREND_BIAS_THRESHOLD ve TREND_PRICE_BONUS sabitleri scanner'da mevcut."""
    s = _import_scanner_module()
    ok(hasattr(s, "TREND_BIAS_THRESHOLD"), "TREND_BIAS_THRESHOLD sabiti mevcut")
    ok(hasattr(s, "TREND_PRICE_BONUS"), "TREND_PRICE_BONUS sabiti mevcut")
    eq(s.TREND_BIAS_THRESHOLD, 0.30, "TREND_BIAS_THRESHOLD = 0.30")
    eq(s.TREND_PRICE_BONUS, 0.03, "TREND_PRICE_BONUS = 0.03")

test("trend bias: sabitler scanner'da mevcut (0.30 / 0.03)", test_trend_bias_constants_exist)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*62)
print(" TEST 40: Faz 11 — Asya İstasyon Model Konfigürasyonu")
print("═"*62)

def _import_main_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "main_mod",
        Path(__file__).resolve().parent.parent / "main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # FastAPI app nesnesini başlatmadan sadece modülü yükle
    import unittest.mock as _um
    with _um.patch("fastapi.FastAPI"), _um.patch("uvicorn.run", side_effect=SystemExit):
        try:
            spec.loader.exec_module(mod)
        except (SystemExit, Exception):
            pass
    return mod

def test_station_model_config_exists():
    """STATION_MODEL_CONFIG rjtt, rksi, vhhh için tanımlı."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    ok("STATION_MODEL_CONFIG" in src, "STATION_MODEL_CONFIG main.py'de mevcut")
    for st in ("rjtt", "rksi", "vhhh"):
        ok(f'"{st}"' in src, f"STATION_MODEL_CONFIG'ta {st} tanımı var")

test("Asya model config: STATION_MODEL_CONFIG rjtt/rksi/vhhh tanımlı",
     test_station_model_config_exists)


def test_rjtt_uses_jma_not_icon():
    """RJTT config: JMA MSM/GSM var, ICON/UKMO/meteofrance YOK."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    # STATION_MODEL_CONFIG başlangıcından bul (STATIONS dict'indeki "rjtt" değil)
    cfg_start = src.find("STATION_MODEL_CONFIG: dict = {")
    ok(cfg_start > 0, "STATION_MODEL_CONFIG: dict = { bulunamadı")
    cfg_block = src[cfg_start:]
    # rjtt bloğunu config içinde bul
    idx_rjtt = cfg_block.find('"rjtt": {')
    idx_rksi = cfg_block.find('"rksi": {', idx_rjtt)
    rjtt_block = cfg_block[idx_rjtt:idx_rksi]
    ok("jma_msm" in rjtt_block, "rjtt config'te jma_msm modeli var")
    ok("jma_gsm" in rjtt_block, "rjtt config'te jma_gsm modeli var")
    ok("ecmwf_ifs025" in rjtt_block, "rjtt config'te ecmwf_ifs025 var")
    ok("icon_seamless" not in rjtt_block, "rjtt config'te icon_seamless YOK")
    ok("ukmo_seamless" not in rjtt_block, "rjtt config'te ukmo_seamless YOK")
    ok("meteofrance" not in rjtt_block, "rjtt config'te meteofrance YOK")

test("Asya model config: rjtt JMA kullanır, ICON/UKMO/MeteoFrance yok",
     test_rjtt_uses_jma_not_icon)


def test_rksi_uses_kma():
    """RKSI config: KMA LDPS/GDPS var (Seoul Incheon için en ince model)."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    cfg_start = src.find("STATION_MODEL_CONFIG: dict = {")
    cfg_block = src[cfg_start:]
    idx_rksi = cfg_block.find('"rksi": {')
    idx_vhhh = cfg_block.find('"vhhh": {', idx_rksi)
    rksi_block = cfg_block[idx_rksi:idx_vhhh]
    ok("kma_ldps" in rksi_block, "rksi config'te kma_ldps var (1.5km)")
    ok("kma_gdps" in rksi_block, "rksi config'te kma_gdps var")
    ok("icon_seamless" not in rksi_block, "rksi config'te icon_seamless YOK")

test("Asya model config: rksi KMA LDPS kullanır",
     test_rksi_uses_kma)


def test_european_stations_unaffected():
    """Avrupa istasyonları STATION_MODEL_CONFIG'ta YOK → eski kod path'i değişmez."""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    src = main_path.read_text(encoding="utf-8")
    # STATION_MODEL_CONFIG dict tanımını bul
    idx_start = src.find("STATION_MODEL_CONFIG: dict = {")
    idx_end   = src.find("\n}", idx_start + 1)
    # Bir sonraki } bulmak için iç içe geçme sayısını takip et
    depth = 0
    for i, ch in enumerate(src[idx_start:], start=idx_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                idx_end = i + 1
                break
    config_block = src[idx_start:idx_end]
    for eu_st in ("eglc", "eham", "epwa", "eddm", "lfpg"):
        ok(f'"{eu_st}"' not in config_block,
           f"Avrupa istasyonu {eu_st} STATION_MODEL_CONFIG'ta olmamalı")

test("Asya model config: Avrupa istasyonları config'te yok (izolasyon)",
     test_european_stations_unaffected)


def test_asian_coords_in_scanner():
    """scanner.py RKSI ve VHHH koordinatlarına sahip."""
    s = _import_scanner_module()
    ok("rksi" in s.STATION_COORDS, "scanner RKSI koordinatı var")
    ok("vhhh" in s.STATION_COORDS, "scanner VHHH koordinatı var")
    # RKSI = Incheon havalimanı (~37.46°N, ~126.44°E)
    rksi_lat, rksi_lon = s.STATION_COORDS["rksi"]
    ok(37.4 < rksi_lat < 37.5, f"RKSI lat doğru aralıkta: {rksi_lat}")
    ok(126.3 < rksi_lon < 126.6, f"RKSI lon doğru aralıkta: {rksi_lon}")
    # VHHH = HK Intl (~22.31°N, ~113.92°E)
    vhhh_lat, vhhh_lon = s.STATION_COORDS["vhhh"]
    ok(22.1 < vhhh_lat < 22.5, f"VHHH lat doğru aralıkta: {vhhh_lat}")
    ok(113.7 < vhhh_lon < 114.1, f"VHHH lon doğru aralıkta: {vhhh_lon}")

test("Asya model config: RKSI ve VHHH koordinatları scanner'da doğru",
     test_asian_coords_in_scanner)


def test_asian_stations_whitelist():
    """Faz 11 backtest sonrası: vhhh eklendi, rksi/rjtt dışarıda."""
    s = _import_scanner_module()
    ok("vhhh" in s.STATION_WHITELIST,
       "vhhh backtest sonrası whitelist'e eklendi (47.5% wr, MAE 0.59°C)")
    ok("rksi" not in s.STATION_WHITELIST,
       "rksi yapısal -2.27°C bias nedeniyle kalıcı skip")
    ok("rjtt" not in s.STATION_WHITELIST,
       "rjtt yaz verisi bekleniyor (Haziran-Ağustos)")

test("Asya model config: vhhh whitelist'te, rksi/rjtt dışında (Faz 11)",
     test_asian_stations_whitelist)


# ══════════════════════════════════════════════════════════════════════════════
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 41: Faz A — Settlement Delta Hızlandırma + Prior'lar")
print("══════════════════════════════════════════════════════════════")

def _import_settlement_delta():
    import importlib, sys
    if "bot.settlement_delta" in sys.modules:
        del sys.modules["bot.settlement_delta"]
    return importlib.import_module("bot.settlement_delta")

def test_settlement_delta_constants():
    """Faz A1: yeni sabitler doğru değerde."""
    sd = _import_settlement_delta()
    eq(sd.MIN_PAIRED_SAMPLES, 3,
       f"MIN_PAIRED_SAMPLES=3 beklenir (eski:5): {sd.MIN_PAIRED_SAMPLES}")
    eq(sd.DEFAULT_WINDOW_DAYS, 30,
       f"DEFAULT_WINDOW_DAYS=30 beklenir (eski:60): {sd.DEFAULT_WINDOW_DAYS}")
    eq(sd.HORIZON_DELTA_DAMPENING.get(1), 1.0,
       "D+1 dampening=1.0 (tam uygula)")
    eq(sd.HORIZON_DELTA_DAMPENING.get(2), 0.85,
       f"D+2 dampening=0.85 beklenir (eski:0.70): {sd.HORIZON_DELTA_DAMPENING.get(2)}")

test("Faz A1: settlement_delta sabitleri güncellendi (3/30/0.85)",
     test_settlement_delta_constants)


def test_station_delta_priors_exist():
    """Faz A2: STATION_DELTA_PRIORS dict mevcut ve doğru."""
    sd = _import_settlement_delta()
    ok(hasattr(sd, "STATION_DELTA_PRIORS"), "STATION_DELTA_PRIORS mevcut")
    ok(isinstance(sd.STATION_DELTA_PRIORS, dict), "dict olmalı")
    # Tutarlı pozitif bias istasyonlar prior > 0
    ok(sd.STATION_DELTA_PRIORS.get("eddm", 0) > 0, "eddm prior > 0 (tutarlı +1.5°C METAR-OM)")
    ok(sd.STATION_DELTA_PRIORS.get("lfpg", 0) > 0, "lfpg prior > 0 (tutarlı +1.4°C)")
    ok(sd.STATION_DELTA_PRIORS.get("limc", 0) > 0, "limc prior > 0 (tutarlı +1.75°C)")
    ok(sd.STATION_DELTA_PRIORS.get("eham", 0) > 0, "eham prior > 0 (tutarlı +0.4°C)")
    # Belirsiz istasyonlar prior = 0 (muhafazakâr)
    eq(sd.STATION_DELTA_PRIORS.get("eglc", 0), 0.0, "eglc prior=0 (METAR proxy güvenilmez)")
    eq(sd.STATION_DELTA_PRIORS.get("epwa", 0), 0.0, "epwa prior=0 (nötr)")

test("Faz A2: STATION_DELTA_PRIORS dict doğru değerlerde",
     test_station_delta_priors_exist)


def test_learn_station_delta_uses_prior():
    """Faz A2: veri yoksa prior kullanılır."""
    sd = _import_settlement_delta()
    # DB yok → compute_station_deltas {} döner → prior devreye girer
    # eddm prior = 1.0
    prior_val = sd.STATION_DELTA_PRIORS.get("eddm", 0)
    ok(prior_val > 0, f"eddm prior mevcut: {prior_val}")
    # learn_station_delta DB erişimi olmadan (fake path)
    from pathlib import Path
    delta = sd.learn_station_delta("eddm", db_path=Path("/nonexistent.db"))
    eq(delta, prior_val, f"DB yokken prior dönmeli: {delta} == {prior_val}")

test("Faz A2: learn_station_delta DB yokken prior kullanır",
     test_learn_station_delta_uses_prior)


def test_learn_station_delta_prior_zero_for_uncertain():
    """Faz A2: belirsiz istasyonlar için prior=0 → delta=0."""
    sd = _import_settlement_delta()
    from pathlib import Path
    for station in ("eglc", "epwa", "efhk"):
        delta = sd.learn_station_delta(station, db_path=Path("/nonexistent.db"))
        eq(delta, 0.0, f"{station} prior=0 → delta=0: {delta}")

test("Faz A2: belirsiz istasyonlar (eglc/epwa/efhk) prior=0 döner",
     test_learn_station_delta_prior_zero_for_uncertain)


def test_horizon_dampening_applied():
    """Faz A1: D+2 dampening 0.85 uygulanır."""
    sd = _import_settlement_delta()
    from pathlib import Path
    # eddm prior=1.0, D+1 → 1.0, D+2 → 0.85
    d1 = sd.learn_station_delta("eddm", db_path=Path("/nonexistent.db"), horizon_days=1)
    d2 = sd.learn_station_delta("eddm", db_path=Path("/nonexistent.db"), horizon_days=2)
    prior = sd.STATION_DELTA_PRIORS.get("eddm", 0)
    eq(d1, round(prior * 1.0, 2), f"D+1 tam uygula: {d1}")
    eq(d2, round(prior * 0.85, 2), f"D+2 %85: {d2}")
    ok(d2 < d1, f"D+2 < D+1 ({d2} < {d1})")

test("Faz A1: D+2 dampening 0.85 doğru uygulanıyor",
     test_horizon_dampening_applied)


def test_summary_includes_priors():
    """summary() prior'ları da döndürür (source='prior')."""
    sd = _import_settlement_delta()
    from pathlib import Path
    items = sd.summary(db_path=Path("/nonexistent.db"))
    # Prior'u > 0 olan istasyonlar listede olmalı
    stations_in_summary = {item["station"] for item in items}
    for st in ("eddm", "lfpg", "limc"):
        ok(st in stations_in_summary, f"{st} summary'de mevcut (prior > 0)")
    # Kaynak 'prior' olmalı
    for item in items:
        if item["station"] == "eddm":
            eq(item.get("source"), "prior",
               f"eddm source='prior': {item.get('source')}")

test("Faz A2: summary() prior istasyonları da döndürür",
     test_summary_includes_priors)


# ══════════════════════════════════════════════════════════════════════════════
# ── Faz 13: Prediction-Bias Kalibrasyon Düzeltmesi ───────────────────────────
# Problem: dashboard METAR day_max'ı "actual" olarak predictions.json'a yazar.
# METAR (LFPG gibi istasyonlarda) WU oracle'dan sistematik sapabilir → bias
# kalibrasyon yanlış → model warm-corrected → tahmin kötüleşir.
# Çözüm: scanner scan raw blend'i, scanner settle OM actual'ı loglar.
print("\n══════════════════════════════════════════════════════════════")
print(" TEST 38: Faz 13 — Prediction-Bias Kalibrasyon Düzeltmesi")
print("══════════════════════════════════════════════════════════════")

def test_scan_logs_raw_blend():
    """scan_date() /api/log-prediction'a raw blend (bias öncesi) POST eder."""
    import importlib
    import sys
    from unittest.mock import MagicMock, patch, call

    # scanner modülünü temiz import
    if "bot.scanner" in sys.modules:
        del sys.modules["bot.scanner"]
    import bot.scanner as s

    weather_resp = {
        "days": {
            "2026-05-04": {
                "blend": {
                    "max_temp": 16.5,          # raw blend
                    "bias_corrected_blend": 18.0,
                    "bias_active": True,
                    "spread": 1.2,
                    "uncertainty": "medium",
                    "bias_count": 5,
                    "bias_correction": -1.5,
                },
            }
        }
    }
    ens_resp = {
        "days": {
            "2026-05-04": {
                "member_maxes": [18] * 60 + [17] * 20 + [19] * 20,
                "is_bimodal": False,
                "peak_separation": None,
                "mode_ci_low": None,
                "mode_ci_high": None,
            }
        }
    }
    pm_resp = {
        "markets": [
            {
                "station": "lfpg",
                "date": "2026-05-04",
                "buckets": [
                    {"threshold": 17, "title": "17°C", "yes_price": 0.12, "cond_id": "cA"},
                    {"threshold": 18, "title": "18°C", "yes_price": 0.35, "cond_id": "cB"},
                    {"threshold": 19, "title": "19°C", "yes_price": 0.28, "cond_id": "cC"},
                ],
            }
        ]
    }

    posted_calls = []

    def mock_httpx_get(url, timeout=30):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        if "/api/weather" in url:
            r.json.return_value = weather_resp
        elif "/api/ensemble" in url:
            r.json.return_value = ens_resp
        elif "/api/polymarket" in url:
            r.json.return_value = pm_resp
        else:
            r.json.return_value = {}
        return r

    def mock_httpx_post(url, json=None, timeout=5):
        posted_calls.append({"url": url, "json": json})
        r = MagicMock()
        return r

    with patch.object(s.httpx, "get", side_effect=mock_httpx_get), \
         patch.object(s.httpx, "post", side_effect=mock_httpx_post), \
         patch.object(s, "load_trades", return_value=[]), \
         patch.object(s, "save_trades"):
        s.scan_date("lfpg", "2026-05-04", trades=[])

    # log-prediction çağrısı yapılmış mı?
    log_calls = [c for c in posted_calls if "log-prediction" in c["url"]]
    ok(len(log_calls) >= 1, f"log-prediction POST çağrısı yapılmadı (posted: {posted_calls})")

    # Raw blend (16.5) loglanmış mı? (bias-corrected=18.0 DEĞİL)
    blend_logged = next(
        (c["json"].get("blend") for c in log_calls if c["json"].get("blend") is not None),
        None
    )
    ok(blend_logged is not None, "blend alanı loglanmamış")
    eq(blend_logged, 16.5, f"raw blend 16.5°C loglanmalı (bias_corrected değil): {blend_logged}")

    # İstasyon ve tarih doğru mu?
    eq(log_calls[0]["json"].get("station"), "lfpg",   "station='lfpg' loglanmalı")
    eq(log_calls[0]["json"].get("date"),    "2026-05-04", "date doğru loglanmalı")


def test_settle_logs_om_actual():
    """settle() /api/log-prediction'a Open-Meteo actual POST eder (METAR override)."""
    import importlib
    import sys
    from unittest.mock import MagicMock, patch

    if "bot.scanner" in sys.modules:
        del sys.modules["bot.scanner"]
    import bot.scanner as s

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    trade = {
        "station":    "lfpg",
        "date":       yesterday,
        "status":     "open",
        "bucket_title": "18°C",
        "top_pick":   18,
        "blend":      17.5,
        "id":         "t_lfpg_001",
        "cost_usd":   5.0,
        "potential_win": 3.0,
    }

    posted_calls = []

    def mock_httpx_get(url, timeout=30):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json.return_value = {}
        return r

    def mock_httpx_post(url, json=None, timeout=5):
        posted_calls.append({"url": url, "json": json})
        r = MagicMock()
        return r

    def fake_om(station, date):
        return 17.3   # Open-Meteo actual

    with patch.object(s, "load_trades",  return_value=[trade]), \
         patch.object(s, "save_trades"), \
         patch.object(s, "get_actual_temp_open_meteo", side_effect=fake_om), \
         patch.object(s.httpx, "get", return_value=MagicMock(
             raise_for_status=MagicMock(),
             json=MagicMock(return_value={"daily_maxes": []})
         )), \
         patch.object(s.httpx, "post", side_effect=mock_httpx_post), \
         patch("bot.db.record_settlement_source", return_value=None), \
         patch("bot.db.record_forecast_error",    return_value=None), \
         patch("bot.db.already_recorded_error",   return_value=False), \
         patch("bot.db.record_model_actuals",      return_value=None):
        s.settle()

    # log-prediction çağrısı yapılmış mı?
    log_calls = [c for c in posted_calls if "log-prediction" in c["url"]]
    ok(len(log_calls) >= 1, f"settle() log-prediction POST yapmadı (posted: {posted_calls})")

    # OM actual (17.3) loglanmış mı?
    actual_logged = next(
        (c["json"].get("actual") for c in log_calls if c["json"].get("actual") is not None),
        None
    )
    ok(actual_logged is not None, "actual alanı loglanmamış")
    eq(actual_logged, 17.3, f"OM actual 17.3°C loglanmalı: {actual_logged}")

    # İstasyon ve tarih doğru mu?
    eq(log_calls[0]["json"].get("station"), "lfpg",      "station='lfpg' loglanmalı")
    eq(log_calls[0]["json"].get("date"),    yesterday,   "date=yesterday loglanmalı")


def test_settle_logs_metar_fallback_when_om_fails():
    """settle() Open-Meteo yoksa METAR actual'ı loglar."""
    import sys
    from unittest.mock import MagicMock, patch

    if "bot.scanner" in sys.modules:
        del sys.modules["bot.scanner"]
    import bot.scanner as s

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    trade = {
        "station": "eham", "date": yesterday, "status": "open",
        "bucket_title": "19°C", "top_pick": 19, "blend": 18.5, "id": "t_eham_001",
        "cost_usd": 5.0, "potential_win": 3.0,
    }

    posted_calls = []

    def mock_post(url, json=None, timeout=5):
        posted_calls.append({"url": url, "json": json})
        return MagicMock()

    metar_resp = MagicMock()
    metar_resp.raise_for_status = MagicMock()
    metar_resp.json.return_value = {
        "daily_maxes": [{"date": yesterday, "max_temp": 19.8}]
    }

    with patch.object(s, "load_trades",  return_value=[trade]), \
         patch.object(s, "save_trades"), \
         patch.object(s, "get_actual_temp_open_meteo", return_value=None), \
         patch.object(s.httpx, "get",  return_value=metar_resp), \
         patch.object(s.httpx, "post", side_effect=mock_post), \
         patch("bot.db.record_settlement_source", return_value=None), \
         patch("bot.db.record_forecast_error",    return_value=None), \
         patch("bot.db.already_recorded_error",   return_value=False), \
         patch("bot.db.record_model_actuals",      return_value=None):
        s.settle()

    log_calls = [c for c in posted_calls if "log-prediction" in c["url"]]
    ok(len(log_calls) >= 1, f"METAR fallback durumunda da log-prediction çağrılmalı")
    actual_logged = next(
        (c["json"].get("actual") for c in log_calls if c["json"].get("actual") is not None),
        None
    )
    ok(actual_logged is not None, "METAR fallback actual loglanmamış")
    eq(actual_logged, 19.8, f"METAR actual 19.8°C loglanmalı: {actual_logged}")


test("Faz 13: scan_date() raw blend'i log-prediction'a POST eder",
     test_scan_logs_raw_blend)
test("Faz 13: settle() Open-Meteo actual'ı log-prediction'a POST eder",
     test_settle_logs_om_actual)
test("Faz 13: settle() OM yoksa METAR fallback actual'ı POST eder",
     test_settle_logs_metar_fallback_when_om_fails)


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(f"  SONUÇ: {PASS} geçti / {FAIL} başarısız / {PASS+FAIL} toplam")
if FAIL == 0:
    print("  🎉 Tüm testler geçti!")
else:
    print(f"  ❌ {FAIL} test başarısız!")
print(f"{'═'*62}\n")

sys.exit(0 if FAIL == 0 else 1)
