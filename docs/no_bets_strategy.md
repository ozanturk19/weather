# NO Betleri Strateji Dokümanı
> Oluşturulma: 2026-04-25 | Güncelleme: 2026-04-26 (commit 0c64633)

---

## 1. Mevcut Durum

### NO Bot (`weather_no_bot.ts`)

| Parametre | Değer | Notlar |
|-----------|-------|--------|
| Dosya | `/opt/polymarket/bot/weather_no_bot.ts` | ~730 satır, TypeScript |
| Veri kaynağı | `http://localhost:8001/api/ens-buckets` | YES botu ile aynı endpoint |
| Trade dosyası | `/opt/polymarket/bot/data/weather_no_trades.json` | |
| Order tipi | GTC maker, `negRisk=true` | Polymarket CLOB NO token |
| `BUY_SHARES` | **7** | ~~6~~ → 7 (26 Nis): 7×0.999=6.993 → MIN_SELL_SHARES buffer güvenli |
| `MIN_SELL_SHARES` | **5** | CLOB minimum satış miktarı |
| `AUTO_SELL_PRICE` | **0.99** | ~~0.997~~ → fill kolaylığı için düşürüldü (26 Nis) |
| `AUTO_SELL_FALLBACK` | 0.99 | Tick 0.01 ise fallback (artık ana fiyatla aynı) |
| `AUTO_SELL_MIN_EDGE` | 0.005 | Fill sonrası en az 0.5 cent kâr zorunlu |
| `MIN_USDC_RESERVE` | **8.0 USDC** | ~~5.0~~ → YES bot $10 ile dengeli (26 Nis) |
| `MAX_OPEN` | 20 | Aynı anda max açık pozisyon |
| `MIN_LIQUIDITY` | $300 | Her iki tier için minimum likidite |

**Cron programı:**
```
0  1,3,5,...,23 * * *  scan          → her 2 saatte bir (tek saatler UTC)
*/15 * * * *           check-fills   → 15 dakikada bir
30  1,7,13,19 * * *    cancel-stale  → günde 4 kez (4 saat eşiği)
20  11      * * *      redeem        → 11:20 UTC, Python bot'tan 5dk sonra
```

### Performans (2026-04-26 itibarıyla)

| Durum | Adet |
|-------|------|
| Sold (tamamlanan) | 7 |
| Cancelled | 14 |
| Settled Pending | 3 |
| Sell Pending | 1 |
| **Toplam** | **25** |

**Kapalı 7 trade — PnL dökümü:**

| Tier | İstasyon | Tarih | Bucket | Buy | Sell | PnL |
|------|----------|-------|--------|-----|------|-----|
| OLD | LTAC | 25 Nis | 19°C | 0.920 | 0.870 | **-$0.30** |
| OLD | LFPG | 25 Nis | 24°C | 0.930 | 0.910 | **-$0.12** |
| OLD | EFHK | 25 Nis | 5°C | 0.940 | 0.940 | $0.00 |
| OLD | LFPG | 25 Nis | 24°C | 0.870 | 0.990 | **+$0.72** |
| OLD | EHAM | 25 Nis | 16°C | 0.780 | 0.990 | **+$1.26** |
| T1 | LTAC | 25 Nis | 16°C | 0.930 | 0.990 | **+$0.30** |
| T1 | LTFM | 25 Nis | 22°C+ | 0.970 | 0.990 | **+$0.10** |

**Net PnL: +$1.96** | Win rate: 4/7 = **57%** (3 OLD + 2 T1 kâr)

> ⚠️ Erken dönem, veri az. OLD tier = sistematik strateji öncesi manuel+ilk emirler.
> T1-CAPPED tier: 2/2 kâr (%100 win rate, 2 trade).

### Açık Pozisyonlar (26 Nisan)

