#!/usr/bin/env python3
"""
SQLite altyapısı — birincil kayıt hedefi, JSON insan-okunur yedek (Faz 7).

Tasarım ilkeleri (2026-04 güncel):
  - SQLite = **birincil yazma hedefi**. Her trade state değişikliği önce DB'ye
    atomic transaction olarak yazılır, sonra JSON'a dump edilir.
  - JSON = insan-okunur yedek + eski araçlarla uyumluluk (paper_trades.json,
    live_trades.json). JSON bozulursa `rebuild_json_from_db()` ile yeniden
    oluşturulabilir. DB bozulursa `sync_from_json()` ile JSON'dan kurulur.
  - WAL + synchronous=NORMAL: crash sonrası otomatik kurtarma, hızlı yazma.

  Write akışı (save_live_trades / save_trades içinde):
    1. write_*_trades_list(trades)  → SQLite transaction (crash-safe)
    2. json.dumps(trades) → disk    → yedek + okuma kolaylığı

Kullanım:
  from bot.db import get_db, sync_all, init_db

  with get_db() as conn:
      rows = conn.execute("SELECT * FROM paper_trades WHERE station=?", ("eglc",)).fetchall()
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
PAPER_JSON  = BASE_DIR / "bot" / "paper_trades.json"
LIVE_JSON   = BASE_DIR / "bot" / "live_trades.json"
DB_PATH     = BASE_DIR / "bot" / "trades.db"


# ── Şema ────────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ───────────────────────────────────────────────────────────────────────
-- paper_trades: paper_trades.json'ın birebir aynası
-- JSON her zaman kaynaktır; bu tablo sync_paper_trades() ile yenilenir
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_trades (
    id            TEXT PRIMARY KEY,            -- JSON'daki id alanı
    station       TEXT NOT NULL,
    date          TEXT NOT NULL,               -- 'YYYY-MM-DD'
    blend         REAL,
    spread        REAL,
    uncertainty   TEXT,
    top_pick      INTEGER,
    raw_top_pick  INTEGER,
    bias_applied  INTEGER,
    ens_mode_pct  INTEGER,
    ens_2nd_pick  INTEGER,
    ens_2nd_pct   INTEGER,
    -- Faz 2: ensemble şekil metrikleri
    ens_is_bimodal   INTEGER,
    ens_peak_sep     INTEGER,
    ens_mode_ci_low  INTEGER,
    ens_mode_ci_high INTEGER,
    -- Faz 3: sinyal kalitesi
    signal_score     INTEGER,
    signal_grade     TEXT,
    bucket_title  TEXT,
    condition_id  TEXT,
    entry_price   REAL,
    shares        REAL,
    cost_usd      REAL,                        -- yeni format
    size_usd      REAL,                        -- eski format (cost_usd yoksa)
    potential_win REAL,
    liquidity     REAL,
    status        TEXT NOT NULL,               -- open | closed | superseded
    entered_at    TEXT,                        -- ISO timestamp
    actual_temp   REAL,
    result        TEXT,                        -- WIN | LOSS | NULL
    pnl           REAL,
    settled_at    TEXT,
    two_bucket    INTEGER,                     -- 0/1
    notes         TEXT,
    synced_at     INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_paper_station_date ON paper_trades(station, date);
CREATE INDEX IF NOT EXISTS idx_paper_status       ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_result       ON paper_trades(result);

-- ───────────────────────────────────────────────────────────────────────
-- live_trades: live_trades.json aynası
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS live_trades (
    id              TEXT PRIMARY KEY,
    paper_id        TEXT,
    station         TEXT NOT NULL,
    date            TEXT NOT NULL,
    top_pick        INTEGER,
    bucket_title    TEXT,
    condition_id    TEXT,
    order_id        TEXT,
    limit_price     REAL,
    shares          REAL,
    cost_usdc       REAL,
    fill_price      REAL,
    fill_time       TEXT,
    placed_at       TEXT,
    expires_at      TEXT,
    horizon         TEXT,
    status          TEXT NOT NULL,             -- pending_fill | filled | settled_win | settled_loss | cancelled | sell_pending
    result          TEXT,
    pnl_usdc        REAL,
    settled_at      TEXT,
    notes           TEXT,
    redeemed        INTEGER DEFAULT 0,
    redeemed_at     TEXT,
    redeem_tx       TEXT,
    sell_order_id   TEXT,
    sell_placed_at  TEXT,
    sell_price      REAL,
    synced_at       INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_live_station_date ON live_trades(station, date);
CREATE INDEX IF NOT EXISTS idx_live_status       ON live_trades(status);
CREATE INDEX IF NOT EXISTS idx_live_order_id     ON live_trades(order_id);

-- ───────────────────────────────────────────────────────────────────────
-- forecast_errors: her settlement sonrası kaydedilen model hataları
-- (Faz 3-4 için gerekli: Kalman bias, dynamic weighting, CRPS)
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS forecast_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,               -- forecast target date
    station       TEXT NOT NULL,
    horizon_days  INTEGER,                     -- 0, 1, 2
    month         INTEGER,                     -- 1-12
    season        TEXT,                        -- winter|spring|summer|autumn

    blend         REAL,                        -- p50 ensemble (bias öncesi)
    top_pick      INTEGER,                     -- modun round'u
    spread        REAL,                        -- p90-p10 / std
    uncertainty   TEXT,

    actual_temp   REAL NOT NULL,
    error_c       REAL NOT NULL,               -- blend - actual
    abs_error_c   REAL NOT NULL,
    pick_error    INTEGER,                     -- top_pick - actual (integer)

    trade_id      TEXT,                        -- ilgili paper_trade.id
    created_at    INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_err_station_date ON forecast_errors(station, date);
CREATE INDEX IF NOT EXISTS idx_err_station      ON forecast_errors(station);
CREATE INDEX IF NOT EXISTS idx_err_created      ON forecast_errors(created_at);

-- ───────────────────────────────────────────────────────────────────────
-- model_forecasts: her gün her model için ham max_temp kaydı (Faz 4)
-- Settle sırasında actual_temp doldurulur → per-model RMSE hesaplanabilir
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_forecasts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       TEXT NOT NULL,
    date          TEXT NOT NULL,               -- forecast target date
    model         TEXT NOT NULL,               -- gfs|ecmwf|icon|ukmo|meteofrance
    horizon_days  INTEGER,                     -- 0, 1, 2
    max_temp      REAL NOT NULL,               -- tahminin günlük maksimumu
    actual_temp   REAL,                        -- settle sonrası doldurulur
    abs_error     REAL,                        -- |max_temp - actual|
    recorded_at   INTEGER DEFAULT (strftime('%s','now')),
    settled_at    INTEGER,
    UNIQUE (station, date, model)              -- günde bir model bir kayıt
);

CREATE INDEX IF NOT EXISTS idx_mf_station_model ON model_forecasts(station, model);
CREATE INDEX IF NOT EXISTS idx_mf_date          ON model_forecasts(date);
CREATE INDEX IF NOT EXISTS idx_mf_settled       ON model_forecasts(settled_at);

-- ───────────────────────────────────────────────────────────────────────
-- bias_corrections: Kalman filter bias history (Faz 3)
-- Her settle sonrası istasyon için güncellenir
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bias_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       TEXT NOT NULL,
    date          TEXT NOT NULL,               -- observation date
    measured_err  REAL NOT NULL,               -- predicted - actual (bu gözlem)
    bias_est      REAL NOT NULL,               -- Kalman state x
    uncertainty   REAL NOT NULL,               -- Kalman covariance P
    correction    REAL NOT NULL,               -- apply to top_pick
    created_at    INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_bias_station_date ON bias_corrections(station, date);
CREATE INDEX IF NOT EXISTS idx_bias_station      ON bias_corrections(station);

-- ───────────────────────────────────────────────────────────────────────
-- model_weights: istasyon × model rolling RMSE bazlı dinamik ağırlık (Faz 4)
-- Her settle sonrası güncellenir
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_weights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT NOT NULL,
    model        TEXT NOT NULL,                -- gfs, ecmwf_ifs, icon, ukmo, meteofrance
    weight       REAL NOT NULL,
    rmse_30d     REAL,
    n_samples    INTEGER,
    recorded_at  INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_weights_station_recorded ON model_weights(station, recorded_at);
CREATE INDEX IF NOT EXISTS idx_weights_station_model    ON model_weights(station, model);

-- ───────────────────────────────────────────────────────────────────────
-- settlement_audit: çok-kaynaklı settle doğrulama (Faz 6b)
-- Aynı (station, date) için her kaynak (open-meteo / metar / wu / ...)
-- tek satır. Kaynaklar arası fark izleme için.
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settlement_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT NOT NULL,
    date         TEXT NOT NULL,               -- gözlem tarihi
    source       TEXT NOT NULL,               -- 'open-meteo' | 'metar' | 'wu' | ...
    actual_temp  REAL,                        -- ham °C (yuvarlamasız)
    rounded_temp INTEGER,                     -- settlement için yuvarlatılmış
    recorded_at  INTEGER DEFAULT (strftime('%s','now')),
    UNIQUE (station, date, source)
);

CREATE INDEX IF NOT EXISTS idx_sa_date    ON settlement_audit(date);
CREATE INDEX IF NOT EXISTS idx_sa_station ON settlement_audit(station, date);

-- ───────────────────────────────────────────────────────────────────────
-- Station Status: pause/resume durumunu kalıcılaştırır (Faz 7).
-- Sadece kod içinde statik set tutmak yerine, runtime'da toggle edilebilir.
-- ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS station_status (
    station        TEXT PRIMARY KEY,
    paused         INTEGER NOT NULL DEFAULT 0,    -- 0=aktif, 1=durduruldu
    reason         TEXT,                           -- insan-okunur sebep
    auto_resume_at INTEGER,                        -- unix ts; null=manuel pause
    updated_at     INTEGER DEFAULT (strftime('%s','now'))
);
"""


