# Weather Blend Backtest

Model + Polymarket edge stratejisinin geçmişte kar/zarar analizi.

## Faz 0 — Veri Toplama

3 veri seti gerekli. Hepsini **VPS'te** çalıştır (bu makinede internet kısıtlı):

```bash
cd /root/weather
source venv/bin/activate
cd backtest
```

### 1. Historical model forecasts (Open-Meteo Previous Runs API)

```bash
python3 fetch_forecasts.py --days 60
```

Her istasyon × her gün × her model için day1/day2/day3 horizon tahminleri.
~2 dakika sürer, rate limit dostu (0.5s gecikme).

### 2. Historical Polymarket markets

```bash
python3 fetch_polymarket.py --days 60
```

Her istasyon × her gün için resolved market bucket'ları.
~5 dakika sürer (gün×istasyon = 360 istek).

### 3. Actual temperatures (METAR)

```bash
python3 fetch_actuals.py --days 30
```

METAR API son 30 gün limitli. Daha eski için ogimet entegrasyonu yapılacak.

## Çıktı Dosyaları

```
backtest/data/
├── forecasts.json     # Historical model predictions
├── polymarket.json    # Historical bucket prices + outcomes
└── actuals.json       # Observed max temperatures
```

## Sonraki Fazlar

- **Faz 1**: `engine.py` — Strateji simülasyonu
- **Faz 2**: `calibration.py` — Kalibrasyon analizi
- **Faz 3**: `optimizer.py` — Parametre grid search
- **Faz 4**: `risk.py` — Risk analizi
- **Faz 5**: Nihai rapor ve go/no-go kararı

## Veri Boyutu

- 6 istasyon × 60 gün = 360 gün kaydı
- Forecasts: ~500KB JSON
- Polymarket: ~2MB JSON (6 istasyon × ~15 bucket)
- Actuals: ~50KB JSON

Toplam ~3MB, git'e commit edilebilir.

## Sorun Giderme

**Open-Meteo 429 Rate Limit:** Sleep süresini 1s'ye çıkar.

**Polymarket boş sonuç:** Market yoksa normaldir (bazı tarihlerde istasyon için market açılmamış).

**METAR 6 gözlem altı:** Havalimanı erişim sorunu veya reporting aralığı, o gün atlanır.
