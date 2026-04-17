# Weather Bot — Canlı Trading Geçiş Planı

> **Bu doküman:** Mevcut paper trading botunu Polymarket CLOB üzerinde gerçek limit order gönderecek şekilde geliştirmek için hazırlanmış tam teknik spesifikasyondur.  
> **Uygulayacak ajan:** Bu dosyayı okuyan ajan, aşağıdaki planı eksiksiz uygulayacaktır.  
> **Sahip:** Mevcut BTC botu ile aynı cüzdan kullanılacaktır.

---

## 1. Proje Özeti

Avrupa havalimanlarındaki maksimum sıcaklık tahminlerini kullanarak Polymarket'te hava durumu marketlerine **otomatik limit order** (maker = sıfır fee) gönderen bir trading botu.

### Temel Mantık
```
Hava Modeli Tahmini  →  En Olası Sıcaklık (top_pick)  →  İlgili PM Bucket  →  Limit Order
```

### Neden Kârlı Olabilir?
- 1 share = $1 payout (eğer bucket doğruysa)
- Ortalama giriş fiyatı: **25¢** → potansiyel kazanç **+$0.75/share**
- Düşük fiyatlı bucketlara girince kayıp küçük (+$0.25), kazanç büyük (+$0.75)
- Hava modelleri bazı şehirlerde piyasadan daha doğru → **edge var**

---

## 2. Mevcut Altyapı

### Sunucu
- **VPS:** Hetzner, `root@135.181.206.109`
- **Port 8001:** FastAPI backend (`main.py`)
- **Servis:** `systemctl status weather`
- **Git repo:** `https://github.com/ozanturk19/weather.git` (branch: `main`)
- **Proje dizini:** `/root/weather`

### Proje Yapısı
```
/root/weather/
├── main.py                  # FastAPI backend (port 8001)
├── static/
│   └── index.html           # Dashboard frontend
├── bot/
│   ├── scanner.py           # Ana bot — sinyal + paper trading
│   ├── paper_trades.json    # Paper trade geçmişi (gitignored)
│   └── scanner.log          # Cron log
├── predictions.json         # Bias verisi (gitignored)
├── .env                     # Gizli anahtarlar (gitignored)
└── LIVE_TRADING_PLAN.md     # Bu dosya
```

### FastAPI Endpoint'leri (localhost:8001)
```
GET /api/weather?station=epwa
    → {"days": {"2026-04-18": {"blend": {"max_temp": 14.2, "bias_corrected_blend": 15.1, 
                                          "bias_active": true, "spread": 0.63, 
                                          "uncertainty": "Düşük"}, ...}}}

GET /api/ensemble?station=epwa
    → {"days": {"2026-04-18": {"member_maxes": [13.1, 14.2, 14.8, 15.0, ...]}}}
    # 39 ECMWF ensemble üyesi

GET /api/polymarket?station=epwa&date=2026-04-18
    → {"buckets": [{"title": "15°C", "yes_price": 0.24, "condition_id": "0x...", 
                    "threshold": 15, "is_above": false, "is_below": false}],
       "liquidity": 1250}

GET /api/metar-history?station=epwa
    → {"daily_maxes": [{"date": "2026-04-17", "max_temp": 15.0}]}
```

### Hava İstasyonları
| Kod | Şehir | Settlement Kaynağı |
|-----|-------|-------------------|
| EGLC | Londra City | METAR |
| LTAC | Ankara Esenboğa | METAR |
| LIMC | Milano Malpensa | METAR |
| LTFM | İstanbul | NOAA |
| LEMD | Madrid | Weather Underground |
| LFPG | Paris CDG | Weather Underground |
| EHAM | Amsterdam Schiphol | Weather Underground |
| EDDM | Münih | Weather Underground |
| EPWA | Varşova Chopin | Weather Underground |
| EFHK | Helsinki Vantaa | Weather Underground |

---

## 3. Scanner (Mevcut Bot) — Detaylı Açıklama

**Dosya:** `/root/weather/bot/scanner.py`

### Çalışma Akışı
```
1. Hava tahmini çek (blend veya bias_corrected_blend)
2. Ensemble verisi çek (39 ECMWF üyesi)
3. top_pick = round(member_max)'ların modu (en sık görülen derece)
4. Adaptif bias düzeltmesi uygula (geçmiş hatalardan öğrenilmiş)
5. Uncertainty filtresi → "Yüksek" ise pas geç
6. Polymarket bucket'ı eşleştir
7. Fiyat filtresi → 0.05 < price < 0.50 ise sinyal
8. Paper trade kaydet → paper_trades.json
```

