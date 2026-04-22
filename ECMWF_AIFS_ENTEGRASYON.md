# ECMWF AIFS Entegrasyon Rehberi

> **Hedef:** `main.py`'daki mevcut 5 model sistemine ECMWF AIFS'i 6. model olarak ekle.
> İki ayrı yer değişiyor: deterministik tahmin (blend) + ensemble (p10/p50/p90).
>
> **Ön bilgi:** ECMWF AIFS (Artificial Intelligence Forecasting System) 2025'te
> GraphCast ve Pangu-Weather'ı geçen en güncel AI modelidir. 51 ensemble üyesiyle
> Open-Meteo üzerinden ücretsiz erişilebilir, ticari kullanım kısıtı yoktur.

---

## Özet: Ne Değişiyor?

```
MEVCUT:  5 model deterministik blend  +  ICON(40) + ECMWF IFS(50) = 90 üye ensemble
SONRASI: 6 model deterministik blend  +  ICON(40) + ECMWF IFS(51) + AIFS(51) = 142 üye ensemble
```

**Toplam kod değişikliği:** `main.py`'da 4 bölüm, yaklaşık 15-20 satır.

---

## İçindekiler

1. [AIFS Hakkında Teknik Bilgi](#1-aifs-hakkında-teknik-bilgi)
2. [Değişiklik 1 — MODELS Sözlüğü](#2-değişiklik-1--models-sözlüğü)
3. [Değişiklik 2 — MODEL_WEIGHTS](#3-değişiklik-2--model_weights)
4. [Değişiklik 3 — get_weather() Deterministic Fetch](#4-değişiklik-3--get_weather-deterministic-fetch)
5. [Değişiklik 4 — get_ensemble() Ensemble Fetch](#5-değişiklik-4--get_ensemble-ensemble-fetch)
6. [Değişiklik 5 — blend_day() Uyumluluğu](#6-değişiklik-5--blend_day-uyumluluğu)
7. [Test Etme](#7-test-etme)
8. [Beklenen Etki](#8-beklenen-etki)
9. [Sorun Giderme](#9-sorun-giderme)

---

## 1. AIFS Hakkında Teknik Bilgi

| Özellik | Değer |
|---------|-------|
| Geliştirici | ECMWF (Avrupa Orta Menzilli Hava Tahminleri Merkezi) |
| Mimari | Graph Neural Network (deterministik) + Diffusion (ensemble) |
| Çözünürlük | 0.25° × 0.25° (~28 km) |
| Open-Meteo forecast API ID | `ecmwf_aifs025` |
| Open-Meteo ensemble API ID | `ecmwf_aifs_025` |
| Ensemble üye sayısı | **51** |
| Güncelleme sıklığı | 6 saatte bir (00Z, 06Z, 12Z, 18Z) |
| Tahmin ufku | 15 gün |
| Ticari kullanım | ✅ **Serbest** (CC-BY 4.0) |
| Maliyet | **Ücretsiz** (Open-Meteo API) |
| Doğruluk | 2025 itibarıyla GraphCast ve Pangu'yu geçti |

**Open-Meteo'daki iki farklı endpoint:**
```
Forecast API  → https://api.open-meteo.com/v1/forecast
               models=ecmwf_aifs025         (deterministik, tek değer)

Ensemble API  → https://ensemble-api.open-meteo.com/v1/ensemble
               models=ecmwf_aifs_025        (51 üye, olasılık dağılımı)
```

> **Not:** İki API'de model ID yazımı farklı:
> - Forecast: `ecmwf_aifs025` (alt çizgi yok ortada)
> - Ensemble: `ecmwf_aifs_025` (alt çizgi var ortada)

---

## 2. Değişiklik 1 — MODELS Sözlüğü

**Dosya:** `main.py`

**Mevcut kod:**
```python
MODELS = {
    "gfs":         "gfs_seamless",
    "ecmwf":       "ecmwf_ifs025",
    "icon":        "icon_seamless",
    "ukmo":        "ukmo_seamless",
    "meteofrance": "meteofrance_seamless",
}
```

**Yeni kod:**
```python
MODELS = {
    "gfs":         "gfs_seamless",
    "ecmwf":       "ecmwf_ifs025",
    "icon":        "icon_seamless",
    "ukmo":        "ukmo_seamless",
    "meteofrance": "meteofrance_seamless",
    "aifs":        "ecmwf_aifs025",          # ← YENİ SATIR
}
```

---

## 3. Değişiklik 2 — MODEL_WEIGHTS

**Dosya:** `main.py`

**Mevcut kod:**
```python
MODEL_WEIGHTS = {
    "ecmwf":       1.5,
    "icon":        1.8,
    "gfs":         1.0,
    "ukmo":        0.5,
    "meteofrance": 0.9,
}
```

**Yeni kod:**
```python
MODEL_WEIGHTS = {
    "ecmwf":       1.5,
    "icon":        1.8,
    "gfs":         1.0,
    "ukmo":        0.5,
    "meteofrance": 0.9,
    "aifs":        1.6,    # ← YENİ SATIR
                           # ECMWF altyapısına dayandığı için ecmwf'e yakın
                           # Dinamik ağırlık sistemi eklenince rolling RMSE ile ayarlanır
}
```

**Ağırlık seçimi gerekçesi:**
```
ECMWF IFS:  1.5  → Uzun süredir kanıtlanmış, temkinli yaklaşım
AIFS:       1.6  → IFS'den biraz yüksek — 2025 benchmark AIFS > IFS gösteriyor
                   ama henüz istasyon bazlı geçmiş verimiz yok → çok agresif artırma

İlerleyen haftalarda dinamik ağırlık sistemi (model improvement dokümanı §2)
bu değeri otomatik güncelleyecek.
```

---

## 4. Değişiklik 3 — get_weather() Deterministic Fetch

Bu bölüm AIFS'in **deterministik** (tek değer) tahminini 5 model blend'ine ekler.

**Dosya:** `main.py`

**Mevcut `get_weather()` yapısı:**
```python
@app.get("/api/weather")
async def get_weather(station: str, refresh: bool = False):
    # ... cache kontrolü ...
    s = STATIONS[station]
    base = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m"
        f"&timezone={s['tz']}&forecast_days=3"
    )

    async def fetch_model(client: httpx.AsyncClient, model_id: str):
        async with _openmeteo_sem:
            return await client.get(base + f"&models={model_id}")

    async with httpx.AsyncClient(timeout=15) as client:
        tasks = [fetch_model(client, mid) for mid in MODELS.values()]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
```

**Bu kısımda değişiklik GEREKMİYOR** — çünkü:
- `MODELS.values()` döngüsü otomatik olarak yeni `"ecmwf_aifs025"` değerini alır
- `fetch_model()` her model için aynı URL yapısını kullanır
- `MODELS` sözlüğüne eklediğin satır yeterli

**Ancak sonuçları işleyen kısma dikkat:**

```python
# Mevcut response işleme (get_weather içinde):
# Her response, MODELS dict'indeki sırayla eşleşiyor olmalı
# Bunu kontrol et:

results = {}
for (name, model_id), resp in zip(MODELS.items(), responses):
    if isinstance(resp, Exception):
        print(f"[{name}] Hata: {resp}")
        continue
    if resp.status_code != 200:
        print(f"[{name}] HTTP {resp.status_code}")
        continue
    data = resp.json()
    results[name] = parse_model_response(data, name)  # veya nasıl parse ediyorsa
```

**Eğer böyle bir zip yapısı varsa** `"aifs"` anahtarı otomatik dahil olur.
**Eğer hardcoded key listesi varsa** `"aifs"` eklenmeli:

```python
# Hardcoded key kullanımı varsa bul ve "aifs" ekle:
# Örnek arama komutu (VPS'te):
# grep -n '"ecmwf"\|"icon"\|"gfs"\|"ukmo"\|"meteofrance"' main.py

# Bulduğun her yerde "aifs" de olduğundan emin ol
```

---

## 5. Değişiklik 4 — get_ensemble() Ensemble Fetch

Bu en kritik değişiklik. Ensemble, p10/p50/p90 hesaplayarak trading kararlarını doğrudan etkiliyor.

**Dosya:** `main.py`

**Mevcut `get_ensemble()` yapısı:**
```python
@app.get("/api/ensemble")
async def get_ensemble(station: str):
    s = STATIONS[station]
    base_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m"
        f"&timezone={s['tz']}&forecast_days=3"
    )
    # ICON (40 üye) + ECMWF IFS (50 üye) → toplam 90 üye
    ENSEMBLE_MODELS = ["icon_seamless", "ecmwf_ifs025"]
```

**Yeni kod — AIFS ensemble ekle:**
```python
@app.get("/api/ensemble")
async def get_ensemble(station: str):
    s = STATIONS[station]
    base_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m"
        f"&timezone={s['tz']}&forecast_days=3"
    )
    # ICON (40 üye) + ECMWF IFS (51 üye) + AIFS (51 üye) → toplam 142 üye
    ENSEMBLE_MODELS = ["icon_seamless", "ecmwf_ifs025", "ecmwf_aifs_025"]  # ← "ecmwf_aifs_025" EKLENDİ
```

**Dikkat: Ensemble API'de model ID `ecmwf_aifs_025` (alt çizgi var)**
Forecast API'deki `ecmwf_aifs025` ile karıştırma.

**Tam ensemble fetch fonksiyonu (eğer yeniden yazman gerekirse):**
```python
@app.get("/api/ensemble")
async def get_ensemble(station: str):
    s = STATIONS[station]

    ENSEMBLE_MODELS = {
        "icon":  "icon_seamless",
        "ecmwf": "ecmwf_ifs025",
        "aifs":  "ecmwf_aifs_025",   # ← YENİ
    }

    ENSEMBLE_MEMBER_COUNTS = {
        "icon":  40,
        "ecmwf": 51,
        "aifs":  51,   # ← YENİ
    }

    base_url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={s['lat']}&longitude={s['lon']}"
        f"&hourly=temperature_2m"
        f"&timezone={s['tz']}&forecast_days=3"
    )

    async def fetch_ensemble_model(client, name, model_id):
        async with _openmeteo_sem:
            resp = await client.get(base_url + f"&models={model_id}", timeout=20)
            if resp.status_code != 200:
                print(f"[ensemble/{name}] HTTP {resp.status_code}: {resp.text[:200]}")
                return name, None
            return name, resp.json()

    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_ensemble_model(client, name, mid)
            for name, mid in ENSEMBLE_MODELS.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Tüm üyeleri birleştir
    all_members: list[float] = []
    per_model_members: dict[str, list[float]] = {}

    for result in results:
        if isinstance(result, Exception):
            print(f"[ensemble] Exception: {result}")
            continue
        name, data = result
        if data is None:
            continue

        hourly = data.get("hourly", {})
        member_keys = [k for k in hourly.keys() if k.startswith("temperature_2m_member")]

        temps = []
        for key in member_keys:
            vals = hourly[key]
            # Hedef tarihin maks sıcaklığını bul
            # (mevcut parse mantığın ne ise onu kullan)
            daily_max = max(v for v in vals if v is not None)
            temps.append(daily_max)

        per_model_members[name] = temps
        all_members.extend(temps)
        print(f"[ensemble/{name}] {len(temps)} üye yüklendi")

    if not all_members:
        raise HTTPException(500, "Ensemble verisi alınamadı")

    all_members_sorted = sorted(all_members)
    n = len(all_members_sorted)

    def pct(p: float) -> float:
        idx = p / 100 * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return round(all_members_sorted[lo] + (idx - lo) * (all_members_sorted[hi] - all_members_sorted[lo]), 1)

    return {
        "p10": pct(10), "p25": pct(25), "p50": pct(50),
        "p75": pct(75), "p90": pct(90),
        "member_count": n,
        "per_model": {k: len(v) for k, v in per_model_members.items()},
    }
```

---

## 6. Değişiklik 5 — blend_day() Uyumluluğu

`blend_day()` fonksiyonu `model_maxes` dict'ini şöyle kullanıyor:

```python
def blend_day(models_data: dict, horizon: int = 1) -> dict:
    model_maxes = {
        name: v["max_temp"]
        for name, v in models_data.items()
        if v.get("max_temp") is not None
    }
    total_w = sum(MODEL_WEIGHTS.get(k, 1.0) for k in model_maxes)
    blend = round(
        sum(v * MODEL_WEIGHTS.get(k, 1.0) for k, v in model_maxes.items()) / total_w, 1
    )
```

**Bu fonksiyonda değişiklik GEREKMİYOR** — çünkü:
- `MODEL_WEIGHTS.get(k, 1.0)` → bilinen anahtar için tanımlı ağırlığı kullanır
- `"aifs"` anahtarını `MODEL_WEIGHTS`'e eklediğin için 1.6 ağırlığını alacak
- Bilinmeyen anahtarlar zaten `1.0` fallback'e düşüyor

**Kontrol:** `blend_day()`'in `models_data` argümanını nereden aldığını bul:
```bash
# VPS'te çalıştır:
grep -n "blend_day" main.py
```

`models_data` `get_weather()` sonucundan geliyorsa ve MODELS dict üzerinden
dolduruluyorsa `"aifs"` verisi otomatik dahil olur.

---

## 7. Test Etme

### Adım 1 — API'yi doğrudan test et (deploy öncesi lokal)

```python
# test_aifs.py — VPS'e yükle veya lokal çalıştır
import httpx
import asyncio

async def test_aifs_forecast():
    """ECMWF AIFS forecast API'nin çalıştığını doğrula."""
    # London City Airport koordinatları
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=51.5074&longitude=-0.0278"
        "&hourly=temperature_2m"
        "&timezone=Europe/London"
        "&forecast_days=3"
        "&models=ecmwf_aifs025"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            temps = data["hourly"]["temperature_2m"][:24]
            print(f"İlk 24 saatlik sıcaklık: {temps}")
            print(f"Yarınki maks tahmini: {max(temps):.1f}°C")
        else:
            print(f"Hata: {r.text}")

async def test_aifs_ensemble():
    """ECMWF AIFS ensemble API'nin çalıştığını doğrula."""
    url = (
        "https://ensemble-api.open-meteo.com/v1/ensemble"
        "?latitude=51.5074&longitude=-0.0278"
        "&hourly=temperature_2m"
        "&timezone=Europe/London"
        "&forecast_days=2"
        "&models=ecmwf_aifs_025"  # Dikkat: alt çizgi var
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        print(f"\nEnsemble Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            keys = [k for k in data["hourly"] if "member" in k]
            print(f"Ensemble üye sayısı: {len(keys)}")
            print(f"Örnek üye anahtarlar: {keys[:3]}")
        else:
            print(f"Ensemble Hata: {r.text[:500]}")

asyncio.run(test_aifs_forecast())
asyncio.run(test_aifs_ensemble())
```

```bash
# VPS'te çalıştır:
cd /root/weather
python3 test_aifs.py
```

**Beklenen çıktı:**
```
Status: 200
İlk 24 saatlik sıcaklık: [13.2, 13.0, 12.8, ...]
Yarınki maks tahmini: 15.3°C

Ensemble Status: 200
Ensemble üye sayısı: 51
Örnek üye anahtarlar: ['temperature_2m_member01', 'temperature_2m_member02', 'temperature_2m_member03']
```

---

### Adım 2 — Blend davranışını kontrol et

```python
# test_blend.py
import httpx
import asyncio

async def test_blend_with_aifs():
    """AIFS blend'e dahil oldu mu?"""
    async with httpx.AsyncClient(timeout=30) as client:
        # Kendi API'ni çağır (botu başlatmadan önce lokal test)
        r = await client.get("http://localhost:8001/api/weather?station=london")
        if r.status_code == 200:
            data = r.json()
            # Blend sonucunda "aifs" var mı?
            if "models" in data:
                print("Model verileri:", list(data["models"].keys()))
                if "aifs" in data["models"]:
                    print(f"✅ AIFS blend'e dahil: {data['models']['aifs']}")
                else:
                    print("❌ AIFS blend'e dahil değil — MODELS dict kontrolü gerekiyor")
        else:
            print(f"API hatası: {r.status_code}")

asyncio.run(test_blend_with_aifs())
```

---

### Adım 3 — Ensemble üye sayısını doğrula

```python
async def test_ensemble_member_count():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("http://localhost:8001/api/ensemble?station=london")
        if r.status_code == 200:
            data = r.json()
            print(f"Toplam üye: {data.get('member_count')}")
            print(f"Model başına:", data.get('per_model'))
            # Beklenen: {'icon': 40, 'ecmwf': 51, 'aifs': 51} → toplam 142
            expected = 142
            actual = data.get('member_count', 0)
            if actual >= expected:
                print(f"✅ Üye sayısı doğru: {actual}")
            else:
                print(f"⚠️  Üye sayısı eksik: {actual} (beklenen: {expected})")
```

---

## 8. Beklenen Etki

### Deterministik Blend

```
Mevcut 5 model ortalaması:
  Örnek: GFS=14.1, ECMWF=13.8, ICON=14.3, UKMO=13.5, MeteoFrance=14.0
  Ağırlıklı blend = 13.97°C

AIFS eklendikten sonra (AIFS tahmini 13.6°C varsayalım):
  6 model blend = 13.89°C (biraz daha soğuk)
  
Etki: AIFS genellikle ECMWF IFS ile yakın tahmin yapar (aynı altyapı).
Büyük sapma olmaz ama ekstrem senaryolarda daha dengeli.
```

### Ensemble Olasılık Dağılımı

```
ÖNCE (90 üye):
  p10=12.0°C, p50=13.8°C, p90=15.5°C
  Spread: 3.5°C

SONRA (142 üye — 52 üye artış):
  p10=12.2°C, p50=13.7°C, p90=15.1°C  (daha dar aralık)
  Spread: 2.9°C (daha güvenilir)

Neden daha iyi?
  - 52 ek AIFS üyesi → istatistiksel olarak daha kararlı percentile
  - AIFS ve IFS bağımsız farklı başlangıç koşullarıyla → gerçek belirsizliği daha iyi yakalar
  - Dar spread → daha az "HIGH uncertainty" sınıflandırması → daha fazla işlem fırsatı
```

### Scanner Performansı

```
Beklenen iyileşme:
  - Yanlış "yüksek belirsizlik" sınıflandırmaları azalır
  - p90 daha gerçekçi → NO trade güvenlik marjı daha sağlıklı
  - Mode_pct (konsensüs oranı) daha anlamlı → 142 üyenin %X'i
```

---

## 9. Sorun Giderme

### Sorun: `HTTP 400 Bad Request` — `ecmwf_aifs025`

```
Hata: {"reason":"Unknown model: ecmwf_aifs025"}

Çözüm: Model adı doğru mu kontrol et
  Forecast API'de: "ecmwf_aifs025"  (alt çizgi YOK)
  Ensemble API'de: "ecmwf_aifs_025" (alt çizgi VAR)

Test et:
  curl "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.1&hourly=temperature_2m&models=ecmwf_aifs025"
```

### Sorun: `HTTP 429 Too Many Requests`

```
Mevcut semaphore limiti: 6 eş zamanlı istek
6 model olunca paralel istek sayısı artıyor.

Çözüm: Semaphore limitini 5'e düşür veya AIFS isteğini seri yap:

# main.py'da:
_openmeteo_sem = asyncio.Semaphore(5)  # 6 → 5

# Veya AIFS'i ayrı, biraz gecikmeli çek:
await asyncio.sleep(0.5)  # AIFS öncesi 500ms bekle
```

### Sorun: AIFS verisi `None` geliyor

```python
# blend_day() içinde modelin None dönmesi silently ignore edilir:
model_maxes = {
    name: v["max_temp"]
    for name, v in models_data.items()
    if v.get("max_temp") is not None   # ← None gelirse "aifs" dahil olmaz ama hata vermez
}
# Bu beklenen davranış — API geçici down olursa blend 5 modelle devam eder.
```

### Sorun: Ensemble üye sayısı 51 değil 52 geliyor

```
ECMWF ensemble 50 deterministik + 1 kontrol üyesi = 51 toplam
AIFS de benzer yapı → 51

Bazen API 50 veya 52 dönebilir — bu normaldir, sabit sayıya bağlı kalma.
```

### Sorun: `temperature_2m_max` daily variable çalışmıyor

```
AIFS ensemble API hourly veri sunuyor, daily değil.
Mevcut kod zaten hourly data'dan manuel maks hesaplıyor:

daily_max = max(v for v in hourly_temps if v is not None)

Bu yaklaşım AIFS için de çalışır — değiştirme.
```

---

## Deployment

```bash
# 1. Değişiklikleri yap (yukarıdaki 4 bölüm)
# 2. Test scriptini çalıştır
cd /root/weather
python3 test_aifs.py

# 3. Botu yeniden başlat
sudo systemctl restart weather-bot
# veya
pm2 restart weather

# 4. Log izle
sudo journalctl -u weather-bot -f --since "1 min ago"

# 5. API endpoint'i kontrol et
curl "http://localhost:8001/api/ensemble?station=london" | python3 -m json.tool
# member_count: 142 görünmeli
```

---

## Özet Kontrol Listesi

```
[ ] MODELS dict'e "aifs": "ecmwf_aifs025" eklendi
[ ] MODEL_WEIGHTS dict'e "aifs": 1.6 eklendi
[ ] get_ensemble() içinde ENSEMBLE_MODELS listesine "ecmwf_aifs_025" eklendi
[ ] test_aifs.py çalıştırıldı, status 200 alındı
[ ] Ensemble test: member_count ≥ 140 görüldü
[ ] Bot yeniden başlatıldı, log'da hata yok
[ ] /api/weather?station=london çalışıyor
[ ] /api/ensemble?station=london çalışıyor, per_model'de "aifs" görünüyor
```

---

*Son güncelleme: 2026-04-22*
*Kaynak: Open-Meteo Ensemble API Docs · ECMWF AIFS technical documentation*
