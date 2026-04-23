# Polymarket Weather Trading Bot — Proje Kılavuzu

> **Son güncelleme:** 2026-04-23 (Faz 5 — kalibrasyon-odaklı filtreler)
> **Çalışma ortamı:** VPS (135.181.206.109) · FastAPI :8001 · 12 istasyon
> **Toplam test:** 280/280 geçiyor (239 normal + 41 crash)

---

## 1. Genel Bakış

Bot, 6 sayısal hava tahmin modelinin (ECMWF, ICON, GFS, UKMO, MeteoFrance, ECMWF-AIFS) 139-üyeli ensemble'ını Polymarket'in günlük maksimum sıcaklık marketlerine bağlar. Akış:

```
ensemble blend → bias düzeltme → top-pick bucket → fiyat & edge → sinyal kalitesi → BUY emir
                                                                                        ↓
                                                                          99.8¢ otomatik SELL (fill sonrası)
```

Filtre katmanları her adımda kötü sinyalleri elemeyi hedefler. **Canlı kalibrasyon 90g skill = −0.36** bulgusu üzerine Faz 5'te mid-range over-confidence bandı devre dışı bırakıldı.

---

## 2. Mimari

```
┌──────────────────────────────────────────────────────────────┐
│                VPS (135.181.206.109) :8001                    │
│                                                                │
│  FastAPI (main.py)  ◄────── static/index.html (dashboard)      │
│       │                                                        │
│       ├─► /api/weather       (blend, bias_active, uncertainty) │
│       ├─► /api/ensemble      (139 üye + bimodal + CI)          │
│       ├─► /api/polymarket    (buckets, condition_id, yes_price)│
│       ├─► /api/metar-history (METAR daily max — settle yedek)  │
│       ├─► /api/calibration   (Brier, reliability, skill, bins) │
│       ├─► /api/portfolio/var (Monte-Carlo VaR 95/99)           │
│       ├─► /api/settlement-audit (kaynak disagreement)          │
│       └─► /api/live-trades   (auto-sell durumu + açık poz)     │
│                                                                │
│  bot/                                                          │
│    ├─ scanner.py         (scan → paper trade)                  │
│    ├─ trader.py          (live CLOB — BUY + auto-SELL)         │
│    ├─ kalman.py          (adaptif bias — Faz 3)                │
│    ├─ signal_score.py    (0-100 kompozit kalite)               │
│    ├─ dynamic_weights.py (istasyon×model RMSE — Faz 4)         │
│    ├─ portfolio_var.py   (Gaussian copula + Cholesky)          │
│    ├─ calibration.py     (Brier skill, reliability bin)        │
│    └─ db.py              (SQLite WAL — model_forecasts, audit) │
│                                                                │
│  SQLite → /root/weather/bot/weather.db                         │
│  JSON   → paper_trades.json / live_trades.json                 │
└──────────────────────────────────────────────────────────────┘
                       │
    ┌──────────────────┼─────────────────────┐
    ▼                  ▼                     ▼
 Open-Meteo         Polymarket            Settlement
 (6 model +        Gamma API              Open-Meteo arşiv
  139-üye ens)     CLOB API               + METAR yedek
```

---

## 3. Strateji — GİRİŞ (Entry)

`bot/scanner.py → scan_date()` her istasyon × (D+1, D+2) için aşağıdaki **katmanlı filtre zincirini** uygular. Bir filtre pas derse trade açılmaz.

### 3.1 Sıralı Filtre Zinciri (8 katman)

| # | Katman | Sabit | Değer | Tanıma/Kaynak |
|---|---|---|---|---|
| **1** | **İstasyon skill pause** (Faz 5) | `STATION_SKILL_PAUSE` | `{lfpg, ltac, limc}` | 90g skill < −0.7, n≥10 |
| **2** | Ensemble konsensüs | `MIN_MODE_PCT` | 30 | üyelerin %30+ı aynı bucket'ta |
| **3** | CI kırılganlık (Faz 2) | `MIN_MODE_CI_LOW` | 20 | bootstrap %5 alt sınırı |
| **4** | **Mid-range over-confidence** (Faz 5) | `MID_RANGE_SKIP_[LOW,HIGH]` | [50, 80) | kalibrasyon kırık zone — **skip** |
| **5** | Bimodal uyarı | `BIMODAL_MAX_SEPARATION` | 1°C | tepe ayrımı > 1°C → pas |
| **6** | Uncertainty filtresi | `SKIP_UNCERTAINTY` | {yüksek, high} | API metadata |
| **7** | Fiyat aralığı | `MIN_PRICE` / `MAX_PRICE` | 0.05 / 0.40 | likidite + edge |
| **7b** | İstasyon fiyat tavanı | `STATION_MAX_PRICE` | `{lfpg: 0.18}` | Paris'e ek sıkı tavan |
| **8** | EV edge | `MIN_EDGE` | 0.05 | mode_pct − price ≥ 5pp |
| **9** | **Signal score gate** (Faz 5) | `MIN_SIGNAL_SCORE` | 55 | 0-100 kompozit, "zayıf"ı blokla |

