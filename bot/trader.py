#!/usr/bin/env python3
"""
Polymarket CLOB Live Order Engine
Limit order (maker = %0 fee) gönder, dolum izle, P&L hesapla.

Kullanım:
  python trader.py balance                   # USDC bakiyesi
  python trader.py status                    # Açık live pozisyonlar
  python trader.py check-fills               # Dolum kontrolü (cron)
  python trader.py cancel-stale              # Stale order iptali (cron)
  python trader.py settle                    # Dünkü pozisyonları kapat
  python trader.py setup-creds               # API credential türet ve .env'e yaz
  python trader.py approve-usdc              # USDC allowance ver (ilk kurulum)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

# ── Ortam ──────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent          # /root/weather
ENV_FILE    = BASE_DIR / ".env"
TRADES_FILE = BASE_DIR / "bot" / "live_trades.json"
PAPER_FILE  = BASE_DIR / "bot" / "paper_trades.json"

load_dotenv(ENV_FILE)

POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
CHAIN_ID        = int(os.getenv("CHAIN_ID", "137"))
PK              = os.getenv("PK")                  # cüzdan private key
WEATHER_API     = "http://localhost:8001"

# ── Risk Parametreleri ──────────────────────────────────────────────────────
LIVE_SHARES          = 5      # her trade'de alınan share adedi
MAX_OPEN_LIVE_TRADES  = 30     # aynı anda max açık pozisyon
MAX_DAILY_SPEND_USDC  = 60.0  # günlük max yeni emir tutarı (USDC) — 10 st × 2 gün × ~$1.3 ≈ $26/scan
MIN_USDC_RESERVE      = 5.0   # bu altına inerse yeni emir açılmaz
ORDER_EXPIRY_D1_HOURS = 5     # D+1: yarın settle → bugün dolması şart, agresif
ORDER_EXPIRY_D2_HOURS = 20    # D+2: 2 gün var, sonraki scan yeniden dener
MIN_PRICE            = 0.05   # çok ucuz → şüpheli
MAX_PRICE            = 0.40   # pahalı → edge yok

STATION_LABELS = {
    "eglc": "Londra   ", "lfpg": "Paris    ", "limc": "Milano   ",
    "lemd": "Madrid   ", "ltfm": "İstanbul ", "ltac": "Ankara   ",
    "eham": "Amsterdam", "eddm": "Münih    ", "epwa": "Varşova  ",
    "efhk": "Helsinki ", "omdb": "Dubai    ", "rjtt": "Tokyo    ",
}

# İstasyon koordinatları (Open-Meteo settlement için)
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

# ── Trade Depolama ──────────────────────────────────────────────────────────
def load_live_trades() -> list:
    if TRADES_FILE.exists():
        try:
            return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception) as e:
            print(f"  ⚠️  live_trades.json okunamadı: {e}")
            return []
    return []

def save_live_trades(trades: list):
    tmp = TRADES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(TRADES_FILE)

# ── CLOB Client Kurulumu ────────────────────────────────────────────────────
def setup_client():
    """Private key'den ClobClient oluştur, Level 2 auth yükle."""
    if not PK:
        print("❌ PK bulunamadı — .env dosyasına PK=0x... ekle")
        sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        print("❌ py-clob-client kurulu değil:")
        print("   pip3 install py-clob-client --break-system-packages")
        sys.exit(1)

    client = ClobClient(host=POLYMARKET_HOST, key=PK, chain_id=CHAIN_ID)

    # Kayıtlı API cred'leri varsa yükle
    api_key    = os.getenv("PM_API_KEY")
    api_secret = os.getenv("PM_API_SECRET")
    api_pass   = os.getenv("PM_API_PASSPHRASE")

    if api_key and api_secret and api_pass:
        client.set_api_creds(ApiCreds(
            api_key        = api_key,
            api_secret     = api_secret,
            api_passphrase = api_pass,
        ))
    else:
        print("⚠️  API cred bulunamadı — 'python trader.py setup-creds' çalıştır")

    return client

# ── Bekleyen TX Kontrolü (BTC botu çakışma koruması) ───────────────────────
def has_pending_tx(wallet_address: str) -> bool:
    """Cüzdanda bekleyen Polygon transaction var mı?"""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com", request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return False
        addr      = w3.to_checksum_address(wallet_address)
        confirmed = w3.eth.get_transaction_count(addr, "latest")
        pending   = w3.eth.get_transaction_count(addr, "pending")
        return pending > confirmed
    except Exception:
        return False  # hata durumunda engelleme

def wallet_address_from_pk(pk: str) -> str:
    """Private key'den cüzdan adresini türet."""
    from eth_account import Account
    return Account.from_key(pk).address

# ── USDC Bakiyesi ───────────────────────────────────────────────────────────
def get_balance() -> float:
    """Polymarket USDC bakiyesini döner.
    Önce CLOB API (birincil), sonra on-chain RPC (yedek) dener.
    """
    import urllib.request as _ur, json as _json
    # ── Birincil: CLOB API balance_allowance ──────────────────────────────
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        from py_clob_client.constants import L1
        client = setup_client()
        resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=L1)
        )
        bal = float(resp.get("balance", 0)) / 1_000_000
        if bal > 0:
            return bal
    except Exception as e_clob:
        print(f"  ⚠️  CLOB balance_allowance başarısız: {e_clob}")

    # ── Yedek: on-chain RPC ────────────────────────────────────────────────
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    RPCS = [
        "https://rpc-mainnet.matic.quiknode.pro",
        "https://polygon-bor-rpc.publicnode.com",
    ]
    try:
        wallet = wallet_address_from_pk(PK)
        padded = wallet[2:].lower().zfill(64)
        payload = _json.dumps({
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": USDC_E, "data": "0x70a08231" + padded}, "latest"],
            "id": 1
        }).encode()
        for rpc in RPCS:
            try:
                req = _ur.Request(rpc, data=payload,
                                  headers={"Content-Type": "application/json"}, method="POST")
                with _ur.urlopen(req, timeout=12) as r:
                    result = _json.loads(r.read()).get("result", "0x0")
                bal = int(result, 16) / 1_000_000
                if bal > 0:
                    return bal
            except Exception as rpc_err:
                print(f"  ⚠️  RPC {rpc[:40]} başarısız: {rpc_err}")
                continue
    except Exception as e:
        print(f"❌ Bakiye alınamadı (RPC): {e}")

    print("❌ Bakiye alınamadı — tüm kaynaklar başarısız")

    return 0.0

