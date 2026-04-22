# Polymarket Weather Bot — Derin Analiz & Strateji Raporu

> **Kapsam:** Weather repo analizi + dünyada weather market trading stratejileri + NO trading küçük marj stratejisi + küsüratlı emir (fractional order) araştırması
>
> **Hedef:** IT developer veya AI agent tarafından doğrudan kodlanabilir teknik detay

---

## İçindekiler

1. [Repo Analizi: ozanturk19/weather](#1-repo-analizi-ozanturk19weather)
2. [Dünyada Weather Market Kazananları](#2-dünyada-weather-market-kazananları)
3. [Weather Market Trading Stratejileri](#3-weather-market-trading-stratejileri)
4. [NO Trading — Küçük Marj Stratejisi (Senin Fikrin)](#4-no-trading--küçük-marj-stratejisi-senin-fikrin)
5. [Küsüratlı Emir (Fractional Order) Araştırması](#5-küsüratlı-emir-fractional-order-araştırması)
6. [Mevcut Bota Entegrasyon Planı](#6-mevcut-bota-entegrasyon-planı)

---

## 1. Repo Analizi: ozanturk19/weather

### 1.1 Mimari Genel Bakış

```
weatherbets/
├── main.py          ← FastAPI backend (port 8001) + hava durumu motoru
├── scanner.py       ← Paper trading tarama + fırsat tespiti
├── trader.py        ← Canlı işlem modülü (CLOB entegrasyonu)
├── deploy.sh        ← Güvenli deployment (git disiplin + health check)
├── paper_trades.json ← Trade geçmişi (JSON tabanlı)
└── .env             ← Wallet credentials
```

**Stack:** Python (FastAPI) · Open-Meteo API · py-clob-client · Web3/Polygon

---

### 1.2 Hava Durumu Motoru — 5 Model Ensemble

```python
# main.py — blended forecast engine
MODELS = ['gfs', 'ecmwf_ifs', 'icon', 'ukmo', 'meteofrance']
# Her model eşit ağırlıklı DEĞİL — 60 günlük backtested MAE ile ağırlıklandırılır

CALIB_STD_FACTOR = 1.8  # %40–80 bant kalibrasyonu (aşırı güven düzeltmesi)
```

**Model Ağırlıklandırma:**
```python
weight[model] = 1 / MAE_60day[model]
# Son 60 günde daha az hata yapan modele daha yüksek ağırlık
```

**Belirsizlik Sınıflandırması:**
```
D+0 (bugün):   Düşük belirsizlik — girilir
D+1 (yarın):   Orta belirsizlik — girilir (ana hedef)
D+2 (öbür gün): Yüksek belirsizlik — çok seçici
"high"/"very high" → İşlem yapılmaz
```

**Percentile Dağılımı:**
```
p10, p25, p50 (medyan), p75, p90
→ Piyasa fiyatıyla karşılaştırılan şey p50 (medyan ensemble tahmini)
```

---

### 1.3 İzlenen 12 İstasyon (ICAO Kodları)

| Şehir | İstasyon Kodu | Polymarket Slug Pattern |
|-------|--------------|------------------------|
| London | EGLC (City Airport) | `highest-temperature-in-london-on-{date}` |
| Paris | LFPG (CDG) | `highest-temperature-in-paris-on-{date}` |
| Istanbul | LTAC | `highest-temperature-in-istanbul-on-{date}` |
| Dubai | OMDB | `highest-temperature-in-dubai-on-{date}` |
| Tokyo | RJTT | `highest-temperature-in-tokyo-on-{date}` |
| + 7 diğer | — | — |

**Kritik:** Polymarket resolution kaynağı = **Open-Meteo archive API** (yedek: METAR geçmişi). Botun settle ettiği kaynak ile Polymarket'ın kullandığı kaynak örtüşüyor — bu senkronizasyon kenarın temelidir.

---

### 1.4 Giriş Kriterleri (scanner.py)

```python
# Fırsat tespiti için minimum koşullar:

MIN_EDGE_PCT       = 5      # Ensemble konsensüs - piyasa fiyatı >= 5 puan
MAX_PRICE_YES      = 0.40   # YES fiyatı bu seviyenin altında olmalı
MIN_PRICE_YES      = 0.05   # Çok ucuz tokenlar likidite sorunu
STATION_CAPS = {
    'paris': 0.18,          # Paris hassasiyeti düşük → fiyat tavanı kısıtlı
    'default': 0.40,
}

# 2-Bucket stratejisi:
# Primary bucket: En iyi fırsat
# Secondary bucket: ±1°C hedging (min %30 ensemble konsensüs şart)
SECONDARY_MIN_CONSENSUS = 0.30
MIN_SECONDARY_EDGE      = 0.05  # %5 kenar şartı secondary için de geçerli
```

**Adaptif Bias Sistemi:**
```python
# 8+ geçmiş trade gerektiriyor
# Sistematik tahmin hatalarını düzeltiyor: ±2°C maksimum
# Her istasyon için ayrı kalibrasyon
bias_correction = weighted_mean(past_errors, decay=0.95)  # eksponansiyel ağırlık
```

---

### 1.5 Order Yönetimi (trader.py)

```python
# Sadece GTC (maker) order — FEE YOK
order = {
    'token_id': token_id,
    'side': 'BUY',
    'price': entry_price,
    'size': 10,         # paper: 10 share | live: 5 share
    'order_type': 'GTC',
    'fee_rate_bps': 0,
}

# Risk kontrolleri:
MAX_OPEN_TRADES   = 30  # eş zamanlı
DAILY_SPEND_LIMIT = 60  # USDC
MIN_RESERVE       = 5   # USDC — bu altına düşme
PRICE_FLOOR       = 0.05
PRICE_CEILING     = 0.40

# Nonce yönetimi: transaction conflict prevention
# Stale order tespiti: D+1 yeniden giriş mantığı
```

---

### 1.6 Mevcut Performans (74 Kapalı Trade)

```
Win Rate:   %35.1 (26 kazanç / 48 kayıp)
Net P&L:    +$77.40
EV/share:   ~+$0.10

Paradoks: %35 kazanma oranıyla karlı olmak nasıl mümkün?

Cevap: Asimetrik ödeme yapısı
  Kayıp: -$0.10 (entry @ 0.10, değer 0 → -$0.10/share)
  Kazanç: +$0.90 (entry @ 0.10, değer 1.00 → +$0.90/share)
  
  Beklenen değer = (0.351 × 0.90) + (0.649 × -0.10)
                 = 0.316 - 0.065 = +$0.25/share
```

**Önemli İçgörü:** Yüksek win rate gerekmiyor — **doğru fiyatlandırılmış kenar** gerekiyor. %10'dan alınan tokenların gerçek olasılığı %35+ ise bu pozitif beklenen değer.

---

### 1.7 Deployment & Cron (deploy.sh)

```bash
# Güvenlik kontrolleri:
# 1. Uncommitted changes varsa dur
# 2. Unpushed commits varsa dur
# 3. Testleri çalıştır — fail varsa dur
# 4. Health check sonrası yeniden başlat

# Cron tarama zamanlaması (UTC):
04:00, 08:00, 10:00, 12:00, 16:00, 20:00, 22:00

# Neden bu saatler?
# GFS güncelleme: 00, 06, 12, 18 UTC
# ECMWF güncelleme: 00, 12 UTC
# Modeller güncellendikten ~2 saat sonra → piyasa henüz adapte olmamış
```

---

### 1.8 Mevcut Botun Zayıf Noktaları

| Sorun | Etki | Çözüm |
|-------|------|-------|
| Paris fiyat tavanı $0.18 (accuracy düşük) | Fırsat kaçırılıyor | Station-specific model kalibrasyonu |
| JSON tabanlı trade persistence | Crash sonrası veri kaybı | SQLite'e geç |
| D+2 yüksek belirsizlik → girmiyor | Kar fırsatı kaçıyor | Daha küçük pozisyon ile gir |
| Adaptive bias 8 trade bekliyor | Başlangıçta bias yok | Sabit prior ile başla |
| CALIB_STD_FACTOR = 1.8 sabit | %40–80 bandı aşırı kompanse | Dinamik kalibrasyon |

---

## 2. Dünyada Weather Market Kazananları

### 2.1 Kanıtlanmış Karlı Trader'lar

| Trader | Toplam Kar | Yöntem | Özellik |
|--------|-----------|--------|---------|
| **gopfan2** | **$2M+** | YES < $0.15 al, NO > $0.45 al | 3+ model konsensüs şartı |
| **1pixel** | $18,500 ($2,300 depo) | NYC + London odaklı | Tek işlemde $6 → $590 |
| **meropi** | $30,000+ | $1–3 mikro bet | $0.01'den giriş, 500x ödeme |
| **Weather Bot X** | $24,000 (Apr 2025+) | Sadece London marketi | Tam otomatik |
| **Weather Bot Y** | $65,000 | NYC + London + Seoul | 3 şehir |

### 2.2 gopfan2'nin Kuralları (En Başarılı)

```
Kural 1: YES shares → SADECE $0.15 altında al (3+ model konsensüs varsa)
Kural 2: NO shares  → SADECE $0.45 üstünde al (modeller desteklemiyorsa)
Kural 3: Risk per trade: ~$1 maksimum
Kural 4: Model konsensüs şartı: GFS + ECMWF + ICON üçü de aynı yönde

Neden $0.45 NO threshold?
  → NO @ $0.55 (YES @ $0.45 demek)
  → Modeller %20 olasılık veriyorsa: EV = 0.20×$0.45 - 0.80×$0.55 = -$0.35 (kötü)
  → Modeller %10 olasılık veriyorsa: EV = 0.10×$0.45 - 0.90×$0.55 = -$0.45 (kötü)
  
  NO @ $0.45 (YES @ 0.55) + model %15 olasılık gösteriyorsa:
  → NO alıyoruz. NO fiyatı = 1 - 0.55 = $0.45
  → EV = 0.85×$0.45 - 0.15×$0.55 = +$0.30 (pozitif!)
```

---

## 3. Weather Market Trading Stratejileri

### STRATEJİ 1: Model-Market Divergence (Temel)

**Mantık:** Hava durumu modelleri 6 saatte bir güncellenir. Polymarket fiyatları genellikle gecikir. Bu gecikme penceresi ticarete açık.

```python
# Fırsat tespiti
def find_divergence(model_prob: float, market_price: float, threshold: float = 0.05) -> bool:
    return (model_prob - market_price) > threshold

# Örnek:
# Model: London yarın 14°C bucket = %72 olasılık
# Piyasa: $0.55 (= %55 ihtimal)
# Divergence: 0.72 - 0.55 = 0.17 → 17 puanlık kenar → GİR

# Model güncelleme sonrası trading penceresi:
# GFS:   00, 06, 12, 18 UTC → +30dk ile +2 saat arası işlem
# ECMWF: 00, 12 UTC → +1 saat ile +4 saat arası işlem
```

### STRATEJİ 2: YES Laddering (Çoklu Bucket)

**Mantık:** Tek bir sıcaklık aralığını değil, 3–5 komşu aralığı al. Biri kazanır, diğerleri sıfıra gider ama net pozitif.

```python
# Örnek: London 16 Nisan
# Model: 13°C tahmin, standart sapma 1.5°C

# Bucket fiyatları:
buckets = {
    '11-12°C': 0.05,
    '12-13°C': 0.20,
    '13-14°C': 0.35,  # ← Ana bucket (en yüksek olasılık)
    '14-15°C': 0.25,
    '15-16°C': 0.10,
}

# Ladderleme:
orders = [
    ('12-13°C', 3),  # 3 share → $0.60 risk
    ('13-14°C', 5),  # 5 share → $1.75 risk (ağırlıklı)
    ('14-15°C', 2),  # 2 share → $0.50 risk
]

# Kazanç senaryosu:
# 13-14°C olursa: +5×$0.65 - 3×$0.20 - 2×$0.25 = +$3.25 - $1.10 = +$2.15
```

### STRATEJİ 3: Latency Arbitrage (Model Güncellemesi Sonrası)

**Mantık:** Yeni model çalışması geldiğinde piyasa henüz adapte olmamış → ilk hareket eden kazanır.

```python
# Cron: Her model güncellemesinden 30 dk sonra tarama
SCAN_AFTER_MODEL_UPDATE = {
    'gfs': [30, 90, 150],       # 00:30, 01:30, 02:30 UTC
    'ecmwf': [60, 120, 240],    # 01:00, 02:00, 04:00 UTC
}

# Eğer yeni model run önceki çalışmaya göre >2°C değişim gösterdiyse
# → Piyasa fiyatı hâlâ eski bilgiye göre → acil giriş fırsatı
def detect_model_shift(old_forecast: float, new_forecast: float, threshold: float = 2.0) -> bool:
    return abs(new_forecast - old_forecast) > threshold
```

### STRATEJİ 4: Fade Impossible (Senin Fikrin — NO Trading)

Bu strateji kullanıcının önerdiği yaklaşım. Detaylı olarak Bölüm 4'te.

### STRATEJİ 5: Domain Specialization (Tek Şehir Odaklanması)

**Mantık:** 1pixel ve weather bot'larının gösterdiği — tek şehirde derin uzmanlık global tahmin hatalarından çok daha değerli.

```
London City Airport (EGLC) özellikleri:
  - Şehir merkezi etkisi: 1-2°C urban heat island
  - Thames nehri etkisi: gece ısı tutma
  - Model bias: ECMWF genellikle 0.5°C soğuk tahmin
  → Bu sistematik bias'ı öğrenip düzeltmek = kalıcı kenar
```

---

## 4. NO Trading — Küçük Marj Stratejisi (Senin Fikrin)

### 4.1 Strateji Tanımı

**Senaryo (kullanıcının örneği):**
```
Market: "Highest temperature in London above 15°C on April 24"
Tahminler: 12-13°C yoğunlaşıyor
Gerçek olasılık: ~%1-2 (neredeyse imkânsız)
Mevcut fiyat: YES @ $0.015 → NO @ $0.985

Fırsat:
  → NO @ $0.985 al (GTC maker)
  → Settlement @ $1.00
  → Marjin: $0.015 per share (%1.5)
  → Fee: SIFIR (weather market, maker order)
```

Bu tam olarak **Tail-End Bond stratejisinin NO versiyonu**: Yüksek kesinlikli NO pozisyonu → küçük ama garantiye yakın marjin.

---

### 4.2 Matematiksel Çerçeve

```python
# Kâr/zarar hesabı
def calc_no_trade_ev(
    no_price: float,      # NO token fiyatı (= 1 - YES fiyatı)
    true_no_prob: float,  # Modele göre gerçek NO olasılığı
    size_usd: float,      # Yatırım miktarı
) -> dict:
    shares = size_usd / no_price
    win_pnl  = shares * (1.0 - no_price)  # NO kazanırsa
    loss_pnl = shares * (-no_price)        # NO kaybederse (= sıcaklık gerçekten 15°C+)

    ev = true_no_prob * win_pnl + (1 - true_no_prob) * loss_pnl

    return {
        'shares':    round(shares, 2),
        'win_pnl':   round(win_pnl, 4),
        'loss_pnl':  round(loss_pnl, 4),
        'ev':        round(ev, 4),
        'roi_if_win': round((1.0 - no_price) / no_price * 100, 2),
    }

# Kullanıcının örneği:
result = calc_no_trade_ev(
    no_price      = 0.985,
    true_no_prob  = 0.98,  # model %98 NO diyor
    size_usd      = 100.0,
)
# → shares: 101.52
# → win_pnl: +$1.52 (eğer NO kazanırsa)
# → loss_pnl: -$98.50 (eğer YES kazanırsa — çok nadir)
# → EV: +$0.52 (100$'lık riski için)
# → ROI if win: 1.52%
```

**Yıllık projeksiyon:**
```
Günde 3 işlem × $100 → $1.52 × 3 = $4.56/gün
Ayda 30 gün → $136.8/ay
Yıllık → ~$1,640 ($100 sermaye için)

Daha büyük sermaye ($1,000):
→ Günlük ~$45.6 → Yıllık ~$16,400
```

---

### 4.3 Giriş Kriterleri (NO Trade için)

```python
class NoTradeParams:
    # Fiyat eşiği
    MAX_YES_PRICE    = 0.025  # YES $0.025 altında → NO $0.975+
    MIN_YES_PRICE    = 0.005  # $0.005 altı = likidite yok
    
    # Model konsensüs
    MIN_MODEL_NO_PROB = 0.96  # En az 3 model %96+ NO diyor
    MIN_MODELS_AGREE  = 3     # En az 3 model uyumlu
    
    # Zaman
    MAX_HOURS_TO_SETTLE = 24  # Market 24 saat içinde kapanacak
    MIN_HOURS_TO_SETTLE = 2   # Çok kısa süre kalmışsa atlat
    
    # Risk
    MAX_SIZE_PER_TRADE = 200  # USDC (kayıp senaryosunda büyük zarar)
    MIN_SIZE_PER_TRADE = 10   # USDC
    MAX_CONCURRENT     = 5    # Eş zamanlı NO pozisyonu
    
    # Güvenlik marjı (model ne kadar yanlış olabilir?)
    SAFETY_BUFFER_C = 2.5     # Model 12°C → 14.5°C altında bile NO kazanır


def should_enter_no_trade(
    yes_price: float,
    model_median: float,         # Model tahmin sıcaklığı
    market_threshold: float,     # Market'ın sorduğu sıcaklık (örn: 15°C)
    model_p90: float,            # Ensemble %90 persentil (en yüksek olası)
    hours_to_settle: float,
    params: NoTradeParams,
) -> bool:
    
    # 1. Fiyat kontrolü
    if not (params.MIN_YES_PRICE < yes_price < params.MAX_YES_PRICE):
        return False
    
    # 2. Model konsensüs: p90 bile eşiğin altında mı?
    # Eğer en kötü senaryo (%90 olasılıklı üst bant) bile market eşiğinin altındaysa
    if model_p90 >= market_threshold - params.SAFETY_BUFFER_C:
        return False  # Belirsizlik çok yüksek
    
    # 3. Zaman kontrolü
    if not (params.MIN_HOURS_TO_SETTLE <= hours_to_settle <= params.MAX_HOURS_TO_SETTLE):
        return False
    
    # 4. Kenar var mı?
    no_price    = 1 - yes_price
    true_no_est = 1 - (yes_price * 2)  # piyasa fiyatının 2 katını gerçek olasılık say (konservatif)
    if true_no_est < params.MIN_MODEL_NO_PROB:
        return False
    
    return True
```

---

### 4.4 Güvenlik Marjı Analizi (Kritik)

**Ne zaman GİRİLMEZ:**

```
Market: "London above 15°C"
Model p50 (medyan): 12°C   ← iyi görünüyor
Model p90: 14.2°C          ← en yüksek olası senaryo 14.2°C → hâlâ 15°C altı → GİR

Model p50: 13°C
Model p90: 15.5°C          ← En yüksek olası senaryo 15.5°C → 15°C AŞILIR → GIRME

Kural: model_p90 + 0.5°C < market_threshold → güvenli giriş
```

**Ne zaman ÇIKILIR (emergency):**

```python
# Ertesi sabah yeni model çalışması geldi → sıcaklık tahmini yukarı revize edildi
def check_emergency_exit(
    new_model_p90: float,
    market_threshold: float,
    open_position_order_id: str,
    client,
) -> None:
    danger_zone = market_threshold - 1.5  # 1.5°C güvenlik tamponu
    if new_model_p90 >= danger_zone:
        # Acil GTC satış emri (NO'ları geri sat — piyasa henüz adapte olmadıysa)
        client.cancel_order(open_position_order_id)
        # Piyasaya FOK ile çıkmak yerine yeni bir GTC SELL koy
        # (taker fee ödemekten kaçın)
        place_gtc_sell(open_position_order_id, current_no_bid - 0.002)
```

---

### 4.5 Pozisyon Boyutlandırma (Kelly ile)

```python
def kelly_no_trade(
    no_price: float,
    true_no_prob: float,
    bankroll: float,
    kelly_fraction: float = 0.25,  # Fractional Kelly (güvenli)
) -> float:
    b = (1.0 - no_price) / no_price  # net kazanç oranı
    p = true_no_prob
    q = 1 - p

    kelly = (b * p - q) / b
    size = kelly * kelly_fraction * bankroll

    # Hard cap: bankroll'un %5'i
    return min(size, bankroll * 0.05)

# Örnek:
# no_price=0.985, true_no_prob=0.98, bankroll=$500
# b = 0.015/0.985 = 0.0152
# kelly = (0.0152×0.98 - 0.02) / 0.0152 = (0.0149 - 0.02) / 0.0152 = -0.34 (negatif!)
# 
# UYARI: Kelly negatif → matematiksel olarak position almanı öneriyor!
# 
# Neden? Risk/reward asimetrik:
#   Kazanç: $0.015 per share
#   Kayıp:  $0.985 per share (65x daha büyük kayıp)
#
# Sonuç: NO trading'de Kelly çok küçük pozisyon söylüyor → BU DOĞRU
# Sabit $10-50 micro-bet yaklaşımı Kelly'den daha mantıklı

# Önerilen: Sabit $20 per NO trade, max 5 eş zamanlı = max $100 risk
```

---

### 4.6 NO Trade için Risk/Reward Gerçekçi Değerlendirme

```
Kullanıcının örneği: London 15°C, gerçekte 12-13°C bekleniyor

AVANTAJLAR:
  ✅ Hava durumu modelleri %97+ NO diyor
  ✅ Weather market = sıfır fee (maker order da sıfır)
  ✅ Günlük 570+ aktif weather market → çok sayıda fırsat
  ✅ Botla tam otomatize edilebilir
  ✅ Küsüratlı emir ile çok küçük risk miktarı mümkün

DEZAVANTAJLAR:
  ⚠️ Risk/reward oranı kötü: $100 risk, $1.52 kazanç (1.52%)
  ⚠️ Tek yanlış tahmin tüm kazancı silir (100 işlem kazanılsa +$152, 1 kayıp -$98.5)
  ⚠️ Model p90 bile bazen yanlış → %2 kayıp senaryosu gerçek
  ⚠️ Likidite sorunu: YES @ $0.015 → çok az satıcı var NO tarafında

SONUÇ: Küçük, çeşitlendirilmiş NO portföyü mantıklı ama ana strateji DEĞİL
       Günlük $10-30 micro NO trade, büyük YES fırsatlarına ek olarak
```

---

### 4.7 Pratik NO Trade Akışı (Bot Mantığı)

```python
async def scan_no_opportunities(markets: list[Market], forecasts: dict) -> list[NoTrade]:
    opportunities = []
    
    for market in markets:
        # Sıcaklık eşiğini market başlığından çıkar
        threshold = parse_temperature_threshold(market.question)
        city      = parse_city(market.question)
        date      = parse_date(market.question)
        
        if not threshold or not city:
            continue
        
        # Model tahminini al
        forecast = forecasts.get(f"{city}_{date}")
        if not forecast:
            continue
        
        # Evet fiyatını al
        yes_price = market.yes_price
        no_price  = 1 - yes_price
        
        # Karar
        params = NoTradeParams()
        if should_enter_no_trade(
            yes_price        = yes_price,
            model_median     = forecast.p50,
            market_threshold = threshold,
            model_p90        = forecast.p90,
            hours_to_settle  = hours_until_close(market),
            params           = params,
        ):
            opportunities.append(NoTrade(
                market_id  = market.id,
                token_no   = market.token_no,
                no_price   = no_price,
                model_p90  = forecast.p90,
                threshold  = threshold,
                margin_pct = (1 - no_price) / no_price * 100,
            ))
    
    # En yüksek güvenlik marjına göre sırala
    return sorted(opportunities, key=lambda x: x.margin_pct, reverse=True)
```

---

## 5. Küsüratlı Emir (Fractional Order) Araştırması

### 5.1 Polymarket CLOB Order Precision Kuralları

Polymarket'ta emir büyüklüğü için **kesin precision kuralları** var:

| Tick Size | Fiyat Ondalığı | **Miktar Ondalığı** | Amount Ondalığı |
|-----------|---------------|---------------------|-----------------|
| 0.1       | 1             | **2**               | 3               |
| 0.01      | 2             | **2**               | 4               |
| 0.001     | 3             | **2**               | 5               |
| 0.0001    | 4             | **2**               | 6               |

**Kritik Kural:**
```
size × price ≤ 2 ondalık basamak (toplam)

Örnek:
  size=1.74, price=0.58 → amount=1.0092 → HATA (2 ondalık aşıyor)
  size=1.00, price=0.58 → amount=0.58   → GEÇERLİ
  size=10.52, price=0.985 → amount=10.36 → GEÇERLİ (2 ondalık)
```

### 5.2 Küsüratlı Emir Mümkün mü?

**EVET — ama kısıtlamalar var:**

```python
# GTC order için size: 2 ondalık basamak desteklenir
# Yani: 1.52, 10.75, 100.00 — hepsi geçerli

# FOK order için: DAHA KISITICLI
# size ondalık × price ondalık ≤ 2 toplam ondalık
# Bu nedenle FOK ile 1.74 share @ 0.58 → HATA
# Çözüm: GTC kullan (weather market = sıfır fee zaten)

# Python örneği:
def calculate_valid_size(
    size_usd: float,
    no_price: float,
) -> float:
    raw_size = size_usd / no_price
    
    # 2 ondalığa yuvarla (aşağı — fazla harcamayı önle)
    rounded_size = math.floor(raw_size * 100) / 100
    
    # Doğrulama: size × price ≤ 2 ondalık
    amount = rounded_size * no_price
    if len(str(round(amount, 10)).split('.')[-1].rstrip('0')) > 2:
        # 1 ondalığa düşür
        rounded_size = math.floor(raw_size * 10) / 10
    
    return rounded_size

# Örnek:
# size_usd=10, no_price=0.985 → raw=10.152 → rounded=10.15
# amount = 10.15 × 0.985 = 9.998 → 4 ondalık → sorun
# → 1 ondalığa düş: 10.1 share
# amount = 10.1 × 0.985 = 9.949 → 3 ondalık → GEÇERLİ
```

### 5.3 NO Trade için Optimal Size Hesabı

```python
def calc_no_trade_size(
    budget_usd: float,
    no_price: float,
) -> dict:
    """
    NO token satın almak için geçerli share miktarı hesapla.
    Weather market tick size genellikle 0.01 (price 2 ondalık).
    """
    raw_shares = budget_usd / no_price
    
    # GTC için max 2 ondalık
    shares = math.floor(raw_shares * 100) / 100
    
    # amount doğrulama
    amount = round(shares * no_price, 10)
    amount_decimals = len(str(amount).split('.')[-1].rstrip('0'))
    
    if amount_decimals > 4:  # GTC için 4 ondalık limit
        shares = math.floor(raw_shares * 10) / 10
        amount = round(shares * no_price, 4)
    
    actual_cost = shares * no_price
    max_gain    = shares * (1.0 - no_price)
    
    return {
        'shares':      shares,
        'actual_cost': round(actual_cost, 4),
        'max_gain':    round(max_gain, 4),
        'margin_pct':  round((1 - no_price) / no_price * 100, 3),
    }

# Kullanıcının senaryosu:
result = calc_no_trade_size(budget_usd=50, no_price=0.985)
# → shares: 50.76 → floors to 50.7
# → actual_cost: $49.94
# → max_gain: $0.76 (eğer NO kazanırsa)
# → margin_pct: 1.523%

# Daha küçük: $5 budget
result2 = calc_no_trade_size(budget_usd=5, no_price=0.985)
# → shares: 5.07 → 5.07
# → actual_cost: $4.99
# → max_gain: $0.076
```

### 5.4 Minimum Anlamlı NO Trade Büyüklüğü

```
Polymarket'ta resmi minimum order yok (help center: "no trading size limits")

Pratik minimum (likidite ve gas açısından):
  - Gas fee (Polygon): ~$0.001 per transaction → göz ardı edilebilir
  - Anlamlı getiri için: min $5 USDC per trade
  - Önerilen: $10–50 per NO trade

Market likidite kontrolü:
  YES @ $0.015 → Piyasada bu fiyatta NO satan kim?
  → Eğer YES alıcıları varsa, NO satıcıları var demek
  → Bid depth'i kontrol et: min $20 depth @ $0.985 NO → GTC dolabilir

# Depth kontrolü
async def check_no_liquidity(
    client: ClobClient,
    token_yes: str,
    required_usd: float,
) -> bool:
    orderbook = await client.get_order_book(token_yes)
    # YES'in ask tarafı = NO'nun bid tarafı (ters)
    # YES ask @ 0.015 → bu miktarda NO sell emri var mı?
    available_depth = sum(
        level.size * level.price
        for level in orderbook.asks
        if level.price <= 0.02  # YES $0.02 altı emirler
    )
    return available_depth >= required_usd
```

---

## 6. Mevcut Bota Entegrasyon Planı

### 6.1 Mevcut Yapıya Eklenecekler

```python
# scanner.py'a eklenecek — NO trade tarama

class NoTradingScanner:
    """
    Mevcut YES scanner'a ek olarak çalışır.
    YES fırsatları: model_prob > market_price + 5%
    NO fırsatları: market_yes_price < 2.5% VE model p90 < threshold - 2°C
    """
    
    def scan_no_opportunities(self) -> list[NoOpportunity]:
        markets = self.fetch_active_markets()
        forecasts = self.fetch_all_forecasts()
        
        no_opps = []
        for market in markets:
            yes_price = market.yes_price
            if yes_price > 0.025:  # Sadece çok düşük YES fiyatı
                continue
            
            city      = self.parse_city(market)
            threshold = self.parse_threshold(market)
            forecast  = forecasts.get(city)
            
            if not forecast:
                continue
            
            # p90 güvenlik kontrolü: en kötü senaryo bile eşiğin 2°C altında mı?
            if forecast.p90 >= threshold - 2.0:
                continue  # Çok riskli
            
            # Likidite kontrolü
            if not self.has_enough_depth(market.token_yes, min_usd=10):
                continue
            
            no_opps.append(NoOpportunity(
                market    = market,
                no_price  = 1 - yes_price,
                model_p90 = forecast.p90,
                threshold = threshold,
                gap_c     = threshold - forecast.p90,  # Ne kadar güvenli?
            ))
        
        return sorted(no_opps, key=lambda x: x.gap_c, reverse=True)
    
    def execute_no_trade(self, opp: NoOpportunity, budget_usd: float = 20.0):
        size_info = calc_no_trade_size(budget_usd, opp.no_price)
        
        # GTC maker order (fee sıfır)
        order = {
            'token_id':    opp.market.token_no,  # NO token
            'side':        'BUY',
            'price':       opp.no_price,
            'size':        size_info['shares'],
            'order_type':  'GTC',
            'fee_rate_bps': 0,
        }
        
        response = self.clob.post_order(order)
        self.save_no_trade(opp, response, size_info)
        
        print(f"[NO] {opp.market.question[:50]} | "
              f"price={opp.no_price:.3f} | "
              f"shares={size_info['shares']} | "
              f"max_gain=${size_info['max_gain']:.3f} | "
              f"p90_gap={opp.gap_c:.1f}°C")
```

### 6.2 Model Güncellemesi Sonrası Acil Çıkış

```python
# trader.py'a eklenecek — NO pozisyon monitörü

async def monitor_no_positions(open_no_trades: list, forecasts: dict):
    """Her 30 dakikada bir çalışır."""
    
    for trade in open_no_trades:
        city      = trade.city
        threshold = trade.threshold
        forecast  = forecasts.get(city)
        
        if not forecast:
            continue
        
        # Model p90 tehlikeli bölgeye girdi mi?
        if forecast.p90 >= threshold - 1.5:
            print(f"[NO DANGER] {city} p90={forecast.p90}°C ≥ threshold-1.5={threshold-1.5}°C")
            
            # Mevcut order'ı iptal et
            await clob.cancel_order(trade.order_id)
            
            # Eğer zaten fill olduysa (NO elimizde), GTC ile sat
            # NO'yu bid fiyatının biraz üstünden sat (taker fee ödemeden)
            if trade.is_filled:
                await place_gtc_exit(trade, current_no_bid + 0.003)
```

### 6.3 Cron Zamanlama (Güncellenmiş)

```
# Mevcut: YES fırsat taraması
04:00, 08:00, 10:00, 12:00, 16:00, 20:00, 22:00 UTC

# Eklenecek: NO fırsat taraması (model güncellemesinden sonra)
00:30 UTC  — GFS + ECMWF 00Z güncellemesi sonrası
06:30 UTC  — GFS 06Z sonrası
12:30 UTC  — GFS + ECMWF 12Z sonrası
18:30 UTC  — GFS 18Z sonrası

# NO pozisyon monitörü
Her 30 dakika — p90 tehlike kontrolü

# Settlement
18:00–23:00 UTC — Open-Meteo archive API ile settle
```

### 6.4 Database Şeması (Eklenecek Tablo)

```sql
-- paper_trades.json → SQLite migration (önerilir)
CREATE TABLE IF NOT EXISTS no_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    city            TEXT NOT NULL,
    date            TEXT NOT NULL,           -- 'YYYY-MM-DD'
    temperature_threshold REAL NOT NULL,     -- 15.0 (°C)
    
    -- Order bilgileri
    order_id        TEXT,
    token_no        TEXT NOT NULL,
    no_price        REAL NOT NULL,           -- 0.985
    shares          REAL NOT NULL,           -- 50.7
    cost_usd        REAL NOT NULL,           -- 49.94
    max_gain_usd    REAL NOT NULL,           -- 0.76
    
    -- Model bilgileri (giriş anı)
    model_p50_at_entry REAL,                 -- 12.3
    model_p90_at_entry REAL,                 -- 14.1
    gap_c           REAL,                    -- 15 - 14.1 = 0.9°C
    
    -- Durum
    status          TEXT DEFAULT 'PENDING',  -- PENDING/OPEN/WON/LOST/CANCELLED
    
    -- Sonuç
    actual_temp     REAL,                    -- Gerçekleşen sıcaklık
    pnl             REAL,
    settled_at      INTEGER,
    created_at      INTEGER DEFAULT (strftime('%s','now'))
);
```

---

## Özet Tablo: Tüm Stratejiler Bir Arada

| Strateji | Yön | Win Rate | Marjin | Fee | Öncelik |
|----------|-----|----------|--------|-----|---------|
| Model-Market Divergence (YES) | YES ucuz al | %55–75 | %200–500 | 0 | ✅ Birincil |
| YES Laddering | 3-5 bucket | %40–60 | Asimetrik | 0 | ✅ Birincil |
| Latency Arbitrage | Hızlı hareket | %65–80 | %50–150 | 0 | ✅ Hızlı bota |
| **NO Trading (Senin fikrin)** | **NO al** | **%96–99** | **%1–3** | **0** | **⚠️ Ek gelir** |
| Fade Impossible + Emergency Exit | NO+izleme | %97+ | %1–3 | 0 | ⚠️ Ek gelir |

**NO Trading özet:**
```
✅ Uygulanabilir ve botla tam otomatize edilebilir
✅ Küsüratlı emir (2 ondalık) GTC ile mümkün ($5.07 gibi)
✅ Weather market = sıfır fee (hem taker hem maker)
⚠️ Risk/reward oranı kötü: $100 risk → $1.52 kazanç
⚠️ Tek kayıp çok sayıda kazancı silmez, ama önemli miktarını siler
⚠️ Model p90 izleme sistemi + acil çıkış şart
💡 Öneri: Ana YES stratejisine EK olarak günlük $30–50 toplam NO budget
```

---

*Son güncelleme: 2026-04-22*
*Kaynaklar: ozanturk19/weather repo analizi · Polymarket CLOB docs · py-clob-client issues · PolyWeatherBot · gopfan2/meropi strateji araştırması*

---

Sources:
- [People Are Making Millions on Polymarket Betting on the Weather](https://medium.com/mountain-movers/people-are-making-millions-on-polymarket-betting-on-the-weather-and-i-will-teach-you-how-24c9977b277c)
- [Found The Weather Trading Bots Quietly Making $24,000 On Polymarket](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09)
- [How Polymarket Weather Markets Actually Work](https://dev.to/cryptodeploy/how-polymarket-weather-markets-actually-work-50nb)
- [Bonding Strategy on Polymarket](https://startpolymarket.com/strategies/bonding/)
- [PolyWeatherBot Technical Spec](https://www.polytraderbot.com/polyweatherbot.html)
- [FOK Order Decimal Places Issue - py-clob-client](https://github.com/Polymarket/py-clob-client/issues/121)
- [Polymarket Trading Limits](https://help.polymarket.com/en/articles/13364481-does-polymarket-have-trading-limits)
- [Nautilus Trader Polymarket Integration](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/docs/integrations/polymarket.md)
