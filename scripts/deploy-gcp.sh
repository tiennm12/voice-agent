#!/bin/bash
# Deploy voice-agent to a fresh Debian/Ubuntu GCP VM (IP-only, no domain).
# Run this via GCP Console's browser SSH. Safe to re-run.
set -e

REPO_URL="https://github.com/tiennm12/voice-agent.git"
BRANCH="${BRANCH:-python-port}"
APP_DIR="$HOME/voice-agent"

echo "== Installing Python (needs 3.11+) =="
sudo apt-get update
sudo apt-get install -y python3 python3-venv
python3 --version
python3 -c 'import sys; sys.exit("!! Python 3.11+ required" if sys.version_info < (3, 11) else 0)'

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
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

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
ExecStart=$APP_DIR/.venv/bin/python -m voice_agent.server
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable voice-agent
sudo systemctl restart voice-agent

echo "== Generating self-signed TLS cert for this VM's external IP =="
# `tls internal` on a host-less ":443" site block has no hostname to issue a
# cert for, so on-demand issuance fails at handshake time ("TLS alert:
# internal error") for a bare IP connection with no SNI. Generate a static
# self-signed cert for the VM's actual external IP instead — this is the
# reliable option for an IP-only (no domain) deployment.
EXTERNAL_IP="$(curl -s -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip')"
if [ -z "$EXTERNAL_IP" ]; then
  echo "!! Could not auto-detect external IP from GCE metadata; falling back to 'tls internal' (may not work for bare-IP browser access)."
  CERT_DIRECTIVE="tls internal"
else
  echo "External IP: $EXTERNAL_IP"
  if [ ! -f /etc/caddy/selfsigned.crt ]; then
    sudo openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /etc/caddy/selfsigned.key -out /etc/caddy/selfsigned.crt \
      -days 365 -subj "/CN=$EXTERNAL_IP" -addext "subjectAltName=IP:$EXTERNAL_IP"
    sudo chown caddy:caddy /etc/caddy/selfsigned.key /etc/caddy/selfsigned.crt
    sudo chmod 600 /etc/caddy/selfsigned.key
  fi
  CERT_DIRECTIVE="tls /etc/caddy/selfsigned.crt /etc/caddy/selfsigned.key"
fi

echo "== Writing Caddyfile =="
printf ':443 {\n\t%s\n\treverse_proxy localhost:3000\n}\n' "$CERT_DIRECTIVE" | sudo tee /etc/caddy/Caddyfile > /dev/null

sudo systemctl restart caddy

echo ""
echo "== Done =="
echo "Edit API keys if you haven't: nano $APP_DIR/.env && sudo systemctl restart voice-agent"
echo "Check app logs:   sudo journalctl -u voice-agent -f"
echo "Check caddy logs: sudo journalctl -u caddy -f"
echo "Open https://<VM_EXTERNAL_IP>/ on your phone (accept the self-signed cert warning once)."
echo "Make sure a GCP firewall rule allows tcp:443 ingress to this VM."