# ── Günlük Harcama Kontrolü ─────────────────────────────────────────────────
def today_spend() -> float:
    """Bugün açılan live trade'lerin toplam maliyeti."""
    today  = datetime.now().strftime("%Y-%m-%d")
    trades = load_live_trades()
    return sum(
        t.get("cost_usdc", 0)
        for t in trades
        if t.get("placed_at", "")[:10] == today
        and t["status"] not in ("cancelled", "expired")
    )

# ── Orderbook'tan En İyi Alış Fiyatı ───────────────────────────────────────
def get_best_bid(client, token_id: str) -> float | None:
    """Orderbook'tan en yüksek alış (bid) fiyatını döner."""
    try:
        ob = client.get_order_book(token_id)
        if ob and ob.bids:
            return float(ob.bids[0].price)
    except Exception:
        pass
    return None

# ── Limit Order Gönder ──────────────────────────────────────────────────────
def place_limit_order(
    condition_id: str,
    price:        float,
    station:      str,
    date:         str,
    top_pick:     int,
    bucket_title: str,
    paper_id:     str,
    shares:       int = LIVE_SHARES,
) -> dict | None:
    """
    GTC limit order (post_only=True → maker = %0 fee) gönder.

    Döner: {"order_id": ..., "limit_price": ..., "status": "pending_fill"}
    veya None (risk limit / hata durumunda).
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    label = STATION_LABELS.get(station, station.upper())

    # ── Risk kontrolleri ────────────────────────────────────────────────────
    live_trades = load_live_trades()
    open_live   = [t for t in live_trades if t["status"] == "pending_fill"]

    if len(open_live) >= MAX_OPEN_LIVE_TRADES:
        print(f"  ⛔ MAX_OPEN_LIVE_TRADES ({MAX_OPEN_LIVE_TRADES}) doldu — emir açılmadı")
        return None

    spent = today_spend()
    cost  = round(shares * price, 2)
    if spent + cost > MAX_DAILY_SPEND_USDC:
        print(f"  ⛔ Günlük limit ({MAX_DAILY_SPEND_USDC} USDC) aşılıyor — emir açılmadı")
        return None

    # Aynı station+date için fill'lenmiş pozisyon var mı? → blokla
    already_filled = any(
        t["station"] == station and t["date"] == date
        and t["status"] in ("filled", "settled_win", "settled_loss")
        for t in live_trades
    )
    if already_filled:
        print(f"  ⬜ {station.upper()} {label} {date} — zaten fill'lenmiş pozisyon var, atlanıyor")
        return None

    # Aynı station+date için pending emir var mı?
    # Farklı top_pick → model görüşünü güncelledi: eskiyi iptal et, yeniye gir
    stale_pending = [
        t for t in live_trades
        if t["station"] == station and t["date"] == date
        and t["status"] == "pending_fill"
    ]
    if stale_pending:
        if stale_pending[0]["top_pick"] == top_pick:
            print(f"  ⬜ {station.upper()} {label} {date} {top_pick}°C — aynı emir zaten açık")
            return None
        # Farklı top_pick: eski pending'i iptal et
        client_tmp = setup_client()
        for old in stale_pending:
            try:
                client_tmp.cancel(old["order_id"])
                old["status"] = "cancelled"
                old["notes"]  = f"Model güncellendi → {top_pick}°C"
                print(
                    f"  🔄 GÜNCELLEME  {station.upper()} {label}  "
                    f"{old['top_pick']}°C → {top_pick}°C  eski emir iptal edildi"
                )
            except Exception as e:
                print(f"  ⚠️  Eski emir iptal edilemedi: {e}")
        save_live_trades(live_trades)
        live_trades = load_live_trades()   # güncel listeyi yeniden yükle

    if price < MIN_PRICE or price > MAX_PRICE:
        print(f"  ⬜ {station.upper()} {label} — fiyat aralık dışı ({price:.2f})")
        return None

    # ── Bakiye kontrolü ─────────────────────────────────────────────────────
    balance = get_balance()
    if balance < MIN_USDC_RESERVE + cost:
        print(f"  ⛔ Yetersiz bakiye: ${balance:.2f} USDC (min rezerv + maliyet = ${MIN_USDC_RESERVE + cost:.2f})")
        return None

    # ── BTC botu nonce çakışma koruması ─────────────────────────────────────
    wallet = wallet_address_from_pk(PK)
    for attempt in range(3):
        if not has_pending_tx(wallet):
            break
        print(f"  ⏳ Bekleyen TX var (BTC botu?) — 10sn bekleniyor... ({attempt+1}/3)")
        time.sleep(10)
    else:
        print("  ⚠️  Pending TX hâlâ var — devam ediyoruz (CLOB order on-chain değil)")

    # ── Order gönder ─────────────────────────────────────────────────────────
    client = setup_client()

    # Orderbook'tan best_bid al, daha iyi fiyata girmeye çalış
    best_bid    = get_best_bid(client, condition_id)
    limit_price = price
    if best_bid is not None and best_bid > price:
        limit_price = best_bid   # piyasa bize daha iyi fiyat sunuyor
        print(f"  📈 Best bid {best_bid:.3f} > scanner fiyatı {price:.3f} — daha iyisine girildi")

    # Fiyatı 2 ondalığa yuvarla (Polymarket tick = 0.01)
    limit_price = round(limit_price, 2)

    try:
        order_args   = OrderArgs(
            token_id = condition_id,
            price    = limit_price,
            size     = float(shares),
            side     = BUY,
        )
        signed_order = client.create_order(order_args)
        resp         = client.post_order(signed_order, OrderType.GTC, post_only=True)

        # resp dict veya exception
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        if not order_id:
            print(f"  ❌ Order ID alınamadı: {resp}")
            return None

    except Exception as e:
        print(f"  ❌ Order gönderilemedi: {e}")
        return None

    # ── live_trades.json'a kaydet ────────────────────────────────────────────
    now = datetime.now()

    # D+1 (yarın settle) → 5h, D+2 → 20h (sonraki scan yeniden dener)
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    expiry_h = ORDER_EXPIRY_D1_HOURS if date == tomorrow else ORDER_EXPIRY_D2_HOURS

    trade_rec  = {
        "id":           f"{station}_{date}_{now.strftime('%H%M%S')}_live",
        "paper_id":     paper_id,
        "station":      station,
        "date":         date,
        "top_pick":     top_pick,
        "bucket_title": bucket_title,
        "condition_id": condition_id,
        "order_id":     order_id,
        "limit_price":  limit_price,
        "shares":       shares,
        "cost_usdc":    cost,
        "fill_price":   None,
        "fill_time":    None,
        "placed_at":    now.isoformat(),
        "expires_at":   (now + timedelta(hours=expiry_h)).isoformat(),
        "horizon":      "D+1" if date == tomorrow else "D+2",
        "status":       "pending_fill",
        "result":       None,
        "pnl_usdc":     None,
        "settled_at":   None,
        "notes":        "",
    }

    live_trades.append(trade_rec)
    save_live_trades(live_trades)

    pct = round(limit_price * 100)
    print(
        f"  🔴 LIVE ORDER  {station.upper()} {label}  "
        f"🎯{top_pick}°C [{bucket_title}]  "
        f"@ {pct}¢  {shares} share · ${cost:.2f} USDC  "
        f"→ {order_id[:16]}..."
    )
    return {"order_id": order_id, "limit_price": limit_price, "status": "pending_fill"}

# ── Dolum Kontrolü ──────────────────────────────────────────────────────────
def check_fills() -> int:
    """
    Tüm pending_fill order'larını CLOB API'ye sor.
    Dolan → status='filled'. Süresi dolan → cancel + log.
    Döner: güncellenen trade sayısı.
    """
    trades  = load_live_trades()
    pending = [t for t in trades if t["status"] == "pending_fill"]

    if not pending:
        print("  ℹ️  Bekleyen live order yok.")
        return 0

    client  = setup_client()
    updated = 0

    # get_order(single_id) 401 döndürebiliyor — get_orders() listesini kullan
    try:
        open_orders = client.get_orders()
        open_map = {o.get("id") or o.get("order_id"): o for o in (open_orders or [])}
    except Exception as e:
        print(f"  ⚠️  CLOB order listesi alınamadı: {e}")
        open_map = {}

    for t in pending:
        try:
            resp = open_map.get(t["order_id"])
            if resp is None:
                # CLOB'da yoksa → fill veya cancel olmuş, individual sorgu dene
                try:
                    resp = client.get_order(t["order_id"])
                except Exception:
                    resp = {}
            status = (resp.get("status") or "").upper()
            matched = float(resp.get("size_matched") or 0)
            size    = float(resp.get("original_size") or t["shares"])

            if status == "MATCHED" or matched >= size:
                # Tamamen doldu
                fill_price = float(resp.get("price") or t["limit_price"])
                t["status"]     = "filled"
                t["fill_price"] = fill_price
                t["fill_time"]  = datetime.now().isoformat()
                label = STATION_LABELS.get(t["station"], t["station"].upper())
                print(
                    f"  ✅ FILL  {t['station'].upper()} {label}  "
                    f"🎯{t['top_pick']}°C  @ {round(fill_price*100)}¢  "
                    f"{t['shares']} share"
                )
                updated += 1

            elif status in ("CANCELLED", "CANCELED"):
                t["status"] = "cancelled"
                t["notes"]  = "CLOB tarafından iptal edildi"
                updated += 1

            elif matched > 0:
                # Kısmi dolum — bilgi ver ama bekle
                pct = round(matched / size * 100)
                print(f"  🔄 PARTIAL  {t['station'].upper()}  %{pct} doldu ({matched:.1f}/{size:.1f} share)")

        except Exception as e:
            print(f"  ⚠️  {t['station'].upper()} {t['date']} fill kontrolü başarısız: {e}")

    save_live_trades(trades)
    print(f"\n  📋 {updated} trade güncellendi ({len(pending)} kontrol edildi)")
    return updated

# ── Stale Order İptali ──────────────────────────────────────────────────────
def cancel_stale_orders() -> int:
    """
    expires_at geçmiş order'ları iptal et.
    D+1 stale → aynı fiyata yeniden gir (agresif fill için)
    D+2 stale → sadece iptal et, sonraki scan yeniden açar
    """
    trades  = load_live_trades()
    pending = [t for t in trades if t["status"] == "pending_fill"]
    client  = setup_client()
    now     = datetime.now()
    tomorrow  = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    cancelled = 0
    requeued  = 0

    for t in pending:
        try:
            expires = datetime.fromisoformat(t["expires_at"])
        except Exception:
            continue

        if now < expires:
            continue   # henüz süresi dolmadı

        label   = STATION_LABELS.get(t["station"], t["station"].upper())
        horizon = t.get("horizon", "D+2")

        try:
            client.cancel(t["order_id"])
            t["status"] = "cancelled"
            t["notes"]  = f"Stale ({horizon})"
            cancelled  += 1
            print(f"  🗑️  CANCEL [{horizon}]  {t['station'].upper()} {label}  🎯{t['top_pick']}°C")
        except Exception as e:
            print(f"  ⚠️  İptal başarısız {t['order_id'][:16]}: {e}")
            continue

        # D+1 stale: aynı fiyatla yeniden gir (market hâlâ açık, fill şansı var)
        if horizon == "D+1" and t["date"] == tomorrow:
            # Disk'e yazıp sonra yeniden gir — place_limit_order disk'ten okur,
            # henüz kaydedilmediyse eski pending_fill görür ve duplicate engeller.
            save_live_trades(trades)
            try:
                new_result = place_limit_order(
                    condition_id = t["condition_id"],
                    price        = t["limit_price"],   # aynı fiyat
                    station      = t["station"],
                    date         = t["date"],
                    top_pick     = t["top_pick"],
                    bucket_title = t["bucket_title"],
                    paper_id     = t["paper_id"],
                )
                if new_result:
                    requeued += 1
                    print(f"  🔄 YENİDEN GİRİLDİ  {t['station'].upper()} {label}")
                    # place_limit_order kendi save_live_trades'ini yaptı; reload
                    trades = load_live_trades()
            except Exception as e:
                print(f"  ⚠️  Yeniden giriş başarısız: {e}")

    save_live_trades(trades)

    summary = f"{cancelled} stale order iptal edildi"
    if requeued:
        summary += f", {requeued} D+1 yeniden girildi"
    if cancelled:
        print(f"\n  🗑️  {summary}")
    else:
        print("  ✅ Stale order yok")
    return cancelled

# ── Bucket Eşleştirme (scanner.py'den kopyalandı) ──────────────────────────
def bucket_won(title: str, actual: float) -> bool | None:
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

# ── Open-Meteo Gerçek Sıcaklık (Settlement İçin Birincil Kaynak) ───────────
def get_actual_temp_open_meteo(station: str, date: str):
    """Open-Meteo arşivinden günlük maks. sıcaklık çek (°C).

    Polymarket, WU (Weather Underground) verisiyle settle eder.
    Open-Meteo temperature_2m_max → WU daily max ile uyumlu.
    METAR saatlik ölçümlerinden çok daha doğru settlement tahmini verir.
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

