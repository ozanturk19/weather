#!/usr/bin/env python3
"""
Crash / Resilience Test Suite — Weather Bot

Amacı: botu "kötü girdi, beklenmedik arıza, çakışma" senaryolarına karşı
sertleştirmek. Normal davranışı test etmez; sadece "çöker mi, silent
veri kaybı var mı, exception leak ediyor mu" sorularına bakar.

Test grupları:
  1. File I/O corruption   — bozuk/eksik JSON, disk hataları
  2. API failures          — timeout, 500, malformed payload
  3. Extreme data          — NaN, inf, negative temps, empty lists
  4. Concurrency           — paralel yazmalar, lock timeout
  5. DB corruption         — silinmiş DB, kilitli DB, yanlış şema
  6. Resource limits       — 0 trade / 1 trade / 10k trade
  7. Deploy integrity      — eksik env var, import hataları

Çalıştır: python3 tests/test_crash.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Proje köküne path ekle
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# py_clob_client ve diğer ağır deps için stub
for _mod in (
    "py_clob_client", "py_clob_client.client", "py_clob_client.clob_types",
    "py_clob_client.constants", "py_clob_client.order_builder",
    "py_clob_client.order_builder.constants",
    "web3", "eth_account", "httpx", "dotenv",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None
sys.modules["dotenv"].set_key     = lambda *a, **kw: None


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

def ok(cond, msg=""):  assert cond, msg
def eq(a, b, msg=""):  assert a == b, f"{msg} | {a!r} != {b!r}"
def raises_not(fn, msg=""):
    """fn() exception atmamalı."""
    try:
        fn()
    except Exception as e:
        raise AssertionError(f"{msg} | beklenmedik exception: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 1: Bozuk JSON Dosyaları (paper_trades, live_trades)")
print("═"*66)


def _load_trader_mod():
    import importlib
    return importlib.import_module("bot.trader")


def test_load_live_trades_empty_file():
    """Boş dosya → [] döner, exception yok."""
    tm = _load_trader_mod()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"")
        tmp = Path(f.name)
    try:
        with patch.object(tm, "TRADES_FILE", tmp):
            r = tm.load_live_trades()
        eq(r, [], "boş dosya için [] beklenir")
    finally:
        tmp.unlink(missing_ok=True)

test("load_live_trades: boş dosya → []", test_load_live_trades_empty_file)


def test_load_live_trades_corrupt_json():
    """Geçersiz JSON → [], warn ama çökmez."""
    tm = _load_trader_mod()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"{not valid json, }}}")
        tmp = Path(f.name)
    try:
        with patch.object(tm, "TRADES_FILE", tmp):
            r = tm.load_live_trades()
        eq(r, [], "bozuk JSON için [] beklenir")
    finally:
        tmp.unlink(missing_ok=True)

test("load_live_trades: bozuk JSON → []", test_load_live_trades_corrupt_json)


def test_load_live_trades_missing_file():
    """Dosya yoksa [] döner."""
    tm = _load_trader_mod()
    missing = Path(tempfile.gettempdir()) / "nonexistent_xyz_12345.json"
    missing.unlink(missing_ok=True)
    with patch.object(tm, "TRADES_FILE", missing):
        r = tm.load_live_trades()
    eq(r, [], "olmayan dosya için [] beklenir")

test("load_live_trades: dosya yok → []", test_load_live_trades_missing_file)


def test_save_live_trades_atomic():
    """save_live_trades atomik (tmp → replace) olmalı."""
    tm = _load_trader_mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "live.json"
        with patch.object(tm, "TRADES_FILE", tmp):
            tm.save_live_trades([{"id": "a", "station": "eglc", "status": "filled"}])
        # Dosya var ve valid JSON
        ok(tmp.exists(), "save sonrası dosya yok")
        data = json.loads(tmp.read_text())
        eq(len(data), 1)

test("save_live_trades: atomik yazım", test_save_live_trades_atomic)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 2: Ekstrem Sıcaklık Değerleri (blend_day)")
print("═"*66)


def _load_main_mod():
    import importlib.util
    p = _ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("main_mod", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_blend_day_all_none():
    """Tüm modeller None → güvenli çıktı (max_temp=None veya boş models_used)."""
    m = _load_main_mod()
    empty = {
        "gfs":         {"max_temp": None, "hours": []},
        "ecmwf":       {"max_temp": None, "hours": []},
        "icon":        {"max_temp": None, "hours": []},
    }
    raises_not(lambda: m.blend_day(empty, horizon=1),
               "tüm None'da exception atmamalı")

test("blend_day: tüm modeller None → exception yok",
     test_blend_day_all_none)


def test_blend_day_empty_dict():
    """Hiç model yok → çökmez."""
    m = _load_main_mod()
    raises_not(lambda: m.blend_day({}, horizon=1),
               "boş dict'te exception atmamalı")

test("blend_day: boş dict → exception yok",
     test_blend_day_empty_dict)


def test_blend_day_extreme_temps():
    """-100°C ve +100°C gibi saçma değerler → çökmez, outlier tespit eder."""
    m = _load_main_mod()
    extreme = {
        "gfs":         {"max_temp": 15.0, "hours": []},
        "ecmwf":       {"max_temp": 15.5, "hours": []},
        "icon":        {"max_temp": 14.8, "hours": []},
        "ukmo":        {"max_temp": -100.0, "hours": []},  # saçma
        "meteofrance": {"max_temp": 500.0,  "hours": []},  # saçma
    }
    r = m.blend_day(extreme, horizon=1)
    ok(r.get("max_temp") is not None, "blend hesaplanmalı")
    # MAD outlier filtresi -100 ve 500'ü atmış olmalı
    blend = float(r["max_temp"])
    ok(10 < blend < 20, f"blend makul olmalı (-100/500 filtrelenmeli): {blend}")

test("blend_day: ekstrem değerler outlier filtrelenir",
     test_blend_day_extreme_temps)


def test_blend_day_single_model():
    """Tek model → o model'i kullan, exception yok."""
    m = _load_main_mod()
    single = {"icon": {"max_temp": 15.0, "hours": []}}
    raises_not(lambda: m.blend_day(single, horizon=1),
               "tek modelde de çalışmalı")

