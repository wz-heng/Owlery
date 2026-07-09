#!/bin/bash
set -e

# Create owlery service
sudo tee /etc/systemd/system/owlery.service > /dev/null << 'EOF'
[Unit]
Description=Owlery Server
After=network.target

[Service]
Type=simple
User=start-up
WorkingDirectory=/home/start-up
EnvironmentFile=/home/start-up/Owlery/.env
# systemd's default PATH excludes ~/.local/bin where `claude` is installed.
Environment=PATH=/home/start-up/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/home/start-up/Owlery/.venv/bin/owlery serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Create cloudflared service
sudo tee /etc/systemd/system/cloudflared.service > /dev/null << 'EOF'
[Unit]
Description=Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=start-up
ExecStart=/home/start-up/.local/bin/cloudflared tunnel run owlery
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable owlery cloudflared
sudo systemctl start owlery cloudflared

echo "Done! Checking status..."
sudo systemctl status owlery cloudflared --no-pager
