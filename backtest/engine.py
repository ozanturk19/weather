"""
Faz 1 — Backtest Engine

forecasts.json + polymarket.json + actuals.json üçlüsünü birleştirerek:
1. Model doğruluk metrikleri (MAE, RMSE, Bias — her istasyon × horizon)
2. Bucket olasılık kalibrasyonu (model prob vs gerçekleşme oranı)
3. Edge simülasyonu — model fiyat vs PM fiyat, $100 sabit pozisyon
4. Per-model ağırlık önerileri

Kullanım:
    python3 engine.py
    python3 engine.py --station eglc
    python3 engine.py --horizon 1
    python3 engine.py --report full

Çıktı: terminal raporu + backtest/data/results.json
"""

import argparse
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

DATA = Path(__file__).parent / "data"
MODELS = ["gfs", "ecmwf", "icon", "ukmo", "meteofrance"]
STATIONS = ["eglc", "ltac", "limc", "ltfm", "lemd", "lfpg", "eham", "eddm", "epwa", "efhk"]
STATION_NAMES = {
    "eglc": "EGLC · Londra",
    "ltac": "LTAC · Ankara",
    "limc": "LIMC · Milano",
    "ltfm": "LTFM · İstanbul",
    "lemd": "LEMD · Madrid",
    "lfpg": "LFPG · Paris",
    "eham": "EHAM · Amsterdam",
    "eddm": "EDDM · Münih",
    "epwa": "EPWA · Varşova",
    "efhk": "EFHK · Helsinki",
}
# Model ağırlıkları (mevcut production değerleri — backtest sonrası güncellenecek)
CURRENT_WEIGHTS = {
    "ecmwf": 2.0, "icon": 1.0, "gfs": 0.8, "ukmo": 0.9, "meteofrance": 1.0,
}

# Horizon etiketleri: day1=D+0 (aynı gün run), day2=D+1, day3=D+2
HORIZON_MAP = {"day1": 0, "day2": 1, "day3": 2}


def load_data():
    fc = json.loads((DATA / "forecasts.json").read_text())
    pm = json.loads((DATA / "polymarket.json").read_text())
    ac = json.loads((DATA / "actuals.json").read_text())
    return fc, pm, ac


def weighted_blend(model_preds: dict, weights: dict) -> float | None:
    """Ağırlıklı ortalama blend — outlier filtresi yok (backtest için saf blend)."""
    vals = {k: v for k, v in model_preds.items() if v is not None}
    if not vals:
        return None
    total_w = sum(weights.get(k, 1.0) for k in vals)
    return sum(v * weights.get(k, 1.0) for k, v in vals.items()) / total_w


# ── 1. Model Doğruluk Analizi ─────────────────────────────────────────────────

def accuracy_analysis(fc: dict, ac: dict, station_filter=None, horizon_filter=None):
    """
    Her istasyon × her model × her horizon için MAE, RMSE, Bias hesapla.
    Ayrıca ağırlıklı blend'i de değerlendir.
    """
    # {station: {horizon_key: {model: [errors]}}}
    errors = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for station, days in fc.items():
        if station_filter and station != station_filter:
            continue
        actuals = ac.get(station, {})

        for date_str, horizons in days.items():
            actual = actuals.get(date_str, {}).get("max_temp")
            if actual is None:
                continue

            for horizon_key, model_preds in horizons.items():
                if horizon_filter is not None and HORIZON_MAP.get(horizon_key) != horizon_filter:
                    continue

                # Her model için hata
                for model in MODELS:
                    pred = model_preds.get(model)
                    if pred is not None:
                        errors[station][horizon_key][model].append(pred - actual)

                # Blend hatası
                blend = weighted_blend(model_preds, CURRENT_WEIGHTS)
                if blend is not None:
                    errors[station][horizon_key]["blend"].append(blend - actual)

    # Metrik hesaplama
    results = {}
    for station, horizons in errors.items():
        results[station] = {}
        for horizon_key, models in horizons.items():
            results[station][horizon_key] = {}
            for model_name, errs in models.items():
                if not errs:
                    continue
                n = len(errs)
                mae  = sum(abs(e) for e in errs) / n
                rmse = math.sqrt(sum(e**2 for e in errs) / n)
                bias = sum(errs) / n
                results[station][horizon_key][model_name] = {
                    "n": n, "mae": round(mae, 3),
                    "rmse": round(rmse, 3), "bias": round(bias, 3),
                }

    return results


