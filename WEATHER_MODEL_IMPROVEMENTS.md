# Weather Bot — Model Geliştirme Analizi

> **Kapsam:** ozanturk19/weather reposunun mevcut modeli derinlemesine analiz edildi.
> Her geliştirme önerisi: sorun tespiti → neden önemli → nasıl yapılır (kod dahil) formatında.

---

## İçindekiler

1. [Ensemble Kalibrasyon — CALIB_STD_FACTOR Sorunu](#1-ensemble-kalibrasyon--calib_std_factor-sorunu)
2. [Dinamik Model Ağırlıklandırma](#2-dinamik-model-ağırlıklandırma)
3. [Bias Düzeltme Sistemi — Kalman Filter](#3-bias-düzeltme-sistemi--kalman-filter)
4. [CRPS Tabanlı Sinyal Kalitesi Skoru](#4-crps-tabanlı-sinyal-kalitesi-skoru)
5. [Bimodal Ensemble Tespiti](#5-bimodal-ensemble-tespiti)
6. [Çok Boyutlu Hata Analizi Matrisi](#6-çok-boyutlu-hata-analizi-matrisi)
7. [Portföy Korelasyon Risk Yönetimi](#7-portföy-korelasyon-risk-yönetimi)
8. [Settlement Kaynak Güvenilirliği](#8-settlement-kaynak-güvenilirliği)
9. [Bootstrap Güven Aralıkları](#9-bootstrap-güven-aralıkları)
10. [Gerçek Zamanlı Kalibrasyon Dashboard](#10-gerçek-zamanlı-kalibrasyon-dashboard)
11. [İstasyon Uzmanlığı — Sistematik Edge](#11-istasyon-uzmanlığı--sistematik-edge)
12. [Öncelik Matrisi ve Uygulama Sırası](#12-öncelik-matrisi-ve-uygulama-sırası)

---

## 1. Ensemble Kalibrasyon — CALIB_STD_FACTOR Sorunu

### Problem Tespiti

`main.py` satır 272'de:
```python
CALIB_STD_FACTOR = 1.8
```

Bu sabit, ensemble standart sapmasını tüm koşullarda 1.8 ile çarpıyor. Amaç: modelin aşırı güvenini (overconfidence) düzeltmek. Ama sorunlar:

1. **Sabit çarpan her durumda aynı** — kış/yaz, D+1/D+2, geniş/dar spread — hepsi aynı 1.8
2. **Doğrulama kaydı yok** — 1.8 sayısının doğruluğunu ölçen hiçbir metrik yok
3. **Yönlü değil** — bazı modeller underconfident, bazıları overconfident; tek çarpan ikisine de uymuyor

### Etki

```
Gerçek senaryo:
  Ensemble spread dar (tüm modeller hemfikir): spread = 0.5°C
  CALIB: 0.5 × 1.8 = 0.9°C → doğru genişletme

  Ensemble spread geniş (modeller ayrışmış): spread = 3°C
  CALIB: 3 × 1.8 = 5.4°C → aşırı genişletme, tüm sinyali bulanıklaştırır

Sonuç: Geniş spread durumlarında gerçek fırsatlar kaçırılıyor
```

### Çözüm: Dinamik Kalibrasyon

```python
# main.py — replace static CALIB_STD_FACTOR

def dynamic_calib_factor(
    ensemble_spread: float,     # max - min ensemble temperature
    horizon_days: int,          # 0=bugün, 1=yarın, 2=öbür gün
    station: str,
    month: int,
    historical_calib: dict,     # öğrenilmiş istasyon bazlı faktörler
) -> float:
    """
    Statik 1.8 yerine bağlama duyarlı kalibrasyon faktörü.
    """
    # Temel faktör: horizon'a göre (D+2 daha belirsiz)
    base = {0: 1.2, 1: 1.5, 2: 2.0}.get(horizon_days, 1.8)

    # Spread penalty: geniş spread → daha az agresif genişletme
    # Dar spread zaten güvenilir, genişletmek mantıklı
    # Geniş spread belirsiz, daha da genişletmek gereksiz
    if ensemble_spread < 1.5:
        spread_factor = 1.3    # dar → biraz genişlet
    elif ensemble_spread < 3.0:
        spread_factor = 1.0    # orta → nötr
    else:
        spread_factor = 0.7    # geniş → orijinal spread'i küçük tut

    # İstasyon bazlı öğrenilmiş düzeltme
    station_adj = historical_calib.get(station, {}).get(month, 1.0)

    return round(base * spread_factor * station_adj, 3)

# Öğrenme döngüsü (her settlement sonrası çağır)
def update_calib_factor(
    station: str,
    month: int,
    predicted_prob: float,    # modelin verdiği olasılık (0-1)
    actual_outcome: bool,     # gerçekten o bucket'e düştü mü?
    historical_calib: dict,
    learning_rate: float = 0.05,
) -> dict:
    """
    Calibration error'a göre istasyon/ay bazlı faktörü güncelle.
    Brier score minimizasyonu.
    """
    brier_error = (predicted_prob - float(actual_outcome)) ** 2
    current = historical_calib.get(station, {}).get(month, 1.0)
    
    # Gradient: overconfident → artır faktörü, underconfident → azalt
    gradient = 2 * (predicted_prob - float(actual_outcome)) * predicted_prob
    new_factor = current - learning_rate * gradient
    new_factor = max(0.8, min(2.5, new_factor))  # sınırla
    
    historical_calib.setdefault(station, {})[month] = round(new_factor, 4)
    return historical_calib
```

---

## 2. Dinamik Model Ağırlıklandırma

### Problem Tespiti

`main.py`'daki mevcut sabit ağırlıklar:
```python
MODEL_WEIGHTS = {
    'icon': 1.8,
    'ecmwf_ifs': 1.5,
    'gfs': 1.0,
    'meteofrance': 0.9,
    'ukmo': 0.5,
}
```

Bu ağırlıklar "60 günlük backtesting"e göre belirlenmiş ama:
- **Zaman içinde değişmiyor** — ECMWF 2024'te model güncellemesi yaptı, ICON değişti
- **İstasyon bazlı değil** — London için ECMWF iyi olabilir, Dubai için GFS daha iyi
- **Mevsim bazlı değil** — Kış aylarında model performansları değişir

### Çözüm: Rolling RMSE Tabanlı Ağırlık

```python
from collections import deque
import math

class ModelWeightTracker:
    """
    Her istasyon için son 30 günlük rolling RMSE hesaplar,
    inverse-variance weighting uygular.
    """
    def __init__(self, window: int = 30, min_samples: int = 5):
        self.window = window
        self.min_samples = min_samples
        # {station: {model: deque([squared_errors])}}
        self.errors: dict[str, dict[str, deque]] = {}

    def record(self, station: str, model: str, predicted: float, actual: float):
        self.errors.setdefault(station, {}).setdefault(model, deque(maxlen=self.window))
        self.errors[station][model].append((predicted - actual) ** 2)

    def get_weights(self, station: str) -> dict[str, float]:
        station_errors = self.errors.get(station, {})
        rmse_map = {}

        for model, sq_errs in station_errors.items():
            if len(sq_errs) < self.min_samples:
                rmse_map[model] = None  # Yetersiz veri
            else:
                rmse_map[model] = math.sqrt(sum(sq_errs) / len(sq_errs))

        # Inverse-variance weighting
        valid = {m: r for m, r in rmse_map.items() if r is not None and r > 0}
        if not valid:
            # Fallback: orijinal sabit ağırlıklar
            return {'icon': 1.8, 'ecmwf_ifs': 1.5, 'gfs': 1.0,
                    'meteofrance': 0.9, 'ukmo': 0.5}

        inv_rmse = {m: 1.0 / r for m, r in valid.items()}
        total = sum(inv_rmse.values())
        weights = {m: v / total * len(inv_rmse) for m, v in inv_rmse.items()}

        # Verisiz modeller için fallback
        for model in ['icon', 'ecmwf_ifs', 'gfs', 'meteofrance', 'ukmo']:
            if model not in weights:
                weights[model] = 1.0  # nötr ağırlık

        return weights

    def get_model_drift_alert(self, station: str) -> list[str]:
        """RMSE son 7 günde %50+ arttıysa uyarı ver."""
        alerts = []
        for model, errs in self.errors.get(station, {}).items():
            if len(errs) < 14:
                continue
            errs_list = list(errs)
            recent_7  = math.sqrt(sum(errs_list[-7:]) / 7)
            previous_7 = math.sqrt(sum(errs_list[-14:-7]) / 7)
            if previous_7 > 0 and recent_7 / previous_7 > 1.5:
                alerts.append(f"{model}@{station}: RMSE +{((recent_7/previous_7)-1)*100:.0f}%")
        return alerts

# Kullanım (main.py blend fonksiyonuna entegre):
weight_tracker = ModelWeightTracker()

def blend_forecasts_dynamic(station: str, model_forecasts: dict) -> dict:
    weights = weight_tracker.get_weights(station)
    
    alerts = weight_tracker.get_model_drift_alert(station)
    for alert in alerts:
        print(f"[MODEL DRIFT] {alert}")
    
    # Ağırlıklı ortalama hesapla
    weighted_temps = []
    for model, forecast in model_forecasts.items():
        w = weights.get(model, 1.0)
        weighted_temps.extend([forecast.p50] * int(w * 10))  # ağırlıklı liste
    
    return {
        'p50': statistics.median(weighted_temps),
        'weights_used': weights,
    }
```

---

## 3. Bias Düzeltme Sistemi — Kalman Filter

### Problem Tespiti

Mevcut bias düzeltme (`main.py` satır 272-284):
```python
recent_bias = bias_entries[-7:]
w = [2 ** i for i in range(len(recent_bias))]
w_bias = sum(err * wi for err, wi in zip(recent_bias, w)) / sum(w)
mae = sum(abs(e) for e in recent_bias) / len(recent_bias)
bias_correction = round(max(-mae, min(mae, w_bias)), 2)
```

**Sorunlar:**
1. `2^i` ağırlıkı son güne %51 ağırlık veriyor — tek gün anomalisine aşırı duyarlı
2. `-MAE / +MAE` kırpma asimetrik düzeltmeye neden oluyor
3. Bias belirsizliği ölçülmüyor — 1 günlük veriye mi, 7 günlük veriye mi güveniyoruz?
4. Sistematik bias ile rastgele hata ayrıştırılmıyor

### Çözüm: Kalman Filter Bias Estimator

```python
class KalmanBiasEstimator:
    """
    Bias'ı zamanla değişen bir sinyal olarak modeller.
    Her yeni gözlemde (tahmin - gerçek) Kalman güncelleme yapar.
    
    State: bias (sıcaklık farkı, °C)
    Measurement: (predicted - actual) her gün
    """
    def __init__(
        self,
        process_noise: float = 0.1,    # bias ne hızlı değişir? (°C²/gün)
        measurement_noise: float = 1.0, # günlük ölçüm ne kadar gürültülü?
        initial_bias: float = 0.0,
        initial_uncertainty: float = 2.0,
    ):
        self.x = initial_bias        # Tahmin edilen bias
        self.P = initial_uncertainty  # Belirsizlik (variance)
        self.Q = process_noise        # Süreç gürültüsü
        self.R = measurement_noise    # Ölçüm gürültüsü

    def update(self, measured_error: float) -> tuple[float, float]:
        """
        measured_error = predicted_temp - actual_temp
        Returns: (bias_estimate, uncertainty)
        """
        # Predict step
        P_pred = self.P + self.Q

        # Update step (Kalman gain)
        K = P_pred / (P_pred + self.R)
        self.x = self.x + K * (measured_error - self.x)
        self.P = (1 - K) * P_pred

        return round(self.x, 3), round(self.P, 3)

    def get_correction(self, max_correction: float = 2.0) -> float:
        """Uygulanacak bias düzeltmesi (sınırlı)."""
        return max(-max_correction, min(max_correction, -self.x))
        # Negatif: tahmin gerçekten yüksek → düşür

    def get_confidence(self) -> str:
        """Bias tahminine olan güven seviyesi."""
        if self.P < 0.3:   return "HIGH"
        if self.P < 0.8:   return "MEDIUM"
        return "LOW"

# Her istasyon için ayrı Kalman estimator
kalman_bias = {
    'eglc': KalmanBiasEstimator(process_noise=0.05),   # London: stabil
    'lfpg': KalmanBiasEstimator(process_noise=0.15),   # Paris: daha değişken
    'ltac': KalmanBiasEstimator(process_noise=0.20),   # Istanbul: yüksek variabilite
    'omdb': KalmanBiasEstimator(process_noise=0.10),   # Dubai: stabil ama sıcak
}

# Settlement sonrası çağır:
def on_settlement(station: str, predicted: float, actual: float):
    estimator = kalman_bias.get(station)
    if not estimator:
        return
    error = predicted - actual
    bias, uncertainty = estimator.update(error)
    confidence = estimator.get_confidence()
    correction = estimator.get_correction()
    
    print(f"[BIAS] {station}: error={error:+.1f}°C | "
          f"bias_est={bias:+.2f}°C | "
          f"correction={correction:+.2f}°C | "
          f"confidence={confidence} (P={uncertainty:.3f})")
```

---

## 4. CRPS Tabanlı Sinyal Kalitesi Skoru

### Problem Tespiti

`scanner.py`'da her fırsat için binary karar: "MIN_EDGE=%5 var mı?" — evet/hayır. 
Fırsatlar arasında **sıralama yok**. Bütçe kısıtlıysa hangisini önce al?

### Çözüm: Bileşik Sinyal Skoru

```python
# properscoring veya scoringrules kütüphanesi
# pip install properscoring

import properscoring as ps
import numpy as np

def calc_signal_quality_score(
    ensemble_members: list[float],   # tüm ensemble tahminleri (°C)
    market_price: float,             # piyasanın verdiği olasılık (0-1)
    market_threshold: float,         # bucket üst sınırı (°C)
    liquidity_usd: float,            # bu fiyatta mevcut likidite
    horizon_days: int,               # 0=bugün, 1=yarın, 2=öbür gün
) -> dict:
    """
    0-100 arası bileşik sinyal kalitesi skoru.
    Yüksek skor = daha iyi fırsat.
    """
    members = np.array(ensemble_members)
    actual_prob = np.mean(members <= market_threshold)

    # --- Bileşen 1: Edge büyüklüğü (0-40 puan) ---
    raw_edge = actual_prob - market_price
    edge_score = min(40, max(0, raw_edge * 200))  # 20% edge = 40 puan

    # --- Bileşen 2: Ensemble tutarlılığı (0-30 puan) ---
    # CRPS: daha düşük = ensemble daha keskin ve kalibrasyonlu
    # Sanal gözlem olarak market_threshold kullan (ama bu proxy)
    ensemble_spread = np.std(members)
    consistency_score = max(0, 30 - ensemble_spread * 8)
    # Dar spread (< 1°C) → 30 puan, geniş spread (3.75°C+) → 0 puan

    # --- Bileşen 3: Likidite (0-20 puan) ---
    if liquidity_usd >= 500:    liq_score = 20
    elif liquidity_usd >= 200:  liq_score = 15
    elif liquidity_usd >= 50:   liq_score = 10
    elif liquidity_usd >= 10:   liq_score = 5
    else:                       liq_score = 0

    # --- Bileşen 4: Horizon cezası (0-10 puan) ---
    horizon_score = {0: 10, 1: 7, 2: 3}.get(horizon_days, 0)

    total = edge_score + consistency_score + liq_score + horizon_score

    return {
        'total':       round(total, 1),
        'edge':        round(edge_score, 1),
        'consistency': round(consistency_score, 1),
        'liquidity':   round(liq_score, 1),
        'horizon':     round(horizon_score, 1),
        'actual_prob': round(actual_prob, 3),
        'edge_pct':    round(raw_edge * 100, 1),
    }

# Kullanım:
MIN_SIGNAL_SCORE = 45  # sadece 45+ skora sahip fırsatları al

opportunities = []
for market in active_markets:
    score = calc_signal_quality_score(...)
    if score['total'] >= MIN_SIGNAL_SCORE:
        opportunities.append((market, score))

# Skora göre sırala — en iyi fırsat önce
opportunities.sort(key=lambda x: x[1]['total'], reverse=True)
```

### CRPS ile Geriye Dönük Model Değerlendirme

```python
def evaluate_ensemble_crps(
    ensemble_members: list[float],
    actual_temp: float,
) -> float:
    """
    CRPS hesapla. Düşük CRPS = daha iyi ensemble.
    Perfect deterministic forecast: CRPS = 0
    """
    return ps.crps_ensemble(actual_temp, np.array(ensemble_members))

# Her settlement sonrası kaydet, model ağırlıklandırmasını güncelle
# Hedef: CRPS'i minimize eden model kombinasyonu
```

---

## 5. Bimodal Ensemble Tespiti

### Problem Tespiti

Bazen ensemble iki gruba ayrılır:
```
Senaryo: ECMWF 50 üye → 25'i 12°C, 25'i 20°C tahmin ediyor
Medyan = 16°C ama hiçbir üye 16°C tahmin etmiyor!
Mevcut sistem bu durumu görmüyor → yanlış bucket'e girilir
```

### Çözüm: Bimodal Tespit

```python
from sklearn.mixture import GaussianMixture
import numpy as np

def detect_bimodal_ensemble(
    members: list[float],
    separation_threshold: float = 3.0,  # iki mod arası min fark (°C)
    min_members: int = 10,
) -> dict:
    """
    Ensemble iki kümeye ayrılıyor mu?
    BIC (Bayesian Information Criterion) karşılaştırır: 1 vs 2 bileşen.
    """
    if len(members) < min_members:
        return {'bimodal': False, 'reason': 'insufficient_members'}

    X = np.array(members).reshape(-1, 1)

    # 1-bileşen (unimodal)
    gmm1 = GaussianMixture(n_components=1, random_state=0).fit(X)
    bic1 = gmm1.bic(X)

    # 2-bileşen (bimodal)
    gmm2 = GaussianMixture(n_components=2, random_state=0).fit(X)
    bic2 = gmm2.bic(X)

    if bic2 < bic1 - 10:  # BIC farkı anlamlı
        means = sorted(gmm2.means_.flatten())
        separation = abs(means[1] - means[0])

        if separation >= separation_threshold:
            weights = gmm2.weights_.flatten()
            return {
                'bimodal':    True,
                'mode_1':     round(means[0], 1),
                'mode_2':     round(means[1], 1),
                'weight_1':   round(float(weights[0]), 2),
                'weight_2':   round(float(weights[1]), 2),
                'separation': round(separation, 1),
                'action':     'SKIP_OR_HEDGE',
            }

    return {'bimodal': False, 'separation': 0}

# scanner.py'de kullanım:
bm = detect_bimodal_ensemble(all_member_temps)
if bm['bimodal']:
    print(f"[BIMODAL] {city}: mod1={bm['mode_1']}°C ({bm['weight_1']*100:.0f}%) "
          f"mod2={bm['mode_2']}°C ({bm['weight_2']*100:.0f}%) → atla")
    continue  # bu markete girme
```

---

## 6. Çok Boyutlu Hata Analizi Matrisi

### Problem Tespiti

Mevcut hata takibi: `actual_temp - top_pick` → tek boyutlu JSON kaydı.
Hangi durumda model yanılıyor? Bilinmiyor.

### Çözüm: Segmentlenmiş Hata Matrisi

```python
import sqlite3
from dataclasses import dataclass
from enum import Enum

class Season(Enum):
    WINTER = 'winter'   # Aralık, Ocak, Şubat
    SPRING = 'spring'   # Mart, Nisan, Mayıs
    SUMMER = 'summer'   # Haziran, Temmuz, Ağustos
    AUTUMN = 'autumn'   # Eylül, Ekim, Kasım

class EnsembleSpreadCategory(Enum):
    TIGHT    = 'tight'    # std < 1°C
    MODERATE = 'moderate' # 1-2.5°C
    WIDE     = 'wide'     # > 2.5°C

# SQLite tablo (Bölüm 2 dokümanında tam migration)
CREATE_FORECAST_ERRORS = """
CREATE TABLE IF NOT EXISTS forecast_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    station       TEXT NOT NULL,
    horizon_days  INTEGER NOT NULL,
    season        TEXT NOT NULL,
    month         INTEGER NOT NULL,
    
    model_p50     REAL NOT NULL,
    model_p10     REAL NOT NULL,
    model_p90     REAL NOT NULL,
    ensemble_std  REAL NOT NULL,
    spread_cat    TEXT NOT NULL,
    
    actual_temp   REAL NOT NULL,
    error_c       REAL NOT NULL,  -- predicted - actual
    abs_error_c   REAL NOT NULL,
    in_80pct_ci   INTEGER NOT NULL,  -- 1 = actual p10-p90 arasında
    
    market_price  REAL,
    signal_score  REAL,
    trade_taken   INTEGER DEFAULT 0,
    trade_pnl     REAL,
    
    created_at    INTEGER DEFAULT (strftime('%s','now'))
);
"""

def analyze_error_matrix(db_path: str) -> dict:
    """
    Hangi boyutlarda hata yüksek? → Strateji optimizasyonu.
    """
    conn = sqlite3.connect(db_path)
    
    report = {}
    
    # 1. İstasyon bazlı MAE
    report['by_station'] = conn.execute("""
        SELECT station,
               COUNT(*) as n,
               ROUND(AVG(abs_error_c), 2) as mae,
               ROUND(AVG(error_c), 2) as mean_bias,
               ROUND(AVG(in_80pct_ci) * 100, 1) as coverage_80pct
        FROM forecast_errors
        GROUP BY station
        ORDER BY mae DESC
    """).fetchall()
    
    # 2. Horizon bazlı MAE
    report['by_horizon'] = conn.execute("""
        SELECT horizon_days,
               ROUND(AVG(abs_error_c), 2) as mae,
               ROUND(AVG(in_80pct_ci) * 100, 1) as coverage_80pct
        FROM forecast_errors
        GROUP BY horizon_days
    """).fetchall()
    
    # 3. Ensemble spread ve hata ilişkisi
    report['by_spread'] = conn.execute("""
        SELECT spread_cat,
               ROUND(AVG(abs_error_c), 2) as mae,
               COUNT(*) as n
        FROM forecast_errors
        GROUP BY spread_cat
    """).fetchall()
    
    # 4. Sezon bazlı performans
    report['by_season'] = conn.execute("""
        SELECT season, station,
               ROUND(AVG(abs_error_c), 2) as mae,
               ROUND(AVG(error_c), 2) as systematic_bias
        FROM forecast_errors
        GROUP BY season, station
        ORDER BY mae DESC
    """).fetchall()
    
    # 5. Signal score ve gerçek sonuç korelasyonu
    report['signal_calibration'] = conn.execute("""
        SELECT
          CASE
            WHEN signal_score < 40 THEN 'weak (<40)'
            WHEN signal_score < 60 THEN 'moderate (40-60)'
            ELSE 'strong (60+)'
          END as signal_bucket,
          COUNT(*) as n,
          ROUND(AVG(CASE WHEN trade_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) as win_rate,
          ROUND(AVG(trade_pnl), 3) as avg_pnl
        FROM forecast_errors
        WHERE trade_taken = 1
        GROUP BY signal_bucket
    """).fetchall()
    
    conn.close()
    return report
```

---

## 7. Portföy Korelasyon Risk Yönetimi

### Problem Tespiti

30 eş zamanlı pozisyon açılabiliyor ama aralarındaki korelasyon kontrol edilmiyor.

**Gerçek risk:**
```
Senaryo: Atlantik'ten soğuk hava dalgası geliyor
→ London, Paris, Amsterdam, Madrid hepsinde tahmin tutmuyor
→ 30 pozisyon aynı anda kaybediyor = gerçek risk 30x değil, çok daha az çeşitlendirilmiş
```

### Çözüm: Korelasyon Matrisi ve VaR

```python
import numpy as np

# İstasyonlar arası tarihsel korelasyon (°C hataları)
STATION_CORRELATION = {
    ('eglc', 'lfpg'): 0.72,   # London-Paris yüksek korelasyon
    ('eglc', 'eham'): 0.68,   # London-Amsterdam
    ('lfpg', 'eham'): 0.75,   # Paris-Amsterdam
    ('ltac', 'eglc'): 0.35,   # Istanbul-London düşük
    ('omdb', 'eglc'): 0.10,   # Dubai-London çok düşük
    ('rjtt', 'eglc'): 0.05,   # Tokyo-London neredeyse yok
}

def get_correlation(s1: str, s2: str) -> float:
    key = tuple(sorted([s1, s2]))
    return STATION_CORRELATION.get(key, 0.2)  # varsayılan orta korelasyon

def calc_portfolio_var(
    open_positions: list[dict],  # [{station, size_usd, win_prob}, ...]
    confidence: float = 0.95,
) -> float:
    """
    Portföyün 95% VaR'ını hesapla (1000 Monte Carlo simülasyonu).
    """
    n = len(open_positions)
    if n == 0:
        return 0.0

    # Korelasyon matrisi oluştur
    corr_matrix = np.eye(n)
    stations = [p['station'] for p in open_positions]
    for i in range(n):
        for j in range(i + 1, n):
            c = get_correlation(stations[i], stations[j])
            corr_matrix[i, j] = c
            corr_matrix[j, i] = c

    # Cholesky decomposition ile korelasyonlu simülasyon
    try:
        L = np.linalg.cholesky(corr_matrix)
    except np.linalg.LinAlgError:
        L = np.eye(n)  # korelasyon matris singular ise nötr

    N_SIM = 1000
    portfolio_losses = []
    for _ in range(N_SIM):
        z = np.random.standard_normal(n)
        correlated_z = L @ z  # korelasyonlu normal değişkenler

        sim_loss = 0
        for i, pos in enumerate(open_positions):
            # Win → +gain, Lose → -cost
            win_prob = pos['win_prob']
            outcome = 1 if (correlated_z[i] < win_prob * 2 - 1) else 0
            if outcome == 0:
                sim_loss += pos['size_usd']  # tam kayıp (YES: entry fiyatı)

        portfolio_losses.append(sim_loss)

    var_95 = np.percentile(sorted(portfolio_losses), confidence * 100)
    return round(float(var_95), 2)

def should_add_position(
    open_positions: list[dict],
    new_position: dict,
    max_var_usd: float = 50.0,
) -> bool:
    """Yeni pozisyon VaR limitini aşar mı?"""
    current_var = calc_portfolio_var(open_positions)
    new_var     = calc_portfolio_var(open_positions + [new_position])
    incremental = new_var - current_var

    print(f"[VAR] Current=${current_var:.2f} | "
          f"New=${new_var:.2f} | "
          f"Incremental=${incremental:.2f} | "
          f"Limit=${max_var_usd:.2f}")

    return new_var <= max_var_usd
```

---

## 8. Settlement Kaynak Güvenilirliği

### Problem Tespiti

Paris (LFPG) için kodda yorum:
```python
# Paris: settlement kaynağı uyumsuzluğu
# Weather Underground vs Open-Meteo = +1.9°C fark
# Bu yüzden Paris max fiyatı 0.18 ile sınırlı
```

Bu geçici bant-aid çözüm. Gerçek fix:

### Çözüm: Çok Kaynaklı Settlement Doğrulama

```python
import httpx
import asyncio

SETTLEMENT_SOURCES = {
    'open_meteo_archive': {
        'url': 'https://archive-api.open-meteo.com/v1/archive',
        'weight': 1.0,
    },
    'noaa_ghcnd': {
        'url': 'https://www.ncei.noaa.gov/cdo-web/api/v2/data',
        'weight': 1.2,  # resmi kaynak, daha güvenilir
    },
    'metar_historical': {
        'url': 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py',
        'weight': 0.8,
    },
}

# İstasyon bazlı tarihsel kaynak uyumsuzluğu
SOURCE_DISAGREEMENT_DB: dict[str, list[float]] = {
    'lfpg': [-1.9, -2.1, -1.8, -2.0],  # Open-Meteo genellikle 1.9°C düşük
    'eglc': [0.1, 0.0, -0.2, 0.1],     # London tutarlı
}

async def fetch_multi_source_settlement(
    station_icao: str,
    date: str,
) -> dict:
    results = {}
    
    async with httpx.AsyncClient(timeout=10) as client:
        # Open-Meteo
        r = await client.get(
            'https://archive-api.open-meteo.com/v1/archive',
            params={
                'latitude': STATION_COORDS[station_icao]['lat'],
                'longitude': STATION_COORDS[station_icao]['lon'],
                'start_date': date,
                'end_date': date,
                'daily': 'temperature_2m_max',
            }
        )
        if r.status_code == 200:
            results['open_meteo'] = r.json()['daily']['temperature_2m_max'][0]
    
    # Kaynaklar arası uyumsuzluk kontrolü
    temps = list(results.values())
    if len(temps) >= 2:
        disagreement = max(temps) - min(temps)
        if disagreement > 1.0:
            print(f"[SETTLEMENT WARNING] {station_icao} {date}: "
                  f"sources disagree by {disagreement:.1f}°C → {results}")
    
    # Ağırlıklı ortalama
    if temps:
        return {
            'temp': round(sum(temps) / len(temps), 1),
            'sources': results,
            'disagreement': round(max(temps) - min(temps), 2) if len(temps) > 1 else 0,
            'confidence': 'HIGH' if len(temps) >= 2 and (max(temps) - min(temps)) < 0.5 else 'LOW',
        }
    
    return {'temp': None, 'sources': {}, 'confidence': 'NONE'}
```

---

## 9. Bootstrap Güven Aralıkları

### Problem Tespiti

Mevcut `pct()` fonksiyonu (`main.py`):
```python
def pct(sorted_vals: list, p: float) -> float:
    n = len(sorted_vals)
    idx = p / 100 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return round(sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo]), 1)
```

Bu **nokta tahmini** — p10 değerinin güven aralığı yok. 40 üyeyle yapılan p10 tahmini ne kadar güvenilir?

### Çözüm: Bootstrap CI

```python
import numpy as np
from typing import NamedTuple

class PercentileWithCI(NamedTuple):
    estimate: float
    ci_low:   float
    ci_high:  float
    width:    float  # ci_high - ci_low

def bootstrap_percentile(
    values: list[float],
    p: float,
    n_boot: int = 500,
    ci: float = 0.90,
) -> PercentileWithCI:
    """
    Bootstrap ile percentile güven aralığı.
    n_boot=500: hızlı ama yeterince kararlı.
    """
    arr = np.array(values)
    boot_estimates = []
    
    for _ in range(n_boot):
        sample = np.random.choice(arr, size=len(arr), replace=True)
        boot_estimates.append(np.percentile(sample, p))
    
    boot_estimates.sort()
    lower_idx = int((1 - ci) / 2 * n_boot)
    upper_idx = int((1 + ci) / 2 * n_boot)
    
    return PercentileWithCI(
        estimate = round(float(np.percentile(arr, p)), 2),
        ci_low   = round(boot_estimates[lower_idx], 2),
        ci_high  = round(boot_estimates[upper_idx], 2),
        width    = round(boot_estimates[upper_idx] - boot_estimates[lower_idx], 2),
    )

# Kullanım — scanner'a ekle:
p90_result = bootstrap_percentile(member_temps, p=90)
print(f"p90: {p90_result.estimate}°C "
      f"[90% CI: {p90_result.ci_low}–{p90_result.ci_high}°C, "
      f"width={p90_result.width}°C]")

# Geniş CI → belirsiz → daha küçük pozisyon
if p90_result.width > 3.0:
    size_modifier = 0.5  # yarı boyut
elif p90_result.width > 2.0:
    size_modifier = 0.75
else:
    size_modifier = 1.0
```

---

## 10. Gerçek Zamanlı Kalibrasyon Dashboard

### Eksik Olan Metrikler

```python
# Her gün hesaplanmalı:

def compute_calibration_metrics(trades_db: list[dict]) -> dict:
    """
    Brier Score, Reliability, Sharpness, CRPS.
    """
    import properscoring as ps
    
    probs = [t['predicted_prob'] for t in trades_db if t['settled']]
    outcomes = [1 if t['won'] else 0 for t in trades_db if t['settled']]
    
    if len(probs) < 10:
        return {'error': 'insufficient_data', 'n': len(probs)}
    
    # Brier Score (düşük = iyi, perfect = 0)
    brier = sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)
    
    # Reliability (calibration): her 10-puan bandında win rate
    reliability = []
    bands = [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
             (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in bands:
        in_band = [(p, o) for p, o in zip(probs, outcomes) if lo <= p < hi]
        if in_band:
            mean_p = sum(p for p, _ in in_band) / len(in_band)
            mean_o = sum(o for _, o in in_band) / len(in_band)
            reliability.append({'band': f'{lo:.1f}-{hi:.1f}', 'predicted': mean_p, 'actual': mean_o, 'n': len(in_band)})
    
    # Sharpness (mean predicted prob — ne kadar emin tahmin yapıyoruz?)
    sharpness = sum(p * (1 - p) for p in probs) / len(probs)
    
    # 80% interval coverage
    in_ci = [t for t in trades_db if t.get('in_80pct_ci')]
    coverage_80 = len(in_ci) / len(trades_db) if trades_db else 0
    
    return {
        'brier_score':   round(brier, 4),
        'sharpness':     round(sharpness, 4),
        'coverage_80pct': round(coverage_80 * 100, 1),
        'reliability':   reliability,
        'n_trades':      len(probs),
        'calibrated':    abs(coverage_80 - 0.80) < 0.05,  # %80 CI gerçekten %80'i mi kapsıyor?
    }
```

---

## 11. İstasyon Uzmanlığı — Sistematik Edge

### Mevcut Botun Yakalamadığı Sistematik Faktörler

**London City Airport (EGLC) özel faktörler:**
```python
EGLC_CORRECTIONS = {
    # Urban heat island: Şehir merkezi 0.5-1.5°C daha sıcak
    'urban_heat_island': +0.8,  # yaz aylarında daha belirgin

    # Thames nehri etkisi: gece ısı tutma
    # → Günlük maksimum çok etkilenmez ama minimum etkiler
    'thames_effect': 0.0,  # günlük max için nötr

    # Batı rüzgarı: Atlantik kökenli
    # → GFS batı Atlantik üzerinde iyi, ICON biraz soğuk
    'westerly_bias': {
        'ecmwf': -0.2,  # biraz soğuk tahmin
        'gfs': 0.0,
        'icon': +0.3,   # biraz soğuk (continental bias)
    },
}
```

**Sistematik fırsatlar:**
```python
# Örnek: Summer heat wave senaryosu
# ICON continental model → Batı Avrupa'da hafif soğuk bias
# GFS okyanusa yakın model → Atlantik sistemleri daha iyi temsil eder
# Sonuç: Yaz aylarında London marketlerinde ICON'u az ağırlıklandır

# Kış soğuk spell senaryosu
# ECMWF continental cold air → Doğu Avrupa'dan gelen soğuk harika tanır
# GFS bazen gecikmeli tepki → Kış aylarında London'da ECMWF > GFS
```

---

## 12. Öncelik Matrisi ve Uygulama Sırası

| Öncelik | Geliştirme | Etki Tahmini | Karmaşıklık | Süre |
|---------|-----------|-------------|-------------|------|
| 🔴 1 | Dinamik model ağırlıklandırma (§2) | +15–25% Sharpe | Orta | 1 hafta |
| 🔴 2 | Kalman bias estimator (§3) | +10–15% doğruluk | Orta | 3 gün |
| 🔴 3 | Bimodal tespit (§5) | Kötü trade'leri %10 azalt | Düşük | 2 gün |
| 🟡 4 | Sinyal kalitesi skoru (§4) | Pozisyon sıralamayı iyileştirir | Düşük | 2 gün |
| 🟡 5 | Çok boyutlu hata matrisi (§6) | Sürekli iyileştirme temeli | Yüksek | 1 hafta |
| 🟡 6 | Bootstrap CI (§9) | Boyut kalibrasyonu | Düşük | 1 gün |
| 🟢 7 | Portfolio VaR (§7) | Kuyruk riski yönetimi | Yüksek | 2 hafta |
| 🟢 8 | Çok kaynaklı settlement (§8) | Paris gibi problemleri çöz | Orta | 1 hafta |
| 🟢 9 | Kalibrasyon dashboard (§10) | Monitoring | Orta | 1 hafta |

**Acil uygulama (bu hafta):**
1. Bimodal tespit → hemen ekle, basit sklearn kodu
2. Kalman bias estimator → mevcut bias kodunu değiştir
3. Sinyal skoru → scanner.py'e entegre et

**Sonraki sprint:**
4. Dinamik model ağırlıklandırması → SQLite migration ile birlikte (hata takibi şart)

---

*Son güncelleme: 2026-04-22*

Sources:
- [EMOS/NGR Calibration Paper](https://rmets.onlinelibrary.wiley.com/doi/10.1002/qj.4701)
- [CRPS for Ensembles - scores library](https://scores.readthedocs.io/en/stable/tutorials/CRPS_for_Ensembles.html)
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [FuXi-ENS Machine Learning Ensemble](https://pmc.ncbi.nlm.nih.gov/articles/PMC12577690/)