| Pozisyon | Status | Fill | Token | Beklenti |
|---|---|---|---|---|
| LIMC Milano 24°C Apr26 | 📤 SATIŞTA @0.99 | 0.94 | 5 token | ~+$0.25 kâr |
| LTAC Ankara 18°C Apr26 | ⏳ SETTLEMENT BEKLE | 0.98 | 4 token | Settlement ~$4 |
| LEMD Madrid 27°C Apr26 | ⏳ SETTLEMENT BEKLE | 0.84 | 4 token | Settlement ~$4 |
| EGLC Londra 22°C Apr25 | ⏳ SETTLEMENT BEKLE | 0.90 | 1 token | Settlement ~$1 |

**LTAC ve LEMD token sayısı 4 — `min_size_stuck`**: negRisk under-delivery nedeniyle 5 share sipariş edilip 4.995 token gelmesi (bkz. §5b). Satılamıyor, settlement bekliyor.

---

## 2. Strateji Mantığı

### TIER 1 — CAPPED (0 ensemble üyesi)

```
Filtreler:
  • cappedSet = true (ENS% = 3% cap — 0 model üyesi bu bucket'ı seçmedi)
  • PM YES fiyatı: %1.5 – %40  (YES_MIN_CAPPED – YES_MAX_CAPPED)
  • Minimum likidite: $300
  • Engeller (GFS soğuk bias koruması):
      ISINMA (warming) + isAbove  → ATLA  (üst extreme, warming ile örtüşüyor)
      SOGUMA (cooling) + isBelow  → ATLA  (alt extreme, cooling ile örtüşüyor)

Fiyat:
  buyPrice = min(1 - yes_price, 0.99)   ← at-market, doğrudan taker fill

Mantık:
  Model hiçbir senaryoda bu bucket'ı settle olmaya yatkın görmüyor.
  Capped ENS = %3 üst sınır → gerçek ihtimal çok daha düşük (muhtemelen %0.5-1).
  Edge = PM% - ENS% negatif görünse de gerçek edge pozitif (cap yanıltıcı).

Risk:
  • GFS modeli soğuk yanlı bias → warming+isAbove kombinasyonu gerçekten riskli
  • PM bazen düşük fiyatlı bucket'larda bozuk orderbook (MIN_LIQUIDITY=$300 filtresi)
  • negRisk under-delivery → 5 sipariş = 4 token (bkz. §5b)
```

### TIER 2 — NEAR-MISS (1-3 ensemble üyesi)

```
Filtreler:
  • ENS%: %3 – %7  (ENS_MAX_NEAR = 0.07)
  • PM YES fiyatı: en az %5  (YES_MIN_NEAR)
  • PM% – ENS% ≥ 4 puan fark  (EDGE_MIN_NEAR = 0.04)
  • Mode'dan en az 2°C uzak  (MIN_MODE_DIST = 2)
  • Trend filtresi ZORUNLU (NOTR → atla):

Trend filtresi (mean - mode):
  > +0.3°C → ISINMA: sadece mode'un ALTINDAKI (isBelow değil, min 2°C uzak) bucket'lar
  < -0.3°C → SOGUMA: sadece mode'un ÜSTÜNDEKİ bucket'lar güvenli
  ≤ 0.3°C  → NOTR:  Tier 2 atlanır (yön belirsiz)

Fiyat:
  buyPrice = min(1 - yes_price - 0.01, 0.99)   ← 1 tick altından maker

Mantık:
  Az üye → zayıf ihtimal ama yok değil. Yön analizi şart.
  Örn: model 19°C, ısınma trendi → 17°C bucket NO güvenli, 21°C NO riskli.
```

---

## 3. Sistem Entegrasyonu

### Veri Akışı

```
Open-Meteo + ICON
      ↓
  main.py (FastAPI, port 8001)
  - bias correction (Kalman offset)
  - settlement delta (WU-OM Faz A)
      ↓ /api/ens-buckets
      ├── YES Bot (Python)  → /root/weather/bot/live_trades.json
      │     corrected_mode, corrected_mean, trend, bias
      └── NO Bot (TypeScript) → /opt/polymarket/bot/data/weather_no_trades.json
            cappedSet, bucketProbs, mode, mean, trend
```

Her iki bot **aynı endpoint**'ten besleniyor — bias correction ve settlement delta
`ens-buckets` sunucusu tarafından uygulanıyor, her ikisine otomatik yansıyor.

