#!/usr/bin/env bash
# Full bootstrap for a fresh Ubuntu 22.04 DreamCompute VM.
# Run this once over SSH after the VM is up and DNS is pointed at it.
#
# Usage:
#   ENGINE_HOST=engine.yourdomain.com \
#   CORS_ORIGINS=https://studio.yourdomain.com \
#   bash setup-vm.sh
#
# What this does (in order):
#   1. Install Docker
#   2. Install Caddy
#   3. Build the remediation engine image (expects the repo to be in ~/remediation/)
#   4. Start the engine container
#   5. Write and start the Caddyfile
#   6. Verify the HTTPS health endpoint

set -euo pipefail

ENGINE_HOST="${ENGINE_HOST:?Set ENGINE_HOST to your engine subdomain, e.g. engine.yourdomain.com}"
CORS_ORIGINS="${CORS_ORIGINS:-*}"
REPO_DIR="${REPO_DIR:-$HOME/remediation}"
CADDY_CFG="/etc/caddy/Caddyfile"

echo "==> [1/6] Installing Docker"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  # Re-exec so group membership takes effect without re-login.
  exec sg docker "$0"
fi

echo "==> [2/6] Installing Caddy"
if ! command -v caddy &>/dev/null; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update
  sudo apt-get install -y caddy
fi

echo "==> [3/6] Building the engine image (this takes a few minutes)"
if [ ! -d "$REPO_DIR" ]; then
  echo "ERROR: Repo not found at $REPO_DIR"
  echo "Copy the project there first, e.g.:"
  echo "  scp -r ./remediation user@VM_IP:~/remediation"
  exit 1
fi
cd "$REPO_DIR"
docker build -f docker/Dockerfile -t remediation-engine .

echo "==> [4/6] Starting the engine container"
# Remove any previous run.
docker rm -f remediation-engine 2>/dev/null || true
docker run -d \
  --name remediation-engine \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e "CORS_ORIGINS=${CORS_ORIGINS}" \
  remediation-engine

echo "==> [5/6] Writing Caddyfile and (re)starting Caddy"
sudo tee "$CADDY_CFG" > /dev/null <<EOF
${ENGINE_HOST} {
    reverse_proxy localhost:8000
}
EOF
sudo systemctl enable --now caddy
sudo systemctl reload caddy || sudo systemctl restart caddy

echo "==> [6/6] Waiting for engine to be ready (up to 30s)"
for i in $(seq 1 15); do
  if curl -sf "https://${ENGINE_HOST}/health" | python3 -m json.tool; then
    echo ""
    echo "SUCCESS: engine is live at https://${ENGINE_HOST}"
    exit 0
  fi
  echo "  (attempt $i/15, retrying in 2s...)"
  sleep 2
done

echo ""
echo "WARN: health check did not pass within 30s."
echo "Check container logs: docker logs remediation-engine"
echo "Check Caddy logs:     sudo journalctl -u caddy -n 50"
