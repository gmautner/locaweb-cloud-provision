#!/usr/bin/env python3
"""
Infrastructure teardown script for Locaweb CloudStack deployment.

Destroys all CloudStack resources belonging to a project, identified by
its network name (<repo-name>-<repo-id>).

Destruction order (reverse of creation):
1. Snapshot policies
2. Detach and delete data volumes
3. Disable static NAT
4. Firewall rules
5. Release public IPs
6. Destroy VMs
7. Delete network
8. Delete SSH key pair

Usage:
    # Tear down a specific zone:
    python3 scripts/teardown_infrastructure.py --network-name my-app-123456789 --zone ZP01

    # Tear down all zones (all networks matching the name):
    python3 scripts/teardown_infrastructure.py --network-name my-app-123456789
"""
import argparse
import json
import subprocess
import sys
import time


CMK_MAX_RETRIES = 5


def cmk(*args):
    """Run a cmk command and return parsed JSON.

    Retries up to CMK_MAX_RETRIES times with exponential backoff
    (2, 4, 8, 16, 32s) to handle intermittent CloudStack API errors.
    Unlike the provisioning script, final errors are non-fatal warnings
    since resources may already be partially deleted.
    """
    cmd = ["cmk"] + list(args)
    for attempt in range(CMK_MAX_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if not result.stdout.strip():
                return {}
            return json.loads(result.stdout)
        error_msg = result.stderr.strip() or result.stdout.strip()
        if attempt < CMK_MAX_RETRIES:
            backoff = 2 ** (attempt + 1)
            print(f"  Retry {attempt + 1}/{CMK_MAX_RETRIES}: cmk {' '.join(args)}: {error_msg} (backoff {backoff}s)")
            time.sleep(backoff)
        else:
            print(f"  Warning: cmk {' '.join(args)} failed after {CMK_MAX_RETRIES + 1} attempts: {error_msg}")
            return None


def find_keypair(name):
    """Check if an SSH key pair exists."""
    data = cmk("list", "sshkeypairs", f"name={name}")
    return bool(data and data.get("sshkeypair"))


def delete_keypair(name):
    """Delete an SSH key pair if it exists."""
    if not find_keypair(name):
        print(f"  Already deleted: {name}")
        return
    cmk("delete", "sshkeypair", f"name={name}")
    print(f"  Deleted {name}")


def resolve_zone(zone_name):
    """Resolve zone name to ID."""
    data = cmk("list", "zones", f"name={zone_name}", "filter=id,name")
    if data:
        for z in data.get("zone", []):
            if z["name"] == zone_name:
                return z["id"]
    raise RuntimeError(f"Zone '{zone_name}' not found")


def teardown(network_name, zone_id=None):
    """Destroy all resources for a project in one network instance.

    When zone_id is provided, only the network in that zone is targeted.
    When zone_id is None, all networks matching the name are torn down.
    """
    keypair_name = f"{network_name}-key"

    # Find matching network(s)
    list_args = ["list", "networks", "filter=id,name,zoneid"]
    if zone_id:
        list_args.append(f"zoneid={zone_id}")
    data = cmk(*list_args)

    matching_networks = []
    if data:
        for n in data.get("network", []):
            if n["name"] == network_name:
                matching_networks.append(n)

    if not matching_networks:
        zone_hint = f" in zone {zone_id}" if zone_id else ""
        print(f"  Network '{network_name}' not found{zone_hint}. Nothing to tear down.")
        # Still try to delete the keypair (it's zone-independent)
        print("[8/8] Deleting SSH key pair...")
        delete_keypair(keypair_name)
        return

    for net in matching_networks:
        net_id = net["id"]
        net_zone_id = net.get("zoneid", "unknown")

        print(f"\n{'='*60}")
        print(f"Tearing down: {network_name} (network={net_id}, zone={net_zone_id})")
        print(f"{'='*60}\n")

        # Find all VMs in this network
        data = cmk("list", "virtualmachines", f"networkid={net_id}",
                    "filter=id,name,state")
        vms = data.get("virtualmachine", []) if data else []

        # 1. Delete snapshot policies for data volumes
        print("[1/8] Removing snapshot policies...")
        vol_args = ["list", "volumes", "type=DATADISK",
                    "tags[0].key=locaweb-cloud-provision-id",
                    f"tags[0].value={network_name}",
                    "filter=id,name"]
        if zone_id:
            vol_args.append(f"zoneid={zone_id}")
        data = cmk(*vol_args)
        volumes = data.get("volume", []) if data else []
        for vol in volumes:
            policies = cmk("list", "snapshotpolicies", f"volumeid={vol['id']}")
            if policies and policies.get("snapshotpolicy"):
                for p in policies["snapshotpolicy"]:
                    cmk("delete", "snapshotpolicies", f"id={p['id']}")
                    print(f"  Deleted snapshot policy for {vol['name']}")

        # 2. Detach and delete data volumes
        print("[2/8] Detaching and deleting data volumes...")
        for vol in volumes:
            cmk("detach", "volume", f"id={vol['id']}")
            print(f"  Detached {vol['name']}")
            time.sleep(2)
            cmk("delete", "volume", f"id={vol['id']}")
            print(f"  Deleted {vol['name']}")

        # 3. Disable static NAT
        print("[3/8] Disabling static NAT...")
        ip_data = cmk("list", "publicipaddresses",
                       f"associatednetworkid={net_id}",
                       "filter=id,ipaddress,issourcenat,isstaticnat")
        ips = []
        if ip_data:
            for ip in ip_data.get("publicipaddress", []):
                if not ip.get("issourcenat", False):
                    ips.append(ip)
        for ip in ips:
            if ip.get("isstaticnat", False):
                cmk("disable", "staticnat", f"ipaddressid={ip['id']}")
                print(f"  Disabled static NAT on {ip['ipaddress']}")

        # 4. Delete firewall rules
        print("[4/8] Deleting firewall rules...")
        for ip in ips:
            rules = cmk("list", "firewallrules", f"ipaddressid={ip['id']}",
                         "filter=id,startport,endport")
            if rules:
                for r in rules.get("firewallrule", []):
                    cmk("delete", "firewallrule", f"id={r['id']}")
                    print(f"  Deleted FW rule {r.get('startport')}-{r.get('endport')} on {ip['ipaddress']}")

        # 5. Release public IPs
        print("[5/8] Releasing public IPs...")
        for ip in ips:
            cmk("disassociate", "ipaddress", f"id={ip['id']}")
            print(f"  Released {ip['ipaddress']}")

        # 6. Destroy VMs
        print("[6/8] Destroying VMs...")
        for vm in vms:
            cmk("destroy", "virtualmachine", f"id={vm['id']}", "expunge=true")
            print(f"  Destroyed {vm['name']}")

        # 7. Delete network
        print("[7/8] Deleting network...")
        time.sleep(5)  # Wait for VMs to fully expunge
        cmk("delete", "network", f"id={net_id}")
        print(f"  Deleted {network_name} (zone={net_zone_id})")

    # 8. Delete SSH key pair (once, after all networks are cleaned up)
    print("[8/8] Deleting SSH key pair...")
    delete_keypair(keypair_name)

    print(f"\n{'='*60}")
    print("Teardown complete!")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Tear down CloudStack infrastructure for a project")
    parser.add_argument("--network-name", required=True,
                        help="Network name (<repo-name>-<repo-id>)")
    parser.add_argument("--zone", default=None,
                        help="CloudStack zone name (e.g. ZP01). "
                             "When set, only tears down resources in that zone. "
                             "When omitted, tears down all zones.")
    args = parser.parse_args()

    zone_id = None
    if args.zone:
        zone_id = resolve_zone(args.zone)
        print(f"Zone filter: {args.zone} ({zone_id})")

    teardown(args.network_name, zone_id=zone_id)


if __name__ == "__main__":
    main()
