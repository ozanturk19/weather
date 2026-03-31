# Weather Dashboard — VPS Geliştirme Ortamı

## Git Kuralları
- **Git push için daima `Bash` aracını kullan** — MCP GitHub tool kullanma
- Varsayılan branch: `main`
- Remote: `https://github.com/ozanturk19/weather.git`
- Push öncesi remote URL'yi HTTPS+PAT ile ayarla:
  `git remote set-url origin https://GITHUB_PAT@github.com/ozanturk19/weather.git`
- PAT yoksa kullanıcıdan iste

## Proje
- Yerel dizin: `/Users/mac/Projects Weather`
- VPS dizin: `/root/weather` (git pull ile güncellenir)
- FastAPI backend: `main.py` (port 8001)
- Frontend: `static/index.html`
- Bias verisi: `predictions.json` (gitignored, kaydetme)
- Servis: `systemctl status weather` (VPS'te)

## İstasyonlar
- EGLC — Londra City
- LTAC — Ankara Esenboğa  
- LIMC — Milano Malpensa
- LTFM — İstanbul Havalimanı (NOAA settlement)

## Deployment
```bash
git add -A && git commit -m "..." && git push origin main
systemctl restart weather  # gerekirse
```
