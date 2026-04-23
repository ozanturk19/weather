# Weather Bot — Güncel Durum Analiz Raporu
**Tarih:** 2026-04-22  
**Repo:** ozanturk19/weather  
**Analiz kapsamı:** Tüm geliştirmeler uygulandıktan sonra kalan sorunlar ve geliştirilecek alanlar

---

## Yönetici Özeti

Bot altyapısı ciddi ölçüde olgunlaşmıştır: 6 model, Kalman bias düzeltme, dinamik ağırlıklandırma, SQLite katmanı, sinyal skoru, Monte Carlo VaR ve backtest iskelet kodunun tamamı mevcut. Ancak **birbirine bağlı 4 kritik sorun** sistemin gerçek potansiyelini engellemektedir:

1. JSON hâlâ asıl yazma hedefi — SQLite salt-okunur ayna
2. Polymarket settlement kaynağı (Weather Underground) ile tahmin kaynağı (Open-Meteo) uyumsuzluğu çözülmedi
3. Backtest sıfır sonuç üretti (Phase 0, veri toplama aşamasında takılı)
4. NO trading modülü dokümanlara rağmen kod tabanına girmedi

Aşağıda, kritiklik sırasına göre her sorun somut düzeltme önerileriyle verilmiştir.

---

## BÖLÜM 1 — KRİTİK SORUNLAR

### 1.1 JSON Birincil Kayıt Olmaya Devam Ediyor

**Sorun:** `db.py` kendi başlığında "JSON as source of truth" yazmaktadır. `trader.py` pozisyon açıldığında `paper_trades.json` / `live_trades.json` dosyalarına yazar; SQLite satırları sonradan `sync_all()` ile yansıtılır. Bu demektir:

- JSON bozulursa (disk dolması, yarım yazma, encoding hatası) SQLite'daki analitik tablolar da tutarsız hale gelir
- `sync_all()` idempotent olsa da zamanlama farklılıkları nedeniyle canlı işlem sırasında tutarsız durum oluşabilir
- `forecast_errors`, `model_forecasts`, `bias_corrections` tabloları doğrudan db.py üzerinden yazılırken `live_trades` hâlâ JSON'dan okunuyor — iki ayrı yetki kaynağı oluşuyor

**Etki Seviyesi:** Yüksek  
**Düzeltme:**  
`trader.py`'de her `place_order()` / `close_position()` çağrısından hemen sonra `db.write_live_trade()` çağrısı ekleyin. JSON dosyasını salt yedek olarak tutun. `sync_all()` startup'ta bir kez çalıştırılıp sonra devre dışı bırakılabilir.

```python
# trader.py — place_order() sonrası eklenecek satır
db.write_live_trade(market_id, side, price, size, order_id, status="open")
```

---

### 1.2 Settlement Kaynağı Uyumsuzluğu Çözülmedi

**Sorun:** Polymarket, hava durumu marketlerini **Weather Underground** (WUND) verisiyle kapatır. Bot, pozisyon kapatma kararını **Open-Meteo arşivi** üzerinden verir. Paris (LFPG) için +1.9°C sistematik sapma belgelenmiş; geçici düzeltme maksimum giriş fiyatını $0.18'e kısıtlamak oldu.

- Sorun Paris'e özgü değil. İstasyona göre WUND–Open-Meteo farkı değişir
- $0.18 tavanı Paris'te gerçek kenara girmeyi engeller; diğer istasyonlarda bu kapasite kaybı telafi edilemez
- `settlement_audit` tablosu var ama `scanner.py` kapatma kararında bunu kullanmıyor

**Etki Seviyesi:** Yüksek  
**Düzeltme:**  
Her istasyon için WUND–Open-Meteo delta'sını ölçen ve `bias_corrections` tablosuna yazan bir servis ekleyin:

```python
# Yeni dosya: bot/settlement_calibrator.py
def wund_vs_open_meteo_delta(station_icao: str, date: str) -> float:
    """
    Weather Underground API ile Open-Meteo arşivini karşılaştırarak
    sistematik farkı döndürür. bias_corrections tablosuna kaydeder.
    """
    wund_temp = fetch_wund_observation(station_icao, date)
    om_temp = fetch_open_meteo_archive(station_icao, date)
    delta = wund_temp - om_temp
    db.insert_bias_correction(station_icao, "settlement_delta", delta, date)
    return delta
```

`scanner.py` giriş sırasında bu delta'yı ensemble medyanına eklemeli:

```python
adjusted_median = ensemble_median + settlement_delta(station)
edge = threshold - adjusted_median
```

---

### 1.3 Backtest Sistemi Veri Toplamaktan İlerleyemedi

**Sorun:** `backtest/` dizini 4 dosyadan oluşuyor: `engine.py`, `fetch_actuals.py`, `fetch_forecasts.py`, `fetch_polymarket.py`. README bunların Phase 0 (veri toplama) olduğunu belirtiyor. Hiçbir simülasyon çalıştırılmamış, hiçbir strateji valide edilmemiş.

Bu önemli çünkü:
- Kalman Q/R parametreleri (Q=0.04, R=1.0) tarihsel veride optimize edilmedi
- Dinamik ağırlıkların hangi koşulda çalıştığı test edilmedi
- 5-bileşenli sinyal skoru için eşik değerleri (≥70 güçlü) gerekçesizdir
- CALIB_STD_FACTOR değişimi (statik 1.8 → dinamik?) backtest olmadan değerlendirilemez

**Etki Seviyesi:** Orta-Yüksek  
**Düzeltme:**  
En az 90 günlük geçmiş veriyle bir Phase 1 çalıştırın:

```bash
cd backtest
python fetch_actuals.py --start 2025-10-01 --end 2026-01-01
python fetch_forecasts.py --start 2025-10-01 --end 2026-01-01
python fetch_polymarket.py --start 2025-10-01 --end 2026-01-01
python engine.py --strategy baseline  # mevcut strateji
python engine.py --strategy kalman    # Kalman düzeltmeli
python engine.py --strategy dynamic   # dinamik ağırlıklı
```

Başarı metriği: Sharpe ≥ 1.5, Brier Skill Score ≥ 0.10.

---

### 1.4 NO Trading Modülü Eksik

**Sorun:** Orijinal talep, "tahminler 12-13°C iken market hâlâ 98.5 YES fiyatlıyorsa NO al" stratejisini içeriyordu. Bu strateji `WEATHER_BOT_ANALYSIS_AND_STRATEGIES.md`'de belgelendi ancak `scanner.py`'e girmedi.

Mevcut `scanner.py` yalnızca YES pozisyon açıyor (`BUY YES` side), $0.05–$0.40 fiyat aralığı hedefliyor.

**Etki Seviyesi:** Orta  
**Düzeltme:**  
`scanner.py`'e NO tarama bloğu ekleyin:

```python
def evaluate_no_trade(market: dict, ensemble: dict) -> dict | None:
    """
    Yüksek consensus, düşük YES fiyatı: NO buy değerlendirme.
    Güvenlik: model_p90 < threshold - 2°C şartı zorunlu.
    """
    yes_price = market["yes_price"]
    no_price = 1.0 - yes_price

    # Sadece neredeyse-kesin marketler (YES >= 0.96)
    if yes_price < 0.96:
        return None

    # Ensemble konsensüsü threshold'un çok altında mı?
    if ensemble["p90"] >= market["threshold"] - 2.0:
        return None

    # NO tarafında anlamlı kenar var mı?
    model_no_prob = 1.0 - ensemble["yes_prob"]
    edge = model_no_prob - no_price
    if edge < 0.03:  # %3 minimum kenar
        return None

    return {
        "side": "NO",
        "size": 20,           # Küçük sabit miktar
        "price": no_price,
        "edge": edge,
        "reason": "high_consensus_no"
    }
```

Sabit büyüklük önerilir (Kelly NO pozisyonları için çoğunlukla negatif sonuç verir).

---

## BÖLÜM 2 — ORTA ÖNCELİKLİ SORUNLAR

### 2.1 Dinamik Ağırlıklar için Cold Start Körü

**Sorun:** `dynamic_weights.py`, bir modele ağırlık vermek için en az **10 örnek + 2 model** şartı arar. Yeni bir istasyon veya yeni bir model eklendiğinde (AIFS gibi) bu eşiğe ulaşılana kadar sistem statik fallback'e döner. Statik AIFS ağırlığı 1.6 — ancak bu değer hiçbir performans verisine dayalı değil.

**Düzeltme:**  
Bayesian prior başlatma ekleyin:

```python
# dynamic_weights.py
PRIOR_RMSE = {
    "gfs": 2.1, "ecmwf": 1.7, "icon": 1.5,
    "ukmo": 3.2, "meteofrance": 2.0, "aifs": 1.6
}

def effective_weights(station: str) -> dict:
    rolling = compute_rolling_rmse(station)
    weights = {}
    for model in MODELS:
        observed_rmse = rolling.get(model)
        if observed_rmse and len(observed_rmse) >= MIN_SAMPLES:
            rmse = np.mean(observed_rmse[-30:])
        else:
            # Prior ile mevcut gözlem karışımı
            n = len(observed_rmse or [])
            rmse = (PRIOR_RMSE[model] * (10 - n) + 
                    np.mean(observed_rmse or [PRIOR_RMSE[model]]) * n) / 10
        weights[model] = 1.0 / max(rmse, RMSE_EPSILON)
    total = sum(weights.values())
    return {m: w / total for m, w in weights.items()}
```

---

### 2.2 CALIB_STD_FACTOR Durumu Belirsiz

**Sorun:** `main.py` orijinal implementasyonunda `CALIB_STD_FACTOR = 1.8` (sabit) mevcuttu. `WEATHER_MODEL_IMPROVEMENTS.md` §1'de dinamik gradient descent versiyonu önerildi. Ancak `main.py` incelendiğinde bu değişkenin dinamik hale getirilip getirilmediği net değil.

Eğer hâlâ statik 1.8 ise: farklı ufuklar (D+1 vs D+2), farklı mevsimler ve farklı ensemble spread büyüklükleri için aynı kalibrasyon genişletmesi uygulanıyor demektir.

**Düzeltme:**  
`main.py`'de şu değişkeni kontrol edin:

```python
# Statik (sorunlu):
calib_std = ensemble_std * CALIB_STD_FACTOR  # 1.8 sabit

# Dinamik (önerilen):
calib_std = ensemble_std * dynamic_calib_factor(
    horizon_hours=horizon,
    station=station,
    season=month
)
```

`dynamic_calib_factor()` her istasyon-sezon-ufuk kombinasyonu için ayrı bir öğrenilmiş çarpan döndürmeli, `bias_corrections` tablosuna kaydedilmeli.

---

### 2.3 Sinyal Skoru Eşikleri Ampirik Değil

**Sorun:** `signal_score.py` güçlü/orta/zayıf sınıflandırması için ≥70/50-70/<50 eşiklerini kullanıyor. Bu eşikler gerçek çıktı verisiyle kalibre edilmedi.

**Düzeltme:**  
`calibration.py`'den Brier skoru ile sinyal skoru bantları arasındaki korelasyonu ölçen bir analiz ekleyin:

```python
def calibrate_signal_thresholds(closed_trades_df) -> dict:
    bins = [0, 30, 50, 70, 90, 100]
    for low, high in zip(bins, bins[1:]):
        subset = closed_trades_df[
            (closed_trades_df.signal_score >= low) & 
            (closed_trades_df.signal_score < high)
        ]
        brier = compute_brier(subset)
        win_rate = subset.outcome.mean()
        print(f"Score {low}-{high}: Brier={brier:.3f}, WR={win_rate:.1%}")
```

Backtest verileri gelince bu analizi çalıştırın ve eşikleri gerçek performansa göre ayarlayın.

---

### 2.4 İstasyon Duraklama Mantığı Tek Yönlü

**Sorun:** `scanner.py` içindeki istasyon skill pause (yetkinlik duraklaması) mekanizması zayıf performans gösteren istasyonları devre dışı bırakıyor, ancak:

- Hangi koşulda yeniden aktive edildiği belirsiz
- Pause durumu muhtemelen bellekte tutuluyor, bot yeniden başladığında sıfırlanıyor
- RMSE iyileştiğinde (örn. yeni Kalman parametreleri sonrası) station otomatik olarak yeniden açılmıyor

**Düzeltme:**  
Pause durumunu SQLite'a kalıcı hale getirin:

```sql
-- db.py içinde yeni tablo
CREATE TABLE IF NOT EXISTS station_status (
    station TEXT PRIMARY KEY,
    paused INTEGER DEFAULT 0,
    pause_reason TEXT,
    paused_at TEXT,
    review_after TEXT,   -- 7 gün sonra otomatik kontrol
    last_rmse REAL
);
```

```python
# scanner.py — Her tarama döngüsünde
def should_trade_station(station: str) -> bool:
    status = db.get_station_status(station)
    if status["paused"]:
        if datetime.utcnow().isoformat() > status["review_after"]:
            recent_rmse = compute_recent_rmse(station, days=7)
            if recent_rmse < SKILL_THRESHOLD:
                db.reactivate_station(station)
                return True
        return False
    return True
```

---