### Önemli Sabitler
```python
SHARES       = 10      # paper trading'de 10 share/trade
MIN_PRICE    = 0.05    # çok ucuz → şüpheli
MAX_PRICE    = 0.50    # pahalı → edge yok
MIN_BIAS_TRADES = 4    # bias hesabı için minimum kapalı trade
SKIP_UNCERTAINTY = {"yüksek", "high", "very high"}
```

### Cron Schedule
```bash
# Hava modelleri 00z/06z/12z/18z UTC güncellenir
# Türkiye saati: 03:00/09:00/15:00/21:00
# Tarama: güncellemeden 1 saat sonra
0 4,10,16,22 * * *  cd /root/weather && python3 bot/scanner.py scan >> bot/scanner.log 2>&1
0 11          * * *  cd /root/weather && python3 bot/scanner.py settle >> bot/scanner.log 2>&1
```

### Scanner Komutları
```bash
python3 bot/scanner.py scan      # D+1 ve D+2 için tara
python3 bot/scanner.py settle    # Dünkü pozisyonları kapat
python3 bot/scanner.py report    # Tam geçmiş raporu
python3 bot/scanner.py status    # Açık pozisyon özeti
```

### Adaptif Bias Sistemi
Scanner, her taramada `compute_station_biases()` ile geçmiş kapanan trade'lerden istasyon bazlı sistematik hata öğrenir:

| İstasyon | Geçmiş Hata (n=trade) | Uygulanan Bias |
|----------|----------------------|----------------|
| EPWA Varşova | +1.38°C (n=8) | +1°C |
| EFHK Helsinki | +0.60°C (n=5) | +1°C |
| LTAC Ankara | +0.75°C (n=8) | +1°C |
| LFPG Paris | +2.67°C (n=6) | +3°C |
| Diğerleri | ~0°C | 0 |

**Dikkat:** Bias değerleri daha fazla trade kapandıkça otomatik güncellenir.

### Duplicate Kontrol Mantığı
- Aynı `station + date + top_pick` kombinasyonu için bir pozisyon açık → **pas**
- Aynı `station + date` ama farklı `top_pick` → yeni pozisyon açılabilir (forecast değişti)
- D+1 VE D+2 her taramada taranır

### paper_trades.json Yapısı
```json
{
  "id": "epwa_2026-04-18_100523",
  "station": "epwa",
  "date": "2026-04-18",
  "blend": 14.1,
  "spread": 0.63,
  "uncertainty": "Düşük",
  "top_pick": 15,
  "raw_top_pick": 14,
  "bias_applied": 1,
  "ens_mode_pct": 46,
  "ens_2nd_pick": 14,
  "ens_2nd_pct": 33,
  "bucket_title": "15°C",
  "condition_id": "0x43881041f15912ad438bfe7121504e4cfe0325d76767c38bfd48c8992c51d19e",
  "entry_price": 0.24,
  "shares": 10,
  "cost_usd": 2.40,
  "potential_win": 7.60,
  "liquidity": 1450,
  "status": "open",
  "entered_at": "2026-04-17T04:00:12",
  "actual_temp": null,
  "result": null,
  "pnl": null,
  "settled_at": null
}
```

---

## 4. Paper Trading Sonuçları (Validasyon)

> Gerçek paraya geçmeden önce 2+ hafta paper trading yapıldı.

### Genel Performans
| Metrik | Değer |
|--------|-------|
| Toplam kapalı trade | 74 |
| Kazanç | 26 (%35.1) |
| Kayıp | 48 (%64.9) |
| Net P&L | **+$77.40** |
| Ortalama giriş fiyatı | 0.250 (25¢) |
| Ortalama kazanç/trade | +$27.47 |
| Ortalama kayıp/trade | -$13.27 |

### Neden Düşük Win Rate'e Rağmen Kârlı?
```
Asimetrik payout yapısı:
  Kazanınca: ~75¢ × share sayısı (çünkü 25¢'ye aldık)
  Kaybedince: -25¢ × share sayısı

Beklenen değer: (0.35 × 0.75) - (0.65 × 0.25) = +0.10/share → pozitif EV
```

