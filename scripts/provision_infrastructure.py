#!/usr/bin/env python3
"""
Infrastructure provisioning script for Locaweb CloudStack deployment.

Creates CloudStack resources based on a validated JSON configuration:
- Isolated network with SSH key pair and networkdomain for DNS resolution
- Web VM (always) with data disk
- Worker VMs (optional, N replicas, stateless — no data disks)
- Accessory VMs (generic — one per entry in accessories list) with data disks
- Public IPs with static NAT (1:1 mapping per VM)
- Firewall rules (SSH+HTTP+HTTPS for web; SSH only for workers and accessories)
- Daily snapshot policies for data disks

The script is idempotent — running it twice will skip existing resources.

Usage:
    python3 scripts/provision_infrastructure.py \\
        --repo-name my-app \\
        --unique-id 12345 \\
        --env-name preview \\
        --config /tmp/config.json
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NETWORK_OFFERING_NAME = "Default Guest Network"
DISK_OFFERING_NAME = "data.disk.general"
TEMPLATE_REGEX = re.compile(r"^Ubuntu.*24.*$")
SNAPSHOT_SCHEDULE = "00:06"
SNAPSHOT_MAX = 3
SNAPSHOT_TIMEZONE = "Etc/UTC"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_USERDATA = os.path.join(SCRIPT_DIR, "userdata", "web_vm.sh")
WORKER_USERDATA = os.path.join(SCRIPT_DIR, "userdata", "worker_vm.sh")
ACCESSORY_USERDATA = os.path.join(SCRIPT_DIR, "userdata", "accessory_vm.sh")

# ---------------------------------------------------------------------------
# CloudMonkey helpers
# ---------------------------------------------------------------------------

CMK_MAX_RETRIES = 5


def cmk(*args):
    """Run a cmk command and return parsed JSON.

    Retries up to CMK_MAX_RETRIES times with exponential backoff
    (2, 4, 8, 16, 32s) to handle intermittent CloudStack API errors.
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
            raise RuntimeError(f"cmk {' '.join(args)} failed after {CMK_MAX_RETRIES + 1} attempts: {error_msg}")


def cmk_quiet(*args):
    """Run cmk, return None on error instead of raising."""
    try:
        return cmk(*args)
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def resolve_zone(zone_name):
    """Resolve zone name to ID."""
    data = cmk("list", "zones", f"name={zone_name}", "filter=id,name")
    for z in data.get("zone", []):
        if z["name"] == zone_name:
            return z["id"]
    raise RuntimeError(f"Zone '{zone_name}' not found")


def resolve_all_zone_ids():
    """Resolve all available zone IDs (for snapshot replication)."""
    data = cmk("list", "zones", "filter=id")
    return [z["id"] for z in data.get("zone", [])]


def resolve_network_offering(name):
    """Resolve network offering name to ID."""
    data = cmk("list", "networkofferings", "filter=id,name")
    for no in data.get("networkoffering", []):
        if no["name"] == name:
            return no["id"]
    raise RuntimeError(f"Network offering '{name}' not found")


def resolve_service_offering(name):
    """Resolve service offering name to ID."""
    data = cmk("list", "serviceofferings", "filter=id,name")
    for so in data.get("serviceoffering", []):
        if so["name"] == name:
            return so["id"]
    raise RuntimeError(f"Service offering '{name}' not found")


def resolve_disk_offering(name):
    """Resolve disk offering name to ID."""
    data = cmk("list", "diskofferings", "filter=id,name")
    for do in data.get("diskoffering", []):
        if do["name"] == name:
            return do["id"]
    raise RuntimeError(f"Disk offering '{name}' not found")


def discover_template(zone_id):
    """Discover the Ubuntu 24.x template in the given zone."""
    data = cmk("list", "templates", "templatefilter=featured",
               "keyword=Ubuntu", f"zoneid={zone_id}", "filter=id,name,created")
    matches = []
    seen = set()
    for t in data.get("template", []):
        if TEMPLATE_REGEX.match(t["name"]) and t["id"] not in seen:
            seen.add(t["id"])
            matches.append(t)
    if not matches:
        raise RuntimeError("No Ubuntu template matching ^Ubuntu.*24.*$ found")
    best = sorted(matches, key=lambda t: t["created"], reverse=True)[0]
    print(f"  Template: {best['name']} ({best['id']})")
    return best["id"]


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

