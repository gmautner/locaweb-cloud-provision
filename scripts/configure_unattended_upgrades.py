#!/usr/bin/env python3
"""
Configure unattended-upgrades on all provisioned VMs via SSH.

Writes APT configuration files to enable automatic security updates and
optionally automatic reboots at a specified time.

Usage:
    python3 scripts/configure_unattended_upgrades.py \
        --ssh-key /tmp/ssh_key \
        --provision-output /tmp/provision-output.json \
        --automatic-reboot true \
        --reboot-time "05:00"
"""
import argparse
import json
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]


def ssh_run(ip, command, key_path, retries=3):
    """Run a remote command via SSH. Retries on transient connection errors."""
    cmd = ["ssh"] + SSH_OPTS + ["-i", key_path, f"root@{ip}", command]
    for attempt in range(retries):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 or "Connection reset" not in result.stderr:
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        if attempt < retries - 1:
            time.sleep(5)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def wait_for_ssh(ip, key_path, timeout=180):
    """Poll SSH every 10s until it responds or timeout is reached."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, _, _ = ssh_run(ip, "true", key_path)
            if rc == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(10)
    return False


# ---------------------------------------------------------------------------
# APT configuration content
# ---------------------------------------------------------------------------

AUTO_UPGRADES_CONTENT = """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
"""

AUTOMATIC_REBOOT_TEMPLATE = """\
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "{time}";
"""


# ---------------------------------------------------------------------------
# Configuration logic
# ---------------------------------------------------------------------------

def configure_vm(ip, key_path, automatic_reboot, reboot_time):
    """Apply unattended-upgrades configuration to a single VM."""
    print(f"  Configuring {ip}...")

    # Ensure the VM uses UTC so scheduled times are interpreted correctly
    rc, _, stderr = ssh_run(ip, "timedatectl set-timezone Etc/UTC", key_path)
    if rc != 0:
        print(f"    [WARN] Failed to set timezone to UTC on {ip}: {stderr}")
        return False

    # Always write the auto-upgrades config
    rc, _, stderr = ssh_run(
        ip,
        f"cat > /etc/apt/apt.conf.d/20auto-upgrades << 'APTEOF'\n"
        f"{AUTO_UPGRADES_CONTENT}APTEOF",
        key_path,
    )
    if rc != 0:
        print(f"    [WARN] Failed to write 20auto-upgrades on {ip}: {stderr}")
        return False

    # Handle automatic reboot config
    if automatic_reboot:
        content = AUTOMATIC_REBOOT_TEMPLATE.format(time=reboot_time)
        rc, _, stderr = ssh_run(
            ip,
            f"cat > /etc/apt/apt.conf.d/52-automatic-reboots << 'APTEOF'\n"
            f"{content}APTEOF",
            key_path,
        )
        if rc != 0:
            print(f"    [WARN] Failed to write 52-automatic-reboots on {ip}: {stderr}")
            return False
    else:
        # Remove the file if it exists (idempotent)
        rc, _, _ = ssh_run(
            ip,
            "rm -f /etc/apt/apt.conf.d/52-automatic-reboots",
            key_path,
        )

    print(f"    OK (reboot={'yes @ ' + reboot_time if automatic_reboot else 'no'})")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Configure unattended-upgrades on provisioned VMs")
    parser.add_argument("--ssh-key", required=True,
                        help="Path to SSH private key")
    parser.add_argument("--provision-output", required=True,
                        help="Path to provision-output.json")
    parser.add_argument("--automatic-reboot", required=True,
                        help="Enable automatic reboot (true/false)")
    parser.add_argument("--reboot-time", default="05:00",
                        help="Reboot time in HH:MM UTC (default: 05:00)")
    args = parser.parse_args()

    automatic_reboot = args.automatic_reboot.lower() == "true"

    with open(args.provision_output) as f:
        output = json.load(f)

    # Collect all VM IPs
    ips = []
    web_ip = output.get("web_ip", "")
    if web_ip:
        ips.append(("web", web_ip))
    for i, ip in enumerate(output.get("worker_ips", []), 1):
        ips.append((f"worker-{i}", ip))
    for name, data in output.get("accessories", {}).items():
        ip = data.get("ip", "")
        if ip:
            ips.append((name, ip))

    if not ips:
        print("No VMs found in provision output, skipping.")
        return

    print(f"Configuring unattended-upgrades on {len(ips)} VM(s)...")
    print(f"  Automatic reboot: {automatic_reboot}")
    if automatic_reboot:
        print(f"  Reboot time (UTC): {args.reboot_time}")

    failed = []
    for label, ip in ips:
        print(f"\n[{label}] Waiting for SSH on {ip}...")
        if not wait_for_ssh(ip, args.ssh_key, timeout=180):
            print(f"  [FAIL] SSH not reachable on {ip} after 180s")
            failed.append(ip)
            continue
        if not configure_vm(ip, args.ssh_key, automatic_reboot, args.reboot_time):
            failed.append(ip)

    if failed:
        print(f"\n[ERROR] Configuration failed on: {', '.join(failed)}")
        sys.exit(1)

    print(f"\nUnattended-upgrades configured successfully on all {len(ips)} VM(s).")


if __name__ == "__main__":
    main()
