#!/bin/bash
# =============================================================================
# PolarisFolio Web - Proxmox LXC Deploy Script
# Run this inside the container as root after copying polarisfolio_web/ across.
#
# Usage:
#   1. Copy files to container:
#      scp -r polarisfolio_web/ root@<container-ip>:/root/polarisfolio_web
#
#   2. SSH into container and run:
#      bash /root/polarisfolio_web/deploy.sh
# =============================================================================

set -e  # exit on any error

# -- Config -------------------------------------------------------------------
APP_USER="polarisfolio"
APP_DIR="/home/${APP_USER}/polarisfolio_web"
PDF_DIR="/home/${APP_USER}/polarisfolio_pdfs"
SERVICE_PORT="8001"
SOURCE_DIR="/root/polarisfolio_web"

# Colour output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[+]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
section() { echo -e "\n${GREEN}=== $1 ===${NC}"; }

# -- Root check ---------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Run this script as root.${NC}"
  exit 1
fi

section "System update"
apt-get update -qq
apt-get upgrade -y -qq
info "System up to date"

section "Installing packages"
apt-get install -y -qq \
  python3 \
  python3-pip \
  python3-venv \
  nginx \
  curl \
  git \
  ufw
info "Packages installed"

section "Creating app user"
if id "$APP_USER" &>/dev/null; then
  warn "User ${APP_USER} already exists, skipping"
else
  useradd -m -s /bin/bash "$APP_USER"
  info "Created user: ${APP_USER}"
fi

section "Copying application files"
if [ ! -d "$SOURCE_DIR" ]; then
  echo -e "${RED}Source directory not found: ${SOURCE_DIR}${NC}"
  echo "Make sure you copied polarisfolio_web/ to /root/polarisfolio_web first."
  exit 1
fi

mkdir -p "$APP_DIR"
cp -r "$SOURCE_DIR"/. "$APP_DIR/"
mkdir -p "$PDF_DIR"
chown -R "$APP_USER":"$APP_USER" "/home/${APP_USER}"
info "Files copied to ${APP_DIR}"

section "Setting up Python virtual environment"
su - "$APP_USER" -c "
  cd ${APP_DIR}
  python3 -m venv venv
  source venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  echo 'Python environment ready'
"
info "Virtual environment created"

section "Installing systemd service"
# Write the service file with correct paths
cat > /etc/systemd/system/polarisfolio.service << EOF
[Unit]
Description=PolarisFolio Web
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/uvicorn app:app --host 127.0.0.1 --port ${SERVICE_PORT}
Restart=on-failure
RestartSec=5
Environment="POLARISFOLIO_DB=/home/${APP_USER}/.polarisfolio_web.db"
Environment="POLARISFOLIO_PDF_DIR=${PDF_DIR}"

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable polarisfolio
systemctl start polarisfolio
sleep 2

if systemctl is-active --quiet polarisfolio; then
  info "PolarisFolio service running"
else
  warn "Service may not have started correctly. Check: journalctl -u polarisfolio -n 50"
fi

section "Configuring nginx"
# Detect container IP
CONTAINER_IP=$(hostname -I | awk '{print $1}')

cat > /etc/nginx/sites-available/polarisfolio << EOF
server {
    listen 80;
    server_name ${CONTAINER_IP} _;

    # Increase timeout for PDF generation
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;

    # Increase max body size for PDF downloads
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:${SERVICE_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Disable default site, enable polarisfolio
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/polarisfolio /etc/nginx/sites-enabled/polarisfolio

nginx -t
systemctl enable nginx
systemctl reload nginx
info "Nginx configured"

section "Configuring firewall"
ufw --force reset > /dev/null 2>&1
ufw default deny incoming > /dev/null
ufw default allow outgoing > /dev/null
ufw allow ssh > /dev/null
ufw allow 'Nginx HTTP' > /dev/null
ufw --force enable > /dev/null
info "Firewall configured (SSH + HTTP allowed)"

section "Done"
echo ""
echo -e "  PolarisFolio is running at: ${GREEN}http://${CONTAINER_IP}${NC}"
echo ""
echo "  Next steps:"
echo "  1. Open http://${CONTAINER_IP} in your browser"
echo "  2. Go to Settings and add your Azure app client ID"
echo "  3. Add redirect URI to your Azure app:"
echo "     http://${CONTAINER_IP}/auth/callback"
echo "  4. Go to Calendars and connect Microsoft 365"
echo ""
echo "  Useful commands:"
echo "  systemctl status polarisfolio         - check service status"
echo "  journalctl -u polarisfolio -f         - follow logs"
echo "  systemctl restart polarisfolio        - restart after code changes"
echo ""