def find_network(name, zone_id=None):
    """Find existing network by name, return ID or None."""
    cmd = ["list", "networks", "filter=id,name"]
    if zone_id:
        cmd.append(f"zoneid={zone_id}")
    data = cmk_quiet(*cmd)
    if data:
        for n in data.get("network", []):
            if n["name"] == name:
                return n["id"]
    return None


def find_keypair(name):
    """Find existing SSH key pair by name."""
    data = cmk_quiet("list", "sshkeypairs", f"name={name}")
    return bool(data and data.get("sshkeypair"))


def find_vm(name, zone_id=None, network_id=None):
    """Find existing VM by name, return dict or None."""
    cmd = ["list", "virtualmachines", f"name={name}",
           "filter=id,name,state,serviceofferingid"]
    if zone_id:
        cmd.append(f"zoneid={zone_id}")
    if network_id:
        cmd.append(f"networkid={network_id}")
    data = cmk_quiet(*cmd)
    if data:
        for vm in data.get("virtualmachine", []):
            if vm["name"] == name:
                return vm
    return None


def find_volume(name, zone_id=None):
    """Find existing volume by name, return dict or None."""
    cmd = ["list", "volumes", f"name={name}", "type=DATADISK",
           "filter=id,name,virtualmachineid,state,size"]
    if zone_id:
        cmd.append(f"zoneid={zone_id}")
    data = cmk_quiet(*cmd)
    if data:
        for v in data.get("volume", []):
            if v["name"] == name:
                return v
    return None


def find_public_ips(network_id):
    """Find non-source-NAT public IPs associated with a network."""
    data = cmk_quiet("list", "publicipaddresses",
                     f"associatednetworkid={network_id}",
                     "filter=id,ipaddress,issourcenat")
    ips = []
    if data:
        for ip in data.get("publicipaddress", []):
            if not ip.get("issourcenat", False):
                ips.append(ip)
    return ips


def find_firewall_rules(ip_id):
    """Find firewall rules for an IP."""
    data = cmk_quiet("list", "firewallrules", f"ipaddressid={ip_id}",
                     "filter=id,startport,endport")
    return data.get("firewallrule", []) if data else []


def is_static_nat_enabled(ip_id):
    """Check if static NAT is already enabled for an IP."""
    data = cmk_quiet("list", "publicipaddresses", f"id={ip_id}",
                     "filter=id,isstaticnat,virtualmachineid")
    if data and data.get("publicipaddress"):
        return data["publicipaddress"][0].get("isstaticnat", False)
    return False


# ---------------------------------------------------------------------------
# Userdata helpers
# ---------------------------------------------------------------------------

def encode_userdata(script_path):
    """Read a userdata script and return its base64-encoded content."""
    with open(script_path, "r") as f:
        content = f.read()
    return base64.b64encode(content.encode()).decode()


# ---------------------------------------------------------------------------
# Resource creation helpers
# ---------------------------------------------------------------------------

def deploy_vm(name, offering_id, template_id, zone_id, net_id, keypair_name,
              userdata_path=None):
    """Deploy a VM or return existing one's ID and whether it was scaled.

    Returns (vm_id, scaled) where *scaled* is True when the VM already
    existed but its service offering was changed in-place.

    If userdata_path is provided and the file exists, the script is
    base64-encoded and passed as cloud-init userdata during deployment.
    """
    vm = find_vm(name, zone_id=zone_id, network_id=net_id)
    if vm:
        vm_id = vm["id"]
        current_offering = vm.get("serviceofferingid", "")
        if current_offering and current_offering != offering_id:
            print(f"  Offering changed: {name} ({vm_id})")
            scale_vm(vm_id, name, offering_id, zone_id=zone_id)
            return vm_id, True
        else:
            print(f"  Already exists: {name} ({vm_id})")
        return vm_id, False
    deploy_args = [
        "deploy", "virtualmachine",
        f"serviceofferingid={offering_id}",
        f"templateid={template_id}",
        f"zoneid={zone_id}",
        f"networkids={net_id}",
        f"keypair={keypair_name}",
        f"name={name}",
        f"displayname={name}",
    ]
    if userdata_path and os.path.exists(userdata_path):
        deploy_args.append(f"userdata={encode_userdata(userdata_path)}")
    data = cmk(*deploy_args)
    vm_id = data["virtualmachine"]["id"]
    print(f"  Created: {name} ({vm_id})")
    if userdata_path and os.path.exists(userdata_path):
        print(f"  Userdata: {os.path.basename(userdata_path)} (cloud-init)")
    return vm_id, False