### İstasyon Performansı
| İstasyon | W/L | Win Rate | Net P&L |
|----------|-----|----------|---------|
| EGLC Londra | 2/9 | 22% | **+$132** |
| EFHK Helsinki | 3/5 | 60% | **+$103** |
| LEMD Madrid | 3/5 | 60% | **+$102** |
| LTFM İstanbul | 3/8 | 38% | +$48 |
| LIMC Milano | 3/8 | 38% | +$12 |
| LFPG Paris | 1/6 | 17% | $0 |
| EDDM Münih | 2/8 | 25% | -$38 |
| LTAC Ankara | 1/8 | 13% | -$49 |
| EHAM Amsterdam | 2/9 | 22% | -$92 |
| EPWA Varşova | 1/8 | 13% | -$150 |

**Not:** Varşova ve Amsterdam bias düzeltmesi eklendikten sonra henüz kapatılmış trade yok — bu istasyonların performansı yakında düzelecek.

---

## 5. Polymarket CLOB — Teknik Detaylar

### CLOB (Central Limit Order Book)
- **Host:** `https://clob.polymarket.com`
- **Ağ:** Polygon (chain_id: 137)
- **Para:** USDC on Polygon
- **Maker fee:** %0 (limit order = maker = **hedefimiz**)
- **Taker fee:** %0.1 (market order = kaçınılacak)
- **Settlement:** Otomatik, Polymarket tarafından

### Python SDK
```bash
pip install py-clob-client eth-account python-dotenv
```

**Resmi repo:** https://github.com/Polymarket/py-clob-client

### Authentication
Polymarket, private key'den API credential türetir — ayrıca kayıt gerekmez:

```python
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,          # 0x... formatında wallet private key
    chain_id=137,             # Polygon mainnet
)

# API key türet (ilk çalıştırmada oluşturur, sonrakinde aynısını döner)
api_creds = client.create_or_derive_api_creds()
# api_creds: {"api_key": "...", "api_secret": "...", "api_passphrase": "..."}

# Credential'ları client'a kaydet
client.set_api_creds(api_creds)
```

### Limit Order Gönderme (Maker = %0 Fee)
```python
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
from py_clob_client.order_builder.constants import BUY

# Orderbook'tan en iyi alış fiyatını çek
orderbook = client.get_orderbook(token_id=condition_id)
best_bid = float(orderbook.bids[0].price) if orderbook.bids else None

# Limit order oluştur ve gönder
order_args = OrderArgs(
    token_id=condition_id,    # paper_trades.json'daki condition_id
    price=entry_price,         # 0.24 gibi (NOT 24, değil %)
    size=LIVE_SHARES,          # 5 share
    side=BUY,
)
signed_order = client.create_order(order_args)
resp = client.post_order(signed_order, OrderType.GTC)  # GTC = Good Till Cancelled

# resp örneği:
# {"orderID": "0xabc123...", "status": "matched" veya "unmatched"}
```

### Order Status Kontrolü
```python
order = client.get_order(order_id="0xabc123...")
# order.status: "OPEN" | "FILLED" | "CANCELLED" | "PARTIALLY_FILLED"
# order.size_matched: dolan miktar
# order.price: emir fiyatı
```

### Order İptali
```python
client.cancel(order_id="0xabc123...")
# veya hepsini iptal et:
client.cancel_all()
```

### USDC Bakiye Kontrolü
```python
balance = client.get_balance_allowance(
    asset_type=AssetType.USDC,
    signature_type=SignatureType.EOA
)
# balance.balance: USDC miktarı (6 decimal, 1_000_000 = 1 USDC)
usdc_available = float(balance.balance) / 1_000_000
```

### Önemli: USDC Allowance (Tek Seferlik)
Cüzdan ilk kez Polymarket'te işlem yapacaksa CTF Exchange'in USDC harcamasına izin vermek gerekir:
```python
client.update_balance_allowance(
    asset_type=AssetType.USDC,
    signature_type=SignatureType.EOA,
    amount=str(1_000_000_000)  # 1000 USDC allowance
)
```
Alternatif: https://polymarket.com'a cüzdanla giriş yapıp ilk trade onayını web'den vermek.

---

## 6. Yapılacaklar — trader.py Modülü

**Oluşturulacak dosya:** `/root/weather/bot/trader.py`

### Modül Yapısı
```python
# bot/trader.py
"""
Polymarket CLOB Live Order Engine
Limit order gönder, dolum izle, P&L hesapla.

Kullanım:
  python trader.py place --station epwa --date 2026-04-18
  python trader.py check-fills
  python trader.py cancel-stale
  python trader.py status
  python trader.py balance
"""
```