# ── 2. Bucket Olasılık Kalibrasyonu ──────────────────────────────────────────

def bucket_calibration(fc: dict, pm: dict, ac: dict):
    """
    Model tahminine göre bucket olasılığı vs gerçekleşme oranı.
    Bins: [0-20%), [20-40%), [40-60%), [60-80%), [80-100%]
    """
    bins = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
    bin_data = {b: {"predicted": [], "realized": []} for b in bins}

    for station in STATIONS:
        actuals = ac.get(station, {})
        for date_str, day_pm in pm.get(station, {}).items():
            actual = actuals.get(date_str, {}).get("max_temp")
            if actual is None:
                continue
            buckets = day_pm.get("buckets", [])
            if not buckets:
                continue

            fc_day = fc.get(station, {}).get(date_str, {})
            model_preds = fc_day.get("day2", {})  # D+1 tahmin
            if not model_preds:
                continue
            blend = weighted_blend(model_preds, CURRENT_WEIGHTS)
            if blend is None:
                continue

            # Gaussian dağılım (blend mean, model std)
            vals = [v for v in model_preds.values() if v is not None]
            if len(vals) < 2:
                continue
            std = max(math.sqrt(sum((v - blend)**2 for v in vals) / len(vals)), 0.5)

            for b in buckets:
                t = b.get("threshold")
                if t is None:
                    continue
                # Gaussian prob
                if b.get("is_below"):
                    p = _gaussian_cdf(t + 0.5, blend, std)
                elif b.get("is_above"):
                    p = 1 - _gaussian_cdf(t - 0.5, blend, std)
                else:
                    p = max(0, _gaussian_cdf(t + 0.5, blend, std) - _gaussian_cdf(t - 0.5, blend, std))

                p = max(0.03, min(0.97, p))
                realized = _bucket_outcome(b, actual)
                if realized is None:
                    continue

                for bin_range in bins:
                    if bin_range[0] <= p < bin_range[1] or (bin_range[1] == 1.0 and p == 1.0):
                        bin_data[bin_range]["predicted"].append(p)
                        bin_data[bin_range]["realized"].append(int(realized))
                        break

    calibration = {}
    for bin_range, d in bin_data.items():
        n = len(d["predicted"])
        if n == 0:
            continue
        avg_pred = sum(d["predicted"]) / n
        avg_real = sum(d["realized"]) / n
        calibration[f"{int(bin_range[0]*100)}-{int(bin_range[1]*100)}%"] = {
            "n": n,
            "avg_predicted": round(avg_pred, 3),
            "avg_realized":  round(avg_real, 3),
            "overconfident": round(avg_pred - avg_real, 3),
        }
    return calibration


# ── 3. Edge Simülasyonu ───────────────────────────────────────────────────────