Tüm filtreler geçtiğinde **BUY emir** açılır. Paper + live modda aynı zincir çalışır.

### 3.2 Bias Düzeltme (Adaptif — Faz 3 Kalman)

- Son 8+ kapalı trade için Kalman filtresi `yi = top_pick_i + ε_i` takip eder.
- Öğrenilen sistematik hata `top_pick'e +/- eklenir (max ±2°C).
- Örnek: EDDM geçmişte +1.4°C over-forecast → `top_pick += 1`.

### 3.3 2-Bucket Stratejisi (Hedge)

Ensemble'ın 2. tepesi 1°C bitişikteyse her iki bucket'a küçük pozisyon açar — WU/METAR ±1°C ölçüm belirsizliğine karşı doğal hedge. Her iki bucket aynı filtre zincirine tabidir.

---

## 4. Strateji — ÇIKIŞ (Exit)

### 4.1 Auto-Sell (Faz "auto-sell" — nakit döngüsü)

`bot/trader.py → place_auto_sell()` BUY fill'inden sonra **hemen** 99.8¢ limit SELL emri koyar:

| Adım | Eylem |
|---|---|
| BUY fill | `trader.check_fills()` her 30dk kontrol eder |
| Tick lookup | `client.get_tick_size()` → market'e göre 0.001 / 0.01 |
| Snap | 99.8¢ → tick'e yuvarlanır (0.99 veya 0.998) |
| SELL | GTC post_only (maker fee = 0%) |
| Margin guard | fill_price ≥ sell_price − `AUTO_SELL_MIN_EDGE` (0.02) → emir atlanır |

**Neden?** Claim/redeem cycle 24-48 saat, nakit döngüsünü bozuyordu. 99.8¢ SELL maker fee 0 ile redeem'le ekonomik olarak eşdeğer ama **likiditeyi** serbest bırakır.

### 4.2 Settlement (Cron 11:00)

`scanner.py settle` dünkü pozisyonları iki kaynakla kapatır:
1. **Birincil:** Open-Meteo archive API (Wunderground uyumlu daily max)
2. **Yedek:** METAR saatlik ölçüm → günlük max

Her iki kaynak da `record_settlement_source()` ile audit'e yazılır (Faz 6b). Uyumsuzluk ≥1°C bucket farkı varsa uyarı log'lanır.

---

## 5. Risk Yönetimi & Sabitler

`bot/scanner.py`:
```python
SHARES       = 10          # paper per-trade
MIN_PRICE    = 0.05
MAX_PRICE    = 0.40
MIN_MODE_PCT = 30
MIN_EDGE     = 0.05
MIN_BIAS_TRADES     = 8
MAX_BIAS_CORRECTION = 2    # bias tavan ±°C
MID_RANGE_SKIP_LOW  = 50   # Faz 5
MID_RANGE_SKIP_HIGH = 80
MIN_SIGNAL_SCORE    = 55   # Faz 5
STATION_SKILL_PAUSE = {"lfpg", "ltac", "limc"}
STATION_MAX_PRICE   = {"lfpg": 0.18}
```

`bot/trader.py`:
```python
LIVE_SHARES        = 5      # live per-trade (paper'ın yarısı)
AUTO_SELL_PRICE    = 0.998  # tick'e snap olur
AUTO_SELL_FALLBACK = 0.99
AUTO_SELL_MIN_EDGE = 0.02   # fill çok yüksekse SELL atlanır
```

---

## 6. Model Stack (6 Model, 139 Üye)

| Model | Tip | Üye | Statik Weight | Not |
|---|---|---|---|---|
| ICON | det + ens | 40 | 1.8 | En iyi D+1 MAE (1.08) |
| ECMWF | det + ens | 51 | 1.5 | İkinci en iyi (1.20) |
| **AIFS** | **AI** | det | 1.6 | **Faz 6d — ECMWF AI-forecast (2025)** |
| MeteoFrance | det + ens | 35 | 0.9 | — |
| GFS | det + ens | 21 | 1.0 | — |
| UKMO | det | — | 0.5 | LTFM'de MAE 3.22°C → düşük weight |

**Dinamik weights (Faz 4):** `bot/dynamic_weights.py`, son 30g istasyon×model RMSE → `1/(rmse+ε)` normalize. `model_forecasts` tablosunda ≥10 örnek varsa statik yerine dinamik kullanılır.

---

## 7. Faz Tarihçesi

| Faz | Ne eklendi | Beklenen etki |
|---|---|---|
| **1** | SQLite altyapısı (WAL, model_forecasts, audit tabloları) | JSON → DB hibrit; geçmiş analiz |
| **2** | Bimodal detection + bootstrap CI + dinamik CALIB | Kırılgan consensus'u reddet |
| **3** | Kalman bias filtresi + signal_score kompozit (0-100) | Mevsim kaymasına adapte bias |
| **4** | İstasyon×model dinamik ağırlık (rolling RMSE) | LTFM UKMO gibi outlier'ı otomatik sustur |
| **6a** | Portföy VaR (Gaussian copula + Cholesky Monte-Carlo) | Korelasyonlu risk ölçümü |
| **6b** | Çok kaynaklı settlement audit (OM + METAR) | Uyumsuzluk tespiti (LFPG +1.9°C sorunu) |
| **6c** | Kalibrasyon dashboard (Brier skill + reliability) | `/api/calibration` ve panel |
| **6d** | ECMWF AIFS 6. model olarak | AI ensemble entegrasyonu |
| **auto-sell** | 99.8¢ fill-sonrası SELL | Nakit döngüsü 48sa → anında |
| **crash-tests** | 11 grup × 41 crash test | Bot resilience |
| **Faz 5** | Kalibrasyon-odaklı filtre üçlüsü | Skill −0.36 → +0.05..+0.15 hedefi |
| **Faz 7** | SQLite-first yazım + settlement delta + dinamik size + VaR gate + Bayes prior + station pause DB + AIFS member validation | Mimari sağlamlaştırma: tek-kaynak doğruluk, kaynaklar arası sapma öğrenme, sinyal→boyut, tail-risk gate |

---

## 7b. Faz 7 Özeti (2026-04-23)

"Current state report" üzerine hayata geçirilen iyileştirmeler:

| Madde | Dosya | Ne değişti |
|---|---|---|
| SQLite birincil | `bot/db.py`, `trader.py`, `scanner.py` | `write_paper_trades_list()` + `write_live_trades_list()` + `rebuild_json_from_db()`. `save_*()` önce DB, sonra JSON yedek |
| Settlement delta | `bot/settlement_delta.py` (YENİ) | WU ↔ Open-Meteo rolling 60g medyan delta (proxy: METAR-OM). `apply_delta()` `scan_date` içinde Kalman bias sonrası `top_pick`'e eklenir |
| Dinamik size | `bot/position_sizing.py` (YENİ) | `signal_score` → SHARES çarpanı: Premium 1.5x, Strong 1.2x, Moderate 1.0x |
| VaR gate | `bot/trader.py::place_limit_order` | Hipotetik portföyü simüle et; `var_95 < -1.5×MAX_DAILY` ise emri bloke |
| Station pause DB | `bot/db.py::station_status` tablosu | `should_pause_station()` önce DB'den okur, yoksa statik set fallback |
| Bayes cold-start | `bot/dynamic_weights.py` | `posterior = (n·observed + k·prior)/(n+k)` — az veride model-özel prior ile shrinkage |
| AIFS üye valid. | `main.py` | Model üye sayısı beklenen %80'in altındaysa uyarı log |
| Stray skill | repo kökü | `web-designer.skill` silindi |

**Rapordaki 2 yanlış iddia (zaten yapılmış):**
- §2.2 "CALIB_STD_FACTOR statik" — Aslında `dynamic_calib_factor(horizon, spread)` (main.py:105) zaten dinamik.
- §2.5 "Bimodal trades yine de girer" — Scanner `peak_sep > BIMODAL_MAX_SEPARATION=1` olan trade'leri zaten pas geçiyor.

**Ertelenen (ayrı iş):**
- 90 günlük backtest koşumu (geçmiş tur "0-sonuç" cevap vermişti; datetime window hatası ihtimali araştırılmalı)
- NO-trade özelliği (scanner tek YES tarafını değerlendiriyor; NO satın alma ayrı bir branch)

---

## 8. Faz 5 Tanısı & Rationale (2026-04-23)

**Bulgu:** `/api/calibration` 90g veride:
- n=131, Brier=0.2709, Brier_ref=0.1993 → **skill = −0.36**
- Mid-range bandı (mode_pct ∈ [50, 70)): 51/131 trade (%39), gap ≈ **−0.39**
- Bu trade'lerin gerçek win oranı %20 (beklenen ~%58) — sistematik over-confidence
- Düşük güven bandı [30, 50): n=69, gap ≤ 0.07 → kalibre
- Per-station: yalnızca **efhk** (Helsinki) karlı (skill +0.28); lfpg (−1.96), ltac (−1.71), limc (−0.70) en kötüler

**Fix üçlüsü:**
1. `[50, 80)` mid-range → hard-skip
2. `{lfpg, ltac, limc}` → istasyon pause
3. `MIN_SIGNAL_SCORE=55` → kompozit gate'i aktive et

**Beklenen:** ~%42 hacim düşüşü, skill pozitife dönüş.

**Ertelenen (ayrı PR):** Platt scaling / isotonic regression ile mode_pct → kalibre olasılık dönüşümü. Bu yapılınca mid-range skip kaldırılabilir.

---

## 9. Kritik Endpoint'ler & Dashboard

Dashboard (`static/index.html`) 4 collapsible analitik paneli içerir:

| Panel | Endpoint | Ne gösterir |
|---|---|---|
| 📊 Kalibrasyon | `/api/calibration?days=90` | Brier, skill, reliability bin, per-station |
| 📈 Portföy VaR | `/api/portfolio/var?sims=10000` | E[P&L], VaR 95/99, corr |
| 🔍 Settlement Audit | `/api/settlement-audit` | Open-Meteo vs METAR uyumsuzluk |
| 💼 Canlı İşlemler | `/api/live-trades` | Auto-sell durumu, açık pozisyon |

Her panel **lazy-load** (ilk açılışta fetch).

---

## 10. Operasyon — Cron

```cron
# /etc/cron.d/weather  (VPS)
0 4,10,16,22 * * *  cd /root/weather && python3 bot/scanner.py scan --live
0 11 * * *          cd /root/weather && python3 bot/scanner.py settle
5 11 * * *          cd /root/weather && python3 bot/trader.py settle
15 11 * * *         cd /root/weather && python3 bot/trader.py redeem
*/30 * * * *        cd /root/weather && python3 bot/trader.py check-fills
0 4,8,12,16,20 * *  cd /root/weather && python3 bot/trader.py cancel-stale
```

`check-fills` içinde BUY fill tespit edilirse `place_auto_sell()` hook'u tetiklenir.

---

## 11. Deploy — Git + SSH

**KRİTİK KURAL:** VPS'te doğrudan dosya değiştirme. Akış:

```bash
# Mac'te
git add -p
git commit -m "..."
git push origin main