### .env Dosyası (VPS'te, gitignored)
```env
# /root/weather/.env
PK=0x...                          # Trading cüzdanı private key
POLYMARKET_HOST=https://clob.polymarket.com
CHAIN_ID=137
PM_API_KEY=...                    # client.create_or_derive_api_creds() ile üretilir
PM_API_SECRET=...
PM_API_PASSPHRASE=...
```

**Güvenlik:** `.env` dosyası `/root/weather/.gitignore`'da zaten var (predictions.json gibi).

### live_trades.json Yapısı
**Dosya:** `/root/weather/bot/live_trades.json`

```json
[
  {
    "id": "epwa_2026-04-18_100523_live",
    "paper_id": "epwa_2026-04-18_100523",
    "station": "epwa",
    "date": "2026-04-18",
    "top_pick": 15,
    "bucket_title": "15°C",
    "condition_id": "0x43881041f15912ad438bfe7121504e4cfe0325d76767c38bfd48c8992c51d19e",
    "order_id": "0xabc123def456...",
    "order_status": "OPEN",
    "limit_price": 0.24,
    "shares": 5,
    "cost_usdc": 1.20,
    "fill_price": null,
    "fill_time": null,
    "placed_at": "2026-04-18T04:00:15",
    "expires_at": "2026-04-18T16:00:15",
    "status": "pending_fill",
    "result": null,
    "pnl_usdc": null,
    "settled_at": null,
    "notes": ""
  }
]
```

**Status değerleri:**
- `pending_fill` — order gönderildi, dolmadı
- `filled` — tamamen doldu
- `partially_filled` — kısmen doldu
- `cancelled` — iptal edildi (stale veya manuel)
- `settled_win` — market kapandı, kazandık
- `settled_loss` — market kapandı, kaybettik

### Fonksiyon Spesifikasyonları

#### `setup_client() -> ClobClient`
```python
def setup_client() -> ClobClient:
    """Env'den PK oku, CLOB client oluştur, cred türet."""
    pk   = os.getenv("PK")
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    cid  = int(os.getenv("CHAIN_ID", "137"))
    
    client = ClobClient(host=host, key=pk, chain_id=cid)
    
    # Kayıtlı cred varsa kullan, yoksa türet ve kaydet
    api_key = os.getenv("PM_API_KEY")
    if api_key:
        client.set_api_creds({"api_key": api_key, ...})
    else:
        creds = client.create_or_derive_api_creds()
        # → .env dosyasına yaz
    return client
```

#### `place_limit_order(condition_id, price, shares, station, date, top_pick, bucket_title, paper_id) -> dict | None`
```python
def place_limit_order(...) -> dict | None:
    """
    1. USDC bakiye kontrolü (MIN_USDC_RESERVE altındaysa None döner)
    2. Max açık live trade kontrolü (MAX_OPEN_LIVE_TRADES)
    3. Aynı station+date+top_pick için live order zaten var mı?
    4. best_bid'i orderbook'tan çek
    5. limit_price = max(price, best_bid) → daha iyi fiyata gir
    6. GTC limit order gönder
    7. live_trades.json'a kaydet
    8. dict döner: {"order_id": "...", "limit_price": ..., "status": "pending_fill"}
    """
```

#### `check_fills() -> int`
```python
def check_fills() -> int:
    """
    Tüm pending_fill order'larını CLOB API'ye sor.
    Dolmuşsa status="filled", fill_price güncelle.
    Stale (expires_at geçmişse) → cancel + re-place.
    Returns: güncellenen trade sayısı
    """
```

#### `cancel_stale_orders(max_age_hours=12) -> int`
```python
def cancel_stale_orders(max_age_hours=12) -> int:
    """
    max_age_hours saatten eski pending_fill order'ları iptal et.
    İstersen aynı fiyatla veya +1¢ fazlasıyla yeniden gir.
    """
```

#### `settle_live_trades()`
```python
def settle_live_trades():
    """
    Dün tarihli filled order'lar için settlement kontrol et.
    METAR/WU'dan gerçek sıcaklık çek (scanner.py'deki settle mantığıyla aynı).
    pnl_usdc hesapla, status güncelle.
    """
```

#### `get_balance() -> float`
```python
def get_balance() -> float:
    """USDC on Polygon bakiyesini döner."""
```