# ── Bağlantı ────────────────────────────────────────────────────────────────
@contextmanager
def get_db(db_path: Path = DB_PATH, readonly: bool = False):
    """Thread-safe SQLite bağlantısı. WAL mode sayesinde eş zamanlı read OK."""
    if readonly:
        uri  = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    else:
        conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Şema oluştur (idempotent) + olası yeni kolonları ekle (migration)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_add_columns(conn)


def _migrate_add_columns(conn) -> None:
    """Şema evrimi — eksik kolonları idempotent olarak ekler."""
    # paper_trades: Faz 2 şekil metrikleri
    existing = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
    new_cols = [
        ("ens_is_bimodal",   "INTEGER"),
        ("ens_peak_sep",     "INTEGER"),
        ("ens_mode_ci_low",  "INTEGER"),
        ("ens_mode_ci_high", "INTEGER"),
        # Faz 3
        ("signal_score",     "INTEGER"),
        ("signal_grade",     "TEXT"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")

    # Faz 8: station_status tablosuna auto_resume_at
    try:
        st_existing = {
            row[1] for row in conn.execute("PRAGMA table_info(station_status)")
        }
        if "auto_resume_at" not in st_existing:
            conn.execute(
                "ALTER TABLE station_status ADD COLUMN auto_resume_at INTEGER"
            )
    except Exception:
        pass


# ── JSON → SQLite Mirror Senkronizasyonu ────────────────────────────────────
PAPER_FIELDS = [
    "id", "station", "date", "blend", "spread", "uncertainty",
    "top_pick", "raw_top_pick", "bias_applied",
    "ens_mode_pct", "ens_2nd_pick", "ens_2nd_pct",
    # Faz 2 şekil metrikleri
    "ens_is_bimodal", "ens_peak_sep", "ens_mode_ci_low", "ens_mode_ci_high",
    # Faz 3 sinyal kalitesi
    "signal_score", "signal_grade",
    "bucket_title", "condition_id", "entry_price", "shares",
    "cost_usd", "size_usd", "potential_win", "liquidity",
    "status", "entered_at", "actual_temp", "result", "pnl", "settled_at",
    "two_bucket", "notes",
]

LIVE_FIELDS = [
    "id", "paper_id", "station", "date", "top_pick", "bucket_title",
    "condition_id", "order_id", "limit_price", "shares", "cost_usdc",
    "fill_price", "fill_time", "placed_at", "expires_at", "horizon",
    "status", "result", "pnl_usdc", "settled_at", "notes",
    "redeemed", "redeemed_at", "redeem_tx",
    "sell_order_id", "sell_placed_at", "sell_price",
]


def _bool_to_int(v):
    if isinstance(v, bool):
        return 1 if v else 0
    return v


def sync_paper_trades(db_path: Path = DB_PATH, json_path: Path = PAPER_JSON) -> int:
    """paper_trades.json'ı SQLite'a aynala. Döner: aynalanan kayıt sayısı."""
    if not json_path.exists():
        return 0
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0

    placeholders = ", ".join(":" + f for f in PAPER_FIELDS)
    columns      = ", ".join(PAPER_FIELDS)
    sql = f"INSERT OR REPLACE INTO paper_trades ({columns}) VALUES ({placeholders})"

    with get_db(db_path) as conn:
        # Tam tazeleme: mevcut satırları temizle, sonra tüm JSON'u yaz
        # (status değişenleri yakalamanın en güvenli yolu)
        conn.execute("DELETE FROM paper_trades")
        for raw in data:
            row = {f: _bool_to_int(raw.get(f)) for f in PAPER_FIELDS}
            conn.execute(sql, row)
    return len(data)


def sync_live_trades(db_path: Path = DB_PATH, json_path: Path = LIVE_JSON) -> int:
    """live_trades.json'ı SQLite'a aynala."""
    if not json_path.exists():
        return 0
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0

    placeholders = ", ".join(":" + f for f in LIVE_FIELDS)
    columns      = ", ".join(LIVE_FIELDS)
    sql = f"INSERT OR REPLACE INTO live_trades ({columns}) VALUES ({placeholders})"

    with get_db(db_path) as conn:
        conn.execute("DELETE FROM live_trades")
        for raw in data:
            row = {f: _bool_to_int(raw.get(f)) for f in LIVE_FIELDS}
            conn.execute(sql, row)
    return len(data)


# ── Birincil Yazım (SQLite-first, Faz 7) ────────────────────────────────────
def write_paper_trades_list(
    trades: list, db_path: Path = DB_PATH, clear_first: bool = True
) -> int:
    """Paper trade listesini tek transaction ile DB'ye yaz.

    SQLite-first akış: JSON dosyasına dokunmaz; çağıran önce bunu, sonra JSON
    dump'ı yapar. Crash olursa DB tutarlı kalır.
    clear_first=True (default): status değişenlerin eski kopyalarını temizler.
    """
    if not isinstance(trades, list):
        return 0
    placeholders = ", ".join(":" + f for f in PAPER_FIELDS)
    columns      = ", ".join(PAPER_FIELDS)
    sql = f"INSERT OR REPLACE INTO paper_trades ({columns}) VALUES ({placeholders})"
    init_db(db_path)
    with get_db(db_path) as conn:
        if clear_first:
            conn.execute("DELETE FROM paper_trades")
        for raw in trades:
            row = {f: _bool_to_int(raw.get(f)) for f in PAPER_FIELDS}
            conn.execute(sql, row)
    return len(trades)


def write_live_trades_list(
    trades: list, db_path: Path = DB_PATH, clear_first: bool = True
) -> int:
    """Live trade listesini tek transaction ile DB'ye yaz (SQLite-first)."""
    if not isinstance(trades, list):
        return 0
    placeholders = ", ".join(":" + f for f in LIVE_FIELDS)
    columns      = ", ".join(LIVE_FIELDS)
    sql = f"INSERT OR REPLACE INTO live_trades ({columns}) VALUES ({placeholders})"
    init_db(db_path)
    with get_db(db_path) as conn:
        if clear_first:
            conn.execute("DELETE FROM live_trades")
        for raw in trades:
            row = {f: _bool_to_int(raw.get(f)) for f in LIVE_FIELDS}
            conn.execute(sql, row)
    return len(trades)


def rebuild_json_from_db(
    db_path: Path = DB_PATH,
    paper_path: Path = PAPER_JSON,
    live_path:  Path = LIVE_JSON,
) -> dict:
    """DB bozulmadığı halde JSON kaybolduysa tersine dump (disaster recovery)."""
    result = {"paper": 0, "live": 0}
    try:
        with get_db(db_path, readonly=True) as conn:
            papers = [dict(r) for r in conn.execute("SELECT * FROM paper_trades")]
            lives  = [dict(r) for r in conn.execute("SELECT * FROM live_trades")]
        paper_path.write_text(json.dumps(papers, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        live_path.write_text(json.dumps(lives, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        result["paper"] = len(papers)
        result["live"]  = len(lives)
    except Exception:
        pass
    return result


def sync_all(db_path: Path = DB_PATH) -> dict:
    """Her iki JSON'ı da SQLite'a aynala. Sessiz başarısızlık (bot asla bozmasın)."""
    result = {"paper": 0, "live": 0, "errors": []}
    try:
        init_db(db_path)
    except Exception as e:
        result["errors"].append(f"init_db: {e}")
        return result
    # NOT: modül-düzeyi sabitleri çağrı anında resolve et (monkey-patch'e saygı)
    try:
        result["paper"] = sync_paper_trades(db_path, PAPER_JSON)
    except Exception as e:
        result["errors"].append(f"sync_paper: {e}")
    try:
        result["live"] = sync_live_trades(db_path, LIVE_JSON)
    except Exception as e:
        result["errors"].append(f"sync_live: {e}")
    return result


# ── Settlement-time Veri Kaydı (Faz 3+ için) ────────────────────────────────
def record_forecast_error(
    date: str,
    station: str,
    horizon_days: int | None,
    blend: float | None,
    top_pick: int | None,
    spread: float | None,
    uncertainty: str | None,
    actual_temp: float,
    trade_id: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    """Settlement sonrası model hatasını forecast_errors tablosuna yaz.

    Aynı (date, station, trade_id) tekrar yazılırsa duplicate olur — bu tabloda
    unique constraint yok çünkü tarihsel seriyi çoğaltarak öğrenme noktalarını
    değiştirmek istemiyoruz. Çağıran 'zaten kayıtlı mı' kontrolünden sorumlu.
    """
    if blend is None or actual_temp is None:
        return
    error_c     = round(blend - actual_temp, 3)
    abs_error_c = round(abs(error_c), 3)
    pick_err    = (top_pick - round(actual_temp)) if top_pick is not None else None

    month = int(date[5:7]) if date else None
    season = (
        "winter" if month in (12, 1, 2)
        else "spring" if month in (3, 4, 5)
        else "summer" if month in (6, 7, 8)
        else "autumn" if month in (9, 10, 11)
        else None
    )

    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forecast_errors
                (date, station, horizon_days, month, season,
                 blend, top_pick, spread, uncertainty,
                 actual_temp, error_c, abs_error_c, pick_error, trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date, station, horizon_days, month, season,
             blend, top_pick, spread, uncertainty,
             actual_temp, error_c, abs_error_c, pick_err, trade_id),
        )


# ── Model Forecast Recording (Faz 4) ────────────────────────────────────────
def record_model_forecast(
    station: str,
    date: str,
    model: str,
    max_temp: float,
    horizon_days: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    """Tek model tahminini model_forecasts'a ekle (UPSERT).

    Aynı (station, date, model) için birden fazla çağrı zararsız: sonuncu kazanır.
    """
    if max_temp is None:
        return
    try:
        with get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO model_forecasts
                   (station, date, model, horizon_days, max_temp)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(station, date, model) DO UPDATE SET
                     max_temp     = excluded.max_temp,
                     horizon_days = excluded.horizon_days,
                     recorded_at  = strftime('%s','now')""",
                (station, date, model, horizon_days, float(max_temp)),
            )
    except Exception:
        pass


def record_model_actuals(
    station: str,
    date: str,
    actual_temp: float,
    db_path: Path = DB_PATH,
) -> int:
    """O (station, date) için tüm modellerin actual_temp + abs_error alanını
    güncelle. Döner: güncellenen satır sayısı."""
    try:
        with get_db(db_path) as conn:
            cur = conn.execute(
                """UPDATE model_forecasts
                   SET actual_temp = ?,
                       abs_error   = ABS(max_temp - ?),
                       settled_at  = strftime('%s','now')
                   WHERE station = ? AND date = ? AND actual_temp IS NULL""",
                (float(actual_temp), float(actual_temp), station, date),
            )
            return cur.rowcount
    except Exception:
        return 0


def already_recorded_error(trade_id: str, db_path: Path = DB_PATH) -> bool:
    """Bu trade_id için forecast_errors'a zaten yazıldı mı?"""
    if not trade_id:
        return False
    with get_db(db_path, readonly=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM forecast_errors WHERE trade_id=? LIMIT 1",
            (trade_id,),
        ).fetchone()
        return row is not None


# ── Hızlı Sorgular (debugging / monitoring) ─────────────────────────────────
def summary_stats(db_path: Path = DB_PATH) -> dict:
    """Hızlı özet: kaç trade, kaç error kaydı, hangi istasyonlar."""
    with get_db(db_path, readonly=True) as conn:
        return {
            "paper_total":   conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0],
            "paper_open":    conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'").fetchone()[0],
            "paper_closed":  conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='closed'").fetchone()[0],
            "live_total":    conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0],
            "live_pending":  conn.execute("SELECT COUNT(*) FROM live_trades WHERE status='pending_fill'").fetchone()[0],
            "live_filled":   conn.execute("SELECT COUNT(*) FROM live_trades WHERE status='filled'").fetchone()[0],
            "live_settled":  conn.execute("SELECT COUNT(*) FROM live_trades WHERE status LIKE 'settled_%'").fetchone()[0],
            "errors_total":  conn.execute("SELECT COUNT(*) FROM forecast_errors").fetchone()[0],
            "bias_total":    conn.execute("SELECT COUNT(*) FROM bias_corrections").fetchone()[0],
            "weights_total": conn.execute("SELECT COUNT(*) FROM model_weights").fetchone()[0],
        }


# ── Settlement Audit (Faz 6b) ───────────────────────────────────────────────
def record_settlement_source(
    station: str,
    date: str,
    source: str,
    actual_temp: float | None,
    rounded_temp: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    """Tek kaynaktan gelen gözlemi settlement_audit'a yaz (UPSERT).

    Aynı (station, date, source) için yeniden çağrı zararsız: sonuncu kazanır.
    Sessiz başarısızlık — settle akışını asla bozmasın.
    """
    if actual_temp is None:
        return
    try:
        r = int(round(actual_temp)) if rounded_temp is None else int(rounded_temp)
        with get_db(db_path) as conn:
            conn.execute(
                """INSERT INTO settlement_audit
                   (station, date, source, actual_temp, rounded_temp)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(station, date, source) DO UPDATE SET
                     actual_temp  = excluded.actual_temp,
                     rounded_temp = excluded.rounded_temp,
                     recorded_at  = strftime('%s','now')""",
                (station, date, source, float(actual_temp), r),
            )
    except Exception:
        pass


def get_settlement_audit(
    days: int = 30,
    station: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Son `days` gün için settlement audit — kaynaklar arası karşılaştırma.

    Döner: [{station, date, sources: {source: actual_temp, ...},
             max_diff_c, max_diff_bucket}, ...]  (tarih desc)
    """
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")
    sql = """
        SELECT station, date, source, actual_temp, rounded_temp
        FROM settlement_audit
        WHERE date >= ?
    """
    params: list = [cutoff]
    if station:
        sql += " AND station = ?"
        params.append(station)
    sql += " ORDER BY date DESC, station, source"

    try:
        with get_db(db_path, readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []

    # Grupla: (station, date) -> {source: actual_temp, ...}
    groups: dict = {}
    bucket: dict = {}
    for r in rows:
        key = (r[0], r[1])
        groups.setdefault(key, {})[r[2]] = r[3]
        bucket.setdefault(key, {})[r[2]] = r[4]

    result: list = []
    for (st, dt), src_map in groups.items():
        temps = [v for v in src_map.values() if v is not None]
        bucks = [v for v in bucket[(st, dt)].values() if v is not None]
        max_diff_c       = round(max(temps) - min(temps), 2) if len(temps) >= 2 else 0.0
        max_diff_bucket  = int(max(bucks) - min(bucks)) if len(bucks) >= 2 else 0
        result.append({
            "station":         st,
            "date":            dt,
            "sources":         src_map,
            "rounded":         bucket[(st, dt)],
            "n_sources":       len(src_map),
            "max_diff_c":      max_diff_c,
            "max_diff_bucket": max_diff_bucket,
            "disagreement":    max_diff_bucket >= 1,  # bucket farkı = settlement risk
        })
    # Tarih desc sırasını koru
    result.sort(key=lambda r: (r["date"], r["station"]), reverse=True)
    return result


def settlement_disagreement_stats(
    days: int = 60, db_path: Path = DB_PATH
) -> dict:
    """İstasyon bazlı kaynak uyumsuzluk özet istatistiği."""
    audit = get_settlement_audit(days=days, db_path=db_path)
    by_station: dict = {}
    for a in audit:
        st = a["station"]
        s  = by_station.setdefault(st, {"n_days": 0, "n_disagreement": 0,
                                         "max_diff_c": 0.0, "sum_diff_c": 0.0})
        if a["n_sources"] < 2:
            continue
        s["n_days"] += 1
        s["sum_diff_c"] += a["max_diff_c"]
        if a["disagreement"]:
            s["n_disagreement"] += 1
        if a["max_diff_c"] > s["max_diff_c"]:
            s["max_diff_c"] = a["max_diff_c"]
    # oran + ortalama
    for st, s in by_station.items():
        n = s["n_days"] or 1
        s["disagreement_rate"] = round(s["n_disagreement"] / n, 3)
        s["mean_diff_c"]       = round(s["sum_diff_c"] / n, 3)
    return by_station


# ── Station Status (pause/resume) ───────────────────────────────────────────
def set_station_paused(
    station: str,
    paused: bool,
    reason: str | None = None,
    auto_resume_at: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    """İstasyonun pause durumunu DB'ye yazar (override). Faz 7+Faz 8.

    auto_resume_at (unix ts): verilirse, should_pause_station bu zaman
    geçtikten sonra otomatik unpause olarak davranır (circuit breaker için).
    """
    init_db(db_path)
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO station_status
                (station, paused, reason, auto_resume_at, updated_at)
            VALUES (?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(station) DO UPDATE SET
                paused         = excluded.paused,
                reason         = excluded.reason,
                auto_resume_at = excluded.auto_resume_at,
                updated_at     = excluded.updated_at
            """,
            (station.lower(), 1 if paused else 0, reason, auto_resume_at),
        )


def list_paused_stations(db_path: Path = DB_PATH) -> list:
    """Pause edilmiş istasyonları döner (dashboard/audit için)."""
    try:
        with get_db(db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT station, paused, reason, updated_at "
                "FROM station_status ORDER BY station"
            ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # CLI: python3 -m bot.db
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "init":
        init_db()
        print(f"✅ Schema oluşturuldu: {DB_PATH}")
    elif cmd == "sync":
        result = sync_all()
        print(f"📥 Paper: {result['paper']} | Live: {result['live']}")
        if result["errors"]:
            for e in result["errors"]:
                print(f"  ⚠️  {e}")
    elif cmd == "stats":
        init_db()
        result = sync_all()
        stats = summary_stats()
        for k, v in stats.items():
            print(f"  {k:16s}: {v}")
    else:
        print(f"Kullanım: python3 -m bot.db [init|sync|stats]")