def edge_simulation(fc: dict, pm: dict, ac: dict, min_edge: float = 0.08):
    """
    Her gün × her istasyon × her bucket için:
    - Model prob hesapla (D+1 blend Gaussian)
    - PM fiyatı ile karşılaştır → edge
    - edge > min_edge ise pozisyon aç ($100 sabit)
    - Gerçek sonuca göre P&L hesapla
    """
    trades = []

    for station in STATIONS:
        actuals = ac.get(station, {})
        for date_str, day_pm in pm.get(station, {}).items():
            actual = actuals.get(date_str, {}).get("max_temp")
            if actual is None:
                continue
            winning_bucket = day_pm.get("winning_bucket")
            buckets = day_pm.get("buckets", [])
            if not buckets:
                continue

            fc_day = fc.get(station, {}).get(date_str, {})
            model_preds = fc_day.get("day2", {})  # D+1 tahmin
            if not model_preds:
                continue
            blend = weighted_blend(model_preds, CURRENT_WEIGHTS)
            if blend is None:
                continue

            vals = [v for v in model_preds.values() if v is not None]
            if len(vals) < 2:
                continue
            std = max(math.sqrt(sum((v - blend)**2 for v in vals) / len(vals)), 0.5)

            for b in buckets:
                t = b.get("threshold")
                pm_yes = b.get("yes_price")
                if t is None or pm_yes is None:
                    continue

                # Model prob (Gaussian)
                if b.get("is_below"):
                    model_p = _gaussian_cdf(t + 0.5, blend, std)
                elif b.get("is_above"):
                    model_p = 1 - _gaussian_cdf(t - 0.5, blend, std)
                else:
                    model_p = max(0, _gaussian_cdf(t + 0.5, blend, std) - _gaussian_cdf(t - 0.5, blend, std))

                model_p = max(0.03, min(0.97, model_p))
                edge = model_p - pm_yes

                if abs(edge) < min_edge:
                    continue

                side = "YES" if edge > 0 else "NO"
                price = pm_yes if side == "YES" else (1 - pm_yes)
                if price < 0.02:   # fiyat sıfıra çok yakınsa skip (resolved bucket)
                    continue
                realized = _bucket_outcome(b, actual)
                if realized is None:
                    continue

                win = (side == "YES" and realized) or (side == "NO" and not realized)
                pnl = round((100 / price) - 100, 2) if win else -100

                trades.append({
                    "station":   station,
                    "date":      date_str,
                    "bucket":    b.get("title", str(t)),
                    "side":      side,
                    "model_p":   round(model_p, 3),
                    "pm_price":  round(price, 3),
                    "edge":      round(abs(edge), 3),
                    "blend":     round(blend, 1),
                    "actual":    actual,
                    "win":       win,
                    "pnl":       pnl,
                })

    return trades


# ── 4. Per-Model Ağırlık Önerisi ──────────────────────────────────────────────

def weight_recommendations(accuracy: dict, horizon_key: str = "day2"):
    """
    MAE bazlı ters ağırlık önerisi — düşük hata → yüksek ağırlık.
    Normalize et: toplam = mevcut ağırlıkların toplamı kadar.
    """
    recs = {}
    for station, horizons in accuracy.items():
        h_data = horizons.get(horizon_key, {})
        model_maes = {m: h_data[m]["mae"] for m in MODELS if m in h_data}
        if len(model_maes) < 2:
            continue
        # Ters MAE → ham ağırlık
        inv_mae = {m: 1 / mae for m, mae in model_maes.items() if mae > 0}
        total_inv = sum(inv_mae.values())
        target_total = sum(CURRENT_WEIGHTS.values())
        normalized = {m: round(v / total_inv * target_total, 2) for m, v in inv_mae.items()}
        recs[station] = normalized

    return recs


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def _erf(x: float) -> float:
    a1, a2, a3, a4, a5, p = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429, 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1 / (1 + p * x)
    return sign * (1 - (((((a5*t+a4)*t)+a3)*t+a2)*t+a1)*t*math.exp(-x*x))


def _gaussian_cdf(x: float, mean: float, std: float) -> float:
    return 0.5 * (1 + _erf((x - mean) / (std * math.sqrt(2))))


def _bucket_outcome(bucket: dict, actual: float) -> bool | None:
    """Bucket YES kazandı mı? WU/METAR tam °C yuvarlama varsayımı."""
    t = bucket.get("threshold")
    if t is None:
        return None
    rounded = round(actual)
    if bucket.get("is_below"):
        return rounded <= t
    if bucket.get("is_above"):
        return rounded >= t
    return rounded == t


# ── Rapor Yazdırma ─────────────────────────────────────────────────────────────

