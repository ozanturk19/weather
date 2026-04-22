# JSON → SQLite Migration Rehberi

> **Hedef:** `paper_trades.json` (ve varsa `live_trades.json`) dosyalarını SQLite veritabanına taşı.
> Sıfır veri kaybı, atomik işlem, geri alma planı ile.

---

## İçindekiler

1. [Neden JSON'dan SQLite'a Geçmeli?](#1-neden-jsondan-sqlitea-geçmeli)
2. [Ön Hazırlık ve Yedekleme](#2-ön-hazırlık-ve-yedekleme)
3. [SQLite Şema Tasarımı](#3-sqlite-şema-tasarımı)
4. [Migration Script (Tam Kod)](#4-migration-script-tam-kod)
5. [scanner.py — SQLite Entegrasyonu](#5-scannerpy--sqlite-entegrasyonu)
6. [trader.py — SQLite Entegrasyonu](#6-traderpy--sqlite-entegrasyonu)
7. [main.py — Settlement Güncellemesi](#7-mainpy--settlement-güncellemesi)
8. [Doğrulama ve Test](#8-doğrulama-ve-test)
9. [Deployment Adımları](#9-deployment-adımları)
10. [Rollback Planı](#10-rollback-planı)

---

## 1. Neden JSON'dan SQLite'a Geçmeli?

### Mevcut JSON Sistemi Sorunları

```
paper_trades.json ve live_trades.json:
  ❌ Bot çöktüğünde yarım yazılmış JSON → bozuk dosya → tüm geçmiş kaybolur
  ❌ Eş zamanlı yazma: iki process aynı anda yazmak isterse race condition
  ❌ Sorgulama yok: "Paris'teki kayıplar nedir?" için tüm dosyayı okuyup filtrele
  ❌ Büyüme problemi: 10.000 trade sonrası JSON parse yavaşlar
  ❌ Atomic update yok: "yaz ve kapat" arasında power cut → corrupted

SQLite ile:
  ✅ WAL (Write-Ahead Logging): crash sonrası otomatik kurtarma
  ✅ ACID transactions: ya hepsi yazılır ya hiçbiri
  ✅ SQL sorguları: "Son 30 günde London win rate?" tek satır
  ✅ Concurrent reads: trader.py + scanner.py aynı anda okuyabilir
  ✅ Boyut bağımsız: 1M trade da aynı hızda
  ✅ Tek dosya: taşıma, yedekleme kolay
```

---

## 2. Ön Hazırlık ve Yedekleme

### Adım 1: Yedekle (çalıştırmadan önce)

```bash
# VPS'te çalıştır
cd /root/weather

# Mevcut JSON'ları yedekle
cp bot/paper_trades.json bot/paper_trades_backup_$(date +%Y%m%d_%H%M%S).json

# Eğer live_trades.json varsa
cp bot/live_trades.json  bot/live_trades_backup_$(date +%Y%m%d_%H%M%S).json

# Yedek aldığını doğrula
ls -la bot/*backup*
```

### Adım 2: Mevcut JSON yapısını incele

```bash
# JSON'ın gerçek yapısını gör
python3 -c "
import json
with open('bot/paper_trades.json') as f:
    data = json.load(f)
print('Type:', type(data))
if isinstance(data, list) and data:
    print('First record keys:', list(data[0].keys()))
    print('Total records:', len(data))
    print('Sample:', json.dumps(data[0], indent=2))
elif isinstance(data, dict):
    print('Top-level keys:', list(data.keys()))
"
```

### Adım 3: Bağımlılıkları kur

```bash
# SQLite Python'da built-in — ek kurulum gerekmez
# Ama migration için:
pip install python-dateutil  # tarih parse için (zaten yüklü olabilir)
```

---

## 3. SQLite Şema Tasarımı

```sql
-- db/schema.sql
-- Tüm tabloları ve indexleri tanımlar

PRAGMA journal_mode = WAL;      -- Crash recovery
PRAGMA foreign_keys = ON;       -- İlişki bütünlüğü
PRAGMA synchronous = NORMAL;    -- Hız/güvenlik dengesi (WAL ile güvenli)

-- ─────────────────────────────────────────────
-- TABLO 1: paper_trades
-- paper_trades.json'dan migrate edilir
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Market tanımı
    city            TEXT NOT NULL,
    station         TEXT NOT NULL,              -- 'eglc', 'lfpg' vb.
    date            TEXT NOT NULL,              -- 'YYYY-MM-DD'
    market_id       TEXT,                       -- Polymarket market ID
    question        TEXT,                       -- Tam soru metni

    -- Pozisyon bilgileri
    side            TEXT NOT NULL,              -- 'YES' | 'NO'
    bucket          TEXT NOT NULL,              -- '13-14' (°C aralığı)
    bucket_low      REAL,                       -- 13.0
    bucket_high     REAL,                       -- 14.0

    -- Fiyat ve büyüklük
    entry_price     REAL NOT NULL,              -- 0.12
    shares          REAL NOT NULL DEFAULT 10,
    cost_usd        REAL,                       -- entry_price × shares

    -- Model bilgileri (giriş anında)
    model_p50       REAL,                       -- Ensemble medyan tahmini
    model_p10       REAL,
    model_p90       REAL,
    ensemble_std    REAL,
    model_consensus REAL,                       -- mode_pct (0-1)
    signal_score    REAL,                       -- Sinyal kalitesi (0-100)
    horizon_days    INTEGER,                    -- 0, 1, 2

    -- Sonuç
    actual_temp     REAL,                       -- Gerçekleşen sıcaklık
    status          TEXT NOT NULL DEFAULT 'OPEN',
                                                -- 'OPEN' | 'WON' | 'LOST' | 'CANCELLED'
    pnl             REAL,                       -- Net kazanç/kayıp (USDC)
    exit_price      REAL,                       -- Settlement fiyatı (0 veya 1)

    -- Zaman damgaları
    opened_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    closed_at       INTEGER,
    settled_date    TEXT,                       -- 'YYYY-MM-DD HH:MM'

    -- Migration flag
    migrated_from_json INTEGER DEFAULT 0,
    json_original_id   TEXT                    -- JSON'daki orijinal ID (varsa)
);

-- ─────────────────────────────────────────────
-- TABLO 2: live_trades
-- Gerçek para işlemleri
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS live_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Market tanımı
    city            TEXT NOT NULL,
    station         TEXT NOT NULL,
    date            TEXT NOT NULL,
    market_id       TEXT,
    question        TEXT,
    token_id        TEXT,                       -- Polymarket CLOB token

    -- CLOB order bilgileri
    order_id        TEXT UNIQUE,                -- Polymarket order ID
    side            TEXT NOT NULL,
    bucket          TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    shares          REAL NOT NULL,
    cost_usd        REAL,

    -- Order durumu
    order_status    TEXT DEFAULT 'PENDING',     -- 'PENDING'|'OPEN'|'FILLED'|'CANCELLED'
    filled_price    REAL,
    filled_at       INTEGER,

    -- Model bilgileri
    model_p50       REAL,
    model_p90       REAL,
    signal_score    REAL,
    horizon_days    INTEGER,

    -- Sonuç
    actual_temp     REAL,
    status          TEXT DEFAULT 'OPEN',        -- 'OPEN'|'WON'|'LOST'|'CANCELLED'
    pnl             REAL,
    redeemed        INTEGER DEFAULT 0,          -- 1 = on-chain redemption yapıldı

    -- Zaman
    opened_at       INTEGER DEFAULT (strftime('%s','now')),
    closed_at       INTEGER,

    -- Migration
    migrated_from_json INTEGER DEFAULT 0
);

-- ─────────────────────────────────────────────
-- TABLO 3: forecast_errors
-- Model doğruluk takibi (Bölüm 6'daki analiz için)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS forecast_errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    station         TEXT NOT NULL,
    horizon_days    INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    season          TEXT NOT NULL,

    model_p50       REAL,
    model_p10       REAL,
    model_p90       REAL,
    ensemble_std    REAL,

    actual_temp     REAL NOT NULL,
    error_c         REAL NOT NULL,             -- predicted - actual
    abs_error_c     REAL NOT NULL,
    in_80pct_ci     INTEGER,                   -- 1=yes, 0=no

    created_at      INTEGER DEFAULT (strftime('%s','now'))
);

-- ─────────────────────────────────────────────
-- TABLO 4: model_weights
-- Dinamik model ağırlıkları tarihçesi
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_weights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station     TEXT NOT NULL,
    model       TEXT NOT NULL,              -- 'ecmwf', 'gfs', 'icon' vb.
    weight      REAL NOT NULL,
    rmse_30d    REAL,
    n_samples   INTEGER,
    recorded_at INTEGER DEFAULT (strftime('%s','now'))
);

-- ─────────────────────────────────────────────
-- TABLO 5: bias_corrections
-- Kalman filter bias geçmişi
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bias_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station     TEXT NOT NULL,
    date        TEXT NOT NULL,
    bias_est    REAL NOT NULL,              -- Kalman tahmin edilen bias (°C)
    uncertainty REAL NOT NULL,             -- P (variance)
    correction  REAL NOT NULL,             -- Uygulanacak düzeltme
    recorded_at INTEGER DEFAULT (strftime('%s','now'))
);

-- ─────────────────────────────────────────────
-- INDEX'LER — sorgu hızı için
-- ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_paper_city_date   ON paper_trades(city, date);
CREATE INDEX IF NOT EXISTS idx_paper_status      ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_station     ON paper_trades(station, status);
CREATE INDEX IF NOT EXISTS idx_paper_opened      ON paper_trades(opened_at);

CREATE INDEX IF NOT EXISTS idx_live_order_id     ON live_trades(order_id);
CREATE INDEX IF NOT EXISTS idx_live_city_date    ON live_trades(city, date);
CREATE INDEX IF NOT EXISTS idx_live_status       ON live_trades(status, order_status);

CREATE INDEX IF NOT EXISTS idx_errors_station    ON forecast_errors(station, date);
CREATE INDEX IF NOT EXISTS idx_errors_horizon    ON forecast_errors(station, horizon_days);
```

---

## 4. Migration Script (Tam Kod)

```python
#!/usr/bin/env python3
"""
migrate_json_to_sqlite.py
JSON paper_trades.json → SQLite migration

Kullanım:
  python3 migrate_json_to_sqlite.py --dry-run        # Önce bunu çalıştır
  python3 migrate_json_to_sqlite.py                  # Gerçek migration
  python3 migrate_json_to_sqlite.py --verify         # Migration sonrası doğrula
"""

import json
import sqlite3
import argparse
import sys
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────
# YOLLAR — VPS yapınıza göre düzenleyin
# ─────────────────────────────────────────────
BASE_DIR         = Path('/root/weather')
JSON_PAPER       = BASE_DIR / 'bot' / 'paper_trades.json'
JSON_LIVE        = BASE_DIR / 'bot' / 'live_trades.json'   # yoksa atla
SQLITE_DB        = BASE_DIR / 'bot' / 'trades.db'
SCHEMA_FILE      = BASE_DIR / 'db' / 'schema.sql'
BACKUP_DIR       = BASE_DIR / 'backups'


def load_json_safe(path: Path) -> list | dict | None:
    """JSON dosyasını güvenli yükle. Bozuksa None döndür."""
    if not path.exists():
        print(f"  [SKIP] {path} bulunamadı")
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"  [OK] {path} yüklendi — {type(data).__name__}, "
              f"{len(data) if isinstance(data, list) else 'dict'} kayıt")
        return data
    except json.JSONDecodeError as e:
        print(f"  [ERROR] {path} bozuk JSON: {e}")
        return None


def normalize_paper_trade(raw: dict, idx: int) -> dict | None:
    """
    JSON trade kaydını SQLite şemasına normalize et.
    Her projenin JSON yapısı farklı — burası projeye özel ayarlanmalı.
    """
    # Olası alan adı varyasyonları (JSON'unuza göre düzenleyin)
    def get(key, *aliases, default=None):
        for k in [key] + list(aliases):
            if k in raw:
                return raw[k]
        return default

    # Zorunlu alanlar
    city = get('city', 'location', 'station_city')
    if not city:
        print(f"  [SKIP] Kayıt #{idx}: 'city' alanı yok")
        return None

    # Tarih normalize et
    date_raw = get('date', 'forecast_date', 'market_date')
    try:
        if date_raw and 'T' in str(date_raw):
            date = datetime.fromisoformat(date_raw).strftime('%Y-%m-%d')
        elif date_raw:
            date = str(date_raw)[:10]
        else:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    except Exception:
        date = str(date_raw)[:10] if date_raw else None

    # opened_at (Unix timestamp'e çevir)
    opened_raw = get('opened_at', 'timestamp', 'created_at', 'entry_time')
    try:
        if isinstance(opened_raw, (int, float)):
            opened_at = int(opened_raw)
        elif isinstance(opened_raw, str):
            opened_at = int(datetime.fromisoformat(opened_raw.replace('Z', '+00:00')).timestamp())
        else:
            opened_at = int(datetime.now(timezone.utc).timestamp())
    except Exception:
        opened_at = int(datetime.now(timezone.utc).timestamp())

    # Status normalize et
    raw_status = str(get('status', 'outcome', default='OPEN')).upper()
    if raw_status in ('WIN', 'WON', 'YES_WIN'):
        status = 'WON'
    elif raw_status in ('LOSS', 'LOST', 'NO_WIN'):
        status = 'LOST'
    elif raw_status in ('CANCEL', 'CANCELLED', 'CANCELED'):
        status = 'CANCELLED'
    else:
        status = 'OPEN'

    # closed_at
    closed_raw = get('closed_at', 'exit_time', 'settled_at')
    try:
        if isinstance(closed_raw, (int, float)):
            closed_at = int(closed_raw)
        elif isinstance(closed_raw, str):
            closed_at = int(datetime.fromisoformat(closed_raw.replace('Z', '+00:00')).timestamp())
        else:
            closed_at = None
    except Exception:
        closed_at = None

    return {
        'city':             city,
        'station':          str(get('station', 'icao', 'station_code', default=city)).lower(),
        'date':             date,
        'market_id':        get('market_id', 'polymarket_id'),
        'question':         get('question', 'market_question'),
        'side':             str(get('side', 'direction', default='YES')).upper(),
        'bucket':           str(get('bucket', 'temperature_range', 'top_pick', default='')),
        'bucket_low':       get('bucket_low', 'temp_low'),
        'bucket_high':      get('bucket_high', 'temp_high'),
        'entry_price':      get('entry_price', 'price', 'cost'),
        'shares':           get('shares', 'size', 'quantity', default=10),
        'cost_usd':         get('cost_usd', 'total_cost'),
        'model_p50':        get('model_p50', 'blend_temp', 'predicted_temp', 'top_pick_temp'),
        'model_p10':        get('model_p10', 'p10'),
        'model_p90':        get('model_p90', 'p90'),
        'ensemble_std':     get('ensemble_std', 'std'),
        'model_consensus':  get('model_consensus', 'mode_pct', 'consensus'),
        'signal_score':     get('signal_score'),
        'horizon_days':     get('horizon_days', 'horizon', default=1),
        'actual_temp':      get('actual_temp', 'actual', 'result_temp'),
        'status':           status,
        'pnl':              get('pnl', 'profit', 'net_pnl'),
        'exit_price':       get('exit_price', 'settlement_price'),
        'opened_at':        opened_at,
        'closed_at':        closed_at,
        'settled_date':     get('settled_date', 'settled_at'),
        'migrated_from_json': 1,
        'json_original_id': str(get('id', default=idx)),
    }


def init_db(conn: sqlite3.Connection, schema_file: Path):
    """Schema SQL dosyasını çalıştır."""
    if schema_file.exists():
        sql = schema_file.read_text(encoding='utf-8')
        conn.executescript(sql)
    else:
        # Inline schema (schema.sql yoksa)
        conn.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                station TEXT NOT NULL,
                date TEXT NOT NULL,
                market_id TEXT,
                question TEXT,
                side TEXT NOT NULL DEFAULT 'YES',
                bucket TEXT NOT NULL DEFAULT '',
                bucket_low REAL, bucket_high REAL,
                entry_price REAL NOT NULL DEFAULT 0,
                shares REAL NOT NULL DEFAULT 10,
                cost_usd REAL,
                model_p50 REAL, model_p10 REAL, model_p90 REAL,
                ensemble_std REAL, model_consensus REAL,
                signal_score REAL, horizon_days INTEGER,
                actual_temp REAL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                pnl REAL, exit_price REAL,
                opened_at INTEGER DEFAULT (strftime('%s','now')),
                closed_at INTEGER, settled_date TEXT,
                migrated_from_json INTEGER DEFAULT 0,
                json_original_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_paper_city_date ON paper_trades(city, date);
            CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
        """)
    conn.commit()
    print("  [OK] Schema oluşturuldu")


def migrate(dry_run: bool = False):
    print("=" * 60)
    print(f"JSON → SQLite Migration {'(DRY RUN)' if dry_run else '(GERÇEK)'}")
    print("=" * 60)

    # ── 1. Yedek al ──
    if not dry_run:
        BACKUP_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        for src in [JSON_PAPER, JSON_LIVE]:
            if src.exists():
                dst = BACKUP_DIR / f"{src.stem}_backup_{ts}.json"
                shutil.copy2(src, dst)
                print(f"  [BACKUP] {src.name} → {dst}")

    # ── 2. JSON yükle ──
    print("\n[1] JSON dosyaları yükleniyor...")
    paper_data = load_json_safe(JSON_PAPER)
    live_data  = load_json_safe(JSON_LIVE)

    if paper_data is None and live_data is None:
        print("[ERROR] Hiçbir JSON dosyası yüklenemedi. Çıkılıyor.")
        sys.exit(1)

    # Liste mi dict mi? Normalize et
    if isinstance(paper_data, dict) and 'trades' in paper_data:
        paper_data = paper_data['trades']
    elif isinstance(paper_data, dict):
        paper_data = list(paper_data.values())
    paper_data = paper_data or []

    # ── 3. SQLite bağlantısı ──
    print(f"\n[2] SQLite {'(bellek — dry run)' if dry_run else SQLITE_DB}...")
    conn = sqlite3.connect(':memory:' if dry_run else str(SQLITE_DB))
    conn.row_factory = sqlite3.Row

    init_db(conn, SCHEMA_FILE)

    # ── 4. Migration ──
    print(f"\n[3] {len(paper_data)} paper trade migrate ediliyor...")

    INSERT_SQL = """
        INSERT INTO paper_trades (
            city, station, date, market_id, question,
            side, bucket, bucket_low, bucket_high,
            entry_price, shares, cost_usd,
            model_p50, model_p10, model_p90, ensemble_std,
            model_consensus, signal_score, horizon_days,
            actual_temp, status, pnl, exit_price,
            opened_at, closed_at, settled_date,
            migrated_from_json, json_original_id
        ) VALUES (
            :city, :station, :date, :market_id, :question,
            :side, :bucket, :bucket_low, :bucket_high,
            :entry_price, :shares, :cost_usd,
            :model_p50, :model_p10, :model_p90, :ensemble_std,
            :model_consensus, :signal_score, :horizon_days,
            :actual_temp, :status, :pnl, :exit_price,
            :opened_at, :closed_at, :settled_date,
            :migrated_from_json, :json_original_id
        )
    """

    success = 0
    skipped = 0
    errors  = 0

    try:
        with conn:  # transaction context — hata olursa rollback
            for i, raw in enumerate(paper_data):
                normalized = normalize_paper_trade(raw, i)
                if normalized is None:
                    skipped += 1
                    continue
                try:
                    conn.execute(INSERT_SQL, normalized)
                    success += 1
                except sqlite3.Error as e:
                    print(f"  [ERROR] Kayıt #{i}: {e} | data={normalized}")
                    errors += 1

    except sqlite3.Error as e:
        print(f"\n[FATAL] Transaction başarısız: {e}")
        conn.close()
        sys.exit(1)

    print(f"  ✅ Başarılı: {success}")
    print(f"  ⏭️  Atlanan:  {skipped}")
    print(f"  ❌ Hatalı:   {errors}")

    # ── 5. Özet ──
    print("\n[4] Doğrulama sorguları...")
    rows = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    print(f"  Toplam kayıt: {rows}")

    stats = conn.execute("""
        SELECT status, COUNT(*) as n, ROUND(SUM(pnl),2) as total_pnl
        FROM paper_trades GROUP BY status
    """).fetchall()
    for s in stats:
        print(f"  {s['status']:12s}: {s['n']:4d} kayıt | PnL: ${s['total_pnl'] or 0:.2f}")

    conn.close()

    if dry_run:
        print("\n[DRY RUN] Gerçek yazma yapılmadı. Hazırsa: python3 migrate_json_to_sqlite.py")
    else:
        print(f"\n✅ Migration tamamlandı → {SQLITE_DB}")


def verify():
    """Migration sonrası doğrulama."""
    print("=" * 60)
    print("Doğrulama Raporu")
    print("=" * 60)

    if not SQLITE_DB.exists():
        print(f"[ERROR] {SQLITE_DB} bulunamadı")
        sys.exit(1)

    conn = sqlite3.connect(str(SQLITE_DB))
    conn.row_factory = sqlite3.Row

    # JSON'daki toplam kayıt
    json_count = len(json.load(open(JSON_PAPER))) if JSON_PAPER.exists() else 0
    db_count   = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE migrated_from_json=1").fetchone()[0]

    print(f"JSON kayıt sayısı:    {json_count}")
    print(f"SQLite kayıt sayısı:  {db_count}")
    print(f"Eşleşiyor:            {'✅ EVET' if json_count == db_count else '❌ HAYIR'}")

    # Örnek kayıtlar
    samples = conn.execute("SELECT * FROM paper_trades LIMIT 3").fetchall()
    print("\nÖrnek kayıtlar:")
    for s in samples:
        print(f"  id={s['id']} city={s['city']} date={s['date']} "
              f"status={s['status']} pnl={s['pnl']}")

    conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--verify',  action='store_true')
    args = parser.parse_args()

    if args.verify:
        verify()
    else:
        migrate(dry_run=args.dry_run)
```

---

## 5. scanner.py — SQLite Entegrasyonu

JSON okuma/yazma kodunu SQLite ile değiştir:

```python
# bot/db.py — yeni yardımcı modül
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path('/root/weather/bot/trades.db')

@contextmanager
def get_db():
    """Thread-safe database bağlantısı."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────
# scanner.py'deki ESKİ JSON fonksiyonlarını REPLACE et
# ─────────────────────────────────────────────

# ESKI:
# def load_trades():
#     with open('paper_trades.json') as f:
#         return json.load(f)
#
# def save_trades(trades):
#     with open('paper_trades.json', 'w') as f:
#         json.dump(trades, f, indent=2)

# YENİ:
def open_trade(
    city: str,
    station: str,
    date: str,
    bucket: str,
    entry_price: float,
    shares: float,
    model_p50: float,
    model_p90: float,
    signal_score: float,
    horizon_days: int,
    market_id: str = None,
    side: str = 'YES',
) -> int:
    """Yeni paper trade aç. Döndürür: yeni kaydın ID'si."""
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO paper_trades
                (city, station, date, market_id, side, bucket,
                 entry_price, shares, cost_usd,
                 model_p50, model_p90, signal_score, horizon_days,
                 status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """, (
            city, station, date, market_id, side, bucket,
            entry_price, shares, entry_price * shares,
            model_p50, model_p90, signal_score, horizon_days,
        ))
        trade_id = cur.lastrowid
        print(f"[DB] Trade #{trade_id} açıldı: {city} {bucket} @ {entry_price}")
        return trade_id


def close_trade(trade_id: int, actual_temp: float, won: bool):
    """Trade'i kapat ve sonucu kaydet."""
    import time
    with get_db() as conn:
        # Giriş bilgilerini al
        trade = conn.execute(
            "SELECT * FROM paper_trades WHERE id=?", (trade_id,)
        ).fetchone()

        if not trade:
            print(f"[DB ERROR] Trade #{trade_id} bulunamadı")
            return

        exit_price = 1.0 if won else 0.0
        pnl = (exit_price - trade['entry_price']) * trade['shares']
        status = 'WON' if won else 'LOST'

        conn.execute("""
            UPDATE paper_trades SET
                actual_temp = ?,
                status      = ?,
                exit_price  = ?,
                pnl         = ?,
                closed_at   = ?
            WHERE id = ?
        """, (actual_temp, status, exit_price, pnl, int(time.time()), trade_id))

        print(f"[DB] Trade #{trade_id} kapatıldı: {status} | pnl=${pnl:.3f}")


def get_open_trades(city: str = None, date: str = None) -> list:
    """Açık trade'leri listele."""
    with get_db() as conn:
        if city and date:
            rows = conn.execute("""
                SELECT * FROM paper_trades
                WHERE status='OPEN' AND city=? AND date=?
            """, (city, date)).fetchall()
        elif city:
            rows = conn.execute("""
                SELECT * FROM paper_trades
                WHERE status='OPEN' AND city=?
            """, (city,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status='OPEN'"
            ).fetchall()
        return [dict(r) for r in rows]


def get_stats(days: int = 30) -> dict:
    """Son N günün istatistikleri."""
    import time
    since = int(time.time()) - days * 86400
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='WON'  THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl), 2) as net_pnl,
                ROUND(AVG(CASE WHEN status='WON' THEN 1.0 ELSE 0.0 END) * 100, 1) as win_rate
            FROM paper_trades
            WHERE opened_at >= ? AND status != 'OPEN'
        """, (since,)).fetchone()
        return dict(row)


def has_open_trade(city: str, date: str, bucket: str) -> bool:
    """Bu market + bucket için zaten açık pozisyon var mı?"""
    with get_db() as conn:
        row = conn.execute("""
            SELECT id FROM paper_trades
            WHERE city=? AND date=? AND bucket=? AND status='OPEN'
            LIMIT 1
        """, (city, date, bucket)).fetchone()
        return row is not None
```

---

## 6. trader.py — SQLite Entegrasyonu

```python
# bot/db.py'ye eklenecek live trade fonksiyonları

def open_live_trade(
    city: str,
    station: str,
    date: str,
    token_id: str,
    order_id: str,
    bucket: str,
    entry_price: float,
    shares: float,
    model_p50: float = None,
    model_p90: float = None,
    side: str = 'YES',
) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO live_trades
                (city, station, date, token_id, order_id,
                 side, bucket, entry_price, shares, cost_usd,
                 model_p50, model_p90, order_status, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'OPEN')
        """, (
            city, station, date, token_id, order_id,
            side, bucket, entry_price, shares, entry_price * shares,
            model_p50, model_p90,
        ))
        return cur.lastrowid


def update_order_filled(order_id: str, filled_price: float):
    """Order CLOB'da doldu."""
    import time
    with get_db() as conn:
        conn.execute("""
            UPDATE live_trades SET
                order_status = 'FILLED',
                filled_price = ?,
                filled_at    = ?
            WHERE order_id = ?
        """, (filled_price, int(time.time()), order_id))


def close_live_trade(order_id: str, actual_temp: float, won: bool, pnl: float):
    """Live trade settlement."""
    import time
    with get_db() as conn:
        conn.execute("""
            UPDATE live_trades SET
                actual_temp = ?,
                status      = ?,
                pnl         = ?,
                closed_at   = ?
            WHERE order_id = ?
        """, (actual_temp, 'WON' if won else 'LOST', pnl, int(time.time()), order_id))


def get_open_live_orders() -> list[dict]:
    """CLOB'da hâlâ açık olan emirler."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM live_trades
            WHERE status='OPEN' AND order_status IN ('PENDING','FILLED')
        """).fetchall()
        return [dict(r) for r in rows]


def get_daily_spend(date: str) -> float:
    """Bugün harcanan toplam USDC."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(cost_usd), 0) as spent
            FROM live_trades
            WHERE date=? AND status != 'CANCELLED'
        """, (date,)).fetchone()
        return float(row['spent'])


def count_open_live_trades() -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM live_trades WHERE status='OPEN'"
        ).fetchone()[0]
```

---

## 7. main.py — Settlement Güncellemesi

```python
# main.py'daki settle endpoint'ini güncelle

# ESKİ:
# trades = load_json('paper_trades.json')
# for trade in trades:
#     if trade['status'] == 'OPEN':
#         # ... settlement mantığı
# save_json(trades)

# YENİ:
from bot.db import get_open_trades, close_trade

@app.get("/settle/{city}/{date}")
async def settle_city_date(city: str, date: str):
    """Belirli şehir ve tarih için açık trade'leri settle et."""
    open_trades = get_open_trades(city=city, date=date)

    if not open_trades:
        return {"message": "Açık trade yok", "city": city, "date": date}

    # Gerçek sıcaklığı çek (mevcut fonksiyon)
    actual = await fetch_actual_temperature(city, date)
    if actual is None:
        return {"error": "Gerçek sıcaklık alınamadı"}

    settled = []
    for trade in open_trades:
        # Bucket kontrolü
        bucket_low  = trade.get('bucket_low')
        bucket_high = trade.get('bucket_high')

        if bucket_low is not None and bucket_high is not None:
            won = bucket_low <= actual < bucket_high
        else:
            # Bucket parse et: "13-14" → low=13, high=14
            parts = trade['bucket'].split('-')
            won = float(parts[0]) <= actual < float(parts[1])

        close_trade(trade['id'], actual, won)
        settled.append({
            'id':     trade['id'],
            'bucket': trade['bucket'],
            'actual': actual,
            'result': 'WON' if won else 'LOST',
        })

    return {
        "city":     city,
        "date":     date,
        "actual":   actual,
        "settled":  len(settled),
        "details":  settled,
    }
```

---

## 8. Doğrulama ve Test

```python
# test_migration.py

import sqlite3
import json
from pathlib import Path

def test_migration_integrity():
    """Migration sonrası veri bütünlüğü testi."""
    json_path = Path('/root/weather/bot/paper_trades.json')
    db_path   = Path('/root/weather/bot/trades.db')

    json_data = json.load(open(json_path))
    conn      = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Test 1: Kayıt sayısı eşleşiyor mu?
    db_count   = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE migrated_from_json=1").fetchone()[0]
    json_count = len(json_data)
    assert db_count == json_count, f"FAIL: JSON={json_count} DB={db_count}"
    print(f"✅ Kayıt sayısı eşleşiyor: {db_count}")

    # Test 2: NULL olmayan zorunlu alanlar
    nulls = conn.execute("""
        SELECT COUNT(*) FROM paper_trades
        WHERE city IS NULL OR entry_price IS NULL OR status IS NULL
    """).fetchone()[0]
    assert nulls == 0, f"FAIL: {nulls} kayıtta NULL zorunlu alan"
    print(f"✅ Zorunlu alanlar dolu")

    # Test 3: Status değerleri geçerli mi?
    invalid_status = conn.execute("""
        SELECT COUNT(*) FROM paper_trades
        WHERE status NOT IN ('OPEN','WON','LOST','CANCELLED')
    """).fetchone()[0]
    assert invalid_status == 0, f"FAIL: {invalid_status} geçersiz status"
    print(f"✅ Status değerleri geçerli")

    # Test 4: PnL tutarlı mı?
    won_negative = conn.execute("""
        SELECT COUNT(*) FROM paper_trades
        WHERE status='WON' AND pnl IS NOT NULL AND pnl < -0.01
    """).fetchone()[0]
    assert won_negative == 0, f"FAIL: {won_negative} WON trade negatif PnL"
    print(f"✅ PnL tutarlılığı")

    # Test 5: Toplam PnL JSON ile örtüşüyor mu?
    json_pnl = sum(t.get('pnl', 0) or 0 for t in json_data if t.get('status') in ('WON','LOST'))
    db_pnl   = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE status IN ('WON','LOST')"
    ).fetchone()[0]
    diff = abs(json_pnl - db_pnl)
    assert diff < 0.01, f"FAIL: PnL farkı ${diff:.3f} (JSON={json_pnl:.2f} DB={db_pnl:.2f})"
    print(f"✅ Toplam PnL eşleşiyor: ${db_pnl:.2f}")

    conn.close()
    print("\n✅ Tüm testler geçti — migration başarılı")


if __name__ == '__main__':
    test_migration_integrity()
```

---

## 9. Deployment Adımları

```bash
# 1. Botu durdur
sudo systemctl stop weather-bot    # veya: pm2 stop weather

# 2. Dry run
cd /root/weather
python3 migrate_json_to_sqlite.py --dry-run

# 3. Çıktıyı kontrol et — her şey OK görünüyorsa

# 4. Gerçek migration
python3 migrate_json_to_sqlite.py

# 5. Doğrulama
python3 migrate_json_to_sqlite.py --verify
python3 test_migration.py

# 6. scanner.py ve trader.py'i güncelle (JSON → SQLite fonksiyonları)

# 7. Kısa test çalışması
python3 bot/scanner.py scan --dry-run  # Hata yoksa

# 8. Botu yeniden başlat
sudo systemctl start weather-bot

# 9. Logları izle
sudo journalctl -u weather-bot -f
```

---

## 10. Rollback Planı

Bir şeyler ters giderse:

```bash
# Seçenek A: JSON'a geri dön
# scanner.py ve trader.py'deki SQLite kodunu kaldır, JSON versiyonuna dön
# JSON yedekleri BACKUP_DIR'da mevcut

# Seçenek B: SQLite'ı sıfırla, migration'ı tekrar çalıştır
rm /root/weather/bot/trades.db
python3 migrate_json_to_sqlite.py

# Seçenek C: Belirli bir yedek JSON'u geri yükle
ls /root/weather/backups/
cp /root/weather/backups/paper_trades_backup_20260422_143025.json \
   /root/weather/bot/paper_trades.json
```

**Kesinlikle yapılmaması gereken:**
```
❌ JSON yedeklerini silme (en az 30 gün sakla)
❌ trades.db'yi kopyalarken botu çalışır bırakma
❌ Migration sırasında başka bir process'in yazmasına izin verme
```

---

*Son güncelleme: 2026-04-22*

Sources:
- [SQLite Versioning and Migration Strategies](https://www.sqliteforum.com/p/sqlite-versioning-and-migration-strategies)
- [Data Migration Strategies for SQLite Databases](https://www.slingacademy.com/article/data-migration-strategies-for-sqlite-databases/)
- [Database Transactions and Error Handling](https://www.kevsrobots.com/learn/sqlite3/08_transations_and_error_handling.html)
