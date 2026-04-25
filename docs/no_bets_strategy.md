# NO Betleri Strateji Dokümanı
> Oluşturulma: 2026-04-25 | Güncelleme: her sprint sonu

## 1. Mevcut Durum

### NO Bot (`weather_no_bot.ts`)

| Parametre | Değer |
|-----------|-------|
| Dosya | `/opt/polymarket/bot/weather_no_bot.ts` (722 satır, TypeScript) |
| Veri kaynağı | `http://localhost:8001/api/ens-buckets` (YES botu ile aynı) |
| Trade dosyası | `/opt/polymarket/bot/data/weather_no_trades.json` |
| Order tipi | GTC maker, `negRisk=true` (Polymarket CLOB NO token) |
| BUY_SHARES | 6 |
| AUTO_SELL_PRICE | 0.997 |
| MIN_USDC_RESERVE | 5.0 USDC |

**Cron programı:**
```
0 1,3,5,...,23 * * *  scan          → her 2 saatte bir (tek saatler)
*/15 * * * *           check-fills   → 15 dakikada bir
30 1,7,13,19 * * *    cancel-stale  → günde 4 kez
```

### Performans (2026-04-25 itibarıyla)

| Durum | Adet |
|-------|------|
| Sold (tamamlanan) | 7 |
| Cancelled | 14 |
| Settled Pending | 3 |
| Sell Pending | 1 |
| **Toplam** | **25** |

**Satılan 7 trade PnL:**

| İstasyon | Tarih | Bucket | Buy | Sell | PnL |
|----------|-------|--------|-----|------|-----|
| LTAC | 25 Nis | 19°C | 0.920 | 0.870 | -0.30 |
| LFPG | 25 Nis | 24°C | 0.930 | 0.910 | -0.12 |
| EFHK | 25 Nis | 5°C | 0.940 | 0.940 | 0.00 |
| LFPG | 25 Nis | 24°C | 0.870 | 0.990 | +0.72 |
| EHAM | 25 Nis | 16°C | 0.780 | 0.990 | **+1.26** |
| LTAC | 25 Nis | 16°C | 0.930 | 0.990 | +0.30 |
| LTFM | 25 Nis | 22°C+ | 0.970 | 0.990 | +0.10 |

**Net PnL: +$1.96** | Win rate: 5/7 = **71%** | Erken dönem, veri az.

---

## 2. Strateji Mantığı

### TIER 1 — CAPPED (0 ensemble üyesi)

```
Koşul:
  • ENS% = 3% (cap — 0 model üyesi bu bucket'ı seçmedi)
  • PM YES fiyatı: %1.5 – %40

Mantık:
  Model hiçbir senaryoda bu bucket'ı settle olmaya yatkın görmüyor.
  PM'de likidite varsa bile fiyat sıfıra yaklaşmalı → NO al.
  Trend filtresi UYGULANMAZ (zaten 0 üye → yön belirsizliği önemsiz).

Risk: PM bazen çok düşük fiyatlı bucket'ları yanlışlıkla likit gösterir
      (manipulation, bozuk orderbook). MIN_LIQUIDITY=$300 filtresi var.
```

### TIER 2 — NEAR-MISS (1-3 ensemble üyesi)

```
Koşul:
  • ENS%: %3 – %7 (1-3 üye bandı)
  • PM YES fiyatı: en az %5
  • PM% – ENS%: en az 4 puan fark
  • Mode'dan en az 2°C uzak olmalı
  • Trend filtresi ZORUNLU:

Trend filtresi:
  ISINMA (mean – mode > 0.3): sadece mode'un ALTINDAKI bucket'lar güvenli
  SOGUMA (mean – mode < -0.3): sadece mode'un ÜSTÜNDEKİ bucket'lar güvenli
  NOTR: Tier 2 atlanır

Mantık:
  Az üye → zayıf ihtimal ama yok değil. Yön analizi şart.
  Örn: model 19°C gösteriyor + ısınma trendi → 17°C bucket'a NO güvenli
       ama 21°C bucket'a NO riskli (trend oraya doğru).
```