def scale_vm(vm_id, name, new_offering_id, zone_id=None):
    """Scale a VM to a new service offering (in-place).

    Stops the VM first, scales, then starts it again.  Hot (live) scaling
    is not attempted because CloudStack rejects it for fixed service
    offerings, which is always our case.
    """
    print(f"  Stopping VM for offline scale...")
    cmk("stop", "virtualmachine", f"id={vm_id}")
    for _ in range(30):  # wait up to ~150s
        vm = find_vm(name, zone_id=zone_id)
        if vm and vm.get("state") == "Stopped":
            break
        time.sleep(5)
    cmk("scale", "virtualmachine",
        f"id={vm_id}", f"serviceofferingid={new_offering_id}")
    cmk("start", "virtualmachine", f"id={vm_id}")
    # Wait for Running state after start
    for _ in range(30):  # wait up to ~150s
        vm = find_vm(name, zone_id=zone_id)
        if vm and vm.get("state") == "Running":
            break
        time.sleep(5)
    print(f"  Scaled: {name} (offline — stopped, scaled, started)")


def ensure_vm_running(vm_id, name, zone_id=None, timeout=120):
    """Poll until a VM reaches Running state, starting it if Stopped.

    CloudStack may stop a VM when attaching a data disk.  This helper
    ensures the VM is back in Running state before continuing.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        vm = find_vm(name, zone_id=zone_id)
        if vm and vm.get("state") == "Running":
            return
        if vm and vm.get("state") == "Stopped":
            print(f"  VM {name} is Stopped after disk attach, starting...")
            cmk("start", "virtualmachine", f"id={vm_id}")
        time.sleep(5)
    print(f"  Warning: VM {name} did not reach Running state within {timeout}s")


def resize_volume(vol, desired_gb, desc):
    """Resize a volume if needed.  Rejects shrink requests.

    Args:
        vol: Volume dict from find_volume (must include 'size' in bytes).
        desired_gb: Desired size in GB.
        desc: Human-readable description for log messages.
    """
    current_bytes = vol.get("size", 0)
    desired_bytes = desired_gb * (1024 ** 3)
    if desired_bytes > current_bytes:
        cmk("resize", "volume", f"id={vol['id']}", f"size={desired_gb}")
        print(f"    Resized {desc}: {current_bytes // (1024**3)}GB -> {desired_gb}GB")
    elif desired_bytes < current_bytes:
        raise RuntimeError(
            f"Cannot shrink {desc}: current {current_bytes // (1024**3)}GB "
            f"> desired {desired_gb}GB")


def create_disk(disk_name, disk_offering_id, zone_id, size_gb, vm_id,
                network_name, desc):
    """Create, tag, and attach a data disk, or skip if it already exists.

    If the disk already exists but is smaller than size_gb, it is resized
    in-place.  Shrinking is rejected with an error.
    """
    vol = find_volume(disk_name, zone_id)
    if vol:
        vol_id = vol["id"]
        print(f"  {desc}: already exists ({vol_id})")
        resize_volume(vol, size_gb, desc)
        if not vol.get("virtualmachineid"):
            cmk("attach", "volume", f"id={vol_id}",
                f"virtualmachineid={vm_id}")
            print(f"    Attached to VM")
    else:
        data = cmk("create", "volume",
                    f"name={disk_name}",
                    f"diskofferingid={disk_offering_id}",
                    f"zoneid={zone_id}",
                    f"size={size_gb}")
        vol_id = data["volume"]["id"]
        print(f"  {desc}: created ({vol_id})")
        cmk("create", "tags",
            f"resourceids={vol_id}",
            "resourcetype=Volume",
            "tags[0].key=locaweb-cloud-deploy-id",
            f"tags[0].value={network_name}")
        print(f"    Tagged with locaweb-cloud-deploy-id={network_name}")
        cmk("attach", "volume", f"id={vol_id}",
            f"virtualmachineid={vm_id}")
        print(f"    Attached to VM")
    return vol_id


def create_snapshot_policy(vol_id, network_name, snapshot_zoneids, desc):
    """Create daily snapshot policy if one does not already exist."""
    existing = cmk_quiet("list", "snapshotpolicies", f"volumeid={vol_id}")
    if existing and existing.get("snapshotpolicy"):
        print(f"  {desc}: policy already exists")
    else:
        cmk("create", "snapshotpolicy",
            f"volumeid={vol_id}",
            "intervaltype=daily",
            f"schedule={SNAPSHOT_SCHEDULE}",
            f"maxsnaps={SNAPSHOT_MAX}",
            f"timezone={SNAPSHOT_TIMEZONE}",
            f"zoneids={snapshot_zoneids}",
            "tags[0].key=locaweb-cloud-deploy-id",
            f"tags[0].value={network_name}")
        print(f"  {desc}: daily snapshot policy created")


def find_latest_snapshots(network_name, zone_id, accessory_names):
    """Find the latest snapshots for web and accessory volumes.

    Looks for snapshots in the given zone whose volume name matches
    {network_name}-webdata and {network_name}-{name}data.  Returns the
    most recent snapshot of each type.
    """
    data = cmk("list", "snapshots", f"zoneid={zone_id}",
               "filter=id,name,volumename,created,state",
               "snapshottype=MANUAL",
               f"tags[0].key=locaweb-cloud-deploy-id",
               f"tags[0].value={network_name}")
    snapshots = data.get("snapshot", [])

    # Also check recurring (policy-created) snapshots
    data2 = cmk("list", "snapshots", f"zoneid={zone_id}",
                "filter=id,name,volumename,created,state",
                "snapshottype=RECURRING",
                f"tags[0].key=locaweb-cloud-deploy-id",
                f"tags[0].value={network_name}")
    snapshots.extend(data2.get("snapshot", []))

    result = {}

    # Web data disk
    web_vol_name = f"{network_name}-webdata"
    web_snaps = sorted(
        [s for s in snapshots
         if s.get("volumename") == web_vol_name and s.get("state") == "BackedUp"],
        key=lambda s: s["created"], reverse=True)
    if web_snaps:
        result["webdata"] = web_snaps[0]

    # Accessory data disks
    for acc_name in accessory_names:
        vol_name = f"{network_name}-{acc_name}data"
        acc_snaps = sorted(
            [s for s in snapshots
             if s.get("volumename") == vol_name and s.get("state") == "BackedUp"],
            key=lambda s: s["created"], reverse=True)
        if acc_snaps:
            result[f"{acc_name}data"] = acc_snaps[0]

    return result


def recovery_preflight(network_name, zone_id, accessory_names):
    """Run pre-flight checks for disaster recovery into a target zone.

    1. No network with deployment name in target zone
    2. No web/accessory volumes in target zone
    3. Snapshots must exist in target zone

    Returns the snapshot dict for use in disk creation.
    """
    print("\nRunning recovery pre-flight checks...")

    # Check no existing network
    if find_network(network_name, zone_id):
        raise RuntimeError(
            f"Cannot recover: network '{network_name}' already exists in "
            f"target zone. Teardown the existing deployment first.")

    # Check no existing volumes
    web_vol_name = f"{network_name}-webdata"
    if find_volume(web_vol_name, zone_id):
        raise RuntimeError(
            f"Cannot recover: volume '{web_vol_name}' already exists in "
            f"target zone. Teardown the existing deployment first.")
    for acc_name in accessory_names:
        vol_name = f"{network_name}-{acc_name}data"
        if find_volume(vol_name, zone_id):
            raise RuntimeError(
                f"Cannot recover: volume '{vol_name}' already exists in "
                f"target zone. Teardown the existing deployment first.")

    # Find snapshots
    snapshots = find_latest_snapshots(network_name, zone_id, accessory_names)
    if "webdata" not in snapshots:
        raise RuntimeError(
            f"Cannot recover: no web data snapshot found for '{network_name}' "
            f"in target zone. Ensure snapshots have been replicated.")
    for acc_name in accessory_names:
        key = f"{acc_name}data"
        if key not in snapshots:
            raise RuntimeError(
                f"Cannot recover: no {acc_name} data snapshot found for "
                f"'{network_name}' in target zone. Ensure snapshots have "
                f"been replicated.")

    print(f"  Web data snapshot: {snapshots['webdata']['id']} "
          f"(created {snapshots['webdata']['created']})")
    for acc_name in accessory_names:
        key = f"{acc_name}data"
        if key in snapshots:
            print(f"  {acc_name} snapshot: {snapshots[key]['id']} "
                  f"(created {snapshots[key]['created']})")
    print("  Pre-flight checks passed.\n")

    return snapshots


def create_disk_from_snapshot(disk_name, snapshot_id, vm_id, network_name,
                              zone_id, desc):
    """Create a volume from a snapshot, tag it, and attach to VM."""
    data = cmk("create", "volume",
               f"name={disk_name}",
               f"snapshotid={snapshot_id}",
               f"zoneid={zone_id}")
    vol_id = data["volume"]["id"]
    print(f"  {desc}: created from snapshot ({vol_id})")
    cmk("create", "tags",
        f"resourceids={vol_id}",
        "resourcetype=Volume",
        "tags[0].key=locaweb-cloud-deploy-id",
        f"tags[0].value={network_name}")
    print(f"    Tagged with locaweb-cloud-deploy-id={network_name}")
    cmk("attach", "volume", f"id={vol_id}",
        f"virtualmachineid={vm_id}")
    print(f"    Attached to VM")
    return vol_id


def find_public_ip_for_vm(network_id, vm_id):
    """Find the public IP with static NAT pointing to a specific VM."""
    data = cmk_quiet("list", "publicipaddresses",
                     f"associatednetworkid={network_id}",
                     "filter=id,ipaddress,issourcenat,isstaticnat,virtualmachineid")
    if data:
        for ip in data.get("publicipaddress", []):
            if ip.get("virtualmachineid") == vm_id:
                return ip
    return None


def remove_vm_and_ip(vm_name, vm_id, net_id):
    """Remove a VM and its associated public IP, firewall rules, and NAT."""
    ip = find_public_ip_for_vm(net_id, vm_id)
    if ip and not ip.get("issourcenat", False):
        if ip.get("isstaticnat", False):
            cmk("disable", "staticnat", f"ipaddressid={ip['id']}")
            print(f"    Disabled static NAT on {ip['ipaddress']}")
        rules = find_firewall_rules(ip["id"])
        for r in rules:
            cmk("delete", "firewallrule", f"id={r['id']}")
            print(f"    Deleted FW rule on {ip['ipaddress']}")
        cmk("disassociate", "ipaddress", f"id={ip['id']}")
        print(f"    Released {ip['ipaddress']}")
    cmk("destroy", "virtualmachine", f"id={vm_id}", "expunge=true")
    print(f"    Destroyed {vm_name}")


def get_vm_internal_ip(vm_id):
    """Get the internal/private IP of a VM."""
    data = cmk("list", "virtualmachines", f"id={vm_id}", "filter=id,nic")
    return data["virtualmachine"][0]["nic"][0]["ipaddress"]


# ---------------------------------------------------------------------------
# Main provisioning logic
# ---------------------------------------------------------------------------

def provision(config, repo_name, unique_id, env_name, public_key, recover=False):
    """Provision all infrastructure based on the validated config."""
    zone_name = config["zone"]
    web_plan = config["web_plan"]
    web_disk_size_gb = config["web_disk_size_gb"]
    workers_replicas = config.get("workers_replicas", 0)
    accessories = config.get("accessories", [])

    network_name = f"{repo_name}-{unique_id}-{env_name}"
    keypair_name = f"{network_name}-key"
    web_vm_name = "web"
    web_disk_name = f"{network_name}-webdata"

    # Build accessory name list for snapshot/recovery lookups
    accessory_names = [a["name"] for a in accessories]

    results = {"network_name": network_name}

    # Count total public IPs needed
    total_ips = 1  # web always
    total_ips += workers_replicas
    total_ips += len(accessories)

    print(f"\n{'='*60}")
    print(f"Provisioning: {network_name}")
    if recover:
        print(f"Mode: DISASTER RECOVERY (from snapshots)")
    print(f"Zone: {zone_name}")
    print(f"Web: {web_plan} | Web disk: {web_disk_size_gb}GB")
    if workers_replicas > 0:
        print(f"Workers: {workers_replicas}x {config['workers_plan']}")
    for acc in accessories:
        print(f"Accessory '{acc['name']}': {acc['plan']} | Disk: {acc['disk_size_gb']}GB")
    print(f"Total public IPs needed: {total_ips}")
    print(f"{'='*60}\n")

    # --- Resolve all names to IDs ---
    print("Resolving infrastructure names...")
    zone_id = resolve_zone(zone_name)
    all_zone_ids = resolve_all_zone_ids()
    snapshot_zoneids = ",".join(all_zone_ids)
    net_offering_id = resolve_network_offering(NETWORK_OFFERING_NAME)
    disk_offering_id = resolve_disk_offering(DISK_OFFERING_NAME)
    web_offering_id = resolve_service_offering(web_plan)
    template_id = discover_template(zone_id)

    worker_offering_id = None
    if workers_replicas > 0:
        worker_offering_id = resolve_service_offering(config["workers_plan"])

    # Resolve accessory offerings
    acc_offering_ids = {}
    for acc in accessories:
        acc_offering_ids[acc["name"]] = resolve_service_offering(acc["plan"])

    print("  All names resolved.\n")

    # --- Recovery pre-flight ---
    recovery_snapshots = None
    if recover:
        recovery_snapshots = recovery_preflight(network_name, zone_id,
                                                accessory_names)

    # --- Network ---
    print("Creating isolated network...")
    net_id = find_network(network_name, zone_id)
    if net_id:
        print(f"  Already exists: {net_id}")
    else:
        data = cmk("create", "network",
                    f"name={network_name}",
                    f"displaytext={network_name}",
                    f"networkofferingid={net_offering_id}",
                    f"zoneid={zone_id}",
                    f"networkdomain={env_name}.internal")
        net_id = data["network"]["id"]
        print(f"  Created: {net_id}")
    results["network_id"] = net_id

    # --- SSH Key Pair ---
    print("\nRegistering SSH key pair...")
    if find_keypair(keypair_name):
        print(f"  Already exists: {keypair_name}")
    else:
        cmk("register", "sshkeypair",
            f"name={keypair_name}",
            f"publickey={public_key}")
        print(f"  Registered: {keypair_name}")
    results["keypair_name"] = keypair_name

    # --- Deploy VMs ---
    print("\nDeploying web VM...")
    web_vm_id, _ = deploy_vm(web_vm_name, web_offering_id, template_id,
                             zone_id, net_id, keypair_name,
                             userdata_path=WEB_USERDATA)
    results["web_vm_id"] = web_vm_id

    worker_vm_ids = []
    if workers_replicas > 0:
        print(f"\nDeploying {workers_replicas} worker VM(s)...")
        for i in range(1, workers_replicas + 1):
            worker_name = f"worker-{i}"
            wid, _ = deploy_vm(worker_name, worker_offering_id, template_id,
                              zone_id, net_id, keypair_name,
                              userdata_path=WORKER_USERDATA)
            worker_vm_ids.append(wid)
        results["worker_vm_ids"] = worker_vm_ids

    # --- Deploy accessory VMs ---
    acc_results = {}
    for acc in accessories:
        acc_name = acc["name"]
        acc_vm_name = acc_name
        print(f"\nDeploying accessory VM '{acc_name}'...")
        acc_vm_id, acc_vm_scaled = deploy_vm(
            acc_vm_name, acc_offering_ids[acc_name], template_id,
            zone_id, net_id, keypair_name,
            userdata_path=ACCESSORY_USERDATA)
        acc_results[acc_name] = {
            "vm_id": acc_vm_id,
            "vm_scaled": acc_vm_scaled,
        }

    # --- Scale down excess workers ---
    desired_workers = workers_replicas
    print("\nChecking for excess workers...")
    excess_idx = desired_workers + 1
    removed = 0
    while True:
        worker_name = f"worker-{excess_idx}"
        vm = find_vm(worker_name, zone_id=zone_id, network_id=net_id)
        if not vm:
            break
        print(f"  Removing: {worker_name}")
        remove_vm_and_ip(worker_name, vm["id"], net_id)
        removed += 1
        excess_idx += 1
    if removed == 0:
        print("  No excess workers found.")
    else:
        print(f"  Removed {removed} excess worker(s).")

    # --- Public IPs & Static NAT ---
    print("\nAssigning public IPs...")

    # Build ordered list of VMs needing IPs
    vm_assignments = [("Web", web_vm_id)]
    if workers_replicas > 0:
        for i, wid in enumerate(worker_vm_ids, 1):
            vm_assignments.append((f"Worker {i}", wid))
    for acc in accessories:
        vm_assignments.append((f"Accessory ({acc['name']})", acc_results[acc["name"]]["vm_id"]))

    # Check existing static NAT assignments
    ip_map = {}  # vm_id -> ip_obj
    for label, vm_id in vm_assignments:
        existing_ip = find_public_ip_for_vm(net_id, vm_id)
        if existing_ip:
            ip_map[vm_id] = existing_ip
            print(f"  {label}: reusing {existing_ip['ipaddress']}")

    # Find unassigned existing non-source-NAT IPs
    all_existing = find_public_ips(net_id)
    assigned_ip_ids = {ip["id"] for ip in ip_map.values()}
    unassigned = [ip for ip in all_existing if ip["id"] not in assigned_ip_ids]

    # Acquire additional IPs if needed
    vms_needing_ips = [(l, vid) for l, vid in vm_assignments
                       if vid not in ip_map]
    new_needed = len(vms_needing_ips) - len(unassigned)
    if new_needed > 0:
        for _ in range(new_needed):
            data = cmk("associate", "ipaddress", f"networkid={net_id}")
            unassigned.append(data["ipaddress"])
        print(f"  Acquired {new_needed} new IP(s)")

    # Assign unassigned IPs to VMs that need them + enable static NAT
    ui = 0
    for label, vm_id in vms_needing_ips:
        ip_obj = unassigned[ui]; ui += 1
        ip_map[vm_id] = ip_obj
        cmk("enable", "staticnat",
            f"ipaddressid={ip_obj['id']}",
            f"virtualmachineid={vm_id}")
        print(f"  {label}: assigned {ip_obj['ipaddress']}")

    # Build results from ip_map
    web_ip = ip_map[web_vm_id]
    print(f"  Web IP: {web_ip['ipaddress']}")
    results["web_ip"] = web_ip["ipaddress"]
    results["web_ip_id"] = web_ip["id"]

    worker_ips = []
    if workers_replicas > 0:
        for i, wid in enumerate(worker_vm_ids, 1):
            wip = ip_map[wid]
            worker_ips.append(wip)
            print(f"  Worker {i} IP: {wip['ipaddress']}")
        results["worker_ips"] = [ip["ipaddress"] for ip in worker_ips]

    for acc in accessories:
        acc_name = acc["name"]
        acc_vm_id = acc_results[acc_name]["vm_id"]
        acc_ip = ip_map[acc_vm_id]
        print(f"  {acc_name} IP: {acc_ip['ipaddress']}")
        acc_results[acc_name]["ip"] = acc_ip["ipaddress"]
        acc_results[acc_name]["ip_id"] = acc_ip["id"]

    # --- Firewall Rules ---
    print("\nCreating firewall rules...")
    fw_rules = [
        (web_ip["id"], 22, 22, "SSH (web)"),
        (web_ip["id"], 80, 80, "HTTP (web)"),
        (web_ip["id"], 443, 443, "HTTPS (web)"),
    ]
    if workers_replicas > 0:
        for i, wip in enumerate(worker_ips, 1):
            fw_rules.append((wip["id"], 22, 22, f"SSH (worker-{i})"))
    for acc in accessories:
        acc_name = acc["name"]
        fw_rules.append((acc_results[acc_name]["ip_id"], 22, 22,
                         f"SSH ({acc_name})"))

    for ip_id, start, end, desc in fw_rules:
        existing = find_firewall_rules(ip_id)
        already = any(
            int(r.get("startport", 0)) == start and int(r.get("endport", 0)) == end
            for r in existing
        )
        if already:
            print(f"  {desc} ({start}-{end}): already exists")
        else:
            cmk("create", "firewallrule",
                f"ipaddressid={ip_id}", "protocol=TCP",
                f"startport={start}", f"endport={end}",
                "cidrlist=0.0.0.0/0")
            print(f"  {desc} ({start}-{end}): created")

    # --- Data Disks ---
    # Track VMs that may need restart after disk attachment (CloudStack may
    # stop a VM during hot-attach).
    vms_to_check = []

    if recover:
        print("\nRecovering data disks from snapshots...")
        web_vol_id = create_disk_from_snapshot(
            web_disk_name, recovery_snapshots["webdata"]["id"],
            web_vm_id, network_name, zone_id, "Web data disk")
        results["web_volume_id"] = web_vol_id
        vms_to_check.append((web_vm_id, web_vm_name))

        for acc in accessories:
            acc_name = acc["name"]
            acc_disk_name = f"{network_name}-{acc_name}data"
            acc_vol_id = create_disk_from_snapshot(
                acc_disk_name, recovery_snapshots[f"{acc_name}data"]["id"],
                acc_results[acc_name]["vm_id"], network_name, zone_id,
                f"{acc_name} data disk")
            acc_results[acc_name]["volume_id"] = acc_vol_id
            vms_to_check.append((acc_results[acc_name]["vm_id"], acc_name))
    else:
        print("\nCreating data disks...")
        web_vol_id = create_disk(web_disk_name, disk_offering_id, zone_id,
                                  web_disk_size_gb, web_vm_id,
                                  network_name, "Web data disk")
        results["web_volume_id"] = web_vol_id
        vms_to_check.append((web_vm_id, web_vm_name))

        for acc in accessories:
            acc_name = acc["name"]
            acc_disk_name = f"{network_name}-{acc_name}data"
            acc_vol_id = create_disk(acc_disk_name, disk_offering_id, zone_id,
                                     acc["disk_size_gb"],
                                     acc_results[acc_name]["vm_id"],
                                     network_name, f"{acc_name} data disk")
            acc_results[acc_name]["volume_id"] = acc_vol_id
            vms_to_check.append((acc_results[acc_name]["vm_id"], acc_name))

    # Ensure all VMs with attached disks are Running
    print("\nEnsuring VMs are Running after disk attach...")
    for vm_id, vm_name in vms_to_check:
        ensure_vm_running(vm_id, vm_name, zone_id=zone_id)

    # --- Snapshot Policies ---
    print("\nCreating snapshot policies...")
    create_snapshot_policy(web_vol_id, network_name, snapshot_zoneids, "Web data disk")
    for acc in accessories:
        acc_name = acc["name"]
        create_snapshot_policy(acc_results[acc_name]["volume_id"],
                               network_name, snapshot_zoneids,
                               f"{acc_name} data disk")

    # --- Internal IPs ---
    print("\nRetrieving internal IPs...")
    results["web_internal_ip"] = get_vm_internal_ip(web_vm_id)
    print(f"  Web: {results['web_internal_ip']}")

    if workers_replicas > 0:
        results["worker_internal_ips"] = []
        for i, wid in enumerate(worker_vm_ids, 1):
            wip = get_vm_internal_ip(wid)
            results["worker_internal_ips"].append(wip)
            print(f"  Worker {i}: {wip}")

    # Store accessories in output
    if acc_results:
        results["accessories"] = acc_results

    # --- Summary ---
    print(f"\n{'='*60}")
    print("Provisioning complete!")
    print(f"{'='*60}")
    print(f"  Network:      {network_name} ({net_id})")
    print(f"  SSH Key Pair: {keypair_name}")
    print(f"  Web VM:       {web_vm_name} -> {web_ip['ipaddress']}")
    if workers_replicas > 0:
        for i in range(workers_replicas):
            print(f"  Worker {i+1} VM:  worker-{i+1} -> {worker_ips[i]['ipaddress']}")
    for acc in accessories:
        acc_name = acc["name"]
        print(f"  {acc_name} VM:    {acc_name} -> {acc_results[acc_name]['ip']}")
    print(f"{'='*60}\n")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Provision CloudStack infrastructure from a validated config")
    parser.add_argument("--repo-name", required=True,
                        help="Repository name")
    parser.add_argument("--unique-id", required=True,
                        help="Unique identifier (repository ID)")
    parser.add_argument("--env-name", default="preview",
                        help="Environment name (default: preview)")
    parser.add_argument("--config", required=True,
                        help="Path to validated JSON config file")
    parser.add_argument("--public-key", required=True,
                        help="Path to SSH public key file")
    parser.add_argument("--output", default=None,
                        help="Path to write JSON output (default: stdout)")
    parser.add_argument("--recover", action="store_true",
                        help="Recover from snapshots (disaster recovery)")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    with open(args.public_key) as f:
        public_key = f.read().strip()

    try:
        results = provision(config, repo_name=args.repo_name,
                            unique_id=args.unique_id,
                            env_name=args.env_name, public_key=public_key,
                            recover=args.recover)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"\nOutput written to {args.output}")
        else:
            json.dump(results, sys.stdout, indent=2)
            print()
    except RuntimeError as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
