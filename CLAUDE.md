# Weather Dashboard — VPS Geliştirme Ortamı

## Deployment Kuralı (KRİTİK)
VPS'te doğrudan dosya değiştirme. Tüm değişiklikler git üzerinden:
1. Lokal Mac'te düzenle → commit → push
2. VPS'te: cd /root/weather && ./deploy.sh

Deploy script (./deploy.sh) iki şeyi engeller:
- Uncommitted değişiklik varsa → abort
- Push edilmemiş commit varsa → abort

Test: python3 tests/test_weather_bot.py

## Git
- Remote: git@github.com:ozanturk19/weather.git (SSH)
- Branch: main

## Dosyalar
- FastAPI backend: main.py (port 8001, uvicorn)
- Frontend: static/index.html
- Live trades: bot/live_trades.json
- Paper trades: bot/paper_trades.json
- Env: /root/weather/.env (PK, API_TOKEN, gitignored)

## Cron
- 04,10,16,22: scanner scan --live
- 11:00: scanner settle | 11:05: trader settle | 11:15: trader redeem
- her 30dk: trader check-fills
- 04,08,12,16,20: trader cancel-stale