# ── Live Settlement ─────────────────────────────────────────────────────────
def settle_live():
    """
    'filled' order'ların gerçek sonucunu METAR/WU'dan çek.
    Bugünden önceki tüm tarihleri settle eder (cron atlama durumu için).
    """
    trades    = load_live_trades()
    today     = datetime.now().strftime("%Y-%m-%d")
    to_settle = [
        t for t in trades
        if t["date"] <= today and t["status"] == "filled"
    ]

    dates_str = ", ".join(sorted({t["date"] for t in to_settle})) if to_settle else yesterday
    print(f"\n{'='*62}")
    print(f"  🏁 LIVE SETTLEMENT — {dates_str}")
    print(f"{'='*62}")

    if not to_settle:
        print("  Settle edilecek live pozisyon yok.\n")
        return

    settled = 0
    for trade in to_settle:
        station = trade["station"]
        label   = STATION_LABELS.get(station, station.upper())

        try:
            actual = None

            # Birincil: Open-Meteo — Polymarket'ın WU kaynağıyla uyumlu
            actual_om = get_actual_temp_open_meteo(station, trade["date"])
            if actual_om is not None:
                actual = round(actual_om)
                print(f"  🌡️  {station.upper()} Open-Meteo: {actual_om:.1f}°C → {actual}°C")

            # Yedek: METAR API
            if actual is None:
                try:
                    r = httpx.get(
                        f"{WEATHER_API}/api/metar-history?station={station}", timeout=30
                    )
                    r.raise_for_status()
                    daily      = r.json().get("daily_maxes", [])
                    day_record = next(
                        (d for d in daily if d["date"] == trade["date"]), None
                    )
                    if day_record:
                        actual = round(day_record["max_temp"])
                        print(f"  🌡️  {station.upper()} METAR (yedek): {actual}°C")
                except Exception as metar_e:
                    print(f"  ⚠️  METAR yedek başarısız ({station}): {metar_e}")

            if actual is None:
                print(f"  ⏳ {station.upper()} {label} — gerçek veri henüz yok")
                continue

            won = bucket_won(trade["bucket_title"], actual)

            if won is None:
                print(f"  ❓ {station.upper()} {label} — bucket sonuç belirlenemedi")
                continue

            fill_p = trade.get("fill_price") or trade["limit_price"]
            if won:
                pnl = round(trade["shares"] * (1.0 - fill_p), 2)
            else:
                pnl = round(-trade["cost_usdc"], 2)

            trade.update({
                "status":      "settled_win" if won else "settled_loss",
                "result":      "WIN" if won else "LOSS",
                "pnl_usdc":   pnl,
                "settled_at": datetime.now().isoformat(),
                "notes":      f"actual={actual}°C",
            })
            settled += 1

            emoji = "🟢" if won else "🔴"
            print(
                f"  {emoji} {station.upper()} {label}  "
                f"tahmin={trade['top_pick']}°C  gerçek={actual}°C  "
                f"[{trade['bucket_title']}]  "
                f"→ {'KAZANDI' if won else 'KAYBETTİ'}  "
                f"${'+' if pnl >= 0 else ''}{pnl:.2f} USDC"
            )

        except Exception as e:
            print(f"  ❌ {station.upper()} {label} — settle hatası: {e}")

    save_live_trades(trades)

    # Özet
    closed  = [t for t in trades if t["status"] in ("settled_win", "settled_loss")]
    wins    = [t for t in closed if t["result"] == "WIN"]
    tot_pnl = sum(t["pnl_usdc"] for t in closed if t["pnl_usdc"] is not None)
    wr      = len(wins) / len(closed) * 100 if closed else 0

    print(f"\n  Bugün settle: {settled} | "
          f"Toplam: {len(wins)}/{len(closed)} kazanıldı ({wr:.0f}%) | "
          f"Net P&L: ${'+'if tot_pnl>=0 else ''}{tot_pnl:.2f} USDC")
    print()