### Settlement Pipeline

```
11:05 UTC → Python bot: scanner settle (on-chain market resolution kontrolü)
11:15 UTC → Python bot: trader redeem (kendi pozisyonları için CTF redeem)
11:20 UTC → NO bot:     redeem command (settled_pending trades → CTF redeem)
```

`redeem` komutu her trade için `payoutDenominator` kontrol eder;
> 0 ise `redeemPositions(USDC.e, bytes32(0), conditionId, [2])` çağırır.
conditionId dönüşümü: decimal string → `hexZeroPad(BigNumber.from(id), 32)`.

### Koordinasyon Eksiklikleri

| Sorun | Etki | Çözüm |
|-------|------|-------|
| YES bot LONG → NO bot aynı bucket'a NO | Kendine karşı bahis | `live_trades.json` cross-check |
| Reserve paylaşımı yok | İkisi aynı anda bakiye tükütebilir | Ortak bakiye limiti |
| JSON file race condition | Eş zamanlı scan+check-fills → veri kaybı | File lock veya DB |

---

## 4. NO Betlerinin Avantajı

| Kriter | YES Bahsi | NO Bahsi |
|--------|-----------|----------|
| Doğruluk gereksinimi | Kesin bucket tahmini | "Bu bucket değil" yeterli |
| Hata toleransı | 1°C kayması → kayıp | ±2-3°C kayması → hâlâ kazanç |
| Model bağımlılığı | Yüksek | Daha düşük (negatif sinyal) |
| Tipik NO fiyat aralığı | — | 0.60 – 0.985 |

**Özellikle güçlü senaryolar:**
1. **Extreme bucket'lar** — model 0 üye koyar, PM spekülatif fiyat (T1-CAPPED)
2. **Tersine trend** — model cooling gösterirken PM sıcak bucket'ları fazla fiyatlar (T2)
3. **Settlement kaynak uyumsuzluğu** — WU farklı okurken PM WU baz alır, model OM baz alır

---

## 5. Bilinen Sorunlar, Bulgular ve Düzeltmeler

### ✅ Çözüldü

**a) AUTO_SELL_PRICE = 0.997 → 0.99 (26 Nisan 2026)**
- NO token'lar tick 0.01'lik piyasalarda 0.997'den fill almak çok zor
- Artık direkt 0.99 — fallback ile de örtüşüyor
- Fallback ve ana fiyat artık aynı (0.99)

**b) negRisk token under-delivery — BUY_SHARES = 6 çözümü**
- Polymarket negRisk mekanizması: 5 share sipariş → 4.995 token
- `Math.floor(4.995) = 4` token → CLOB min satış boyutu 5 → satış imkânsız
- **Çözüm:** `BUY_SHARES = 6` → ~5.994 token → floor = **5** → satılabilir
- MIN_SELL_SHARES = 5 olarak sabitlendi
- `placeAutoSell` gerçek token bakiyesini sorgular; <5 ise `min_size_stuck` notlar, `settled_pending` statüsüne alır

**c) `settled_pending` duplicate check eksikliği (25 Nisan 2026 keşfedildi)**
- Scanner, `settled_pending` statüsündeki trades için `alreadyOpen` kontrolü yapmıyordu
- Aynı station+date+bucket kombinasyonunu tekrar alıyordu (double position!)
- **Çözüm:** `alreadyOpen` ve `openTrades` filtrelerine `'settled_pending'` eklendi

**d) `check-fills` — filled ama sell_order_id eksik trades**
- `placeAutoSell` başarısız olunca status `filled` kalıyordu ama sell emri yoktu
- Sonraki check-fills çalışmalarında bu trades atlanıyordu
- **Çözüm:** `filledNoSell` loop eklendi — her check-fills bu trades için retry yapar

### ✅ Çözüldü (26 Nisan 2026 — commit 0c64633)

**e) JSON race condition → Atomic saveTrades**
- `writeFileSync` → `.tmp` + `renameSync` → aynı filesystem'de atomic
- Cron offset: check-fills `*/15` → `7,22,37,52` (scan :00 çakışması giderildi)

