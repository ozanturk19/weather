#!/usr/bin/env python3
"""
İstasyon Win-Rate Circuit Breaker (Faz 8).

Amaç: Bir istasyonun son N (=10) kapalı live trade'inin win-rate'i %30 altına
düşerse, o istasyonu 3 gün otomatik pause et. 90 günlük statik skill pause
(scanner.py STATION_SKILL_PAUSE) uzun dönemli; bu ise kısa dönem davranış
değişikliklerine hızlı reaksiyon için.

Pause yazımı: bot.db.set_station_paused() + auto_resume_at = now + 3 gün.
should_pause_station auto_resume_at süresinden sonra otomatik False döner.

Kullanım:
    python3 bot/circuit_breaker.py enforce

Cron'a eklenebilir (tipik: günde bir kez settle sonrası).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# ── Eşikler ─────────────────────────────────────────────────────────────────
CB_LOOKBACK_TRADES   = 10     # son kaç kapalı live trade'e bakılacak
CB_MIN_WIN_RATE      = 0.30   # altındaysa devre kes
CB_PAUSE_DAYS        = 3      # kaç gün pause edilsin
CB_MIN_SAMPLES       = 10     # en az bu kadar kapalı trade yoksa karar verme
CB_REASON_TAG        = "auto_circuit_breaker"


def _recent_live_trades(station: str, limit: int, db_path: Path | None = None) -> list[dict]:
    """İstasyonun son `limit` kapalı live trade'ini döner (en yeni ilk)."""
    try:
        from bot.db import DB_PATH, get_db
        path = db_path or DB_PATH
        with get_db(path, readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT station, status, result, pnl_usdc, settled_at
                FROM live_trades
                WHERE station = ?
                  AND status IN ('settled_win', 'settled_loss')
                ORDER BY COALESCE(settled_at, placed_at) DESC
                LIMIT ?
                """,
                (station, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def check_station_circuit_breaker(
    station: str,
    lookback: int = CB_LOOKBACK_TRADES,
    min_win_rate: float = CB_MIN_WIN_RATE,
    min_samples: int = CB_MIN_SAMPLES,
    db_path: Path | None = None,
) -> bool:
    """Circuit breaker tetiklenmeli mi?

    True → son `lookback` trade'de win-rate < `min_win_rate` (pause uygula).
    False → kararlı veya veri yetersiz.
    """
    trades = _recent_live_trades(station, lookback, db_path=db_path)
    if len(trades) < min_samples:
        return False
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    wr   = wins / len(trades)
    return wr < min_win_rate


def enforce_circuit_breakers(
    stations: list[str] | None = None,
    pause_days: int = CB_PAUSE_DAYS,
    db_path: Path | None = None,
) -> dict:
    """Tüm istasyonlar için circuit breaker'ı çalıştır.

    Tetiklenen istasyonlar set_station_paused(paused=True, reason=CB_REASON_TAG,
    auto_resume_at=now+pause_days*86400) ile DB'ye yazılır.

    Döner: {"paused": [...], "resumed": [...], "checked": N}
    """
    # scanner.STATIONS listesini tek kaynak olarak kullan
    if stations is None:
        try:
            from bot.scanner import STATIONS
            stations = list(STATIONS)
        except Exception:
            stations = []

    from bot.db import DB_PATH, set_station_paused, get_db
    path = db_path or DB_PATH

    now_ts = int(time.time())
    resume_ts = now_ts + int(pause_days * 86400)

    result = {"paused": [], "resumed": [], "checked": 0}

    # Mevcut pause durumunu oku (auto resume tespit için)
    current: dict = {}
    try:
        with get_db(path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT station, paused, reason, auto_resume_at FROM station_status"
            ).fetchall()
            current = {r[0]: dict(station=r[0], paused=r[1], reason=r[2], auto_resume_at=r[3])
                       for r in rows}
    except Exception:
        current = {}

    for st in stations:
        result["checked"] += 1
        triggered = check_station_circuit_breaker(st, db_path=path)
        row = current.get(st, {})
        is_cb_paused = (
            row.get("paused") == 1
            and row.get("reason") == CB_REASON_TAG
        )
        auto_resume = row.get("auto_resume_at")

        if triggered:
            # Yeni veya pause süresi uzatılacak
            set_station_paused(
                st,
                paused=True,
                reason=CB_REASON_TAG,
                auto_resume_at=resume_ts,
                db_path=path,
            )
            result["paused"].append(st)
            print(
                f"  ⛔ CIRCUIT BREAKER  {st.upper()}  "
                f"{pause_days}g pause (win-rate < {CB_MIN_WIN_RATE:.0%})"
            )
        elif is_cb_paused and auto_resume is not None and int(auto_resume) <= now_ts:
            # CB tarafından pause edilmişti + süre dolmuş + artık tetiklenmiyor
            set_station_paused(
                st,
                paused=False,
                reason=None,
                auto_resume_at=None,
                db_path=path,
            )
            result["resumed"].append(st)
            print(f"  🟢 RESUME  {st.upper()}  circuit breaker süresi doldu")

    if not result["paused"] and not result["resumed"]:
        print("  ✅ Circuit breaker: tüm istasyonlar normal aralıkta")
    return result


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "enforce"
    if cmd == "enforce":
        out = enforce_circuit_breakers()
        print(
            f"\n  📋 Kontrol: {out['checked']} "
            f"| Pause: {len(out['paused'])} "
            f"| Resume: {len(out['resumed'])}\n"
        )
    else:
        print("Kullanım: python3 bot/circuit_breaker.py enforce")
        sys.exit(1)