# ── Status ──────────────────────────────────────────────────────────────────
def cmd_status():
    trades  = load_live_trades()
    pending = [t for t in trades if t["status"] == "pending_fill"]
    filled  = [t for t in trades if t["status"] == "filled"]
    settled = [t for t in trades if t["status"] in ("settled_win", "settled_loss")]
    wins    = [t for t in settled if t["result"] == "WIN"]
    tot_pnl = sum(t["pnl_usdc"] for t in settled if t["pnl_usdc"] is not None)

    print(f"\n{'='*62}")
    print(f"  🔴 LIVE TRADING DURUMU")
    print(f"{'='*62}")
    print(f"  Bekleyen emir  : {len(pending)}")
    print(f"  Dolmuş (settle bekliyor): {len(filled)}")
    print(f"  Kapanmış : {len(settled)} ({len(wins)} kazanç)")
    print(f"  Net P&L  : ${'+'if tot_pnl>=0 else ''}{tot_pnl:.2f} USDC\n")

    if pending:
        print(f"  BEKLEYEN EMİRLER ({len(pending)}):")
        now = datetime.now()
        for t in sorted(pending, key=lambda x: x["date"]):
            label   = STATION_LABELS.get(t["station"], t["station"].upper())
            exp     = datetime.fromisoformat(t["expires_at"])
            kalan   = max(0, int((exp - now).total_seconds() / 3600))
            pct     = round(t["limit_price"] * 100)
            print(
                f"  📋 {t['station'].upper()} {label}  {t['date']}  "
                f"🎯{t['top_pick']}°C @ {pct}¢  "
                f"{t['shares']} share · ${t['cost_usdc']:.2f}  "
                f"⏱ {kalan}h kaldı  [{t['order_id'][:12]}...]"
            )

    if filled:
        print(f"\n  DOLMUŞ — SETTLEMENT BEKLIYOR ({len(filled)}):")
        for t in sorted(filled, key=lambda x: x["date"]):
            label = STATION_LABELS.get(t["station"], t["station"].upper())
            fp    = round((t.get("fill_price") or t["limit_price"]) * 100)
            print(
                f"  ✅ {t['station'].upper()} {label}  {t['date']}  "
                f"🎯{t['top_pick']}°C @ {fp}¢ dolu  "
                f"{t['shares']} share · ${t['cost_usdc']:.2f}"
            )
    print()