---

## 3. Sistem Entegrasyonu

### Veri Akışı (Mevcut)

```
Open-Meteo + ICON
      ↓
  main.py (FastAPI, port 8001)
      ↓ /api/ens-buckets
      ├── YES Bot (Python)  → live_trades.json
      │     corrected_mode, corrected_mean, trend, bias
      └── NO Bot (TypeScript) → weather_no_trades.json
            cappedSet, bucketProbs, mode, mean, trend
```

Her iki bot **aynı endpoint**'ten besleniyor — bias correction, Kalman offset, settlement delta Faz A'daki düzeltmeler anında her ikisine de yansıyor. Senkronizasyon sorunu yok.

### Koordinasyon Eksiklikleri (Gelecek İyileştirme)

| Sorun | Etki | Çözüm |
|-------|------|-------|
| YES bot LONG → NO bot aynı bucket'a NO açıyor | Kendine karşı bahis | Cross-check endpoint |
| Reserve paylaşımı yok | İkisi aynı anda bakiye tükütebilir | Ortak bakiye limiti |
| NO bot cancel'ları YES bot'u etkilemiyor | Bilgi kaybı | Shared signal log |

---

## 4. NO Betlerinin Avantajı

### Neden NO bahsi YES'ten daha kolay olabilir?

| Kriter | YES Bahsi | NO Bahsi |
|--------|-----------|----------|
| Doğruluk gereksinimi | Kesin bucket tahmini | "Bu bucket değil" yeterli |
| Hata toleransı | 1°C kayması → kayıp | ±2-3°C kayması → hâlâ kazanç |
| Model bağımlılığı | Yüksek | Daha düşük (negatif sinyal) |
| Tipik fiyat aralığı | %5–40 | %60–98.5 (ters) |

**Örnek:** Model 19°C diyor. YES bot 19°C'ye bahse giriyor (%8 fiyat).
NO bot ise 22°C bucket'a NO açıyor (%95 fiyat) → 22°C'ye settle olursa sadece kaybeder,
ama model %3 ihtimal verdiği yerde PM %5 fiyat varsa edge var.

### Özellikle güçlü senaryolar:
1. **Extreme bucket'lar** (çok sıcak / çok soğuk) → model 0 üye koyar, PM spekülatif fiyat
2. **Tersine ısınma** → model cooling gösterirken PM çok sıcak bucket'ları fazla fiyatlar
3. **Settlement kaynak uyumsuzluğu** → WU farklı okurken PM WU baz alır, model OM baz alır

---

## 5. Mevcut Sorunlar ve İyileştirme Öncelikleri

### Kritik

**a) Sell fiyatı çok yüksek (0.997)**
- NO token'ları genellikle 0.99'dan daha yüksek fill edilemiyor (tick boyutu 0.01)
- Satışlar çok uzun sürüyor → bekleyen pozisyon birikiyor
- Öneri: `AUTO_SELL_PRICE = 0.99`, fallback `0.98` — hız > mükemmel fiyat

**b) 14 cancelled trade**
- Scan sık ama fill oranı düşük — maker GTC emirler settle öncesi dolmuyor
- Öneri: expiry window daralt (24h → 8h limit), agresif fiyat belirleme

**c) MIN_USDC_RESERVE = 5.0 (YES bot $10'a çıktı)**
- İki bot ayrı rezerv kullanıyor → koordineli bakiye yönetimi yok
- Öneri: NO bot reserve'ü de $7–10'a çıkar (ortak wallet)

### Orta Öncelik

**d) TIER 1'de spread filtresi yok**
- Bazı düşük likidite bucket'larda orderbook çok geniş
- 0 ensemble → PM fiyatı %15 ama spread %8 → gerçek edge yok
- Öneri: `best_ask – best_bid < 0.05` filtresi ekle

**e) YES-NO cross check eksik**
- YES bot LONG aldığı bucket için NO bot aksine bahis açabilir
- Öneri: `/root/weather/bot/live_trades.json` okuyup çakışan bucket'ları atla