# VPS'e deploy
ssh root@135.181.206.109 "cd /root/weather && ./deploy.sh"
```

`deploy.sh` iki koruyucu kontrole sahip:
- Uncommitted değişiklik varsa → abort
- Push edilmemiş commit varsa → abort
- `git pull` → test suite → `systemctl restart weather`

---

## 12. Test Süiti (291/291)

```bash
python3 tests/test_weather_bot.py   # 250 birim + entegrasyon testi
python3 tests/test_crash.py         # 41 crash/defensive test
```

**TEST numaraları:**
- 1–24: Temel bucket, settle, blend, bias
- 25: SQLite altyapısı (Faz 1)
- 26: Bimodal + CI + CALIB (Faz 2)
- 27: Kalman bias + signal score (Faz 3)
- 28: Dinamik weights (Faz 4)
- 29: Portföy VaR (Faz 6a)
- 30: Çok-kaynaklı settlement audit (Faz 6b)
- 31: Kalibrasyon dashboard (Faz 6c)
- 32: ECMWF AIFS entegrasyonu (Faz 6d)
- 33: Otomatik satış (99.8¢ post-fill)
- 34: Kalibrasyon-odaklı filtreler (Faz 5)
- **35: SQLite-first + settlement delta + dinamik size (Faz 7) ← yeni**

**Crash test grupları (test_crash.py):**
JSON corruption · ekstrem sıcaklıklar · DB hataları · kalibrasyon edge · VaR (non-PSD, zero-var) · settlement (None, UPSERT, corrupt DB) · auto-sell (empty, timeout) · concurrency (parallel JSON write, WAL read) · API integrity · env/deploy · bucket defensive.

---

## 13. Dosya Yapısı (Güncel)

```
/root/weather/
├── main.py                      FastAPI backend
├── deploy.sh                    VPS deploy (git pull + test + restart)
├── BOT.md                       Bu kılavuz
├── CLAUDE.md                    Kısa proje notları (Claude için)
├── static/
│   └── index.html              Dashboard (4 analitik panel)
├── bot/
│   ├── scanner.py              Scan + settle
│   ├── trader.py               Live CLOB + auto-sell
│   ├── kalman.py               Adaptif bias (Faz 3)
│   ├── signal_score.py         Kompozit kalite
│   ├── dynamic_weights.py      Rolling RMSE (Faz 4)
│   ├── portfolio_var.py        Cholesky Monte-Carlo
│   ├── calibration.py          Brier/reliability
│   ├── db.py                   SQLite WAL + tablolar
│   ├── paper_trades.json       Paper geçmişi
│   ├── live_trades.json        Live geçmişi (auto-sell dahil)
│   └── weather.db              SQLite (model_forecasts, audit, weights)
├── backtest/
│   └── engine.py               Geçmiş ∪ filtre analizi
└── tests/
    ├── test_weather_bot.py     239 birim/entegrasyon test
    └── test_crash.py           41 resilience test
