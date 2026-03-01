#!/bin/bash
# Userdata script for Accessory VMs (database, cache, etc.)
# Installs fail2ban and waits for attached data disk and formats/mounts it.
# Docker is installed automatically by Kamal on first deploy.
set -euo pipefail

# --- fail2ban: block SSH brute-force attempts ---
apt-get update -qq
apt-get install -y -qq fail2ban
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

DEVICE="/dev/vdb"
MOUNT_POINT="/data"

# Wait for the attached data disk to appear
echo "Waiting for $DEVICE..."
TIMEOUT=300
INTERVAL=5
ELAPSED=0
while [ ! -b "$DEVICE" ]; do
  if [ $ELAPSED -ge $TIMEOUT ]; then
    echo "ERROR: $DEVICE not found after ${TIMEOUT}s"
    exit 1
  fi
  sleep $INTERVAL
  ELAPSED=$((ELAPSED + INTERVAL))
done
echo "$DEVICE found after ${ELAPSED}s"

# Format if no filesystem exists
if ! blkid "$DEVICE" >/dev/null 2>&1; then
  echo "Formatting $DEVICE as ext4..."
  mkfs.ext4 -q "$DEVICE"
fi

# Create mount point and mount
mkdir -p "$MOUNT_POINT"
mount "$DEVICE" "$MOUNT_POINT"

# Add to fstab for persistence
if ! grep -q "$DEVICE" /etc/fstab; then
  echo "$DEVICE $MOUNT_POINT ext4 defaults,nofail 0 2" >> /etc/fstab
fi
