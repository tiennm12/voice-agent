#!/bin/bash
# Deploy voice-agent to a fresh Debian/Ubuntu GCP VM (IP-only, no domain).
# Run this via GCP Console's browser SSH. Safe to re-run.
set -e

REPO_URL="https://github.com/tiennm12/voice-agent.git"
APP_DIR="$HOME/voice-agent"

echo "== Installing Node.js 20.x =="
if ! command -v node >/dev/null || [ "$(node -v | grep -oE '^v[0-9]+' | tr -d v)" -lt 18 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
node -v

echo "== Installing git =="
sudo apt-get install -y git

echo "== Installing Caddy (reverse proxy + self-signed TLS) =="
if ! command -v caddy >/dev/null; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update
  sudo apt-get install -y caddy
fi
caddy version

echo "== Fetching code =="
if [ -d "$APP_DIR" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
npm install

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ""
  echo "!! .env created from template with placeholder values."
  echo "!! Edit it now with your real API keys: nano $APP_DIR/.env"
  echo "!! Then run: sudo systemctl restart voice-agent"
  echo ""
fi

echo "== Writing systemd service =="
sudo tee /etc/systemd/system/voice-agent.service > /dev/null <<EOF
[Unit]
Description=Voice Agent
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$(command -v node) src/server.js
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable voice-agent
sudo systemctl restart voice-agent

echo "== Writing Caddyfile (self-signed cert, IP-only) =="
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
:443 {
	tls internal
	reverse_proxy localhost:3000
}
EOF

sudo systemctl restart caddy

echo ""
echo "== Done =="
echo "Edit API keys if you haven't: nano $APP_DIR/.env && sudo systemctl restart voice-agent"
echo "Check app logs:   sudo journalctl -u voice-agent -f"
echo "Check caddy logs: sudo journalctl -u caddy -f"
echo "Open https://<VM_EXTERNAL_IP>/ on your phone (accept the self-signed cert warning once)."
echo "Make sure a GCP firewall rule allows tcp:443 ingress to this VM."
