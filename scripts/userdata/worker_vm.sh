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

# --- Force all DNS queries through the CloudStack virtual router ---
# Disable DHCP-provided DNS (which includes public servers) and set the
# gateway as the sole resolver. The virtual router resolves .internal
# hostnames directly and forwards external queries upstream, eliminating
# the risk of systemd-resolved failing over to a public DNS server
# that returns NXDOMAIN for .internal names.
GATEWAY=$(ip -4 -json route show default | jq -r '.[0].gateway')
IFACE=$(ip -4 -json route show default | jq -r '.[0].dev')

NETFILE=$(ls /etc/systemd/network/*.network 2>/dev/null | head -1)
if [ -n "$NETFILE" ]; then
  DROPIN_DIR="${NETFILE}.d"
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN_DIR/dns-override.conf" << EOF
[DHCPv4]
UseDNS=false

[Network]
DNS=${GATEWAY}
EOF
  networkctl reload && networkctl reconfigure "$IFACE"
fi