def print_accuracy_report(accuracy: dict, horizon_key: str = "day2"):
    h_label = {0: "D+0", 1: "D+1", 2: "D+2"}[HORIZON_MAP[horizon_key]]
    print(f"\n{'='*72}")
    print(f"MODEL DOĞRULUK RAPORU — {h_label} (forecasts[{horizon_key}])")
    print(f"{'='*72}")
    print(f"{'':10} {'GFS':>8} {'ECMWF':>8} {'ICON':>8} {'UKMO':>8} {'MF':>8} {'BLEND':>8}")
    print(f"{'':10} {'MAE':>8} {'MAE':>8} {'MAE':>8} {'MAE':>8} {'MAE':>8} {'MAE':>8}")
    print("-" * 72)

    all_maes = defaultdict(list)
    for station in STATIONS:
        h_data = accuracy.get(station, {}).get(horizon_key, {})
        if not h_data:
            continue
        row = f"{STATION_NAMES[station][:10]:<10}"
        for model in [*MODELS, "blend"]:
            d = h_data.get(model)
            if d:
                row += f"  {d['mae']:>6.2f}"
                all_maes[model].append(d["mae"])
            else:
                row += f"  {'—':>6}"
        # Bias
        blend_d = h_data.get("blend")
        if blend_d:
            bias_str = f"  bias={blend_d['bias']:+.2f}"
            row += bias_str
        print(row)

    print("-" * 72)
    avg_row = f"{'ORTALAMA':<10}"
    for model in [*MODELS, "blend"]:
        maes = all_maes[model]
        if maes:
            avg_row += f"  {sum(maes)/len(maes):>6.2f}"
        else:
            avg_row += f"  {'—':>6}"
    print(avg_row)


def print_edge_report(trades: list):
    if not trades:
        print("\n⚠ Hiç trade bulunamadı — min_edge eşiğini düşür")
        return

    wins  = [t for t in trades if t["win"]]
    total_pnl = sum(t["pnl"] for t in trades)
    wr    = len(wins) / len(trades) * 100

    print(f"\n{'='*72}")
    print(f"EDGE SİMÜLASYONU — $100 sabit pozisyon, min_edge=%8")
    print(f"{'='*72}")
    print(f"Toplam trade : {len(trades)}")
    print(f"Kazanan      : {len(wins)}  ({wr:.1f}%)")
    print(f"Kaybeden     : {len(trades)-len(wins)}")
    print(f"Toplam P&L   : ${total_pnl:+.0f}")
    print(f"Trade başı   : ${total_pnl/len(trades):+.1f}")
    print()

    # İstasyon bazlı özet
    by_station = defaultdict(list)
    for t in trades:
        by_station[t["station"]].append(t)

    print(f"{'İstasyon':<12} {'Trade':>6} {'Win%':>6} {'P&L':>8} {'AvgEdge':>8}")
    print("-" * 44)
    for station in STATIONS:
        ts = by_station.get(station, [])
        if not ts:
            continue
        w = [t for t in ts if t["win"]]
        pnl = sum(t["pnl"] for t in ts)
        avg_edge = sum(t["edge"] for t in ts) / len(ts)
        print(f"{STATION_NAMES[station][:12]:<12} {len(ts):>6} {len(w)/len(ts)*100:>5.0f}% {pnl:>+8.0f} {avg_edge*100:>7.0f}%")


def print_calibration_report(cal: dict):
    print(f"\n{'='*72}")
    print("OLASILİK KALİBRASYONU (D+1 Gaussian blend)")
    print(f"{'='*72}")
    print(f"{'Bin':<12} {'N':>5} {'Model%':>8} {'Gerçek%':>9} {'Fark':>8}")
    print("-" * 44)
    for bin_label, d in sorted(cal.items()):
        diff = d["overconfident"]
        flag = "⬆ over" if diff > 0.05 else ("⬇ under" if diff < -0.05 else "✓")
        print(f"{bin_label:<12} {d['n']:>5} {d['avg_predicted']*100:>7.1f}% {d['avg_realized']*100:>8.1f}% {diff*100:>+7.1f}% {flag}")


