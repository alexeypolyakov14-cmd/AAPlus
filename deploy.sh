#!/bin/bash
set -e

APP_DIR="/var/www/aaplus"
cd "$APP_DIR"

echo "=== Pulling latest changes ==="
git fetch origin main
git reset --hard origin/main

echo "=== Installing dependencies ==="
npm install --production

echo "=== Restarting app ==="
pm2 restart aaplus --update-env

echo "=== Deploy complete at $(date) ==="