**f) CLOB null → auto-cancel**
- `getOrder` null/boş yanıt döndürünce `clob-null-cancel` notu ile `cancelled`
- 4 adet "clob-not-found" trade bu kategori (Apr26 LTFM, LFPG, RJTT, RKSI)

**g) T2 stale threshold 4h → 6h**
- T2-near maker emirleri notes'ta `T2-near` ile tespit ediliyor
- T1 at-market: 4h | T2 maker (-1tick): 6h
- Cancel analizi: T2 cancelled = 0 (henüz); T1 cancelled = 14 (16 toplam)

### ✅ Çözüldü (26 Nisan 2026 — commit 0c64633, devam)

**h) Cancel analizi tamamlandı**
- 16 cancelled: 6 stale, 4 clob-not-found (=CLOB null), 2 market-closed, 2 ISINMA-risk, 2 diğer
- T1 cancelled=14, T2 cancelled=0 → T2 stale fix önleyici nitelikte

**i) YES-NO cross-check**
- Scan sırasında `/root/weather/bot/live_trades.json` okunuyor
- Aynı station+date+threshold için active YES pozisyon varsa NO emri atlanıyor
- YES bot kapalı olsa bile dosya yoksa sessizce geçiyor

**j) MIN_USDC_RESERVE $5 → $8**
- YES bot $10 ile dengeli; aynı cüzdanda çakışmayı önler

### 📋 Backlog

**k) Settlement delta direkt entegrasyon**
- YES bot settlement delta (WU-OM offset) kullanıyor
- NO bot bu delta'yı yalnızca dolaylı (endpoint üzerinden) alıyor
- Bucket seçiminde direkt delta uygulaması daha doğru NO eşiği üretebilir

**l) T1 istasyon genişletme**
- Şu an whitelist yok; tüm 14 istasyon T1-CAPPED için taranıyor
- Backtest verisi olmayan RJTT/RKSI gibi istasyonlarda fill sonrası settlement sürprizleri
- Öneri: T1 için de istasyon bazlı win-rate takibi başlatılsın

---

## 6. Cron Programı (Tam)

```bash
# ── Weather NO Bot (TypeScript) — 2 saatlik tarama ──────────────────────
# Scan: her çift saatte bir (Python scanner 04,10,16,22 → NO bot 01,03,...,23)
0 1,3,5,7,9,11,13,15,17,19,21,23 * * * \
  cd /opt/polymarket/bot && npx ts-node weather_no_bot.ts scan \
  >> /opt/polymarket/bot/logs/weather_no_scan.log 2>&1

# Check-fills: 15 dakikada bir
*/15 * * * * \
  cd /opt/polymarket/bot && npx ts-node weather_no_bot.ts check-fills \
  >> /opt/polymarket/bot/logs/weather_no_fills.log 2>&1

# Cancel-stale: scan'dan 30 dk sonra (1:30, 7:30, 13:30, 19:30 UTC)
30 1,7,13,19 * * * \
  cd /opt/polymarket/bot && npx ts-node weather_no_bot.ts cancel-stale \
  >> /opt/polymarket/bot/logs/weather_no_scan.log 2>&1

# Redeem: Python bot redeem'inden 5 dk sonra (11:20 UTC)
20 11 * * * \
  cd /opt/polymarket/bot && npx ts-node weather_no_bot.ts redeem \
  >> /opt/polymarket/bot/logs/weather_no_redeem.log 2>&1
```

---

## 7. Komut Referansı

```bash
cd /opt/polymarket/bot

# Manuel scan — fırsat tara, yeni BUY NO aç
npx ts-node weather_no_bot.ts scan

# Fill kontrolü + auto-sell yerleştir
npx ts-node weather_no_bot.ts check-fills

# Açık pozisyon görüntüle
npx ts-node weather_no_bot.ts status

# Eski emirleri iptal et (>4h)
npx ts-node weather_no_bot.ts cancel-stale

# settled_pending pozisyonları on-chain redeem et
npx ts-node weather_no_bot.ts redeem
```