# ── Balance ──────────────────────────────────────────────────────────────────
def cmd_balance():
    bal   = get_balance()
    spent = today_spend()
    remaining_budget = max(0.0, MAX_DAILY_SPEND_USDC - spent)
    trades = load_live_trades()
    pending_cost = sum(
        t["cost_usdc"] for t in trades if t["status"] == "pending_fill"
    )
    print(f"\n  💰 USDC Bakiyesi   : ${bal:.4f}")
    print(f"  📊 Bugün harcanan  : ${spent:.2f} / ${MAX_DAILY_SPEND_USDC:.2f} limit")
    print(f"  📈 Kalan bütçe     : ${remaining_budget:.2f}")
    print(f"  🔒 Emir'deki USDC  : ${pending_cost:.2f}\n")

# ── Credential Türetme ──────────────────────────────────────────────────────
def cmd_setup_creds():
    """API cred'lerini private key'den türet ve .env'e yaz."""
    from py_clob_client.client import ClobClient

    print("\n  🔑 API credential türetiliyor...")
    client = ClobClient(host=POLYMARKET_HOST, key=PK, chain_id=CHAIN_ID)
    try:
        creds = client.create_or_derive_api_creds()
        # ApiCreds object — dict veya attribute erişimi
        api_key  = creds.api_key        if hasattr(creds, "api_key")        else creds["api_key"]
        api_sec  = creds.api_secret     if hasattr(creds, "api_secret")     else creds["api_secret"]
        api_pass = creds.api_passphrase if hasattr(creds, "api_passphrase") else creds["api_passphrase"]

        set_key(str(ENV_FILE), "PM_API_KEY",        api_key)
        set_key(str(ENV_FILE), "PM_API_SECRET",     api_sec)
        set_key(str(ENV_FILE), "PM_API_PASSPHRASE", api_pass)

        wallet = wallet_address_from_pk(PK)
        print(f"  ✅ Cüzdan : {wallet}")
        print(f"  ✅ API Key: {api_key[:20]}...")
        print(f"  ✅ .env dosyasına yazıldı\n")
    except Exception as e:
        print(f"  ❌ Hata: {e}\n")
        sys.exit(1)

# ── USDC Approve ─────────────────────────────────────────────────────────────
def cmd_approve_usdc():
    """CTF Exchange'e USDC harcama izni ver (ilk kurulum)."""
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    from py_clob_client.constants import L1
    client = setup_client()
    print("\n  🔐 USDC allowance veriliyor...")
    try:
        # Mevcut bakiye & allowance
        resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=L1)
        )
        bal = float(resp.get("balance", 0)) / 1_000_000
        alw = float(resp.get("allowance", 0)) / 1_000_000
        print(f"  Mevcut bakiye   : ${bal:.4f} USDC")
        print(f"  Mevcut allowance: ${alw:.4f} USDC")

        if alw >= 100:
            print("  ✅ Yeterli allowance zaten var, işlem gerekmez\n")
            return

        # Allowance ver
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=L1)
        )
        print("  ✅ 1000 USDC allowance verildi\n")
    except Exception as e:
        print(f"  ❌ Hata: {e}\n")
        sys.exit(1)

# ── Auto Redeem Kazanan Token'lar ───────────────────────────────────────────

POLYGON_RPCS = [
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
]
CTF_CONTRACT  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]


def _get_w3():
    """Polygon RPC bağlantısı kur."""
    from web3 import Web3
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
        except Exception:
            pass
    raise RuntimeError("Polygon RPC bağlantısı kurulamadı")