def print_weight_recommendations(recs: dict):
    print(f"\n{'='*72}")
    print("ÖNERİLEN MODEL AĞIRLIKLARI (MAE⁻¹ normalize, D+1)")
    print(f"{'='*72}")
    print(f"{'':12} {'GFS':>7} {'ECMWF':>7} {'ICON':>7} {'UKMO':>7} {'MF':>7}")
    print(f"{'Mevcut':12} " + " ".join(f"{CURRENT_WEIGHTS.get(m,1.0):>7.1f}" for m in MODELS))
    print("-" * 48)
    for station, weights in recs.items():
        row = f"{STATION_NAMES[station][:12]:<12} "
        row += " ".join(f"{weights.get(m, 0):>7.2f}" for m in MODELS)
        print(row)


# ── Ana Giriş ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weather backtest engine")
    parser.add_argument("--station",  default=None, help="Tek istasyon filtrele (örn: eglc)")
    parser.add_argument("--horizon",  type=int, default=None, help="Horizon filtrele: 0/1/2")
    parser.add_argument("--min-edge", type=float, default=0.08, help="Min edge eşiği (0.08 = %8)")
    parser.add_argument("--report",   default="full", choices=["accuracy", "edge", "calibration", "weights", "full"])
    args = parser.parse_args()

    print("Veriler yükleniyor...")
    fc, pm, ac = load_data()

    total_ac = sum(len(v) for v in ac.values())
    total_pm = sum(
        sum(1 for v in days.values() if v.get("winning_bucket") is not None)
        for days in pm.values()
    )
    print(f"✓ forecasts: {sum(len(v) for v in fc.values())} gün-istasyon")
    print(f"✓ polymarket: {total_pm} resolved market")
    print(f"✓ actuals: {total_ac} gün-istasyon")

    # Kesişim özeti
    common = 0
    for s in STATIONS:
        f_dates = set(fc.get(s, {}).keys())
        p_dates = {d for d, v in pm.get(s, {}).items() if v.get("winning_bucket") is not None}
        a_dates = set(ac.get(s, {}).keys())
        common += len(f_dates & p_dates & a_dates)
    print(f"✓ üç kaynak kesişim: {common} gün-istasyon")

    # Doğruluk analizi
    accuracy = accuracy_analysis(fc, ac, station_filter=args.station, horizon_filter=args.horizon)
    if args.report in ("accuracy", "full"):
        for hk in (["day1", "day2", "day3"] if args.horizon is None else
                   [next(k for k,v in HORIZON_MAP.items() if v == args.horizon)]):
            print_accuracy_report(accuracy, horizon_key=hk)

    # Kalibrasyon
    if args.report in ("calibration", "full"):
        cal = bucket_calibration(fc, pm, ac)
        print_calibration_report(cal)

    # Edge simülasyonu
    if args.report in ("edge", "full"):
        trades = edge_simulation(fc, pm, ac, min_edge=args.min_edge)
        print_edge_report(trades)

    # Ağırlık önerileri
    if args.report in ("weights", "full"):
        recs = weight_recommendations(accuracy)
        print_weight_recommendations(recs)

    # Sonuçları kaydet
    results = {
        "accuracy":      accuracy,
        "weight_recs":   weight_recommendations(accuracy),
        "edge_summary": {
            "total_trades": 0, "win_rate": 0, "total_pnl": 0,
        },
    }
    if args.report in ("edge", "full"):
        trades = edge_simulation(fc, pm, ac, min_edge=args.min_edge)
        wins = [t for t in trades if t["win"]]
        results["edge_summary"] = {
            "total_trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 3) if trades else 0,
            "total_pnl": sum(t["pnl"] for t in trades),
            "trades": trades,
        }
    (DATA / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n✅ Sonuçlar kaydedildi: {DATA / 'results.json'}")


if __name__ == "__main__":
    main()
