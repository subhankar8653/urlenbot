#!/usr/bin/env bash
# VPS (Ubuntu/Debian) par one-shot setup:  sudo bash deploy_vps.sh
set -e
[ "$(id -u)" -eq 0 ] || { echo "root se chalao:  sudo bash deploy_vps.sh"; exit 1; }
[ -f config.env ] || { echo "config.env nahi mili — pehle usme API_ID, BOT_TOKEN, MONGO_URI bharo"; exit 1; }

echo "▶ Docker check..."
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

echo "▶ Network tuning (BBR + bade TCP buffers => upload/download fast)..."
cat >/etc/sysctl.d/99-urlenbot.conf <<'SYS'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.ipv4.tcp_rmem = 4096 262144 33554432
net.ipv4.tcp_wmem = 4096 262144 33554432
net.ipv4.tcp_mtu_probing = 1
SYS
sysctl --system >/dev/null || true

echo "▶ Bot build + start..."
docker compose up -d --build
echo "✅ Done. Logs:  docker compose logs -f --tail=100"
echo "⚠️  Railway wala bot BAND karo (ek BOT_TOKEN ek hi jagah chale)."