def _redeem_ctf(w3, wallet: str, pk: str, cond_id_hex: str) -> str:
    """
    CTF kontratını kullanarak pozisyonu on-chain redeem et.
    Başarılı olursa TX hash döner, hata durumunda exception fırlatır.
    """
    cond_bytes = bytes.fromhex(cond_id_hex[2:] if cond_id_hex.startswith("0x") else cond_id_hex)
    ctf = w3.eth.contract(address=CTF_CONTRACT, abi=CTF_ABI)

    # Önce on-chain raporlama yapılmış mı kontrol et
    denom = ctf.functions.payoutDenominator(cond_bytes).call()
    if denom == 0:
        raise ValueError("condition on-chain raporlanmamış (payout denom=0)")

    gas_price = int(w3.eth.gas_price * 1.4)  # %40 üstü
    nonce = w3.eth.get_transaction_count(wallet, "latest")

    tx = ctf.functions.redeemPositions(
        USDC_CONTRACT,
        b"\x00" * 32,   # parentCollectionId = bytes32(0)
        cond_bytes,
        [1]             # YES token indexSet
    ).build_transaction({
        "from": wallet,
        "nonce": nonce,
        "gas": 150000,
        "gasPrice": gas_price,
        "chainId": 137,
    })

    from eth_account import Account
    signed = Account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def cmd_redeem():
    """
    settled_win durumundaki trade'lerin Polymarket token'larını on-chain redeem et.

    Düzeltilen mantık:
    - condition_id (asset/token_id decimal) → positions API'deki 'asset' alanıyla eşleştir
    - 'redeemable: true' olan pozisyonlar için CTF kontratı üzerinden web3 redemption yap
    - 'payoutDenominator > 0' kontrol ederek zamanından önce deneme yapma
    - Başarılı redemption sonrası redeemed=True ve redeemed_at=now() yaz
    """
    trades    = load_live_trades()
    to_redeem = [t for t in trades if t.get("status") == "settled_win"
                 and not t.get("redeemed")]

    if not to_redeem:
        print("  ✅ Redeem edilecek kazanç yok.\n")
        return

    print(f"\n  💰 {len(to_redeem)} kazanan trade redeem edilecek...")

    # Pozisyonları çek: asset (decimal token_id) → position_data
    wallet = wallet_address_from_pk(PK)
    try:
        r = httpx.get(
            f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.001",
            timeout=12
        )
        positions_by_asset = {str(p["asset"]): p for p in r.json()} if r.is_success else {}
    except Exception as e:
        print(f"  ⚠️  Positions API hatası: {e}")
        positions_by_asset = {}

    # web3 bağlantısı
    try:
        w3 = _get_w3()
    except Exception as e:
        print(f"  ❌ Web3 bağlantı hatası: {e}")
        return

    updated = 0

    for t in to_redeem:
        token_id = str(t.get("condition_id", ""))
        label    = f"{t.get('station','?').upper()} {t.get('date','?')}"
        pos      = positions_by_asset.get(token_id)

        if pos is None:
            # Positions API'de yok → token transfer edilmiş veya çok küçük
            print(f"  ℹ️  {label} — positions'ta bulunamadı, muhtemelen zaten ödendi")
            t["redeemed"] = True
            t["redeemed_at"] = datetime.utcnow().isoformat()
            updated += 1
            continue

        current_val = float(pos.get("currentValue", 0))
        redeemable  = pos.get("redeemable", False)
        cond_id_hex = pos.get("conditionId", "")

        if current_val < 0.01:
            print(f"  ✅ {label} — value={current_val:.3f} ≈ 0, zaten ödendi")
            t["redeemed"] = True
            t["redeemed_at"] = datetime.utcnow().isoformat()
            updated += 1
            continue

        if not redeemable:
            print(f"  ⏳ {label} — redeemable=False, piyasa henüz çözümlenmedi (${current_val:.2f})")
            continue

        # On-chain redemption
        try:
            tx_hash = _redeem_ctf(w3, wallet, PK, cond_id_hex)
            print(f"  🔗 {label} — TX gönderildi: {tx_hash[:20]}... ${current_val:.2f}")

            # 20 sn bekle, onay kontrol et
            time.sleep(20)
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                if receipt and receipt.status == 1:
                    print(f"  ✅ {label} — REDEEM ONAYLANDI ${current_val:.2f}")
                    t["redeemed"] = True
                    t["redeemed_at"] = datetime.utcnow().isoformat()
                    t["redeem_tx"] = tx_hash
                    updated += 1
                elif receipt and receipt.status == 0:
                    print(f"  ❌ {label} — TX revert oldu! hash={tx_hash[:20]}")
                else:
                    # Hala pending → flag set et, sonraki çalışmada kontrol edilmez
                    # ama en azından TX gönderdik
                    print(f"  ⏳ {label} — TX pending: {tx_hash[:20]}... sonraki çalışmada tekrar dene")
            except Exception:
                print(f"  ⏳ {label} — receipt alınamadı, TX muhtemelen pending")

        except ValueError as e:
            if "on-chain raporlanmamış" in str(e):
                print(f"  ⏳ {label} — oracle henüz on-chain raporlamamış (${current_val:.2f}), tekrar denenecek")
            else:
                print(f"  ❌ {label} — redeem hatası: {e}")
        except Exception as e:
            print(f"  ❌ {label} — redeem hatası: {e}")

    save_live_trades(trades)
    print(f"\n  {updated} trade işlendi.\n")


