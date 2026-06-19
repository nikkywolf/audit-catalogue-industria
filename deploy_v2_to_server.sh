#!/usr/bin/env bash
set -euo pipefail

SERVER="ubuntu@144.217.80.100"
REMOTE_DIR="/home/ubuntu/audit-catalogue-industria"
SERVICE="industria-dashboard"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" != "--live" ]]; then
  echo "Mode simulation. Aucune modification ne sera faite en ligne."
  echo ""
  echo "Quand tu es prête, lance:"
  echo "  ./deploy_v2_to_server.sh --live"
  echo ""
  rsync -avzn \
    --exclude ".env" \
    --exclude ".DS_Store" \
    --exclude "venv/" \
    --exclude "__pycache__/" \
    --exclude "*.pyc" \
    --exclude "industria_catalogue.db-shm" \
    --exclude "industria_catalogue.db-wal" \
    "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"
  exit 0
fi

echo "Préparation de la base SQLite locale..."
sqlite3 "$LOCAL_DIR/industria_catalogue.db" "PRAGMA wal_checkpoint(FULL);"

BACKUP_DIR="/home/ubuntu/audit-catalogue-industria-backups/$(date +%Y%m%d-%H%M%S)"

echo "Création d'une sauvegarde sur le serveur: $BACKUP_DIR"
ssh "$SERVER" "mkdir -p '$BACKUP_DIR' && cp -a '$REMOTE_DIR/.' '$BACKUP_DIR/'"

echo "Envoi de la v2 vers le serveur..."
rsync -avz \
  --exclude ".env" \
  --exclude ".DS_Store" \
  --exclude "venv/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "industria_catalogue.db-shm" \
  --exclude "industria_catalogue.db-wal" \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

echo "Installation/vérification des dépendances Python..."
ssh "$SERVER" "cd '$REMOTE_DIR' && if [ -x venv/bin/pip ]; then venv/bin/pip install -r requirements.txt; else python3 -m pip install -r requirements.txt; fi"

echo "Redémarrage du dashboard..."
ssh "$SERVER" "sudo systemctl restart '$SERVICE' && sudo systemctl status '$SERVICE' --no-pager -l"

echo ""
echo "Déploiement terminé."
echo "Sauvegarde disponible sur le serveur:"
echo "  $BACKUP_DIR"