```

---

## 14. Günlük Operasyonel Checklist

- [ ] Dashboard aç → Kalibrasyon paneli → skill > 0 mı?
- [ ] Canlı İşlemler paneli → auto-sell emirlerinin durumu?
- [ ] Settlement Audit → OM/METAR uyumsuzluk var mı?
- [ ] `journalctl -u weather -f` → cron hataları
- [ ] `tail -f /root/deploy.log` → son deploy sağlıklı mı?

---

## 15. Sonraki Adımlar

- [ ] **Platt/isotonic kalibrasyon** → mid-range skip'i kaldır, gerçek kalibre olasılık kullan
- [ ] Bimodal trade performansını ayrıştır (is_bimodal=1 vs 0), yeterli veriyle hard-skip düşün
- [ ] Dinamik STATION_SKILL_PAUSE (30g rolling skill < −0.5 → otomatik ekle/kaldır)
- [ ] Sinyal skoru kalibrasyonu: gerçek P&L ile korelasyon + bin bazlı win-rate
- [ ] Per-bucket liquidity check (thin bucket'tan kaçın)
- [ ] Discord/Telegram günlük bildirim (yeni trade + settled + P&L)

---

## 16. Referans: Commit Tarihçesi (Son 6)

```
3f1f685 faz5: kalibrasyon-odaklı scanner filtreleri (skill -0.36 → +)
092576b dashboard: analitik panel (Kalibrasyon/VaR/Audit/Live) + crash test süiti
c6d7cc3 auto-sell: fill sonrası 99.8¢ SELL limit — nakit döngüsü hızlandırma
6a2a15e ecmwf-aifs: 6. model olarak AIFS entegrasyonu (deterministik + ensemble)
f44575d faz6c: kalibrasyon dashboard — Brier + reliability + sharpness
209613f faz6b: çok-kaynaklı settlement audit + uyumsuzluk tespiti
```

---

## 17. Hızlı Referans — Komutlar

```bash
# Lokal test
python3 tests/test_weather_bot.py        # 239 test
python3 tests/test_crash.py              # 41 crash test

# Manuel scan (paper)
python3 bot/scanner.py scan

# Manuel scan (paper + live)
python3 bot/scanner.py scan --live

# Manuel settle
python3 bot/scanner.py settle && python3 bot/trader.py settle

# Retroaktif auto-sell (mevcut filled pozisyonlar için)
python3 bot/trader.py auto-sell            # 0.998 default
python3 bot/trader.py auto-sell 0.995      # özel fiyat

# Canlı kalibrasyon
curl -s http://localhost:8001/api/calibration | jq '.overall'

# VPS deploy
ssh root@135.181.206.109 "cd /root/weather && ./deploy.sh"
```

---

*Bu dosya Claude Code tarafından güncellenir. Değişiklikler git'ten izlenir, VPS'e deploy.sh ile iletilir.*