# ── Pozisyon Satışı (Exit / Take-Profit) ────────────────────────────────────
def cmd_sell(threshold: float = 0.70):
    """
    Polymarket positions API'den yüksek değerli pozisyonları tespit et,
    CLOB'da SELL limit order ver.

    Kullanım:
      python trader.py sell          # varsayılan: ≥70¢ pozisyonlar
      python trader.py sell 0.85     # yalnızca ≥85¢ olanlar

    Mantık:
      - CLOB üzerinden fill'lenen pozisyonlar Exchange contract'ta tutuluyor.
      - WU settlement'ı beklemek yerine CLOB'da satmak daha güvenli:
        anında USDC kredisi, WU ≠ METAR riskinden bağımsız.
      - Satış emri: post_only=False (taker ok — hızlı fill istiyoruz)
        limit price = best_bid - 1 tick (0.01)
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL as SELL_SIDE

    trades = load_live_trades()
    wallet = wallet_address_from_pk(PK)

    # Positions API'den mevcut değerleri çek
    try:
        r = httpx.get(
            f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.001",
            timeout=15
        )
        positions = {str(p["asset"]): p for p in r.json()} if r.is_success else {}
    except Exception as e:
        print(f"  ❌ Positions API hatası: {e}")
        return

    client     = setup_client()
    now        = datetime.now()
    sold_count = 0

    print(f"\n{'='*62}")
    print(f"  📤 SELL TARAMA — eşik ≥{round(threshold*100)}¢/share")
    print(f"{'='*62}")

    for t in trades:
        # filled      : normal açık pozisyon
        # settled_win : bot METAR'a göre win dedi, Polymarket henüz resolve etmemiş
        # settled_loss: bot METAR'a göre loss dedi AMA WU farklı göstermiş olabilir
        #               → positions API'de val>0 ise piyasa hâlâ YES fiyatlıyor, sat
        if t["status"] not in ("filled", "settled_win", "settled_loss"):
            continue

        token_id = str(t.get("condition_id", ""))
        pos      = positions.get(token_id)
        if pos is None:
            continue

        size        = float(pos.get("size", 0))
        current_val = float(pos.get("currentValue", 0))
        redeemable  = pos.get("redeemable", False)
        price_now   = current_val / size if size > 0 else 0
        label       = STATION_LABELS.get(t["station"], t["station"].upper())

        # Zaten resolve olmuş ve val=$0 → kayıp, sat diyemeyiz
        if redeemable and current_val < 0.01:
            print(f"  ❌ {t['station'].upper()} {label} {t['date']} — resolve oldu, val=$0 (kayıp)")
            continue

        if price_now < threshold:
            print(f"  ⬜ {t['station'].upper()} {label} {t['date']} {t['top_pick']}°C"
                  f" — şu an {round(price_now*100)}¢ (eşik {round(threshold*100)}¢ altında)")
            continue

        entry_price = t.get("fill_price") or t["limit_price"]
        profit_if_sold = round((price_now - entry_price) * size, 2)

        # Orderbook'tan best_bid al — bir tick altına sat (maker olmak için)
        try:
            ob        = client.get_order_book(token_id)
            best_bid  = float(ob.bids[0].price) if ob and ob.bids else price_now
        except Exception:
            best_bid  = price_now

        sell_price = round(max(best_bid - 0.01, 0.01), 2)

        # Güvenlik: satış fiyatı giriş fiyatının %50'sinden düşük olamaz
        # Orderbook bid'i çökmüşse (market resolve aşaması) satma, bekle
        min_acceptable = round(entry_price * 0.50, 2)
        if sell_price < min_acceptable:
            print(f"  ⚠️  {t['station'].upper()} {label} {t['date']} {t['top_pick']}°C"
                  f" — orderbook bid çökmüş ({round(sell_price*100)}¢ < min {round(min_acceptable*100)}¢)"
                  f" → market resolve aşamasında, Polymarket otomatik ödeyecek")
            continue

        print(f"  📤 SELL  {t['station'].upper()} {label}  {t['date']}  "
              f"🎯{t['top_pick']}°C  {size:.0f} share  "
              f"@ {round(sell_price*100)}¢  "
              f"(giriş {round(entry_price*100)}¢, kâr ≈${profit_if_sold:+.2f})")

        try:
            order_args   = OrderArgs(
                token_id = token_id,
                price    = sell_price,
                size     = size,
                side     = SELL_SIDE,
            )
            signed_order = client.create_order(order_args)
            # Satış emri: post_only=False (taker fee var ama hızlı fill şart)
            resp         = client.post_order(signed_order, OrderType.GTC)
            order_id     = resp.get("orderID") or resp.get("order_id") or resp.get("id")

            if order_id:
                t["status"]   = "sell_pending"
                t["sell_price"] = sell_price
                t["sell_order_id"] = order_id
                t["sell_placed_at"] = now.isoformat()
                t["notes"]    = (t.get("notes", "") + f" | SELL@{sell_price}").strip(" | ")
                sold_count   += 1
                print(f"       ✅ Satış emri → {order_id[:20]}...")
            else:
                print(f"       ❌ Order ID alınamadı: {resp}")

        except Exception as e:
            print(f"       ❌ Satış emri gönderilemedi: {e}")

    save_live_trades(trades)
    print(f"\n  {sold_count} pozisyon için satış emri verildi.\n")


# ── Pozisyon Özeti (Positions API) ──────────────────────────────────────────
def cmd_positions():
    """Polymarket positions API'den mevcut portföy değerini göster."""
    wallet = wallet_address_from_pk(PK)
    try:
        r = httpx.get(
            f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0",
            timeout=15
        )
        positions = r.json() if r.is_success else []
    except Exception as e:
        print(f"  ❌ Positions API: {e}")
        return

    trades       = load_live_trades()
    token_to_trade = {str(t["condition_id"]): t for t in trades}

    total_val    = 0
    total_cost   = 0
    claimable    = 0

    print(f"\n{'='*62}")
    print(f"  💼 PORTFÖY  (wallet: {wallet[:10]}...)")
    print(f"{'='*62}")

    for p in sorted(positions, key=lambda x: -float(x.get("currentValue", 0))):
        val        = float(p.get("currentValue", 0))
        size       = float(p.get("size", 0))
        redeemable = p.get("redeemable", False)
        title      = (p.get("title") or "")[:52]
        price_now  = val / size if size > 0 else 0
        t          = token_to_trade.get(str(p.get("asset", "")))
        entry      = (t.get("fill_price") or t.get("limit_price", 0)) if t else 0
        cost       = t.get("cost_usdc", 0) if t else 0
        pnl        = val - cost if cost else 0

        if redeemable and val < 0.01:
            status_icon = "❌"
        elif redeemable and val > 0:
            status_icon = "💰"
            claimable  += val
        elif price_now >= 0.80:
            status_icon = "🔥"
        elif price_now >= 0.50:
            status_icon = "📈"
        else:
            status_icon = "📋"

        total_val  += val
        total_cost += cost
        arrow = "↑" if pnl >= 0 else "↓"
        print(f"  {status_icon}  ${val:5.2f}  {round(price_now*100):3}¢  {arrow}${abs(pnl):.2f}  {title}")

    print(f"\n  Toplam değer  : ${total_val:.2f}")
    print(f"  Toplam maliyet: ${total_cost:.2f}")
    print(f"  Unrealized P&L: ${total_val - total_cost:+.2f}")
    if claimable > 0:
        print(f"  Claim edilebilir: ${claimable:.2f}")
    print()


