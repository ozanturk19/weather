# Polymarket Weather Trading Bot — Mimari & Dokümantasyon

## Genel Bakış

Bu bot, hava durumu tahmin modelimizin çıktısını Polymarket'teki sıcaklık marketlerine bağlar.
Model en olası settlement sıcaklığını (top pick) hesaplar, ilgili bucket ucuzsa otomatik pozisyon açar.

---

## Sistem Bileşenleri

```
┌─────────────────────────────────────────────────────────┐
│                   VPS (135.181.206.109)                  │
│                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │  FastAPI    │    │   Scanner   │    │  Backtest   │  │
│  │  main.py   │    │  bot/       │    │  engine.py  │  │
│  │  :8001     │    │  scanner.py │    │             │  │
│  └──────┬──────┘    └──────┬──────┘    └─────────────┘  │
│         │                  │                             │
│         ▼                  ▼                             │
│  ┌─────────────────────────────────────────────────┐    │
│  │             Open-Meteo API                      │    │
│  │  GFS · ECMWF · ICON · UKMO · MeteoFrance        │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│              Polymarket                                  │
│  Gamma API (fiyat okuma) + CLOB API (emir verme)        │
│  Settlement: Wunderground (5 şehir) + NOAA (İstanbul)   │
└─────────────────────────────────────────────────────────┘
```

---

## İstasyonlar & Settlement

| İstasyon | Şehir | PM Hacim/Gün | Settlement | Durum |
|---|---|---|---|---|
| EGLC | Londra City | ~$450K | Wunderground EGLC | Aktif |
| LFPG | Paris CDG | ~$219K | Wunderground LFPG | Aktif |
| LIMC | Milano Malpensa | ~$112K | Wunderground LIMC | Aktif |
| LEMD | Madrid Barajas | ~$105K | Wunderground LEMD | Aktif |
| LTFM | İstanbul | ~$90K | NOAA weather.gov | Aktif |
| LTAC | Ankara Esenboğa | ~$22K | Wunderground LTAC | Aktif |
| EHAM | Amsterdam Schiphol | ~$86K | Wunderground EHAM | Aktif |
| EDDM | Münih | ~$75K | Wunderground EDDM | Aktif |
| EPWA | Varşova Chopin | ~$77K | Wunderground EPWA | Aktif |
| EFHK | Helsinki Vantaa | ~$42K | Wunderground EFHK | Aktif |

---

## Model Ağırlıkları (Backtest Sonucu — 60 gün, MAE⁻¹)

```python
MODEL_WEIGHTS = {
    "icon":        1.8,   # D+1 MAE 1.08 — en iyi
    "ecmwf":       1.5,   # D+1 MAE 1.20
    "meteofrance": 0.9,
    "gfs":         1.0,
    "ukmo":        0.5,   # D+1 MAE 1.80 (LTFM'de 3.22°C — çok kötü)
}
```

---

## Karar Mantığı (Top Pick Stratejisi)

```
1. blend = ağırlıklı model ortalaması (ICON+ECMWF öncelikli)
2. bias_correction uygula (son 7 günlük sistematik hata)
3. top_pick = round(blend)           → en olası settlement °C
4. PM bucket'ını bul (threshold eşleştirme)
5. price = bucket.yes_price

Karar:
  price < 0.20  → 💰 ÇOK UCUZ — güçlü al sinyali
  price < 0.50  → ←  AL sinyali
  price ≥ 0.50  → market aynı fikirde, pas geç
```

---

## Bot Çalışma Döngüsü

```
Her gün 3 kez tarama:
  06:00 → Sabah (D+1 market açıldı mı?)
  12:00 → Öğlen (fiyat değişimi var mı?)
  18:00 → Akşam (son güncelleme)

Ertesi gün:
  10:00 → Settle (dünkü pozisyonlar kapandı mı?)
```

---

## Polymarket CLOB API — Canlı Trading Kurulumu

### Gereksinimler
```
1. Polymarket hesabı (polymarket.com)
2. MetaMask cüzdanı (Polygon ağı)
3. USDC on Polygon (Coinbase/Binance'den transfer)
4. pip install py-clob-client
```

### Kimlik Doğrulama
```python
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

client = ClobClient(
    "https://clob.polymarket.com",
    key=PRIVATE_KEY,       # MetaMask private key
    chain_id=POLYGON
)
# API credentials (L2) — ilk kurulumda bir kez çalıştır
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
```

### Emir Verme
```python
# YES pozisyon aç (top pick bucket satın al)
order = client.create_and_post_order({
    "token_id":   condition_id,   # bucket condition ID (Gamma API'dan gelir)
    "price":      price,          # limit fiyat (örn: 0.35)
    "size":       amount,         # share sayısı (size_usd / price)
    "side":       "BUY",
    "order_type": "GTC",          # Good Till Cancelled
})
```

---

## Risk Parametreleri

```python
BET_SIZE_USD   = 50    # tek işlem max $
DAILY_MAX_USD  = 300   # günlük toplam max $
MIN_PRICE      = 0.05  # çok ucuk = şüpheli likidite
MAX_PRICE      = 0.50  # bu altı "ucuz" sayılır
MAX_OPEN_POS   = 5     # aynı anda max açık pozisyon
STOP_DAILY_PNL = -200  # günlük -$200 → bot durur
```

---

## Paper Trading vs Canlı Trading

| | Paper | Canlı |
|---|---|---|
| Gerçek para | ❌ | ✅ |
| Cüzdan gerekir | ❌ | ✅ |
| Sonuçlar gerçek | ✅ | ✅ |
| Slippage | ❌ simüle edilmez | ✅ gerçek |
| Amaç | Strateji doğrulama | Kâr |

**Önce paper trading ile en az 30 gün test, sonra canlıya geç.**

---

## Dosya Yapısı

```
/root/weather/
├── main.py              # FastAPI backend
├── static/index.html    # Frontend dashboard
├── predictions.json     # Bias tracking (gitignored)
├── BOT.md               # Bu dosya
├── bot/
│   ├── scanner.py       # Paper/canlı trading botu
│   └── paper_trades.json  # Paper trade geçmişi (gitignored)
└── backtest/
    ├── fetch_actuals.py # Gerçek veriler (Iowa IEM ASOS)
    ├── engine.py        # Backtest motoru
    └── data/
        ├── actuals.json
        ├── forecasts.json
        └── results.json
```

---

## Kalibrasyon Sonuçları (60 gün backtest)

| Güven Aralığı | Model % | Gerçek % | Durum |
|---|---|---|---|
| 0–20% | %4.9 | %5.5 | ✓ İyi |
| 20–40% | %28.7 | %29.1 | ✓ İyi |
| 40–60% | %48.5 | %24.4 | ⚠ 2x overconfident |
| 60–80% | %65.1 | %17.9 | ⚠ 3.6x overconfident |

**Uygulanan düzeltme:** `CALIB_STD_FACTOR = 1.8` (Gaussian std inflate)

---

## Sonraki Adımlar

- [ ] Per-station model ağırlıkları (4 yeni istasyon 60 gün dolunca)
- [ ] CLOB API cüzdan bağlantısı
- [ ] Canlı emir verme (py-clob-client)
- [ ] Bias spike alert (hata >3°C → uyarı)
- [ ] Günlük P&L email/Telegram bildirimi