### 2.5 Bimodal Tespit Karar Zincirini Etkilemiyor

**Sorun:** `main.py`'de bimodal dağılım tespiti (sklearn GaussianMixture, BIC karşılaştırması) yapılıyor. Ancak tespit sonucu yalnızca API yanıtında `is_bimodal: true` olarak raporlanıyor. `scanner.py`'deki trading kararında bimodal durumun herhangi bir etkisi yok.

Bimodal bir dağılım, "mod konsensüsü" hesabını anlamsız kılar. 50-50 iki mod arasındaki belirsizlik kenar hesabını boşa düşürür.

**Düzeltme:**  
`scanner.py`'de bimodal bayrağı kontrol edin ve bu pazarları atlayın:

```python
if forecast.get("is_bimodal") and forecast.get("bimodal_confidence", 0) > 0.7:
    logger.info(f"Skipping {market_id}: bimodal distribution detected")
    continue
```

---

### 2.6 Nonce Çakışma Önleme Heuristik

**Sorun:** `trader.py`'de BTC botu ile nonce çakışmasını önlemek için "bekleyen işlem tespiti" yapılıyor. Açıklama bunu zamanlama tabanlı bir sezgiye dayandırıyor. Bu güvenilir değil: iki bot aynı blokta işlem gönderirse heuristik başarısız olur.

**Düzeltme:**  
İki bot arasında nonce koordinasyonu için paylaşımlı bir kilitleme mekanizması kullanın:

```python
# shared/nonce_manager.py
import fcntl, json, time

NONCE_LOCK_FILE = "/tmp/polymarket_nonce.lock"

def get_next_nonce(w3, address: str) -> int:
    with open(NONCE_LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        nonce = w3.eth.get_transaction_count(address, "pending")
        time.sleep(0.1)  # Ağ propagasyon tamponu
        return nonce
```

---

## BÖLÜM 3 — DÜŞÜK ÖNCELİKLİ / İYİLEŞTİRME FIRSATLARI

### 3.1 Pozisyon Büyüklüğü Sabit Kaldı

`MIN_SHARES = 10`, `MAX_PRICE = 0.40` — tüm pazarlar için aynı büyüklük uygulanıyor. Sinyal skoru artık mevcut; büyüklük bununla orantılı olabilir:

```python
def compute_position_size(signal_score: float, base_size: int = 10) -> int:
    if signal_score >= 80:
        return base_size * 2       # 20 share
    elif signal_score >= 65:
        return base_size           # 10 share (default)
    else:
        return max(5, base_size // 2)  # 5 share
```

---

### 3.2 calibration.py ve portfolio_var.py Pasif

Her iki modül de yalnızca API endpoint'leri (`/api/calibration`, `/api/portfolio/var`) üzerinden manuel çağrılıyor. Canlı trading döngüsüne entegre değiller.

**Öneri:**  
- `portfolio_var.py`: Yeni bir pozisyon açılmadan önce portfolio-level VaR hesaplanmalı; VaR limit (%5 sermaye) aşılacaksa pozisyon atlanmalı
- `calibration.py`: Günlük Brier skoru izleme için scheduler eklenebilir (cron veya APScheduler)

```python
# trader.py — place_order() öncesi kontrol
var_result = portfolio_var.portfolio_var(current_positions)
if var_result["var_95"] > DAILY_CAPITAL * 0.05:
    logger.warning("VaR limit exceeded, skipping new position")
    return
```

---

### 3.3 AIFS Ensemble Üye Sayısı Doğrulanmıyor

Open-Meteo AIFS ensemble API'si 51 üye döndürmeli (`member00`–`member50`). Ancak API versiyonuna göre bu sayı 50 veya 52 olabilir. Mevcut kodda doğrulama yok.

**Düzeltme:**  
`main.py`'deki AIFS fetch fonksiyonuna kontrol ekleyin:

```python
aifs_members = [col for col in df.columns if col.startswith("temperature_2m_member")]
expected = 51
if len(aifs_members) != expected:
    logger.warning(f"AIFS returned {len(aifs_members)} members, expected {expected}")
# Gelen kaç üye varsa kullan, sabit varsayım yapma
```

---

### 3.4 web-designer.skill Dosyası Repo'da

Repo kökünde `web-designer.skill` adlı ilgisiz bir dosya var. Bu dosya deployment'ta gereksiz yer kaplıyor ve repo temizliğini bozuyor. Silinmeli veya `.gitignore`'a eklenmeli.

---

### 3.5 Monitoring / Alerting Yok