# ── Kapanmış Kayıp Pozisyonları Temizle ─────────────────────────────────────
def cmd_cleanup():
    """
    Polymarket'ta resolve olmuş, value=$0 olan kayıp pozisyonları
    live_trades.json'dan temizle (redeemed=True işaretle).

    Hangi pozisyonlar temizlenir:
      - settled_loss: zaten kayıp işaretli, sadece redeemed flag eksik
      - settled_win ama positions API'de value≈$0 (WU/METAR fark → aslında kayıp)
      - pending_fill / filled ama positions API'de redeemable=True AND value=0
        (market dolduktan sonra yanlış yönde kapandı)

    On-chain TX gerekmez — $0 değerli YES token'ların redemption'ı $0 döner.
    Sadece lokal kayıt temizlenir.
    """
    trades = load_live_trades()
    wallet = wallet_address_from_pk(PK)

    # Positions API'den resolve olmuş pozisyonları çek
    try:
        r = httpx.get(
            f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0",
            timeout=15
        )
        positions_by_asset = {str(p["asset"]): p for p in r.json()} if r.is_success else {}
    except Exception as e:
        print(f"  ❌ Positions API hatası: {e}")
        positions_by_asset = {}

    now      = datetime.utcnow().isoformat()
    cleaned  = 0
    skipped  = 0

    print(f"\n{'='*62}")
    print(f"  🧹 PORTFÖY TEMİZLEME")
    print(f"{'='*62}")

    for t in trades:
        if t.get("redeemed"):
            continue   # zaten temizlenmiş

        label    = f"{t.get('station','?').upper()} {t.get('date','?')} {t.get('top_pick','?')}°C"
        token_id = str(t.get("condition_id", ""))
        pos      = positions_by_asset.get(token_id)

        # ── Durum 1: settled_loss — lokal olarak kayıp işaretli ────────────
        if t["status"] == "settled_loss":
            t["redeemed"]    = True
            t["redeemed_at"] = now
            t["notes"]       = (t.get("notes", "") + " | cleanup:settled_loss").strip(" | ")
            cleaned += 1
            print(f"  🗑️  {label}  settled_loss → temizlendi")
            continue

        # ── Durum 2: settled_win ama positions API'de value≈$0 ─────────────
        if t["status"] == "settled_win" and pos is not None:
            val = float(pos.get("currentValue", 0))
            if pos.get("redeemable") and val < 0.01:
                t["redeemed"]    = True
                t["redeemed_at"] = now
                t["notes"]       = (t.get("notes", "") + " | cleanup:win_val0").strip(" | ")
                cleaned += 1
                print(f"  🗑️  {label}  settled_win ama val=$0 (WU kayıp) → temizlendi")
                continue

        # ── Durum 3: filled/pending ama market resolve olmuş, value=$0 ─────
        if t["status"] in ("filled", "pending_fill") and pos is not None:
            val = float(pos.get("currentValue", 0))
            if pos.get("redeemable") and val < 0.01:
                t["status"]      = "settled_loss"
                t["result"]      = "LOSS"
                t["pnl_usdc"]    = round(-t.get("cost_usdc", 0), 2)
                t["settled_at"]  = now
                t["redeemed"]    = True
                t["redeemed_at"] = now
                t["notes"]       = (t.get("notes", "") + " | cleanup:resolved_loss").strip(" | ")
                cleaned += 1
                print(f"  🗑️  {label}  {t['status']} → resolve edilmiş kayıp, temizlendi")
                continue

        skipped += 1

    save_live_trades(trades)

    remaining = sum(
        1 for t in trades
        if not t.get("redeemed") and t["status"] not in ("cancelled", "expired", "superseded")
    )
    print(f"\n  🗑️  {cleaned} pozisyon temizlendi  |  {remaining} aktif pozisyon kaldı\n")

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    # sell komutu opsiyonel threshold argümanı alır
    if cmd == "sell":
        threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.70
        cmd_sell(threshold)
        sys.exit(0)

    commands = {
        "status":        cmd_status,
        "balance":       cmd_balance,
        "positions":     cmd_positions,
        "check-fills":   check_fills,
        "cancel-stale":  cancel_stale_orders,
        "settle":        settle_live,
        "redeem":        cmd_redeem,
        "cleanup":       cmd_cleanup,
        "sell":          lambda: cmd_sell(0.70),
        "setup-creds":   cmd_setup_creds,
        "approve-usdc":  cmd_approve_usdc,
    }

    if cmd not in commands:
        print(f"\nKullanım: python trader.py [{' | '.join(commands.keys())}]\n")
        sys.exit(1)

    commands[cmd]()
