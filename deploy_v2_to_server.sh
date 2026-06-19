#!/usr/bin/env bash
set -euo pipefail

SERVER="ubuntu@144.217.80.100"
REMOTE_DIR="/home/ubuntu/audit-catalogue-industria"
SERVICE="industria-dashboard"
BRANCH="main"

if [[ "${1:-}" != "--live" ]]; then
  echo "Mode simulation. Aucune modification ne sera faite en ligne."
  echo ""
  echo "Ce script déploie maintenant depuis GitHub, pas depuis l'ordinateur local."
  echo "Quand tu es prête, lance:"
  echo "  ./deploy_v2_to_server.sh --live"
  echo ""
  ssh "$SERVER" "cd '$REMOTE_DIR' && git fetch origin '$BRANCH' && git status --short && git log --oneline -1 origin/'$BRANCH'"
  exit 0
fi

BACKUP_DIR="/home/ubuntu/audit-catalogue-industria-backups/github-deploy-$(date +%Y%m%d-%H%M%S)"

echo "Création d'une sauvegarde sur le serveur: $BACKUP_DIR"
ssh "$SERVER" "mkdir -p '$BACKUP_DIR' && rsync -a --exclude venv '$REMOTE_DIR/' '$BACKUP_DIR/'"

echo "Mise à jour depuis GitHub..."
ssh "$SERVER" "cd '$REMOTE_DIR' && git fetch origin '$BRANCH' && git reset --hard origin/'$BRANCH'"

echo "Installation/vérification des dépendances Python..."
ssh "$SERVER" "cd '$REMOTE_DIR' && if [ -x venv/bin/pip ]; then venv/bin/pip install -r requirements.txt; else python3 -m pip install -r requirements.txt; fi"

echo "Redémarrage du dashboard..."
ssh "$SERVER" "sudo systemctl restart '$SERVICE' && sudo systemctl status '$SERVICE' --no-pager -l"

echo ""
echo "Déploiement terminé depuis GitHub."
echo "Sauvegarde disponible sur le serveur:"
echo "  $BACKUP_DIR"