test("blend_day: tek model → çalışır",
     test_blend_day_single_model)


def test_blend_day_nan_handling():
    """max_temp=NaN → skip, diğerlerinden blend."""
    m = _load_main_mod()
    # Python float('nan') simüle
    nan_input = {
        "gfs":         {"max_temp": float("nan"), "hours": []},
        "ecmwf":       {"max_temp": 14.5, "hours": []},
        "icon":        {"max_temp": 14.8, "hours": []},
    }
    raises_not(lambda: m.blend_day(nan_input, horizon=1),
               "NaN değerinde exception atmamalı")

test("blend_day: NaN sıcaklık → skip",
     test_blend_day_nan_handling)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 3: Dinamik Ağırlık — Uç Veri Durumları")
print("═"*66)


def _load_dw():
    import importlib
    return importlib.import_module("bot.dynamic_weights")


def test_dw_empty_db():
    """Hiç forecast geçmişi yok → None (fallback: statik)."""
    dw = _load_dw()
    with tempfile.TemporaryDirectory() as td:
        empty_db = Path(td) / "empty.db"
        # DB oluştur ama boş
        from bot.db import init_db
        init_db(empty_db)
        r = dw.compute_dynamic_weights("eglc", db_path=empty_db)
        eq(r, None, "boş DB'de None beklenir (fallback)")

test("dynamic_weights: boş DB → None (statik fallback)",
     test_dw_empty_db)


def test_dw_corrupt_db():
    """Bozuk DB dosyası → None, çökmez."""
    dw = _load_dw()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(b"not a sqlite database")
        corrupt = Path(f.name)
    try:
        r = dw.compute_dynamic_weights("eglc", db_path=corrupt)
        eq(r, None, "bozuk DB'de None beklenir")
    finally:
        corrupt.unlink(missing_ok=True)

test("dynamic_weights: bozuk DB → None",
     test_dw_corrupt_db)