Bot sessizce hata yapabilir. Şu senaryolarda uyarı mekanizması yok:
- API rate limit (Open-Meteo 429)
- SQLite yazma hatası
- CLOB order rejection
- Günlük spend cap'e ulaşıldı
- Bir istasyon 7 gün durduruldu

**Öneri:** Minimum seviyede Telegram bot entegrasyonu:

```python
# bot/alerts.py
import httpx

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_alert(message: str, level: str = "INFO"):
    emoji = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🚨"}.get(level, "")
    httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{emoji} WeatherBot: {message}"}
    )
```

---

### 3.6 Bootstrap CI Performans Riski

Her tarama döngüsünde 500 iterasyon bootstrap CI hesaplanıyor. Eğer 20+ pazar aynı anda tarıyorsa bu 10.000+ yeniden örnekleme işlemi anlamına gelir ve gecikmeye yol açabilir.

**Düzeltme:**  
Bootstrap'ı eşzamanlı veya önbellekli çalıştırın:

```python
from functools import lru_cache

@lru_cache(maxsize=128)
def cached_bootstrap_ci(ensemble_tuple: tuple, n_iter: int = 500) -> tuple:
    ensemble = np.array(ensemble_tuple)
    return bootstrap_ci(ensemble, n_iter)
```

---

## BÖLÜM 4 — MİMARİ DEĞERLENDİRME

### Güçlü Yönler

| Alan | Durum |
|------|-------|
| Model çeşitliliği | ✅ 6 model, 142 ensemble üyesi |
| Bias düzeltme | ✅ Kalman filtresi entegre |
| Fee yapısı | ✅ GTC maker, 0% fee |
| Risk metrikleri | ✅ Monte Carlo VaR mevcut |
| Sinyal skoru | ✅ 5-bileşen, graded |
| Settlement takibi | ✅ settlement_audit tablosu |
| Dinamik ağırlık | ✅ 30-günlük RMSE bazlı |

### Zayıf Yönler

| Alan | Durum |
|------|-------|
| Birincil veri kaynağı | ❌ JSON, SQLite değil |
| Settlement kaynağı | ❌ WUND–Open-Meteo uçurumu |
| Backtest | ❌ Sıfır sonuç |
| NO trading | ❌ Eksik |
| Monitoring | ❌ Yok |
| Sinyal→büyüklük bağlantısı | ❌ Sabit 10 share |
| VaR→giriş kararı bağlantısı | ❌ Sadece API endpoint |

---

## BÖLÜM 5 — ÖNERİLEN GELİŞTİRME SIRASI

| Öncelik | Görev | Tahmini Çaba |
|---------|-------|--------------|
| 🔴 1 | `trader.py` → doğrudan SQLite yazımı | 2-3 saat |
| 🔴 2 | `settlement_calibrator.py` — WUND delta | 4-6 saat |
| 🔴 3 | Backtest Phase 1 çalıştır (90 gün veri) | 1-2 gün |
| 🟡 4 | NO trading bloğunu `scanner.py`'e ekle | 3-4 saat |
| 🟡 5 | Bimodal → skip logic | 30 dakika |
| 🟡 6 | İstasyon pause → SQLite kalıcılığı | 2 saat |
| 🟡 7 | VaR → giriş kapısı entegrasyonu | 1-2 saat |
| 🟡 8 | Cold start Bayesian prior | 1-2 saat |
| 🟢 9 | Telegram alerting | 1 saat |
| 🟢 10 | Sinyal skoru → dinamik pozisyon büyüklüğü | 1 saat |
| 🟢 11 | AIFS üye sayısı doğrulama | 30 dakika |
| 🟢 12 | web-designer.skill dosyasını sil | 5 dakika |

---

## Sonuç

Bot teknik altyapı açısından çok iyi bir noktaya geldi — 6 model, Kalman, dinamik ağırlık, Monte Carlo VaR hepsi mevcut. Asıl sorun şu: **bu bileşenler birbirine bağlı bir karar zinciri oluşturmuyor**. VaR hesaplanıyor ama giriş kararını durdurmaya bağlı değil. Sinyal skoru üretiliyor ama pozisyon büyüklüğünü etkilemiyor. Bimodal tespit yapılıyor ama o pazarlar atlanmıyor. Settlement delta ölçülüyor ama ensemble medyanına uygulanmıyor.

Önümüzdeki sprint'in odak noktası bu bileşenleri karar zincirine **gerçekten entegre etmek** olmalı — yeni modül yazmak değil.