**Loglar:**
```bash
tail -100 /opt/polymarket/bot/logs/weather_no_scan.log
tail -100 /opt/polymarket/bot/logs/weather_no_fills.log
tail -100 /opt/polymarket/bot/logs/weather_no_redeem.log
```

**Trade dosyaları:**
```bash
# NO bot trades
cat /opt/polymarket/bot/data/weather_no_trades.json | python3 -m json.tool

# YES bot trades
cat /root/weather/bot/live_trades.json | python3 -m json.tool
```

---

## 8. negRisk Özel Notlar

Polymarket weather marketleri negRisk formatındadır (11 bucket / şehir-gün).
Bu mekanizmanın standart binary market'ten farkları:

| Konu | Davranış | Etki |
|------|----------|------|
| Token üretimi | 6 share sipariş → 5.994 token | `BUY_SHARES=6` ile aşılıyor |
| Min satış | CLOB min_size = 5 token | <5 token → satış imkânsız → settlement bekle |
| USDC lock | Maker emirlerde balance API'ye tam yansımıyor | Fazla emir → CLOB iptali |
| IndexSet | NO = indexSet 2, YES = indexSet 1 | `redeemPositions(..., [2])` |
| conditionId | Decimal string olarak saklanıyor | `hexZeroPad(BigNumber.from(id), 32)` ile bytes32'ye çevir |

---

## 9. Sistem Mimarisi

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
               ▼                                   ▼
        YES Bot (Python)                   NO Bot (TypeScript)
        scanner.py + trader.py             weather_no_bot.ts
               │                                   │
        live_trades.json               weather_no_trades.json
               │                                   │
               └─────────────────┬─────────────────┘
                                 │
                    ┌────────────▼────────────────┐
                    │  Dashboard (port 8004)       │
                    │  /weather-bot (PWA)          │
                    │  /api/weather-no             │
                    └─────────────────────────────┘
```

**Kısa vadeli iyileştirmeler — Tamamlandı:**
1. ~~AUTO_SELL_PRICE 0.997→0.99~~ ✅
2. ~~settled_pending duplicate check~~ ✅
3. ~~redeem komutu~~ ✅
4. ~~BUY_SHARES 5→6→7 (negRisk buffer)~~ ✅
5. ~~auto_redeem.js min_size_stuck handling~~ ✅
6. ~~JSON race condition (atomic save + cron offset)~~ ✅
7. ~~CLOB null → auto-cancel~~ ✅
8. ~~T2 stale 4h→6h~~ ✅
9. ~~YES-NO cross-check~~ ✅
10. ~~MIN_USDC_RESERVE 5→8~~ ✅

**Backlog (k, l yukarıda):**
- Settlement delta direkt entegrasyon
- T1 istasyon win-rate takibi
7. Unified dashboard: YES + NO pozisyonları tek ekranda
8. Settlement delta → NO bot bucket seçimine direkt etki

**Uzun vade:**
9. ML layer: YES/NO sinyal kombinasyonu → en iyi edge bucket çifti
10. Dynamic position sizing: total risk kontrolü

---

## 10. Özet Tablo

| Konu | Mevcut | Hedef |
|------|--------|-------|
| Strateji | TIER 1 (capped, 0 üye) + TIER 2 (near-miss, 1-3 üye) | + cross-check filtresi |
| Veri kaynağı | `/api/ens-buckets` (aynı YES bot endpoint) | Gelecekte shared log |
| Aktif PnL (7 trade) | **+$1.96** (T1: %100 win, OLD: %57 win) | İzlemeye devam |
| AUTO_SELL_PRICE | **0.99** ✅ | — |
| Cancelled rate | 14/25 = **%56** | < %30 hedef |
| negRisk under-delivery | BUY_SHARES=6 ✅ | MIN_SELL_SHARES=5 izle |
| Settlement | redeem cron 11:20 UTC ✅ | Auto-detect improved |
| YES-NO çakışma | Yok (bağımsız) | Cross-check ekle |
| Reserve | $5 (YES $10) | $7-10'a çıkar |
| Race condition | Var (JSON dosya) | File lock / SQLite |
