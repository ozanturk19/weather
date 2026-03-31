# Weather Dashboard — VPS Geliştirme Ortamı

## Git Kuralları
- **Git push için daima `Bash` aracını kullan** — MCP GitHub tool kullanma
- SSH key ile push çalışıyor: `git push origin <branch>`
- Varsayılan branch: `main`
- Remote: `git@github.com:ozanturk19/weather.git`

## Proje
- FastAPI backend: `main.py` (port 8001)
- Frontend: `static/index.html`
- Bias verisi: `predictions.json` (gitignored, kaydetme)
- Servis: `systemctl status weather`

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