### Risk Kontrol Sabitleri
```python
LIVE_SHARES         = 5       # her işlemde 5 share (paper'da 10'du)
MAX_OPEN_LIVE_TRADES = 20     # aynı anda max açık pozisyon
MAX_DAILY_SPEND_USDC = 30.0   # günlük yeni emir limiti
MIN_USDC_RESERVE    = 10.0    # bu altına inerse yeni emir açılmaz
ORDER_EXPIRY_HOURS  = 12      # bu kadar saat dolmazsa stale
```

---

## 7. Scanner Entegrasyonu

### `scanner.py`'a Ekleme

`scan()` fonksiyonuna `--live` flag'i:

```python
# scanner.py scan() fonksiyonunda:
import sys
LIVE_MODE = "--live" in sys.argv

# scan_date() başarılı sinyal döndürünce:
trade = scan_date(station, target_date, trades, station_biases)
if trade:
    trades.append(trade)
    
    if LIVE_MODE:
        from bot.trader import place_limit_order
        live_result = place_limit_order(
            condition_id = trade["condition_id"],
            price        = trade["entry_price"],
            shares       = LIVE_SHARES,          # 5
            station      = trade["station"],
            date         = trade["date"],
            top_pick     = trade["top_pick"],
            bucket_title = trade["bucket_title"],
            paper_id     = trade["id"],
        )
        if live_result:
            print(f"  🔴 LIVE ORDER: {live_result['order_id']} @ {live_result['limit_price']}")
        else:
            print(f"  ⚠️  Live order gönderilemedi (bakiye/limit)")
```

### Cron Güncelleme (canlıya geçince)
```bash
# Mevcut (paper only):
0 4,10,16,22 * * *  python3 bot/scanner.py scan

# Canlıya geçince:
0 4,10,16,22 * * *  python3 bot/scanner.py scan --live

# Check-fills (her 30 dakika):
*/30 * * * *  cd /root/weather && python3 bot/trader.py check-fills >> bot/trader.log 2>&1

# Live settle (settle ile aynı saatte, peşinden):
5 11 * * *  cd /root/weather && python3 bot/trader.py settle-live >> bot/trader.log 2>&1
```

---

## 8. BTC Botu ile Çakışma Yönetimi

**Problem:** Aynı cüzdan BTC botu tarafından da kullanılıyor. İkisi aynı anda Polygon'a işlem atarsa **nonce çakışması** olabilir (transaction reddedilir).

**Çözüm — `place_limit_order()` içinde:**
```python
from web3 import Web3

def check_pending_tx(wallet_address: str) -> bool:
    """Cüzdanda bekleyen Polygon transaction var mı?"""
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    nonce_confirmed = w3.eth.get_transaction_count(wallet_address, "latest")
    nonce_pending   = w3.eth.get_transaction_count(wallet_address, "pending")
    return nonce_pending > nonce_confirmed  # True = pending tx var

# place_limit_order içinde:
if check_pending_tx(wallet_address):
    time.sleep(10)   # 10 sn bekle, tekrar kontrol et
    # max 3 deneme
```

**Ekstra bağımlılık:**
```bash
pip install web3
```

---

## 9. Uygulama Adımları (Sıralı)

### Adım 1 — Bağımlılıklar (VPS'te)
```bash
pip install py-clob-client eth-account web3 python-dotenv
```

### Adım 2 — .env Dosyası
```bash
# /root/weather/.env dosyasına ekle:
PK=0x[CÜZDAN_PRIVATE_KEY]
POLYMARKET_HOST=https://clob.polymarket.com
CHAIN_ID=137
# PM_API_KEY, PM_API_SECRET, PM_API_PASSPHRASE ilk çalıştırmada doldurulacak
```

### Adım 3 — API Credential Türetme
```bash
cd /root/weather
python3 -c "
from py_clob_client.client import ClobClient
import os; from dotenv import load_dotenv; load_dotenv()
c = ClobClient(os.getenv('POLYMARKET_HOST'), key=os.getenv('PK'), chain_id=int(os.getenv('CHAIN_ID')))
creds = c.create_or_derive_api_creds()
print(creds)
# Çıktıyı .env'e ekle
"
```

### Adım 4 — USDC Allowance Kontrolü
```bash
python3 -c "
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, SignatureType
import os; from dotenv import load_dotenv; load_dotenv()
c = ClobClient(os.getenv('POLYMARKET_HOST'), key=os.getenv('PK'), chain_id=int(os.getenv('CHAIN_ID')))
c.set_api_creds({...})
bal = c.get_balance_allowance(asset_type=AssetType.USDC, signature_type=SignatureType.EOA)
print('USDC balance:', float(bal.balance)/1e6, 'allowance:', float(bal.allowance)/1e6)
"
```

