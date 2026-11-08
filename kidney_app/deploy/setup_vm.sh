#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VM (Nebius Compute). Run as a sudo user:  bash deploy/setup_vm.sh
set -euo pipefail
sudo apt-get update
sudo apt-get install -y ca-certificates curl git ufw
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo ">> Docker installed. Log out and back in once so 'docker' works without sudo."
fi
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
mkdir -p ~/kidney_app/data ~/backups
echo ">> VM ready. Next: copy the code, .env, and data/reference.duckdb, then: docker compose up -d --build"
