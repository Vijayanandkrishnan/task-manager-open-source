#!/bin/bash
# Task Manager - New Instance Setup Script
# Usage: ./setup_instance.sh <client-name> <port> [domain]
# Example: ./setup_instance.sh acme-corp 5051 acme.yourdomain.com

set -e

CLIENT="$1"
PORT="$2"
DOMAIN="$3"

if [ -z "$CLIENT" ] || [ -z "$PORT" ]; then
    echo "Usage: $0 <client-name> <port> [domain]"
    echo "Example: $0 acme-corp 5051 acme.yourdomain.com"
    exit 1
fi

APP_DIR="/opt/task-manager-$CLIENT"
SERVICE_NAME="task-manager-$CLIENT"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR"

echo "=== Setting up Task Manager for: $CLIENT ==="
echo "Directory: $APP_DIR"
echo "Port: $PORT"
echo "Service: $SERVICE_NAME"

# 1. Create app directory
if [ -d "$APP_DIR" ]; then
    echo "ERROR: $APP_DIR already exists!"
    exit 1
fi

mkdir -p "$APP_DIR/templates"

# 2. Copy application files
cp "$TEMPLATE_DIR/app.py" "$APP_DIR/"
cp "$TEMPLATE_DIR/templates/"*.html "$APP_DIR/templates/"
cp "$TEMPLATE_DIR/requirements.txt" "$APP_DIR/"

echo "Files copied."

# 3. Create Python virtual environment
python3 -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"
pip install -q -r "$APP_DIR/requirements.txt"
deactivate

echo "Virtual environment created."

# 4. Generate secret key
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 5. Create systemd service
cat > "/etc/systemd/system/$SERVICE_NAME.service" << EOF
[Unit]
Description=Task Manager - $CLIENT
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=SECRET_KEY=$SECRET_KEY
Environment=TELEGRAM_BOT_TOKEN=
Environment=TELEGRAM_GROUP_CHAT_ID=
Environment=TELEGRAM_ACCOUNTANT_CHAT_ID=
Environment=INITIAL_ADMIN_PASSWORD=${INITIAL_ADMIN_PASSWORD:-}
Environment=INITIAL_OWNER_PASSWORD=${INITIAL_OWNER_PASSWORD:-}
ExecStart=$APP_DIR/venv/bin/gunicorn -w 2 -b 127.0.0.1:$PORT app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Systemd service created."

# 6. Create Nginx config
if [ -n "$DOMAIN" ]; then
    cat > "/etc/nginx/sites-available/$SERVICE_NAME" << EOF
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 90;
    }
}
EOF
    ln -sf "/etc/nginx/sites-available/$SERVICE_NAME" "/etc/nginx/sites-enabled/"
    nginx -t && systemctl reload nginx
    echo "Nginx configured for $DOMAIN"
else
    echo "No domain specified. Access via http://SERVER_IP:$PORT (or add Nginx config manually)"
fi

# 7. Start the service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

sleep 2

# 8. Verify
STATUS=$(curl -s "http://127.0.0.1:$PORT/api/health" 2>/dev/null || echo "FAILED")

echo ""
echo "=== Setup Complete ==="
echo "Client: $CLIENT"
echo "Directory: $APP_DIR"
echo "Port: $PORT"
echo "Service: $SERVICE_NAME"
echo "Health: $STATUS"
echo ""
echo "Initial admin credentials are generated on first startup."
echo "If you did not set INITIAL_ADMIN_PASSWORD and INITIAL_OWNER_PASSWORD before running this script, fetch the generated values with:"
echo "  journalctl -u $SERVICE_NAME -n 80 --no-pager"
echo ""
if [ -n "$DOMAIN" ]; then
    echo "URL: http://$DOMAIN"
else
    echo "URL: http://$(hostname -I | awk '{print $1}'):$PORT"
fi
echo ""
echo "Next steps:"
echo "  1. Review the first-run credentials in the service logs"
echo "  2. Configure Telegram (edit /etc/systemd/system/$SERVICE_NAME.service)"
echo "  3. Set company branding in Settings"
echo "  4. Add SSL: certbot --nginx -d $DOMAIN"