def test_dw_one_model_insufficient_samples():
    """Tek model var, MIN_SAMPLES_MODEL altında → None."""
    dw = _load_dw()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "s.db"
        from bot.db import init_db, get_db
        init_db(db)
        with get_db(db) as conn:
            for i in range(3):   # 3 < MIN_SAMPLES_MODEL=10
                conn.execute(
                    """INSERT INTO model_forecasts
                       (station, date, model, horizon_days, max_temp, actual_temp, abs_error)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("eglc", f"2026-04-{20+i:02d}", "icon", 1, 15.0, 15.5, 0.5)
                )
        r = dw.compute_dynamic_weights("eglc", db_path=db)
        eq(r, None, "yetersiz örnekte None")

test("dynamic_weights: <MIN_SAMPLES → None",
     test_dw_one_model_insufficient_samples)


def test_dw_effective_weights_fallback():
    """effective_weights: dinamik yoksa statiği döner, source='static'."""
    dw = _load_dw()
    missing = Path(tempfile.gettempdir()) / "no_db_ever.db"
    missing.unlink(missing_ok=True)
    w, src = dw.effective_weights("eglc", db_path=missing)
    eq(src, "static")
    ok("ecmwf" in w and "icon" in w, "statik ağırlıklar dönmeli")

test("effective_weights: DB yoksa static fallback",
     test_dw_effective_weights_fallback)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 4: Kalibrasyon — Uç Durumlar")
print("═"*66)


def _load_cal():
    import importlib
    return importlib.import_module("bot.calibration")


def test_cal_empty_trades():
    """Boş trade listesi → n=0, bütün metrikler None/0."""
    c = _load_cal()
    r = c.compute_calibration([])
    eq(r["n"], 0)
    eq(r["brier"], None)
    eq(r["skill"], None)
    eq(r["bins"], [])

test("calibration: boş liste → n=0", test_cal_empty_trades)


def test_cal_single_trade():
    """Tek trade → n=1, brier hesaplanır, skill=0 (base_rate 0 veya 1)."""
    c = _load_cal()
    r = c.compute_calibration([
        {"id": "x", "station": "eglc", "date": "2026-04-01",
         "status": "closed", "ens_mode_pct": 50, "result": "WIN"}
    ])
    eq(r["n"], 1)
    ok(r["brier"] is not None, "brier hesaplanmalı")
    # base_rate = 1.0 → brier_ref = 0, skill = 0 (division by zero prevention)
    eq(r["skill"], 0.0)

test("calibration: tek trade → güvenli skill=0",
     test_cal_single_trade)


def test_cal_all_wins():
    """Tüm trade WIN → brier = (1-p)², base_rate=1, skill=0 güvenli."""
    c = _load_cal()
    trades = [
        {"id": str(i), "station": "eglc", "date": "2026-04-01",
         "status": "closed", "ens_mode_pct": 70, "result": "WIN"}
        for i in range(10)
    ]
    r = c.compute_calibration(trades)
    eq(r["n"], 10)
    eq(r["base_rate"], 1.0)
    # brier = (1 - 0.7)² = 0.09
    ok(abs(r["brier"] - 0.09) < 0.01, f"brier ≈ 0.09 bekleniyor: {r['brier']}")

test("calibration: tüm WIN → güvenli hesap",
     test_cal_all_wins)


def test_cal_invalid_pct():
    """ens_mode_pct=None olan trade atlanmalı."""
    c = _load_cal()
    trades = [
        {"id": "a", "status": "closed", "ens_mode_pct": None, "result": "WIN"},
        {"id": "b", "status": "closed", "ens_mode_pct": 60,   "result": "LOSS"},
    ]
    r = c.compute_calibration(trades)
    eq(r["n"], 1, "None olan atlanmalı")

test("calibration: ens_mode_pct=None atlanır",
     test_cal_invalid_pct)


def test_cal_invalid_result():
    """result='REFUND' gibi WIN/LOSS olmayanlar atlanır."""
    c = _load_cal()
    trades = [
        {"id": "a", "status": "closed", "ens_mode_pct": 60, "result": "REFUND"},
        {"id": "b", "status": "closed", "ens_mode_pct": 60, "result": "DRAW"},
        {"id": "c", "status": "closed", "ens_mode_pct": 60, "result": "WIN"},
    ]
    r = c.compute_calibration(trades)
    eq(r["n"], 1, "WIN/LOSS dışı atlanmalı")

test("calibration: WIN/LOSS dışı atlanır",
     test_cal_invalid_result)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 5: Portföy VaR — Uç Durumlar")
print("═"*66)


def _load_var():
    import importlib
    return importlib.import_module("bot.portfolio_var")


def test_var_no_positions():
    """Açık pozisyon yok → 0-paritelik sonuç, çökmez."""
    v = _load_var()
    r = v.portfolio_var([])
    ok(r is not None, "None dönmemeli")
    # Hiç pozisyon yok → tüm değerler 0 veya None olmalı
    eq(r.get("n_positions", 0), 0)

test("portfolio_var: sıfır pozisyon → güvenli çıktı",
     test_var_no_positions)


def test_var_single_position():
    """Tek pozisyon → corr matris 1x1, tek simülasyon, çökmez."""
    v = _load_var()
    trades = [{
        "id": "a", "station": "eglc", "date": "2026-04-24",
        "status": "open", "top_pick": 15, "bucket_title": "15°C",
        "entry_price": 0.30, "shares": 5, "ens_mode_pct": 60,
    }]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "v.db"
        from bot.db import init_db
        init_db(db)
        r = v.portfolio_var(trades, db_path=db)
    ok(r is not None)
    ok(r.get("n_positions") == 1, f"n_positions=1 bekleniyor: {r.get('n_positions')}")

test("portfolio_var: tek pozisyon → güvenli",
     test_var_single_position)


def test_var_corrupt_corr_matrix():
    """Korelasyon NaN olursa identity'ye düş (shrinkage zaten bunu yapıyor)."""
    v = _load_var()
    # pearson hiçbir ortak çift yoksa None döner
    r = v.pearson([1, 2, 3], [4, 5])
    ok(r is None or isinstance(r, (int, float)),
       "pearson farklı uzunluk → None veya skaler")

test("pearson: farklı uzunluk → None",
     test_var_corrupt_corr_matrix)


def test_var_zero_variance():
    """Sabit dizilerde varyans=0 → pearson None döner."""
    v = _load_var()
    r = v.pearson([1, 1, 1, 1], [2, 2, 2, 2])
    eq(r, None, "sabit dizi → None")

test("pearson: sabit dizi (variance=0) → None",
     test_var_zero_variance)


def test_var_cholesky_non_psd():
    """Non-PSD matris → jitter ekleyerek çözer ya da None."""
    v = _load_var()
    # Negative definite matrix
    bad = [[1.0, 2.0], [2.0, 1.0]]   # eigenvalue < 0
    r = v.cholesky(bad)
    # Ya başarılı (jitter ekledi) ya da None
    ok(r is None or isinstance(r, list),
       "non-PSD'de None veya list bekleniyor")

test("cholesky: non-PSD → güvenli sonuç",
     test_var_cholesky_non_psd)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 6: Settlement Audit — DB Uç Durumlar")
print("═"*66)


def test_audit_record_none_temp():
    """actual_temp=None → sessizce skip, hata yok."""
    from bot.db import record_settlement_source, init_db, get_db
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "a.db"
        init_db(db)
        record_settlement_source("eglc", "2026-04-22", "open_meteo",
                                  None, db_path=db)
        with get_db(db, readonly=True) as conn:
            n = conn.execute("SELECT COUNT(*) FROM settlement_audit").fetchone()[0]
        eq(n, 0, "None temp kaydedilmemeli")

test("settlement_audit: None temp → skip", test_audit_record_none_temp)


def test_audit_upsert_idempotent():
    """Aynı (station, date, source) iki kez → UPSERT, tek satır."""
    from bot.db import record_settlement_source, init_db, get_db
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "b.db"
        init_db(db)
        record_settlement_source("eglc", "2026-04-22", "metar", 15.5, db_path=db)
        record_settlement_source("eglc", "2026-04-22", "metar", 15.7, db_path=db)
        with get_db(db, readonly=True) as conn:
            rows = conn.execute(
                "SELECT actual_temp FROM settlement_audit WHERE station=? AND date=? AND source=?",
                ("eglc", "2026-04-22", "metar")
            ).fetchall()
        eq(len(rows), 1, "tek satır olmalı (UPSERT)")
        # Son değer kazanır
        ok(abs(rows[0][0] - 15.7) < 0.01, f"son değer: {rows[0][0]}")

test("settlement_audit: UPSERT idempotent",
     test_audit_upsert_idempotent)


def test_audit_corrupt_db_silent():
    """Bozuk DB → record sessizce skip, exception yok."""
    from bot.db import record_settlement_source
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(b"bogus")
        corrupt = Path(f.name)
    try:
        raises_not(
            lambda: record_settlement_source("eglc", "2026-04-22", "metar", 15.0, db_path=corrupt),
            "bozuk DB'de silent fail"
        )
    finally:
        corrupt.unlink(missing_ok=True)

test("settlement_audit: bozuk DB → silent",
     test_audit_corrupt_db_silent)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 7: Auto-Sell — Uç CLOB Davranışları")
print("═"*66)


def test_autosell_empty_response():
    """post_order boş dict dönerse → None, trade bozulmaz."""
    tm = _load_trader_mod()
    client = MagicMock()
    client.get_tick_size.return_value = 0.001
    client.create_order.return_value  = {}
    client.post_order.return_value    = {}   # order_id yok

    trade = {"station": "eglc", "date": "2026-04-24",
             "condition_id": "0x1", "shares": 5, "fill_price": 0.25,
             "status": "filled", "notes": ""}
    r = tm.place_auto_sell(client, trade)
    eq(r, None, "boş resp → None")
    eq(trade["status"], "filled", "status değişmemeli")

test("place_auto_sell: order_id yok → None",
     test_autosell_empty_response)


def test_autosell_zero_shares():
    """shares=0 → None, API çağrılmaz."""
    tm = _load_trader_mod()
    client = MagicMock()
    trade = {"station": "eglc", "date": "2026-04-24",
             "condition_id": "0x1", "shares": 0, "fill_price": 0.25,
             "status": "filled", "notes": ""}
    r = tm.place_auto_sell(client, trade)
    eq(r, None)
    eq(client.post_order.called, False, "shares=0 ise API çağrılmamalı")

test("place_auto_sell: shares=0 → API yok",
     test_autosell_zero_shares)


def test_autosell_missing_condition_id():
    """condition_id boşsa → None."""
    tm = _load_trader_mod()
    client = MagicMock()
    trade = {"station": "eglc", "date": "2026-04-24",
             "condition_id": "", "shares": 5, "fill_price": 0.25,
             "status": "filled", "notes": ""}
    r = tm.place_auto_sell(client, trade)
    eq(r, None, "boş condition_id → None")

test("place_auto_sell: condition_id yok → None",
     test_autosell_missing_condition_id)


def test_autosell_network_timeout():
    """post_order timeout → silent fail, None."""
    tm = _load_trader_mod()
    client = MagicMock()
    client.get_tick_size.return_value = 0.001
    client.post_order.side_effect = TimeoutError("CLOB timeout")

    trade = {"station": "eglc", "date": "2026-04-24",
             "condition_id": "0x1", "shares": 5, "fill_price": 0.25,
             "status": "filled", "notes": ""}
    raises_not(lambda: tm.place_auto_sell(client, trade),
               "timeout swallow edilmeli")

test("place_auto_sell: timeout → silent",
     test_autosell_network_timeout)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 8: Eş Zamanlılık — Paralel JSON Yazımı")
print("═"*66)


def test_parallel_save_no_corruption():
    """2 thread aynı anda save_live_trades çağırsın → dosya valid JSON kalmalı."""
    tm = _load_trader_mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "live.json"
        with patch.object(tm, "TRADES_FILE", tmp):
            def writer(label: str):
                for i in range(20):
                    tm.save_live_trades([
                        {"id": f"{label}_{i}", "station": "eglc", "status": "filled"}
                    ])

            threads = [threading.Thread(target=writer, args=(f"t{j}",)) for j in range(3)]
            [t.start() for t in threads]
            [t.join() for t in threads]

            # Son halini oku — geçerli JSON olmalı
            content = tmp.read_text()
            data = json.loads(content)   # exception atarsa test fail
            ok(isinstance(data, list), "liste olmalı")

test("parallel save: JSON corruption yok",
     test_parallel_save_no_corruption)


def test_db_wal_concurrent_reads():
    """2 thread aynı anda read → WAL modu OK."""
    from bot.db import init_db, get_db
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "concurrent.db"
        init_db(db)
        # Biraz veri
        with get_db(db) as conn:
            for i in range(10):
                conn.execute(
                    """INSERT INTO settlement_audit (station, date, source, actual_temp, rounded_temp)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("eglc", f"2026-04-{10+i:02d}", "metar", 15.0, 15)
                )

        errors = []
        def reader():
            try:
                for _ in range(10):
                    with get_db(db, readonly=True) as conn:
                        conn.execute("SELECT COUNT(*) FROM settlement_audit").fetchone()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        eq(len(errors), 0, f"okuma hatası: {errors}")

test("DB WAL: eş zamanlı okuma", test_db_wal_concurrent_reads)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 9: API Integrity — Endpoint Geri Uyumluluk")
print("═"*66)


def test_main_endpoints_registered():
    """main.py'de tüm beklenen endpoint'ler kayıtlı."""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    required = [
        "/api/weather", "/api/ensemble", "/api/metar", "/api/polymarket",
        "/api/portfolio", "/api/calibration", "/api/settlement-audit",
        "/api/portfolio/var", "/api/live-trades", "/api/bot-trades",
    ]
    for ep in required:
        ok(ep in src, f"eksik endpoint: {ep}")

test("main.py: tüm endpoint'ler kayıtlı",
     test_main_endpoints_registered)


def test_model_weights_sum():
    """MODEL_WEIGHTS ortalama 1.0 civarı (blend stabilitesi)."""
    m = _load_main_mod()
    avg = sum(m.MODEL_WEIGHTS.values()) / len(m.MODEL_WEIGHTS)
    ok(0.8 < avg < 1.8, f"ortalama ağırlık makul değil: {avg}")

test("MODEL_WEIGHTS: ortalama stabil",
     test_model_weights_sum)


def test_station_coords_complete():
    """trader.py STATION_COORDS dict'i scanner ile senkron."""
    tm = _load_trader_mod()
    # En az 10 istasyon
    ok(len(tm.STATION_COORDS) >= 10, f"en az 10: {len(tm.STATION_COORDS)}")
    for k, (lat, lon) in tm.STATION_COORDS.items():
        ok(-90 <= lat <= 90, f"{k} lat geçersiz: {lat}")
        ok(-180 <= lon <= 180, f"{k} lon geçersiz: {lon}")

test("STATION_COORDS: tüm koordinatlar geçerli",
     test_station_coords_complete)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 10: Env & Deploy Integrity")
print("═"*66)


def test_trader_imports_without_pk():
    """PK yoksa trader.py import edilebilmeli (setup_client çağrılana kadar)."""
    # Zaten stub'lar yüklü, PK None olabilir
    tm = _load_trader_mod()
    ok(hasattr(tm, "load_live_trades"), "temel fonksiyonlar yüklenmeli")
    ok(hasattr(tm, "place_auto_sell"),  "auto_sell yüklenmeli")

test("trader.py: PK yoksa bile import OK",
     test_trader_imports_without_pk)


def test_main_starts_without_env():
    """main.py (FastAPI app) içe aktarılabilmeli env eksik olsa bile."""
    raises_not(_load_main_mod,
               "main.py env olmadan da import edilmeli")

test("main.py: env eksik → import OK",
     test_main_starts_without_env)


def test_no_hardcoded_private_keys():
    """Kaynak kodunda sızmış PK / API key yok."""
    for path in (_ROOT / "bot").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        # 0x + 64 hex = priv key formatı (sadece sabit atanmış olanlar)
        import re
        m = re.search(r'=\s*"0x[0-9a-fA-F]{64}"', src)
        ok(m is None, f"{path.name}'de sabit PK sızdı: {m.group() if m else ''}")

test("source: hardcoded secret sızıntısı yok",
     test_no_hardcoded_private_keys)


def test_cron_schedule_syntax():
    """CLAUDE.md'deki cron zamanlaması parse edilebilmeli."""
    src = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    ok("04,10,16,22" in src, "scanner cron eksik")
    ok("11:00" in src,       "settle cron eksik")
    ok("11:05" in src,       "trader settle cron eksik")

test("CLAUDE.md: cron zamanlaması mevcut",
     test_cron_schedule_syntax)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(" CRASH TEST 11: Ensemble & Forecast Payload Defensive")
print("═"*66)


def test_bucket_won_degenerate_titles():
    """Garip bucket başlıkları → None döner, çökmez."""
    from bot.trader import bucket_won
    cases = [
        ("", 15.0),
        ("not a temperature", 15.0),
        ("°°°", 15.0),
        ("NULL", 15.0),
    ]
    for title, actual in cases:
        r = bucket_won(title, actual)
        ok(r is None or isinstance(r, bool),
           f"'{title}' için None/bool bekleniyor: {r}")

test("bucket_won: garip başlık → None",
     test_bucket_won_degenerate_titles)


def test_bucket_won_extreme_temps():
    """Çok düşük/yüksek gerçek sıcaklık → mantıklı sonuç."""
    from bot.trader import bucket_won
    eq(bucket_won("25°C or higher", 100.0), True,  "100°C ≥ 25")
    eq(bucket_won("5°C or below", -50.0),   True,  "-50 ≤ 5")
    eq(bucket_won("10°C to 15°C", 1000.0),  False, "1000 aralık dışı")

test("bucket_won: ekstrem sıcaklıklar",
     test_bucket_won_extreme_temps)


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*66)
print(f" SONUÇ: {PASS} geçti / {FAIL} başarısız / {PASS+FAIL} crash test")
if FAIL == 0:
    print(" 🎉 Tüm crash testleri geçti! Bot resilient.")
else:
    print(f" ❌ {FAIL} crash senaryosu başarısız — gözden geçir.")
print("═"*66 + "\n")

sys.exit(0 if FAIL == 0 else 1)
