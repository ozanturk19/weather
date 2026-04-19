#!/bin/bash
# /root/weather/deploy.sh
# Weather Bot + Dashboard güvenli deploy scripti
#
# Kullanım:
#   ./deploy.sh              # normal deploy (uncommitted varsa abort)
#   ./deploy.sh --force      # uncommitted değişiklikleri stash'le, pull, stash pop
#   ./deploy.sh --status     # git durumunu göster, deploy yapma

set -e

WEATHER_DIR="/root/weather"
DASHBOARD_DIR="/opt/polymarket/dashboard"
LOG="/root/deploy.log"
FORCE=0
STATUS_ONLY=0

for arg in "$@"; do
  case $arg in
    --force)  FORCE=1 ;;
    --status) STATUS_ONLY=1 ;;
  esac
done

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
die() { echo "❌ $*"; exit 1; }

log "═══════════════════════════════════════"
log "Deploy başladı (force=$FORCE)"

# ─── Durum Göster ─────────────────────────────────────────────────────────
cd "$WEATHER_DIR"
DIRTY=$(git diff HEAD --name-only 2>/dev/null)
STAGED=$(git diff --cached --name-only 2>/dev/null)

if [[ -n "$DIRTY" || -n "$STAGED" ]]; then
  echo ""
  echo "⚠️  Commit edilmemiş değişiklikler:"
  git status --short
  echo ""
fi

if [[ "$STATUS_ONLY" == "1" ]]; then
  log "Sadece durum gösterildi, deploy yapılmadı."
  exit 0
fi

# ─── Uncommitted Değişiklik Kontrolü ─────────────────────────────────────
if [[ -n "$DIRTY" || -n "$STAGED" ]]; then
  if [[ "$FORCE" == "1" ]]; then
    log "⚠️  --force: değişiklikler stash'leniyor..."
    git stash push -m "deploy-stash-$(date +%Y%m%d-%H%M%S)"
    STASHED=1
  else
    die "Commit edilmemiş değişiklikler var. Deploy iptal.
  → Commit edip push et, sonra tekrar çalıştır.
  → Ya da: ./deploy.sh --force (stash+pull+stash-pop)"
  fi
fi

# ─── Push Edilmemiş Commit Kontrolü ──────────────────────────────────────
AHEAD=$(git rev-list "@{u}..HEAD" --count 2>/dev/null || echo 0)
if [[ "$AHEAD" -gt 0 ]]; then
  die "Push edilmemiş $AHEAD commit var — GitHub ile senkron değil. Deploy iptal.
  → git push origin main, sonra tekrar çalıştır."
fi

# ─── Git Pull ─────────────────────────────────────────────────────────────
log "git pull origin main..."
PULL_OUT=$(git pull origin main 2>&1)
echo "$PULL_OUT"

if echo "$PULL_OUT" | grep -q "Already up to date"; then
  log "ℹ️  Kod değişmedi."
  CHANGED_WEATHER=0
  CHANGED_DASHBOARD=0
else
  log "Yeni commit'ler çekildi."
  CHANGED_WEATHER=1
  # Dashboard da pull'dan etkilendi mi?
  CHANGED_DASHBOARD=$(echo "$PULL_OUT" | grep -c "dashboard\|next\|\.tsx\|\.ts" || true)
fi

# ─── Stash Pop (--force ile yapıldıysa) ───────────────────────────────────
if [[ "${STASHED:-0}" == "1" ]]; then
  log "Stash geri alınıyor..."
  if ! git stash pop; then
    echo ""
    echo "❌ Stash pop sırasında conflict oluştu!"
    echo "   Manuel çözüm gerekiyor: git status → conflict'leri düzelt → git stash drop"
    exit 1
  fi
  log "Stash başarıyla geri alındı."
fi

# ─── Test Suite ────────────────────────────────────────────────────────────
if [[ -f "$WEATHER_DIR/tests/test_weather_bot.py" ]]; then
  log "Test suite çalışıyor..."
  if python3 "$WEATHER_DIR/tests/test_weather_bot.py" > /tmp/test_out.txt 2>&1; then
    PASS=$(grep -o '[0-9]* geçti' /tmp/test_out.txt | head -1)
    log "✅ Testler geçti ($PASS)"
  else
    cat /tmp/test_out.txt
    die "Test başarısız — deploy iptal. Testleri düzelt, tekrar çalıştır."
  fi
fi

# ─── Weather FastAPI Yeniden Başlat ───────────────────────────────────────
# systemctl kullanıyoruz — nohup orphan process bırakıyordu (port çakışması)
if [[ "$CHANGED_WEATHER" == "1" ]]; then
  log "Weather FastAPI yeniden başlatılıyor..."

  # Önce port 8001'deki her orphan process'i temizle (eski nohup kalıntıları)
  ORPHAN=$(fuser 8001/tcp 2>/dev/null | tr -d ' ')
  if [[ -n "$ORPHAN" ]]; then
    kill "$ORPHAN" 2>/dev/null || true
    sleep 1
  fi

  # systemd ile yönet — tek otorite systemd olsun
  systemctl restart weather
  sleep 3

  # Health check
  WEATHER_PID=$(systemctl show weather -p MainPID --value)
  if curl -sf http://localhost:8001/api/live-trades > /dev/null 2>&1; then
    log "✅ Weather API sağlıklı (PID=$WEATHER_PID)"
  else
    die "Weather API yanıt vermiyor! Log: journalctl -u weather -n 20"
  fi
fi

# ─── Dashboard (Next.js/PM2) Yeniden Başlat ───────────────────────────────
if [[ "$CHANGED_DASHBOARD" -gt 0 ]]; then
  log "Dashboard değişti — Next.js build + PM2 restart..."
  cd "$DASHBOARD_DIR"
  npm run build 2>&1 | tail -5
  pm2 restart polymarket-dashboard
  log "✅ Dashboard yeniden başlatıldı"
fi

# ─── Özet ──────────────────────────────────────────────────────────────────
echo ""
log "═══════════════════════════════════════"
log "✅ Deploy tamamlandı"
log "   Weather API  : http://localhost:8001"
log "   Dashboard    : http://localhost:8004"
log "   Log          : $LOG"