**f) Settlement delta Faz A (WU-OM offset) NO bot'a henüz yansımıyor**
- YES bot artık settlement delta kullanıyor (settlement_delta.py Faz A1+A2)
- NO bot bu bilgiyi kullanmıyor — `ens-buckets` endpoint'i zaten delta uyguluyor,
  ama NO bot'un bucket seçim mantığında ek ayar gerekebilir
- **Durum:** `ens-buckets` server-side bias ile birleşiyor → dolaylı etki var, direkt yok

---

## 6. Önerilen Birleşik Sistem Mimarisi

```
                    ┌─────────────────────────────┐
                    │  main.py  (port 8001)        │
                    │  /api/ens-buckets            │
                    │  - corrected_mode/mean       │
                    │  - settlement_delta (Faz A)  │
                    │  - Kalman bias               │
                    └────────────┬────────────────┘
                                 │
               ┌─────────────────┼─────────────────┐
               │                                   │
        YES Bot (Python)                   NO Bot (TypeScript)
        scanner.py + trader.py             weather_no_bot.ts
               │                                   │
        live_trades.json               weather_no_trades.json
               │                                   │
               └─────────────────┬─────────────────┘
                                 │
                    ┌────────────▼────────────────┐
                    │  Unified Signal Log          │
                    │  (gelecek — shared DB)       │
                    │  - Hangi bucket'lar alındı   │
                    │  - Cross-check collision     │
                    │  - Günlük PnL her iki bot    │
                    └─────────────────────────────┘
```

**Kısa vadeli entegrasyon (1-2 sprint):**
1. NO bot, YES bot live_trades.json'u okusun → çakışan bucket'ları atla
2. NO bot MIN_USDC_RESERVE $5→$7 (YES bot $10 ile dengeli)
3. NO bot AUTO_SELL_PRICE 0.997→0.99

**Orta vade (3-4 sprint):**
4. Shared signal log endpoint'i: `/api/signal-log` → her iki bot yazsın, okusun
5. Unified dashboard: hem YES hem NO pozisyonları tek ekranda
6. Settlement delta → NO bot bucket seçimine direkt entegrasyon

**Uzun vade (Faz C ile birlikte):**
7. ML layer: YES/NO sinyal kombinasyonu → hangi bucket çifti en iyi edge?
8. Dynamic position sizing: YES+NO aynı anda → total risk kontrolü

---

## 7. Hızlı Referans

### NO bot komutları (VPS)
```bash
cd /opt/polymarket/bot

# Manuel scan
npx ts-node weather_no_bot.ts scan

# Fill kontrolü
npx ts-node weather_no_bot.ts check-fills

# Açık pozisyon görüntüle
npx ts-node weather_no_bot.ts status

# Eski emirleri iptal et
npx ts-node weather_no_bot.ts cancel-stale
```

### Loglar
```bash
tail -100 /opt/polymarket/bot/logs/weather_no_scan.log
tail -100 /opt/polymarket/bot/logs/weather_no_fills.log
```

### Trade dosyaları
```bash
# NO bot trades
cat /opt/polymarket/bot/data/weather_no_trades.json | python3 -m json.tool

# YES bot trades  
cat /root/weather/bot/live_trades.json | python3 -m json.tool
```

---

## 8. Özet Tablo

| Konu | Mevcut | Hedef |
|------|--------|-------|
| NO bot stratejisi | TIER 1 + TIER 2 (capped / near-miss) | Aynı + cross-check |
| Veri kaynağı | /api/ens-buckets (aynı YES bot) | Aynı, gelecekte shared log |
| Aktif PnL (7 trade) | +$1.96 (+71% win rate) | İzlemeye devam |
| Sell fiyatı sorunu | 0.997 (çok yüksek, fill zor) | 0.99'a düşür |
| Cancelled emirler | 14/25 (%56) | <30% hedef |
| YES-NO çakışma | Yok (her biri bağımsız) | Cross-check ekle |
| Reserve | $5 (YES bot $10'a çıktı) | $7-10'a çıkar |
| Settlement delta | Dolaylı (endpoint üzerinden) | Direkt entegrasyon (Faz B) |