Eğer allowance düşükse:
```bash
python3 -c "
# ... client setup ...
c.update_balance_allowance(
    asset_type=AssetType.USDC,
    signature_type=SignatureType.EOA,
    amount=str(1_000_000_000)  # 1000 USDC
)
print('Allowance verildi')
"
```

### Adım 5 — trader.py Yaz
Yukarıdaki fonksiyon spesifikasyonlarına göre `/root/weather/bot/trader.py` dosyasını oluştur.

### Adım 6 — Test (Küçük Miktar)
```bash
# Önce paper scan ile sinyal bul:
python3 bot/scanner.py scan

# Tek bir trade için manuel live test:
python3 bot/trader.py place --station eglc --date 2026-04-19 --condition-id 0x... --price 0.24 --shares 1

# Sonucu izle:
python3 bot/trader.py status

# Fill kontrolü:
python3 bot/trader.py check-fills
```

### Adım 7 — Cron Aktivasyonu
Paper scan'i `--live` flagiyle güncelle (kullanıcı onayından sonra).

---

## 10. Güvenlik Kontrol Listesi

- [ ] `.env` dosyası `/root/weather/.gitignore`'a ekli (kontrol et: `git check-ignore -v .env`)
- [ ] `live_trades.json` da gitignore'da (paper_trades.json gibi)
- [ ] Private key terminal history'de görünmesin (`history -d` veya `HISTCONTROL=ignorespace`)
- [ ] VPS'e SSH key ile girilsin, password auth kapalı olsun
- [ ] `MAX_DAILY_SPEND_USDC` limiti aktif olsun
- [ ] İlk 1 hafta 1-2 share ile test (tam 5'e geçmeden önce)

---

## 11. Beklenen Live Trading Rakamları

### Başlangıç Senaryosu (5 share/trade)
```
Ortalama fiyat: 0.25 USDC/share
Cost per trade: 5 × 0.25 = 1.25 USDC
Win payoff:     5 × (1 - 0.25) = 3.75 USDC
Loss payoff:    -1.25 USDC

Günlük yeni emir (ortalama 5 şehir × 2 gün):
  5 × 2 × 1.25 = 12.50 USDC (max yeni sermaye)

Haftalık P&L tahmini (paper performance baz alınırsa):
  %35 win rate → EV per trade = (0.35 × 3.75) - (0.65 × 1.25) = +0.50 USDC/trade
  Haftada ~15-20 trade settle olursa: +7.50 - +10.00 USDC/hafta
  ROI: ~%60-80 (yüksek, küçük sermaye nedeniyle abartılı görünebilir)
```

### Başlangıç Sermayesi Önerisi
```
Minimum: 50 USDC
Rahat:   100 USDC (risk yönetimi için tampon)
Ayrıca:  1-2 MATIC (gas için, Polygon'da çok ucuz)
```

---

## 12. Frontend Güncellemesi (Opsiyonel — Sonraki Aşama)

`static/index.html`'e live trading sekmesi eklenecek:
- **📄 Paper** | **🔴 Live** sekme geçişi
- Live pozisyonlar: order_id, fill durumu, gerçek USDC maliyet, P&L
- Bakiye göstergesi: `/api/live-balance` endpoint'i
- Order iptal butonu (belirli order'lar için)

Bu aşama trader.py tamamlandıktan sonra yapılacak.

---

## 13. Özet — Bu Ajanın Yapacakları

1. **VPS'e bağlan:** `ssh root@135.181.206.109`
2. **Bağımlılıkları kur:** `pip install py-clob-client eth-account web3 python-dotenv`
3. **`.env` dosyasını oluştur:** Private key + Polymarket creds
4. **API creds türet:** `create_or_derive_api_creds()`
5. **USDC allowance ver:** `update_balance_allowance()`
6. **`/root/weather/bot/trader.py` yaz:** Yukarıdaki spesifikasyona göre
7. **`scanner.py`'ı güncelle:** `--live` flag desteği ekle
8. **Test:** Önce 1 share ile tek trade, izle, sonra aktifleştir
9. **Cron güncelle:** `scan --live` ve `check-fills` ekle
10. **Kullanıcıya bildir:** Her şey hazır, onay bekle

---

*Son güncelleme: 2026-04-17 | Paper trading sonuçları: 74 trade, %35.1 win rate, +$77.40 net P&L*
