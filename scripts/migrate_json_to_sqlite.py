#!/usr/bin/env python3
"""
JSON → SQLite tek seferlik migration + sürekli doğrulama aracı.

Kullanım:
  python3 scripts/migrate_json_to_sqlite.py --dry-run
  python3 scripts/migrate_json_to_sqlite.py
  python3 scripts/migrate_json_to_sqlite.py --verify
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# bot package'ı import edebilmek için kök dizini path'e ekle
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from bot.db import (
    DB_PATH,
    LIVE_JSON,
    PAPER_JSON,
    init_db,
    summary_stats,
    sync_all,
    sync_live_trades,
    sync_paper_trades,
)

BACKUP_DIR = BASE_DIR / "backups"


def _backup_json_files() -> list[Path]:
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    created = []
    for src in [PAPER_JSON, LIVE_JSON]:
        if src.exists():
            dst = BACKUP_DIR / f"{src.stem}_premigration_{ts}.json"
            shutil.copy2(src, dst)
            created.append(dst)
            print(f"  [BACKUP] {src.name} → {dst.name} ({dst.stat().st_size:,} bytes)")
    return created


def _json_counts() -> dict:
    out = {"paper": 0, "live": 0}
    if PAPER_JSON.exists():
        try:
            out["paper"] = len(json.loads(PAPER_JSON.read_text(encoding="utf-8")))
        except Exception:
            pass
    if LIVE_JSON.exists():
        try:
            out["live"] = len(json.loads(LIVE_JSON.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def _pnl_sums() -> dict:
    """JSON dosyalarından net P&L topla (doğrulama için)."""
    out = {"paper_pnl": 0.0, "live_pnl": 0.0}
    try:
        if PAPER_JSON.exists():
            for t in json.loads(PAPER_JSON.read_text(encoding="utf-8")):
                if t.get("pnl") is not None:
                    out["paper_pnl"] += float(t["pnl"])
    except Exception:
        pass
    try:
        if LIVE_JSON.exists():
            for t in json.loads(LIVE_JSON.read_text(encoding="utf-8")):
                if t.get("pnl_usdc") is not None:
                    out["live_pnl"] += float(t["pnl_usdc"])
    except Exception:
        pass
    return out


def migrate(dry_run: bool):
    print("=" * 62)
    print(f"  JSON → SQLite Migration {'(DRY RUN)' if dry_run else '(GERÇEK)'}")
    print(f"  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 62)

    print(f"\n[1] JSON kaynak dosyaları:")
    counts = _json_counts()
    pnl    = _pnl_sums()
    print(f"    paper_trades.json: {counts['paper']:>5} kayıt | "
          f"net pnl=${pnl['paper_pnl']:+.2f}")
    print(f"    live_trades.json : {counts['live']:>5} kayıt | "
          f"net pnl=${pnl['live_pnl']:+.2f} USDC")

    if counts["paper"] == 0 and counts["live"] == 0:
        print("\n  ⚠️  JSON dosyaları boş veya okunamadı. Çıkılıyor.")
        sys.exit(1)

    if dry_run:
        # Geçici DB üzerinde test et
        tmp_db = BASE_DIR / "bot" / "trades.db.dryrun"
        if tmp_db.exists():
            tmp_db.unlink()
        print(f"\n[2] DRY RUN — geçici DB: {tmp_db}")
        init_db(tmp_db)
        p = sync_paper_trades(tmp_db, PAPER_JSON)
        l = sync_live_trades(tmp_db, LIVE_JSON)
        print(f"    ✓ {p} paper + {l} live yazıldı")
        stats = summary_stats(tmp_db)
        print(f"\n[3] Özet:")
        for k, v in stats.items():
            print(f"    {k:16s}: {v}")
        tmp_db.unlink()
        for suf in ("-shm", "-wal"):
            p2 = Path(str(tmp_db) + suf)
            if p2.exists():
                p2.unlink()
        print(f"\n  ✅ DRY RUN başarılı — gerçek migration için:")
        print(f"     python3 scripts/migrate_json_to_sqlite.py")
        return

    # Gerçek migration
    print(f"\n[2] Yedekleme:")
    backups = _backup_json_files()
    print(f"    ✓ {len(backups)} dosya yedeklendi → {BACKUP_DIR}")

    print(f"\n[3] SQLite DB: {DB_PATH}")
    # Önceki varsa yedekle
    if DB_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prev_bak = BACKUP_DIR / f"trades_pre_{ts}.db"
        shutil.copy2(DB_PATH, prev_bak)
        print(f"    Önceki DB yedeklendi: {prev_bak.name}")

    init_db(DB_PATH)
    result = sync_all(DB_PATH)
    print(f"    ✓ {result['paper']} paper + {result['live']} live senkronize")
    if result["errors"]:
        for e in result["errors"]:
            print(f"    ⚠️  {e}")

    stats = summary_stats(DB_PATH)
    print(f"\n[4] Doğrulama:")
    ok = True
    if stats["paper_total"] != counts["paper"]:
        print(f"    ❌ paper_trades sayı uyuşmuyor: "
              f"JSON={counts['paper']} DB={stats['paper_total']}")
        ok = False
    else:
        print(f"    ✅ paper_trades: {stats['paper_total']} ✓")
    if stats["live_total"] != counts["live"]:
        print(f"    ❌ live_trades sayı uyuşmuyor: "
              f"JSON={counts['live']} DB={stats['live_total']}")
        ok = False
    else:
        print(f"    ✅ live_trades:  {stats['live_total']} ✓")

    if ok:
        print(f"\n  ✅ Migration tamamlandı → {DB_PATH}")
    else:
        print(f"\n  ⚠️  Migration tamamlandı ama doğrulama hatalı.")
        sys.exit(1)


def verify():
    print("=" * 62)
    print("  Doğrulama Raporu")
    print("=" * 62)

    counts = _json_counts()
    pnl    = _pnl_sums()
    print(f"\nJSON dosyaları:")
    print(f"  paper: {counts['paper']:>5} kayıt | pnl=${pnl['paper_pnl']:+.2f}")
    print(f"  live : {counts['live']:>5} kayıt | pnl=${pnl['live_pnl']:+.2f} USDC")

    if not DB_PATH.exists():
        print(f"\n  ❌ {DB_PATH} bulunamadı. Önce migration çalıştır.")
        sys.exit(1)

    stats = summary_stats(DB_PATH)
    print(f"\nSQLite DB ({DB_PATH}):")
    for k, v in stats.items():
        print(f"  {k:16s}: {v}")

    from bot.db import get_db
    with get_db(DB_PATH, readonly=True) as conn:
        db_paper_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE pnl IS NOT NULL"
        ).fetchone()[0]
        db_live_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc), 0) FROM live_trades WHERE pnl_usdc IS NOT NULL"
        ).fetchone()[0]

    print(f"\nP&L doğrulama:")
    paper_match = abs(db_paper_pnl - pnl["paper_pnl"]) < 0.01
    live_match  = abs(db_live_pnl  - pnl["live_pnl"])  < 0.01
    print(f"  paper: JSON=${pnl['paper_pnl']:+.2f} DB=${db_paper_pnl:+.2f} "
          f"{'✅' if paper_match else '❌'}")
    print(f"  live : JSON=${pnl['live_pnl']:+.2f} USDC DB=${db_live_pnl:+.2f} "
          f"{'✅' if live_match else '❌'}")

    if stats["paper_total"] != counts["paper"] or stats["live_total"] != counts["live"]:
        print(f"\n  ❌ Kayıt sayısı uyuşmazlığı")
        sys.exit(1)
    if not (paper_match and live_match):
        print(f"\n  ❌ P&L uyuşmazlığı")
        sys.exit(1)
    print(f"\n  ✅ Tüm doğrulamalar geçti")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify",  action="store_true")
    args = ap.parse_args()

    if args.verify:
        verify()
    else:
        migrate(dry_run=args.dry_run)
