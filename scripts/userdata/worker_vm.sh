#!/bin/bash
# Userdata script for Worker VM
# Installs fail2ban to block SSH brute-force attempts.
# Docker is installed automatically by Kamal on first deploy.
set -euo pipefail

# --- Use global Ubuntu mirror to avoid regional mirror sync issues ---
sed -i 's|br\.archive\.ubuntu\.com|archive.ubuntu.com|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || \
  sed -i 's|br\.archive\.ubuntu\.com|archive.ubuntu.com|g' /etc/apt/sources.list 2>/dev/null || true

# --- fail2ban: block SSH brute-force attempts ---
apt-get update -qq
apt-get install -y -qq fail2ban jq
cat > /etc/fail2ban/jail.local << 'F2BEOF'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 3

[sshd]
enabled = true
mode = aggressive
F2BEOF
systemctl restart fail2ban

# --- Route .internal DNS queries to the CloudStack virtual router ---
# The gateway of the isolated network is always the virtual router,
# which is the only DNS server that resolves internal VM hostnames.
# Without this, systemd-resolved may failover to a public DNS server
# that returns NXDOMAIN for internal names, breaking inter-VM connectivity.
GATEWAY=$(ip -4 -json route show default | jq -r '.[0].gateway')

mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/internal-dns.conf << EOF
[Resolve]
DNS=${GATEWAY}
Domains=~internal
EOF

systemctl restart systemd-resolved
